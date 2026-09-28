"""Where things live, and how that location is remembered.

The only setting that is genuinely hard to get right is the Claude folder.
``CLAUDE_CONFIG_DIR`` is the environment variable Claude Code itself honours,
so it wins over everything; otherwise the default is ``~/.claude`` on the
platform.  Whatever the user picks in Settings is persisted, and if that path
later disappears the app falls back to the default rather than showing an
empty library with no explanation.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

CONFIG_DIR = Path(
    os.environ.get("VELLUM_HOME")
    or (Path.home() / ".vellum")
)
CONFIG_FILE = CONFIG_DIR / "settings.json"


def default_claude_dir() -> str:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return env
    if sys.platform == "win32":
        return str(Path.home() / ".claude")
    if sys.platform == "darwin":
        return str(Path.home() / "Library" / "Application Support" / "Claude")
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return str(Path(xdg or Path.home() / ".config") / "Claude")


def claude_projects() -> str:
    """The projects directory holding the JSONL transcripts."""
    return str(Path(claude_dir()) / "projects")


def claude_dir() -> str:
    saved = _read().get("claude_dir")
    if saved and Path(saved).is_dir():
        return saved
    return default_claude_dir()


def _read() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_FILE)


def set_claude_dir(path: str) -> None:
    _write({**_read(), "claude_dir": str(path)})


def get(key: str, default=None):
    return _read().get(key, default)


def set(key: str, value) -> None:
    _write({**_read(), key: value})
