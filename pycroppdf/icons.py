"""Small, dependency-free vector icon set for the application UI."""

from __future__ import annotations

from PyQt6.QtCore import QByteArray, QRectF, QSize, Qt
from PyQt6.QtGui import QIcon, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer

_ICON_BODIES = {
    "open": '<path d="M3 7h7l2 2h9l-2 10H4L3 7Z"/><path d="M3 7V5h7l2 2"/>',
    "save": (
        '<path d="M5 3h12l3 3v15H4V3h1Z"/><path d="M8 3v6h8V3"/>'
        '<rect x="8" y="14" width="8" height="7" rx="1"/>'
    ),
    "undo": '<path d="m9 7-5 5 5 5"/><path d="M5 12h8a6 6 0 0 1 6 6"/>',
    "odd-even": (
        '<rect x="3" y="5" width="7" height="14" rx="1"/>'
        '<rect x="14" y="5" width="7" height="14" rx="1"/>'
    ),
    "stack": ('<rect x="6" y="4" width="13" height="15" rx="1"/><path d="M4 7v14h12"/>'),
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v6"/><path d="M12 7h.01"/>',
    "crop": '<path d="M7 3v14a2 2 0 0 0 2 2h12"/><path d="M3 7h14a2 2 0 0 1 2 2v12"/>',
    "crop-coactive": (
        '<path d="M7 3v14a2 2 0 0 0 2 2h12"/>'
        '<path d="M3 7h14a2 2 0 0 1 2 2v12"/>'
        '<path d="m16.5 4.5 1.5 1.5 3-3"/>'
    ),
    "cover": '<rect x="3" y="6" width="18" height="12" rx="1"/>',
    "rotate": ('<path d="M20 7v5h-5"/><path d="M19 12a7 7 0 1 0-1.4 4.2"/>'),
    "delete": (
        '<path d="M4 7h16"/><path d="M9 7V4h6v3"/>'
        '<path d="m7 7 1 14h8l1-14"/><path d="M10 11v6M14 11v6"/>'
    ),
    "check": '<path d="m4 12 5 5L20 6"/>',
    "reset": '<path d="M4 4v6h6"/><path d="M5.5 9A8 8 0 1 1 5 16"/>',
    "rotate-left": '<path d="M8 5 3 10l5 5"/><path d="M4 10h8a7 7 0 0 1 7 7"/>',
    "rotate-right": '<path d="m16 5 5 5-5 5"/><path d="M20 10h-8a7 7 0 0 0-7 7"/>',
    "preview": (
        '<path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z"/>'
        '<circle cx="12" cy="12" r="2.5"/>'
    ),
    "discard": '<path d="m6 6 12 12M18 6 6 18"/>',
    "deskew": (
        '<path d="M4 6h12M4 10h16M4 14h13"/>'
        '<path d="m4 19 16-2"/><path d="m18 4 .5-2M21 6l2-.5M20 3l1.5-1.5"/>'
    ),
    "palette": (
        '<path d="M12 3a9 9 0 0 0 0 18h1.5a2 2 0 0 0 0-4H12a2 2 0 0 1 0-4h4a5 5 0 0 0 0-10h-4Z"/>'
        '<circle cx="7.5" cy="9" r=".7"/><circle cx="10" cy="6.5" r=".7"/>'
        '<circle cx="14" cy="6.5" r=".7"/><circle cx="17" cy="9" r=".7"/>'
    ),
    "eyedropper": (
        '<path d="m14 4 6 6"/><path d="m17 3 4 4-9 9-3-3 8-10Z"/><path d="m10 15-5 5H3v-2l5-5"/>'
    ),
}


def _render_icon(name: str, color: str, size: int) -> QPixmap:
    body = _ICON_BODIES[name]
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
        f'fill="none" stroke="{color}" stroke-width="1.8" '
        'stroke-linecap="round" stroke-linejoin="round">'
        f"{body}</svg>"
    )
    scale = 2
    pixmap = QPixmap(size * scale, size * scale)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(QByteArray(svg.encode("utf-8"))).render(
        painter,
        QRectF(0, 0, size * scale, size * scale),
    )
    painter.end()
    pixmap.setDevicePixelRatio(scale)
    return pixmap


def vector_icon(name: str, *, color: str = "#F4F4F4", size: int = 18) -> QIcon:
    """Return a crisp icon with explicit normal and disabled states."""
    if name not in _ICON_BODIES:
        raise KeyError(f"Unknown icon: {name}")
    icon = QIcon()
    normal = _render_icon(name, color, size)
    disabled = _render_icon(name, "#858585", size)
    icon.addPixmap(normal, QIcon.Mode.Normal, QIcon.State.Off)
    icon.addPixmap(normal, QIcon.Mode.Normal, QIcon.State.On)
    icon.addPixmap(normal, QIcon.Mode.Active, QIcon.State.Off)
    icon.addPixmap(normal, QIcon.Mode.Selected, QIcon.State.On)
    icon.addPixmap(disabled, QIcon.Mode.Disabled, QIcon.State.Off)
    icon.addPixmap(disabled, QIcon.Mode.Disabled, QIcon.State.On)
    return icon


def icon_size() -> QSize:
    """Return the standard toolbar icon size."""
    return QSize(18, 18)
