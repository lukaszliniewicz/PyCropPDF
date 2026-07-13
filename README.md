# PyCropPDF

PyCropPDF is a desktop application for cropping, visually masking, and redacting PDF files. It is particularly suited for documents where multiple pages share a common layout structure—such as scanned books, academic papers, or reports—allowing you to hide margins, headers, footers, or watermarks across many pages simultaneously.

![PyCropPDF Screenshot](https://github.com/user-attachments/assets/f626c86e-8799-4f54-98e0-4342649fac64)

## Key Concepts

### 1. Transparent Overlay Preview
Instead of showing pages one-by-one, PyCropPDF renders and overlays page previews transparently on top of each other. This enables you to visually check that a selected crop boundary or whiteout mask fits every page in the document (or group of pages) without clipping text or diagrams.

### 2. View Modes
- **All Pages (Overlay)**: Overlays all document pages together. Helpful for verifying margins across the entire document.
- **Odd / Even Pages**: Splits the preview into two separate overlays—one for odd pages and one for even pages. This is useful for double-sided documents (like bound books) where the left and right margins alternate.
- **Single Page Preview**: Focuses on a single page, allowing you to fine-tune boundaries for specific layout exceptions.

### 3. Crop, Mask, and Redact Tools
- **Crop Box**: Defines the PDF's visible area. Cropping changes the page CropBox, so hidden source content remains in the file and can be restored by another PDF tool.
- **Visual Mask**: Applies a solid rectangular overlay to cover page content. You can choose a custom color to match the page background. A visual mask does **not** remove underlying text or images.
- **Redact**: Permanently removes page text and overlapping graphics/images in the selected rectangle. Redaction does not remove document metadata, attachments, or information duplicated elsewhere in the PDF.

Use **Redact** for content that must not remain extractable. Cropping and visual masking are presentation tools, not secure redaction.

### 4. Page Selection & Deletion
A sidebar displays thumbnails of all pages. 
- Multi-selection is supported using `Ctrl + Click` (toggle individual pages) and `Shift + Click` (select a range of pages).
- Operations (cropping, visual masking, and redaction) can be limited to the selected pages.
- Selected pages can be deleted from the document.
- Operations can be undone step-by-step.

### 5. Provenance Manifests
When you save a modified PDF, the application generates a JSON sidecar manifest (e.g., `document_modified.pdf.pycroppdf.json`). This manifest records:
- SHA-256 hashes of the source and output PDF files.
- Original page counts and mapping of output pages to original pages.
- Explicit lists of deleted page indices.
- Exact coordinates and dimensions of crops, visual masks, and secure redactions applied.

This manifest enables downstream automated pipelines to trace the history and modifications of the edited PDF back to its original source.

---

## Installation

### Prerequisites
- Python 3.10 or higher.

### Installation from PyPI
You can install PyCropPDF directly using pip:
```bash
pip install pycroppdf
```

### Installation from Source (Development Mode)
To clone the repository and install it in editable mode with the test and lint tools:
```bash
git clone https://github.com/lukaszliniewicz/PyCropPDF.git
cd PyCropPDF
pip install -e .[dev]
```

---

## Usage

Start the graphical interface from the terminal:
```bash
pycroppdf
```

To start the interface with a PDF already loaded:
```bash
pycroppdf --input /path/to/document.pdf
```

### Command-Line Arguments & Automation
The application accepts arguments to pre-configure paths and manifest outputs:
- `--input /path/to/file.pdf`: Loads the specified PDF at launch.
- `--save-to /path/to/directory/`: Specifies the folder where the edited PDF will be saved.
- `--save-as filename.pdf`: Specifies the filename for the output PDF.
- `--manifest-out /path/to/output.json`: Overrides the default location for the JSON provenance manifest.

*Note: If `--save-to` or `--save-as` is specified, the standard "Save File" dialog is bypassed. Clicking "Save PDF..." immediately saves the file to the pre-configured path.*

### Basic Edit Workflow
1. **Open a PDF**: Drag and drop a PDF file into the window, or go to **File > Open PDF...**.
2. **Select View Mode**: Choose **All**, **Odd/Even**, or click a page thumbnail to view a single page.
3. **Apply a Crop**: Select the **Crop Box** tool, click and drag a box on the canvas, and click **Apply Crop**. If odd/even mode is active, you can define separate boxes for odd and even pages.
4. **Mask or Redact**: Select **Visual Mask** to cover content without removing it, or **Redact** to remove page content. Click and drag over the target area.
5. **Manage Pages**: Select thumbnails in the sidebar to delete unnecessary pages.
6. **Undo Changes**: Use the **Undo** button or `Ctrl + Z` to revert your actions.
7. **Save**: Click **Save PDF** to export the modified document and its provenance manifest.

---

## Development & Building

### Running Unit Tests
Install the development extra and execute checks from the project root:
```bash
pip install -e .[dev]
pytest
ruff check .
```

### Building a Standalone Executable
You can compile PyCropPDF into a standalone executable using `pyinstaller`. From your environment, run:
```bash
pyinstaller --onefile --noconsole --name PyCropPDF run.py
```
This produces a single, self-contained executable file inside the `dist/` directory.

---

## License
This project is licensed under the MIT License. See the `LICENSE` file for details.
