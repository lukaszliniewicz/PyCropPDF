import argparse
import multiprocessing
import os
import sys


def main():
    multiprocessing.freeze_support()
    # Set up argument parser
    parser = argparse.ArgumentParser(description="PyCropPDF: crop, rotate, and cover PDFs")
    parser.add_argument("--input", type=str, help="Path to input PDF file")
    parser.add_argument("--save-to", type=str, help="Directory to save modified PDF")
    parser.add_argument("--save-as", type=str, help="Filename for the saved modified PDF")
    parser.add_argument(
        "--manifest-out", type=str, help="Write a JSON provenance manifest when the PDF is saved"
    )
    parser.add_argument("--check-deskew", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--check-deskew-workers", action="store_true", help=argparse.SUPPRESS)

    args = parser.parse_args()

    if args.check_deskew:
        from .rotation import deskew_available

        if deskew_available():
            print("deskew runtime available")
            return
        print("deskew runtime unavailable", file=sys.stderr)
        raise SystemExit(2)

    if args.check_deskew_workers:
        import fitz

        from .rotation import deskew_available, deskew_pdf_bytes

        if not deskew_available():
            print("deskew runtime unavailable", file=sys.stderr)
            raise SystemExit(2)
        document = fitz.open()
        try:
            for page_number in range(2):
                page = document.new_page(width=300, height=400)
                for y in range(50, 350, 25):
                    page.insert_text((30, y), f"Parallel deskew check {page_number + 1}")
            deskew_pdf_bytes(document.tobytes(), range(2), max_workers=2)
        finally:
            document.close()
        print("parallel deskew runtime available")
        return

    # Validate save directory if provided
    if args.save_to and not os.path.isdir(args.save_to):
        print(f"Error: Save directory '{args.save_to}' does not exist")
        sys.exit(1)

    # Validate input file if provided
    if args.input and not os.path.isfile(args.input):
        print(f"Error: Input file '{args.input}' does not exist")
        sys.exit(1)

    from PyQt6.QtWidgets import QApplication

    from .main_window import PDFViewer

    app = QApplication(sys.argv)
    viewer = PDFViewer(
        input_pdf=args.input,
        save_directory=args.save_to,
        save_filename=args.save_as,
        manifest_path=args.manifest_out,
    )
    viewer.show()
    sys.exit(app.exec())
