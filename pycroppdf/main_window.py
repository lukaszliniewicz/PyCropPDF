import os
import tempfile
import traceback

import fitz
from PyQt6.QtCore import QRectF, QSize, Qt, QThreadPool
from PyQt6.QtGui import QAction, QActionGroup, QImage, QPainter, QPixmap, QColor
from PyQt6.QtWidgets import (QApplication, QFileDialog, QGridLayout,
                             QHBoxLayout, QMainWindow, QMessageBox,
                             QPushButton, QScrollArea, QToolBar, QVBoxLayout,
                             QWidget)

from .widgets import PageGraphicsView, ThumbnailWidget
from .workers import RenderAllPagesWorker, SaveWorker, _translate_rect_to_pdf_coords


class PDFViewer(QMainWindow):
    def __init__(self, input_pdf=None, save_directory=None, save_filename=None):
        super().__init__()
        self.images = []
        self.setAcceptDrops(True)
        self.pdf_doc = None
        self.pdf_path = None
        self.original_pdf_path = None
        self.view_mode = 'odd_even'
        self.save_directory = save_directory
        self.save_filename = save_filename
        self.threadpool = QThreadPool()
        self.is_processing = False
        self.pages_rendered = 0
        self.preview_page_num = None
        self.show_crop_success_msg = False
        self.show_whiteout_success_msg = False
        self.active_crop_info = None
        self._is_syncing_selection = False
        self.undo_stack = []
        self.whiteout_color = (1, 1, 1) # Default white
        
        self.single_view_pixmap_cache = None
        self.odd_view_pixmap_cache = None
        self.even_view_pixmap_cache = None
        
        # Create views
        self.single_view = PageGraphicsView()
        self.odd_view = PageGraphicsView()
        self.even_view = PageGraphicsView()
        
        self.current_view = self.single_view
        
        self.single_view.selectionChanged.connect(self.sync_selection_from_single)
        self.odd_view.selectionChanged.connect(self.sync_selection_to_even)
        self.even_view.selectionChanged.connect(self.sync_selection_to_odd)
        
        self.single_view.colorPicked.connect(self.onColorPicked)
        self.odd_view.colorPicked.connect(self.onColorPicked)
        self.even_view.colorPicked.connect(self.onColorPicked)

        self.single_view.whiteoutRequested.connect(self.handleWhiteoutRequest)
        self.odd_view.whiteoutRequested.connect(self.handleWhiteoutRequest)
        self.even_view.whiteoutRequested.connect(self.handleWhiteoutRequest)

        self.initUI()
        self.showMaximized()

        # Load PDF if provided
        if input_pdf:
            self.loadPDF(input_pdf)

    def initUI(self):
        self.setAcceptDrops(True)
        self.setAttribute(Qt.WidgetAttribute.WA_AcceptDrops, True)
        self.setWindowTitle('PDF Overlay Viewer')
        self.setGeometry(100, 100, 1400, 800)

        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #2b2b2b;
                color: #f0f0f0;
            }
            QMenuBar {
                background-color: #3c3c3c;
                color: #f0f0f0;
            }
            QMenuBar::item:selected {
                background-color: #555555;
            }
            QMenu {
                background-color: #3c3c3c;
                color: #f0f0f0;
                border: 1px solid #555555;
            }
            QMenu::item:selected {
                background-color: #555555;
            }
            QToolBar {
                border: none;
                padding: 5px;
                spacing: 5px;
            }
            QPushButton {
                background-color: #555555;
                color: #f0f0f0;
                border: 1px solid #666666;
                padding: 6px 12px;
                border-radius: 4px;
                font-size: 11px;
            }
            QPushButton:hover {
                background-color: #6a6a6a;
            }
            QPushButton:pressed {
                background-color: #7a7a7a;
            }
            QPushButton:disabled {
                background-color: #444444;
                color: #888888;
                border-color: #555555;
            }
            QToolBar::separator {
                background-color: #555555;
                width: 1px;
                margin: 4px 6px;
            }
            QScrollArea {
                background-color: #2b2b2b;
                border: none;
            }
            QCheckBox {
                color: #f0f0f0;
            }
            QLabel {
                color: #f0f0f0;
            }
        """)

        # Create Menus
        menu_bar = self.menuBar()

        # File menu
        file_menu = menu_bar.addMenu('&File')
        open_action = QAction('&Open PDF...', self)
        open_action.triggered.connect(self.openPDF)
        file_menu.addAction(open_action)

        save_action = QAction('&Save PDF...', self)
        save_action.triggered.connect(self.savePDF)
        file_menu.addAction(save_action)
        file_menu.addSeparator()

        self.fast_save_action = QAction('Fast Save (larger file)', self)
        self.fast_save_action.setCheckable(True)
        self.fast_save_action.setChecked(True)
        self.fast_save_action.setToolTip("Disable compression for faster saving. May result in a larger file size.")
        file_menu.addAction(self.fast_save_action)

        # View menu
        view_menu = menu_bar.addMenu('&View')
        self.view_mode_group = QActionGroup(self)
        self.view_mode_group.setExclusive(True)

        self.odd_even_action = QAction('Separate Odd/Even Pages', self)
        self.odd_even_action.setCheckable(True)
        self.odd_even_action.setChecked(self.view_mode == 'odd_even')
        self.odd_even_action.triggered.connect(lambda: self.setViewMode('odd_even'))
        self.view_mode_group.addAction(self.odd_even_action)
        view_menu.addAction(self.odd_even_action)

        self.all_pages_action = QAction('All Pages Overlay', self)
        self.all_pages_action.setCheckable(True)
        self.all_pages_action.setChecked(self.view_mode == 'all')
        self.all_pages_action.triggered.connect(lambda: self.setViewMode('all'))
        self.view_mode_group.addAction(self.all_pages_action)
        view_menu.addAction(self.all_pages_action)

        # Help menu
        help_menu = menu_bar.addMenu('&Help')
        about_action = QAction('&About', self)
        about_action.triggered.connect(self.showHelp)
        help_menu.addAction(about_action)

        # Create main widget and layout
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)

        # Create toolbar
        toolbar = QToolBar()
        toolbar.setMovable(False)
        toolbar.setFloatable(False)


        # Add Crop Selection button
        self.crop_btn = QPushButton('Crop Selection')
        self.crop_btn.setMinimumWidth(100)
        self.crop_btn.clicked.connect(self.cropSelection)
        toolbar.addWidget(self.crop_btn)

        toolbar.addSeparator()

        # Add Reset Crop button
        self.reset_crop_btn = QPushButton('Reset Crop')
        self.reset_crop_btn.setMinimumWidth(100)
        self.reset_crop_btn.clicked.connect(self.resetCrop)
        toolbar.addWidget(self.reset_crop_btn)

        toolbar.addSeparator()

        # Add White Out button
        self.whiteout_btn = QPushButton('White Out')
        self.whiteout_btn.setCheckable(True)
        self.whiteout_btn.setMinimumWidth(80)
        self.whiteout_btn.clicked.connect(self.toggleWhiteoutTool)
        toolbar.addWidget(self.whiteout_btn)

        # Add Color Picker button
        self.pick_color_btn = QPushButton('Pick Color')
        self.pick_color_btn.setMinimumWidth(80)
        self.pick_color_btn.clicked.connect(self.pickColor)
        toolbar.addWidget(self.pick_color_btn)

        toolbar.addSeparator()

        # Add existing delete button
        self.delete_btn = QPushButton('Delete Selected Pages')
        self.delete_btn.setMinimumWidth(150)
        self.delete_btn.clicked.connect(self.deleteSelectedPages)
        toolbar.addWidget(self.delete_btn)

        toolbar.addSeparator()

        # Add Undo button
        self.undo_btn = QPushButton('Undo')
        self.undo_btn.setMinimumWidth(80)
        self.undo_btn.clicked.connect(self.undo)
        self.undo_btn.setEnabled(False)
        toolbar.addWidget(self.undo_btn)

        main_layout.addWidget(toolbar)

        # Create content widget
        content_widget = QWidget()
        content_layout = QHBoxLayout(content_widget)

        # Create sidebar for thumbnails
        sidebar = QWidget()
        sidebar.setMinimumWidth(220)
        sidebar.setMaximumWidth(220)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(4, 4, 4, 4)  # Add some padding

        # Create thumbnail scroll area
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.thumbnail_widget = QWidget()
        self.thumbnail_layout = QGridLayout(self.thumbnail_widget)
        self.thumbnail_layout.setSpacing(8)  # Increased spacing between thumbnails
        self.scroll_area.setWidget(self.thumbnail_widget)
        sidebar_layout.addWidget(self.scroll_area)

        content_layout.addWidget(sidebar)

        # Create view stack
        self.view_stack = QWidget()
        self.view_stack_layout = QHBoxLayout(self.view_stack)
        self.view_stack_layout.addWidget(self.single_view)
        self.single_view.hide()
        self.view_stack_layout.addWidget(self.odd_view)
        self.view_stack_layout.addWidget(self.even_view)
        
        content_layout.addWidget(self.view_stack)
        main_layout.addWidget(content_widget)

    def invalidate_pixmap_cache(self):
        self.single_view_pixmap_cache = None
        self.odd_view_pixmap_cache = None
        self.even_view_pixmap_cache = None

    def resetCrop(self):
        if not self.active_crop_info:
            QMessageBox.information(self, "Info", "No crop is currently active.")
            return
        
        self.active_crop_info = None
        self.reloadImages()
        QMessageBox.information(self, "Success", "Crop has been reset.")

    def toggleWhiteoutTool(self):
        is_active = self.whiteout_btn.isChecked()
        tool = 'whiteout' if is_active else 'select'
        
        self.single_view.setTool(tool)
        self.odd_view.setTool(tool)
        self.even_view.setTool(tool)
        
        if is_active:
            self.crop_btn.setEnabled(False)
            self.delete_btn.setEnabled(False)
        else:
            self.crop_btn.setEnabled(True)
            self.delete_btn.setEnabled(True)

    def pickColor(self):
        # Deactivate whiteout tool if active
        if self.whiteout_btn.isChecked():
            self.whiteout_btn.setChecked(False)
            self.toggleWhiteoutTool()

        self.single_view.setTool('pick_color')
        self.odd_view.setTool('pick_color')
        self.even_view.setTool('pick_color')
        
        QMessageBox.information(self, "Pick Color", "Click on the page to select a color for whiteout.")

    def onColorPicked(self, color):
        self.whiteout_color = (color.redF(), color.greenF(), color.blueF())
        QMessageBox.information(self, "Color Picked", f"Whiteout color set to RGB({int(color.red())}, {int(color.green())}, {int(color.blue())})")
        # Restore select tool
        self.single_view.setTool('select')
        self.odd_view.setTool('select')
        self.even_view.setTool('select')

    def pushUndo(self):
        if self.pdf_doc:
            self.undo_stack.append(self.pdf_doc.tobytes())
            if len(self.undo_stack) > 10: # Limit stack size
                self.undo_stack.pop(0)
            self.undo_btn.setEnabled(True)

    def undo(self):
        if not self.undo_stack:
            return
        
        try:
            self.setUIProcessing(True)
            pdf_bytes = self.undo_stack.pop()
            if not self.undo_stack:
                self.undo_btn.setEnabled(False)
            
            if self.pdf_doc:
                self.pdf_doc.close()
            
            self.pdf_doc = fitz.open("pdf", pdf_bytes)
            self.active_crop_info = None # Reset crop on undo to avoid sync issues
            self.reloadImages()
            QMessageBox.information(self, "Success", "Undo successful.")
        except Exception as e:
            self.setUIProcessing(False)
            QMessageBox.critical(self, "Error", f"Failed to undo: {str(e)}")

    def sync_selection_from_single(self, rect):
        if self._is_syncing_selection:
            return
        
        self._is_syncing_selection = True
        self.odd_view.setSelection(rect)
        self.even_view.setSelection(rect)
        self._is_syncing_selection = False

    def sync_selection_to_even(self, rect):
        if self._is_syncing_selection:
            return
        
        self._is_syncing_selection = True
        # Sync size to even view, but not position if a selection already exists
        even_selection = self.even_view.getSelectionRect()
        if even_selection:
            new_even_rect = QRectF(even_selection.topLeft(), rect.size())
            self.even_view.setSelection(new_even_rect)
        else:  # If even has no selection, copy the new one from odd
            self.even_view.setSelection(rect)
        self.single_view.setSelection(rect)
        self._is_syncing_selection = False

    def sync_selection_to_odd(self, rect):
        if self._is_syncing_selection:
            return

        self._is_syncing_selection = True
        # Sync size to odd view, but not position if a selection already exists
        odd_selection = self.odd_view.getSelectionRect()
        if odd_selection:
            new_odd_rect = QRectF(odd_selection.topLeft(), rect.size())
            self.odd_view.setSelection(new_odd_rect)
        else:  # If odd has no selection, copy the new one from even
            self.odd_view.setSelection(rect)
        self.single_view.setSelection(rect)
        self._is_syncing_selection = False

    def setViewMode(self, mode):
        if self.view_mode == mode:
            return

        selection = None
        if self.view_mode == 'all':
            selection = self.single_view.getSelectionRect()
        else:
            selection = self.odd_view.getSelectionRect()
        
        self.view_mode = mode
        if mode == 'all':
            self.all_pages_action.setChecked(True)
        else:
            self.odd_even_action.setChecked(True)
        
        self.updateOverlay()
        
        if selection:
            if self.view_mode == 'all':
                self.single_view.setSelection(selection)
            else:
                self.odd_view.setSelection(selection)

    def showHelp(self):
        help_text = """<b>Basic Usage:</b>
