"""Carry a WeatherDesk user's data over to RBSB-Weather (the app's name before 2.0.0).

Runs once, at startup, before the main window loads its settings. Everything is
copied, never moved or deleted, so going back to a WeatherDesk package still finds
its data where it left it:

* ~/.config/weatherdesk -> ~/.config/rbsb-weather   settings, saved locations,
                                                    keys.json (stays 0600)
* ~/.cache/weatherdesk  -> ~/.cache/rbsb-weather    saved forecasts, cloud map,
                                                    provider status (not the logs)
* API keys in the system keyring ("WeatherDesk" service) are copied to the
  "RBSB-Weather" service. The old entries are left in place.
* "Start at login": ~/.config/autostart/weatherdesk.desktop is replaced by
  rbsb-weather.desktop. The old file is renamed to weatherdesk.desktop.migrated
  (not deleted) so both don't start at login.

A folder is only copied when the new one has no data of its own, so nothing
RBSB-Weather saved is ever overwritten. When the old folder has been handled, a
marker file in the new config folder stops this from running again.

Set RBSB_WEATHER_MIGRATE=0 to skip it. It is also skipped when the test-suite
folder overrides (RBSB_WEATHER_CONFIG_DIR / RBSB_WEATHER_CACHE_DIR) are set, so
running the tests never touches a real WeatherDesk profile.

Created By Shane BlackbuRn
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger(__name__)

LEGACY_ID = "weatherdesk"            # old folder, command, desktop-file and socket name
LEGACY_NAME = "WeatherDesk"
LEGACY_KEYRING_SERVICE = "WeatherDesk"
MARKER = ".migrated-from-weatherdesk"
_SKIP = ("*.log", "*.log.*", "__pycache__", "*.pyc", ".*.tmp")


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or os.path.join(Path.home(), default))


def legacy_config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / LEGACY_ID


def legacy_cache_dir() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache") / LEGACY_ID


def legacy_autostart_path() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "autostart" / f"{LEGACY_ID}.desktop"


def _is_skipped(name: str) -> bool:
    import fnmatch
    return any(fnmatch.fnmatch(name, pat) for pat in _SKIP)


def _has_own_data(folder: Path) -> bool:
    """True if `folder` holds anything besides logs (which RBSB-Weather writes at startup)."""
    try:
        return any(not _is_skipped(p.name) for p in folder.iterdir())
    except FileNotFoundError:
        return False
    except OSError:
        return True                      # can't tell: be safe, don't write into it


def copy_folder(old: Path, new: Path) -> bool:
    """Copy `old` to `new` unless `new` already has data. Returns True if copied.

    File modes are kept (keys.json stays 0600, folders keep 0700). A new folder is
    built under a temporary name and renamed into place, so an interrupted copy
    never leaves a half-filled folder that would block the next attempt."""
    if not old.is_dir() or _has_own_data(new):
        return False
    ignore = shutil.ignore_patterns(*_SKIP)
    if not new.exists():
        new.parent.mkdir(parents=True, exist_ok=True)
        tmp = new.parent / f".{new.name}.migrating-{os.getpid()}"
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            shutil.copytree(old, tmp, symlinks=True, ignore=ignore, copy_function=_copy)
            os.chmod(tmp, old.stat().st_mode & 0o7777)
            os.rename(tmp, new)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
    else:                                # only logs so far: merge, never replacing a file
        shutil.copytree(old, new, symlinks=True, ignore=ignore, dirs_exist_ok=True,
                        copy_function=_copy_if_absent)
        try:
            os.chmod(new, old.stat().st_mode & 0o7777)
        except OSError:
            pass
    return True


def _copy(src: str, dst: str) -> str:
    return shutil.copy2(src, dst)          # contents, mode and times


def _copy_if_absent(src: str, dst: str) -> str:
    if os.path.lexists(dst):
        return dst
    return _copy(src, dst)


def copy_keyring(provider_ids: Iterable[str], keyring_mod, new_service: str) -> list[str]:
    """Copy keys from the old keyring service to the new one, where the new one has
    none. Returns the provider ids copied. Old entries are kept."""
    moved = []
    for pid in provider_ids:
        try:
            old = keyring_mod.get_password(LEGACY_KEYRING_SERVICE, pid)
            if not old or keyring_mod.get_password(new_service, pid):
                continue
            keyring_mod.set_password(new_service, pid, old)
            moved.append(pid)
        except Exception as e:           # a locked or broken keyring must never stop startup
            log.warning("could not copy the %s key from the old keyring entry (%s)", pid, type(e).__name__)
    return moved


def move_autostart(old: Path, new: Path, entry: str) -> bool:
    """Swap the old "Start at login" file for the new one. Returns True if done."""
    if not old.exists():
        return False
    if not new.exists():
        new.parent.mkdir(parents=True, exist_ok=True)
        tmp = new.with_name(f".{new.name}.tmp")
        tmp.write_text(entry, encoding="utf-8")
        os.replace(tmp, new)
    target = old.with_name(old.name + ".migrated")
    if target.exists():
        target = old.with_name(f"{old.name}.migrated-{int(time.time())}")
    os.rename(old, target)
    return True


def migrate(*, old_config: Path, new_config: Path, old_cache: Path, new_cache: Path,
            old_autostart: Path, new_autostart: Path, autostart_entry: str,
            keyring_mod=None, provider_ids: Iterable[str] = (),
            new_service: str = "RBSB-Weather") -> list[str]:
    """Do the migration with explicit paths (what the tests call). Returns a list of
    plain-English notes about what was carried over (empty if nothing was)."""
    notes: list[str] = []
    marker = new_config / MARKER
    if old_config.is_dir() and not marker.exists():
        try:
            if copy_folder(old_config, new_config):
                notes.append("settings and saved locations")
        except OSError as e:
            log.error("could not copy the WeatherDesk settings from %s: %s", old_config, e)
            return notes                 # try again next start; don't write the marker
        try:
            if copy_folder(old_cache, new_cache):
                notes.append("saved forecasts")
        except OSError as e:             # only a cache: it refills on the next update
            log.warning("could not copy the WeatherDesk cache from %s: %s", old_cache, e)
        if keyring_mod is not None:
            moved = copy_keyring(provider_ids, keyring_mod, new_service)
            if moved:
                notes.append(f"{len(moved)} API key{'s' if len(moved) != 1 else ''} in the keyring")
        try:
            new_config.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({"from": str(old_config), "time": int(time.time()),
                                          "copied": notes}) + "\n", encoding="utf-8")
        except OSError as e:
            log.warning("could not write the migration marker: %s", e)
    try:                                 # separate: a WeatherDesk could add it back later
        if move_autostart(old_autostart, new_autostart, autostart_entry):
            notes.append("Start at login")
    except OSError as e:
        log.warning("could not move the WeatherDesk login entry: %s", e)
    if notes:
        log.info("carried over from %s: %s", LEGACY_NAME, ", ".join(notes))
    return notes


def run() -> list[str]:
    """Startup entry point: migrate the real profile (unless disabled)."""
    if os.environ.get("RBSB_WEATHER_MIGRATE", "1") == "0":
        return []
    from .osplat import IS_WINDOWS
    if IS_WINDOWS:                     # WeatherDesk was never on Windows: nothing to carry over
        return []
    if os.environ.get("RBSB_WEATHER_CONFIG_DIR") or os.environ.get("RBSB_WEATHER_CACHE_DIR"):
        return []
    from .storage import AUTOSTART_ENTRY, autostart_path, cache_dir, config_dir
    old_config, old_autostart = legacy_config_dir(), legacy_autostart_path()
    new_config = config_dir()
    if not old_autostart.exists() and (not old_config.is_dir() or (new_config / MARKER).exists()):
        return []                        # the usual case: two quick checks, nothing to do
    keyring_mod, ids = None, []
    if old_config.is_dir() and not (new_config / MARKER).exists():
        from .keystore import SERVICE, _usable_keyring
        if os.environ.get("RBSB_WEATHER_KEYRING", "1") != "0":
            keyring_mod = _usable_keyring()
        try:
            from . import providers
            ids = [i.id for i in providers.all_infos()]
        except Exception:
            ids = []
    else:
        from .keystore import SERVICE
    try:
        return migrate(old_config=old_config, new_config=new_config,
                       old_cache=legacy_cache_dir(), new_cache=cache_dir(),
                       old_autostart=old_autostart, new_autostart=autostart_path(),
                       autostart_entry=AUTOSTART_ENTRY, keyring_mod=keyring_mod,
                       provider_ids=ids, new_service=SERVICE)
    except Exception:                    # never stop the app from starting
        log.exception("carrying over WeatherDesk data failed")
        return []


def legacy_server_name() -> Optional[str]:
    """The single-instance socket a running WeatherDesk (1.x) listens on, or None
    when the test suite has its own instance name (then RBSB_WEATHER_LEGACY_INSTANCE
    can name a fake one)."""
    env = os.environ.get("RBSB_WEATHER_LEGACY_INSTANCE")
    if env is not None:
        return env or None
    if os.environ.get("RBSB_WEATHER_INSTANCE"):
        return None
    from .osplat import IS_WINDOWS, user_id
    if IS_WINDOWS:
        return None
    return f"{LEGACY_ID}-{user_id()}"
