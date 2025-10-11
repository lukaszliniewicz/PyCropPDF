# PyCropPDF

A PDF cropping utility for removing headers, footers, and margins from multiple pages at once. It provides a visual overlay of pages to help define a consistent crop area.

https://github.com/user-attachments/assets/0f1c7ae7-f273-4116-852d-9dc271cbc43f

## Features

- **Overlay Cropping**: Select a crop area while viewing a semi-transparent overlay of all pages.
- **View Modes**: Switch between a single overlay of all pages or separate overlays for odd and even pages. This allows for applying different crop areas to odd and even pages (e.g., for documents with different margin layouts).
- **Interactive Crop Area**: The crop selection can be moved and resized after being drawn.
- **Page Management**:
    - **Deletion**: Remove selected pages from the document.
    - **Individual Preview**: Click a thumbnail to view a single page without the overlay.
- **Navigation**:
    - **Pan**: Hold the `Spacebar` key and drag to move around the page view.
    - **Zoom**: Use the mouse wheel to zoom in and out.
- **File Handling**:
    - **Drag and Drop**: Open PDF files by dragging them into the application window.
    - **Fast Save**: An option to save without compression for increased speed.
- **Command-line Interface**: Open files and specify save locations directly from the command line.

## Installation

Clone the repository:

```
git clone https://github.com/lukaszliniewicz/PyCropPDF
```
In the PyCropPDF directory execute:

```
pip install -r requirements.txt
```

Make sure that GIT and Python are installed on your system and in PATH.

## Usage

To run the application, execute:
```
python run.py
```
You can also provide a PDF file to open on startup:
```
python run.py --input /path/to/your.pdf
```