<ol>
<li>Open a PDF using <b>File > Open PDF...</b> or by dragging it into the window.</li>
<li>Select a crop area by clicking and dragging on the page view(s).</li>
<li>Click <b>Crop Selection</b> to apply the crop. You can reset it with <b>Reset Crop</b>.</li>
<li>Use the checkboxes next to the thumbnails to select pages for deletion, then click <b>Delete Selected Pages</b>.</li>
<li>Use the <b>View</b> menu to switch between a single overlay of all pages or separate overlays for odd and even pages.</li>
<li>Save your changes with <b>File > Save PDF...</b>.</li>
</ol>"""
        QMessageBox.information(self, "About PyCropPDF", help_text)

    def togglePagePreview(self, page_num):
        if self.is_processing:
            return

        selection = None
        is_exiting_preview = (self.preview_page_num == page_num)
        
        if self.preview_page_num is not None: # Currently in preview
            selection = self.single_view.getSelectionRect()
        elif self.view_mode == 'all':
            selection = self.single_view.getSelectionRect()
        else: # odd_even mode
            selection = self.odd_view.getSelectionRect()

        if is_exiting_preview:
            self.preview_page_num = None
        else:
            self.preview_page_num = page_num
        
        self.updateOverlay()

        if selection:
            if self.preview_page_num is not None: # In preview mode (or just entered)
                self.single_view.setSelection(selection)
            elif self.view_mode == 'all':
                self.single_view.setSelection(selection)
            else: # odd/even view
                self.odd_view.setSelection(selection)

        # Update thumbnail selection highlights
        for i in range(self.thumbnail_layout.count()):
            widget = self.thumbnail_layout.itemAt(i).widget()
            if isinstance(widget, ThumbnailWidget):
                widget.setSelectedForPreview(widget.page_num == self.preview_page_num)

    def clearAllSelections(self):
        self.single_view.clearSelection()
        self.odd_view.clearSelection()
        self.even_view.clearSelection()

    def openPDF(self):
        fileName, _ = QFileDialog.getOpenFileName(
            self, 
            "Open PDF",
            "",
            "PDF Files (*.pdf)"
        )
        if fileName:
            self.original_pdf_path = fileName  # Store the original path
            self.pdf_path = fileName
            self.loadPDF(fileName)

    def loadPDF(self, pdf_path):
        try:
            if self.pdf_doc:
                self.pdf_doc.close()

            if not pdf_path.endswith('.temp.pdf') and not self.original_pdf_path:
                self.original_pdf_path = pdf_path

            self.pdf_doc = fitz.open(pdf_path)
            self.active_crop_info = None
            self.preview_page_num = None
            self.undo_stack = []
            self.undo_btn.setEnabled(False)
            
            for view in [self.single_view, self.odd_view, self.even_view]:
                view.clearScene()
                view.selecting = False
                view.selection_start = None
                view.selection_rect = None
                
            self.reloadImages()

        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load PDF: {str(e)}")
            self.setUIProcessing(False)

    def reloadImages(self):
        if not self.pdf_doc:
            return

        self.setUIProcessing(True)
        self.invalidate_pixmap_cache()

        num_pages = len(self.pdf_doc)
        if num_pages == 0:
            self.updateThumbnails()
            self.updateOverlay()
            self.setUIProcessing(False)
            return

        self.images = [None] * num_pages
        self.pages_rendered = 0
        
        pdf_bytes = self.pdf_doc.tobytes()
        worker = RenderAllPagesWorker(pdf_bytes, num_pages, self.active_crop_info)
        worker.signals.result.connect(self.pageRendered)
        worker.signals.error.connect(self.processingError)
        worker.signals.finished.connect(self.processingFinished)
        self.threadpool.start(worker)

    def pageRendered(self, result):
        page_num, image = result
        self.images[page_num] = image
        self.pages_rendered += 1
        
        if self.pages_rendered == len(self.pdf_doc):
            self.updateThumbnails()
            self.updateOverlay()

    def showSinglePagePreview(self, page_num):
        self.single_view.show()
        self.odd_view.hide()
        self.even_view.hide()
        
        self.single_view.clearScene()
        
        if 0 <= page_num < len(self.images):
            image = self.images[page_num]
            pixmap = QPixmap.fromImage(image)
            self.single_view.scene.addPixmap(pixmap)
            self.single_view.setSceneRect(QRectF(pixmap.rect()))
            self.single_view.fitInView(self.single_view.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def updateOverlay(self):
        if not self.images:
            return

        if self.preview_page_num is not None:
            self.showSinglePagePreview(self.preview_page_num)
            return

        if self.view_mode == 'all':
            self.single_view.show()
            self.odd_view.hide()
            self.even_view.hide()
            self.updateSingleViewOverlay()
        else:
            self.single_view.hide()
            self.odd_view.show()
            self.even_view.show()
            self.updateSplitViewOverlay()

    def updateSingleViewOverlay(self):
        if not self.images:
            return
            
        self.single_view.clearScene()
        
        if self.single_view_pixmap_cache is None:
            # Find maximum dimensions
            valid_images = [img for img in self.images if img]
            if not valid_images:
                return
            max_width = max(img.width() for img in valid_images)
            max_height = max(img.height() for img in valid_images)
            
            # Create a transparent base image of maximum size
            base_img = QImage(max_width, max_height, QImage.Format.Format_ARGB32)
            base_img.fill(Qt.GlobalColor.transparent)
            
            painter = QPainter(base_img)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

            # Draw first page fully opaque
            img = valid_images[0]
            x = (max_width - img.width()) // 2
            y = (max_height - img.height()) // 2
            painter.setOpacity(1.0)
            painter.drawImage(x, y, img)

            # Draw remaining pages semi-transparent
            for img in valid_images[1:]:
                x = (max_width - img.width()) // 2
                y = (max_height - img.height()) // 2
                painter.setOpacity(0.2)
                painter.drawImage(x, y, img)

            painter.end()
            self.single_view_pixmap_cache = QPixmap.fromImage(base_img)

        self.single_view.scene.addPixmap(self.single_view_pixmap_cache)
        self.single_view.setSceneRect(QRectF(self.single_view_pixmap_cache.rect()))
        self.single_view.fitInView(self.single_view.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def updateSplitViewOverlay(self):
        if not self.images:
            return
            
        self.odd_view.clearScene()
        self.even_view.clearScene()

        # Find maximum dimensions across ALL pages to ensure consistent canvas size
        valid_images = [img for img in self.images if img]
        if not valid_images:
            return
        max_width = max(img.width() for img in valid_images)
        max_height = max(img.height() for img in valid_images)

        # Process odd pages
        odd_pages = [img for img in self.images[::2] if img]
        if odd_pages:
            if self.odd_view_pixmap_cache is None:
                base_odd = QImage(max_width, max_height, QImage.Format.Format_ARGB32)
                base_odd.fill(Qt.GlobalColor.transparent)
                
                painter = QPainter(base_odd)
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

                # Draw first odd page fully opaque
                img = odd_pages[0]
                x = (max_width - img.width()) // 2
                y = (max_height - img.height()) // 2
                painter.setOpacity(1.0)
                painter.drawImage(x, y, img)

                # Draw remaining odd pages semi-transparent
                for img in odd_pages[1:]:
                    x = (max_width - img.width()) // 2
                    y = (max_height - img.height()) // 2
                    painter.setOpacity(0.2)
                    painter.drawImage(x, y, img)

                painter.end()
                self.odd_view_pixmap_cache = QPixmap.fromImage(base_odd)

            self.odd_view.scene.addPixmap(self.odd_view_pixmap_cache)
            self.odd_view.setSceneRect(QRectF(self.odd_view_pixmap_cache.rect()))
            self.odd_view.fitInView(self.odd_view.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

        # Process even pages
        even_pages = [img for img in self.images[1::2] if img]
        if even_pages:
            if self.even_view_pixmap_cache is None:
                base_even = QImage(max_width, max_height, QImage.Format.Format_ARGB32)
                base_even.fill(Qt.GlobalColor.transparent)
                
                painter = QPainter(base_even)
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

                # Draw first even page fully opaque
                img = even_pages[0]
                x = (max_width - img.width()) // 2
                y = (max_height - img.height()) // 2
                painter.setOpacity(1.0)
                painter.drawImage(x, y, img)

                # Draw remaining even pages semi-transparent
                for img in even_pages[1:]:
                    x = (max_width - img.width()) // 2
                    y = (max_height - img.height()) // 2
                    painter.setOpacity(0.2)
                    painter.drawImage(x, y, img)

                painter.end()
                self.even_view_pixmap_cache = QPixmap.fromImage(base_even)

            self.even_view.scene.addPixmap(self.even_view_pixmap_cache)
            self.even_view.setSceneRect(QRectF(self.even_view_pixmap_cache.rect()))
            self.even_view.fitInView(self.even_view.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def updateThumbnails(self):
        # Reset any existing row stretches to prevent alignment issues with varying page counts
        for i in range(self.thumbnail_layout.rowCount()):
            self.thumbnail_layout.setRowStretch(i, 0)

        # Clear existing thumbnails
        while self.thumbnail_layout.count():
            item = self.thumbnail_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # Create grid layout if not already using one
        if not isinstance(self.thumbnail_layout, QGridLayout):
            old_widget = self.thumbnail_widget
            self.thumbnail_widget = QWidget()
            self.thumbnail_layout = QGridLayout(self.thumbnail_widget)
            self.thumbnail_layout.setSpacing(4)
            self.scroll_area.setWidget(self.thumbnail_widget)
            old_widget.deleteLater()

        # Add thumbnails in a 2-column grid
        for i, image in enumerate(self.images):
            row = i // 2
            col = i % 2
            thumbnail = ThumbnailWidget(i, image)
            thumbnail.previewRequested.connect(self.togglePagePreview)
            self.thumbnail_layout.addWidget(thumbnail, row, col)

        # Add a stretch to the bottom to align thumbnails to the top
        if self.images:
            num_rows = (len(self.images) + 1) // 2
            self.thumbnail_layout.setRowStretch(num_rows, 1)

    def getSelectedPages(self):
        selected = []
        for i in range(self.thumbnail_layout.count()):
            widget = self.thumbnail_layout.itemAt(i).widget()
            if isinstance(widget, ThumbnailWidget) and widget.checkbox.isChecked():
                selected.append(widget.page_num)
        return selected
    
    def dragEnterEvent(self, event):
        try:
            mime_data = event.mimeData()
            if mime_data.hasUrls():
                for url in mime_data.urls():
                    file_path = url.toLocalFile()
                    if file_path.lower().endswith('.pdf'):
                        event.accept()
                        return
            event.ignore()
        except Exception:
            event.ignore()

    def dragMoveEvent(self, event):
        try:
            mime_data = event.mimeData()
            if mime_data.hasUrls():
                for url in mime_data.urls():
                    if url.toLocalFile().lower().endswith('.pdf'):
                        event.accept()
                        return
            event.ignore()
        except Exception:
            event.ignore()

    def dropEvent(self, event):
        try:
            mime_data = event.mimeData()
            if mime_data.hasUrls():
                for url in mime_data.urls():
                    file_path = url.toLocalFile()
                    if file_path.lower().endswith('.pdf'):
                        self.original_pdf_path = file_path
                        self.pdf_path = file_path
                        self.loadPDF(file_path)
                        event.accept()
                        return
            event.ignore()
        except Exception:
            event.ignore()

    def cropSelection(self):
        if self.is_processing or not self.pdf_doc:
            return

        crop_rects = {}
        if self.view_mode == 'all':
            scene_rect = self.single_view.getSelectionRect()
            if not scene_rect:
                QMessageBox.warning(self, "Warning", "Please make a selection first.")
                return
            for page_num in range(len(self.pdf_doc)):
                crop_rects[page_num] = scene_rect
        else:
            odd_rect = self.odd_view.getSelectionRect()
            even_rect = self.even_view.getSelectionRect()
            if not (odd_rect or even_rect):
                QMessageBox.warning(self, "Warning", "Please make at least one selection.")
                return
            if odd_rect:
                for i in range(0, len(self.pdf_doc), 2):
                    crop_rects[i] = odd_rect
            if even_rect:
                for i in range(1, len(self.pdf_doc), 2):
                    crop_rects[i] = even_rect
        
        self.active_crop_info = {
            'rects': crop_rects,
            'view_mode': self.view_mode,
            'image_dims': [(img.width(), img.height()) for img in self.images if img]
        }
        
        self.show_crop_success_msg = True
        self.reloadImages()

    def handleWhiteoutRequest(self, rect):
        if self.is_processing or not self.pdf_doc:
            return

        if self.active_crop_info:
            QMessageBox.warning(self, "Warning", "Cannot apply whiteout while a crop is active. Please Reset Crop first.")
            return

        sender = self.sender()
        target_pages = []

        if self.preview_page_num is not None:
            target_pages = [self.preview_page_num]
        elif sender == self.single_view:
            target_pages = range(len(self.pdf_doc))
        elif sender == self.odd_view:
            target_pages = range(0, len(self.pdf_doc), 2)
        elif sender == self.even_view:
            target_pages = range(1, len(self.pdf_doc), 2)
        
        if not target_pages:
            return

        self.applyWhiteout(rect, target_pages)

    def applyWhiteout(self, rect, target_pages):
        # Prepare for translation
        valid_images = [img for img in self.images if img]
        if not valid_images:
            return
            
        max_width = max(img.width() for img in valid_images)
        max_height = max(img.height() for img in valid_images)
        
        max_dims = (max_width, max_height)
        
        # We need page dims for each page
        all_page_dims = [(img.width(), img.height()) if img else (0,0) for img in self.images]

        try:
            self.setUIProcessing(True)
            self.pushUndo() # Save state before modification

            for page_num in target_pages:
                page = self.pdf_doc[page_num]
                
                visual_rect = None
                
                # If in single page preview, the rect is relative to that page's image (0,0)
                if self.preview_page_num is not None:
                    page_dims = all_page_dims[page_num]
                    ref_rect = page.rect
                    scale_factor_x = ref_rect.width / page_dims[0] if page_dims[0] > 0 else 0
                    scale_factor_y = ref_rect.height / page_dims[1] if page_dims[1] > 0 else 0
                    
                    crop_x0 = rect.x() * scale_factor_x + ref_rect.x0
                    crop_y0 = rect.y() * scale_factor_y + ref_rect.y0
                    crop_x1 = crop_x0 + (rect.width() * scale_factor_x)
                    crop_y1 = crop_y0 + (rect.height() * scale_factor_y)
                    
                    visual_rect = fitz.Rect(crop_x0, crop_y0, crop_x1, crop_y1)
                else:
                    # Translate to visual PDF coordinates using overlay logic
                    visual_rect = _translate_rect_to_pdf_coords(
                        rect,
                        all_page_dims[page_num],
                        page.rect,
                        page_num,
                        self.view_mode,
                        max_dims,
                        max_dims, # max_odd_dims
                        max_dims  # max_even_dims
                    )
                
                # Transform to physical coordinates for drawing
                pdf_rect = visual_rect * page.derotation_matrix
                
                # Draw rectangle with selected color
                page.draw_rect(pdf_rect, color=self.whiteout_color, fill=self.whiteout_color, width=0)

            self.show_whiteout_success_msg = True
            self.reloadImages()
            
        except Exception as e:
            self.setUIProcessing(False)
            QMessageBox.critical(self, "Error", f"Failed to apply whiteout: {str(e)}")

    def deleteSelectedPages(self):
        if not self.pdf_doc:
            QMessageBox.warning(self, "Warning", "No PDF is open.")
            return
            
        selected_pages = sorted(self.getSelectedPages(), reverse=True)
        if not selected_pages:
            QMessageBox.warning(self, "Warning", "Please select pages to delete.")
            return

        try:
            self.pushUndo() # Save state before modification
            self.invalidate_pixmap_cache()

            # Remap crop rectangles if a crop is active
            if self.active_crop_info and 'rects' in self.active_crop_info:
                new_crop_rects = {}
                current_page_index = 0
                original_rects = self.active_crop_info['rects']
                # Use non-reversed list for set for efficiency
                deleted_set = set(self.getSelectedPages())

                for i in range(len(self.pdf_doc)):
                    if i not in deleted_set:
                        if i in original_rects:
                            new_crop_rects[current_page_index] = original_rects[i]
                        current_page_index += 1
                self.active_crop_info['rects'] = new_crop_rects

            for page_num in selected_pages:
                self.pdf_doc.delete_page(page_num)
                self.images.pop(page_num)
                if self.active_crop_info and self.active_crop_info.get('image_dims'):
                    self.active_crop_info['image_dims'].pop(page_num)

            self.updateThumbnails()
            self.updateOverlay()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to delete pages: {str(e)}")

    def savePDF(self):
        if self.is_processing or not self.pdf_doc:
            return

        if self.save_directory:
            if self.save_filename:
                save_path = os.path.join(self.save_directory, self.save_filename)
            else:
                original_name = os.path.basename(self.original_pdf_path or self.pdf_path)
                base_name = os.path.splitext(original_name)[0]
                save_path = os.path.join(self.save_directory, f"{base_name}_modified.pdf")
        else:
            save_path, _ = QFileDialog.getSaveFileName(
                self, "Save PDF", "", "PDF Files (*.pdf)"
            )

        if not save_path:
            return

        self.setUIProcessing(True)
        deflate_enabled = not self.fast_save_action.isChecked()
        pdf_bytes = self.pdf_doc.tobytes()
        worker = SaveWorker(pdf_bytes, save_path, self.active_crop_info, deflate=deflate_enabled)
        worker.signals.result.connect(self.saveFinished)
        worker.signals.error.connect(self.processingError)
        worker.signals.finished.connect(self.processingFinished)
        self.threadpool.start(worker)

    def setUIProcessing(self, is_processing):
        self.is_processing = is_processing
        self.menuBar().setEnabled(not is_processing)
        self.crop_btn.setDisabled(is_processing)
        self.reset_crop_btn.setDisabled(is_processing)
        self.whiteout_btn.setDisabled(is_processing)
        self.pick_color_btn.setDisabled(is_processing)
        self.delete_btn.setDisabled(is_processing)
        self.undo_btn.setDisabled(is_processing or not self.undo_stack)
        
        # If processing finished, restore button states based on logic
        if not is_processing:
            if self.whiteout_btn.isChecked():
                self.crop_btn.setEnabled(False)
                self.delete_btn.setEnabled(False)
        
        if is_processing:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        else:
            QApplication.restoreOverrideCursor()

    def saveFinished(self, success):
        if success:
            QMessageBox.information(self, "Success", "PDF saved successfully!")

    def processingError(self, error_str):
        QMessageBox.critical(self, "Error", f"An error occurred:\n{error_str}")
        print(error_str)

    def processingFinished(self):
        self.setUIProcessing(False)
        if self.show_crop_success_msg:
            self.show_crop_success_msg = False
            QMessageBox.information(self, "Success", "PDF cropped successfully!")
        elif self.show_whiteout_success_msg:
            self.show_whiteout_success_msg = False
            QMessageBox.information(self, "Success", "Whiteout applied successfully!")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Only update overlay if a PDF is loaded and not currently processing pages
        if self.pdf_doc and not self.is_processing:
            self.updateOverlay()

    def closeEvent(self, event):
        if self.pdf_doc:
            self.pdf_doc.close()
        
        if self.pdf_path and self.pdf_path.endswith('.temp.pdf'):
            try:
                os.remove(self.pdf_path)
            except:
                pass
        
        super().closeEvent(event)
