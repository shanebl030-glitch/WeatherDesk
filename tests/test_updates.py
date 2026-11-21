"""2.1.0: Check for updates. Unit tests of rbsb_weather/updates.py and the window's
update flow, against a local fake GitHub (never the real one).

The fake answers like GitHub: /repos/.../releases/latest with release JSON, and
asset downloads that redirect from one host ("github.com") to another (the
download host), here 127.0.0.1 -> localhost.

Created By Shane BlackbuRn
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import shutil
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path

from helpers import TempEnvTestCase, qapp, wait_until

from rbsb_weather import __version__, updates
from rbsb_weather.storage import Settings
from rbsb_weather.updates import UpdateClient, UpdateError, UpdateInfo

NEW = "9.1.0"
DEB_NAME = f"rbsb-weather_{NEW}_all.deb"


def fake_deb(size=300_000) -> bytes:
    body = bytes((i * 7 + 3) % 256 for i in range(size))
    return updates.DEB_MAGIC + b"\n" + body


class FakeGitHub:
    """A tiny threaded HTTP server with GitHub's shape. Everything is configurable."""

    def __init__(self):
        self.deb = fake_deb()
        self.version = NEW
        self.sums = None                 # None: correct; or override text
        self.latest_status = 200
        self.latest_headers = {}
        self.latest_body = None          # None: built from fields; or raw bytes
        self.declared_size = None        # None: real size
        self.redirect_to = None          # None: localhost download host; or a URL prefix
        self.delay_first = 0.0
        self.requests: list[str] = []
        self.deb_gets = 0
        self.slow_deb = 0.0
        self.windows = False             # serve the Windows portable zip as the package
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body: bytes, headers=None, ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):
                outer.requests.append(self.path)
                if self.path.endswith("/releases/latest"):
                    if outer.delay_first and len(outer.requests) == 1:
                        time.sleep(outer.delay_first)
                    body = outer.latest_body if outer.latest_body is not None else \
                        json.dumps(outer.release_json()).encode()
                    self._send(outer.latest_status, body, outer.latest_headers)
                elif self.path.startswith("/download/"):
                    name = self.path.rsplit("/", 1)[1]
                    target = (outer.redirect_to or f"http://localhost:{outer.port}/cdn/") + name
                    self.send_response(302)
                    self.send_header("Location", target)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif self.path.startswith("/cdn/") and self.path != "/cdn/SHA256SUMS":
                    outer.deb_gets += 1
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(outer.deb)))
                    self.end_headers()
                    try:
                        for i in range(0, len(outer.deb), 32768):
                            self.wfile.write(outer.deb[i:i + 32768])
                            if outer.slow_deb:
                                time.sleep(outer.slow_deb)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                elif self.path == "/cdn/SHA256SUMS":
                    text = outer.sums if outer.sums is not None else \
                        f"{hashlib.sha256(outer.deb).hexdigest()}  {outer.package_name()}\n" \
                        f"{'0' * 64}  rbsb-weather-{outer.version}-source.tar.gz\n"
                    self._send(200, text.encode(), ctype="text/plain")
                else:
                    self._send(404, b'{"message": "Not Found"}')

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/repos/shanebl030-glitch/RBSB-Weather/releases/latest"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def package_name(self) -> str:
        return updates.package_name(self.version, self.windows)

    def release_json(self) -> dict:
        base = f"http://127.0.0.1:{self.port}/download/"
        deb = self.package_name()
        return {
            "tag_name": f"v{self.version}", "name": f"RBSB-Weather {self.version}",
            "html_url": f"https://github.com/shanebl030-glitch/RBSB-Weather/releases/tag/v{self.version}",
            "draft": False, "prerelease": False, "published_at": "2026-12-01T09:00:00Z",
            "body": "## What's new\n\n- A **test** feature.\n",
            "assets": [
                {"name": deb, "browser_download_url": base + deb,
                 "size": self.declared_size if self.declared_size is not None else len(self.deb)},
                {"name": f"rbsb-weather-{self.version}-source.tar.gz", "browser_download_url": base + "src.tar.gz",
                 "size": 10},
                {"name": "SHA256SUMS", "browser_download_url": base + "SHA256SUMS", "size": 200},
            ],
        }

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# ======================================================================= pure logic
class TestParsing(unittest.TestCase):
    def rel(self, **kw):
        d = {"tag_name": "v2.1.0", "name": "RBSB-Weather 2.1.0", "html_url": "https://github.com/x/y/releases/tag/v2.1.0",
             "body": "notes", "assets": [
                 {"name": "rbsb-weather_2.1.0_all.deb", "browser_download_url": "https://github.com/a.deb", "size": 5},
                 {"name": "SHA256SUMS", "browser_download_url": "https://github.com/s"}]}
        d.update(kw)
        return d

    def test_normal_release(self):
        i = updates.parse_release(self.rel())
        self.assertEqual((i.version, i.deb_name, i.deb_size), ("2.1.0", "rbsb-weather_2.1.0_all.deb", 5))
        self.assertTrue(i.installable)

    def test_tag_forms(self):
        self.assertEqual(updates.version_from_tag("V1.15.0"), "1.15.0")
        self.assertEqual(updates.version_from_tag("2.0"), "2.0")
        for bad in ("", "latest", "v2.1.0-beta", "v2..1", "../../etc"):
            self.assertIsNone(updates.version_from_tag(bad), bad)

    def test_unusable_releases_rejected(self):
        for d in (self.rel(draft=True), self.rel(prerelease=True), self.rel(tag_name="nightly"), [], "x", None):
            with self.assertRaises(UpdateError):
                updates.parse_release(d)

    def test_only_our_own_deb_is_used(self):
        d = self.rel(assets=[{"name": "rbsb-weather_2.0.0_all.deb", "browser_download_url": "https://github.com/o"},
                             {"name": "evil.deb", "browser_download_url": "https://github.com/e"},
                             {"name": "SHA256SUMS", "browser_download_url": "https://github.com/s"}])
        i = updates.parse_release(d)
        self.assertEqual(i.deb_url, "")
        self.assertFalse(i.installable)

    def test_long_notes_cut_and_bad_page_replaced(self):
        i = updates.parse_release(self.rel(body="x" * 50_000, html_url="javascript:alert(1)", name=""))
        self.assertLess(len(i.notes), updates.MAX_NOTES_CHARS + 100)
        self.assertEqual(i.page_url, updates.RELEASES_PAGE)
        self.assertEqual(i.title, "RBSB-Weather 2.1.0")

    def test_versions(self):
        mk = lambda v: UpdateInfo(v, "v" + v, "", "", "")
        self.assertTrue(updates.is_newer(mk("2.1.0"), "2.0.0"))
        self.assertTrue(updates.is_newer(mk("2.10.0"), "2.9.0"))
        self.assertFalse(updates.is_newer(mk("2.0.0"), "2.0.0"))
        self.assertFalse(updates.is_newer(mk("1.18.0"), "2.0.0"))

    def test_sums(self):
        h = "a" * 64
        self.assertEqual(updates.parse_sums(f"{h}  x.deb\n", "x.deb"), h)
        self.assertEqual(updates.parse_sums(f"{h.upper()} *x.deb", "x.deb"), h)
        self.assertIsNone(updates.parse_sums(f"{h}  y.deb", "x.deb"))
        self.assertIsNone(updates.parse_sums("short  x.deb", "x.deb"))

    def test_when_due(self):
        now = 1_000_000_000.0
        H = 3600
        self.assertTrue(updates.due({}, now))                                          # never checked
        self.assertFalse(updates.due({"last_ok": now - H, "last_attempt": now - H}, now))
        self.assertTrue(updates.due({"last_ok": now - 25 * H, "last_attempt": now - 25 * H}, now))
        self.assertFalse(updates.due({"last_ok": now - 30 * H, "last_attempt": now - 1 * H}, now))  # failed 1 h ago
        self.assertTrue(updates.due({"last_ok": now - 30 * H, "last_attempt": now - 4 * H}, now))   # failed 4 h ago
        self.assertTrue(updates.due({"last_ok": now + 5 * H, "last_attempt": now + 5 * H}, now))    # clock went back
        self.assertTrue(updates.due({"last_ok": "junk", "last_attempt": True}, now))

    def test_only_github_addresses(self):
        c = UpdateClient(updates.LATEST_URL)              # the real address
        self.assertFalse(c.allow_local)
        ok = ("https://api.github.com/repos/a/b/releases/latest", "https://github.com/a/b/releases/download/v1/x.deb",
              "https://objects.githubusercontent.com/x", "https://release-assets.githubusercontent.com/x")
        bad = ("http://github.com/x", "https://evil.com/x", "https://githubusercontent.com.evil.com/x",
               "https://github.com.evil.com/x", "file:///etc/passwd", "http://127.0.0.1/x", "not a url")
        for u in ok:
            self.assertTrue(c.url_allowed(u), u)
        for u in bad:
            self.assertFalse(c.url_allowed(u), u)
        far = UpdateClient("http://evil.example/releases/latest")    # http only allowed for this computer
        self.assertFalse(far.allow_local)
        with self.assertRaises(UpdateError):
            far.check()

    def test_settings(self):
        s = Settings()
        self.assertTrue(s.update_check, "on by default")
        self.assertEqual(s.update_skip, "")
        s2 = Settings.from_dict({**s.to_dict(), "update_check": False, "update_skip": "2.1.0"})
        self.assertEqual((s2.update_check, s2.update_skip), (False, "2.1.0"))
        s3 = Settings.from_dict({"update_check": "no", "update_skip": "../x"})
        self.assertEqual((s3.update_check, s3.update_skip), (True, ""))

    def test_state_file(self):
        d = Path(tempfile.mkdtemp())
        try:
            p = d / "updates.json"
            self.assertEqual(updates.load_state(p), {})
            updates.save_state({"last_ok": 5}, p)
            self.assertEqual(updates.load_state(p), {"last_ok": 5})
            p.write_text("{broken")
            self.assertEqual(updates.load_state(p), {})
        finally:
            shutil.rmtree(d)


