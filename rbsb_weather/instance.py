"""Single-instance handling with version hand-over.

Only one RBSB-Weather runs per user. When it's launched again:

* same version already running  -> the running copy comes to the front;
                                   the new launch exits.
* OLDER version running (>=1.1.1) -> the old copy is asked to quit cleanly
                                   (it saves its settings), and the new
                                   version takes over. This makes upgrades
                                   with `apt install` take effect immediately.
* newer version running          -> it stays; the older launch exits.
* legacy copy (1.0.0-1.1.0, which
  can't answer the version question) -> it's sent SIGTERM (which those
                                   versions handle by saving and exiting
                                   cleanly), and the new version takes over.

To make sure nothing else is ever touched, the only process signalled is the
one that owns RBSB-Weather's single-instance socket, found through
/proc/net/unix and the process's open file descriptors.

WeatherDesk (this app's name before 2.0.0) listened on "weatherdesk-<uid>". At
startup RBSB-Weather also asks there, so a WeatherDesk still running after the
upgrade hands over the same way (2.0.0 is newer than any WeatherDesk, so it quits).

Created By Shane BlackbuRn
"""

from __future__ import annotations

import logging
import os
import signal
import time
from typing import Optional

from . import APP_ID

log = logging.getLogger(__name__)

CONNECT_MS = 300
REPLY_MS = 2000
LEGACY_ATTEMPTS = 3      # a busy-but-current copy answers a retry; a legacy copy never does


def server_name() -> str:
    # RBSB_WEATHER_INSTANCE lets the test suite use its own name, so running
    # the tests never interferes with a real RBSB-Weather you have open.
    from .osplat import user_id
    return os.environ.get("RBSB_WEATHER_INSTANCE") or f"{APP_ID}-{user_id()}"


def parse_version(v: str) -> Optional[tuple[int, ...]]:
    try:
        parts = tuple(int(x) for x in v.strip().split("."))
        return parts if parts else None
    except ValueError:
        return None


def compare_versions(a: str, b: str) -> int:
    """-1 if a<b, 0 if equal or unparseable, 1 if a>b."""
    pa, pb = parse_version(a), parse_version(b)
    if pa is None or pb is None:
        return 0
    n = max(len(pa), len(pb))
    pa += (0,) * (n - len(pa))
    pb += (0,) * (n - len(pb))
    return (pa > pb) - (pa < pb)


def decide(request: bytes, my_version: str) -> str:
    """Running instance: reply to a launch request. Returns 'ok', 'yield' or 'newer <v>'."""
    parts = request.decode("utf-8", "replace").split()
    if not parts or parts[0] != "show" or len(parts) < 2:
        return "ok"
    c = compare_versions(parts[1], my_version)
    if c > 0:
        return "yield"
    if c < 0:
        return f"newer {my_version}"
    return "ok"


def contact_running(name: str, my_version: str) -> tuple[str, Optional[str], str]:
    """New launch: talk to any running instance.

    Returns (status, socket_path, detail) where status is one of
    'none' (nothing running), 'same', 'yield' (it will quit), 'newer', 'legacy'.

    'legacy' is only reported after LEGACY_ATTEMPTS requests in a row get no
    answer, so a current copy that's momentarily busy is never mistaken for an
    old one (and closed).
    """
    result = ("none", None, "")
    for attempt in range(LEGACY_ATTEMPTS):
        result = _contact_once(name, my_version)
        if result[0] != "legacy":
            return result
        log.info("running copy did not answer (attempt %d/%d)", attempt + 1, LEGACY_ATTEMPTS)
    return result


def _contact_once(name: str, my_version: str) -> tuple[str, Optional[str], str]:
    from PyQt6.QtNetwork import QLocalSocket
    sock = QLocalSocket()
    sock.connectToServer(name)
    if not sock.waitForConnected(CONNECT_MS):
        return "none", None, ""
    path = sock.fullServerName()
    sock.write(f"show {my_version}\n".encode())
    sock.flush()
    sock.waitForBytesWritten(CONNECT_MS)
    reply = b""
    deadline = time.monotonic() + REPLY_MS / 1000
    while time.monotonic() < deadline and b"\n" not in reply:
        if sock.waitForReadyRead(int(max(1, (deadline - time.monotonic()) * 1000))):
            reply += bytes(sock.readAll())
        elif sock.state() != QLocalSocket.LocalSocketState.ConnectedState:
            break
    sock.disconnectFromServer()
    text = reply.decode("utf-8", "replace").strip()
    if text == "ok":
        return "same", path, ""
    if text == "yield":
        return "yield", path, ""
    if text.startswith("newer"):
        return "newer", path, text[6:].strip()
    return "legacy", path, text


