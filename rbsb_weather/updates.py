"""Check for updates (2.1.0): is there a newer RBSB-Weather release on GitHub?

* check()    asks GitHub for the latest published release of
             shanebl030-glitch/RBSB-Weather and reads its version, notes and files.
* download() fetches the release's .deb into a folder (normally Downloads) and
             checks it against the release's SHA256SUMS before keeping it. A file
             that doesn't match is deleted, never offered for installing.

Installing is left to the desktop's package installer (Mint asks for your
password as usual). RBSB-Weather itself never runs anything as root.

Safety:
* only https, and only GitHub's own addresses (github.com, api.github.com and
  its *.githubusercontent.com download hosts), including after redirects;
* size limits on every response (1 MB of release data, 64 KB of checksums,
  100 MB for the package);
* the file name must look like rbsb-weather_<version>_all.deb, the download
  must start like a Debian package, and must match its SHA256 checksum;
* downloads are written to a temporary file and only renamed into place once
  verified; an existing file is never overwritten.

RBSB_WEATHER_UPDATE_URL points the check at another address. The tests use it
for a local fake GitHub; plain http is then allowed only for this computer
(127.0.0.1 / localhost).

When to check automatically is decided by the main window (see due()). The
time of the last check is kept in the cache folder (updates.json), not in
settings.json.

Created By Shane BlackbuRn
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
import ssl
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import __version__
from .instance import compare_versions

log = logging.getLogger(__name__)

REPO = "shanebl030-glitch/RBSB-Weather"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"

CHECK_EVERY_S = 24 * 3600          # automatic check: once a day...
RETRY_AFTER_FAIL_S = 3 * 3600      # ...or 3 hours after a check that failed
FIRST_CHECK_DELAY_S = 60           # never in the first minute (startup stays quick)
TIMEOUT_S = 20.0

MAX_RELEASE_BYTES = 1024 * 1024
MAX_SUMS_BYTES = 64 * 1024
MAX_DEB_BYTES = 300 * 1024 * 1024     # the Windows portable zip (Python and Qt inside) is the larger
MAX_NOTES_CHARS = 20_000
CHUNK = 64 * 1024

_VERSION_RE = re.compile(r"\d{1,4}(\.\d{1,4}){0,3}")
_DEB_RE = re.compile(r"rbsb-weather_\d{1,4}(\.\d{1,4}){0,3}_all\.deb"
                     r"|rbsb-weather-\d{1,4}(\.\d{1,4}){0,3}-windows-portable\.zip")
_GITHUB_HOSTS = ("github.com", "api.github.com")
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
DEB_MAGIC = b"!<arch>\ndebian-binary"
ZIP_MAGIC = b"PK\x03\x04"


def package_name(version: str, windows: Optional[bool] = None) -> str:
    """The release file this system installs: the .deb on Linux, the portable zip on Windows."""
    from .osplat import IS_WINDOWS
    win = IS_WINDOWS if windows is None else windows
    return f"rbsb-weather-{version}-windows-portable.zip" if win else f"rbsb-weather_{version}_all.deb"


class UpdateError(Exception):
    """A plain-English reason the check or download didn't work."""


class UpdateCancelled(UpdateError):
    pass


@dataclass
class UpdateInfo:
    version: str                    # "2.1.0"
    tag: str                        # "v2.1.0"
    title: str                      # "RBSB-Weather 2.1.0"
    notes: str                      # the release description (Markdown)
    page_url: str                   # the release's page on GitHub
    published: str = ""             # ISO time
    deb_name: str = ""              # the package for this system (.deb, or the Windows zip);
                                    # "" when the release has none
    deb_url: str = ""
    deb_size: int = 0
    sums_url: str = ""

    @property
    def installable(self) -> bool:
        return bool(self.deb_url and self.sums_url)


def version_from_tag(tag: str) -> Optional[str]:
    t = (tag or "").strip()
    if t[:1] in ("v", "V"):
        t = t[1:]
    return t if _VERSION_RE.fullmatch(t) else None


