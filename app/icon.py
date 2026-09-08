"""The app icon, drawn in code.

Generated rather than shipped as a binary asset so it stays crisp at every tray
size (Windows asks for 16/20/24/32px depending on DPI) and so there is no image
file to keep in sync with the design.

The mark is three ascending bars -- a level meter -- inside a rounded square,
which reads at 16px where a literal music note turns to mush.
"""

from __future__ import annotations

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QBrush, QColor, QIcon, QImage, QPainter, QPixmap

PLATE = QColor(16, 17, 23)
INK = QColor(255, 255, 255)

# Bar heights as a fraction of the icon, left to right.
BARS = (0.34, 0.62, 0.46)


def _draw(size: int, muted: bool) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    radius = size * 0.26
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(PLATE))
    painter.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)

    ink = QColor(INK)
    # Dim the bars when playback is paused or absent, so the tray icon reports
    # state without needing a second icon file.
    ink.setAlphaF(0.34 if muted else 0.95)
    painter.setBrush(QBrush(ink))

    bar_width = size * 0.14
    gap = size * 0.10
    total = len(BARS) * bar_width + (len(BARS) - 1) * gap
    x = (size - total) / 2.0
    baseline = size * 0.76

    for fraction in BARS:
        height = size * fraction
        painter.drawRoundedRect(
            QRectF(x, baseline - height, bar_width, height),
            bar_width / 2.4,
            bar_width / 2.4,
        )
        x += bar_width + gap

    painter.end()
    return pixmap


def app_icon(muted: bool = False) -> QIcon:
    """Multi-resolution icon for the tray and window."""
    icon = QIcon()
    for size in (16, 20, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(_draw(size, muted))
    return icon


def write_ico(path) -> bool:
    """Write a .ico for Windows shortcuts. Returns False if Pillow is absent."""
    try:
        from PIL import Image
    except ImportError:
        return False

    frames = []
    for size in (16, 24, 32, 48, 64, 128, 256):
        # Convert to a format whose byte order is defined, rather than relying
        # on the platform-native ARGB32 layout QPixmap happens to use.
        image = _draw(size, muted=False).toImage().convertToFormat(
            QImage.Format.Format_RGBA8888
        )
        raw = bytes(image.constBits().asarray(image.sizeInBytes()))
        frames.append(Image.frombytes("RGBA", (size, size), raw, "raw", "RGBA"))

    frames.sort(key=lambda im: im.size[0], reverse=True)
    frames[0].save(path, format="ICO", sizes=[im.size for im in frames])
    return True
