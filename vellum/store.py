"""The index: SQLite schema, incremental re-indexing, and search.

Design notes that matter more than the SQL:

**The archive is replicated.**  Measured over one user's archive: 378 files,
1.66 GB, 806,759 JSON records -- but only 72,915 distinct ``uuid`` values.
Claude Code writes a *new* session file every time a conversation is rewound
or branched, copying the messages it inherited with their original UUIDs under
a new ``sessionId``.  57% of UUIDs appear in more than one session, and one
appears in 56.  So the bytes on disk are ~11x the actual conversation.

Two consequences drive this whole module:

1.  A session is identified by its *content*, not by its file.  ``fingerprint``
    is a hash over the message UUIDs.  Sessions that share a fingerprint are
    forks of each other, and the index keeps one canonical copy and records
    the rest as aliases.  Without this, search returns the same conversation
    up to 56 times and every count in the UI is wrong.
2.  The list must show 324 sessions, not 378 files, and the "archive size"
    figure should report unique bytes.

**Full text lives in the messages table, not only in FTS.**  FTS5 external
content is faster, but it makes the index the *only* way to read a message
back -- so a rebuild that goes wrong silently empties the reader, and export
cannot work off the index at all.  Correctness beats the space.

**FTS is a word tokenizer, not trigram.**  This was measured, not assumed.  On
a 44,473-message sample from this archive, building a trigram index took
19.0s versus 1.7s for ``unicode61`` -- 11x slower -- and the entire build is
paid on the first launch.  In exchange, trigram found exactly one additional
case out of eight (an underscored compound), because a word tokenizer with
``tokenchars '_-'`` already handles most of what trigram is bought for.
A 2-character trigram also matches nearly the whole corpus, which is a
1.1-second query.  Word-prefix search is what every code search does, and it
buys an 11x faster first index.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from typing import Any, Callable, Iterable

SCHEMA_VERSION = 5

# Bookkeeping records. These carry no conversation content; dropping them
# silently is correct and keeps the reader clean.
_SILENT_TYPES = {
    "file-history-snapshot", "file-history-delta", "attachment", "atis-latch",
    "queue-operation", "mode", "agent-name", "relocated", "pr-link",
    "cost-state", "system",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);

CREATE TABLE IF NOT EXISTS sessions (
    id             TEXT PRIMARY KEY,
    parent_id      TEXT,
    kind           TEXT NOT NULL,          -- main | subagent
    project        TEXT NOT NULL,
    path           TEXT NOT NULL DEFAULT '',
    title          TEXT NOT NULL,
    custom_title   TEXT,
    ai_title       TEXT,
    cwd            TEXT,
    git_branch     TEXT,
    version        TEXT,
    model          TEXT,
    first_ts       TEXT,
    last_ts        TEXT,
    bytes          INTEGER NOT NULL DEFAULT 0,
    mtime          REAL    NOT NULL DEFAULT 0,
    n_turns        INTEGER NOT NULL DEFAULT 0,
    n_user         INTEGER NOT NULL DEFAULT 0,
    n_assistant    INTEGER NOT NULL DEFAULT 0,
    n_tool         INTEGER NOT NULL DEFAULT 0,
    in_tok         INTEGER NOT NULL DEFAULT 0,
    out_tok        INTEGER NOT NULL DEFAULT 0,
    cache_read     INTEGER NOT NULL DEFAULT 0,
    cache_create   INTEGER NOT NULL DEFAULT 0,
    thinking_tok   INTEGER NOT NULL DEFAULT 0,
    preview        TEXT NOT NULL DEFAULT '',
    ribbon         TEXT NOT NULL DEFAULT '[]',
    duration_s     REAL    NOT NULL DEFAULT 0,
    compact_count  INTEGER NOT NULL DEFAULT 0,
    error_count    INTEGER NOT NULL DEFAULT 0,
    fingerprint    TEXT,                   -- hash of the message UUID set
    canonical      TEXT,                   -- id of the session this is a fork of
    is_fork        INTEGER NOT NULL DEFAULT 0,
    pinned         INTEGER NOT NULL DEFAULT 0,
    read_state     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS sessions_project ON sessions(project);
CREATE INDEX IF NOT EXISTS sessions_last    ON sessions(last_ts DESC);
CREATE INDEX IF NOT EXISTS sessions_finger  ON sessions(fingerprint);

CREATE TABLE IF NOT EXISTS messages (
    session_id  TEXT NOT NULL,
    ordinal     INTEGER NOT NULL,
    role        TEXT NOT NULL,
    text        TEXT NOT NULL DEFAULT '',
    ts          TEXT,
    model       TEXT,
    tool        TEXT,
    label       TEXT,
    in_tok      INTEGER NOT NULL DEFAULT 0,
    out_tok     INTEGER NOT NULL DEFAULT 0,
    cache_tok   INTEGER NOT NULL DEFAULT 0,
    sidechain   INTEGER NOT NULL DEFAULT 0,
    images      INTEGER NOT NULL DEFAULT 0,
    duration_s  REAL,
    uuid        TEXT,
    PRIMARY KEY (session_id, ordinal)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS messages_uuid ON messages(uuid);

CREATE VIRTUAL TABLE IF NOT EXISTS msg_fts USING fts5(
    text, label, tool,
    session_id UNINDEXED, ordinal UNINDEXED, role UNINDEXED,
    tokenize = "unicode61 remove_diacritics 2 tokenchars '_-'"
);

-- Per-session tool frequency, so the reader and insights can show what a
-- conversation was *about* without re-walking the messages.
CREATE TABLE IF NOT EXISTS tool_freq (
    session_id TEXT NOT NULL, tool TEXT NOT NULL, n INTEGER NOT NULL,
    PRIMARY KEY (session_id, tool)
) WITHOUT ROWID;
"""

