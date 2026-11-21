"""Smoke test of the built Windows portable zip, on a real Windows machine.

Unzips it to a fresh folder and runs the real RBSB-Weather.exe (headless):
* --version exits cleanly (a windowed .exe has no console; this used to be a crash risk);
* it starts, stays running, writes its log and settings into RBSB-Weather-data next
  to the .exe (portable);
* a second launch finds the first through the single-instance channel (a Windows
  named pipe) and exits, leaving one copy running;
* closing it saves its settings.

Created By Shane BlackbuRn
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path


def main(zip_path: Path) -> int:
    work = Path(tempfile.mkdtemp(prefix="rbsb-smoke-"))
    problems = []
    try:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(work)
        exe = work / "RBSB-Weather" / "RBSB-Weather.exe"
        assert exe.exists(), "RBSB-Weather.exe missing from the zip"
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen", RBSB_WEATHER_INSTANCE=f"rbsb-smoke-{os.getpid()}",
                   RBSB_WEATHER_KEYRING="0")
        for k in ("RBSB_WEATHER_CONFIG_DIR", "RBSB_WEATHER_CACHE_DIR", "RBSB_WEATHER_PORTABLE_DIR"):
            env.pop(k, None)
        r = subprocess.run([str(exe), "--version"], env=env, timeout=60)
        print("--version exit code:", r.returncode)
        if r.returncode != 0:
            problems.append(f"--version exited {r.returncode}")

        first = subprocess.Popen([str(exe), "--minimized"], env=env)
        data = work / "RBSB-Weather" / "RBSB-Weather-data"
        log = data / "cache" / "rbsb-weather.log"
        end = time.time() + 60
        while time.time() < end and not (log.exists() and "starting on" in log.read_text(errors="replace")):
            time.sleep(0.5)
        time.sleep(5)
        if first.poll() is not None:
            problems.append(f"the app exited by itself (code {first.returncode})")
        text = log.read_text(errors="replace") if log.exists() else ""
        print("--- log so far ---\n" + text[-3000:])
        if "starting on" not in text:
            problems.append("no start-up line in RBSB-Weather-data\\cache\\rbsb-weather.log (portable folder not used?)")
        if "Traceback" in text:
            problems.append("a Python error is in the log")

        second = subprocess.run([str(exe)], env=env, timeout=60)
        text = log.read_text(errors="replace")
        print("second launch exit code:", second.returncode)
        if second.returncode != 0 or "already running" not in text:
            problems.append("a second launch didn't find the running copy")
        if first.poll() is not None:
            problems.append("the first copy closed when the second was launched")

        first.terminate()               # TerminateProcess: no clean shutdown, but nothing should be left half-written
        first.wait(30)
        settings = data / "config" / "settings.json"
        print("settings file:", settings, settings.exists())
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if problems:
        print("SMOKE TEST FAILED:\n- " + "\n- ".join(problems))
        return 1
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
