from PyQt6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QCursor, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (QCheckBox, QGraphicsScene, QGraphicsView, QLabel,
                             QVBoxLayout, QWidget)


class PageGraphicsView(QGraphicsView):
    selectionChanged = pyqtSignal(QRectF)
    colorPicked = pyqtSignal(QColor)
    whiteoutRequested = pyqtSignal(QRectF)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.scene = QGraphicsScene()
        self.setScene(self.scene)
        self.selection_item = None
        
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setMouseTracking(True)
        
        self._mode = 'none'  # 'none', 'draw', 'move', 'resize'
        self._tool = 'select' # 'select', 'whiteout', 'pick_color'
        self._resize_handle = None
        self._start_pos = None
        self._original_rect = None
        self.handle_margin = 4

        # Zooming/panning state
        self._pan = False
        self._last_pan_pos = QPointF()
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)

    def setTool(self, tool):
        self._tool = tool
        if tool in ['pick_color', 'whiteout']:
            self.setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        
        if tool != 'select':
            self.clearSelection()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._pan = True
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._pan = False
            self.setCursor(Qt.CursorShape.ArrowCursor)  # Will be updated by mouseMoveEvent
        else:
            super().keyReleaseEvent(event)

    def wheelEvent(self, event):
        zoom_factor = 1.15
        if event.angleDelta().y() < 0:
            zoom_factor = 1.0 / zoom_factor
        self.scale(zoom_factor, zoom_factor)

    def _get_resize_handle(self, pos):
        if not self.selection_item:
            return None
        rect = self.selection_item.rect()
        
        on_left = abs(pos.x() - rect.left()) < self.handle_margin
        on_right = abs(pos.x() - rect.right()) < self.handle_margin
        on_top = abs(pos.y() - rect.top()) < self.handle_margin
        on_bottom = abs(pos.y() - rect.bottom()) < self.handle_margin

        if on_top and on_left: return 'top_left'
        if on_top and on_right: return 'top_right'
        if on_bottom and on_left: return 'bottom_left'
        if on_bottom and on_right: return 'bottom_right'
        if on_top: return 'top'
        if on_bottom: return 'bottom'
        if on_left: return 'left'
        if on_right: return 'right'
        if rect.contains(pos): return 'move'
        
        return None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self._tool == 'pick_color':
                pos = self.mapToScene(event.pos())
                items = self.scene.items(pos)
                for item in items:
                    if hasattr(item, 'pixmap'):
                        # Map scene pos to item pos
                        item_pos = item.mapFromScene(pos)
                        pixmap = item.pixmap()
                        x = int(item_pos.x())
                        y = int(item_pos.y())
                        if 0 <= x < pixmap.width() and 0 <= y < pixmap.height():
                            color = pixmap.toImage().pixelColor(x, y)
                            self.colorPicked.emit(color)
                            self.setTool('select')
                            return
                return

            if self._pan:
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                self._last_pan_pos = event.pos()
                return

            if self._tool == 'whiteout':
                pos = self.mapToScene(event.pos())
                self._start_pos = pos
                self._mode = 'draw'
                if self.selection_item:
                    self.scene.removeItem(self.selection_item)
                    self.selection_item = None
                return

            pos = self.mapToScene(event.pos())
            self._start_pos = pos
            handle = self._get_resize_handle(pos)

            if handle and self.selection_item:
                if handle == 'move':
                    self._mode = 'move'
                else:
                    self._mode = 'resize'
                    self._resize_handle = handle
                self._original_rect = self.selection_item.rect()
            else:
                self._mode = 'draw'
                if self.selection_item:
                    self.scene.removeItem(self.selection_item)
                    self.selection_item = None


    def mouseMoveEvent(self, event):
        if self._tool == 'pick_color':
            return

        if self._pan:
            if event.buttons() & Qt.MouseButton.LeftButton:
                delta = event.pos() - self._last_pan_pos
                self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
                self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
                self._last_pan_pos = event.pos()
            return

        pos = self.mapToScene(event.pos())

        if self._mode == 'none':
            handle = self._get_resize_handle(pos)
            if handle == 'move': self.setCursor(Qt.CursorShape.SizeAllCursor)
            elif handle in ['top_left', 'bottom_right']: self.setCursor(Qt.CursorShape.SizeFDiagCursor)
            elif handle in ['top_right', 'bottom_left']: self.setCursor(Qt.CursorShape.SizeBDiagCursor)
            elif handle in ['top', 'bottom']: self.setCursor(Qt.CursorShape.SizeVerCursor)
            elif handle in ['left', 'right']: self.setCursor(Qt.CursorShape.SizeHorCursor)
            else: self.setCursor(Qt.CursorShape.ArrowCursor)
        
        elif self._mode == 'draw':
            rect = QRectF(self._start_pos, pos).normalized()
            if not self.selection_item:
                pen = QPen(Qt.GlobalColor.blue, 2, Qt.PenStyle.SolidLine)
                self.selection_item = self.scene.addRect(rect, pen)
            else:
                self.selection_item.setRect(rect)
        
        elif self._mode == 'move':
            delta = pos - self._start_pos
            new_rect = self._original_rect.translated(delta)
            self.selection_item.setRect(new_rect)
            
        elif self._mode == 'resize':
            new_rect = QRectF(self._original_rect)
            delta = pos - self._start_pos
            
            if 'top' in self._resize_handle: new_rect.setTop(self._original_rect.top() + delta.y())
            if 'bottom' in self._resize_handle: new_rect.setBottom(self._original_rect.bottom() + delta.y())
            if 'left' in self._resize_handle: new_rect.setLeft(self._original_rect.left() + delta.x())
            if 'right' in self._resize_handle: new_rect.setRight(self._original_rect.right() + delta.x())
            
            self.selection_item.setRect(new_rect.normalized())

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self._pan:
                self.setCursor(Qt.CursorShape.OpenHandCursor)
                return

            if self._tool == 'whiteout' and self._mode == 'draw':
                rect = QRectF(self._start_pos, self.mapToScene(event.pos())).normalized()
                if rect.isValid() and not rect.isNull():
                    self.whiteoutRequested.emit(rect)
                
                # Cleanup drawing artifact
                if self.selection_item:
                    self.scene.removeItem(self.selection_item)
                    self.selection_item = None
                
                self._mode = 'none'
                self._start_pos = None
                return

            if self._mode in ['draw', 'move', 'resize']:
                if self.selection_item and self.selection_item.rect().isValid():
                    self.selectionChanged.emit(self.selection_item.rect())
                else: # Invalid rect, e.g. zero size
                    self.clearSelection()
            self._mode = 'none'
            self._resize_handle = None
            self._start_pos = None
            self._original_rect = None
    
    def setSelection(self, rect):
        if not rect or rect.isNull() or not rect.isValid():
            if self.selection_item:
                self.clearSelection()
            return

        if self.selection_item and self.selection_item.rect() == rect:
            return

        if not self.selection_item:
            pen = QPen(Qt.GlobalColor.blue, 2, Qt.PenStyle.SolidLine)
            self.selection_item = self.scene.addRect(rect, pen)
        else:
            self.selection_item.setRect(rect)
        self.selectionChanged.emit(rect)

    def clearSelection(self):
        if self.selection_item:
            self.scene.removeItem(self.selection_item)
            self.selection_item = None
            self.selectionChanged.emit(QRectF())

    def getSelectionRect(self):
        return self.selection_item.rect() if self.selection_item else None

    def clearScene(self):
        self.scene.clear()
        self.selection_item = None

