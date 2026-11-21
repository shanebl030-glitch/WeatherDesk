"""API key storage, and keeping keys out of logs and reports.

Where keys live:
  1. The desktop's keyring (GNOME Keyring / KWallet via the Secret Service), when
     the python3-keyring package and a real keyring are available. Keys are then
     encrypted at rest and unlocked with your login.
  2. Otherwise ~/.config/rbsb-weather/keys.json, readable only by you: the folder is
     0700 and the file 0600, written atomically so a crash can't leave a partial
     or world-readable file. The file is outside the source tree, and .gitignore
     also refuses key files, so keys can't end up on GitHub.

Keys are never written to settings.json, the forecast cache, the log or the
connection-test report. A logging filter removes saved keys, and anything that
looks like key=/appid=/apikey=/token= in a URL, from every log message.

Created By Shane BlackbuRn
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import tempfile
import threading
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger(__name__)

SERVICE = "RBSB-Weather"          # keyring service name; the provider id is the "user name"
MASK = "••••••••"

# ---- redaction ----------------------------------------------------------------------
_QUERY_SECRET = re.compile(r"(?i)\b(key|appid|apikey|api_key|token|access_token)=([^&\s'\"]+)")
_known: set[str] = set()          # keys loaded or saved this session (redacted everywhere)
_known_lock = threading.Lock()


def remember_secret(value: Optional[str]) -> None:
    if value and len(value) >= 4:
        with _known_lock:
            _known.add(value)


def redact(text: str, extra: Iterable[Optional[str]] = ()) -> str:
    """Text with any known API key, and any key-like URL parameter, replaced by •••."""
    if not text:
        return text
    with _known_lock:
        secrets = set(_known)
    secrets.update(s for s in extra if s and len(s) >= 4)
    for s in sorted(secrets, key=len, reverse=True):
        text = text.replace(s, MASK)
    return _QUERY_SECRET.sub(lambda m: f"{m.group(1)}={MASK}", text)


class RedactingFilter(logging.Filter):
    """Installed on RBSB-Weather's log handlers: no key can reach the log file or terminal."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        clean = redact(msg)
        if clean != msg:
            record.msg, record.args = clean, None
        if record.exc_info and record.exc_text is None:
            # format now so the traceback text can be cleaned too
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def install_log_redaction(logger: Optional[logging.Logger] = None) -> None:
    """Add the filter to every handler of `logger` (default: the root logger)."""
    lg = logger or logging.getLogger()
    for h in lg.handlers:
        if not any(isinstance(f, RedactingFilter) for f in h.filters):
            h.addFilter(RedactingFilter())


# ---- storage ------------------------------------------------------------------------
def _config_dir() -> Path:
    from .storage import config_dir
    return config_dir()


class KeyStore:
    """get/set/delete API keys by provider id. Uses the system keyring when available
    (unless RBSB_WEATHER_KEYRING=0), else a private file."""

    def __init__(self, directory: Optional[Path] = None, use_keyring: Optional[bool] = None):
        self.dir = Path(directory) if directory else _config_dir()
        self.path = self.dir / "keys.json"
        self._lock = threading.Lock()
        # Keys looked up this session (1.15 audit): the Weather Providers dialog and every
        # update ask for keys often, and each keyring lookup is a D-Bus call that can stall
        # the window (or ask to unlock the keyring). Only answers the keyring really gave
        # are kept, so a locked or failing keyring is asked again next time.
        self._cache: dict[str, Optional[str]] = {}
        if use_keyring is None:
            use_keyring = os.environ.get("RBSB_WEATHER_KEYRING", "1") != "0"
        self._keyring = _usable_keyring() if use_keyring else None

    @property
    def backend(self) -> str:
        """Plain-English description of where keys are kept (for the dialog)."""
        if self._keyring is not None:
            return "the system keyring (encrypted, unlocked with your login)"
        return f"{self.path} (private file, readable only by you)"

    # -- public
    def get(self, provider_id: str) -> Optional[str]:
        with self._lock:
            if provider_id in self._cache:
                return self._cache[provider_id]
        value = None
        reliable = True
        if self._keyring is not None:
            try:
                value = self._keyring.get_password(SERVICE, provider_id)
            except Exception as e:
                reliable = False
                log.warning("keyring read failed (%s); using the private key file", type(e).__name__)
        if value is None:
            value = self._read_file().get(provider_id)
        remember_secret(value)
        value = value or None
        if reliable:
            with self._lock:
                self._cache[provider_id] = value
        return value

    def has(self, provider_id: str) -> bool:
        return bool(self.get(provider_id))

    def set(self, provider_id: str, value: str) -> None:
        value = (value or "").strip()
        if not value:
            raise ValueError("The API key is empty.")
        if len(value) > 512 or any(c.isspace() for c in value):
            raise ValueError("That doesn't look like an API key (it contains spaces or is too long).")
        remember_secret(value)
        with self._lock:
            self._cache.pop(provider_id, None)
        if self._keyring is not None:
            try:
                self._keyring.set_password(SERVICE, provider_id, value)
                self._delete_from_file(provider_id)        # don't leave an older copy behind
                with self._lock:
                    self._cache[provider_id] = value
                return
            except Exception as e:
                log.warning("keyring write failed (%s); using the private key file", type(e).__name__)
        with self._lock:
            data = self._read_file()
            data[provider_id] = value
            self._write_file(data)
            self._cache[provider_id] = value

    def delete(self, provider_id: str) -> None:
        with self._lock:
            self._cache.pop(provider_id, None)
        if self._keyring is not None:
            try:
                self._keyring.delete_password(SERVICE, provider_id)
            except Exception:
                pass
        self._delete_from_file(provider_id)

    # -- private file
    def _read_file(self) -> dict:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return {}
        except OSError:
            return {}
        from .osplat import IS_WINDOWS
        # Windows has no Unix permission bits (every file reports 0o666); the file lives in
        # the user's own folder, and the system keyring (Credential Manager) is used first
        if not IS_WINDOWS and st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            # someone loosened the permissions: tighten them again before reading
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                log.warning("the API key file is readable by others and couldn't be fixed; ignoring it")
                return {}
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        keys = data.get("keys") if isinstance(data, dict) else None
        return {str(k): str(v) for k, v in (keys or {}).items() if isinstance(v, str) and v}

    def _write_file(self, data: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass
        fd, tmp = tempfile.mkstemp(prefix=".keys-", dir=str(self.dir))   # created 0600
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"note": "RBSB-Weather API keys. Keep private.", "keys": data}, f)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _delete_from_file(self, provider_id: str) -> None:
        with self._lock:
            data = self._read_file()
            if provider_id in data:
                del data[provider_id]
                if data:
                    self._write_file(data)
                else:
                    try:
                        self.path.unlink()
                    except OSError:
                        pass


def _usable_keyring():
    """The keyring module if it has a real, secure backend; else None."""
    try:
        import keyring
        from keyring.backends import fail
        kr = keyring.get_keyring()
    except Exception:
        return None
    name = (type(kr).__module__ + "." + type(kr).__name__).lower()
    if isinstance(kr, fail.Keyring) or any(x in name for x in ("fail", "null", "plaintext")):
        return None
    try:
        # a priority below 1 means the backend isn't really usable here
        if getattr(kr, "priority", 1) < 1:
            return None
    except Exception:
        return None
    return keyring
