"""Make RBSB-Weather.ico from packaging/rbsb-weather.svg (needs PyQt6 with QtSvg).

The .ico holds PNG images at 16-256 px, which Windows Vista and later read directly,
so no image library beyond Qt is needed.

Created By Shane BlackbuRn
"""
import struct
import sys
from pathlib import Path

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PyQt6.QtGui import QGuiApplication, QImage, QPainter
from PyQt6.QtSvg import QSvgRenderer

ROOT = Path(__file__).resolve().parents[2]


def main(out: Path) -> None:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    svg = QSvgRenderer(str(ROOT / "packaging" / "rbsb-weather.svg"))
    if not svg.isValid():
        raise SystemExit("icon SVG unreadable")
    pngs = []
    for size in (16, 24, 32, 48, 64, 128, 256):
        img = QImage(size, size, QImage.Format.Format_ARGB32)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        svg.render(p)
        p.end()
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG")
        pngs.append((size, bytes(ba)))
    head = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries, data = b"", b""
    for size, png in pngs:
        s = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", s, s, 0, 0, 1, 32, len(png), offset + len(data))
        data += png
    out.write_bytes(head + entries + data)
    print(f"wrote {out} ({len(pngs)} sizes)")
    del app


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build" / "RBSB-Weather.ico")
