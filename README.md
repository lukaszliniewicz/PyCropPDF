# PyCropPDF

PyCropPDF is a user-friendly utility for cropping PDF documents. It excels at tasks like removing headers, footers, or margins across multiple pages simultaneously. By overlaying pages, it allows you to define a crop area with precision, ensuring consistency throughout your document. It supports different crop areas for odd and even pages, catering to documents with mirrored margin layouts.

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

### From source (for development)

Clone the repository:
```bash
git clone https://github.com/lukaszliniewicz/PyCropPDF.git
```
Navigate into the directory and install the package in editable mode. This will also install all necessary dependencies.
```bash
cd PyCropPDF
pip install -e .
```

### As a package (recommended)

You can install the package directly from GitHub using pip:
```bash
pip install git+https://github.com/lukaszliniewicz/PyCropPDF.git
```
This will install the application and make the `pycroppdf` command available in your system's PATH.

## Usage

### Running from source
After following the installation steps for running from source, you can run the application directly. As the package is installed in editable mode, the `pycroppdf` command will also work.

```bash
python run.py
# or
pycroppdf
```
<<<<<<< HEAD
You can also provide a PDF file to open on startup:
```bash
pycroppdf --input /path/to/your.pdf
```

### As a package
=======

### As an installed package
>>>>>>> 0e5eed5418b7fe375f40f725ba3df7034150a1b1
After installing the package, you can run the application from any directory:
```bash
pycroppdf
```
Or with arguments:
```bash
pycroppdf --input /path/to/your.pdf
```
<<<<<<< HEAD

## Legacy Usage

For backward compatibility, a `pycroppdf.py` script is maintained at the root of the repository. This script is a wrapper that ensures older programmatic workflows continue to function after recent project restructuring. It is considered deprecated and will be removed in a future version.
=======
>>>>>>> 0e5eed5418b7fe375f40f725ba3df7034150a1b1
