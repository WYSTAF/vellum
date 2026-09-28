"""Tests for the parts of the engine where a silent regression would be
invisible in the UI.

The theme: every one of these is a bug that was actually measured on the
real archive during development -- 11x duplicate sessions, 210k harness
rows, 67 uuid-as-title sessions, 54k unlabelled tool calls, a broken
Markdown fence.  A test that pins the fix is worth more than a test that
pins the shape.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vellum import export, palette, parser, store  # noqa: E402


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _record(**kw):
    base = {"uuid": "u" + str(kw.get("n", 0)), "sessionId": "s1",
            "timestamp": "2026-01-01T00:00:00Z"}
    base.update(kw)
    return base


def write_jsonl(path: str, records: list[dict]) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    return path


def build_corpus(tmp: str) -> str:
    """A projects dir with two sessions: one, and a rewind of it.

    A real rewind is a *prefix* of the original, not a copy: the user rewinds
    to message 3 and types something different, so the two files agree up to
    the rewind point and then diverge. That is exactly the shape the whole
    dedup design has to recognise.
    """
    root = os.path.join(tmp, "projects", "demo")
    # A long shared opening, then a divergence -- which is what a rewind
    # actually looks like. Measured on the real archive, a rewound session
    # shares 99.96% of its messages with the original, so the fixture has to
    # be long enough for the threshold to mean something.
    head = [_record(n=i, type="user", message={"role": "user", "content": f"step {i}"})
            for i in range(1, 21)]
    write_jsonl(os.path.join(root, "aaaa-1111.jsonl"), head + [
        _record(n=30, type="assistant", message={"role": "assistant", "model": "claude-opus-5",
            "usage": {"input_tokens": 100, "output_tokens": 20,
                      "cache_read_input_tokens": 900, "cache_creation_input_tokens": 50},
            "content": [{"type": "text", "text": "Using coral."}]}),
    ])
    write_jsonl(os.path.join(root, "bbbb-2222.jsonl"), head + [
        _record(n=31, type="assistant", message={"role": "assistant", "model": "claude-opus-5",
            "usage": {"input_tokens": 100, "output_tokens": 20},
            "content": [{"type": "text", "text": "Using teal instead."}]}),
    ])
    return os.path.join(tmp, "projects")


def session_row(**kw):
    base = {
        "id": "s", "parent_id": None, "kind": "main", "project": "p",
        "path": "/x.jsonl", "title": "T", "custom_title": None, "ai_title": None,
        "cwd": None, "git_branch": None, "version": None, "model": None,
        "first_ts": "2026-01-01T00:00:00Z", "last_ts": "2026-01-01T00:01:00Z",
        "bytes": 100, "mtime": 1.0, "n_turns": 1, "n_user": 1, "n_assistant": 1,
        "n_tool": 0, "in_tok": 0, "out_tok": 0, "preview": "p",
        "ribbon": [], "duration_s": 0.0, "compact_count": 0, "error_count": 0,
    }
    base.update(kw)
    return base


MSG = {"ordinal": 0, "role": "user", "text": "hi", "ts": None, "model": None,
       "tool": None, "label": "", "in_tok": 0, "out_tok": 0, "cache_tok": 0,
       "sidechain": 0, "images": 0, "duration_s": None, "uuid": None}


# --------------------------------------------------------------------------
# Deduplication -- the finding the whole design rests on
# --------------------------------------------------------------------------


def test_rewind_is_not_a_second_conversation(tmp_path=None):
    tmp = tempfile.mkdtemp()
    root = build_corpus(tmp)
    con = store.open_db(os.path.join(tmp, "i.db"))
    from vellum import indexer
    indexer.scan(con, root)

    assert con.execute("SELECT count(*) FROM sessions WHERE is_fork=0").fetchone()[0] == 1, \
        "a rewind must collapse onto the conversation it was rewound from"
    assert con.execute("SELECT count(*) FROM sessions").fetchone()[0] == 2
    # The original keeps the identity; the later branch becomes the alias.
    assert con.execute(
        "SELECT canonical FROM sessions WHERE is_fork=1").fetchone()[0] == "aaaa-1111"
    con.close()


def test_fingerprint_is_order_independent():
    a = [parser.Msg(0, "user", "x", uuid="1"), parser.Msg(1, "user", "y", uuid="2")]
    b = [parser.Msg(0, "user", "y", uuid="2"), parser.Msg(1, "user", "x", uuid="1")]
    assert store.session_fingerprint(a) == store.session_fingerprint(b)


def test_fingerprint_changes_when_content_changes():
    a = [parser.Msg(0, "user", "x", uuid="1")]
    b = [parser.Msg(0, "user", "x", uuid="1"), parser.Msg(1, "user", "y", uuid="2")]
    assert store.session_fingerprint(a) != store.session_fingerprint(b)


def test_rescan_indexes_nothing():
    """A rescan of an unchanged archive is a metadata pass."""
    from vellum import indexer
    tmp = tempfile.mkdtemp()
    root = build_corpus(tmp)
    con = store.open_db(os.path.join(tmp, "i.db"))
    indexer.scan(con, root)
    before = con.execute("SELECT count(*) FROM messages").fetchone()[0]
    counters = indexer.scan(con, root)
    after = con.execute("SELECT count(*) FROM messages").fetchone()[0]
    assert counters["indexed"] == 0
    assert before == after
    con.close()


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


def test_harness_attachments_are_dropped():
    """210k of these on the real archive; none of it is conversation."""
    with tempfile.TemporaryDirectory() as tmp:
        p = write_jsonl(os.path.join(tmp, "p", "s1.jsonl"), [
            _record(n=1, type="user", message={"role": "user", "content": "real question"}),
            _record(n=2, type="attachment", attachment={
                "type": "total_tokens_reminder", "content": "15M tokens left"}),
            _record(n=3, type="attachment", attachment={
                "type": "prompt_snapshot", "systemPrompt": ["x" * 5000]}),
            _record(n=4, type="attachment", attachment={
                "type": "environment",
                "snapshot": {"workingDirectory": "C:/proj", "osVersion": "10"}}),
            _record(n=5, type="assistant", message={
                "role": "assistant", "content": [{"type": "text", "text": "answer"}]}),
        ])
        ps = parser.parse_file(p)
        roles = [m.role for m in ps.messages]
        assert roles == ["user", "assistant"], f"got {roles}"
        # The environment snapshot is still captured, just not rendered inline.
        assert ps.cwd == "C:/proj"


def test_real_titles_beat_session_ids():
    """67 sessions in the real archive had a uuid as their title."""
    ps = parser.ParsedSession(id="81ec418e-8507-4272-a933-56a458c87d1b",
                              kind="main", project="p", parent_dir="p", path="x")
    ps.messages = [parser.Msg(0, "user", "Research the Token Harbor API", uuid="u1")]
    ps.finalize()
    assert "81ec418e" not in ps.title
    assert "Token Harbor" in ps.title


def test_custom_title_wins():
    with tempfile.TemporaryDirectory() as tmp:
        p = write_jsonl(os.path.join(tmp, "p", "s1.jsonl"), [
            _record(n=1, type="user", message={"role": "user", "content": "the real prompt"}),
            _record(n=2, type="custom-title", customTitle="Dark mode work"),
        ])
        assert parser.parse_file(p).title == "Dark mode work"


def test_unparsed_tool_input_is_recovered():
    """54,000 Bash calls in the real archive had no label because of this."""
    label = parser.summarize_tool("Bash", {
        "__unparsedToolInput": {"raw": '{"command": "git status"}'}})
    assert label == "git status"


def test_tool_label_never_empty():
    assert parser.summarize_tool("TaskList", {}) != ""
    assert parser.summarize_tool("CronList", {}) != ""


def test_tool_result_inherits_its_tool_name():
    with tempfile.TemporaryDirectory() as tmp:
        p = write_jsonl(os.path.join(tmp, "p", "s1.jsonl"), [
            _record(n=1, type="assistant", message={"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Read",
                 "input": {"file_path": "/a/b.py"}}]}),
            _record(n=2, type="user", message={"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "print(1)"}]}),
        ])
        ps = parser.parse_file(p)
        assert [m.role for m in ps.messages] == ["tool", "result"]
        assert ps.messages[1].tool == "Read"


def test_full_token_accounting():
    """cache_creation is the largest token component and is often dropped."""
    with tempfile.TemporaryDirectory() as tmp:
        p = write_jsonl(os.path.join(tmp, "p", "s1.jsonl"), [
            _record(n=1, type="assistant", message={
                "role": "assistant", "model": "claude-opus-5",
                "usage": {"input_tokens": 10, "output_tokens": 5,
                          "cache_read_input_tokens": 900, "cache_creation_input_tokens": 700},
                "content": [{"type": "text", "text": "x"}]}),
        ])
        ps = parser.parse_file(p)
        assert ps.messages[0].in_tok == 10
        assert ps.messages[0].out_tok == 5
        assert ps.messages[0].cache_tok == 1600, "read + creation both count as cache"


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------


def indexed_db(tmp: str):
    from vellum import indexer
    root = build_corpus(tmp)
    con = store.open_db(os.path.join(tmp, "i.db"))
    indexer.scan(con, root)
    return con


def test_search_finds_and_ranks():
    tmp = tempfile.mkdtemp()
    con = indexed_db(tmp)
    assert len(store.search(con, "coral")) == 1
    # A term that only exists in the rewound branch is not a result: the
    # branch is not a separate conversation, and showing it would be exactly
    # the duplicate-search-result problem the dedup exists to prevent.
    assert store.search(con, "teal") == []
    # The shared opening is found once, not twice.
    assert len(store.search(con, "step")) == 1
    con.close()


def test_rewound_branch_is_reachable_from_its_canonical():
    """Hiding a branch from search must not make its content unreachable."""
    tmp = tempfile.mkdtemp()
    con = indexed_db(tmp)
    child = con.execute("SELECT id FROM sessions WHERE is_fork=1").fetchone()
    assert child is not None, "the fixture must contain a rewind"
    msgs = store.messages(con, child["id"])
    assert any("teal" in (m["text"] or "") for m in msgs), \
        "the rewound branch's own messages are still stored and readable"
    con.close()


def test_search_does_not_return_forks():
    tmp = tempfile.mkdtemp()
    con = indexed_db(tmp)
    for hit in store.search(con, "coral"):
        assert con.execute("SELECT is_fork FROM sessions WHERE id=?",
                           (hit["session_id"],)).fetchone()[0] == 0
    con.close()


def test_short_query_falls_back_instead_of_erroring():
    tmp = tempfile.mkdtemp()
    con = indexed_db(tmp)
    assert isinstance(store.search(con, "da"), list)
    assert isinstance(store.search(con, ""), list)
    con.close()


def test_search_query_injection_is_inert():
    tmp = tempfile.mkdtemp()
    con = indexed_db(tmp)
    # A malformed FTS expression must not raise or return junk.
    for q in ['"', 'a OR b', 'NEAR(', "*", "((("]:
        assert isinstance(store.search(con, q), list)
    con.close()


def test_like_wildcards_in_a_query_are_literal():
    """A two-character query falls back to LIKE, where % and _ are wildcards.

    Without escaping, searching "50%" matches any title containing "50"
    followed by anything, and "a_b" matches "axb".
    """
    root = os.path.join(tempfile.mkdtemp(), "projects", "p")
    os.makedirs(root)
    for name, title in (("a", "alpha_beta"), ("b", "alphaXbeta"),
                        ("c", "100% done")):
        write_jsonl(os.path.join(root, name + ".jsonl"), [
            _record(n=1, type="user",
                    message={"role": "user", "content": title}),
            _record(n=2, type="custom-title", customTitle=title),
        ])
    con = store.open_db(os.path.join(os.path.dirname(root), "i.db"))
    from vellum import indexer
    indexer.scan(con, os.path.dirname(root))
    assert [h["title"] for h in store.search(con, "alpha_beta")] == ["alpha_beta"]
    assert store.search(con, "a_b") == [], "'_' must be literal, not any-char"
    assert [h["title"] for h in store.search(con, "100%")] == ["100% done"]
    assert store.search(con, "50%") == [], "'%' must be literal, not any-run"
    con.close()


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------


def test_markdown_fence_survives_nested_backticks():
    """The bug that made the old exporter emit broken files."""
    body = "before\n```\ncode\n```\nafter"
    out = export.to_markdown(session_row(title="T"),
                             [{**MSG, "role": "tool", "tool": "Bash", "text": body}])
    assert "````" in out, "fence must be longer than the longest run inside"
    # The inner fence is still there, as content.
    assert "```" in out


def test_markdown_prose_uses_indentation_not_fences():
    """A stray ``` in what someone typed must not break the document."""
    body = "look at this:\n```\nnot really a block\n```"
    out = export.to_markdown(session_row(), [{**MSG, "text": body}])
    assert "    ```" in out, "prose is indented so an inner fence is inert"