def peer_hung_up(fd: int) -> bool:
    """True if the other end of this connected socket has closed it (a launch that gave
    up waiting). Peeks without consuming data and without blocking."""
    import socket as _socket
    if fd < 0:
        return True
    try:
        s = _socket.fromfd(fd, _socket.AF_UNIX, _socket.SOCK_STREAM)    # a duplicate fd
    except OSError:
        return True
    try:
        return s.recv(1, _socket.MSG_PEEK | _socket.MSG_DONTWAIT) == b""
    except (BlockingIOError, InterruptedError):
        return False                       # still connected, just nothing more to read
    except OSError:
        return True
    finally:
        s.close()                          # closes only the duplicate


def socket_owner_pids(path: str) -> list[int]:
    """PIDs (same user, not us) holding the unix socket bound at `path`. Linux only: on
    Windows (no /proc) this is always empty, and hand-over relies on the running copy
    quitting by itself when asked, which every version that exists on Windows does."""
    if not os.path.exists("/proc/net/unix"):
        return []
    inodes: set[str] = set()
    try:
        with open("/proc/net/unix", encoding="utf-8", errors="replace") as f:
            next(f, None)
            for line in f:
                parts = line.split()
                if len(parts) >= 8 and parts[7] == path:
                    inodes.add(parts[6])
    except OSError:
        return []
    if not inodes:
        return []
    me, uid = os.getpid(), os.getuid()
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == me:
            continue
        try:
            if os.stat(f"/proc/{entry}").st_uid != uid:
                continue
            for fd in os.listdir(f"/proc/{entry}/fd"):
                try:
                    link = os.readlink(f"/proc/{entry}/fd/{fd}")
                except OSError:
                    continue
                if link.startswith("socket:[") and link[8:-1] in inodes:
                    found.append(int(entry))
                    break
        except OSError:
            continue
    return found


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:   # a zombie (exited, not yet reaped) counts as gone
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False


def wait_until_gone(pids: list[int], timeout: float = 8.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not any(_alive(p) for p in pids):
            return True
        time.sleep(0.05)
    return not any(_alive(p) for p in pids)


# Module and command names of this app, and of WeatherDesk (its name before 2.0.0),
# so an older WeatherDesk that's still running can be handed over to.
_PROGRAM_NAMES = (b"rbsb_weather", b"rbsb-weather", b"weatherdesk")


def is_rbsb_weather(pid: int) -> bool:
    """Extra safety check: the process really is RBSB-Weather (python -m rbsb_weather,
    or an older WeatherDesk: python -m weatherdesk)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            args = f.read().split(b"\0")
    except OSError:
        return False
    return any(a == n or a.endswith(b"/" + n) or a.endswith(n + b"/__main__.py")
               for a in args for n in _PROGRAM_NAMES)


def take_over(status: str, path: Optional[str]) -> bool:
    """Make an older running copy exit. Returns True when it's gone."""
    pids = [p for p in (socket_owner_pids(path) if path else []) if is_rbsb_weather(p)]
    if status == "legacy":
        for p in pids:
            log.info("stopping older RBSB-Weather (pid %d) so the new version can start", p)
            try:
                os.kill(p, signal.SIGTERM)       # older versions save and exit on SIGTERM
            except ProcessLookupError:
                pass
    if not pids:                                 # it may already have gone
        time.sleep(0.3)
        return True
    if wait_until_gone(pids, 8.0):
        return True
    for p in pids:                               # last resort; settings writes are atomic
        log.warning("older RBSB-Weather (pid %d) did not exit; forcing it", p)
        try:
            os.kill(p, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return wait_until_gone(pids, 3.0)
