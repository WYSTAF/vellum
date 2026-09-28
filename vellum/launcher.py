"""Start the app.

Three ways to present the same local server, in order of preference:

1. a native window via pywebview, if it is installed;
2. the default browser, pointed at the local address;
3. a plain terminal message with the URL, if even the browser cannot open.

The server binds to 127.0.0.1 only. This app reads a private archive and
writes nothing but an index and exports; it must not be reachable from the
network.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
import webbrowser

import uvicorn

from . import app as app_module

HOST = "127.0.0.1"


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


def _serve(port: int) -> None:
    uvicorn.run(app_module.app, host=HOST, port=port, log_level="warning")


def main() -> int:
    port = free_port()
    url = f"http://{HOST}:{port}/"

    server = threading.Thread(target=_serve, args=(port,), daemon=True,
                              name="vellum-server")
    server.start()

    # Wait for the port to actually accept before pointing anything at it,
    # otherwise the window opens to a connection-refused page.
    deadline = time.time() + 15
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.25)
            if s.connect_ex((HOST, port)) == 0:
                break
        time.sleep(0.1)
    else:
        print(f"Could not start the server on {url}", file=sys.stderr)
        return 1

    try:
        import webview  # type: ignore
    except ImportError:
        webview = None

    if webview is not None:
        try:
            window = webview.create_window(
                "Vellum", url,
                width=1360, height=880, min_size=(980, 620),
                background_color="#17161A",
            )
            webview.start()
            return 0
        except Exception as exc:  # noqa: BLE001 -- fall back to the browser
            print(f"Window failed ({exc}); opening the browser instead.",
                  file=sys.stderr)

    print(f"Vellum is running at {url}")
    print("Close this window, or press Ctrl+C, to stop it.")
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
