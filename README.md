# PyCropPDF

A small PDF cropping tool that lets you see all pages overlaid while cropping - for removing headers, footers, and margins from multiple pages at once. Built in pure Python with no system dependencies required.

https://github.com/user-attachments/assets/0f1c7ae7-f273-4116-852d-9dc271cbc43f

## Features

- **Visual Overlay Cropping**: See all pages overlaid while selecting crop boundaries, making it easy to avoid cutting off content
- **Batch Processing**: Crop all pages, or all odd/even pages at once with a single selection
- **Preview**: Semi-transparent overlay shows content boundaries across all pages
- **Page Management**: Remove unwanted pages
- **No System Dependencies**: Pure Python implementation using PyQt6 and PyMuPDF

## Common Use Cases

- Remove headers and footers from documents
- Trim excess margins for better reading experience
- Clean up PDFs before OCR processing
- Standardize page dimensions across documents
- Remove watermarks or unwanted margins

## Installation

Clone the repository (git clone https://github.com/lukaszliniewicz/PyCropPDF), and from the PyCropPDF directory execute:

```
pip install -r requirements.txt
```

Make sure that GIT and Python are installed on your system and in PATH.
