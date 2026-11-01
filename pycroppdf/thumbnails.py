"""A virtual page sidebar: metadata for every page, widgets for none of them."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable
from typing import Any

from PyQt6.QtCore import QAbstractListModel, QModelIndex, QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QKeyEvent, QMouseEvent, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QListView,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionButton,
    QStyleOptionViewItem,
    QWidget,
)

THUMBNAIL_PIXMAP_BUDGET = 4 * 1024 * 1024
_CELL_SIZE = QSize(96, 170)
_PREVIEW_ROLE = Qt.ItemDataRole.UserRole.value + 1
_READY_ROLE = Qt.ItemDataRole.UserRole.value + 2
_ROOT_INDEX = QModelIndex()


class ThumbnailModel(QAbstractListModel):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._page_count = 0
        self._image: Callable[[int], QImage | None] = lambda _page: None
        self._ready: Callable[[int], bool] = lambda _page: False
        self._checked: frozenset[int] = frozenset()
        self._preview: int | None = None
        self.selection_enabled = False
        self._pixmaps: OrderedDict[int, QPixmap] = OrderedDict()
        self.pixmap_bytes = 0

    def rowCount(self, parent: QModelIndex = _ROOT_INDEX) -> int:
        return 0 if parent.isValid() else self._page_count

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < self._page_count:
            return None
        page = index.row()
        if role == Qt.ItemDataRole.DisplayRole:
            return f"Page {page + 1}"
        if role == Qt.ItemDataRole.DecorationRole:
            return self.thumbnail(page)
        if role == Qt.ItemDataRole.CheckStateRole:
            return Qt.CheckState.Checked if page in self._checked else Qt.CheckState.Unchecked
        if role == _PREVIEW_ROLE:
            return page == self._preview
        if role == _READY_ROLE:
            return self._ready(page)
        if role in (Qt.ItemDataRole.AccessibleTextRole, Qt.ItemDataRole.ToolTipRole):
            state = "ready" if self._ready(page) else "preview pending"
            selection = ", selected for deletion" if page in self._checked else ""
            preview = ", currently previewed" if page == self._preview else ""
            return f"Page {page + 1}, {state}{selection}{preview}"
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if self.selection_enabled:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def set_source(
        self,
        page_count: int,
        image: Callable[[int], QImage | None],
        ready: Callable[[int], bool],
    ) -> None:
        self.beginResetModel()
        self._page_count = page_count
        self._image, self._ready = image, ready
        self._pixmaps.clear()
        self.pixmap_bytes = 0
        self.endResetModel()

    def update_pages(self, pages: Iterable[int]) -> None:
        for page in sorted(set(pages)):
            if not 0 <= page < self._page_count:
                continue
            old = self._pixmaps.pop(page, None)
            if old is not None:
                self.pixmap_bytes -= old.width() * old.height() * 4
            index = self.index(page)
            self.dataChanged.emit(index, index)

    def set_state(self, checked: Iterable[int], preview: int | None, enabled: bool) -> None:
        checked = frozenset(checked)
        if (checked, preview, enabled) == (self._checked, self._preview, self.selection_enabled):
            return
        self._checked, self._preview, self.selection_enabled = checked, preview, enabled
        if self._page_count:
            self.dataChanged.emit(self.index(0), self.index(self._page_count - 1))

    def thumbnail(self, page: int) -> QPixmap | None:
        pixmap = self._pixmaps.get(page)
        if pixmap is not None:
            self._pixmaps.move_to_end(page)
            return pixmap
        image = self._image(page)
        if image is None or image.isNull():
            return None
        image = image.scaled(
            160, 240, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        pixmap = QPixmap.fromImage(image)
        size = pixmap.width() * pixmap.height() * 4
        while self._pixmaps and self.pixmap_bytes + size > THUMBNAIL_PIXMAP_BUDGET:
            _page, evicted = self._pixmaps.popitem(last=False)
            self.pixmap_bytes -= evicted.width() * evicted.height() * 4
        self._pixmaps[page] = pixmap
        self.pixmap_bytes += size
        return pixmap


class ThumbnailDelegate(QStyledItemDelegate):
    @staticmethod
    def checkbox_rect(cell: QRect) -> QRect:
        return QRect(cell.left() + 8, cell.top() + 4, 18, 18)

    def sizeHint(self, _option: QStyleOptionViewItem, _index: QModelIndex) -> QSize:
        return _CELL_SIZE

    def paint(
        self, painter: QPainter | None, option: QStyleOptionViewItem, index: QModelIndex
    ) -> None:
        if painter is None:
            return
        painter.save()
        try:
            cell = option.rect.adjusted(2, 2, -2, -2)
            checked = index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
            preview = bool(index.data(_PREVIEW_ROLE))
            ready = bool(index.data(_READY_ROLE))
            if checked:
                painter.fillRect(cell, QColor("#3a3446"))
            if preview or checked or option.state & QStyle.StateFlag.State_HasFocus:
                color = QColor("#b39ddb" if preview else "#9575cd")
                pen = QPen(color, 2)
                if not preview:
                    pen.setStyle(Qt.PenStyle.DashLine)
                painter.setPen(pen)
                painter.drawRoundedRect(cell.adjusted(1, 1, -1, -1), 3, 3)

            check = QStyleOptionButton()
            check.rect = self.checkbox_rect(option.rect)
            check.state = QStyle.StateFlag.State_On if checked else QStyle.StateFlag.State_Off
            if index.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                check.state |= QStyle.StateFlag.State_Enabled
            style = option.widget.style() if option.widget is not None else QApplication.style()
            assert style is not None
            style.drawControl(QStyle.ControlElement.CE_CheckBox, check, painter, option.widget)

            target = QRect(option.rect.left() + 8, option.rect.top() + 26, 80, 120)
            pixmap = index.data(Qt.ItemDataRole.DecorationRole)
            if isinstance(pixmap, QPixmap) and not pixmap.isNull():
                size = pixmap.size().scaled(target.size(), Qt.AspectRatioMode.KeepAspectRatio)
                image_rect = QRect(target.topLeft(), size)
                image_rect.moveCenter(target.center())
                painter.drawPixmap(image_rect, pixmap)
            else:
                painter.fillRect(target, QColor("#55515b"))
                painter.setPen(QColor("#d3cddf"))
                painter.drawText(target, Qt.AlignmentFlag.AlignCenter, "…")
            painter.setPen(option.palette.text().color() if ready else QColor("#a9a2b5"))
            painter.drawText(
                QRect(option.rect.left(), option.rect.top() + 148, option.rect.width(), 20),
                Qt.AlignmentFlag.AlignCenter,
                str(index.data(Qt.ItemDataRole.DisplayRole)),
            )
        finally:
            painter.restore()


class ThumbnailView(QListView):
    previewRequested = pyqtSignal(int)
    selectionRequested = pyqtSignal(int, object, bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.pages = ThumbnailModel(self)
        self.setModel(self.pages)
        self.setItemDelegate(ThumbnailDelegate(self))
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setWrapping(True)
        self.setUniformItemSizes(True)
        self.setGridSize(_CELL_SIZE)
        self.setSpacing(0)
        self.setSelectionMode(QListView.SelectionMode.NoSelection)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.setAccessibleName("PDF pages")
        self.setToolTip(
            "Click a page to preview it. Check a box to select for deletion. "
            "Ctrl/Command toggles pages; Shift selects a range. "
            "Use arrow keys, Enter to preview, and Space to check a page."
        )

    def mousePressEvent(self, event: QMouseEvent | None) -> None:
        if event is None:
            return
        index = self.indexAt(event.position().toPoint())
        if event.button() != Qt.MouseButton.LeftButton or not index.isValid():
            super().mousePressEvent(event)
            return
        self.setCurrentIndex(index)
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        modifiers = event.modifiers()
        select_modifiers = (
            Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.MetaModifier
            | Qt.KeyboardModifier.ShiftModifier
        )
        checkbox = ThumbnailDelegate.checkbox_rect(self.visualRect(index)).contains(
            event.position().toPoint()
        )
        if checkbox or modifiers & select_modifiers:
            if self.pages.selection_enabled:
                self.selectionRequested.emit(index.row(), modifiers, True)
        elif index.data(_READY_ROLE):
            self.previewRequested.emit(index.row())
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent | None) -> None:
        if event is not None and event.button() == Qt.MouseButton.LeftButton:
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent | None) -> None:
        if event is None:
            return
        index = self.currentIndex()
        if not index.isValid() and self.pages.rowCount():
            index = self.pages.index(0)
            self.setCurrentIndex(index)
        if index.isValid() and event.key() == Qt.Key.Key_Space:
            if self.pages.selection_enabled and not event.isAutoRepeat():
                self.selectionRequested.emit(index.row(), event.modifiers(), True)
            event.accept()
        elif index.isValid() and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if index.data(_READY_ROLE):
                self.previewRequested.emit(index.row())
            event.accept()
        else:
            super().keyPressEvent(event)
