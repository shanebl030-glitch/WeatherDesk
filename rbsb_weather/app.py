"""Entry point: argument parsing, logging, single-instance handling.

Created By Shane BlackbuRn
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
import time

from . import APP_ID, APP_NAME, AUTHOR_CREDIT, __version__


def _setup_logging(verbose: bool) -> None:
    from .storage import cache_dir
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        d = cache_dir()
        d.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            d / f"{APP_ID}.log", maxBytes=256_000, backupCount=2, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers)
    # API keys never reach the terminal or the log file (see keystore.py)
    from .keystore import install_log_redaction
    install_log_redaction()


def _console_safe() -> None:
    """A windowed Windows program (the portable RBSB-Weather.exe) has no console:
    sys.stdout and sys.stderr are None, and any print() would crash it. Send that output
    nowhere instead (the log file still records everything)."""
    import os
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    _console_safe()
    ap = argparse.ArgumentParser(prog=APP_ID, description=f"{APP_NAME} – desktop weather. {AUTHOR_CREDIT}")
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {__version__} – {AUTHOR_CREDIT}")
    ap.add_argument("--minimized", action="store_true", help="start hidden in the system tray")
    ap.add_argument("--verbose", action="store_true", help="debug logging")
    ap.add_argument("--diagnose", action="store_true",
                    help="test the connection to the weather service and exit")
    args = ap.parse_args(argv)
    if args.diagnose:
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
        from .keystore import install_log_redaction
        install_log_redaction()             # warnings printed during the test never show a key
        from .diagnostics import run_diagnostics
        print(run_diagnostics())
        return 0

    _setup_logging(args.verbose)
    log = logging.getLogger("rbsb_weather")
    try:
        import platform
        from .storage import config_dir
        log.info("RBSB-Weather %s starting on %s (Python %s); settings in %s", __version__,
                 platform.platform(terse=True), platform.python_version(), config_dir())
    except Exception:
        pass
    try:   # remember "use small packets" for servers behind a broken route, across restarts
        from . import net
        from .storage import cache_dir
        net.HEALTH_FILE = str(cache_dir() / "network.json")
    except Exception:
        pass

    from PyQt6.QtNetwork import QLocalServer
    from PyQt6.QtWidgets import QApplication

    # argv[0] = "rbsb-weather" so the window's WM_CLASS matches the .desktop file
    # (panel grouping and icon) even though we run via "python3 -m".
    app = QApplication([APP_ID])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setDesktopFileName(APP_ID)
    app.setOrganizationName("Shane BlackbuRn")

    # ---- single instance with version hand-over (see instance.py)
    from . import instance
    name = instance.server_name()
    status, path, detail = instance.contact_running(name, __version__)
    replaced = False
    if status == "same":
        log.info("RBSB-Weather is already running; brought it to the front.")
        print("RBSB-Weather is already running; brought it to the front.")
        return 0
    if status == "newer":
        msg = f"A newer RBSB-Weather ({detail}) is already running; leaving it in place."
        log.info(msg)
        print(msg)
        return 0
    if status in ("yield", "legacy"):
        log.info("an older RBSB-Weather is running (%s); replacing it with %s", status, __version__)
        print(f"Replacing the older RBSB-Weather that was running with version {__version__}…")
        if not instance.take_over(status, path):
            log.error("could not stop the older RBSB-Weather")
            print("The older RBSB-Weather could not be closed. Quit it from its tray menu "
                  "(or File → Quit) and start RBSB-Weather again.")
            return 1
        replaced = True
    # WeatherDesk, this app's name before 2.0.0, used its own socket: hand over from a
    # copy that's still running (it saves its settings as it quits), *then* copy its data.
    from . import migrate
    legacy = migrate.legacy_server_name()
    if legacy and status == "none":
        lstatus, lpath, _ = instance.contact_running(legacy, __version__)
        if lstatus in ("yield", "legacy"):
            log.info("WeatherDesk is running (%s); replacing it with RBSB-Weather %s", lstatus, __version__)
            print(f"Replacing the WeatherDesk that was running with RBSB-Weather {__version__}…")
            if not instance.take_over(lstatus, lpath):
                log.error("could not stop the running WeatherDesk")
                print("The running WeatherDesk could not be closed. Quit it from its tray menu "
                      "(or File → Quit) and start RBSB-Weather again.")
                return 1
            replaced = True
        elif lstatus in ("same", "newer"):   # can't happen with real versions; be safe
            log.warning("a WeatherDesk reporting version %s is running; leaving it", lstatus)
    carried = migrate.run()
    from .main_window import MainWindow
    win = MainWindow()

    # Start answering other launches only once the window exists, so a launch
    # request is always answered promptly by the event loop below.
    QLocalServer.removeServer(name)   # clear a stale socket left by a crash or the old copy
    server = QLocalServer()
    ok = server.listen(name)
    # Windows: an older copy that just handed over can hold its named pipe for a moment
    # after quitting; wait for it (up to 3 s) instead of running without the channel
    end = time.monotonic() + 3.0
    while not ok and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.1)
        QLocalServer.removeServer(name)
        ok = server.listen(name)
    if not ok:
        log.warning("single-instance server unavailable: %s", server.errorString())
    if carried:
        win.statusBar().showMessage(
            "Welcome to RBSB-Weather (formerly WeatherDesk). Carried over: "
            + ", ".join(carried) + ".", 30000)
    elif replaced:
        win.statusBar().showMessage(
            f"Updated to RBSB-Weather {__version__} – the older version that was running "
            f"has been closed.", 20000)

    # Answering other launches. Robust against launches that gave up waiting (for
    # example while this copy was suspended or busy): their sockets are already
    # closed by the time we read them. Earlier versions could crash here - a socket
    # was deleted while still inside its own signal (found by the "busy copy" test,
    # about 1 run in 25). Now sockets are only deleted from the event loop, actions
    # run after the socket's signal returns, and requests from launches that already
    # hung up are ignored (they have moved on).
    from PyQt6 import sip
    from PyQt6.QtCore import QTimer
    from PyQt6.QtNetwork import QLocalSocket
    live_conns: set = set()                 # keeps sockets alive until it's safe to delete

    def serve(conn) -> None:
        live_conns.add(conn)
        state = {"buf": bytearray(), "done": False}

        def release():
            if conn in live_conns:
                live_conns.discard(conn)
                if not sip.isdeleted(conn):
                    conn.deleteLater()

        def on_ready():
            if state["done"] or sip.isdeleted(conn):
                return
            state["buf"].extend(bytes(conn.readAll()))
            if b"\n" not in state["buf"] and len(state["buf"]) < 256:
                return
            state["done"] = True
            if (conn.state() != QLocalSocket.LocalSocketState.ConnectedState
                    or instance.peer_hung_up(int(conn.socketDescriptor()))):
                log.info("ignoring a launch request whose sender has already given up")
                return
            reply = instance.decide(bytes(state["buf"]), __version__)
            conn.write((reply + "\n").encode())
            conn.flush()
            if reply == "yield":
                log.info("a newer RBSB-Weather is starting; quitting so it can take over")
                QTimer.singleShot(150, win.quit_app)
            else:
                QTimer.singleShot(0, win.show_normal)
        conn.readyRead.connect(on_ready)
        conn.disconnected.connect(lambda: QTimer.singleShot(0, release))
        if conn.bytesAvailable():
            QTimer.singleShot(0, on_ready)

    def on_new_connection():
        while True:
            conn = server.nextPendingConnection()
            if conn is None:
                return
            serve(conn)
    server.newConnection.connect(on_new_connection)

    # The main window decides when the app ends (closeEvent -> app.quit), so
    # closing the User Guide or About dialog can never end the app by accident,
    # and a window hidden to the tray keeps running.
    app.setQuitOnLastWindowClosed(False)
    if not (args.minimized and win.tray is not None and win.tray.isVisible()):
        win.show()

    # Shut down cleanly (saving settings/geometry) on SIGTERM (logout, shutdown,
    # `kill`) and Ctrl+C. The no-op timer lets Python run its signal handlers
    # while Qt's event loop is in control.
    import signal

    def _on_signal(signum, _frame):
        log.info("received signal %s; quitting", signum)
        # Ignore any further TERM/INT while shutting down (they'd otherwise
        # interrupt the settings save or Qt's teardown).
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        win.quit_app()
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    sig_timer = QTimer()
    sig_timer.timeout.connect(lambda: None)
    sig_timer.start(250)

    rc = app.exec()
    sig_timer.stop()
    # Settings are already saved; don't let a late signal abort teardown.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    server.close()
    log.info("RBSB-Weather exited (%s)", rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
