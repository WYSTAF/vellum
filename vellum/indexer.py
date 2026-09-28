"""Scanning the archive into the index, incrementally and without blocking.

The indexer is the only component that ever reads the whole corpus, and it
does so in bounded batches on a small thread pool.  Three properties matter:

*   **Bounded memory.**  A single transcript can be 10 MB and the corpus is
    1.6 GB, so a session is parsed, written and dropped -- never accumulated
    in a list.  ``concurrent.futures.Executor.map`` holds every result alive
    until the consumer finishes, which on this corpus was a measured 693 MB
    spike; so futures are drained under an explicit in-flight window instead.
*   **Cheap no-ops.**  A file whose (mtime, size) is unchanged is skipped
    before it is opened.  A stat is a syscall; a parse is 10,000 objects.
*   **Fork resolution after write.**  Deduplication is a whole-corpus
    property -- you cannot know two files are forks until you have seen both
    -- so it runs once at the end of a full pass, not per file.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Callable, Iterator

from . import store
from .parser import discover, parse_file, session_id_for

# At most this many files parsed at once. Parsing is CPU-bound and holds the
# GIL for the JSON decode, so more threads buy nothing and cost memory.
WORKERS = min(4, max(2, (os.cpu_count() or 4) // 2))
# At most this many parsed sessions held in memory at once, regardless of how
# fast the parser runs relative to the writer.
INFLIGHT = WORKERS + 1

ProgressFn = Callable[[str], None]


class Cancelled(Exception):
    pass


def _stat_key(path: str) -> tuple[float, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime, st.st_size)


def pending_files(con: sqlite3.Connection, root: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Return (files to index, session ids whose file is gone).

    Only stat is compared -- never the content.  That is what makes a rescan
    of an unchanged 1.6 GB archive a metadata pass: 378 stat calls and no
    JSON decoding at all.
    """
    known = {
        r["id"]: (r["mtime"], r["bytes"])
        for r in con.execute("SELECT id, mtime, bytes FROM sessions").fetchall()
    }
    todo: list[tuple[str, str]] = []
    for path, kind in discover(root):
        # The id has to be derived exactly as parse_file derives it, or every
        # sub-agent looks absent and is deleted and re-parsed on every scan.
        sid, _, _ = session_id_for(path, kind)
        prev = known.pop(sid, None)
        key = _stat_key(path)
        if key is None:
            continue
        if prev != key:
            todo.append((path, kind))
    return todo, list(known)


def _write(con: sqlite3.Connection, ps) -> None:
    store._put_session(con, ps)


def scan(
    con: sqlite3.Connection,
    root: str,
    *,
    progress: ProgressFn | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> dict:
    """Bring the index up to date.  Returns counters for the status line."""
    t0 = time.time()
    todo, stale = pending_files(con, root)
    counters = {"indexed": 0, "skipped": 0, "removed": len(stale), "errors": 0}
    if stale:
        for sid in stale:
            store.delete_session(con, sid)
    total = len(todo)
    if not total:
        store._resolve_forks(con)
        con.commit()
        counters["elapsed"] = time.time() - t0
        return counters

    if progress:
        progress(f"Indexing {total:,} changed conversation(s)…")

    done = 0
    pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="vellum-idx")
    futures: dict = {}
    queue = iter(todo)
    exhausted = False
    try:
        while True:
            while not exhausted and len(futures) < INFLIGHT:
                try:
                    path, kind = next(queue)
                except StopIteration:
                    exhausted = True
                    break
                futures[pool.submit(parse_file, path, kind)] = path
            if not futures:
                break
            finished, _ = wait(list(futures), return_when=FIRST_COMPLETED)
            for fut in finished:
                path = futures.pop(fut)
                done += 1
                if is_cancelled and is_cancelled():
                    raise Cancelled()
                try:
                    ps = fut.result()
                except Exception:
                    counters["errors"] += 1
                    continue
                if ps is None:
                    counters["skipped"] += 1
                    continue
                try:
                    _write(con, ps)
                except Exception:
                    counters["errors"] += 1
                    continue
                counters["indexed"] += 1
            if done % 25 == 0:
                # Commit in batches and checkpoint. Without this the WAL grows
                # to the size of the whole corpus and the final close pays for
                # all of it at once -- a 51s stall on a no-op rescan, measured.
                con.commit()
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            if progress and done % 25 == 0:
                rate = done / max(1e-6, time.time() - t0)
                progress(f"Indexed {done:,}/{total:,} · {rate:.0f}/s")
    except Cancelled:
        pool.shutdown(wait=False, cancel_futures=True)
        # Roll back, do not commit. The stale sessions were deleted at the
        # top of this pass; committing would keep those deletions while the
        # parse work that would have re-added them never ran. A cancelled
        # rescan was leaving the index permanently missing sessions --
        # verified: 2 sessions in, 1 out.
        con.rollback()
        counters["cancelled"] = True
        raise
    finally:
        pool.shutdown(wait=True)

    # Fork resolution needs the whole pass. It is a few UPDATE statements
    # over 378 rows, so doing it once at the end costs nothing.
    store._resolve_forks(con)
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    counters["elapsed"] = time.time() - t0
    if progress:
        s = stats_line(con, counters)
        progress(s)
    return counters


def stats_line(con: sqlite3.Connection, counters: dict | None = None) -> str:
    s = store.stats(con)
    parts = [f"{s['sessions']:,} conversations"]
    if s["forks"]:
        parts.append(f"{s['forks']:,} rewinds collapsed")
    parts.append(f"{s['turns']:,} turns")
    if counters and counters.get("indexed"):
        parts.append(f"{counters['indexed']:,} indexed")
    return " · ".join(parts)


def full_scan(con: sqlite3.Connection, root: str, **kw) -> dict:
    """Drop everything and re-read the corpus.  Used only on explicit request."""
    con.execute("DELETE FROM msg_fts")
    con.execute("DELETE FROM messages")
    con.execute("DELETE FROM tool_freq")
    con.execute("DELETE FROM sessions")
    con.commit()
    counters = scan(con, root, **kw)
    # A rebuild changes the size of every page, and SQLite does not return
    # freed pages to the filesystem on its own -- on this archive the index
    # stayed 20 MB larger after a rebuild until this ran.
    #
    # VACUUM rewrites the whole database, which on a multi-gigabyte index can
    # need more memory than the machine has; it is a space optimisation, not a
    # correctness one, so a failure here must not fail the rebuild.
    try:
        con.execute("VACUUM")
    except (sqlite3.OperationalError, MemoryError) as exc:
        counters["vacuum_skipped"] = str(exc)
    return counters


def is_stale(con: sqlite3.Connection, root: str) -> bool:
    """True if a rescan would find work.  Stat-only; safe to call per keystroke."""
    todo, stale = pending_files(con, root)
    return bool(todo or stale)
