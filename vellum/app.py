"""The HTTP layer: JSON in, JSON out.  No business logic lives here.

Everything the UI needs is a read except for three actions -- pin, mark-read
and export -- so the surface is small on purpose.  Search is served from the
index; the index is refreshed by a background job that the status endpoint
reports on, so a long first scan never blocks a request.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Any, Iterator

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import export, indexer, paths, settings, store

app = FastAPI(title="Vellum", docs_url=None, redoc_url=None)

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")


# --------------------------------------------------------------------------
# Index state. One writer thread, polled by /api/status.
# --------------------------------------------------------------------------


class IndexState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self.message = "Not indexed yet"
        self.running = False
        self.counters: dict[str, Any] = {}
        self.finished_at: float | None = None
        self.error: str | None = None

    def start(self, full: bool = False) -> bool:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._cancel.clear()
            self.running = True
            self.error = None
            self.message = "Starting…"
            self._thread = threading.Thread(
                target=self._run, args=(full,), daemon=True, name="vellum-index"
            )
            self._thread.start()
            return True

    def cancel(self) -> None:
        self._cancel.set()

    def _run(self, full: bool) -> None:
        # open_db is inside the try: a corrupt or locked index raised there
        # before the try existed, so `finally` never ran, `running` stayed
        # True forever, the UI spun indefinitely, and a second POST /api/index
        # started a *second* indexer because the is_alive() guard was now
        # satisfied.
        con = None
        try:
            con = store.open_db(settings.index_path())
            root = paths.claude_projects()
            fn = indexer.full_scan if full else indexer.scan
            counters = fn(
                con, root,
                progress=self._progress,
                is_cancelled=self._cancel.is_set,
            )
            self.counters = counters
            if counters.get("cancelled"):
                self.message = "Indexing cancelled"
            else:
                self.message = indexer.stats_line(con, counters)
        except indexer.Cancelled:
            self.message = "Indexing cancelled"
        except Exception as exc:  # noqa: BLE001 -- surfaced to the UI
            self.error = f"{type(exc).__name__}: {exc}"
            self.message = "Indexing failed"
        finally:
            if con is not None:
                try:
                    con.close()
                except Exception:  # noqa: BLE001
                    pass
            self.running = False
            self.finished_at = time.time()

    def _progress(self, msg: str) -> None:
        self.message = msg

    def snapshot(self) -> dict:
        return {
            "running": self.running, "message": self.message,
            "error": self.error, "counters": self.counters,
            "finished_at": self.finished_at,
        }


state = IndexState()


def _db():
    path = settings.index_path()
    if not os.path.exists(path):
        raise HTTPException(404, "The index has not been built yet.")
    return store.connect(path, read_only=True)


def db() -> Iterator[sqlite3.Connection]:
    """A read-only connection, closed when the request ends.

    FastAPI runs sync endpoints on a worker thread, so a connection opened per
    request and never closed leaks one handle per request until the process
    runs out. Yielding it makes the lifetime the framework's problem.
    """
    con = _db()
    try:
        yield con
    finally:
        con.close()


# --------------------------------------------------------------------------
# Read endpoints
# --------------------------------------------------------------------------


@app.get("/api/sessions")
def api_sessions(
    project: str | None = None,
    sort: str = "recent",
    limit: int = Query(200, le=1000),
    offset: int = 0,
    con: sqlite3.Connection = Depends(db),
) -> dict:
    rows = store.list_sessions(
        con, project=project, sort=sort, limit=limit, offset=offset
    )
    total = con.execute(
        "SELECT COUNT(*) n FROM sessions WHERE is_fork=0"
        + (" AND project=?" if project else ""),
        (project,) if project else (),
    ).fetchone()["n"]
    return {"total": total, "sessions": [dict(r) for r in rows]}


@app.get("/api/session/{session_id}")
def api_session(session_id: str, con: sqlite3.Connection = Depends(db)) -> dict:
    row = store.session(con, session_id)
    if row is None:
        raise HTTPException(404, "No such conversation.")
    msgs = store.messages(con, session_id)
    forks = [dict(f) for f in store.session_forks(con, session_id)]
    tools = [
        dict(t) for t in con.execute(
            "SELECT tool, n FROM tool_freq WHERE session_id=? ORDER BY n DESC",
            (session_id,),
        ).fetchall()
    ]
    return {
        "session": dict(row), "messages": [dict(m) for m in msgs],
        "forks": forks, "tools": tools,
    }


@app.get("/api/messages/{session_id}")
def api_messages(
    session_id: str,
    offset: int = 0,
    limit: int = Query(300, le=2000),
    include_thinking: bool = True,
    include_tools: bool = True,
    con: sqlite3.Connection = Depends(db),
) -> dict:
    """Paged message fetch. The reader never holds a 10 MB transcript."""
    total = len(store.messages(
        con, session_id,
        include_thinking=include_thinking, include_tools=include_tools,
    ))
    rows = store.messages(
        con, session_id, limit=limit, offset=offset,
        include_thinking=include_thinking, include_tools=include_tools,
    )
    return {"total": total, "offset": offset, "messages": [dict(r) for r in rows]}


@app.get("/api/search")
def api_search(
    q: str = "",
    project: str | None = None,
    since: str | None = None,
    until: str | None = None,
    role: str | None = None,
    model: str | None = None,
    limit: int = Query(100, le=500),
    con: sqlite3.Connection = Depends(db),
) -> dict:
    t0 = time.perf_counter()
    hits = store.search(
        con, q, project=project, since=since, until=until,
        role=role, model=model, limit=limit,
    )
    return {
        "query": q, "hits": hits, "count": len(hits),
        "ms": round((time.perf_counter() - t0) * 1000, 1),
    }


@app.get("/api/projects")
def api_projects(con: sqlite3.Connection = Depends(db)) -> dict:
    return {"projects": [dict(p) for p in store.projects(con)]}


@app.get("/api/stats")
def api_stats(con: sqlite3.Connection = Depends(db)) -> dict:
    s = store.stats(con)
    tools = con.execute(
        "SELECT tool, SUM(n) n FROM tool_freq GROUP BY tool ORDER BY n DESC LIMIT 40"
    ).fetchall()
    days = con.execute(
        "SELECT substr(COALESCE(last_ts,''),1,10) d, COUNT(*) n FROM sessions "
        "WHERE is_fork=0 GROUP BY d ORDER BY d"
    ).fetchall()
    s["tools"] = [{"tool": t["tool"], "n": t["n"]} for t in tools]
    s["days"] = [{"day": d["d"], "n": d["n"]} for d in days]
    return s


@app.get("/api/status")
def api_status() -> dict:
    snap = state.snapshot()
    path = settings.index_path()
    snap["indexed"] = os.path.exists(path)
    snap["index_size"] = os.path.getsize(path) if os.path.exists(path) else 0
    if snap["indexed"] and not snap["running"] and snap["message"] == "Not indexed yet":
        # This process did not build the index, but one exists on disk. Report
        # what is actually in it rather than a status that reads as empty.
        try:
            con = store.connect(path, read_only=True)
            snap["message"] = indexer.stats_line(con)
            con.close()
        except Exception:  # noqa: BLE001 -- a corrupt index still shows as present
            pass
    return snap


@app.post("/api/index")
def api_index(full: bool = False) -> dict:
    started = state.start(full=full)
    return {"started": started, **state.snapshot()}


@app.post("/api/index/cancel")
def api_index_cancel() -> dict:
    state.cancel()
    return {"ok": True}


# --------------------------------------------------------------------------
# Mutations
# --------------------------------------------------------------------------


class PinReq(BaseModel):
    session_id: str
    pinned: bool


def _writer() -> sqlite3.Connection:
    """A read/write connection that tolerates a running indexer.

    ``open_db`` sets ``journal_mode=WAL``, which is a write and needs a lock
    the indexer holds in batches. Pin and mark-read are the two actions a
    reader clicks constantly, and they were returning 500 for the whole time
    an index was running. A plain connection can write under WAL while a
    reader holds the database open.
    """
    return store.connect(settings.index_path(), read_only=False)


@app.post("/api/pin")
def api_pin(req: PinReq) -> dict:
    con = _writer()
    try:
        store.set_pinned(con, req.session_id, req.pinned)
    except sqlite3.OperationalError as exc:
        raise HTTPException(503, f"The index is busy: {exc}") from exc
    finally:
        con.close()
    return {"ok": True}


class ReadReq(BaseModel):
    session_id: str
    read: bool = True


@app.post("/api/read")
def api_read(req: ReadReq) -> dict:
    con = _writer()
    try:
        store.set_read(con, req.session_id, req.read)
    except sqlite3.OperationalError as exc:
        # Failing to record "I read this" must never interrupt reading it.
        return {"ok": False, "skipped": str(exc)}
    finally:
        con.close()
    return {"ok": True}


class ExportReq(BaseModel):
    session_id: str
    fmt: str = "md"
    include_thinking: bool = False
    include_tools: bool = True
    dest: str | None = None


@app.post("/api/export")
def api_export(req: ExportReq, con: sqlite3.Connection = Depends(db)) -> dict:
    row = store.session(con, req.session_id)
    if row is None:
        raise HTTPException(404, "No such conversation.")
    msgs = store.messages(
        con, req.session_id,
        include_thinking=req.include_thinking, include_tools=req.include_tools,
    )
    dest = req.dest or settings.export_dir()
    os.makedirs(dest, exist_ok=True)
    name = export.safe_filename(row["title"], req.fmt, req.session_id)
    path = os.path.join(dest, name)
    body = export.render(req.fmt, row, msgs)
    # Write to a temp file in the same directory and rename, so a failure can
    # never leave a half-written export behind. The temp name is unique per
    # call so two exports racing cannot clobber each other's staging file.
    tmp = f"{path}.{os.getpid()}.{threading.get_ident():x}.part"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return {"ok": True, "path": path, "bytes": len(body.encode("utf-8"))}


# --------------------------------------------------------------------------
# Static UI
# --------------------------------------------------------------------------


@app.get("/")
def index() -> FileResponse:
    return FileResponse(
        os.path.join(UI_DIR, "index.html"),
        headers={"Cache-Control": "no-store"},
    )


class _NoCacheStatic(StaticFiles):
    """A local app that is edited in place must not serve a stale bundle.

    The browser cached app.js across a restart of the server here and the UI
    silently ran old code with no error anywhere -- exactly the failure that
    is impossible to debug and trivial to prevent.
    """

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
        return resp


app.mount("/static", _NoCacheStatic(directory=UI_DIR), name="static")