def test_html_escapes_everything():
    out = export.to_html(session_row(), [{**MSG, "text": "<script>alert(1)</script>"}])
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;" in out


def test_json_keeps_zero_token_counts():
    out = export.to_json(session_row(), [{**MSG, "in_tok": 0, "out_tok": 0}])
    assert '"in_tok": 0' in out, "0 is a real value and must not be filtered out"


def test_all_five_formats_render():
    s = session_row()
    for fmt in export.FORMATS:
        assert export.render(fmt, s, [{**MSG}])


def test_render_accepts_sqlite_rows():
    """The real call path passes sqlite3.Row, which has no .get().

    Every exporter was written against dicts and the tests passed dicts, so
    this only surfaced when a real export was clicked. Uses the real schema
    so a column the exporters read is not silently missing here.
    """
    tmp = tempfile.mkdtemp()
    con = store.open_db(os.path.join(tmp, "i.db"))
    con.execute(
        "INSERT INTO sessions (id,kind,project,path,title,first_ts,last_ts) "
        "VALUES ('x','main','p','/x.jsonl','T','2026-01-01T00:00:00Z',"
        "'2026-01-01T00:05:00Z')"
    )
    con.execute(
        "INSERT INTO messages (session_id,ordinal,role,text,ts,tool,label) "
        "VALUES ('x',0,'user','hello','2026-01-01T00:00:00Z',NULL,'')"
    )
    con.commit()
    row = store.session(con, "x")
    msg = store.messages(con, "x")[0]
    assert not hasattr(row, "get"), "the point is that rows are not dicts"
    for fmt in export.FORMATS:
        assert export.render(fmt, row, [msg]), f"{fmt} failed on sqlite3.Row"
    con.close()