def is_newer(info: UpdateInfo, current: str = __version__) -> bool:
    return compare_versions(info.version, current) > 0


def parse_release(data, windows: Optional[bool] = None) -> UpdateInfo:
    """Read GitHub's "latest release" reply. Raises UpdateError if it isn't usable."""
    if not isinstance(data, dict):
        raise UpdateError("GitHub sent release information that couldn't be read.")
    if data.get("draft") or data.get("prerelease"):
        raise UpdateError("The newest release on GitHub is not a finished release.")
    tag = data.get("tag_name")
    version = version_from_tag(tag) if isinstance(tag, str) else None
    if not version:
        raise UpdateError("The newest release on GitHub has no version number RBSB-Weather understands.")
    page = data.get("html_url")
    if not (isinstance(page, str) and page.startswith("https://github.com/")):
        page = RELEASES_PAGE
    notes = data.get("body") if isinstance(data.get("body"), str) else ""
    if len(notes) > MAX_NOTES_CHARS:
        notes = notes[:MAX_NOTES_CHARS] + "\n\n… (see the full notes on GitHub)"
    title = data.get("name") if isinstance(data.get("name"), str) and data.get("name").strip() else f"RBSB-Weather {version}"
    info = UpdateInfo(version=version, tag=tag, title=title.strip()[:200], notes=notes, page_url=page,
                      published=str(data.get("published_at") or "")[:40])
    want = package_name(version, windows)
    for a in data.get("assets") or []:
        if not isinstance(a, dict):
            continue
        name, url = a.get("name"), a.get("browser_download_url")
        if not (isinstance(name, str) and isinstance(url, str)):
            continue
        if name == want and _DEB_RE.fullmatch(name):
            info.deb_name, info.deb_url = name, url
            size = a.get("size")
            info.deb_size = size if isinstance(size, int) and not isinstance(size, bool) and size > 0 else 0
        elif name == "SHA256SUMS":
            info.sums_url = url
    return info


def parse_sums(text: str, name: str) -> Optional[str]:
    """The SHA256 for `name` in a sha256sum-style file ("<hex>  <name>" or "<hex> *<name>")."""
    for line in text.splitlines():
        m = re.fullmatch(r"\s*([0-9a-fA-F]{64})\s+\*?(.+?)\s*", line)
        if m and m.group(2) == name:
            return m.group(1).lower()
    return None


def due(state: dict, now: Optional[float] = None) -> bool:
    """Is an automatic check due? Once a day after a successful check, 3 hours after a
    failed one; a clock that jumped backwards (a stored time in the future) counts as due."""
    now = time.time() if now is None else now
    ok = _num(state.get("last_ok"))
    tried = _num(state.get("last_attempt"))
    if tried > now + 3600 or ok > now + 3600:
        return True
    if now - tried < RETRY_AFTER_FAIL_S:
        return False
    return now - ok >= CHECK_EVERY_S


def _num(v) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


# ---------------------------------------------------------------- state file
def state_path() -> Path:
    from .storage import cache_dir
    return cache_dir() / "updates.json"


def load_state(path: Optional[Path] = None) -> dict:
    try:
        with open(path or state_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict, path: Optional[Path] = None) -> None:
    from .storage import atomic_write_json
    try:
        atomic_write_json(path or state_path(), state)
    except OSError as e:
        log.warning("could not save the update-check time: %s", e)


