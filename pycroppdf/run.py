import argparse
import os
import sys
import multiprocessing

from PyQt6.QtWidgets import QApplication

from .main_window import PDFViewer


def main():
    multiprocessing.freeze_support()
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
