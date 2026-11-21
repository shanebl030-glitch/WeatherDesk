"""What differs between Linux and Windows (3.0.0), kept in one place.

* Where settings and the cache live:
  - Linux: ~/.config/rbsb-weather and ~/.cache/rbsb-weather (XDG), as before.
  - Windows, portable (the RBSB-Weather.exe build): a "RBSB-Weather-data"
    folder next to RBSB-Weather.exe, so the whole folder can be copied or run
    from a USB stick with its places and settings. If that folder can't be
    written (for example the app was put in Program Files), the user's
    %LOCALAPPDATA%\\RBSB-Weather is used instead.
  - Windows, run from source: %LOCALAPPDATA%\\RBSB-Weather.
* "Start at login":
  - Linux: ~/.config/autostart/rbsb-weather.desktop, as before.
  - Windows: a value named "RBSB-Weather" under the current user's
    HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run key, pointing at this
    RBSB-Weather.exe (so it follows the portable folder's current location
    whenever it's turned on). No administrator rights are needed.
* A per-user name for the single-instance channel (Linux user id; Windows user name).
* Showing a file in the file manager.

Created By Shane BlackbuRn
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from . import APP_ID, APP_NAME

IS_WINDOWS = sys.platform == "win32"
IS_FROZEN = bool(getattr(sys, "frozen", False))          # the packaged RBSB-Weather.exe
PORTABLE_DIR_NAME = "RBSB-Weather-data"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = APP_NAME


def program_dir() -> Path:
    """The folder holding RBSB-Weather.exe (packaged) or the project (from source)."""
    if IS_FROZEN:
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _writable(d: Path) -> bool:
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


_data_root: Optional[Path] = None


def windows_data_root() -> Path:
    """Windows: the folder holding config\\ and cache\\ (see the module notes)."""
    global _data_root
    env = os.environ.get("RBSB_WEATHER_PORTABLE_DIR")
    if env:
        return Path(env)
    if _data_root is None:
        local = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / APP_NAME
        root = local
        if IS_FROZEN:
            portable = program_dir() / PORTABLE_DIR_NAME
            if _writable(portable):
                root = portable
        _data_root = root
    return _data_root


def is_portable() -> bool:
    return IS_WINDOWS and windows_data_root() == program_dir() / PORTABLE_DIR_NAME


def config_base() -> Path:
    if IS_WINDOWS:
        return windows_data_root() / "config"
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(Path.home(), ".config")
    return Path(base) / APP_ID


def cache_base() -> Path:
    if IS_WINDOWS:
        return windows_data_root() / "cache"
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(Path.home(), ".cache")
    return Path(base) / APP_ID


def user_id() -> str:
    """Something that identifies the logged-in user, for per-user channel names."""
    if hasattr(os, "getuid"):
        return str(os.getuid())
    name = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:40]


# ------------------------------------------------------------------ start at login
def launch_command(minimized: bool = True) -> str:
    """The command Windows runs at login."""
    if IS_FROZEN:
        cmd = f'"{sys.executable}"'
    else:
        exe = Path(sys.executable)
        pyw = exe.with_name("pythonw.exe")
        cmd = f'"{pyw if pyw.exists() else exe}" -m rbsb_weather'
    return cmd + (" --minimized" if minimized else "")


def _winreg():
    import winreg  # only on Windows
    return winreg


def autostart_enabled() -> bool:
    if IS_WINDOWS:
        try:
            w = _winreg()
            with w.OpenKey(w.HKEY_CURRENT_USER, RUN_KEY) as k:
                w.QueryValueEx(k, RUN_VALUE)
            return True
        except OSError:
            return False
    from .storage import autostart_path
    return autostart_path().exists()


def set_autostart(on: bool) -> None:
    """Turn "Start at login" on or off. Raises OSError if it couldn't be changed."""
    if IS_WINDOWS:
        w = _winreg()
        with w.CreateKey(w.HKEY_CURRENT_USER, RUN_KEY) as k:
            if on:
                w.SetValueEx(k, RUN_VALUE, 0, w.REG_SZ, launch_command(True))
            else:
                try:
                    w.DeleteValue(k, RUN_VALUE)
                except FileNotFoundError:
                    pass
        return
    from .storage import AUTOSTART_ENTRY, autostart_path
    p = autostart_path()
    if on:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(AUTOSTART_ENTRY, encoding="utf-8")
    else:
        try:
            p.unlink()
        except FileNotFoundError:
            pass


# ------------------------------------------------------------------ file manager
def show_in_file_manager(path: str) -> bool:
    """Open the folder holding `path`, with the file selected where the system allows."""
    try:
        if IS_WINDOWS:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            return True
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices
        return QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))
    except Exception:
        return False