# ---------------------------------------------------------------- network
class _GitHubOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to addresses the client allows (GitHub's own)."""

    def __init__(self, allowed: Callable[[str], bool]):
        super().__init__()
        self._allowed = allowed

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not self._allowed(newurl):
            raise UpdateError("The download was redirected somewhere other than GitHub, so it was stopped.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class UpdateClient:
    def __init__(self, latest_url: Optional[str] = None, timeout: float = TIMEOUT_S, ssl_context=None):
        env = os.environ.get("RBSB_WEATHER_UPDATE_URL")
        self.latest_url = latest_url or env or LATEST_URL
        host = urllib.parse.urlsplit(self.latest_url).hostname or ""
        # plain http only for a test server on this computer
        self.allow_local = self.latest_url != LATEST_URL and host in _LOCAL_HOSTS
        self.timeout = timeout
        self.user_agent = f"RBSB-Weather/{__version__} (Created By Shane BlackbuRn)"
        from .net import build_opener
        self._opener = build_opener(ssl_context, _GitHubOnlyRedirect(self.url_allowed))

    def url_allowed(self, url: str) -> bool:
        try:
            u = urllib.parse.urlsplit(url)
        except ValueError:
            return False
        host = (u.hostname or "").lower()
        if self.allow_local and host in _LOCAL_HOSTS and u.scheme in ("http", "https"):
            return True
        if u.scheme != "https":
            return False
        return host in _GITHUB_HOSTS or host.endswith(".githubusercontent.com")

    # -- check
    def check(self) -> UpdateInfo:
        """The latest release. One retry after a timeout or dropped connection."""
        for attempt in (1, 2):
            try:
                raw = self._get(self.latest_url, MAX_RELEASE_BYTES, "application/vnd.github+json")
                break
            except _Retryable as e:
                if attempt == 2:
                    raise UpdateError(str(e)) from None
                log.info("update check: %s; trying once more", e)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise UpdateError("GitHub sent release information that couldn't be read.") from None
        info = parse_release(data)
        log.info("update check: latest release is %s (this is %s)", info.version, __version__)
        return info

    # -- download
    def download(self, info: UpdateInfo, folder: Path,
                 progress: Optional[Callable[[int, int], None]] = None,
                 cancelled: Optional[Callable[[], bool]] = None) -> Path:
        """Download and verify the release's .deb into `folder`. Returns its path."""
        if not info.deb_url or not _DEB_RE.fullmatch(info.deb_name or ""):
            raise UpdateError("This release has no installer package for RBSB-Weather. "
                              "You can download it from GitHub instead.")
        if not info.sums_url:
            raise UpdateError("This release has no checksum file, so the download couldn't be checked. "
                              "You can download it from GitHub instead.")
        for url in (info.deb_url, info.sums_url):
            if not self.url_allowed(url):
                raise UpdateError("The release's files aren't on GitHub, so they weren't downloaded.")
        try:
            sums = self._get(info.sums_url, MAX_SUMS_BYTES, "text/plain").decode("utf-8", "replace")
        except _Retryable as e:
            raise UpdateError(str(e)) from None
        expected = parse_sums(sums, info.deb_name)
        if not expected:
            raise UpdateError(f"The release's checksum file doesn't list {info.deb_name}, "
                              "so the download couldn't be checked.")
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / info.deb_name
        if target.is_file() and _sha256(target) == expected:
            log.info("update: %s is already downloaded and verified", target)
            return target                                   # downloaded before: reuse it
        fd, tmp = tempfile.mkstemp(prefix=".rbsb-weather-update-", suffix=".part", dir=str(folder))
        h = hashlib.sha256()
        done = 0
        try:
            with os.fdopen(fd, "wb") as out:
                req = urllib.request.Request(info.deb_url, headers={"User-Agent": self.user_agent,
                                                                    "Accept": "application/octet-stream"})
                try:
                    with self._opener.open(req, timeout=self.timeout) as resp:
                        self._check_final_url(resp)
                        total = info.deb_size or _int(resp.headers.get("Content-Length"))
                        if total > MAX_DEB_BYTES:
                            raise UpdateError("The update is larger than expected, so it wasn't downloaded.")
                        first = True
                        while True:
                            if cancelled and cancelled():
                                raise UpdateCancelled("Download cancelled.")
                            chunk = resp.read(CHUNK)
                            if not chunk:
                                break
                            magic = ZIP_MAGIC if info.deb_name.endswith(".zip") else DEB_MAGIC
                            if first and not chunk.startswith(magic[:min(len(chunk), len(magic))]):
                                raise UpdateError("The download isn't an installer package, so it was deleted.")
                            first = False
                            done += len(chunk)
                            if done > MAX_DEB_BYTES:
                                raise UpdateError("The update is larger than expected, so it was deleted.")
                            h.update(chunk)
                            out.write(chunk)
                            if progress:
                                progress(done, total)
                except UpdateError:
                    raise
                except Exception as e:
                    raise UpdateError(self._reason(e, "downloading the update")) from None
                out.flush()
                os.fsync(out.fileno())
            if info.deb_size and done != info.deb_size:
                raise UpdateError("The download was incomplete, so it was deleted. Please try again.")
            if h.hexdigest() != expected:
                raise UpdateError("The download didn't match its checksum (it may have been damaged on "
                                  "the way), so it was deleted. Please try again.")
            os.chmod(tmp, 0o644)
            final = _free_name(target)
            os.rename(tmp, final)
            log.info("update: downloaded and verified %s (%d bytes)", final, done)
            return final
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- helpers
    def _get(self, url: str, limit: int, accept: str) -> bytes:
        if not self.url_allowed(url):
            raise UpdateError("Update checks only go to GitHub.")
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent, "Accept": accept})
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                self._check_final_url(resp)
                raw = resp.read(limit + 1)
        except UpdateError:
            raise
        except urllib.error.HTTPError as e:
            code = e.code
            remaining = (e.headers or {}).get("X-RateLimit-Remaining")
            e.close()
            if code in (403, 429) and remaining == "0":
                raise UpdateError("GitHub's limit on update checks was reached for now. "
                                  "It will be checked again later.") from None
            if code == 404:
                raise UpdateError("No published release of RBSB-Weather was found on GitHub.") from None
            raise UpdateError(f"GitHub returned an error (HTTP {code}).") from None
        except (socket.timeout, TimeoutError, ConnectionResetError, ConnectionAbortedError) as e:
            raise _Retryable(self._reason(e, "checking for updates")) from None
        except urllib.error.URLError as e:
            if isinstance(e.reason, UpdateError):
                raise e.reason from None
            if isinstance(e.reason, (socket.timeout, TimeoutError, ConnectionResetError)):
                raise _Retryable(self._reason(e.reason, "checking for updates")) from None
            raise UpdateError(self._reason(e, "checking for updates")) from None
        except (ssl.SSLError, OSError) as e:
            raise UpdateError(self._reason(e, "checking for updates")) from None
        if len(raw) > limit:
            raise UpdateError("GitHub sent more data than expected, so it was ignored.")
        return raw

    def _check_final_url(self, resp) -> None:
        url = resp.geturl() if hasattr(resp, "geturl") else None
        if url and not self.url_allowed(url):
            raise UpdateError("The download was redirected somewhere other than GitHub, so it was stopped.")

    @staticmethod
    def _reason(e: BaseException, doing: str) -> str:
        if isinstance(e, urllib.error.URLError):
            e = e.reason if isinstance(e.reason, BaseException) else e
        if isinstance(e, (socket.timeout, TimeoutError)):
            return f"GitHub didn't respond in time while {doing}."
        if isinstance(e, ssl.SSLCertVerificationError):
            return f"The secure connection to GitHub couldn't be verified while {doing}."
        return f"Couldn't reach GitHub while {doing} ({e})."


class _Retryable(Exception):
    pass


def _int(v) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(CHUNK), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _free_name(target: Path) -> Path:
    """`target`, or rbsb-weather_X_all-2.deb, -3 ... if a different file has that name."""
    if not target.exists():
        return target
    for n in range(2, 100):
        p = target.with_name(f"{target.stem}-{n}{target.suffix}")
        if not p.exists():
            return p
    raise UpdateError(f"Too many older copies of {target.name} in {target.parent}.")


def downloads_folder() -> Path:
    """The user's Downloads folder (XDG), else their home folder."""
    try:
        from PyQt6.QtCore import QStandardPaths
        d = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
        if d:
            return Path(d)
    except Exception:
        pass
    p = Path.home() / "Downloads"
    return p if p.is_dir() else Path.home()
