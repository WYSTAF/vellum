"""User preferences. One JSON file, written atomically."""

from __future__ import annotations

import os
from pathlib import Path

from . import paths

DEFAULTS = {
    "theme": "dark",              # dark | light | system
    "claude_dir": "",             # empty means "use the default"
    "include_thinking": False,
    "include_tools": True,
    "export_dir": "",
    "page_size": 300,             # messages rendered per chunk in the reader
}


def index_path() -> str:
    paths.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return str(paths.CONFIG_DIR / "index.db")


def export_dir() -> str:
    d = paths.get("export_dir") or DEFAULTS["export_dir"]
    if d:
        return d
    return str(Path(paths.CONFIG_DIR / "exports"))


def all() -> dict:
    saved = paths._read()
    return {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}


def get(key: str):
    return all().get(key, DEFAULTS.get(key))


def set(key: str, value) -> None:  # noqa: A001 - mirrors the module's own name
    paths.set(key, value)
