import os
import tempfile
import traceback

import fitz
from PyQt6.QtCore import QRectF, QSize, Qt, QThreadPool
from PyQt6.QtGui import (QAction, QActionGroup, QColor, QImage, QKeySequence,
                         QPainter, QPixmap)
from PyQt6.QtWidgets import (QApplication, QButtonGroup, QFileDialog,
                             QGridLayout, QHBoxLayout, QLabel, QMainWindow,
                             QMessageBox, QPushButton, QScrollArea,
                             QSizePolicy, QToolBar, QVBoxLayout, QWidget)

from .state import (clone_crop_info, remap_crop_info_after_deletions,
                    remap_page_indices_after_deletions)
from .widgets import PageGraphicsView, ThumbnailWidget
from .workers import RenderAllPagesWorker, SaveWorker, _translate_rect_to_pdf_coords


class PDFViewer(QMainWindow):
    def __init__(self, input_pdf=None, save_directory=None, save_filename=None, manifest_path=None):
        super().__init__()
        self.images = []
        self.setAcceptDrops(True)
        self.pdf_doc = None
        self.pdf_path = None
        self.original_pdf_path = None
        self.view_mode = 'odd_even'
        self.save_directory = save_directory
        self.save_filename = save_filename
        self.manifest_path = manifest_path
        self.threadpool = QThreadPool()
        self.is_processing = False
        self.pages_rendered = 0
        self.preview_page_num = None
        self.pending_status_message = ""
        self.active_crop_info = None
        self.active_tool = 'crop'
        self._is_syncing_selection = False
        self.undo_stack = []
        self.page_map = []
        self.original_page_count = 0
        self.whiteout_operations = []
        self.whiteout_color = (1, 1, 1) # Default white
        self.selected_pages = set()
        self.selection_anchor = None
        
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
            QPushButton:checked {
                background-color: #5d4f7f;
                border-color: #9575cd;
            }
            QPushButton#saveButton {
                background-color: #7e57c2;
                color: white;
                border-color: #9575cd;
                font-weight: bold;
            }
            QPushButton#saveButton:hover {
                background-color: #9575cd;
            }
            QPushButton#saveButton:pressed {
                background-color: #5e35b1;
            }
            QPushButton#saveButton:disabled {
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
        save_action.setShortcut(QKeySequence.StandardKey.Save)
        save_action.triggered.connect(self.savePDF)
        file_menu.addAction(save_action)
        file_menu.addSeparator()

        self.fast_save_action = QAction('Fast Save (larger file)', self)
        self.fast_save_action.setCheckable(True)
        self.fast_save_action.setChecked(True)
        self.fast_save_action.setToolTip("Disable compression for faster saving. May result in a larger file size.")
        file_menu.addAction(self.fast_save_action)

        edit_menu = menu_bar.addMenu('&Edit')
        self.undo_action = QAction('&Undo', self)
        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.undo_action.triggered.connect(self.undo)
        edit_menu.addAction(self.undo_action)

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


        toolbar.addWidget(QLabel("Tool:"))

        self.tool_button_group = QButtonGroup(self)
        self.tool_button_group.setExclusive(True)

        self.crop_tool_btn = QPushButton('Crop Box')
        self.crop_tool_btn.setCheckable(True)
        self.crop_tool_btn.setChecked(True)
        self.crop_tool_btn.setToolTip("Draw or adjust the crop box.")
        self.crop_tool_btn.clicked.connect(lambda: self.setActiveTool('crop'))
        self.tool_button_group.addButton(self.crop_tool_btn)
        toolbar.addWidget(self.crop_tool_btn)

        self.whiteout_btn = QPushButton('Whiteout')
        self.whiteout_btn.setCheckable(True)
        self.whiteout_btn.setToolTip("Draw a filled rectangle over unwanted content.")
        self.whiteout_btn.clicked.connect(lambda: self.setActiveTool('whiteout'))
        self.tool_button_group.addButton(self.whiteout_btn)
        toolbar.addWidget(self.whiteout_btn)

        self.pick_color_btn = QPushButton('Whiteout Color')
        self.pick_color_btn.setMinimumWidth(100)
        self.pick_color_btn.clicked.connect(self.pickColor)
        toolbar.addWidget(self.pick_color_btn)

        toolbar.addSeparator()

        self.crop_btn = QPushButton('Apply Crop')
        self.crop_btn.setMinimumWidth(100)
        self.crop_btn.clicked.connect(self.cropSelection)
        toolbar.addWidget(self.crop_btn)

        self.reset_crop_btn = QPushButton('Reset Crop')
        self.reset_crop_btn.setMinimumWidth(100)
        self.reset_crop_btn.clicked.connect(self.resetCrop)
        toolbar.addWidget(self.reset_crop_btn)

        toolbar.addSeparator()

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

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)

        self.save_btn = QPushButton('Save PDF')
        self.save_btn.setObjectName('saveButton')
        self.save_btn.setMinimumWidth(110)
        self.save_btn.clicked.connect(self.savePDF)
        toolbar.addWidget(self.save_btn)

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
        self.statusBar().showMessage(
            "Crop Box tool selected. Draw a box, then click Apply Crop.",
            6000,
        )
        self.updateActionState()

    def invalidate_pixmap_cache(self):
        self.single_view_pixmap_cache = None
        self.odd_view_pixmap_cache = None
        self.even_view_pixmap_cache = None

    def resetCrop(self):
        if not self.active_crop_info:
            QMessageBox.information(self, "Info", "No crop is currently active.")
            return

        self.pushUndo(include_pdf=False)
        self.active_crop_info = None
        self.pending_status_message = "Crop reset."
        self.reloadImages()

    def setActiveTool(self, tool):
        if tool == 'whiteout' and self.active_crop_info:
            QMessageBox.warning(
                self,
                "Reset Crop First",
                "Whiteout cannot be positioned reliably on an active cropped preview. "
                "Reset the crop, apply whiteout, then crop again.",
            )
            tool = 'crop'

        self.active_tool = tool if tool in {'crop', 'whiteout'} else 'crop'
        self.crop_tool_btn.setChecked(self.active_tool == 'crop')
        self.whiteout_btn.setChecked(self.active_tool == 'whiteout')
        view_tool = 'whiteout' if self.active_tool == 'whiteout' else 'select'
        for view in [self.single_view, self.odd_view, self.even_view]:
            view.setTool(view_tool)

        if self.active_tool == 'whiteout':
            self.statusBar().showMessage(
                "Whiteout tool selected. Drag over unwanted content. Selected pages take priority.",
                6000,
            )
        else:
            self.statusBar().showMessage(
                "Crop Box tool selected. Draw a box, then click Apply Crop.",
                6000,
            )
        self.updateActionState()

    def pickColor(self):
        for view in [self.single_view, self.odd_view, self.even_view]:
            view.setTool('pick_color')
        self.statusBar().showMessage("Click a page pixel to choose the whiteout color.", 6000)

    def onColorPicked(self, color):
        self.whiteout_color = (color.redF(), color.greenF(), color.blueF())
        self.statusBar().showMessage(
            f"Whiteout color set to RGB({int(color.red())}, {int(color.green())}, {int(color.blue())}).",
            6000,
        )
        self.setActiveTool('whiteout' if not self.active_crop_info else 'crop')

    def pushUndo(self, include_pdf=True):
        if self.pdf_doc:
            self.undo_stack.append(
                {
                    "pdf_bytes": self.pdf_doc.tobytes() if include_pdf else None,
                    "page_map": list(self.page_map),
                    "whiteouts": [dict(operation) for operation in self.whiteout_operations],
                    "active_crop_info": clone_crop_info(self.active_crop_info),
                    "selected_pages": set(self.selected_pages),
                    "selection_anchor": self.selection_anchor,
                    "preview_page_num": self.preview_page_num,
                }
            )
            if len(self.undo_stack) > 10: # Limit stack size
                self.undo_stack.pop(0)
            self.updateActionState()

    def undo(self):
        if not self.undo_stack:
            return
        
        try:
            self.statusBar().showMessage("Restoring previous document state...")
            snapshot = self.undo_stack.pop()

            if snapshot.get("pdf_bytes") is not None:
                if self.pdf_doc:
                    self.pdf_doc.close()
                self.pdf_doc = fitz.open("pdf", snapshot["pdf_bytes"])
            self.page_map = list(snapshot["page_map"])
            self.whiteout_operations = list(snapshot["whiteouts"])
            self.active_crop_info = clone_crop_info(snapshot.get("active_crop_info"))
            self.selected_pages = set(snapshot.get("selected_pages", set()))
            self.selection_anchor = snapshot.get("selection_anchor")
            self.preview_page_num = snapshot.get("preview_page_num")
            self.pending_status_message = "Undo complete."
            self.reloadImages()
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
<li>Choose <b>Crop Box</b> or <b>Whiteout</b> in the toolbar, then drag on the page view.</li>
<li>Click <b>Apply Crop</b> to preview a crop. You can restore it with <b>Reset Crop</b>.</li>
<li>Click a thumbnail to select one page. Use <b>Ctrl</b> to toggle pages and <b>Shift</b> to select a range.</li>
<li>Selected pages limit crop and whiteout operations and can be removed with <b>Delete Selected Pages</b>.</li>
<li>Use the <b>View</b> menu to switch between a single overlay of all pages or separate overlays for odd and even pages.</li>
<li>Use <b>Undo</b> to restore the previous crop, whiteout, or page deletion.</li>
<li>Save your changes with the purple <b>Save PDF</b> button.</li>
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
            self.pdf_path = pdf_path
            self.active_crop_info = None
            self.preview_page_num = None
            self.pending_status_message = ""
            self.undo_stack = []
            self.page_map = list(range(len(self.pdf_doc)))
            self.original_page_count = len(self.pdf_doc)
            self.whiteout_operations = []
            self.selected_pages = set()
            self.selection_anchor = None
            
            for view in [self.single_view, self.odd_view, self.even_view]:
                view.clearScene()
                view.selecting = False
                view.selection_start = None
                view.selection_rect = None

            self.setActiveTool('crop')
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
        self.statusBar().showMessage(f"Rendering page previews: 0/{num_pages}...")
        
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
        self.statusBar().showMessage(
            f"Rendering page previews: {self.pages_rendered}/{len(self.pdf_doc)}..."
        )
        
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
            thumbnail.selectionRequested.connect(self.handleThumbnailSelection)
            thumbnail.setSelectedForDeletion(i in self.selected_pages)
            thumbnail.setSelectedForPreview(i == self.preview_page_num)
            self.thumbnail_layout.addWidget(thumbnail, row, col)

        # Add a stretch to the bottom to align thumbnails to the top
        if self.images:
            num_rows = (len(self.images) + 1) // 2
            self.thumbnail_layout.setRowStretch(num_rows, 1)

    def getSelectedPages(self):
        return sorted(self.selected_pages)

    def _thumbnail_widgets(self):
        for i in range(self.thumbnail_layout.count()):
            widget = self.thumbnail_layout.itemAt(i).widget()
            if isinstance(widget, ThumbnailWidget):
                yield widget

    def _sync_thumbnail_selection(self):
        for widget in self._thumbnail_widgets():
            widget.setSelectedForDeletion(widget.page_num in self.selected_pages)
            widget.setSelectedForPreview(widget.page_num == self.preview_page_num)

    def handleThumbnailSelection(self, page_num, modifiers):
        if self.is_processing:
            return

        page_num = int(page_num)
        modifiers = modifiers or Qt.KeyboardModifier.NoModifier
        control = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)

        if shift and self.selection_anchor is not None:
            start, end = sorted((self.selection_anchor, page_num))
            range_selection = set(range(start, end + 1))
            if control:
                self.selected_pages.update(range_selection)
            else:
                self.selected_pages = range_selection
        elif control:
            if page_num in self.selected_pages:
                self.selected_pages.remove(page_num)
            else:
                self.selected_pages.add(page_num)
            self.selection_anchor = page_num
        else:
            self.selected_pages = {page_num}
            self.selection_anchor = page_num

        self._sync_thumbnail_selection()
        count = len(self.selected_pages)
        self.statusBar().showMessage(
            f"{count} page{'s' if count != 1 else ''} selected. "
            "Use Ctrl to toggle pages or Shift to select a range.",
            6000,
        )
    
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
        selected_targets = set(self.getSelectedPages())
        if self.preview_page_num is not None:
            scene_rect = self.single_view.getSelectionRect()
            if not scene_rect:
                QMessageBox.warning(self, "Warning", "Please draw a crop box first.")
                return
            target_pages = selected_targets or {self.preview_page_num}
            for page_num in target_pages:
                crop_rects[page_num] = QRectF(scene_rect)
        elif self.view_mode == 'all':
            scene_rect = self.single_view.getSelectionRect()
            if not scene_rect:
                QMessageBox.warning(self, "Warning", "Please draw a crop box first.")
                return
            target_pages = selected_targets or set(range(len(self.pdf_doc)))
            for page_num in target_pages:
                crop_rects[page_num] = QRectF(scene_rect)
        else:
            odd_rect = self.odd_view.getSelectionRect()
            even_rect = self.even_view.getSelectionRect()
            if not (odd_rect or even_rect):
                QMessageBox.warning(self, "Warning", "Please draw at least one crop box.")
                return
            if odd_rect:
                for i in range(0, len(self.pdf_doc), 2):
                    if not selected_targets or i in selected_targets:
                        crop_rects[i] = QRectF(odd_rect)
            if even_rect:
                for i in range(1, len(self.pdf_doc), 2):
                    if not selected_targets or i in selected_targets:
                        crop_rects[i] = QRectF(even_rect)

        if not crop_rects:
            QMessageBox.warning(
                self,
                "Warning",
                "The selected pages do not have a crop box for their odd/even group.",
            )
            return

        self.pushUndo(include_pdf=False)
        self.active_crop_info = {
            'rects': crop_rects,
            'view_mode': self.view_mode,
            'image_dims': [
                (img.width(), img.height()) if img else (0, 0)
                for img in self.images
            ],
        }

        self.pending_status_message = (
            f"Crop preview applied to {len(crop_rects)} page"
            f"{'s' if len(crop_rects) != 1 else ''}."
        )
        self.reloadImages()

    def handleWhiteoutRequest(self, rect):
        if self.is_processing or not self.pdf_doc:
            return

        if self.active_crop_info:
            QMessageBox.warning(self, "Warning", "Cannot apply whiteout while a crop is active. Please Reset Crop first.")
            return

        sender = self.sender()
        target_pages = self.getSelectedPages()

        if target_pages:
            pass
        elif self.preview_page_num is not None:
            target_pages = [self.preview_page_num]
        elif sender == self.single_view:
            target_pages = list(range(len(self.pdf_doc)))
        elif sender == self.odd_view:
            target_pages = list(range(0, len(self.pdf_doc), 2))
        elif sender == self.even_view:
            target_pages = list(range(1, len(self.pdf_doc), 2))
        
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
            self.statusBar().showMessage(
                f"Applying whiteout to {len(target_pages)} page"
                f"{'s' if len(target_pages) != 1 else ''}..."
            )
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
                original_page = self.page_map[page_num] if page_num < len(self.page_map) else page_num
                self.whiteout_operations.append(
                    {
                        "output_page": page_num + 1,
                        "original_page": original_page + 1,
                        "rect": [round(float(value), 3) for value in pdf_rect],
                        "color": [round(float(value), 4) for value in self.whiteout_color],
                    }
                )

            self.pending_status_message = (
                f"Whiteout applied to {len(target_pages)} page"
                f"{'s' if len(target_pages) != 1 else ''}."
            )
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

            deleted_set = set(selected_pages)
            self.active_crop_info = remap_crop_info_after_deletions(
                self.active_crop_info,
                deleted_set,
            )
            if self.preview_page_num is not None:
                remapped_preview = remap_page_indices_after_deletions(
                    {self.preview_page_num},
                    deleted_set,
                )
                self.preview_page_num = next(iter(remapped_preview), None)

            for page_num in selected_pages:
                self.pdf_doc.delete_page(page_num)
                self.images.pop(page_num)
                self.page_map.pop(page_num)

            self.selected_pages = set()
            self.selection_anchor = None
            self.updateThumbnails()
            self.updateOverlay()
            self.updateActionState()
            self.statusBar().showMessage(
                f"Deleted {len(selected_pages)} page"
                f"{'s' if len(selected_pages) != 1 else ''}.",
                6000,
            )
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
        self.statusBar().showMessage("Saving PDF and provenance manifest...")
        deflate_enabled = not self.fast_save_action.isChecked()
        pdf_bytes = self.pdf_doc.tobytes()
        manifest_path = self.manifest_path or f"{save_path}.pycroppdf.json"
        worker = SaveWorker(
            pdf_bytes,
            save_path,
            self.active_crop_info,
            deflate=deflate_enabled,
            source_path=self.original_pdf_path or self.pdf_path,
            manifest_path=manifest_path,
            page_map=self.page_map,
            original_page_count=self.original_page_count,
            whiteouts=self.whiteout_operations,
        )
        worker.signals.result.connect(self.saveFinished)
        worker.signals.error.connect(self.processingError)
        worker.signals.finished.connect(self.processingFinished)
        self.threadpool.start(worker)

    def updateActionState(self):
        has_document = self.pdf_doc is not None
        available = has_document and not self.is_processing
        self.crop_tool_btn.setEnabled(available)
        self.whiteout_btn.setEnabled(available and not self.active_crop_info)
        self.crop_btn.setEnabled(available and self.active_tool == 'crop')
        self.reset_crop_btn.setEnabled(available and bool(self.active_crop_info))
        self.pick_color_btn.setEnabled(available)
        self.delete_btn.setEnabled(available)
        self.undo_btn.setEnabled(available and bool(self.undo_stack))
        self.undo_action.setEnabled(available and bool(self.undo_stack))
        self.save_btn.setEnabled(available)

    def setUIProcessing(self, is_processing):
        was_processing = self.is_processing
        self.is_processing = is_processing
        self.menuBar().setEnabled(not is_processing)
        self.updateActionState()

        if is_processing and not was_processing:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        elif not is_processing and was_processing:
            QApplication.restoreOverrideCursor()

    def saveFinished(self, success):
        if success:
            self.statusBar().showMessage("PDF and provenance manifest saved successfully.", 8000)

    def processingError(self, error_str):
        self.pending_status_message = ""
        self.statusBar().showMessage("The operation failed.", 8000)
        QMessageBox.critical(self, "Error", f"An error occurred:\n{error_str}")
        print(error_str)

    def processingFinished(self):
        self.setUIProcessing(False)
        if self.pending_status_message:
            self.statusBar().showMessage(self.pending_status_message, 8000)
            self.pending_status_message = ""

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