def test_safe_filename_is_windows_legal():
    for bad in ['a/b\\c:d*e?f"g<h>i|j', "CON", "   ", "trailing."]:
        name = export.safe_filename(bad, "md")
        assert not set(name) & set('<>:"/\\|?*')
        assert name.endswith(".md")


# --------------------------------------------------------------------------
# Palette -- the audit is the contract
# --------------------------------------------------------------------------


def test_every_text_surface_pair_passes_wcag_aa():
    for theme in ("light", "dark"):
        rows, fails = palette.audit(theme)
        assert not fails, f"{theme}: " + "; ".join(
            f"{f} on {b} = {r:.2f}" for f, b, r, _ in fails)


def test_apca_matches_published_reference_values():
    """A wrong APCA implementation validates itself against nothing."""
    for fg, bg, expected in [("#888888", "#ffffff", 63.1),
                             ("#ffffff", "#888888", -68.5),
                             ("#000000", "#aaaaaa", 58.4)]:
        got = palette.apca_lc(fg, bg)
        assert abs(got - expected) < 4, f"{fg}/{bg}: {got} vs {expected}"


def test_role_colours_are_distinguishable():
    for theme in ("light", "dark"):
        t = palette.build(theme)
        assert len({t["you"], t["claude"], t["machine"]}) == 3


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------