# ======================================================================= network
class NetCase(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.client = UpdateClient(self.gh.url, timeout=3)
        self.dl = Path(tempfile.mkdtemp(prefix="rbsb-dl-"))

    def tearDown(self):
        self.gh.stop()
        shutil.rmtree(self.dl, ignore_errors=True)

    def files(self):
        return sorted(p.name for p in self.dl.iterdir())


class TestCheck(NetCase):
    def test_newer_release_found(self):
        info = self.client.check()
        self.assertEqual(info.version, NEW)
        self.assertTrue(info.installable)
        self.assertTrue(updates.is_newer(info))
        self.assertIn("test", info.notes)

    def test_github_errors_are_plain_english(self):
        self.gh.latest_status = 404
        with self.assertRaisesRegex(UpdateError, "No published release"):
            self.client.check()
        self.gh.latest_status, self.gh.latest_headers = 403, {"X-RateLimit-Remaining": "0"}
        with self.assertRaisesRegex(UpdateError, "limit on update checks"):
            self.client.check()
        self.gh.latest_status, self.gh.latest_headers = 500, {}
        with self.assertRaisesRegex(UpdateError, "HTTP 500"):
            self.client.check()

    def test_damaged_or_huge_reply(self):
        self.gh.latest_body = b"{not json"
        with self.assertRaisesRegex(UpdateError, "couldn't be read"):
            self.client.check()
        self.gh.latest_body = b" " * (updates.MAX_RELEASE_BYTES + 10)
        with self.assertRaisesRegex(UpdateError, "more data than expected"):
            self.client.check()

    def test_no_network(self):
        c = UpdateClient("http://127.0.0.1:9/releases/latest", timeout=2)
        with self.assertRaisesRegex(UpdateError, "Couldn't reach GitHub"):
            c.check()

    def test_one_retry_after_a_timeout(self):
        self.gh.delay_first = 2.0
        c = UpdateClient(self.gh.url, timeout=0.8)
        info = c.check()
        self.assertEqual(info.version, NEW)
        self.assertEqual(sum(r.endswith("/latest") for r in self.gh.requests), 2)


class TestDownload(NetCase):
    def test_download_verified_through_redirect(self):
        info = self.client.check()
        seen = []
        path = self.client.download(info, self.dl, lambda d, t: seen.append((d, t)))
        self.assertEqual(path, self.dl / DEB_NAME)
        self.assertEqual(path.read_bytes(), self.gh.deb)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
        self.assertEqual(self.files(), [DEB_NAME], "no temporary files left")
        self.assertEqual(seen[-1], (len(self.gh.deb), len(self.gh.deb)))
        self.assertTrue(any(r.startswith("/cdn/") for r in self.gh.requests), "followed the redirect")

    def test_wrong_checksum_deleted(self):
        info = self.client.check()
        self.gh.sums = f"{'b' * 64}  {DEB_NAME}\n"
        with self.assertRaisesRegex(UpdateError, "didn't match its checksum"):
            self.client.download(info, self.dl)
        self.assertEqual(self.files(), [])

    def test_checksum_missing_or_not_listed(self):
        info = self.client.check()
        self.gh.sums = "nothing useful\n"
        with self.assertRaisesRegex(UpdateError, "doesn't list"):
            self.client.download(info, self.dl)
        info.sums_url = ""
        with self.assertRaisesRegex(UpdateError, "no checksum file"):
            self.client.download(info, self.dl)
        self.assertEqual(self.files(), [])
        self.assertEqual(self.gh.deb_gets, 0, "nothing downloaded without a checksum to check it")

    def test_not_a_package(self):
        self.gh.deb = b"<html>error page</html>" * 100
        info = self.client.check()
        with self.assertRaisesRegex(UpdateError, "isn't an installer package"):
            self.client.download(info, self.dl)
        self.assertEqual(self.files(), [])

    def test_incomplete_download(self):
        self.gh.declared_size = len(self.gh.deb) + 1000
        info = self.client.check()
        with self.assertRaisesRegex(UpdateError, "incomplete"):
            self.client.download(info, self.dl)
        self.assertEqual(self.files(), [])

    def test_too_large(self):
        info = self.client.check()
        old = updates.MAX_DEB_BYTES
        updates.MAX_DEB_BYTES = 100_000
        try:
            with self.assertRaisesRegex(UpdateError, "larger than expected"):
                self.client.download(info, self.dl)
        finally:
            updates.MAX_DEB_BYTES = old
        self.assertEqual(self.files(), [])

    def test_redirect_away_from_github_refused(self):
        self.gh.redirect_to = "http://198.51.100.7/steal/"
        info = self.client.check()
        with self.assertRaisesRegex(UpdateError, "other than GitHub"):
            self.client.download(info, self.dl)
        self.assertEqual(self.files(), [])

    def test_cancel(self):
        self.gh.deb = fake_deb(3_000_000)
        self.gh.slow_deb = 0.01
        info = self.client.check()
        flag = threading.Event()

        def progress(done, total):
            if done > 200_000:
                flag.set()
        with self.assertRaises(updates.UpdateCancelled):
            self.client.download(info, self.dl, progress, flag.is_set)
        self.assertEqual(self.files(), [])

    def test_existing_files_reused_or_kept(self):
        info = self.client.check()
        (self.dl / DEB_NAME).write_bytes(self.gh.deb)          # downloaded before
        self.assertEqual(self.client.download(info, self.dl), self.dl / DEB_NAME)
        self.assertEqual(self.gh.deb_gets, 0, "already verified: not downloaded again")
        (self.dl / DEB_NAME).write_bytes(b"someone else's file")
        path = self.client.download(info, self.dl)
        self.assertEqual(path.name, f"rbsb-weather_{NEW}_all-2.deb")
        self.assertEqual((self.dl / DEB_NAME).read_bytes(), b"someone else's file", "never overwritten")

    def test_release_without_our_package(self):
        info = self.client.check()
        info.deb_url = ""
        with self.assertRaisesRegex(UpdateError, "no installer package"):
            self.client.download(info, self.dl)


# ======================================================================= the window
class TestWindowUpdates(TempEnvTestCase):
    def setUp(self):
        super().setUp()
        qapp()
        self.gh = FakeGitHub()
        self.dl = Path(tempfile.mkdtemp(prefix="rbsb-dl-"))
        self._dl_folder = updates.downloads_folder
        updates.downloads_folder = lambda: self.dl
        self._delay = updates.FIRST_CHECK_DELAY_S
        self.opened = []
        self.windows = []

    def tearDown(self):
        for w in self.windows:
            w._quitting = True
            w.close()
            w.deleteLater()
        updates.downloads_folder = self._dl_folder
        updates.FIRST_CHECK_DELAY_S = self._delay
        self.gh.stop()
        shutil.rmtree(self.dl, ignore_errors=True)
        super().tearDown()

    def window(self):
        from rbsb_weather.main_window import MainWindow
        w = MainWindow(enable_tray=False, update_client=UpdateClient(self.gh.url, timeout=3))
        w.open_file = lambda p: self.opened.append(p) or True
        self.windows.append(w)
        return w

    def latest_requests(self):
        return sum(r.endswith("/latest") for r in self.gh.requests)

    def test_menu_entries(self):
        w = self.window()
        texts = [a.text() for m in w.menuBar().actions() for a in m.menu().actions()]
        self.assertIn("Check for &Updates…", texts)
        self.assertIn("Check for &updates automatically", texts)
        self.assertTrue(w.act_update_auto.isChecked(), "on by default")

    def test_manual_check_finds_update_then_installs(self):
        w = self.window()
        w.check_for_updates()
        dlg = w._update_dialog
        self.assertTrue(wait_until(lambda: dlg.info is not None, 10))
        self.assertIn(NEW, dlg.headline.text())
        self.assertTrue(w.update_button.isVisible() or not w.isVisible())
        self.assertIn(NEW, w.update_button.text())
        st = updates.load_state()
        self.assertEqual(st["latest"]["version"], NEW)
        self.assertIn("last_ok", st)
        dlg.btn_install.click()
        self.assertTrue(wait_until(lambda: self.opened, 15), dlg.detail.text())
        self.assertEqual(Path(self.opened[0]), self.dl / DEB_NAME)
        self.assertEqual((self.dl / DEB_NAME).read_bytes(), self.gh.deb)
        self.assertIn("Downloaded and checked", dlg.headline.text())

    def test_bad_download_is_never_opened(self):
        w = self.window()
        w.check_for_updates()
        dlg = w._update_dialog
        self.assertTrue(wait_until(lambda: dlg.info is not None, 10))
        self.gh.sums = f"{'c' * 64}  {DEB_NAME}\n"
        dlg.btn_install.click()
        self.assertTrue(wait_until(lambda: "wasn't downloaded" in dlg.headline.text(), 15))
        self.assertEqual(self.opened, [])
        self.assertEqual(list(self.dl.iterdir()), [])
        self.assertEqual(dlg.btn_install.text(), "Try Again")

    def test_up_to_date_and_error(self):
        self.gh.version = __version__
        w = self.window()
        w.check_for_updates()
        dlg = w._update_dialog
        self.assertTrue(wait_until(lambda: "latest version" in dlg.headline.text(), 10))
        self.assertFalse(w.update_button.isVisibleTo(w))
        self.gh.latest_status = 500
        dlg.btn_retry.click() if dlg.btn_retry.isVisible() else w.check_for_updates()
        self.assertTrue(wait_until(lambda: "Couldn't check" in dlg.headline.text(), 10))
        self.assertIn("HTTP 500", updates.load_state()["last_error"])

    def test_automatic_check_once_a_day_and_switch(self):
        updates.FIRST_CHECK_DELAY_S = 0
        w = self.window()
        w._maybe_check_updates()
        self.assertTrue(wait_until(lambda: w._update_info is not None, 10))
        self.assertEqual(self.latest_requests(), 1)
        self.assertIsNone(w._update_dialog, "an automatic check never pops up the dialog")
        self.assertTrue(w.update_button.isVisibleTo(w))
        self.assertEqual(updates.load_state()["notified"], NEW)
        w._maybe_check_updates()                      # not due again for a day
        time.sleep(0.3)
        self.assertEqual(self.latest_requests(), 1)
        st = updates.load_state()
        st["last_ok"] = st["last_attempt"] = time.time() - 25 * 3600
        updates.save_state(st)
        w.set_update_check(False)                     # switched off: never checks
        self.assertFalse(w.update_button.isVisibleTo(w))
        w._maybe_check_updates()
        time.sleep(0.3)
        self.assertEqual(self.latest_requests(), 1)
        self.assertFalse(Settings.from_dict(json.loads(w.store.path.read_text())).update_check)
        w.set_update_check(True)
        w._maybe_check_updates()
        self.assertTrue(wait_until(lambda: self.latest_requests() == 2, 10))

    def test_not_in_the_first_minute(self):
        w = self.window()                             # FIRST_CHECK_DELAY_S is 60
        w._maybe_check_updates()
        time.sleep(0.3)
        self.assertEqual(self.latest_requests(), 0)

    def test_skip_this_version(self):
        updates.FIRST_CHECK_DELAY_S = 0
        w = self.window()
        w.check_for_updates()
        dlg = w._update_dialog
        self.assertTrue(wait_until(lambda: dlg.info is not None, 10))
        dlg.btn_skip.click()
        self.assertEqual(w.settings.update_skip, NEW)
        self.assertFalse(w.update_button.isVisibleTo(w))
        st = updates.load_state()
        st["last_ok"] = st["last_attempt"] = 0
        updates.save_state(st)
        w._maybe_check_updates()                      # a skipped version isn't announced
        self.assertTrue(wait_until(lambda: self.latest_requests() == 2, 10))
        wait_until(lambda: not w._update_checking, 5)
        self.assertFalse(w.update_button.isVisibleTo(w))
        w.check_for_updates()                         # but the menu still shows it
        dlg = w._update_dialog
        self.assertTrue(wait_until(lambda: dlg.info is not None and "skip" in dlg.detail.text(), 10))
        self.assertFalse(dlg.btn_skip.isVisibleTo(dlg))

    def test_known_update_shown_at_startup_without_network(self):
        w = self.window()
        w.check_for_updates()
        self.assertTrue(wait_until(lambda: w._update_info is not None, 10))
        n = self.latest_requests()
        w2 = self.window()
        self.assertTrue(wait_until(lambda: w2._update_info is not None, 3))
        self.assertTrue(w2.update_button.isVisibleTo(w2))
        self.assertEqual(self.latest_requests(), n, "no network needed")

    def test_closing_during_download_cancels_it(self):
        self.gh.deb = fake_deb(4_000_000)
        self.gh.slow_deb = 0.02
        w = self.window()
        w.check_for_updates()
        dlg = w._update_dialog
        self.assertTrue(wait_until(lambda: dlg.info is not None, 10))
        dlg.btn_install.click()
        self.assertTrue(wait_until(lambda: dlg.progress.value() > 0, 10))
        dlg.close()
        self.assertTrue(wait_until(lambda: w._update_cancel is None, 10))
        time.sleep(0.2)
        self.assertEqual(list(self.dl.iterdir()), [], "partial download deleted")
        self.assertEqual(self.opened, [])


if __name__ == "__main__":
    unittest.main()
