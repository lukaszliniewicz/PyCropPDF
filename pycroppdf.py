import sys
import fitz
from PyQt6.QtWidgets import (QApplication, QMainWindow, QFileDialog, 
                            QGraphicsView, QGraphicsScene, QVBoxLayout, 
                            QHBoxLayout, QWidget, QLabel, QScrollArea,
                            QMessageBox, QCheckBox, QPushButton, QSpinBox,
                            QFrame, QSizePolicy, QToolBar, QGridLayout)
from PyQt6.QtCore import Qt, QRectF, QPointF, QSize
from PyQt6.QtGui import QImage, QPixmap, QPainter, QPen
import tempfile
import os
import argparse

class PageGraphicsView(QGraphicsView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.scene = QGraphicsScene()
        self.setScene(self.scene)
        self.selecting = False
        self.selection_start = None
        self.selection_rect = None
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.selecting = True
            self.selection_start = self.mapToScene(event.pos())
            if self.selection_rect:
                self.scene.removeItem(self.selection_rect)
            self.selection_rect = None

    def mouseMoveEvent(self, event):
        if self.selecting:
            current_pos = self.mapToScene(event.pos())
            if self.selection_rect:
                self.scene.removeItem(self.selection_rect)
            rect = QRectF(self.selection_start, current_pos)
            pen = QPen(Qt.GlobalColor.blue, 2, Qt.PenStyle.SolidLine)
            self.selection_rect = self.scene.addRect(rect, pen)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.selecting = False

    def clearSelection(self):
        if self.selection_rect:
            self.scene.removeItem(self.selection_rect)
            self.selection_rect = None

    def getSelectionRect(self):
        return self.selection_rect.rect() if self.selection_rect else None

class ThumbnailWidget(QWidget):
    def __init__(self, page_num, image, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout()
        layout.setSpacing(2)
        layout.setContentsMargins(2, 2, 2, 2)
        self.checkbox = QCheckBox()
        self.page_num = page_num
        
        # Create thumbnail
        thumbnail = image.scaled(QSize(80, 120), 
                               Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
        self.label = QLabel()
        self.label.setPixmap(QPixmap.fromImage(thumbnail))
        
        layout.addWidget(self.checkbox)
        layout.addWidget(self.label)
        layout.addWidget(QLabel(f"Page {page_num + 1}"))
        self.setLayout(layout)

class PDFViewer(QMainWindow):
    def __init__(self, input_pdf=None, save_directory=None, save_filename=None):
        super().__init__()
        self.images = []
        self.setAcceptDrops(True)
        self.pdf_doc = None
        self.pdf_path = None
        self.original_pdf_path = None 
        self.view_mode = 'all'
        self.save_directory = save_directory
        self.save_filename = save_filename
        
        # Create views
        self.single_view = PageGraphicsView()
        self.odd_view = PageGraphicsView()
        self.even_view = PageGraphicsView()
        
        self.current_view = self.single_view
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


        # Create main widget and layout
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)

        # Create toolbar
        toolbar = QToolBar()
        toolbar.setMovable(False)
        toolbar.setFloatable(False)

        # Add Open PDF button
        open_btn = QPushButton('Open PDF')
        open_btn.setMinimumWidth(100)
        open_btn.clicked.connect(self.openPDF)
        toolbar.addWidget(open_btn)

        toolbar.addSeparator()

        # Add existing mode toggle button
        self.mode_toggle = QPushButton('View Mode: All Pages Overlay')
        self.mode_toggle.setMinimumWidth(250)
        self.mode_toggle.setToolTip('Toggle between viewing all pages overlaid or separating odd/even pages')
        self.mode_toggle.clicked.connect(self.toggleMode)
        toolbar.addWidget(self.mode_toggle)

        toolbar.addSeparator()

        # Add Crop Selection button
        crop_btn = QPushButton('Crop Selection')
        crop_btn.setMinimumWidth(100)
        crop_btn.clicked.connect(self.cropSelection)
        toolbar.addWidget(crop_btn)

        toolbar.addSeparator()

        # Add Reset Crop button
        reset_crop_btn = QPushButton('Reset Crop')
        reset_crop_btn.setMinimumWidth(100)
        reset_crop_btn.clicked.connect(self.resetCrop)
        toolbar.addWidget(reset_crop_btn)

        toolbar.addSeparator()

        # Add existing delete button
        self.delete_btn = QPushButton('Delete Selected Pages')
        self.delete_btn.setMinimumWidth(150)
        self.delete_btn.clicked.connect(self.deleteSelectedPages)
        toolbar.addWidget(self.delete_btn)

        toolbar.addSeparator()

        # Add Save PDF button
        save_btn = QPushButton('Save PDF')
        save_btn.setMinimumWidth(100)
        save_btn.clicked.connect(self.savePDF)
        toolbar.addWidget(save_btn)

        main_layout.addWidget(toolbar)

        # Create content widget
        content_widget = QWidget()
        content_layout = QHBoxLayout(content_widget)

        # Create sidebar for thumbnails
        sidebar = QWidget()
        sidebar.setMinimumWidth(300)  # Increased from 150 to 300
        sidebar.setMaximumWidth(300)  # Also set maximum to keep it fixed
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
        self.odd_view.hide()
        self.even_view.hide()
        self.view_stack_layout.addWidget(self.odd_view)
        self.view_stack_layout.addWidget(self.even_view)
        
        content_layout.addWidget(self.view_stack)
        main_layout.addWidget(content_widget)

    def loadPDF(self, pdf_path):
        try:
            if self.pdf_doc:
                self.pdf_doc.close()
            
            # Store the original PDF path if it's not a temp file
            if not pdf_path.endswith('.temp.pdf'):
                self.original_pdf_path = pdf_path
            
            self.pdf_doc = fitz.open(pdf_path)
            self.images.clear()
            self.single_view.scene.clear()
            self.odd_view.scene.clear()
            self.even_view.scene.clear()

            for page_num in range(len(self.pdf_doc)):
                page = self.pdf_doc[page_num]
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
                
                img = QImage(pix.samples, pix.width, pix.height,
                        pix.stride, QImage.Format.Format_RGB888)
                self.images.append(img)

            self.updateThumbnails()
            self.updateOverlay()
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load PDF: {str(e)}")

    def resetCrop(self):
        if not self.original_pdf_path:
            QMessageBox.warning(self, "Warning", "No original PDF to revert to.")
            return
            
        try:
            # Store the original path temporarily
            original_path = self.original_pdf_path
            current_temp_path = self.pdf_path
            
            # Create new document from original before closing current
            new_doc = fitz.open(original_path)
            
            # Close current document
            if self.pdf_doc:
                self.pdf_doc.close()
                
            # Remove temporary file if it exists
            if current_temp_path and current_temp_path.endswith('.temp.pdf'):
                try:
                    os.remove(current_temp_path)
                except:
                    pass
            
            # Set the new document and path
            self.pdf_doc = new_doc
            self.pdf_path = original_path
            
            # Reset images and views
            self.images.clear()
            self.single_view.scene.clear()
            self.odd_view.scene.clear()
            self.even_view.scene.clear()
            
            # Load images from new document
            for page_num in range(len(self.pdf_doc)):
                page = self.pdf_doc[page_num]
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
                img = QImage(pix.samples, pix.width, pix.height,
                            pix.stride, QImage.Format.Format_RGB888)
                self.images.append(img)
            
            # Update UI
            self.updateThumbnails()
            
            # Make sure views are properly initialized
            if self.view_mode == 'all':
                self.single_view.show()
                self.odd_view.hide()
                self.even_view.hide()
            else:
                self.single_view.hide()
                self.odd_view.show()
                self.even_view.show()
                
            self.updateOverlay()
            QMessageBox.information(self, "Success", "Crop box reset to original state.")
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to reset crop: {str(e)}")
            import traceback
            traceback.print_exc()

    def toggleMode(self):
        self.view_mode = 'odd_even' if self.view_mode == 'all' else 'all'
        button_text = ('View Mode: Separate Odd/Even Pages' 
                    if self.view_mode == 'odd_even' 
                    else 'View Mode: All Pages Overlay')
        self.mode_toggle.setText(button_text)
        
        # Switch views
        if self.view_mode == 'all':
            self.single_view.show()
            self.odd_view.hide()
            self.even_view.hide()
        else:
            self.single_view.hide()
            self.odd_view.show()
            self.even_view.show()
            
        self.clearAllSelections()
        self.updateOverlay()

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
            
            # Store the original PDF path if it's not a temp file and we don't already have one
            if not pdf_path.endswith('.temp.pdf') and not self.original_pdf_path:
                self.original_pdf_path = pdf_path
            
            self.pdf_doc = fitz.open(pdf_path)
            self.images.clear()
            
            # Reset all views
            self.single_view.scene.clear()
            self.odd_view.scene.clear()
            self.even_view.scene.clear()
            
            # Reset selection states
            self.single_view.selecting = False
            self.single_view.selection_start = None
            self.single_view.selection_rect = None
            
            self.odd_view.selecting = False
            self.odd_view.selection_start = None
            self.odd_view.selection_rect = None
            
            self.even_view.selecting = False
            self.even_view.selection_start = None
            self.even_view.selection_rect = None

            # Load images
            for page_num in range(len(self.pdf_doc)):
                page = self.pdf_doc[page_num]
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
                
                img = QImage(pix.samples, pix.width, pix.height,
                        pix.stride, QImage.Format.Format_RGB888)
                self.images.append(img)

            self.updateThumbnails()
            self.updateOverlay()
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load PDF: {str(e)}")

    def updateOverlay(self):
        if not self.images:
            return

        if self.view_mode == 'all':
            self.updateSingleViewOverlay()
        else:
            self.updateSplitViewOverlay()

    def updateSingleViewOverlay(self):
        if not self.images:
            return
            
        self.single_view.scene.clear()
        
        # Find maximum dimensions
        max_width = max(img.width() for img in self.images)
        max_height = max(img.height() for img in self.images)
        
        # Create a transparent base image of maximum size
        base_img = QImage(max_width, max_height, QImage.Format.Format_ARGB32)
        base_img.fill(Qt.GlobalColor.transparent)
        
        painter = QPainter(base_img)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        # Draw first page fully opaque
        img = self.images[0]
        x = (max_width - img.width()) // 2
        y = (max_height - img.height()) // 2
        painter.setOpacity(1.0)
        painter.drawImage(x, y, img)

        # Draw remaining pages semi-transparent
        for img in self.images[1:]:
            x = (max_width - img.width()) // 2
            y = (max_height - img.height()) // 2
            painter.setOpacity(0.2)
            painter.drawImage(x, y, img)

        painter.end()
        pixmap = QPixmap.fromImage(base_img)
        self.single_view.scene.addPixmap(pixmap)
        self.single_view.setSceneRect(QRectF(pixmap.rect()))
        self.single_view.fitInView(self.single_view.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def updateSplitViewOverlay(self):
        if not self.images:
            return
            
        self.odd_view.scene.clear()
        self.even_view.scene.clear()

        # Process odd pages
        odd_pages = self.images[::2]
        if odd_pages:
            max_width = max(img.width() for img in odd_pages)
            max_height = max(img.height() for img in odd_pages)
            
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
            pixmap = QPixmap.fromImage(base_odd)
            self.odd_view.scene.addPixmap(pixmap)
            self.odd_view.setSceneRect(QRectF(pixmap.rect()))
            self.odd_view.fitInView(self.odd_view.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

        # Process even pages
        even_pages = self.images[1::2]
        if even_pages:
            max_width = max(img.width() for img in even_pages)
            max_height = max(img.height() for img in even_pages)
            
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
            pixmap = QPixmap.fromImage(base_even)
            self.even_view.scene.addPixmap(pixmap)
            self.even_view.setSceneRect(QRectF(pixmap.rect()))
            self.even_view.fitInView(self.even_view.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def updateThumbnails(self):
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
            self.thumbnail_layout.addWidget(thumbnail, row, col)

    def getSelectedPages(self):
        selected = []
        for i in range(self.thumbnail_layout.count() - 1):  # -1 for stretch
            widget = self.thumbnail_layout.itemAt(i).widget()
            if isinstance(widget, ThumbnailWidget) and widget.checkbox.isChecked():
                selected.append(widget.page_num)
        return selected
    
    def dragEnterEvent(self, event):
        try:
            mime_data = event.mimeData()
            
            # Print debug info
            print("Drag enter event detected")
            print(f"Has URLs: {mime_data.hasUrls()}")
            if mime_data.hasUrls():
                print(f"URLs: {[url.toString() for url in mime_data.urls()]}")
            
            if mime_data.hasUrls():
                for url in mime_data.urls():
                    file_path = url.toLocalFile()
                    print(f"File path: {file_path}")
                    if file_path.lower().endswith('.pdf'):
                        print("Accepting PDF file")
                        event.accept()
                        return
            event.ignore()
        except Exception as e:
            print(f"Error in dragEnterEvent: {str(e)}")
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
        except Exception as e:
            print(f"Error in dragMoveEvent: {str(e)}")
            event.ignore()

    def dropEvent(self, event):
        try:
            mime_data = event.mimeData()
            print("Drop event detected")
            
            if mime_data.hasUrls():
                for url in mime_data.urls():
                    file_path = url.toLocalFile()
                    print(f"Dropped file: {file_path}")
                    
                    if file_path.lower().endswith('.pdf'):
                        print(f"Loading PDF: {file_path}")
                        self.original_pdf_path = file_path
                        self.pdf_path = file_path
                        self.loadPDF(file_path)
                        event.accept()
                        return
            event.ignore()
        except Exception as e:
            print(f"Error in dropEvent: {str(e)}")
            event.ignore()

    def cropSelection(self):
        if not self.pdf_doc:
            QMessageBox.warning(self, "Warning", "No PDF is open.")
            return

        try:
            # Get crop rectangles based on view mode
            if self.view_mode == 'all':
                if not self.single_view.selection_rect:
                    QMessageBox.warning(self, "Warning", "Please make a selection first.")
                    return
                scene_rect = self.single_view.getSelectionRect()
                crop_rects = {page_num: scene_rect for page_num in range(len(self.pdf_doc))}
            else:
                odd_rect = self.odd_view.getSelectionRect()
                even_rect = self.even_view.getSelectionRect()
                
                if not (odd_rect or even_rect):
                    QMessageBox.warning(self, "Warning", "Please make at least one selection.")
                    return
                
                crop_rects = {}
                if odd_rect:
                    for i in range(0, len(self.pdf_doc), 2):
                        crop_rects[i] = odd_rect
                if even_rect:
                    for i in range(1, len(self.pdf_doc), 2):
                        crop_rects[i] = even_rect

            # Create new PDF with cropped pages
            new_doc = fitz.open()
            
            # Find maximum dimensions for the current view
            if self.view_mode == 'all':
                max_width = max(img.width() for img in self.images)
                max_height = max(img.height() for img in self.images)
            
            for page_num in range(len(self.pdf_doc)):
                if page_num not in crop_rects:
                    continue

                old_page = self.pdf_doc[page_num]
                media_box = old_page.mediabox  # Get the page's media box
                scene_rect = crop_rects[page_num]
                
                # Calculate scale factor and offsets based on view mode
                if self.view_mode == 'all':
                    view = self.single_view
                    page_width = self.images[page_num].width()
                    page_height = self.images[page_num].height()
                    x_offset = (max_width - page_width) // 2
                    y_offset = (max_height - page_height) // 2
                else:
                    view = self.odd_view if page_num % 2 == 0 else self.even_view
                    max_width = max(img.width() for img in self.images[page_num % 2::2])
                    max_height = max(img.height() for img in self.images[page_num % 2::2])
                    page_width = self.images[page_num].width()
                    page_height = self.images[page_num].height()
                    x_offset = (max_width - page_width) // 2
                    y_offset = (max_height - page_height) // 2
                
                # Calculate scale factors for both dimensions
                scale_factor_x = media_box.width / page_width
                scale_factor_y = media_box.height / page_height
                
                # Adjust crop coordinates by subtracting the offset and scaling
                crop_x0 = (scene_rect.x() - x_offset) * scale_factor_x + media_box.x0
                crop_y0 = (scene_rect.y() - y_offset) * scale_factor_y + media_box.y0
                crop_x1 = crop_x0 + (scene_rect.width() * scale_factor_x)
                crop_y1 = crop_y0 + (scene_rect.height() * scale_factor_y)
                
                # Ensure crop rectangle stays within media box bounds
                crop_x0 = max(media_box.x0, min(crop_x0, media_box.x1))
                crop_y0 = max(media_box.y0, min(crop_y0, media_box.y1))
                crop_x1 = max(media_box.x0, min(crop_x1, media_box.x1))
                crop_y1 = max(media_box.y0, min(crop_y1, media_box.y1))
                
                crop_rect = fitz.Rect(crop_x0, crop_y0, crop_x1, crop_y1)

                new_doc.insert_pdf(self.pdf_doc, from_page=page_num, to_page=page_num)
                new_page = new_doc[-1]
                new_page.set_cropbox(crop_rect)

            # Save to temporary file
            temp_fd, temp_path = tempfile.mkstemp(suffix='.pdf')
            os.close(temp_fd)
            
            new_doc.save(temp_path, garbage=4, deflate=True, clean=True)
            new_doc.close()
            
            # Clean up old temporary file if exists
            if self.pdf_path and self.pdf_path.endswith('.temp.pdf'):
                try:
                    os.remove(self.pdf_path)
                except:
                    pass

            # Load the new document
            self.pdf_path = temp_path
            self.pdf_doc = fitz.open(temp_path)  # Open new document before closing old one
            self.loadPDF(temp_path)

        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to crop PDF: {str(e)}")
            import traceback
            traceback.print_exc()

    def deleteSelectedPages(self):
        if not self.pdf_doc:
            QMessageBox.warning(self, "Warning", "No PDF is open.")
            return
            
        selected_pages = sorted(self.getSelectedPages(), reverse=True)
        if not selected_pages:
            QMessageBox.warning(self, "Warning", "Please select pages to delete.")
            return

        try:
            for page_num in selected_pages:
                self.pdf_doc.delete_page(page_num)
                self.images.pop(page_num)

            self.updateThumbnails()
            self.updateOverlay()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to delete pages: {str(e)}")

    def savePDF(self):
        if not self.pdf_doc:
            QMessageBox.warning(self, "Warning", "No PDF is open.")
            return

        try:
            if self.save_directory:
                if self.save_filename:
                    # Use the provided filename
                    save_path = os.path.join(self.save_directory, self.save_filename)
                else:
                    # Generate filename based on input filename
                    original_name = os.path.basename(self.pdf_path)
                    base_name = os.path.splitext(original_name)[0]
                    save_path = os.path.join(self.save_directory, f"{base_name}_modified.pdf")
            else:
                save_path, _ = QFileDialog.getSaveFileName(
                    self,
                    "Save PDF",
                    "",
                    "PDF Files (*.pdf)"
                )
            
            if not save_path:
                return

            self.pdf_doc.save(
                save_path,
                garbage=4,
                deflate=True,
                clean=True
            )
            QMessageBox.information(self, "Success", "PDF saved successfully!")
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save PDF: {str(e)}")

    def resizeEvent(self, event):
        super().resizeEvent(event)
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

def main():
    # Set up argument parser
    parser = argparse.ArgumentParser(description='PDF Overlay Viewer')
    parser.add_argument('--input', type=str, help='Path to input PDF file')
    parser.add_argument('--save-to', type=str, help='Directory to save modified PDF')
    parser.add_argument('--save-as', type=str, help='Filename for the saved modified PDF')
    
    args = parser.parse_args()

    # Validate save directory if provided
    if args.save_to and not os.path.isdir(args.save_to):
        print(f"Error: Save directory '{args.save_to}' does not exist")
        sys.exit(1)

    # Validate input file if provided
    if args.input and not os.path.isfile(args.input):
        print(f"Error: Input file '{args.input}' does not exist")
        sys.exit(1)

    app = QApplication(sys.argv)
    viewer = PDFViewer(input_pdf=args.input, save_directory=args.save_to, save_filename=args.save_as)
    viewer.show()
    sys.exit(app.exec())

if __name__ == '__main__':
    main()
