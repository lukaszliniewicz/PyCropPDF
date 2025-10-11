
# PyCropPDF

A GUI application to crop PDF files. It is primarily designed for documents where multiple pages need the same cropping, such as removing headers, footers, or margins.

## Features

-   **Visual Cropping:** Draw a crop box directly on a preview of your PDF.
-   **Overlay Previews:** All pages are overlaid with transparency, making it easy to define a crop area that fits all pages.
-   **Odd/Even Page Modes:** View and crop odd and even pages separately, useful for books or two-sided documents with different layouts.
-   **Page Deletion:** Select and remove unwanted pages.
-   **Cross-Platform:** Built with Python and PyQt6, it runs on Windows, macOS, and Linux.

## Installation

1.  Ensure you have Python 3.8 or newer installed.
2.  Install the dependencies listed in `requirements.txt`:

    ```bash
    pip install -r requirements.txt
    ```

## Usage

To run the application, execute the `run.py` script from the `PyCropPDF` directory:

```bash
python run.py
```

You can also provide a PDF file to open on startup:

```bash
python run.py --input /path/to/your/document.pdf
```

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