# Shortest query we will send to FTS as a phrase. Below this a prefix match
# is too broad to rank usefully, so short terms fall back to title matching.
MIN_PHRASE = 3

# Which message roles are searchable, and how much of a tool payload is
# indexed. Thinking is excluded: 37k blocks of internal deliberation that
# nobody searches for, and it would double the index.
#
# The tool prefix was chosen by measuring both sides of the trade on this
# archive: indexing all 299 MB of tool text produces an 807 MB FTS index,
# while a 600-character prefix produces 386 MB. A result snippet only ever
# shows ~14 tokens and a `cat` of a source file puts the useful line in the
# first few hundred characters, so the smaller index loses nothing a reader
# can act on. The full text is still stored verbatim in `messages`.
FTS_ROLES = ("user", "assistant", "tool", "result", "attachment")
FTS_TOOL_PREFIX = 600


def connect(db_path: str, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    else:
        con = sqlite3.connect(db_path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    # WAL lets the reader query while the indexer writes. Without it, a
    # re-index blocks the UI for the whole corpus.
    if not read_only:
        con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA mmap_size=268435456")  # 256 MB
    con.execute("PRAGMA cache_size=-65536")     # 64 MB
    con.execute("PRAGMA foreign_keys=ON")
    return con


def open_db(db_path: str) -> sqlite3.Connection:
    con = connect(db_path)
    _migrate(con)
    return con


def _migrate(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    row = con.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
    if row is None:
        con.execute("INSERT INTO meta(k,v) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
    elif int(row["v"]) != SCHEMA_VERSION:
        # The layout changed; a partial migration is worse than a rebuild, and
        # the rebuild is cheap because the source is the JSONL, not this file.
        for table in ("msg_fts", "tool_freq", "messages", "sessions"):
            con.execute(f"DROP TABLE IF EXISTS {table}")
        con.executescript(SCHEMA)
        con.execute("UPDATE meta SET v=? WHERE k='schema_version'", (str(SCHEMA_VERSION),))
    con.commit()


# --------------------------------------------------------------------------
# Fingerprinting and fork detection
# --------------------------------------------------------------------------


def session_fingerprint(messages: Iterable) -> str:
    """A content hash for a session: the set of its message UUIDs.

    Order-independent (a rewind can re-order relative to a sibling fork) but
    sensitive to added or removed messages, which is exactly the signal for
    "these two are not the same conversation".
    """
    h = hashlib.blake2b(digest_size=16)
    uuids = sorted({m.uuid for m in messages if m.uuid})
    if not uuids:
        # A session with no uuids still needs a stable identity; fall back to
        # a hash of its text so two identical untitled files still collapse.
        for m in messages:
            h.update((m.text or "").encode("utf-8", "ignore")[:200])
    else:
        for u in uuids:
            h.update(u.encode("ascii"))
    return h.hexdigest()


def session_uuids(messages: Iterable) -> list[str]:
    """The message UUIDs in order -- the sequence a rewind extends."""
    return [m.uuid for m in messages if m.uuid]


def _prefix_depth(a: list[str], b: list[str]) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


# Below this, two sessions sharing a prefix have shared a system prompt, not
# a conversation.
MIN_REWIND_MESSAGES = 3


def find_rewinds(sequences: list[tuple[str, list[str]]],
                 threshold: float = 0.9) -> list[tuple[str, str]]:
    """Pair each session with the longest session it shares a prefix with.

    This is the correction that matters. Hashing a session's whole UUID set
    finds only *identical* conversations, and a rewind is not identical: it
    shares a long opening and then diverges where the user rewound.
    Measured on this archive, two sessions of 5,231 and 5,226 messages match
    for 5,224 and then differ, and share no fingerprint at all -- so
    set-hashing collapsed 1 of 390 sessions while every other near-duplicate
    still appeared in the list as its own row.

    So the measure is the longest common prefix as a fraction of the shorter
    session, thresholded high.  It has to be high: a shared system prompt is
    not a shared conversation, and a low threshold merges genuinely different
    work that happened to start the same way.

    Each session is assigned to exactly one parent, and chains (A <- B <- C)
    resolve to the root, so the list shows one row per real conversation
    rather than one row per rewind.
    """
    ordered = sorted(sequences, key=lambda kv: -len(kv[1]))
    parent: dict[str, str] = {}
    for i, (sid, seq) in enumerate(ordered):
        if not seq:
            continue
        best: tuple[int, str] | None = None
        for oid, oseq in ordered[:i]:
            if not oseq:
                continue
            shorter = min(len(seq), len(oseq))
            if shorter < MIN_REWIND_MESSAGES:
                continue
            depth = _prefix_depth(seq, oseq)
            if shorter and depth / shorter < threshold:
                continue
            # Prefer the closest ancestor, not merely the first one found.
            if best is None or depth > best[0]:
                best = (depth, oid)
        if best is not None:
            parent[sid] = best[1]

    def root(sid: str) -> str:
        seen = set()
        while sid in parent and sid not in seen:
            seen.add(sid)
            sid = parent[sid]
        return sid

    return [(sid, root(sid)) for sid in parent if root(sid) != sid]


def _resolve_forks(con: sqlite3.Connection) -> None:
    """Collapse sessions that are rewinds of an earlier one.

    A rewind rewrites the conversation into a new file, so the naive view --
    one row per file -- shows the same conversation several times. The
    canonical row is the *longest* version, and the shorter rewinds become
    aliases pointing at it. :func:`session_forks` still exposes them, because
    knowing a conversation was rewound three times is itself useful.
    """
    con.execute("UPDATE sessions SET is_fork=0, canonical=NULL")
    seqs: dict[str, list[str]] = {}
    for r in con.execute(
        "SELECT m.session_id, m.uuid FROM messages m "
        "JOIN sessions s ON s.id = m.session_id "
        "WHERE m.uuid IS NOT NULL ORDER BY m.session_id, m.ordinal"
    ):
        seqs.setdefault(r["session_id"], []).append(r["uuid"])
    if len(seqs) < 2:
        return
    pairs = find_rewinds(list(seqs.items()))
    for child, parent in pairs:
        if child == parent:
            continue
        con.execute(
            "UPDATE sessions SET is_fork=1, canonical=? WHERE id=?", (parent, child)
        )


def session_forks(con: sqlite3.Connection, session_id: str) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT id, kind, mtime, bytes, n_turns FROM sessions "
        "WHERE canonical=? ORDER BY mtime",
        (session_id,),
    ).fetchall()


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


def _put_session(con: sqlite3.Connection, ps: Any, *, fts: bool = True) -> None:
    old = con.execute("SELECT id FROM sessions WHERE id=?", (ps.id,)).fetchone()
    if old:
        con.execute("DELETE FROM msg_fts WHERE session_id=?", (ps.id,))
        con.execute("DELETE FROM messages WHERE session_id=?", (ps.id,))
        con.execute("DELETE FROM tool_freq WHERE session_id=?", (ps.id,))

    con.execute(
        """INSERT OR REPLACE INTO sessions
           (id,parent_id,kind,project,path,title,custom_title,ai_title,cwd,git_branch,
            version,model,first_ts,last_ts,bytes,mtime,n_turns,n_user,n_assistant,
            n_tool,in_tok,out_tok,cache_read,cache_create,thinking_tok,preview,
            ribbon,duration_s,compact_count,error_count,fingerprint)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            ps.id, ps.parent_id, ps.kind, ps.project, ps.path, ps.title or "Untitled",
            ps.custom_title, ps.ai_title, ps.cwd, ps.git_branch, ps.version,
            ps.model, ps.first_ts, ps.last_ts, ps.bytes, ps.mtime, ps.n_turns,
            ps.n_user, ps.n_assistant, ps.n_tool, ps.in_tok, ps.out_tok,
            getattr(ps, "cache_read", 0), getattr(ps, "cache_create", 0),
            getattr(ps, "thinking_tok", 0), ps.preview,
            json.dumps(ps.ribbon), ps.duration_s, ps.compact_count, ps.error_count,
            session_fingerprint(ps.messages),
        ),
    )

    tool_counts: dict[str, int] = {}
    rows, fts_rows = [], []
    for m in ps.messages:
        rows.append((
            ps.id, m.ordinal, m.role, m.text, m.ts, m.model, m.tool, m.label,
            m.in_tok, m.out_tok, m.cache_tok, int(bool(m.sidechain)), m.images,
            m.duration_s, m.uuid,
        ))
        # A tool result is stored under its tool name so a search for "Bash"
        # finds both the call and what it printed.
        if m.tool and m.role in ("tool", "result"):
            tool_counts[m.tool] = tool_counts.get(m.tool, 0) + 1
        if fts and m.role in FTS_ROLES:
            # Tool payloads are stored verbatim in `messages` for the reader,
            # but only a prefix goes into FTS. Measured on this archive,
            # indexing all of them costs ~800 MB of duplicate text; a 2,000
            # character prefix per tool call keeps the index at a few hundred
            # MB and still finds the line you are looking for, because the
            # informative part of a tool result is near the top.
            text = m.text or ""
            fts_rows.append((
                text[:FTS_TOOL_PREFIX] if m.role in ("tool", "result") else text,
                m.label or "", m.tool or "", ps.id, m.ordinal, m.role,
            ))
    con.executemany(
        "INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
    )
    if fts_rows:
        con.executemany("INSERT INTO msg_fts VALUES (?,?,?,?,?,?)", fts_rows)
    con.executemany(
        "INSERT INTO tool_freq VALUES (?,?,?)",
        [(ps.id, t, n) for t, n in tool_counts.items()],
    )


def delete_session(con: sqlite3.Connection, session_id: str) -> None:
    con.execute("DELETE FROM msg_fts WHERE session_id=?", (session_id,))
    con.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
    con.execute("DELETE FROM tool_freq WHERE session_id=?", (session_id,))
    con.execute("DELETE FROM sessions WHERE id=?", (session_id,))


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


_SORTS = {
    "recent": "COALESCE(last_ts,'') DESC, mtime DESC",
    "oldest": "COALESCE(last_ts,'') ASC, mtime ASC",
    "turns": "n_turns DESC, COALESCE(last_ts,'') DESC",
    "size": "bytes DESC, COALESCE(last_ts,'') DESC",
}


def list_sessions(
    con: sqlite3.Connection,
    *,
    project: str | None = None,
    include_forks: bool = False,
    sort: str = "recent",
    limit: int = 500,
    offset: int = 0,
) -> list[sqlite3.Row]:
    where, args = [], []
    if project:
        where.append("project = ?")
        args.append(project)
    if not include_forks:
        where.append("is_fork = 0")
    sql = "SELECT * FROM sessions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    # An untrusted sort key would be an injection point, so it is looked up in
    # a fixed table rather than interpolated.
    order = _SORTS.get(sort, _SORTS["recent"])
    sql += f" ORDER BY {order} LIMIT ? OFFSET ?"
    args += [limit, offset]
    return con.execute(sql, args).fetchall()


def session(con: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()


def messages(
    con: sqlite3.Connection,
    session_id: str,
    *,
    include_sidechains: bool = False,
    include_thinking: bool = True,
    include_tools: bool = True,
    limit: int | None = None,
    offset: int = 0,
) -> list[sqlite3.Row]:
    where = ["session_id = ?"]
    args: list[Any] = [session_id]
    if not include_sidechains:
        where.append("sidechain = 0")
    if not include_thinking:
        where.append("role != 'thinking'")
    if not include_tools:
        where.append("role NOT IN ('tool','result')")
    sql = f"SELECT * FROM messages WHERE {' AND '.join(where)} ORDER BY ordinal"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        args += [limit, offset]
    return con.execute(sql, args).fetchall()


def projects(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT project, COUNT(*) n, COALESCE(SUM(bytes),0) bytes "
        "FROM sessions WHERE is_fork=0 GROUP BY project ORDER BY n DESC"
    ).fetchall()


def stats(con: sqlite3.Connection) -> dict:
    s = con.execute(
        """SELECT COUNT(*) total,
                  COALESCE(SUM(is_fork),0) forks,
                  COALESCE(SUM(n_turns),0) turns,
                  COALESCE(SUM(n_tool),0) tools,
                  COALESCE(SUM(in_tok),0) in_tok,
                  COALESCE(SUM(out_tok),0) out_tok,
                  COALESCE(SUM(cache_read),0) cache_read,
                  COALESCE(SUM(cache_create),0) cache_create,
                  COALESCE(SUM(bytes),0) bytes
           FROM sessions"""
    ).fetchone()
    uniq = con.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(bytes),0) bytes FROM sessions WHERE is_fork=0"
    ).fetchone()
    models = con.execute(
        "SELECT model, COUNT(*) n FROM sessions WHERE model IS NOT NULL AND is_fork=0 "
        "GROUP BY model ORDER BY n DESC"
    ).fetchall()
    return {
        "sessions": uniq["n"], "forks": s["forks"],
        "turns": s["turns"], "tools": s["tools"],
        "in_tok": s["in_tok"], "out_tok": s["out_tok"],
        "cache_read": s["cache_read"], "cache_create": s["cache_create"],
        "bytes_on_disk": s["bytes"], "bytes_unique": uniq["bytes"],
        "models": [(m["model"], m["n"]) for m in models],
    }


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------


def _fts_query(q: str) -> str | None:
    """Turn a user query into a safe FTS5 MATCH expression.

    Every term is double-quoted, which is what makes arbitrary user input
    safe to interpolate -- quotes and FTS operators inside are neutralised.
    Terms of 3+ characters get a ``*`` so results appear while you type;
    shorter ones are quoted phrases, which still match exactly.
    """
    terms = []
    for tok in q.split():
        tok = tok.replace('"', "")
        if not tok:
            continue
        terms.append(f'"{tok}"*' if len(tok) >= MIN_PHRASE else f'"{tok}"')
    return " AND ".join(terms) or None


def _search_titles(
    con: sqlite3.Connection,
    q: str,
    *,
    project: str | None = None,
    since: str | None = None,
    until: str | None = None,
    model: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """Fallback for one- and two-character queries: match titles and previews.

    Still filtered by the same project/date/model predicates so the fallback
    does not quietly ignore an active filter.
    """
    where = ["is_fork = 0"]
    args: list[Any] = []
    if project:
        where.append("project = ?")
        args.append(project)
    if since:
        where.append("COALESCE(last_ts,'') >= ?")
        args.append(since)
    if until:
        where.append("COALESCE(last_ts,'') <= ?")
        args.append(until)
    if model:
        where.append("model = ?")
        args.append(model)
    # The pattern is bound as a parameter, so it cannot inject SQL, but LIKE
    # wildcards inside it are still interpreted: a query of "a_b" would match
    # "axb". ESCAPE '\' is declared in the SQL, so escaping them here is what
    # makes a search mean what the user typed.
    like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    rows = con.execute(
        f"""SELECT id, title, project, last_ts FROM sessions
            WHERE {' AND '.join(where)}
              AND (title LIKE ? ESCAPE '\\' OR preview LIKE ? ESCAPE '\\')
            ORDER BY COALESCE(last_ts,'') DESC LIMIT ?""",
        args + [like, like, limit],
    ).fetchall()
    return [
        {
            "session_id": r["id"], "title": r["title"], "project": r["project"],
            "last_ts": r["last_ts"], "hits": 1, "ordinal": 0, "role": "",
            "tool": None, "snippet": r["title"],
        }
        for r in rows
    ]


def search(
    con: sqlite3.Connection,
    q: str,
    *,
    project: str | None = None,
    since: str | None = None,
    until: str | None = None,
    role: str | None = None,
    model: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """Full-text search across messages, grouped by conversation.

    Returns one row per conversation with a highlighted snippet, ranked by
    BM25.  Queries shorter than three characters fall back to matching
    conversation titles, because a two-letter prefix matches most of the
    corpus and cannot be ranked into anything useful.
    """
    q = (q or "").strip()
    if not q:
        return []
    if len(q) < MIN_PHRASE:
        return _search_titles(con, q, project=project, since=since,
                              until=until, model=model, limit=limit)

    where = ["s.is_fork = 0"]
    sparams: list[Any] = []
    if project:
        where.append("s.project = ?")
        sparams.append(project)
    if since:
        where.append("COALESCE(s.last_ts,'') >= ?")
        sparams.append(since)
    if until:
        where.append("COALESCE(s.last_ts,'') <= ?")
        sparams.append(until)
    if model:
        where.append("s.model = ?")
        sparams.append(model)
    sjoin = " AND ".join(where)

    match = _fts_query(q)
    role_clause = " AND f.role = ?" if role else ""
    # Two stages, and the first has to be per *conversation*, not per message.
    # Ranking messages and grouping afterwards meant one chatty conversation
    # filled the whole candidate window: measured on this archive, "the"
    # matched 394 conversations but returned 56, because 480 message rows from
    # the few longest sessions crowded out the rest -- and a conversation with
    # 1,083 matches was dropped entirely despite ranking well.
    #
    # Grouping first gives one row per conversation (best score, real count),
    # so the window is spent on distinct conversations.
    #
    # bm25() and snippet() are FTS helpers and only work where the MATCH is in
    # the same query block, so the snippet is a second pass over just the
    # conversations that won -- bounded by the page size.
    #
    # The snippet delimiters are ASCII control characters, not guillemets: a
    # transcript is full of fetched web text, and one stray '<' in the indexed
    # content would otherwise open a <mark> that never closes.
    # `rank` is the bm25 of the best row in the group, and unlike bm25() it is
    # allowed alongside GROUP BY -- bm25() itself raises "unable to use
    # function bm25 in the requested context" the moment it is aggregated.
    sql = f"""
        SELECT s.id AS session_id, rank AS score,
               COUNT(*) AS hits, MIN(f.ordinal) AS first_ordinal
        FROM msg_fts f
        JOIN sessions s ON s.id = f.session_id
        WHERE msg_fts MATCH ? AND {sjoin}{role_clause}
        GROUP BY s.id
        ORDER BY score
        LIMIT ?
    """
    args = [match] + sparams + ([role] if role else []) + [limit]
    try:
        rows = con.execute(sql, args).fetchall()
    except sqlite3.OperationalError as exc:
        # Only a malformed MATCH should degrade to "no results". Anything else
        # is a real bug and must not be silently swallowed -- an earlier
        # version caught this broadly and hid a bm25 misplacement for a day.
        if "fts5" not in str(exc).lower() and "malformed" not in str(exc).lower():
            raise
        return []
    if not rows:
        return []

    ids = [r["session_id"] for r in rows]
    placeholders = ",".join("?" * len(ids))
    # One query for every winning conversation, not one per conversation.
    # `session_id` is UNINDEXED, so a per-session lookup re-scans the whole
    # match set: measured at 647ms each, which made a 100-result page take
    # 65 seconds. Fetching them together is one pass and about 3s, and the
    # first row per session after ordering by rank is that session's best
    # match.
    best: dict[str, sqlite3.Row] = {}
    if ids:
        for r in con.execute(
            f"""SELECT session_id, ordinal, role, tool,
                       snippet(msg_fts, 0, char(2), char(3), '?', 14) AS snip,
                       rank
                FROM msg_fts
                WHERE session_id IN ({placeholders}) AND msg_fts MATCH ?{role_clause}
                ORDER BY session_id, rank""",
            ids + [match] + ([role] if role else []),
        ):
            best.setdefault(r["session_id"], r)
    meta = {
        r["id"]: r
        for r in con.execute(
            f"SELECT id, title, project, last_ts FROM sessions WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
    }
    return [
        {
            "session_id": r["session_id"],
            "title": meta[r["session_id"]]["title"] if r["session_id"] in meta else "",
            "project": meta[r["session_id"]]["project"] if r["session_id"] in meta else "",
            "last_ts": meta[r["session_id"]]["last_ts"] if r["session_id"] in meta else "",
            # A real count of matching messages, not a sample of the first N.
            "hits": r["hits"],
            "ordinal": best[r["session_id"]]["ordinal"] if r["session_id"] in best
            else r["first_ordinal"],
            "role": best[r["session_id"]]["role"] if r["session_id"] in best else "",
            "tool": best[r["session_id"]]["tool"] if r["session_id"] in best else None,
            "snippet": best[r["session_id"]]["snip"] if r["session_id"] in best else "",
            "score": r["score"],
        }
        for r in rows if r["session_id"] in meta
    ]


def set_pinned(con: sqlite3.Connection, session_id: str, pinned: bool) -> None:
    con.execute("UPDATE sessions SET pinned=? WHERE id=?", (int(pinned), session_id))
    con.commit()


def set_read(con: sqlite3.Connection, session_id: str, read: bool) -> None:
    con.execute("UPDATE sessions SET read_state=? WHERE id=?", (int(read), session_id))
    con.commit()
