"""3.0.0: Linux/Windows differences (rbsb_weather/osplat.py), tested on any system.

Windows behaviour is simulated here (IS_WINDOWS switched on, a stand-in for the
Windows registry), so it's checked on every Linux run too; GitHub's Windows
machines run the whole suite for real (.github/workflows/windows.yml).

Created By Shane BlackbuRn
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path

from helpers import TempEnvTestCase

from rbsb_weather import osplat, updates


class FakeWinreg(types.SimpleNamespace):
    """Just enough of winreg for the Run key."""
    HKEY_CURRENT_USER = "HKCU"
    REG_SZ = 1

    def __init__(self):
        super().__init__()
        self.store: dict = {}

    class _Key:
        def __init__(self, reg, path):
            self.reg, self.path = reg, path

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def OpenKey(self, root, path):
        if path not in self.store:
            raise FileNotFoundError(path)
        return self._Key(self, path)

    def CreateKey(self, root, path):
        self.store.setdefault(path, {})
        return self._Key(self, path)

    def QueryValueEx(self, key, name):
        vals = self.store.get(key.path, {})
        if name not in vals:
            raise FileNotFoundError(name)
        return vals[name], self.REG_SZ

    def SetValueEx(self, key, name, _r, _t, value):
        self.store[key.path][name] = value

    def DeleteValue(self, key, name):
        if name not in self.store.get(key.path, {}):
            raise FileNotFoundError(name)
        del self.store[key.path][name]


class WindowsSim(unittest.TestCase):
    """Runs a test as if on Windows (portable build in a temporary folder)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rbsb-win-"))
        self.saved = (osplat.IS_WINDOWS, osplat.IS_FROZEN, osplat._data_root, osplat._winreg,
                      sys.executable, os.environ.get("LOCALAPPDATA"), os.environ.get("RBSB_WEATHER_PORTABLE_DIR"))
        self.reg = FakeWinreg()
        osplat.IS_WINDOWS = True
        osplat.IS_FROZEN = True
        osplat._data_root = None
        osplat._winreg = lambda: self.reg
        self.prog = self.tmp / "RBSB-Weather"
        self.prog.mkdir()
        sys.executable = str(self.prog / "RBSB-Weather.exe")
        os.environ["LOCALAPPDATA"] = str(self.tmp / "AppData" / "Local")
        os.environ.pop("RBSB_WEATHER_PORTABLE_DIR", None)

    def tearDown(self):
        (osplat.IS_WINDOWS, osplat.IS_FROZEN, osplat._data_root, osplat._winreg, sys.executable,
         local, portable) = self.saved
        for k, v in (("LOCALAPPDATA", local), ("RBSB_WEATHER_PORTABLE_DIR", portable)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        try:
            os.chmod(self.prog, 0o755)
        except OSError:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestWindowsFolders(WindowsSim):
    def test_portable_data_next_to_the_exe(self):
        self.assertEqual(osplat.config_base(), self.prog / "RBSB-Weather-data" / "config")
        self.assertEqual(osplat.cache_base(), self.prog / "RBSB-Weather-data" / "cache")
        self.assertTrue(osplat.is_portable())
        self.assertEqual(list((self.prog / "RBSB-Weather-data").iterdir()), [], "the write test leaves nothing")

    def test_read_only_program_folder_uses_localappdata(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root can write to read-only folders")
        os.chmod(self.prog, 0o555)
        self.assertEqual(osplat.config_base(), self.tmp / "AppData" / "Local" / "RBSB-Weather" / "config")
        self.assertFalse(osplat.is_portable())

    def test_unwritable_portable_folder_uses_localappdata(self):
        """Same as above, but works as root: a FILE where the data folder would go."""
        (self.prog / "RBSB-Weather-data").write_text("not a folder")
        self.assertEqual(osplat.cache_base(), self.tmp / "AppData" / "Local" / "RBSB-Weather" / "cache")

    def test_from_source_uses_localappdata(self):
        osplat.IS_FROZEN = False
        self.assertEqual(osplat.config_base(), self.tmp / "AppData" / "Local" / "RBSB-Weather" / "config")

    def test_override(self):
        os.environ["RBSB_WEATHER_PORTABLE_DIR"] = str(self.tmp / "elsewhere")
        self.assertEqual(osplat.config_base(), self.tmp / "elsewhere" / "config")

    def test_storage_follows(self):
        from rbsb_weather import storage
        env = {k: os.environ.pop(k, None) for k in ("RBSB_WEATHER_CONFIG_DIR", "RBSB_WEATHER_CACHE_DIR")}
        try:
            self.assertEqual(storage.config_dir(), self.prog / "RBSB-Weather-data" / "config")
            self.assertEqual(storage.cache_dir(), self.prog / "RBSB-Weather-data" / "cache")
        finally:
            for k, v in env.items():
                if v is not None:
                    os.environ[k] = v


class TestWindowsAutostart(WindowsSim):
    def test_run_key_on_and_off(self):
        self.assertFalse(osplat.autostart_enabled())
        osplat.set_autostart(True)
        self.assertTrue(osplat.autostart_enabled())
        cmd = self.reg.store[osplat.RUN_KEY]["RBSB-Weather"]
        self.assertEqual(cmd, f'"{self.prog / "RBSB-Weather.exe"}" --minimized')
        osplat.set_autostart(False)
        self.assertFalse(osplat.autostart_enabled())
        osplat.set_autostart(False)                          # twice is harmless

    def test_moved_folder_is_followed_when_turned_on_again(self):
        osplat.set_autostart(True)
        sys.executable = str(self.tmp / "USB" / "RBSB-Weather" / "RBSB-Weather.exe")
        osplat.set_autostart(True)
        self.assertIn(str(self.tmp / "USB"), self.reg.store[osplat.RUN_KEY]["RBSB-Weather"])


class TestWindowsMisc(WindowsSim):
    def test_user_id_without_getuid(self):
        had = hasattr(os, "getuid")
        real = os.getuid if had else None
        try:
            if had:
                del os.getuid
            os.environ["USERNAME"] = "Shane BlackbuRn"
            self.assertEqual(osplat.user_id(), "Shane_BlackbuRn")
            from rbsb_weather import instance
            env = os.environ.pop("RBSB_WEATHER_INSTANCE", None)
            try:
                self.assertEqual(instance.server_name(), "rbsb-weather-Shane_BlackbuRn")
            finally:
                if env is not None:
                    os.environ["RBSB_WEATHER_INSTANCE"] = env
        finally:
            if had:
                os.getuid = real

    def test_no_weatherdesk_carry_over_on_windows(self):
        from rbsb_weather import migrate
        env = {k: os.environ.pop(k, None) for k in ("RBSB_WEATHER_CONFIG_DIR", "RBSB_WEATHER_CACHE_DIR",
                                                    "RBSB_WEATHER_INSTANCE", "RBSB_WEATHER_LEGACY_INSTANCE")}
        try:
            self.assertEqual(migrate.run(), [])
            self.assertIsNone(migrate.legacy_server_name())
        finally:
            for k, v in env.items():
                if v is not None:
                    os.environ[k] = v

    def test_keys_file_not_ignored_without_unix_permissions(self):
        from rbsb_weather.keystore import KeyStore
        ks = KeyStore(self.tmp / "cfg", use_keyring=False)
        ks.set("tomorrow", "key-abc-123")
        os.chmod(ks.path, 0o666)                     # what Windows reports for every file
        ks2 = KeyStore(self.tmp / "cfg", use_keyring=False)
        self.assertEqual(ks2.get("tomorrow"), "key-abc-123")

    def test_windows_picks_the_portable_zip(self):
        rel = {"tag_name": "v3.1.0", "assets": [
            {"name": "rbsb-weather_3.1.0_all.deb", "browser_download_url": "https://github.com/x.deb"},
            {"name": "rbsb-weather-3.1.0-windows-portable.zip", "browser_download_url": "https://github.com/x.zip"},
            {"name": "SHA256SUMS", "browser_download_url": "https://github.com/s"}]}
        self.assertEqual(updates.parse_release(rel).deb_name, "rbsb-weather-3.1.0-windows-portable.zip")
        self.assertEqual(updates.parse_release(rel, windows=False).deb_name, "rbsb-weather_3.1.0_all.deb")


class TestZipUpdateDownload(unittest.TestCase):
    def test_zip_downloaded_and_checked(self):
        from test_updates import FakeGitHub
        gh = FakeGitHub()
        dl = Path(tempfile.mkdtemp(prefix="rbsb-dl-"))
        try:
            gh.windows = True
            gh.deb = b"PK\x03\x04" + b"zipdata" * 50000
            c = updates.UpdateClient(gh.url, timeout=3)
            info = updates.parse_release(updates.json.loads(c._get(gh.url, 10**6, "application/json")), windows=True)
            path = c.download(info, dl)
            self.assertEqual(path.name, "rbsb-weather-9.1.0-windows-portable.zip")
            self.assertEqual(path.read_bytes(), gh.deb)
            gh.deb = b"<html>not a zip</html>" * 1000
            path.unlink()
            with self.assertRaisesRegex(updates.UpdateError, "isn't an installer package"):
                c.download(info, dl)
        finally:
            gh.stop()
            shutil.rmtree(dl, ignore_errors=True)


class TestLinuxUnchanged(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "Linux paths")
    def test_linux_paths_and_autostart(self):
        self.assertFalse(osplat.IS_WINDOWS)
        env = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = "/tmp/x-config"
        try:
            self.assertEqual(osplat.config_base(), Path("/tmp/x-config/rbsb-weather"))
        finally:
            if env is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = env
        self.assertEqual(updates.package_name("3.0.0"), "rbsb-weather_3.0.0_all.deb")


class TestUpdateDialogZipWording(TempEnvTestCase):
    def test_windows_instructions(self):
        from helpers import qapp
        qapp()
        from rbsb_weather.update_dialog import UpdateDialog
        d = UpdateDialog(None, "3.0.0")
        d._show_downloaded_zip("C:/Users/x/Downloads/rbsb-weather-3.1.0-windows-portable.zip", True)
        t = d.detail.text()
        self.assertIn("RBSB-Weather-data", t)
        self.assertIn("replace the files", t)
        self.assertFalse(d.btn_copy.isVisibleTo(d), "no apt command on Windows")
        d.deleteLater()


if __name__ == "__main__":
    unittest.main()