class ThumbnailWidget(QWidget):
    previewRequested = pyqtSignal(int)

    def __init__(self, page_num, image, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout()
        layout.setSpacing(2)
        layout.setContentsMargins(2, 2, 2, 2)
        self.checkbox = QCheckBox()
        self.page_num = page_num
        
        # Create thumbnail
        self.label = QLabel()
        if image:
            thumbnail = image.scaled(QSize(80, 120),
                                   Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
            self.label.setPixmap(QPixmap.fromImage(thumbnail))
        else:
            # Placeholder for missing image
            placeholder = QPixmap(80, 120)
            placeholder.fill(Qt.GlobalColor.lightGray)
            self.label.setPixmap(placeholder)
        self.label.installEventFilter(self)
        
        layout.addWidget(self.checkbox)
        layout.addWidget(self.label)
        layout.addWidget(QLabel(f"Page {page_num + 1}"))
        self.setLayout(layout)

    def eventFilter(self, source, event):
        if source == self.label and event.type() == QEvent.Type.MouseButtonPress:
            self.previewRequested.emit(self.page_num)
            return True
        return super().eventFilter(source, event)

    def setSelectedForPreview(self, selected):
        if selected:
            self.label.setStyleSheet("border: 2px solid darkviolet;")
        else:
            self.label.setStyleSheet("border: 2px solid transparent;")
