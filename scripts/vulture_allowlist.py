"""References for callbacks Vulture cannot discover statically.

Qt invokes these overrides in C++; the two whiteout names are retained public
compatibility aliases. Keep explicit references so stale entries are type checked.
This file is analysis input, never imported by the application.
"""

# ruff: noqa: B018 -- attribute expressions are Vulture's documented allowlist format.

from pycroppdf.main_window import PDFViewer
from pycroppdf.thumbnails import ThumbnailDelegate
from pycroppdf.widgets import PageGraphicsView

PDFViewer.dragEnterEvent
PDFViewer.dragMoveEvent
PDFViewer.handleWhiteoutRequest
PDFViewer.applyWhiteout
PageGraphicsView.wheelEvent
PageGraphicsView.mousePressEvent
PageGraphicsView.mouseMoveEvent
PageGraphicsView.mouseReleaseEvent
ThumbnailDelegate.sizeHint
ThumbnailDelegate.paint
