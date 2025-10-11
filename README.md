
# PyCropPDF

A GUI application to crop PDF files. It is primarily designed for documents where multiple pages need the same cropping, such as removing headers, footers, or margins.

![python_Kk7XZgDZLZ](https://github.com/user-attachments/assets/f626c86e-8799-4f54-98e0-4342649fac64)

## Features

-   **Visual Cropping:** Draw a crop box directly on a preview of your PDF.
-   **Overlay Previews:** All pages are overlaid with transparency, making it easy to define a crop area that fits all pages.
-   **Odd/Even Page Modes:** View and crop odd and even pages separately, useful for books or two-sided documents with different layouts.
-   **Page Deletion:** Select and remove unwanted pages.
-   **Cross-Platform:** Built with Python and PyQt6, it runs on Windows, macOS, and Linux.

## Installation

You can install PyCropPDF directly from GitHub. Ensure you have Python 3.8+ and git installed.

### Recommended Method
Use pip to install directly from the repository:
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

When `--save-to` or `--save-as` are used, the "Save" dialog is skipped, and the file is saved directly to the specified location after the user clicks "Save PDF..." in the File menu.

A `pycroppdf.py` script is included for backward compatibility with existing programmatic usage; it is a simple wrapper for `run.py`.

### Basic Workflow

1.  Launch the application.
2.  Open a PDF file using **File > Open PDF...** or by dragging and dropping the file onto the window.
3.  The pages will be displayed as an overlay. Use the **View** menu to switch between a single overlay for all pages or separate overlays for odd and even pages.
4.  Click and drag on a page preview to draw a crop box. Adjust the box by dragging its edges or corners.
5.  Click the **Crop Selection** button to apply the crop. A preview of the cropped pages will be shown.
6.  Use the checkboxes next to the page thumbnails to select pages for deletion, then click **Delete Selected Pages**.
7.  Save the modified PDF using **File > Save PDF...**.

## License

This project is licensed under the MIT License. See the `LICENSE` file for details.
