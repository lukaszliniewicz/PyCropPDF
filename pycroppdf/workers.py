import concurrent.futures
import os
import tempfile
import traceback

import fitz
from PyQt6.QtCore import QObject, QRunnable, pyqtSignal
from PyQt6.QtGui import QImage


class WorkerSignals(QObject):
    """
    Defines the signals available from a running worker thread.
    """
    finished = pyqtSignal()
    error = pyqtSignal(str)
    result = pyqtSignal(object)


def _translate_rect_to_pdf_coords(scene_rect, page_dims, pdf_page_rect, page_num, 
                                  view_mode, max_dims, max_odd_dims, max_even_dims):
    """Translates a QRectF from scene coordinates to a fitz.Rect in PDF coordinates (visual)."""
    page_width, page_height = page_dims
    
    if view_mode == 'all':
        current_max_dims = max_dims
    else:
        # page_num 0 is the first page, which is ODD
        if page_num % 2 == 0:  # Odd page
            current_max_dims = max_odd_dims
        else:  # Even page
            current_max_dims = max_even_dims
    
    max_width, max_height = current_max_dims
    x_offset = (max_width - page_width) // 2
    y_offset = (max_height - page_height) // 2
    
    # Use the provided pdf_page_rect (usually page.rect) which matches the visual orientation
    ref_rect = pdf_page_rect
    scale_factor_x = ref_rect.width / page_width if page_width > 0 else 0
    scale_factor_y = ref_rect.height / page_height if page_height > 0 else 0

    crop_x0 = (scene_rect.x() - x_offset) * scale_factor_x + ref_rect.x0
    crop_y0 = (scene_rect.y() - y_offset) * scale_factor_y + ref_rect.y0
    crop_x1 = crop_x0 + (scene_rect.width() * scale_factor_x)
    crop_y1 = crop_y0 + (scene_rect.height() * scale_factor_y)

    crop_x0 = max(ref_rect.x0, min(crop_x0, ref_rect.x1))
    crop_y0 = max(ref_rect.y0, min(crop_y0, ref_rect.y1))
    crop_x1 = max(ref_rect.x0, min(crop_x1, ref_rect.x1))
    crop_y1 = max(ref_rect.y0, min(crop_y1, ref_rect.y1))
    return fitz.Rect(crop_x0, crop_y0, crop_x1, crop_y1)


def _render_page_task(args):
    """Renders a single PDF page. For use with ProcessPoolExecutor."""
    pdf_bytes, page_num, zoom_matrix, crop_args = args
    pdf_doc = fitz.open("pdf", pdf_bytes)
    page = pdf_doc[page_num]

    clip_rect = None
    if crop_args:
        scene_rect = crop_args.get('rect')
        if scene_rect:
            visual_rect = _translate_rect_to_pdf_coords(
                scene_rect,
                crop_args['page_dims'],
                page.rect,
                page_num,
                crop_args['view_mode'],
                crop_args['max_dims'],
                crop_args['max_odd_dims'],
                crop_args['max_even_dims']
            )
            # Transform visual rect to physical coordinates for clip
            clip_rect = visual_rect * page.derotation_matrix

    pix = page.get_pixmap(matrix=zoom_matrix, clip=clip_rect)
    # Return picklable data, not QImage
    result = (page_num, pix.samples, pix.width, pix.height, pix.stride)
    pdf_doc.close()
    return result




class RenderAllPagesWorker(QRunnable):
    """
    Worker thread for rendering all PDF pages in parallel.
    """
    def __init__(self, pdf_bytes, num_pages, crop_info=None):
        super().__init__()
        self.signals = WorkerSignals()
        self.pdf_bytes = pdf_bytes
        self.num_pages = num_pages
        self.crop_info = crop_info

    def run(self):
        try:
            zoom_matrix = fitz.Matrix(1.5, 1.5)
            
            crop_args_list = [None] * self.num_pages
            if self.crop_info:
                all_page_dims = self.crop_info['image_dims']
                view_mode = self.crop_info['view_mode']
                
                max_dims, max_odd_dims, max_even_dims = (0,0), (0,0), (0,0)
                max_width = max(w for w, h in all_page_dims) if all_page_dims else 0
                max_height = max(h for w, h in all_page_dims) if all_page_dims else 0

                if view_mode == 'all':
                    max_dims = (max_width, max_height)
                else:
                    # For split view, both odd and even views are rendered on a canvas
                    # sized to the max dimensions of ALL pages to ensure consistency.
                    # We must use the same max dimensions here for coordinate translation.
                    max_odd_dims = (max_width, max_height)
                    max_even_dims = (max_width, max_height)

                for i in range(self.num_pages):
                    crop_args_list[i] = {
                        'rect': self.crop_info['rects'].get(i),
                        'page_dims': all_page_dims[i],
                        'view_mode': view_mode,
                        'max_dims': max_dims,
                        'max_odd_dims': max_odd_dims,
                        'max_even_dims': max_even_dims,
                    }

            tasks = [(self.pdf_bytes, i, zoom_matrix, crop_args_list[i]) for i in range(self.num_pages)]

            with concurrent.futures.ProcessPoolExecutor() as executor:
                for page_num, samples, width, height, stride in executor.map(_render_page_task, tasks):
                    img = QImage(samples, width, height, stride, QImage.Format.Format_RGB888)
                    self.signals.result.emit((page_num, img))
        except Exception:
            self.signals.error.emit(traceback.format_exc())
        finally:
            self.signals.finished.emit()


class SaveWorker(QRunnable):
    """
    Worker thread for saving PDF.
    """
    def __init__(self, pdf_bytes, save_path, crop_info=None, deflate=False):
        super().__init__()
        self.signals = WorkerSignals()
        self.pdf_bytes = pdf_bytes
        self.save_path = save_path
        self.crop_info = crop_info
        self.deflate = deflate

    def run(self):
        try:
            doc = fitz.open("pdf", self.pdf_bytes)

            # If crop is active, apply it to each page
            if self.crop_info:
                all_page_dims = self.crop_info['image_dims']
                view_mode = self.crop_info['view_mode']
                crop_rects = self.crop_info['rects']
                
                max_dims, max_odd_dims, max_even_dims = (0,0), (0,0), (0,0)
                max_width = max(w for w, h in all_page_dims) if all_page_dims else 0
                max_height = max(h for w, h in all_page_dims) if all_page_dims else 0

                if view_mode == 'all':
                    max_dims = (max_width, max_height)
                else:
                    # For split view, both odd and even views are rendered on a canvas
                    # sized to the max dimensions of ALL pages to ensure consistency.
                    # We must use the same max dimensions here for coordinate translation.
                    max_odd_dims = (max_width, max_height)
                    max_even_dims = (max_width, max_height)

                for page_num, page in enumerate(doc):
                    if page_num in crop_rects and crop_rects[page_num]:
                        scene_rect = crop_rects[page_num]
                        page_dims = all_page_dims[page_num]
                        
                        visual_rect = _translate_rect_to_pdf_coords(
                            scene_rect, page_dims, page.rect, page_num, view_mode,
                            max_dims, max_odd_dims, max_even_dims
                        )
                        # Transform visual rect to physical coordinates
                        crop_rect = visual_rect * page.derotation_matrix
                        page.set_cropbox(crop_rect)
            
            doc.save(self.save_path, garbage=2, deflate=self.deflate)
            doc.close()
            self.signals.result.emit(True)

        except Exception:
            self.signals.error.emit(traceback.format_exc())
        finally:
            self.signals.finished.emit()
