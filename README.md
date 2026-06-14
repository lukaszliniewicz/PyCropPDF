
# PyCropPDF

A GUI application to crop PDF files. It is primarily designed for documents where multiple pages need the same cropping, such as removing headers, footers, or margins.

![python_Kk7XZgDZLZ](https://github.com/user-attachments/assets/f626c86e-8799-4f54-98e0-4342649fac64)

## Features

-   **Visual Cropping:** Draw a crop box directly on a preview of your PDF.
-   **Overlay Previews:** All pages are overlaid with transparency, making it easy to define a crop area that fits all pages.
-   **Odd/Even Page Modes:** View and crop odd and even pages separately, useful for books or two-sided documents with different layouts.
-   **Crop and Whiteout Tools:** Explicitly choose whether dragging creates a crop box or applies a whiteout.
-   **Page Selection:** Select one page, toggle pages with Ctrl, or select ranges with Shift.
-   **Page Deletion and Undo:** Remove unwanted pages and undo crop, whiteout, or deletion operations.
-   **Cross-Platform:** Built with Python and PyQt6, it runs on Windows, macOS, and Linux.

## Installation

### From PyPI (Recommended)
The easiest way to install PyCropPDF is from PyPI:
```bash
pip install pycroppdf
```

### From GitHub
You can also install the latest development version directly from GitHub. Ensure you have Python 3.8+ and git installed.
```bash
pip install git+https://github.com/lukaszliniewicz/PyCropPDF.git
```
This command will handle downloading and installing the package and its dependencies.

### Development / Editable Install
If you plan to modify the code, clone the repository and install it in "editable" mode:
```bash
git clone https://github.com/lukaszliniewicz/PyCropPDF.git
cd PyCropPDF
pip install -e .
```
This will install the package in editable mode and handle all dependencies.

## Usage

After installation, you can run the application from your terminal:

```bash
pycroppdf
```

You can also provide a PDF file to open on startup:

```bash
pycroppdf --input /path/to/your/document.pdf
```

### Programmatic Use & Command-Line Arguments

The application supports command-line arguments that can be useful in scripts or automated workflows that still require manual user input (e.g., for selecting crop boxes).

-   `--input /path/to/file.pdf`: Opens a PDF on startup.
-   `--save-to /path/to/directory/`: Sets the directory for saving the modified PDF.
-   `--save-as filename.pdf`: Sets the filename for the saved PDF.
-   `--manifest-out /path/to/output.json`: Sets the provenance sidecar path.

When `--save-to` or `--save-as` are used, the "Save" dialog is skipped, and the file is saved directly to the specified location after the user clicks "Save PDF..." in the File menu.

Every saved PDF also receives a provenance sidecar by default. It records source and output hashes, deleted pages, original-to-output page mapping, crop rectangles, and whiteout rectangles so downstream applications can retain an auditable relationship to the original PDF.

A `pycroppdf.py` script is included for backward compatibility with existing programmatic usage; it is a simple wrapper for `run.py`.

### Basic Workflow

1.  Launch the application.
2.  Open a PDF file using **File > Open PDF...** or by dragging and dropping the file onto the window.
3.  The pages will be displayed as an overlay. Use the **View** menu to switch between a single overlay for all pages or separate overlays for odd and even pages.
4.  Choose the **Crop Box** or **Whiteout** tool, then click and drag on a page preview.
5.  Click **Apply Crop** to preview the crop. Use **Reset Crop** or **Undo** to restore it.
6.  Click a thumbnail to select one page, Ctrl-click to toggle pages, or Shift-click to select a range. Selected pages limit crop and whiteout operations and can be removed with **Delete Selected Pages**.
7.  Save the modified PDF with the purple **Save PDF** button or **File > Save PDF...**.

## License

This project is licensed under the MIT License. See the `LICENSE` file for details.