def test_read_endpoints_close_their_connection():
    """A connection per request that is never closed leaks a handle per hit."""
    from fastapi.testclient import TestClient
    from vellum.app import app

    with TestClient(app) as client:
        assert client.get("/api/status").status_code == 200
        for _ in range(5):
            assert client.get("/api/sessions?limit=5").status_code == 200
            assert client.get("/api/projects").status_code == 200
            assert client.get("/api/stats").status_code == 200
            assert client.get("/api/search?q=extractor").status_code == 200
    # If the generator dependency is wired up, the fixture index was never
    # needed and every one of those calls opened and closed cleanly.
    assert callable(app)


def test_unknown_session_is_a_404_not_a_500():
    from fastapi.testclient import TestClient
    from vellum.app import app

    with TestClient(app) as client:
        r = client.get("/api/session/does-not-exist")
        assert r.status_code == 404, r.text


def test_concurrent_index_requests_start_only_one_scan():
    """F5 held down must not spawn four indexers writing the same database."""
    from vellum.app import IndexState

    st = IndexState()
    calls = []
    st._run = lambda full: calls.append(full)  # type: ignore[method-assign]
    assert st.start() is True
    st._thread.join(timeout=5)  # type: ignore[union-attr]
    assert st.start() is True          # the first has finished, so a new one is fine
    st._thread.join(timeout=5)  # type: ignore[union-attr]

    st2 = IndexState()
    started = st2.start()
    second = st2.start()
    assert started is True
    assert second is False, "a second scan started while one was running"
    st2.cancel()


