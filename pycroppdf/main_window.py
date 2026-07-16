import logging
import os
from contextlib import suppress

import fitz
from PyQt6.QtCore import QEvent, QRectF, QSignalBlocker, Qt, QThreadPool
from PyQt6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QIcon,
    QImage,
    QKeySequence,
    QPainter,
    QPixmap,
    QTransform,
)
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .icons import icon_size, vector_icon
from .provenance import sha256_bytes
from .rotation import (
    deskew_available,
    normalize_angle,
    pages_with_interactive_objects,
    recommended_deskew_workers,
)
from .state import (
    clone_crop_info,
    remap_crop_info_after_deletions,
    remap_page_indices_after_deletions,
    remap_page_mapping_after_deletions,
)
from .widgets import PageGraphicsView, ThumbnailWidget
from .workers import (
    AutoDeskewWorker,
    RenderAllPagesWorker,
    RotatePagesWorker,
    SaveWorker,
    pdf_rect_to_scene_coords,
    rect_to_tuple,
    scene_rect_to_pdf_coords,
)

LOGGER = logging.getLogger(__name__)
TOOLBAR_CONTROL_HEIGHT = 32


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
        self.cover_operations = []
        self.rotation_operations = []
        self.cover_color = (1, 1, 1)
        self.page_crop_overrides = {}
        self.selected_pages = set()
        self.selection_anchor = None
        self._selection_before_preview = None
        self._reload_after_processing = False
        self._rotation_undo_pushed = False
        self._rotation_preview_angle = 0.0

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

        self.single_view.coverRequested.connect(self.handleCoverRequest)
        self.odd_view.coverRequested.connect(self.handleCoverRequest)
        self.even_view.coverRequested.connect(self.handleCoverRequest)

        QApplication.instance().installEventFilter(self)

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
            QPushButton, QToolButton {
                background-color: #555555;
                color: #f0f0f0;
                border: 1px solid #666666;
                padding: 5px 7px;
                border-radius: 4px;
                font-size: 11px;
            }
            QPushButton:hover, QToolButton:hover {
                background-color: #6a6a6a;
            }
            QPushButton:pressed, QToolButton:pressed {
                background-color: #7a7a7a;
            }
            QPushButton:disabled, QToolButton:disabled {
                background-color: #444444;
                color: #888888;
                border-color: #555555;
            }
            QPushButton:checked, QToolButton:checked {
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
            QComboBox, QDoubleSpinBox {
                background-color: #454545;
                color: #f0f0f0;
                border: 1px solid #666666;
                border-radius: 3px;
                padding: 4px 6px;
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
        self.open_action.setIcon(vector_icon("open"))
        self.open_action.triggered.connect(self.openPDF)
        file_menu.addAction(self.open_action)

        self.save_action = QAction("&Save PDF...", self)
        self.save_action.setIcon(vector_icon("save"))
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
        self.undo_action.setIcon(vector_icon("undo"))
        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.undo_action.triggered.connect(self.undo)
        edit_menu.addAction(self.undo_action)

        # View menu
        view_menu = menu_bar.addMenu("&View")
        self.view_mode_group = QActionGroup(self)
        self.view_mode_group.setExclusive(True)

        self.odd_even_action = QAction("Separate Odd/Even Pages", self)
        self.odd_even_action.setIcon(vector_icon("odd-even"))
        self.odd_even_action.setCheckable(True)
        self.odd_even_action.setChecked(self.view_mode == "odd_even")
        self.odd_even_action.triggered.connect(lambda: self.setViewMode("odd_even"))
        self.view_mode_group.addAction(self.odd_even_action)
        view_menu.addAction(self.odd_even_action)

        self.all_pages_action = QAction("All Pages Overlay", self)
        self.all_pages_action.setIcon(vector_icon("stack"))
        self.all_pages_action.setCheckable(True)
        self.all_pages_action.setChecked(self.view_mode == "all")
        self.all_pages_action.triggered.connect(lambda: self.setViewMode("all"))
        self.view_mode_group.addAction(self.all_pages_action)
        view_menu.addAction(self.all_pages_action)

        # Help menu
        help_menu = menu_bar.addMenu("&Help")
        about_action = QAction("&About", self)
        about_action.setIcon(vector_icon("info"))
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
        toolbar.setIconSize(icon_size())
        self.main_toolbar = toolbar

        self.tool_button_group = QButtonGroup(self)
        self.tool_button_group.setExclusive(True)

        self.crop_tool_btn = QPushButton("Crop")
        self.crop_tool_btn.setIcon(vector_icon("crop"))
        self.crop_tool_btn.setCheckable(True)
        self.crop_tool_btn.setChecked(True)
        self.crop_tool_btn.setToolTip("Draw or adjust the crop box.")
        self.crop_tool_btn.clicked.connect(lambda: self.setActiveTool("crop"))
        self.tool_button_group.addButton(self.crop_tool_btn)
        toolbar.addWidget(self.crop_tool_btn)

        self.cover_tool_btn = QPushButton("Cover")
        self.cover_tool_btn.setIcon(vector_icon("cover"))
        self.cover_tool_btn.setCheckable(True)
        self.cover_tool_btn.setToolTip(
            "Draw a colored visual cover. The underlying PDF content remains."
        )
        self.cover_tool_btn.clicked.connect(lambda: self.setActiveTool("cover"))
        self.tool_button_group.addButton(self.cover_tool_btn)
        toolbar.addWidget(self.cover_tool_btn)

        toolbar.addSeparator()

        self.rotation_options_toggle_btn = QPushButton("Rotate")
        self.rotation_options_toggle_btn.setIcon(vector_icon("rotate"))
        self.rotation_options_toggle_btn.setCheckable(True)
        self.rotation_options_toggle_btn.setToolTip("Show rotation and deskew controls.")
        toolbar.addWidget(self.rotation_options_toggle_btn)

        self.view_stack_btn = QPushButton("Stack")
        self.view_stack_btn.setIcon(vector_icon("stack"))
        self.view_stack_btn.setToolTip("Leave the page preview and return to the stack view.")
        self.view_stack_btn.clicked.connect(self.showStackView)
        toolbar.addWidget(self.view_stack_btn)

        toolbar.addSeparator()

        self.delete_btn = QPushButton("Delete")
        self.delete_btn.setIcon(vector_icon("delete"))
        self.delete_btn.setToolTip("Delete the pages selected with thumbnail checkboxes.")
        self.delete_btn.clicked.connect(self.deleteSelectedPages)
        toolbar.addWidget(self.delete_btn)

        toolbar.addSeparator()

        # Add Undo button
        self.undo_btn = QPushButton("Undo")
        self.undo_btn.setIcon(vector_icon("undo"))
        self.undo_btn.clicked.connect(self.undo)
        self.undo_btn.setEnabled(False)
        toolbar.addWidget(self.undo_btn)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)

        self.save_btn = QPushButton("Save")
        self.save_btn.setIcon(vector_icon("save"))
        self.save_btn.setObjectName("saveButton")
        self.save_btn.setToolTip("Save the edited PDF and provenance manifest.")
        self.save_btn.clicked.connect(self.savePDF)
        toolbar.addWidget(self.save_btn)

        main_layout.addWidget(toolbar)

        crop_toolbar = QToolBar()
        crop_toolbar.setMovable(False)
        crop_toolbar.setFloatable(False)
        crop_toolbar.setIconSize(icon_size())
        self.crop_toolbar = crop_toolbar

        self.crop_btn = QPushButton("Apply")
        self.crop_btn.setIcon(vector_icon("check"))
        self.crop_btn.setToolTip("Apply the stack crop and any page overrides to all pages.")
        self.crop_btn.clicked.connect(self.cropSelection)
        crop_toolbar.addWidget(self.crop_btn)

        self.reset_crop_btn = QPushButton("Reset")
        self.reset_crop_btn.setIcon(vector_icon("reset"))
        self.reset_crop_btn.setToolTip("Reset the active crop preview.")
        self.reset_crop_btn.clicked.connect(self.resetCrop)
        crop_toolbar.addWidget(self.reset_crop_btn)

        crop_toolbar.addSeparator()

        self.crop_page_override_checkbox = QCheckBox("This page")
        self.crop_page_override_checkbox.setChecked(False)
        self.crop_page_override_checkbox.setToolTip(
            "In a page preview, give that page its own crop size and position."
        )
        self.crop_page_override_checkbox.toggled.connect(self.onCropPageOverrideToggled)
        crop_toolbar.addWidget(self.crop_page_override_checkbox)
        main_layout.addWidget(crop_toolbar)
        crop_toolbar.show()

        cover_toolbar = QToolBar()
        cover_toolbar.setMovable(False)
        cover_toolbar.setFloatable(False)
        cover_toolbar.setIconSize(icon_size())
        self.cover_toolbar = cover_toolbar

        self.cover_color_btn = QPushButton("Choose color")
        self.cover_color_btn.setToolTip("Choose the visual cover color.")
        self.cover_color_btn.clicked.connect(self.chooseCoverColor)
        cover_toolbar.addWidget(self.cover_color_btn)

        self.pick_cover_color_btn = QPushButton("Pick color")
        self.pick_cover_color_btn.setIcon(vector_icon("eyedropper"))
        self.pick_cover_color_btn.setToolTip("Pick the visual cover color from a page pixel.")
        self.pick_cover_color_btn.clicked.connect(self.pickCoverColor)
        cover_toolbar.addWidget(self.pick_cover_color_btn)

        cover_toolbar.addSeparator()
        self.cover_note_label = QLabel("Visual cover only — underlying content remains")
        self.cover_note_label.setToolTip(
            "Cover draws over content but does not remove it from the PDF."
        )
        cover_toolbar.addWidget(self.cover_note_label)
        self._updateCoverColorIcon()
        main_layout.addWidget(cover_toolbar)
        cover_toolbar.hide()

        rotation_toolbar = QToolBar()
        rotation_toolbar.setMovable(False)
        rotation_toolbar.setFloatable(False)
        rotation_toolbar.setIconSize(icon_size())
        self.rotation_toolbar = rotation_toolbar
        self.rotation_options_toggle_btn.toggled.connect(rotation_toolbar.setVisible)

        self.rotation_page_override_checkbox = QCheckBox("This page")
        self.rotation_page_override_checkbox.setChecked(False)
        self.rotation_page_override_checkbox.setToolTip(
            "In a page preview, apply rotation or deskew only to that page."
        )
        self.rotation_page_override_checkbox.toggled.connect(self.rotationPreviewInputsChanged)
        rotation_toolbar.addWidget(self.rotation_page_override_checkbox)
        rotation_toolbar.addSeparator()

        self.rotation_scope_combo = QComboBox()
        self.rotation_scope_combo.addItem("All", "all")
        self.rotation_scope_combo.addItem("Odd", "odd")
        self.rotation_scope_combo.addItem("Even", "even")
        self.rotation_scope_combo.setToolTip(
            "Choose the page stack to rotate, or enable This page in a page preview."
        )
        self.rotation_scope_combo.currentIndexChanged.connect(self.rotationPreviewInputsChanged)
        rotation_toolbar.addWidget(self.rotation_scope_combo)

        self.rotate_left_btn = QPushButton("90° L")
        self.rotate_left_btn.setIcon(vector_icon("rotate-left"))
        self.rotate_left_btn.setToolTip("Rotate the selected page stack 90° counter-clockwise.")
        self.rotate_left_btn.clicked.connect(lambda: self.applyRotation(-90.0))
        rotation_toolbar.addWidget(self.rotate_left_btn)

        self.rotate_right_btn = QPushButton("90° R")
        self.rotate_right_btn.setIcon(vector_icon("rotate-right"))
        self.rotate_right_btn.setToolTip("Rotate the selected page stack 90° clockwise.")
        self.rotate_right_btn.clicked.connect(lambda: self.applyRotation(90.0))
        rotation_toolbar.addWidget(self.rotate_right_btn)

        self.rotation_angle_spin = QDoubleSpinBox()
        self.rotation_angle_spin.setRange(-45.0, 45.0)
        self.rotation_angle_spin.setDecimals(1)
        self.rotation_angle_spin.setSingleStep(0.1)
        self.rotation_angle_spin.setSuffix("°")
        self.rotation_angle_spin.setToolTip(
            "Fine rotation to apply. Positive values rotate clockwise."
        )
        self.rotation_angle_spin.valueChanged.connect(self.rotationPreviewInputsChanged)
        rotation_toolbar.addWidget(self.rotation_angle_spin)

        self.preview_rotation_btn = QPushButton("Preview")
        self.preview_rotation_btn.setIcon(vector_icon("preview"))
        self.preview_rotation_btn.setToolTip("Preview the entered angle without changing the PDF.")
        self.preview_rotation_btn.clicked.connect(self.previewRotation)
        rotation_toolbar.addWidget(self.preview_rotation_btn)

        self.discard_rotation_preview_btn = QPushButton("Discard")
        self.discard_rotation_preview_btn.setIcon(vector_icon("discard"))
        self.discard_rotation_preview_btn.setToolTip("Clear the angle and discard its preview.")
        self.discard_rotation_preview_btn.clicked.connect(self.discardRotationPreview)
        rotation_toolbar.addWidget(self.discard_rotation_preview_btn)

        self.apply_rotation_btn = QPushButton("Apply")
        self.apply_rotation_btn.setIcon(vector_icon("check"))
        self.apply_rotation_btn.setToolTip("Apply the fine rotation angle to the target pages.")
        self.apply_rotation_btn.clicked.connect(
            lambda: self.applyRotation(self.rotation_angle_spin.value())
        )
        rotation_toolbar.addWidget(self.apply_rotation_btn)

        self.auto_deskew_btn = QPushButton("Auto deskew")
        self.auto_deskew_btn.setIcon(vector_icon("deskew"))
        self.auto_deskew_btn.clicked.connect(self.autoDeskew)
        if deskew_available():
            self.auto_deskew_btn.setToolTip(
                "Detect and correct a small skew angle independently on each target page."
            )
        else:
            self.auto_deskew_btn.setToolTip(
                "Install pycroppdf[deskew] to enable automatic deskew detection."
            )
        rotation_toolbar.addWidget(self.auto_deskew_btn)
        main_layout.addWidget(rotation_toolbar)
        rotation_toolbar.hide()

        for control in (
            self.crop_tool_btn,
            self.cover_tool_btn,
            self.rotation_options_toggle_btn,
            self.view_stack_btn,
            self.delete_btn,
            self.undo_btn,
            self.save_btn,
            self.crop_btn,
            self.reset_crop_btn,
            self.crop_page_override_checkbox,
            self.cover_color_btn,
            self.pick_cover_color_btn,
            self.cover_note_label,
            self.rotation_page_override_checkbox,
            self.rotation_scope_combo,
            self.rotate_left_btn,
            self.rotate_right_btn,
            self.rotation_angle_spin,
            self.preview_rotation_btn,
            self.discard_rotation_preview_btn,
            self.apply_rotation_btn,
            self.auto_deskew_btn,
        ):
            control.setFixedHeight(TOOLBAR_CONTROL_HEIGHT)

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
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.thumbnail_widget = QWidget()
        self.thumbnail_layout = QGridLayout(self.thumbnail_widget)
        self.thumbnail_layout.setContentsMargins(0, 0, 0, 0)
        self.thumbnail_layout.setSpacing(4)
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
            or self.rotation_operations
            or self.cover_operations
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
        if tool == "cover" and self.active_crop_info:
            QMessageBox.warning(
                self,
                "Reset Crop First",
                "A cover cannot be positioned reliably on an active cropped preview. "
                "Reset the crop, apply the cover, then crop again.",
            )
            tool = "crop"

        self.active_tool = tool if tool in {"crop", "cover"} else "crop"
        self.crop_tool_btn.setChecked(self.active_tool == "crop")
        self.cover_tool_btn.setChecked(self.active_tool == "cover")
        self.crop_toolbar.setVisible(self.active_tool == "crop")
        self.cover_toolbar.setVisible(self.active_tool == "cover")
        view_tool = "cover" if self.active_tool == "cover" else "select"
        for view in [self.single_view, self.odd_view, self.even_view]:
            view.setTool(view_tool)

        if self.active_tool == "cover":
            self.statusBar().showMessage(
                "Cover selected. It draws over every page; underlying PDF content remains.",
                6000,
            )
        else:
            self.statusBar().showMessage(
                "Crop Box tool selected. Draw a box, then click Apply Crop.",
                6000,
            )
        self.updateActionState()

    def _updateCoverColorIcon(self):
        color = QColor.fromRgbF(*self.cover_color)
        swatch = QPixmap(18, 18)
        swatch.fill(color)
        self.cover_color_btn.setIcon(QIcon(swatch))
        self.cover_color_btn.setToolTip(
            f"Choose the visual cover color. Current color: {color.name().upper()}."
        )

    def chooseCoverColor(self):
        current = QColor.fromRgbF(*self.cover_color)
        color = QColorDialog.getColor(current, self, "Choose cover color")
        if color.isValid():
            self.onColorPicked(color)

    def pickCoverColor(self):
        for view in [self.single_view, self.odd_view, self.even_view]:
            view.setTool("pick_color")
        self.statusBar().showMessage("Click a page pixel to choose the cover color.", 6000)

    def onColorPicked(self, color):
        self.cover_color = (color.redF(), color.greenF(), color.blueF())
        self._updateCoverColorIcon()
        self.statusBar().showMessage(
            f"Cover color set to RGB({color.red()}, {color.green()}, {color.blue()}).",
            6000,
        )
        self.setActiveTool("cover" if not self.active_crop_info else "crop")

    def eventFilter(self, watched, event):
        if (
            event.type() in {QEvent.Type.KeyPress, QEvent.Type.KeyRelease}
            and event.key() == Qt.Key.Key_Space
        ):
            if not event.isAutoRepeat():
                active = event.type() == QEvent.Type.KeyPress
                for view in (self.single_view, self.odd_view, self.even_view):
                    view.setPanActive(active)
            return True
        return super().eventFilter(watched, event)

    def pushUndo(self, include_pdf=True, pdf_bytes=None):
        if self.pdf_doc:
            self.undo_stack.append(
                {
                    "pdf_bytes": (pdf_bytes if pdf_bytes is not None else self.pdf_doc.tobytes())
                    if include_pdf
                    else None,
                    "page_map": list(self.page_map),
                    "covers": [dict(operation) for operation in self.cover_operations],
                    "rotations": [dict(operation) for operation in self.rotation_operations],
                    "active_crop_info": clone_crop_info(self.active_crop_info),
                    "selected_pages": set(self.selected_pages),
                    "selection_anchor": self.selection_anchor,
                    "preview_page_num": self.preview_page_num,
                    "page_crop_overrides": dict(self.page_crop_overrides),
                    "crop_selections": {
                        "single": self._selection_copy(self.single_view),
                        "odd": self._selection_copy(self.odd_view),
                        "even": self._selection_copy(self.even_view),
                        "before_preview": (
                            QRectF(self._selection_before_preview)
                            if self._selection_before_preview
                            else None
                        ),
                    },
                    "crop_page_override": self._per_page_crop_override_enabled(),
                    "rotation_page_override": self._per_page_rotation_override_enabled(),
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
            self.cover_operations = list(snapshot.get("covers", snapshot.get("whiteouts", [])))
            self.rotation_operations = list(snapshot.get("rotations", []))
            self.active_crop_info = clone_crop_info(snapshot.get("active_crop_info"))
            self.selected_pages = set(snapshot.get("selected_pages", set()))
            self.selection_anchor = snapshot.get("selection_anchor")
            self.preview_page_num = snapshot.get("preview_page_num")
            self.page_crop_overrides = dict(snapshot.get("page_crop_overrides", {}))
            crop_selections = snapshot.get("crop_selections", {})
            for name, view in (
                ("single", self.single_view),
                ("odd", self.odd_view),
                ("even", self.even_view),
            ):
                selection = crop_selections.get(name)
                if selection:
                    view.setSelection(QRectF(selection), notify=False)
                else:
                    view.clearSelection(notify=False)
            self._selection_before_preview = crop_selections.get("before_preview")
            with QSignalBlocker(self.crop_page_override_checkbox):
                self.crop_page_override_checkbox.setChecked(
                    bool(snapshot.get("crop_page_override") and self.preview_page_num is not None)
                )
            with QSignalBlocker(self.rotation_page_override_checkbox):
                self.rotation_page_override_checkbox.setChecked(
                    bool(
                        snapshot.get("rotation_page_override") and self.preview_page_num is not None
                    )
                )
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

    def _per_page_crop_override_enabled(self):
        return self.preview_page_num is not None and self.crop_page_override_checkbox.isChecked()

    def _per_page_rotation_override_enabled(self):
        return (
            self.preview_page_num is not None and self.rotation_page_override_checkbox.isChecked()
        )

    def onCropPageOverrideToggled(self, enabled):
        page_num = self.preview_page_num
        if enabled and page_num is None:
            with QSignalBlocker(self.crop_page_override_checkbox):
                self.crop_page_override_checkbox.setChecked(False)
            return

        if page_num is not None:
            if enabled:
                selection = self.single_view.getSelectionRect()
                if selection:
                    with suppress(IndexError, ValueError):
                        self.page_crop_overrides[page_num] = rect_to_tuple(
                            self._scene_rect_to_pdf_rect(
                                selection,
                                page_num,
                                self._page_canvas_dimensions(page_num),
                            )
                        )
                self.statusBar().showMessage(f"Custom crop enabled for page {page_num + 1}.", 6000)
            else:
                self.page_crop_overrides.pop(page_num, None)
                self._set_preview_crop_selection(page_num)
                self.statusBar().showMessage(
                    f"Page {page_num + 1} now follows its stack settings.", 6000
                )
        self.updateActionState()

    def _stack_selection_for_page(self, page_num):
        if self.view_mode == "all":
            if self.preview_page_num is not None:
                return (
                    QRectF(self._selection_before_preview)
                    if self._selection_before_preview
                    else None
                )
            return self._selection_copy(self.single_view)
        view = self.odd_view if page_num % 2 == 0 else self.even_view
        return self._selection_copy(view)

    def _set_stack_selection_for_page(self, page_num, rect):
        rect = QRectF(rect)
        if self.view_mode == "all":
            if self.preview_page_num is not None:
                self._selection_before_preview = rect
            else:
                self.single_view.setSelection(rect, notify=False)
            for view in (self.odd_view, self.even_view):
                view.setSelection(
                    self._selection_with_size(rect, view.getSelectionRect()), notify=False
                )
            return

        primary_view = self.odd_view if page_num % 2 == 0 else self.even_view
        secondary_view = self.even_view if primary_view is self.odd_view else self.odd_view
        primary_view.setSelection(rect, notify=False)
        secondary_view.setSelection(
            self._selection_with_size(rect, secondary_view.getSelectionRect()), notify=False
        )

    def _preview_selection_for_page(self, page_num):
        if self.pdf_doc is None or not (0 <= page_num < len(self.images)):
            return None
        pdf_rect = self.page_crop_overrides.get(page_num)
        if pdf_rect is None:
            stack_rect = self._stack_selection_for_page(page_num)
            if not stack_rect:
                return None
            pdf_rect = self._scene_rect_to_pdf_rect(
                stack_rect,
                page_num,
                self._stack_canvas_dimensions(),
            )
        return self._pdf_rect_to_scene_rect(
            pdf_rect,
            page_num,
            self._page_canvas_dimensions(page_num),
        )

    def _set_preview_crop_selection(self, page_num):
        try:
            selection = self._preview_selection_for_page(page_num)
        except (IndexError, ValueError):
            selection = None
        if selection:
            self.single_view.setSelection(selection, notify=False)
        else:
            self.single_view.clearSelection(notify=False)

    def sync_selection_from_single(self, rect):
        if self._is_syncing_selection:
            return

        self._is_syncing_selection = True
        try:
            if not rect or rect.isNull() or not rect.isValid():
                if self.preview_page_num is None:
                    self.odd_view.clearSelection(notify=False)
                    self.even_view.clearSelection(notify=False)
                return

            if self.preview_page_num is not None:
                page_num = self.preview_page_num
                pdf_rect = self._scene_rect_to_pdf_rect(
                    rect,
                    page_num,
                    self._page_canvas_dimensions(page_num),
                )
                if self._per_page_crop_override_enabled():
                    self.page_crop_overrides[page_num] = rect_to_tuple(pdf_rect)
                else:
                    stack_rect = self._pdf_rect_to_scene_rect(
                        pdf_rect,
                        page_num,
                        self._stack_canvas_dimensions(),
                    )
                    self._set_stack_selection_for_page(page_num, stack_rect)
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
        if self.preview_page_num is not None:
            self.showStackView()
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
<li>Click <b>Crop</b> to open its controls. Draw crop boxes in the stack view. Odd/even positions stay independent while their sizes stay uniform. In a page preview, enable <b>This page</b> to make that page differ.</li>
<li>Click <b>Cover</b> to choose or pick a color, then draw a visual cover. It applies to every page and does not remove the underlying PDF content.</li>
<li>Open <b>Rotate</b>, enter a fine angle, and click <b>Preview</b> when you want to inspect it. <b>Discard</b> clears the pending preview; <b>Apply</b> changes the PDF. The rotation toolbar's <b>This page</b> option limits rotation to the previewed page.</li>
<li>Click a thumbnail image to preview it; click it again or use <b>Stack</b> to return. Thumbnail checkboxes select pages for deletion. Ctrl toggles pages and Shift selects a range.</li>
<li>Crop and Cover apply to all pages; explicit crop overrides replace the stack crop only on their pages.</li>
<li>Use <b>Undo</b> to restore the previous crop, cover, rotation, or page deletion.</li>
<li>Save your changes with the purple <b>Save</b> button.</li>
</ol>"""
        QMessageBox.information(self, "About PyCropPDF", help_text)

    def togglePagePreview(self, page_num):
        if self.is_processing:
            return

        page_num = int(page_num)
        if self.preview_page_num == page_num:
            self.showStackView()
            return

        if self.preview_page_num is None:
            self._selection_before_preview = (
                self._selection_copy(self.single_view) if self.view_mode == "all" else None
            )

        self.preview_page_num = page_num
        with QSignalBlocker(self.crop_page_override_checkbox):
            self.crop_page_override_checkbox.setChecked(page_num in self.page_crop_overrides)
        self.updateOverlay()
        self._set_preview_crop_selection(page_num)
        self._sync_thumbnail_selection()
        self.rotationPreviewInputsChanged()

    def showStackView(self):
        if self.preview_page_num is None:
            return

        self.preview_page_num = None
        with QSignalBlocker(self.crop_page_override_checkbox):
            self.crop_page_override_checkbox.setChecked(False)
        with QSignalBlocker(self.rotation_page_override_checkbox):
            self.rotation_page_override_checkbox.setChecked(False)
        self.updateOverlay()
        if self.view_mode == "all" and self._selection_before_preview:
            self.single_view.setSelection(self._selection_before_preview, notify=False)
        self._selection_before_preview = None
        self._sync_thumbnail_selection()
        self.rotationPreviewInputsChanged()

    def _rotation_target_pages(self):
        if self.pdf_doc is None:
            return []
        if self._per_page_rotation_override_enabled():
            return [self.preview_page_num]
        scope = self.rotation_scope_combo.currentData()
        if scope == "odd":
            return list(range(0, len(self.pdf_doc), 2))
        if scope == "even":
            return list(range(1, len(self.pdf_doc), 2))
        return list(range(len(self.pdf_doc)))

    def updateRotationControls(self, *_args):
        self.rotationPreviewInputsChanged()

    def rotationPreviewInputsChanged(self, *_args):
        if abs(self._rotation_preview_angle) >= 0.01:
            self._rotation_preview_angle = 0.0
            if self.images and not self.is_processing:
                self.updateOverlay()
            self.statusBar().showMessage(
                "Rotation preview cleared because its angle or target changed. "
                "Click Preview to refresh it.",
                6000,
            )
        elif abs(self.rotation_angle_spin.value()) >= 0.01:
            self.statusBar().showMessage(
                f"Angle set to {self.rotation_angle_spin.value():g}°. "
                "Click Preview to inspect it or Apply to change the PDF.",
                6000,
            )
        self.updateActionState()

    def previewRotation(self, *_args):
        if self.is_processing or self.pdf_doc is None:
            return
        if self.active_crop_info:
            QMessageBox.warning(
                self,
                "Reset Crop First",
                "Reset the active crop before previewing rotation so coordinates remain valid.",
            )
            return
        angle = normalize_angle(self.rotation_angle_spin.value())
        targets = self._rotation_target_pages()
        if abs(angle) < 0.01 or not targets:
            self.statusBar().showMessage("Enter a non-zero angle and choose a target.", 5000)
            return

        self._rotation_preview_angle = angle
        if self.images:
            self.updateOverlay()
        self.statusBar().showMessage(
            f"Previewing {angle:g}° on {len(targets)} page"
            f"{'s' if len(targets) != 1 else ''}. Apply or Discard when ready.",
            8000,
        )
        self.updateActionState()

    def discardRotationPreview(self, *_args):
        had_preview = abs(self._rotation_preview_angle) >= 0.01
        had_angle = abs(self.rotation_angle_spin.value()) >= 0.01
        self._rotation_preview_angle = 0.0
        with QSignalBlocker(self.rotation_angle_spin):
            self.rotation_angle_spin.setValue(0.0)
        if had_preview and self.images and not self.is_processing:
            self.updateOverlay()
        if had_preview or had_angle:
            self.statusBar().showMessage("Rotation preview discarded.", 5000)
        self.updateActionState()

    def _confirm_arbitrary_rotation(self, target_pages):
        affected = pages_with_interactive_objects(self.pdf_doc, target_pages)
        if not affected:
            return True
        page_labels = ", ".join(str(page_num + 1) for page_num in affected[:8])
        if len(affected) > 8:
            page_labels += ", ..."
        response = QMessageBox.warning(
            self,
            "Interactive PDF Objects",
            "Arbitrary-angle rotation will reposition links, annotations, and form fields, "
            "but some annotation appearances and link destinations cannot be rotated "
            "exactly.\n\n"
            f"Affected pages: {page_labels}\n\nContinue?",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return response == QMessageBox.StandardButton.Ok

    def _start_rotation_worker(self, worker, status_message):
        self.pushUndo()
        self._rotation_undo_pushed = True
        self.setUIProcessing(True)
        self._operation_id += 1
        operation_id = self._operation_id
        self.statusBar().showMessage(status_message)
        worker.signals.result.connect(
            lambda result, operation_id=operation_id: self.rotationFinished(operation_id, result)
        )
        worker.signals.error.connect(
            lambda error, operation_id=operation_id: self.rotationError(operation_id, error)
        )
        worker.signals.finished.connect(
            lambda operation_id=operation_id: self.processingFinished(operation_id)
        )
        self.threadpool.start(worker)

    def applyRotation(self, clockwise_degrees):
        if self.is_processing or self.pdf_doc is None:
            return
        if self.active_crop_info:
            QMessageBox.warning(
                self,
                "Reset Crop First",
                "Reset the active crop before rotating pages so crop coordinates remain valid.",
            )
            return

        clockwise_degrees = normalize_angle(clockwise_degrees)
        if abs(clockwise_degrees) < 0.01:
            self.statusBar().showMessage("Enter a non-zero rotation angle.", 5000)
            return
        target_pages = self._rotation_target_pages()
        if not target_pages:
            QMessageBox.information(
                self,
                "No Rotation Target",
                "Choose a page stack, or preview a page and enable This page.",
            )
            return

        quarter_turn = round(clockwise_degrees / 90.0) * 90.0
        if abs(clockwise_degrees - quarter_turn) >= 0.01 and not self._confirm_arbitrary_rotation(
            target_pages
        ):
            return

        rotations = dict.fromkeys(target_pages, clockwise_degrees)
        worker = RotatePagesWorker(self.pdf_doc.tobytes(), rotations)
        self._start_rotation_worker(
            worker,
            f"Rotating {len(target_pages)} page{'s' if len(target_pages) != 1 else ''}...",
        )

    def autoDeskew(self):
        if self.is_processing or self.pdf_doc is None:
            return
        if self.active_crop_info:
            QMessageBox.warning(
                self,
                "Reset Crop First",
                "Reset the active crop before deskewing pages so crop coordinates remain valid.",
            )
            return
        if not deskew_available():
            QMessageBox.information(
                self,
                "Auto Deskew Not Installed",
                "Install pycroppdf[deskew] to enable automatic skew detection.",
            )
            return

        target_pages = self._rotation_target_pages()
        if not target_pages:
            QMessageBox.information(
                self,
                "No Deskew Target",
                "Choose a page stack, or preview a page and enable This page.",
            )
            return
        if not self._confirm_arbitrary_rotation(target_pages):
            return

        worker_count = recommended_deskew_workers(len(target_pages))
        worker = AutoDeskewWorker(
            self.pdf_doc.tobytes(),
            target_pages,
            max_workers=worker_count,
        )
        worker_suffix = f" using {worker_count} workers" if worker_count > 1 else ""
        self._start_rotation_worker(
            worker,
            f"Detecting skew on {len(target_pages)} page"
            f"{'s' if len(target_pages) != 1 else ''}{worker_suffix}...",
        )

    def rotationFinished(self, operation_id, result):
        if operation_id != self._operation_id:
            return
        rotations = {
            int(page_num): float(angle)
            for page_num, angle in result.get("rotation_deltas", {}).items()
            if abs(float(angle)) >= 0.01
        }
        undetected = list(result.get("undetected_pages", []))
        if not rotations:
            if self._rotation_undo_pushed and self.undo_stack:
                self.undo_stack.pop()
            self._rotation_undo_pushed = False
            self.pending_status_message = "No usable skew angle was detected."
            self.updateActionState()
            return

        try:
            new_document = fitz.open("pdf", result["pdf_bytes"])
            old_document = self.pdf_doc
            self.pdf_doc = new_document
            if old_document is not None:
                old_document.close()

            for page_num, angle in rotations.items():
                original_page = (
                    self.page_map[page_num] if page_num < len(self.page_map) else page_num
                )
                self.rotation_operations.append(
                    {
                        "original_page": original_page + 1,
                        "angle": round(float(angle), 3),
                    }
                )
            self._refresh_dirty_state()
            self._rotation_undo_pushed = False
            with QSignalBlocker(self.rotation_angle_spin):
                self.rotation_angle_spin.setValue(0.0)
            self._rotation_preview_angle = 0.0
            self.page_crop_overrides.clear()
            with QSignalBlocker(self.crop_page_override_checkbox):
                self.crop_page_override_checkbox.setChecked(False)
            self._selection_before_preview = None
            self.clearAllSelections()
            self._reload_after_processing = True
            message = f"Rotated {len(rotations)} page{'s' if len(rotations) != 1 else ''}."
            if undetected:
                message += (
                    f" No angle was detected on {len(undetected)} page"
                    f"{'s' if len(undetected) != 1 else ''}."
                )
            self.pending_status_message = message
        except Exception as error:
            self.rotationError(operation_id, str(error))

    def rotationError(self, operation_id, error_str):
        if operation_id != self._operation_id:
            return
        if self._rotation_undo_pushed and self.undo_stack:
            self.undo_stack.pop()
        self._rotation_undo_pushed = False
        self.processingError(operation_id, error_str)

    def clearAllSelections(self):
        self._is_syncing_selection = True
        try:
            self.single_view.clearSelection(notify=False)
            self.odd_view.clearSelection(notify=False)
            self.even_view.clearSelection(notify=False)
            self._selection_before_preview = None
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
        self._selection_before_preview = None
        self.rotation_scope_combo.setCurrentIndex(0)
        self.pending_status_message = ""
        self.undo_stack = []
        self.page_map = list(range(len(self.pdf_doc)))
        self.original_page_count = len(self.pdf_doc)
        self.cover_operations = []
        self.rotation_operations = []
        self.page_crop_overrides = {}
        self.selected_pages = set()
        self.selection_anchor = None
        self.is_dirty = False
        with QSignalBlocker(self.crop_page_override_checkbox):
            self.crop_page_override_checkbox.setChecked(False)
        with QSignalBlocker(self.rotation_page_override_checkbox):
            self.rotation_page_override_checkbox.setChecked(False)
        with QSignalBlocker(self.rotation_angle_spin):
            self.rotation_angle_spin.setValue(0.0)
        self._rotation_preview_angle = 0.0

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

    def _rotation_preview_targets(self):
        if abs(self._rotation_preview_angle) < 0.01:
            return set()
        return set(self._rotation_target_pages())

    def _preview_image(self, page_num, image):
        if page_num not in self._rotation_preview_targets():
            return image
        return image.transformed(
            QTransform().rotate(self._rotation_preview_angle),
            Qt.TransformationMode.SmoothTransformation,
        )

    @staticmethod
    def _compose_images(images, canvas_dims=None):
        images = [image for image in images if image is not None and not image.isNull()]
        if not images:
            return None
        if canvas_dims is None:
            canvas_dims = (
                max(image.width() for image in images),
                max(image.height() for image in images),
            )
        width, height = canvas_dims
        base = QImage(width, height, QImage.Format.Format_ARGB32)
        base.fill(Qt.GlobalColor.transparent)
        painter = QPainter(base)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        for index, image in enumerate(images):
            painter.setOpacity(1.0 if index == 0 else 0.2)
            painter.drawImage((width - image.width()) // 2, (height - image.height()) // 2, image)
        painter.end()
        return QPixmap.fromImage(base)

    def showSinglePagePreview(self, page_num):
        self.single_view.show()
        self.odd_view.hide()
        self.even_view.hide()

        if 0 <= page_num < len(self.images):
            image = self.images[page_num]
            if image is None or image.isNull():
                return
            selection = self._selection_copy(self.single_view)
            pixmap = QPixmap.fromImage(self._preview_image(page_num, image))
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

        if self._rotation_preview_targets():
            preview_images = [
                self._preview_image(page_num, image)
                for page_num, image in enumerate(self.images)
                if image is not None
            ]
            self._set_view_pixmap(
                self.single_view,
                self._compose_images(preview_images),
                selection,
            )
            return

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

        if self._rotation_preview_targets():
            preview_images = {
                page_num: self._preview_image(page_num, image)
                for page_num, image in enumerate(self.images)
                if image is not None
            }
            canvas_dims = (
                max(image.width() for image in preview_images.values()),
                max(image.height() for image in preview_images.values()),
            )
            self._set_view_pixmap(
                self.odd_view,
                self._compose_images(
                    [preview_images[page_num] for page_num in preview_images if page_num % 2 == 0],
                    canvas_dims,
                ),
                odd_selection,
            )
            even_images = [
                preview_images[page_num] for page_num in preview_images if page_num % 2 == 1
            ]
            if even_images:
                self._set_view_pixmap(
                    self.even_view,
                    self._compose_images(even_images, canvas_dims),
                    even_selection,
                )
            else:
                self.even_view.clearScene()
            return

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
            self.thumbnail_layout.setContentsMargins(0, 0, 0, 0)
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

    def handleThumbnailSelection(self, page_num, modifiers, toggle=False):
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
        elif control or toggle:
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
            "Use the checkboxes or Ctrl to toggle pages; Shift selects a range.",
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

    def _page_canvas_dimensions(self, page_num):
        dimensions = self._current_image_dimensions()[int(page_num)]
        if not all(dimensions):
            raise ValueError("The selected page has no renderable preview.")
        return dimensions

    def _stack_canvas_dimensions(self):
        valid_dims = [
            dimensions for dimensions in self._current_image_dimensions() if all(dimensions)
        ]
        if not valid_dims:
            raise ValueError("No page previews are available.")
        return max(width for width, _ in valid_dims), max(height for _, height in valid_dims)

    def _canvas_dimensions(self, page_num):
        if self.preview_page_num is not None:
            return self._page_canvas_dimensions(page_num)
        return self._stack_canvas_dimensions()

    def _visible_pdf_rect_for_page(self, page_num):
        if self.active_crop_info:
            crop_rect = self.active_crop_info.get("rects", {}).get(page_num)
            if crop_rect:
                return crop_rect
        return self.pdf_doc[page_num].cropbox

    def _scene_rect_to_pdf_rect(self, scene_rect, page_num, canvas_dims=None):
        image_dims = self._current_image_dimensions()
        return scene_rect_to_pdf_coords(
            scene_rect,
            image_dims[page_num],
            canvas_dims or self._canvas_dimensions(page_num),
            self.pdf_doc[page_num],
            self._visible_pdf_rect_for_page(page_num),
        )

    def _pdf_rect_to_scene_rect(self, pdf_rect, page_num, canvas_dims=None):
        image_dims = self._current_image_dimensions()
        return pdf_rect_to_scene_coords(
            pdf_rect,
            image_dims[page_num],
            canvas_dims or self._canvas_dimensions(page_num),
            self.pdf_doc[page_num],
            self._visible_pdf_rect_for_page(page_num),
        )

    def cropSelection(self):
        if self.is_processing or self.pdf_doc is None:
            return
        if abs(self._rotation_preview_angle) >= 0.01:
            QMessageBox.information(
                self,
                "Apply Rotation First",
                "Apply or clear the rotation preview before applying a crop.",
            )
            return

        scene_rects_by_page = {}
        if self.view_mode == "all":
            scene_rect = (
                self._selection_before_preview
                if self.preview_page_num is not None
                else self.single_view.getSelectionRect()
            )
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
            stack_canvas = self._stack_canvas_dimensions()
            crop_rects = {
                int(page_num): rect_to_tuple(
                    self._scene_rect_to_pdf_rect(scene_rect, page_num, stack_canvas)
                )
                for page_num, scene_rect in scene_rects_by_page.items()
            }
            crop_rects.update(
                {
                    int(page_num): rect_to_tuple(rect)
                    for page_num, rect in self.page_crop_overrides.items()
                    if 0 <= int(page_num) < len(self.pdf_doc)
                }
            )
        except (IndexError, ValueError) as error:
            QMessageBox.warning(self, "Invalid Crop", f"{error!s}")
            return

        self.pushUndo(include_pdf=False)
        retained_rects = (
            dict(self.active_crop_info.get("rects", {})) if self.active_crop_info else {}
        )
        retained_rects.update(crop_rects)
        missing_pages = set(range(len(self.pdf_doc))) - set(retained_rects)
        if missing_pages:
            if self.undo_stack:
                self.undo_stack.pop()
            QMessageBox.warning(
                self,
                "Incomplete Crop",
                "Draw a crop box for the stack before adding per-page overrides. "
                "A crop must resolve to every page.",
            )
            return
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
        self.page_crop_overrides.clear()
        with QSignalBlocker(self.crop_page_override_checkbox):
            self.crop_page_override_checkbox.setChecked(False)
        self.clearAllSelections()
        self.reloadImages(set(crop_rects))

    def _target_pages_for_rectangle_request(self):
        return list(range(len(self.pdf_doc)))

    def handleCoverRequest(self, rect):
        if self.is_processing or self.pdf_doc is None:
            return

        if self.active_crop_info:
            QMessageBox.warning(
                self,
                "Reset Crop First",
                "A cover cannot be positioned reliably on an active cropped preview. "
                "Please reset the crop first.",
            )
            return

        target_pages = self._target_pages_for_rectangle_request()
        if not target_pages:
            return
        self.applyCover(rect, target_pages)

    def handleWhiteoutRequest(self, rect):
        """Compatibility alias for integrations using the former tool name."""
        self.handleCoverRequest(rect)

    def applyCover(self, rect, target_pages):
        """Draw a non-destructive visual cover on the requested pages."""
        self._apply_cover_operation(rect, target_pages)

    def applyWhiteout(self, rect, target_pages):
        """Compatibility alias for integrations using the former tool name."""
        self.applyCover(rect, target_pages)

    def _apply_cover_operation(self, rect, target_pages):
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
        before_covers = [dict(operation) for operation in self.cover_operations]
        before_dirty = self.is_dirty
        try:
            self.statusBar().showMessage(
                f"Applying cover to {len(target_pages)} page"
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
                page.draw_rect(
                    pdf_rect,
                    color=self.cover_color,
                    fill=self.cover_color,
                    width=0,
                )
                operation["color"] = [round(float(value), 4) for value in self.cover_color]
                self.cover_operations.append(operation)

            self.pending_status_message = (
                f"Cover applied to {len(target_pages)} page{'s' if len(target_pages) != 1 else ''}."
            )
            self._refresh_dirty_state()
            self.reloadImages(target_pages)
        except Exception as error:
            self.pdf_doc.close()
            self.pdf_doc = fitz.open("pdf", before_pdf_bytes)
            self.cover_operations = before_covers
            self.is_dirty = before_dirty
            if self.undo_stack:
                self.undo_stack.pop()
            QMessageBox.critical(self, "Error", f"Failed to apply cover: {error!s}")

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
            old_canvas = self._stack_canvas_dimensions()

            deleted_set = set(selected_pages)
            self.active_crop_info = remap_crop_info_after_deletions(
                self.active_crop_info,
                deleted_set,
            )
            self.page_crop_overrides = remap_page_mapping_after_deletions(
                self.page_crop_overrides,
                deleted_set,
            )
            if self.preview_page_num is not None:
                remapped_preview = remap_page_indices_after_deletions(
                    {self.preview_page_num},
                    deleted_set,
                )
                self.preview_page_num = next(iter(remapped_preview), None)
                if self.preview_page_num is None:
                    with QSignalBlocker(self.crop_page_override_checkbox):
                        self.crop_page_override_checkbox.setChecked(False)
                    with QSignalBlocker(self.rotation_page_override_checkbox):
                        self.rotation_page_override_checkbox.setChecked(False)

            for page_num in selected_pages:
                self.pdf_doc.delete_page(page_num)
                self.images.pop(page_num)
                self.page_map.pop(page_num)

            new_canvas = self._stack_canvas_dimensions()
            offset_x = (new_canvas[0] - old_canvas[0]) / 2
            offset_y = (new_canvas[1] - old_canvas[1]) / 2
            if offset_x or offset_y:
                for view in (self.odd_view, self.even_view):
                    selection = view.getSelectionRect()
                    if selection:
                        view.setSelection(selection.translated(offset_x, offset_y), notify=False)
                if self._selection_before_preview:
                    self._selection_before_preview.translate(offset_x, offset_y)
                elif self.preview_page_num is None and self.view_mode == "all":
                    selection = self.single_view.getSelectionRect()
                    if selection:
                        self.single_view.setSelection(
                            selection.translated(offset_x, offset_y), notify=False
                        )

            if (
                self.preview_page_num is None
                and self.view_mode == "all"
                and self._selection_before_preview
            ):
                self.single_view.setSelection(self._selection_before_preview, notify=False)
                self._selection_before_preview = None

            self.selected_pages = set()
            self.selection_anchor = None
            self._refresh_dirty_state()
            self.updateThumbnails()
            self.updateOverlay()
            if self.preview_page_num is not None:
                self._set_preview_crop_selection(self.preview_page_num)
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
            rotations=self.rotation_operations,
            whiteouts=self.cover_operations,
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
        coordinate_tools_available = available and abs(self._rotation_preview_angle) < 0.01
        self.crop_tool_btn.setEnabled(coordinate_tools_available)
        cover_available = coordinate_tools_available and not self.active_crop_info
        self.cover_tool_btn.setEnabled(cover_available)
        self.cover_color_btn.setEnabled(cover_available)
        self.pick_cover_color_btn.setEnabled(cover_available)
        self.crop_btn.setEnabled(coordinate_tools_available and self.active_tool == "crop")
        self.reset_crop_btn.setEnabled(available and bool(self.active_crop_info))
        rotation_available = available and not self.active_crop_info
        self.crop_page_override_checkbox.setEnabled(
            coordinate_tools_available and self.preview_page_num is not None
        )
        self.rotation_page_override_checkbox.setEnabled(
            rotation_available and self.preview_page_num is not None
        )
        self.rotation_scope_combo.setEnabled(
            rotation_available and not self._per_page_rotation_override_enabled()
        )
        self.rotate_left_btn.setEnabled(rotation_available and bool(self._rotation_target_pages()))
        self.rotate_right_btn.setEnabled(rotation_available and bool(self._rotation_target_pages()))
        self.rotation_angle_spin.setEnabled(rotation_available)
        rotation_angle_ready = (
            rotation_available
            and bool(self._rotation_target_pages())
            and abs(self.rotation_angle_spin.value()) >= 0.01
        )
        self.preview_rotation_btn.setEnabled(rotation_angle_ready)
        self.discard_rotation_preview_btn.setEnabled(
            available
            and (
                abs(self._rotation_preview_angle) >= 0.01
                or abs(self.rotation_angle_spin.value()) >= 0.01
            )
        )
        self.apply_rotation_btn.setEnabled(rotation_angle_ready)
        self.auto_deskew_btn.setEnabled(
            rotation_available and bool(self._rotation_target_pages()) and deskew_available()
        )
        self.delete_btn.setEnabled(available and bool(self.selected_pages))
        self.view_stack_btn.setEnabled(available and self.preview_page_num is not None)
        self.rotation_options_toggle_btn.setEnabled(available)
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
        if self._reload_after_processing:
            self._reload_after_processing = False
            self.reloadImages()
            return
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
        QApplication.instance().removeEventFilter(self)
        super().closeEvent(event)
