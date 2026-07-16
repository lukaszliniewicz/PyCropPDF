# PyCropPDF

PyCropPDF is a desktop PDF editor for cropping, rotating, and visually covering PDF pages.

![PyCropPDF screenshot](https://github.com/user-attachments/assets/f626c86e-8799-4f54-98e0-4342649fac64)

## Install

PyCropPDF requires Python 3.11 or later.

```bash
pip install pycroppdf
```

Automatic deskew is optional because it adds NumPy and scikit-image:

```bash
pip install "pycroppdf[deskew]"
```

For development:

```bash
git clone https://github.com/lukaszliniewicz/PyCropPDF.git
cd PyCropPDF
pip install -e ".[dev]"
```

## Run

```bash
pycroppdf
pycroppdf --input /path/to/document.pdf
```

Optional arguments:

- `--save-to DIRECTORY` saves to that directory instead of opening a Save dialog.
- `--save-as NAME.pdf` sets the output name when used with `--save-to`; otherwise the default is `<input>_modified.pdf`.
- `--manifest-out PATH` sets the JSON provenance-manifest path. By default it is saved next to the PDF as `<output>.pycroppdf.json`.

## Edit a PDF

1. Open a PDF with **File > Open PDF...** or drag it into the window.
2. Use **View** to choose one overlay for all pages or separate odd/even overlays.
3. Click a thumbnail image to preview that page. Click it again or use **Stack** to return. Thumbnail checkboxes select pages for deletion; `Ctrl`/`Cmd` toggles and `Shift` selects a range.
4. Click **Crop** to open its toolbar, then draw a crop box. Odd/even positions are independent; their sizes stay uniform. In a page preview, enable **This page** before changing that page's crop box.
5. Click **Cover** to open its toolbar. Choose a color or pick one from the page, then draw the cover. This changes presentation only; the underlying PDF content remains.
6. Open **Rotate**, choose all, odd, even, or the previewed page, and enter an angle. Click **Preview** to inspect a fine rotation, **Discard** to clear it, or **Apply** to change the PDF. The 90-degree buttons apply immediately; **Auto deskew** detects and corrects small angles per page.
7. Use **Undo** or `Ctrl`+`Z` to revert an edit, then click **Save**.

Crop and Cover resolve across all pages. A per-page crop override replaces the stack crop only for that page.

Rotate before cropping. For pages with links, annotations, or form fields, the app warns before a fine-angle rotation because some appearances or destinations cannot be transformed exactly.

## Tools

| Tool | Effect |
| --- | --- |
| **Crop Box** | Changes the PDF CropBox. The content outside the visible area remains in the file. |
| **Cover** | Draws a rectangle in the selected color. The covered content remains in the file. |
| **Rotation** | Rotates page content without rasterizing it. Exact 90-degree turns use PDF rotation metadata. |
| **Auto Deskew** | Detects a small text-line angle independently for each target page. Requires `pycroppdf[deskew]`. |

Crop boxes and covers change presentation; they do not securely remove content.
Larger deskew jobs use one fewer worker than the detected CPU count. Two-core systems use both cores. Small jobs stay in one process to avoid startup overhead.

## Provenance manifest

Each save writes a JSON sidecar manifest unless a different path is supplied with `--manifest-out`. It records source and output SHA-256 hashes, page mapping and deletions, rotations, and crop and cover rectangles.

## Development

```bash
pytest
ruff check .
```

Build a standalone Windows executable with PyInstaller:

```powershell
pip install -e ".[build,deskew]"
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_windows.ps1
```

Install `pycroppdf[deskew]` in the build environment to include **Auto Deskew**. The build script supplies the PyInstaller hook needed by scikit-image's runtime package checks. Deskew roughly doubles the executable size.

## License

[MIT](LICENSE)
