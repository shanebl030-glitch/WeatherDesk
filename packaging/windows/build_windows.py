"""Build the Windows portable zip (run on Windows, e.g. by .github/workflows/windows.yml).

    python packaging/windows/build_windows.py

Makes dist/windows/RBSB-Weather/ (RBSB-Weather.exe with Python, Qt and the app
inside; nothing to install), then dist/rbsb-weather-<version>-windows-portable.zip
with that folder at the top, and dist/SHA256SUMS-windows.

Needs: PyQt6, numpy, keyring and pyinstaller (pip install).

Created By Shane BlackbuRn
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "dist"
BUILD = ROOT / "build" / "windows"
APP = "RBSB-Weather"
CREDIT = "Created By Shane BlackbuRn"


def version() -> str:
    return re.search(r'__version__ = "(.+?)"', (ROOT / "rbsb_weather" / "__init__.py").read_text()).group(1)


def version_file(ver: str) -> Path:
    """Windows file properties (right-click → Properties → Details)."""
    nums = (ver.split(".") + ["0", "0", "0"])[:4]
    t = ", ".join(str(int(n)) for n in nums)
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({t}), prodvers=({t}), mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Shane BlackbuRn'),
      StringStruct('FileDescription', 'RBSB-Weather desktop weather'),
      StringStruct('FileVersion', '{ver}'),
      StringStruct('InternalName', '{APP}'),
      StringStruct('LegalCopyright', '{CREDIT}'),
      StringStruct('OriginalFilename', '{APP}.exe'),
      StringStruct('ProductName', '{APP}'),
      StringStruct('ProductVersion', '{ver}'),
      StringStruct('Comments', '{CREDIT}. Weather data: Open-Meteo, MET Norway, NOAA GFS.')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    p = BUILD / "version_info.txt"
    p.write_text(text, encoding="utf-8")
    return p


README = """RBSB-Weather {ver} for Windows (portable)
{credit}

No installation needed.

1. Unzip this folder anywhere you like (Documents, the desktop, a USB stick).
2. Double-click RBSB-Weather.exe.

Windows may say "Windows protected your PC" the first time, because this free
program isn't signed with a paid certificate. Click "More info", then "Run anyway".

Your places and settings are kept in the RBSB-Weather-data folder next to
RBSB-Weather.exe, so you can copy or move the whole folder and they come with it.
(If that folder can't be written to, for example inside Program Files, they're kept
in %LOCALAPPDATA%\\RBSB-Weather instead.)

Updating: RBSB-Weather tells you when there's a new version (Help -> Check for
Updates). It downloads and checks the new zip for you. Close RBSB-Weather, unzip
the new version over this folder (replace the files), and start it again. Your
RBSB-Weather-data folder is kept.

Removing it: turn off View -> Start at login if you turned it on, close RBSB-Weather,
then delete the folder. Nothing else is left on your PC (unless the settings went
to %LOCALAPPDATA%\\RBSB-Weather, which you can delete too).

The full User Guide is inside the program: Help -> User Guide (F1).
Project page: https://github.com/shanebl030-glitch/RBSB-Weather
"""


def main() -> int:
    ver = version()
    BUILD.mkdir(parents=True, exist_ok=True)
    DIST.mkdir(exist_ok=True)
    icon = BUILD / f"{APP}.ico"
    subprocess.run([sys.executable, str(ROOT / "packaging" / "windows" / "make_icon.py"), str(icon)], check=True)
    out_dir = DIST / "windows"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    sep = os.pathsep
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
           "--name", APP, "--icon", str(icon), "--version-file", str(version_file(ver)),
           "--distpath", str(out_dir), "--workpath", str(BUILD / "pyi"), "--specpath", str(BUILD),
           "--add-data", f"{ROOT / 'rbsb_weather' / 'assets'}{sep}rbsb_weather/assets",
           "--add-data", f"{ROOT / 'rbsb_weather' / 'USER_GUIDE.md'}{sep}rbsb_weather",
           "--collect-submodules", "rbsb_weather",
           "--collect-submodules", "keyring",
           "--paths", str(ROOT),
           str(ROOT / "packaging" / "windows" / "launch.py")]
    print(" ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=ROOT)
    folder = out_dir / APP
    exe = folder / f"{APP}.exe"
    if not exe.exists():
        raise SystemExit(f"build failed: {exe} missing")
    (folder / "README-Windows.txt").write_text(README.format(ver=ver, credit=CREDIT), encoding="utf-8")
    shutil.copy(ROOT / "CHANGELOG.md", folder / "CHANGELOG.md")
    zip_path = DIST / f"rbsb-weather-{ver}-windows-portable.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(folder.rglob("*")):
            rel = f.relative_to(out_dir)
            if "RBSB-Weather-data" in rel.parts or "__pycache__" in rel.parts:
                continue
            z.write(f, rel.as_posix())
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    (DIST / "SHA256SUMS-windows").write_text(f"{digest}  {zip_path.name}\n", encoding="utf-8")
    size = zip_path.stat().st_size / 1_048_576
    print(f"Built {zip_path} ({size:.1f} MB)\n{digest}  {zip_path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
