"""Start the app.

Three ways to present the same local server, in order of preference:

1. a native window via pywebview, if it is installed;
2. the default browser, pointed at the local address;
3. a plain terminal message with the URL, if even the browser cannot open.

The server binds to 127.0.0.1 only. This app reads a private archive and
writes nothing but an index and exports; it must not be reachable from the
network.

Every startup path also logs to ``~/.vellum/vellum.log``. A windowed frozen
build has no stdout and no stderr -- Python sets both to ``None`` -- so any
failure that would normally explain itself on a console dies silently there.
That is not hypothetical: uvicorn's default log formatter calls
``sys.stdout.isatty()``, raised ``AttributeError`` during configuration, and
the exe exited without a window and without a word.
"""

from __future__ import annotations

import logging
import logging.handlers
import socket
import sys
import threading
import time
import webbrowser

import uvicorn

from . import app as app_module
from . import paths

HOST = "127.0.0.1"
LOG_NAME = "vellum.log"


def free_port(preferred: int = 8733) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((HOST, preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def _setup_logging() -> str:
    """Log to a file, and to stderr when there is one."""
    try:
        paths.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = paths.CONFIG_DIR / LOG_NAME
        root = logging.getLogger()
        if not any(getattr(h, "_vellum_file", False) for h in root.handlers):
            handler = logging.handlers.RotatingFileHandler(
                log_path, maxBytes=512_000, backupCount=2, encoding="utf-8"
            )
            handler._vellum_file = True  # type: ignore[attr-defined]
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s"))
            root.addHandler(handler)
        if (sys.stderr is not None
                and not any(getattr(h, "_vellum_stream", False) for h in root.handlers)):
            stream = logging.StreamHandler()
            stream._vellum_stream = True  # type: ignore[attr-defined]
            stream.setFormatter(logging.Formatter("%(message)s"))
            root.addHandler(stream)
        root.setLevel(logging.INFO)
        return str(log_path)
    except Exception:  # noqa: BLE001 -- no log file must not stop the app
        return ""


def _alert(text: str) -> None:
    """An unexplained exit is worse than an ugly one: show the failure."""
    logging.getLogger("vellum").error(text)
    try:
        if sys.stderr is not None:
            print(text, file=sys.stderr)
            return
        import ctypes  # only exists on Windows; guarded by the except below
        ctypes.windll.user32.MessageBoxW(None, text, "Vellum", 0x10)
    except Exception:  # noqa: BLE001
        pass


def _serve(port: int) -> None:
    # log_config=None is not cosmetic. uvicorn's default formatter calls
    # sys.stdout.isatty(), and a windowed build has sys.stdout=None, so
    # configuration raises ValueError("Unable to configure formatter") and
    # the server thread dies before it binds. Logging is already configured
    # by _setup_logging, so uvicorn only needs to propagate to the root
    # logger.
    uvicorn.run(
        app_module.app, host=HOST, port=port,
        log_config=None, log_level="warning", access_log=False,
    )


def main() -> int:
    log = logging.getLogger("vellum")
    log_path = _setup_logging()
    port = free_port()
    url = f"http://{HOST}:{port}/"
    log.info("Vellum starting at %s (frozen=%s)", url,
             bool(getattr(sys, "frozen", False)))

    failures: list[BaseException] = []

    def serve() -> None:
        try:
            _serve(port)
        except BaseException as exc:  # noqa: BLE001 -- reported to the user
            failures.append(exc)
            log.exception("server failed")

    server = threading.Thread(target=serve, daemon=True, name="vellum-server")
    server.start()

    # Wait for the port to actually accept before pointing anything at it,
    # otherwise the window opens to a connection-refused page.
    ready = False
    deadline = time.time() + 15
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.25)
            if s.connect_ex((HOST, port)) == 0:
                ready = True
                break
        if failures:
            break
        time.sleep(0.1)

    if not ready:
        reason = f"\n\n{failures[0]}" if failures else ""
        _alert(f"Vellum could not start its server on {url}.{reason}"
               f"\n\nLog: {log_path or 'unavailable'}")
        return 1

    try:
        import webview  # type: ignore
    except Exception:  # noqa: BLE001 -- any failure here means "no window"
        webview = None

    if webview is not None:
        try:
            webview.create_window(
                "Vellum", url,
                width=1360, height=880, min_size=(980, 620),
                background_color="#17161A",
            )
            webview.start()
            log.info("window closed; exiting")
            return 0
        except Exception:  # noqa: BLE001 -- fall back to the browser
            log.exception("window failed; opening the browser instead")

    log.info("Vellum is running at %s", url)
    opened = False
    try:
        opened = webbrowser.open(url)
    except Exception:  # noqa: BLE001
        log.exception("could not open a browser")
    if not opened and sys.stderr is None:
        _alert(f"Vellum is running at {url}\n"
               "Open that address in a browser.")
        return 0
    print(f"Vellum is running at {url}")
    print("Close this window, or press Ctrl+C, to stop it.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("stopped")
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
