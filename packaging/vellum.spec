# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Vellum.

Run with:

    pyinstaller packaging/vellum.spec --noconfirm

Two things here are not automatic, and both fail the same way -- silently,
at runtime, on the machine of someone who downloaded the file.

pywebview has no PyInstaller hook.  Its Windows backend loads the WebView2
interop DLLs and the js/ runtime out of the *installed* package directory,
so a frozen build without them starts, serves, and then fails to open a
window.  They are collected by hand below.

The UI is data, not code.  ``vellum/ui`` is copied as a tree, and app.py
resolves it through ``sys._MEIPASS``.
"""

import os

block_cipher = None

# A spec file is exec'd, not imported, so __file__ does not exist in its
# namespace -- SPECPATH is the supported way to reach this file's directory
# (packaging/), and the project root is its parent.
HERE = os.path.dirname(os.path.abspath(SPECPATH))
UI_DIR = os.path.join(HERE, "vellum", "ui")
ICON = os.path.join(HERE, "assets", "vellum.ico")

# Fail loudly here rather than shipping a build whose window cannot open.
import webview  # noqa: E402

WEBVIEW_DIR = os.path.dirname(os.path.abspath(webview.__file__))


def webview_datas():
    """Data files pywebview needs at runtime, found by walking the package.

    Hard-coding a file list here would silently rot: pywebview renames its
    interop DLLs between releases, and a build made against a version that
    no longer matches ships an exe that opens a browser tab instead of a
    window.  Walking the tree collects whatever this install actually has.
    """
    wanted_dirs = ("js", "lib")
    wanted_suffixes = (".js", ".dll", ".json", ".pak", ".dat", ".bin")
    out = []
    for sub in wanted_dirs:
        root = os.path.join(WEBVIEW_DIR, sub)
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                if fn.lower().endswith(wanted_suffixes):
                    out.append(
                        (os.path.join(dirpath, fn), os.path.join("webview", sub))
                    )
    return out


datas = [(UI_DIR, "vellum/ui")] + webview_datas()
if not os.path.isfile(ICON):
    raise SystemExit(
        f"icon missing: {ICON}\nrun:  python tools/make_icon.py assets"
    )

hiddenimports = [
    "webview",
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "clr",
    "pythonnet",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
]

a = Analysis(
    [os.path.join(HERE, "packaging", "entry.py")],
    pathex=[HERE],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # ctypes and the socket stack are pulled in by uvicorn, and neither is
    # visible to the static import graph.
    excludes=[
        "tkinter",
        "pytest",
        "_pytest",
        "matplotlib",
        "numpy",
        "PIL",  # only tools/make_icon.py needs it
        "scipy",
        "notebook",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="Vellum",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX is a recurring source of AV false positives
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # a windowed app: no console window behind the UI
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
)
