# PyCropPDF

PyCropPDF is a desktop PDF editor for cropping, rotating, and visually covering PDF pages.

![PyCropPDF screenshot](https://github.com/user-attachments/assets/f626c86e-8799-4f54-98e0-4342649fac64)

## Install

### Standalone builds

The [latest release](https://github.com/lukaszliniewicz/PyCropPDF/releases/latest) provides
x86_64 builds for Windows (`.exe`) and Linux (`.AppImage`). Both include Auto deskew.

On Linux:

```bash
chmod +x PyCropPDF-*-x86_64.AppImage
./PyCropPDF-*-x86_64.AppImage
```

AppImage normally uses FUSE 2. If FUSE is unavailable, run it without mounting:

```bash
APPIMAGE_EXTRACT_AND_RUN=1 ./PyCropPDF-*-x86_64.AppImage
```

### PyPI

PyCropPDF requires Python 3.11 or later.

```bash
python -m pip install pycroppdf
```

Auto deskew is optional because it adds NumPy and scikit-image:

```bash
python -m pip install "pycroppdf[deskew]"
```

## Run

```bash
pycroppdf
pycroppdf --input /path/to/document.pdf
```

Optional arguments:

- `--save-to DIRECTORY` saves to that directory instead of opening a Save dialog.
- `--save-as NAME.pdf` sets the output name when used with `--save-to`. Without it, `--save-to` writes `<input>_modified.pdf`.
- `--manifest-out PATH` sets the JSON provenance-manifest path. By default it is saved next to the PDF as `<output>.pycroppdf.json`.

## Edit a PDF

1. Open a PDF with **File > Open PDF...** or drag it into the window.
2. Use **View** to choose one overlay for all pages or separate odd/even overlays.
3. Click a thumbnail image to preview that page. Click it again or use **Stack** to return. Thumbnail checkboxes select pages for deletion; `Ctrl`/`Cmd` toggles and `Shift` selects a range.
4. Open **Rotate**, choose all, odd, even, or the previewed page, and enter an angle. Click **Preview** to inspect a fine rotation, **Discard** to clear it, or **Apply** to change the PDF. The 90-degree buttons apply immediately; **Auto deskew** detects and corrects small angles per page.
5. Click **Cover**, choose **All**, **Odd**, or **Even**, then draw the cover. In a page preview, **This page only** overrides that scope. New covers are added without replacing existing ones and do not remove the underlying PDF content.
6. Click **Crop**, draw a crop box, then click **Apply Crop**. Odd/even positions are independent and their sizes stay uniform. To override one page, preview it, enable **This page only**, draw its crop box, and apply the crop again.
7. Use **Undo** or `Ctrl`+`Z` to revert an edit, then click **Save**.

Crop applies to all pages. Cover uses its selected scope and remains additive. A per-page crop override replaces the stack crop only for that page.

Rotate and add covers before cropping. If a crop preview is active, reset it before using those tools. For pages with links, annotations, or form fields, the app warns before a fine-angle rotation because some appearances or destinations cannot be transformed exactly.

## Tools

| Tool | Effect |
| --- | --- |
| **Crop Box** | Changes the PDF CropBox. The content outside the visible area remains in the file. |
| **Cover** | Adds a rectangle to all, odd, even, or only the previewed page. Existing covers and covered content remain in the file. |
| **Rotation** | Rotates page content without rasterizing it. Exact 90-degree turns use PDF rotation metadata. |
| **Auto deskew** | Detects a small text-line angle independently for each target page. Requires `pycroppdf[deskew]`. |

Crop boxes and covers change presentation; they do not securely remove content.
Larger Auto deskew jobs use one fewer worker than the detected CPU count. Two-core systems use both cores. Small jobs stay in one process to avoid startup overhead.

## Provenance manifest

Each save writes a JSON sidecar manifest unless a different path is supplied with `--manifest-out`. It records source and output SHA-256 hashes, page mapping and deletions, rotations, and crop and cover rectangles.

## Development

```bash
git clone https://github.com/lukaszliniewicz/PyCropPDF.git
cd PyCropPDF
python -m pip install -e ".[dev,deskew]"
pytest
ruff check .
ruff format --check .
```

Build a standalone Windows executable with PyInstaller:

```powershell
python -m pip install -e ".[build,deskew]"
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_windows.ps1
```

Install `pycroppdf[deskew]` in the build environment to include **Auto deskew**. The build script supplies the PyInstaller hook needed by scikit-image's runtime package checks. Deskew roughly doubles the executable size.

## License

[MIT](LICENSE)
