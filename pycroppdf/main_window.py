import logging
import os

import fitz
from PyQt6.QtCore import QRectF, Qt, QThreadPool
from PyQt6.QtGui import QAction, QActionGroup, QImage, QKeySequence, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .provenance import sha256_bytes
from .state import (
    clone_crop_info,
    remap_crop_info_after_deletions,
    remap_page_indices_after_deletions,
)
from .widgets import PageGraphicsView, ThumbnailWidget
from .workers import RenderAllPagesWorker, SaveWorker, rect_to_tuple, scene_rect_to_pdf_coords

LOGGER = logging.getLogger(__name__)


class PDFViewer(QMainWindow):
    def __init__(self, input_pdf=None, save_directory=None, save_filename=None, manifest_path=None):
        super().__init__()
        self.images = []
        self.setAcceptDrops(True)
        self.pdf_doc = None
        self.pdf_path = None
        self.original_pdf_path = None
        self.source_sha256 = None
        self.view_mode = "odd_even"
        self.save_directory = save_directory
        self.save_filename = save_filename
        self.manifest_path = manifest_path
        self.threadpool = QThreadPool()
        self.is_processing = False
        self.pages_rendered = 0
        self._render_target_pages = set()
        self._pending_render_pages = set()
        self._render_was_full = True
        self.preview_page_num = None
        self.pending_status_message = ""
        self._operation_id = 0
        self.is_dirty = False
        self.active_crop_info = None
        self.active_tool = "crop"
        self._is_syncing_selection = False
        self.undo_stack = []
        self.page_map = []
        self.original_page_count = 0
        self.whiteout_operations = []
        self.redaction_operations = []
        self.whiteout_color = (1, 1, 1)  # Default white
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
        self.single_view.redactionRequested.connect(self.handleRedactionRequest)
        self.odd_view.redactionRequested.connect(self.handleRedactionRequest)
        self.even_view.redactionRequested.connect(self.handleRedactionRequest)

        self.initUI()
        self.showMaximized()

        # Load PDF if provided
        if input_pdf:
            self.loadPDF(input_pdf, confirm_discard=False)

    def initUI(self):
        self.setAcceptDrops(True)
        self.setAttribute(Qt.WidgetAttribute.WA_AcceptDrops, True)
        self.setWindowTitle("PyCropPDF")
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
        file_menu = menu_bar.addMenu("&File")
        self.open_action = QAction("&Open PDF...", self)
        self.open_action.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton)
        )
        self.open_action.triggered.connect(self.openPDF)
        file_menu.addAction(self.open_action)

        self.save_action = QAction("&Save PDF...", self)
        self.save_action.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton)
        )
        self.save_action.setShortcut(QKeySequence.StandardKey.Save)
        self.save_action.triggered.connect(self.savePDF)
        file_menu.addAction(self.save_action)
        file_menu.addSeparator()

        self.fast_save_action = QAction("Fast Save (larger file)", self)
        self.fast_save_action.setCheckable(True)
        self.fast_save_action.setChecked(True)
        self.fast_save_action.setToolTip(
            "Disable compression for faster saving. May result in a larger file size."
        )
        file_menu.addAction(self.fast_save_action)

        edit_menu = menu_bar.addMenu("&Edit")
        self.undo_action = QAction("&Undo", self)
        self.undo_action.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowBack))
        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.undo_action.triggered.connect(self.undo)
        edit_menu.addAction(self.undo_action)

        # View menu
        view_menu = menu_bar.addMenu("&View")
        self.view_mode_group = QActionGroup(self)
        self.view_mode_group.setExclusive(True)

        self.odd_even_action = QAction("Separate Odd/Even Pages", self)
        self.odd_even_action.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView)
        )
        self.odd_even_action.setCheckable(True)
        self.odd_even_action.setChecked(self.view_mode == "odd_even")
        self.odd_even_action.triggered.connect(lambda: self.setViewMode("odd_even"))
        self.view_mode_group.addAction(self.odd_even_action)
        view_menu.addAction(self.odd_even_action)

        self.all_pages_action = QAction("All Pages Overlay", self)
        self.all_pages_action.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogListView)
        )
        self.all_pages_action.setCheckable(True)
        self.all_pages_action.setChecked(self.view_mode == "all")
        self.all_pages_action.triggered.connect(lambda: self.setViewMode("all"))
        self.view_mode_group.addAction(self.all_pages_action)
        view_menu.addAction(self.all_pages_action)

        # Help menu
        help_menu = menu_bar.addMenu("&Help")
        about_action = QAction("&About", self)
        about_action.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation)
        )
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

        self.crop_tool_btn = QPushButton("Crop Box")
        self.crop_tool_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_TitleBarMaxButton)
        )
        self.crop_tool_btn.setCheckable(True)
        self.crop_tool_btn.setChecked(True)
        self.crop_tool_btn.setToolTip("Draw or adjust the crop box.")
        self.crop_tool_btn.clicked.connect(lambda: self.setActiveTool("crop"))
        self.tool_button_group.addButton(self.crop_tool_btn)
        toolbar.addWidget(self.crop_tool_btn)

        self.whiteout_btn = QPushButton("Visual Mask")
        self.whiteout_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DialogNoButton)
        )
        self.whiteout_btn.setCheckable(True)
        self.whiteout_btn.setToolTip(
            "Draw a visual overlay. It does not remove text or images from the PDF."
        )
        self.whiteout_btn.clicked.connect(lambda: self.setActiveTool("whiteout"))
        self.tool_button_group.addButton(self.whiteout_btn)
        toolbar.addWidget(self.whiteout_btn)

        self.redact_btn = QPushButton("Redact")
        self.redact_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning)
        )
        self.redact_btn.setCheckable(True)
        self.redact_btn.setToolTip(
            "Permanently remove page content in the selected rectangle. "
            "This does not remove document metadata or attachments."
        )
        self.redact_btn.clicked.connect(lambda: self.setActiveTool("redact"))
        self.tool_button_group.addButton(self.redact_btn)
        toolbar.addWidget(self.redact_btn)

        self.pick_color_btn = QPushButton("Whiteout Color")
        self.pick_color_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DialogResetButton)
        )
        self.pick_color_btn.setMinimumWidth(100)
        self.pick_color_btn.clicked.connect(self.pickColor)
        toolbar.addWidget(self.pick_color_btn)

        toolbar.addSeparator()

        self.crop_btn = QPushButton("Apply Crop")
        self.crop_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton))
        self.crop_btn.setMinimumWidth(100)
        self.crop_btn.clicked.connect(self.cropSelection)
        toolbar.addWidget(self.crop_btn)

        self.reset_crop_btn = QPushButton("Reset Crop")
        self.reset_crop_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload)
        )
        self.reset_crop_btn.setMinimumWidth(100)
        self.reset_crop_btn.clicked.connect(self.resetCrop)
        toolbar.addWidget(self.reset_crop_btn)

        toolbar.addSeparator()

        self.delete_btn = QPushButton("Delete Selected Pages")
        self.delete_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon))
        self.delete_btn.setMinimumWidth(150)
        self.delete_btn.clicked.connect(self.deleteSelectedPages)
        toolbar.addWidget(self.delete_btn)

        toolbar.addSeparator()

        # Add Undo button
        self.undo_btn = QPushButton("Undo")
        self.undo_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowBack))
        self.undo_btn.setMinimumWidth(80)
        self.undo_btn.clicked.connect(self.undo)
        self.undo_btn.setEnabled(False)
        toolbar.addWidget(self.undo_btn)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)

        self.save_btn = QPushButton("Save PDF")
        self.save_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton))
        self.save_btn.setObjectName("saveButton")
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

    def _refresh_dirty_state(self):
        """Derive whether the loaded document has unexported edits."""
        self.is_dirty = bool(
            self.active_crop_info
            or self.whiteout_operations
            or self.redaction_operations
            or self.page_map != list(range(self.original_page_count))
        )

    def _confirm_discard_changes(self) -> bool:
        if not self.is_dirty:
            return True
        response = QMessageBox.question(
            self,
            "Discard unsaved edits?",
            "The current edits have not been exported. Discard them?",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return response == QMessageBox.StandardButton.Discard

    def resetCrop(self):
        if not self.active_crop_info:
            QMessageBox.information(self, "Info", "No crop is currently active.")
            return

        self.pushUndo(include_pdf=False)
        affected_pages = tuple(self.active_crop_info.get("rects", {}))
        self.active_crop_info = None
        self.clearAllSelections()
        self._refresh_dirty_state()
        self.pending_status_message = "Crop reset."
        self.reloadImages(affected_pages)

    def setActiveTool(self, tool):
        if tool in {"whiteout", "redact"} and self.active_crop_info:
            QMessageBox.warning(
                self,
                "Reset Crop First",
                "Masks and redactions cannot be positioned reliably on an active cropped "
                "preview. Reset the crop, apply the operation, then crop again.",
            )
            tool = "crop"

        self.active_tool = tool if tool in {"crop", "whiteout", "redact"} else "crop"
        self.crop_tool_btn.setChecked(self.active_tool == "crop")
        self.whiteout_btn.setChecked(self.active_tool == "whiteout")
        self.redact_btn.setChecked(self.active_tool == "redact")
        view_tool = self.active_tool if self.active_tool in {"whiteout", "redact"} else "select"
        for view in [self.single_view, self.odd_view, self.even_view]:
            view.setTool(view_tool)

        if self.active_tool == "whiteout":
            self.statusBar().showMessage(
                "Visual Mask selected. The rectangle will apply to every page.",
                6000,
            )
        elif self.active_tool == "redact":
            self.statusBar().showMessage(
                "Redact selected. The rectangle will apply to every page.",
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
            view.setTool("pick_color")
        self.statusBar().showMessage("Click a page pixel to choose the whiteout color.", 6000)

    def onColorPicked(self, color):
        self.whiteout_color = (color.redF(), color.greenF(), color.blueF())
        self.statusBar().showMessage(
            f"Whiteout color set to RGB({int(color.red())}, {int(color.green())}, {int(color.blue())}).",
            6000,
        )
        self.setActiveTool("whiteout" if not self.active_crop_info else "crop")

    def pushUndo(self, include_pdf=True, pdf_bytes=None):
        if self.pdf_doc:
            self.undo_stack.append(
                {
                    "pdf_bytes": (pdf_bytes if pdf_bytes is not None else self.pdf_doc.tobytes())
                    if include_pdf
                    else None,
                    "page_map": list(self.page_map),
                    "whiteouts": [dict(operation) for operation in self.whiteout_operations],
                    "redactions": [dict(operation) for operation in self.redaction_operations],
                    "active_crop_info": clone_crop_info(self.active_crop_info),
                    "selected_pages": set(self.selected_pages),
                    "selection_anchor": self.selection_anchor,
                    "preview_page_num": self.preview_page_num,
                    "is_dirty": self.is_dirty,
                }
            )
            if len(self.undo_stack) > 10:  # Limit stack size
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
            self.redaction_operations = list(snapshot.get("redactions", []))
            self.active_crop_info = clone_crop_info(snapshot.get("active_crop_info"))
            self.selected_pages = set(snapshot.get("selected_pages", set()))
            self.selection_anchor = snapshot.get("selection_anchor")
            self.preview_page_num = snapshot.get("preview_page_num")
            self.is_dirty = bool(snapshot.get("is_dirty", False))
            self.pending_status_message = "Undo complete."
            self.reloadImages()
        except Exception as e:
            self.setUIProcessing(False)
            QMessageBox.critical(self, "Error", f"Failed to undo: {e!s}")

    @staticmethod
    def _selection_with_size(rect, existing_rect):
        if existing_rect:
            return QRectF(existing_rect.topLeft(), rect.size())
        return QRectF(rect)

    def sync_selection_from_single(self, rect):
        if self._is_syncing_selection:
            return

        self._is_syncing_selection = True
        try:
            if not rect or rect.isNull() or not rect.isValid():
                self.odd_view.clearSelection(notify=False)
                self.even_view.clearSelection(notify=False)
                return

            if self.preview_page_num is not None and self.view_mode == "odd_even":
                primary_view = self.odd_view if self.preview_page_num % 2 == 0 else self.even_view
                secondary_view = self.even_view if primary_view is self.odd_view else self.odd_view
                primary_view.setSelection(QRectF(rect), notify=False)
                secondary_view.setSelection(
                    self._selection_with_size(rect, secondary_view.getSelectionRect()),
                    notify=False,
                )
                return

            for view in (self.odd_view, self.even_view):
                view.setSelection(
                    self._selection_with_size(rect, view.getSelectionRect()),
                    notify=False,
                )
        finally:
            self._is_syncing_selection = False

    def sync_selection_to_even(self, rect):
        if self._is_syncing_selection:
            return

        self._is_syncing_selection = True
        try:
            if not rect or rect.isNull() or not rect.isValid():
                self.even_view.clearSelection(notify=False)
                self.single_view.clearSelection(notify=False)
                return
            self.even_view.setSelection(
                self._selection_with_size(rect, self.even_view.getSelectionRect()),
                notify=False,
            )
            self.single_view.setSelection(QRectF(rect), notify=False)
        finally:
            self._is_syncing_selection = False

    def sync_selection_to_odd(self, rect):
        if self._is_syncing_selection:
            return

        self._is_syncing_selection = True
        try:
            if not rect or rect.isNull() or not rect.isValid():
                self.odd_view.clearSelection(notify=False)
                self.single_view.clearSelection(notify=False)
                return
            self.odd_view.setSelection(
                self._selection_with_size(rect, self.odd_view.getSelectionRect()),
                notify=False,
            )
            self.single_view.setSelection(QRectF(rect), notify=False)
        finally:
            self._is_syncing_selection = False

    def setViewMode(self, mode):
        if self.view_mode == mode:
            return

        self.view_mode = mode
        if mode == "all":
            self.all_pages_action.setChecked(True)
        else:
            self.odd_even_action.setChecked(True)

        self.updateOverlay()

    def showHelp(self):
        help_text = """<b>Basic Usage:</b>
<ol>
<li>Open a PDF using <b>File > Open PDF...</b> or by dragging it into the window.</li>
<li>Choose <b>Crop Box</b>, <b>Visual Mask</b>, or <b>Redact</b> in the toolbar, then drag on the page view.</li>
<li>Click <b>Apply Crop</b> to preview a crop. You can restore it with <b>Reset Crop</b>.</li>
<li>Click a thumbnail to select one page. Use <b>Ctrl</b> to toggle pages and <b>Shift</b> to select a range.</li>
<li>Crop, masking, and redaction apply to all pages. Odd/even crop boxes keep separate positions for their page groups. Thumbnail selection is only used by <b>Delete Selected Pages</b>.</li>
<li>Use the <b>View</b> menu to switch between a single overlay of all pages or separate overlays for odd and even pages.</li>
<li><b>Visual Mask</b> only covers content. Use <b>Redact</b> to remove selected content; metadata and attachments are not removed.</li>
<li>Use <b>Undo</b> to restore the previous crop, mask, redaction, or page deletion.</li>
<li>Save your changes with the purple <b>Save PDF</b> button.</li>
</ol>"""
        QMessageBox.information(self, "About PyCropPDF", help_text)

    def togglePagePreview(self, page_num):
        if self.is_processing:
            return

        is_exiting_preview = self.preview_page_num == page_num
        target_selection = None
        if not is_exiting_preview and self.view_mode == "odd_even":
            target_view = self.odd_view if page_num % 2 == 0 else self.even_view
            current = target_view.getSelectionRect()
            target_selection = QRectF(current) if current else None

        if is_exiting_preview:
            self.preview_page_num = None
        else:
            self.preview_page_num = page_num

        self.updateOverlay()

        if self.preview_page_num is not None and self.view_mode == "odd_even":
            if target_selection:
                self.single_view.setSelection(target_selection, notify=False)
            else:
                self.single_view.clearSelection(notify=False)

        # Update thumbnail selection highlights
        for i in range(self.thumbnail_layout.count()):
            widget = self.thumbnail_layout.itemAt(i).widget()
            if isinstance(widget, ThumbnailWidget):
                widget.setSelectedForPreview(widget.page_num == self.preview_page_num)

    def clearAllSelections(self):
        self._is_syncing_selection = True
        try:
            self.single_view.clearSelection(notify=False)
            self.odd_view.clearSelection(notify=False)
            self.even_view.clearSelection(notify=False)
        finally:
            self._is_syncing_selection = False

    def openPDF(self):
        fileName, _ = QFileDialog.getOpenFileName(self, "Open PDF", "", "PDF Files (*.pdf)")
        if fileName:
            self.loadPDF(fileName)

    def loadPDF(self, pdf_path, confirm_discard=True):
        if self.is_processing:
            self.statusBar().showMessage(
                "Wait for the current operation to finish before opening another PDF.",
                6000,
            )
            return False

        new_document = None
        try:
            with open(pdf_path, "rb") as source_file:
                source_bytes = source_file.read()
            new_document = fitz.open("pdf", source_bytes)
            if new_document.needs_pass:
                raise ValueError("Password-protected PDFs are not supported yet.")
            if len(new_document) == 0:
                raise ValueError("The PDF contains no pages.")
        except Exception as error:
            if new_document is not None:
                new_document.close()
            QMessageBox.critical(self, "Error", f"Failed to load PDF: {error}")
            return False

        if confirm_discard and not self._confirm_discard_changes():
            new_document.close()
            return False

        old_document = self.pdf_doc
        self.pdf_doc = new_document
        self.pdf_path = os.path.abspath(pdf_path)
        self.original_pdf_path = self.pdf_path
        self.source_sha256 = sha256_bytes(source_bytes)
        self.active_crop_info = None
        self.preview_page_num = None
        self.pending_status_message = ""
        self.undo_stack = []
        self.page_map = list(range(len(self.pdf_doc)))
        self.original_page_count = len(self.pdf_doc)
        self.whiteout_operations = []
        self.redaction_operations = []
        self.selected_pages = set()
        self.selection_anchor = None
        self.is_dirty = False

        for view in [self.single_view, self.odd_view, self.even_view]:
            view.clearScene()

        if old_document is not None:
            old_document.close()
        self.setActiveTool("crop")
        self.reloadImages()
        return True

    def reloadImages(self, page_numbers=None):
        if self.pdf_doc is None or self.is_processing:
            return

        num_pages = len(self.pdf_doc)
        if num_pages == 0:
            self.updateThumbnails()
            self.updateOverlay()
            return

        requested_pages = (
            set(range(num_pages))
            if page_numbers is None
            else {int(page_num) for page_num in page_numbers}
        )
        if not requested_pages:
            return
        if any(page_num < 0 or page_num >= num_pages for page_num in requested_pages):
            raise ValueError("A requested preview page is outside the document.")

        full_render = (
            page_numbers is None
            or len(self.images) != num_pages
            or any(image is None for image in self.images)
        )
        if full_render:
            requested_pages = set(range(num_pages))
            self.images = [None] * num_pages

        self.setUIProcessing(True)
        self.invalidate_pixmap_cache()
        self._operation_id += 1
        operation_id = self._operation_id
        self.pages_rendered = 0
        self._render_target_pages = set(requested_pages)
        self._pending_render_pages = set(requested_pages)
        self._render_was_full = full_render
        target_count = len(requested_pages)
        self.statusBar().showMessage(f"Rendering page previews: 0/{target_count}...")

        pdf_bytes = self.pdf_doc.tobytes()
        worker = RenderAllPagesWorker(
            pdf_bytes,
            num_pages,
            self.active_crop_info,
            page_numbers=requested_pages,
        )
        worker.signals.result.connect(
            lambda result, operation_id=operation_id: self.pageRendered(operation_id, result)
        )
        worker.signals.error.connect(
            lambda error, operation_id=operation_id: self.renderingError(operation_id, error)
        )
        worker.signals.finished.connect(
            lambda operation_id=operation_id: self.processingFinished(operation_id)
        )
        self.threadpool.start(worker)

    def pageRendered(self, operation_id, result):
        if operation_id != self._operation_id or not self.is_processing:
            return
        page_num, image = result
        if page_num >= len(self.images) or page_num not in self._pending_render_pages:
            return
        self.images[page_num] = image
        self._pending_render_pages.remove(page_num)
        self.pages_rendered += 1
        self.statusBar().showMessage(
            f"Rendering page previews: {self.pages_rendered}/{len(self._render_target_pages)}..."
        )

        if self.pdf_doc is not None and not self._pending_render_pages:
            thumbnail_pages = None if self._render_was_full else self._render_target_pages
            self.updateThumbnails(thumbnail_pages)
            self.updateOverlay()

    @staticmethod
    def _selection_copy(view):
        selection = view.getSelectionRect()
        return QRectF(selection) if selection else None

    @staticmethod
    def _fit_view(view):
        scene_rect = view.sceneRect()
        if not scene_rect.isEmpty():
            view.fitInView(scene_rect, Qt.AspectRatioMode.KeepAspectRatio)

    def _set_view_pixmap(self, view, pixmap, selection=None):
        view.clearScene()
        if pixmap is None or pixmap.isNull():
            return
        view.scene.addPixmap(pixmap)
        view.setSceneRect(QRectF(pixmap.rect()))
        self._fit_view(view)
        if selection:
            view.setSelection(selection, notify=False)

    def showSinglePagePreview(self, page_num):
        self.single_view.show()
        self.odd_view.hide()
        self.even_view.hide()

        if 0 <= page_num < len(self.images):
            image = self.images[page_num]
            if image is None or image.isNull():
                return
            selection = self._selection_copy(self.single_view)
            pixmap = QPixmap.fromImage(image)
            self._set_view_pixmap(self.single_view, pixmap, selection)

    def updateOverlay(self):
        if not self.images:
            return

        if self.preview_page_num is not None:
            self.showSinglePagePreview(self.preview_page_num)
            return

        if self.view_mode == "all":
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

        selection = self._selection_copy(self.single_view)

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

        self._set_view_pixmap(self.single_view, self.single_view_pixmap_cache, selection)

    def updateSplitViewOverlay(self):
        if not self.images:
            return

        odd_selection = self._selection_copy(self.odd_view)
        even_selection = self._selection_copy(self.even_view)

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

            self._set_view_pixmap(self.odd_view, self.odd_view_pixmap_cache, odd_selection)
        else:
            self.odd_view.clearScene()

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

            self._set_view_pixmap(self.even_view, self.even_view_pixmap_cache, even_selection)
        else:
            self.even_view.clearScene()

    def updateThumbnails(self, page_numbers=None):
        if page_numbers is not None:
            thumbnails = {widget.page_num: widget for widget in self._thumbnail_widgets()}
            if len(thumbnails) == len(self.images):
                for page_num in sorted({int(page_num) for page_num in page_numbers}):
                    thumbnail = thumbnails.get(page_num)
                    if thumbnail is not None:
                        thumbnail.setImage(self.images[page_num])
                        thumbnail.setSelectedForDeletion(page_num in self.selected_pages)
                        thumbnail.setSelectedForPreview(page_num == self.preview_page_num)
                return

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
        self.updateActionState()
        count = len(self.selected_pages)
        self.statusBar().showMessage(
            f"{count} page{'s' if count != 1 else ''} selected. "
            "Use Ctrl to toggle pages or Shift to select a range.",
            6000,
        )

    def dragEnterEvent(self, event):
        if self.is_processing:
            event.ignore()
            return
        try:
            mime_data = event.mimeData()
            if mime_data.hasUrls():
                for url in mime_data.urls():
                    file_path = url.toLocalFile()
                    if file_path.lower().endswith(".pdf"):
                        event.accept()
                        return
            event.ignore()
        except Exception:
            event.ignore()

    def dragMoveEvent(self, event):
        if self.is_processing:
            event.ignore()
            return
        try:
            mime_data = event.mimeData()
            if mime_data.hasUrls():
                for url in mime_data.urls():
                    if url.toLocalFile().lower().endswith(".pdf"):
                        event.accept()
                        return
            event.ignore()
        except Exception:
            event.ignore()

    def dropEvent(self, event):
        if self.is_processing:
            event.ignore()
            self.statusBar().showMessage(
                "Wait for the current operation to finish before opening another PDF.",
                6000,
            )
            return
        try:
            mime_data = event.mimeData()
            if mime_data.hasUrls():
                for url in mime_data.urls():
                    file_path = url.toLocalFile()
                    if file_path.lower().endswith(".pdf"):
                        if self.loadPDF(file_path):
                            event.accept()
                        else:
                            event.ignore()
                        return
            event.ignore()
        except Exception:
            event.ignore()

    def _current_image_dimensions(self):
        return [(image.width(), image.height()) if image else (0, 0) for image in self.images]

    def _canvas_dimensions(self, page_num):
        image_dims = self._current_image_dimensions()
        if self.preview_page_num is not None:
            return image_dims[page_num]
        valid_dims = [(width, height) for width, height in image_dims if width and height]
        if not valid_dims:
            raise ValueError("No page previews are available.")
        return max(width for width, _ in valid_dims), max(height for _, height in valid_dims)

    def _visible_pdf_rect_for_page(self, page_num):
        if self.active_crop_info:
            crop_rect = self.active_crop_info.get("rects", {}).get(page_num)
            if crop_rect:
                return crop_rect
        return self.pdf_doc[page_num].cropbox

    def _scene_rect_to_pdf_rect(self, scene_rect, page_num):
        image_dims = self._current_image_dimensions()
        return scene_rect_to_pdf_coords(
            scene_rect,
            image_dims[page_num],
            self._canvas_dimensions(page_num),
            self.pdf_doc[page_num],
            self._visible_pdf_rect_for_page(page_num),
        )

    def cropSelection(self):
        if self.is_processing or self.pdf_doc is None:
            return

        scene_rects_by_page = {}
        if self.preview_page_num is not None or self.view_mode == "all":
            scene_rect = self.single_view.getSelectionRect()
            if not scene_rect:
                QMessageBox.warning(self, "Warning", "Please draw a crop box first.")
                return
            for page_num in range(len(self.pdf_doc)):
                scene_rects_by_page[page_num] = scene_rect
        else:
            odd_rect = self.odd_view.getSelectionRect()
            even_rect = self.even_view.getSelectionRect()
            if not (odd_rect or even_rect):
                QMessageBox.warning(self, "Warning", "Please draw at least one crop box.")
                return
            fallback_rect = odd_rect or even_rect
            odd_rect = odd_rect or fallback_rect
            even_rect = even_rect or fallback_rect
            if odd_rect:
                for i in range(0, len(self.pdf_doc), 2):
                    scene_rects_by_page[i] = odd_rect
            if even_rect:
                for i in range(1, len(self.pdf_doc), 2):
                    scene_rects_by_page[i] = even_rect

        if not scene_rects_by_page:
            QMessageBox.warning(
                self,
                "Warning",
                "No pages in the current view have a crop box.",
            )
            return

        try:
            crop_rects = {
                int(page_num): rect_to_tuple(self._scene_rect_to_pdf_rect(scene_rect, page_num))
                for page_num, scene_rect in scene_rects_by_page.items()
            }
        except (IndexError, ValueError) as error:
            QMessageBox.warning(self, "Invalid Crop", f"{error!s}")
            return

        self.pushUndo(include_pdf=False)
        retained_rects = (
            dict(self.active_crop_info.get("rects", {})) if self.active_crop_info else {}
        )
        retained_rects.update(crop_rects)
        self.active_crop_info = {
            "rects": retained_rects,
            "view_mode": self.view_mode,
            "image_dims": (
                list(self.active_crop_info.get("image_dims", []))
                if self.active_crop_info
                else self._current_image_dimensions()
            ),
        }
        self._refresh_dirty_state()

        self.pending_status_message = (
            f"Crop preview applied to {len(crop_rects)} page{'s' if len(crop_rects) != 1 else ''}."
        )
        self.clearAllSelections()
        self.reloadImages(crop_rects)

    def _target_pages_for_rectangle_request(self):
        return list(range(len(self.pdf_doc)))

    def handleWhiteoutRequest(self, rect):
        self._handle_rectangle_request(rect, secure_redaction=False)

    def handleRedactionRequest(self, rect):
        self._handle_rectangle_request(rect, secure_redaction=True)

    def _handle_rectangle_request(self, rect, secure_redaction):
        if self.is_processing or self.pdf_doc is None:
            return

        if self.active_crop_info:
            QMessageBox.warning(
                self,
                "Reset Crop First",
                "Masks and redactions cannot be positioned reliably on an active cropped preview. "
                "Please Reset Crop first.",
            )
            return

        target_pages = self._target_pages_for_rectangle_request()
        if not target_pages:
            return

        if secure_redaction:
            self.applyRedaction(rect, target_pages)
        else:
            self.applyWhiteout(rect, target_pages)

    def applyWhiteout(self, rect, target_pages):
        """Compatibility entry point for tests and integrations using visual masking."""
        self._apply_rectangle_operation(rect, target_pages, secure_redaction=False)

    def applyRedaction(self, rect, target_pages):
        """Apply content-removing redactions to the requested pages."""
        self._apply_rectangle_operation(rect, target_pages, secure_redaction=True)

    def _apply_rectangle_operation(self, rect, target_pages, secure_redaction):
        target_pages = tuple(sorted({int(page_num) for page_num in target_pages}))
        if not target_pages:
            return
        try:
            pdf_rects = {
                page_num: self._scene_rect_to_pdf_rect(rect, page_num) for page_num in target_pages
            }
        except (IndexError, ValueError) as error:
            QMessageBox.warning(self, "Invalid Selection", f"{error!s}")
            return

        before_pdf_bytes = self.pdf_doc.tobytes()
        before_whiteouts = [dict(operation) for operation in self.whiteout_operations]
        before_redactions = [dict(operation) for operation in self.redaction_operations]
        before_dirty = self.is_dirty
        operation_name = "redaction" if secure_redaction else "visual mask"
        try:
            self.statusBar().showMessage(
                f"Applying {operation_name} to {len(target_pages)} page"
                f"{'s' if len(target_pages) != 1 else ''}..."
            )
            self.pushUndo(pdf_bytes=before_pdf_bytes)

            for page_num in target_pages:
                page = self.pdf_doc[page_num]
                pdf_rect = pdf_rects[page_num]
                original_page = (
                    self.page_map[page_num] if page_num < len(self.page_map) else page_num
                )
                operation = {
                    "output_page": page_num + 1,
                    "original_page": original_page + 1,
                    "rect": [round(float(value), 3) for value in pdf_rect],
                }
                if secure_redaction:
                    page.add_redact_annot(pdf_rect, fill=(0, 0, 0), cross_out=False)
                    page.apply_redactions(
                        images=fitz.PDF_REDACT_IMAGE_PIXELS,
                        graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
                        text=fitz.PDF_REDACT_TEXT_REMOVE,
                    )
                    self.redaction_operations.append(operation)
                else:
                    page.draw_rect(
                        pdf_rect,
                        color=self.whiteout_color,
                        fill=self.whiteout_color,
                        width=0,
                    )
                    operation["color"] = [round(float(value), 4) for value in self.whiteout_color]
                    self.whiteout_operations.append(operation)

            self.pending_status_message = (
                f"{operation_name.capitalize()} applied to {len(target_pages)} page"
                f"{'s' if len(target_pages) != 1 else ''}."
            )
            self._refresh_dirty_state()
            self.reloadImages(target_pages)
        except Exception as error:
            self.pdf_doc.close()
            self.pdf_doc = fitz.open("pdf", before_pdf_bytes)
            self.whiteout_operations = before_whiteouts
            self.redaction_operations = before_redactions
            self.is_dirty = before_dirty
            if self.undo_stack:
                self.undo_stack.pop()
            QMessageBox.critical(self, "Error", f"Failed to apply {operation_name}: {error!s}")

    def deleteSelectedPages(self):
        if self.pdf_doc is None:
            QMessageBox.warning(self, "Warning", "No PDF is open.")
            return

        selected_pages = sorted(self.getSelectedPages(), reverse=True)
        if not selected_pages:
            QMessageBox.warning(self, "Warning", "Please select pages to delete.")
            return
        if len(selected_pages) >= len(self.pdf_doc):
            QMessageBox.warning(self, "Warning", "A PDF must retain at least one page.")
            return

        try:
            self.pushUndo()  # Save state before modification
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
            self._refresh_dirty_state()
            self.updateThumbnails()
            self.updateOverlay()
            self.updateActionState()
            self.statusBar().showMessage(
                f"Deleted {len(selected_pages)} page{'s' if len(selected_pages) != 1 else ''}.",
                6000,
            )
        except Exception as error:
            QMessageBox.critical(self, "Error", f"Failed to delete pages: {error!s}")

    def savePDF(self):
        if self.is_processing or self.pdf_doc is None:
            return
        if len(self.pdf_doc) == 0:
            QMessageBox.warning(self, "Cannot Save", "A PDF must contain at least one page.")
            return

        if self.save_directory:
            if self.save_filename:
                save_path = os.path.join(self.save_directory, self.save_filename)
            else:
                original_name = os.path.basename(self.original_pdf_path or self.pdf_path)
                base_name = os.path.splitext(original_name)[0]
                save_path = os.path.join(self.save_directory, f"{base_name}_modified.pdf")
        else:
            save_path, _ = QFileDialog.getSaveFileName(self, "Save PDF", "", "PDF Files (*.pdf)")

        if not save_path:
            return
        if not save_path.lower().endswith(".pdf"):
            save_path = f"{save_path}.pdf"

        try:
            pdf_bytes = self.pdf_doc.tobytes()
        except Exception as error:
            QMessageBox.critical(self, "Error", f"Failed to prepare PDF for saving: {error}")
            return

        self.setUIProcessing(True)
        self._operation_id += 1
        operation_id = self._operation_id
        self.statusBar().showMessage("Saving PDF and provenance manifest...")
        deflate_enabled = not self.fast_save_action.isChecked()
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
            redactions=self.redaction_operations,
            source_sha256=self.source_sha256,
        )
        worker.signals.result.connect(
            lambda result, operation_id=operation_id: self.saveFinished(operation_id, result)
        )
        worker.signals.error.connect(
            lambda error, operation_id=operation_id: self.processingError(operation_id, error)
        )
        worker.signals.finished.connect(
            lambda operation_id=operation_id: self.processingFinished(operation_id)
        )
        self.threadpool.start(worker)

    def updateActionState(self):
        has_document = self.pdf_doc is not None and len(self.pdf_doc) > 0
        available = has_document and not self.is_processing
        self.crop_tool_btn.setEnabled(available)
        self.whiteout_btn.setEnabled(available and not self.active_crop_info)
        self.redact_btn.setEnabled(available and not self.active_crop_info)
        self.crop_btn.setEnabled(available and self.active_tool == "crop")
        self.reset_crop_btn.setEnabled(available and bool(self.active_crop_info))
        self.pick_color_btn.setEnabled(available and not self.active_crop_info)
        self.delete_btn.setEnabled(available and bool(self.selected_pages))
        self.undo_btn.setEnabled(available and bool(self.undo_stack))
        self.undo_action.setEnabled(available and bool(self.undo_stack))
        self.save_btn.setEnabled(available)
        self.save_action.setEnabled(available)

    def setUIProcessing(self, is_processing):
        was_processing = self.is_processing
        self.is_processing = is_processing
        self.menuBar().setEnabled(not is_processing)
        self.updateActionState()

        if is_processing and not was_processing:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        elif not is_processing and was_processing:
            QApplication.restoreOverrideCursor()

    def saveFinished(self, operation_id, result):
        if operation_id != self._operation_id:
            return
        if result.get("manifest_error"):
            self.statusBar().showMessage(
                "PDF saved, but the provenance manifest could not be finalized.", 10000
            )
            QMessageBox.warning(
                self,
                "Manifest Not Saved",
                f"The PDF was saved to:\n{result['pdf_path']}\n\n"
                f"The manifest could not be finalized:\n{result['manifest_error']}",
            )
        else:
            self.is_dirty = False
        if result.get("manifest_written"):
            self.statusBar().showMessage("PDF and provenance manifest saved successfully.", 8000)
        elif not result.get("manifest_error"):
            self.statusBar().showMessage("PDF saved successfully.", 8000)

    def processingError(self, operation_id, error_str):
        if operation_id != self._operation_id:
            return
        self.pending_status_message = ""
        self.statusBar().showMessage("The operation failed.", 8000)
        QMessageBox.critical(self, "Error", f"An error occurred:\n{error_str}")
        LOGGER.error("Background operation failed:\n%s", error_str)

    def renderingError(self, operation_id, error_str):
        if operation_id != self._operation_id:
            return
        self.pending_status_message = ""
        self.statusBar().showMessage(
            "Preview rendering failed; document edits remain applied.", 8000
        )
        QMessageBox.critical(
            self,
            "Preview Error",
            "One or more page previews could not be refreshed. "
            "The document remains loaded and its edits remain applied.",
        )
        LOGGER.error("Preview rendering failed:\n%s", error_str)

    def processingFinished(self, operation_id):
        if operation_id != self._operation_id:
            return
        self.setUIProcessing(False)
        if self.pending_status_message:
            self.statusBar().showMessage(self.pending_status_message, 8000)
            self.pending_status_message = ""

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.pdf_doc is not None and not self.is_processing:
            if self.preview_page_num is not None or self.view_mode == "all":
                self._fit_view(self.single_view)
            else:
                self._fit_view(self.odd_view)
                self._fit_view(self.even_view)

    def closeEvent(self, event):
        if self.is_processing:
            QMessageBox.information(
                self,
                "Operation in Progress",
                "Wait for the current operation to finish before closing.",
            )
            event.ignore()
            return
        if self.isVisible() and not self._confirm_discard_changes():
            event.ignore()
            return
        if self.pdf_doc is not None:
            self.pdf_doc.close()
        super().closeEvent(event)