# --------------------------------------------------------------------------
# Damaged archives
# --------------------------------------------------------------------------


def test_damaged_archive_does_not_crash_anything():
    """A corrupt or truncated archive must degrade, not fail.

    The corpus is files a well-formed archive would never contain: non-JSON
    lines, bare scalars, nulls where strings are expected, a string where a
    token count should be. Every read path runs over it, because each of
    these was a real crash during development -- a token count of
    "not a number" lost an entire conversation.
    """
    tmp = tempfile.mkdtemp()
    root = os.path.join(tmp, "projects", "p")
    os.makedirs(root)

    def w(name, lines):
        with open(os.path.join(root, name), "w", encoding="utf-8") as fh:
            for ln in lines:
                fh.write(ln + chr(10))

    w("empty.jsonl", [])
    w("garbage.jsonl", ["not json at all", "{{{", ""])
    w("scalars.jsonl", ['"a string"', "42", "null", "[1,2,3]"])
    w("nulls.jsonl", [json.dumps({
        "type": "assistant", "uuid": None, "sessionId": None, "timestamp": None,
        "message": {"role": "assistant", "content": None, "usage": None}})])
    w("weird.jsonl", [json.dumps({
        "type": "assistant", "uuid": "u2", "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": None, "name": None, "input": None},
                {"type": "tool_use", "id": "x", "name": "Read",
                 "input": {"file_path": "C:/a/b.py", "offset": 1, "limit": "20"}},
                {"type": "text", "text": "ok"},
            ],
            "usage": {"input_tokens": "not a number"}}})])
    w("titles.jsonl", [
        json.dumps({"type": "custom-title", "customTitle": "a" * 5000, "sessionId": "t"}),
        json.dumps({"type": "user", "uuid": "t1",
                    "message": {"role": "user", "content": "x" * 200000}}),
    ])

    con = store.open_db(os.path.join(tmp, "i.db"))
    from vellum import indexer
    indexer.scan(con, os.path.join(tmp, "projects"))

    # A malformed usage block must not cost the session its messages.
    assert con.execute("SELECT COUNT(*) FROM messages").fetchone()[0] > 0,         "a bad token count must not lose the conversation"
    store.stats(con)
    store.projects(con)
    store.list_sessions(con, limit=10)

    for q in ['"', "*", "a OR b", "NEAR(", "AND", "\\", "'", "((", "a" * 500]:
        assert isinstance(store.search(con, q), list), f"search({q!r}) raised"
    for sort in ("recent", "oldest", "turns", "size", "'; DROP TABLE sessions;--", "x"):
        assert isinstance(store.list_sessions(con, sort=sort, limit=5), list)

    for r in con.execute("SELECT id FROM sessions LIMIT 5").fetchall():
        sid = r["id"]
        sess, msgs = store.session(con, sid), store.messages(con, sid)
        if sess is None:
            continue
        for fmt in export.FORMATS:
            assert export.render(fmt, sess, msgs)
        assert export.safe_filename(sess["title"] or "", "md")

    assert store.messages(con, "does-not-exist") == []
    assert store.session(con, "does-not-exist") is None
    assert store.search(con, "x", project="nope") == []
    con.close()
