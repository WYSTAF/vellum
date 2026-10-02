"""Frozen-build entry point.

``vellum/__main__.py`` uses a relative import, which is right for
``python -m vellum`` but wrong for a PyInstaller build: the entry script is
executed as a top-level ``__main__`` module with no package context, so
``from .launcher import main`` raises ImportError and the exe dies on startup
with no window and no explanation.

This module imports absolutely instead, and exists only to be the Analysis
entry in packaging/vellum.spec.  Nothing else should import it.
"""

import multiprocessing
import sys

from vellum.launcher import main

if __name__ == "__main__":
    # Required for a frozen build to spawn children correctly on Windows.
    multiprocessing.freeze_support()
    sys.exit(main())
