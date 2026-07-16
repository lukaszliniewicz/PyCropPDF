# PyCropPDF

PyCropPDF is a desktop PDF editor for cropping pages, adding visual masks, and redacting selected content.

![PyCropPDF screenshot](https://github.com/user-attachments/assets/f626c86e-8799-4f54-98e0-4342649fac64)

## Install

PyCropPDF requires Python 3.10 or later.

```bash
pip install pycroppdf
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
2. Choose an overlay view for all pages, separate odd/even pages, or a single page from the thumbnail list.
3. Select thumbnails only when deleting pages. Use `Ctrl`/`Cmd`-click to toggle pages and `Shift`-click to select a range.
4. Draw visual masks or redactions first. To make further masks or redactions after cropping, click **Reset Crop**.
5. Select **Crop Box**, draw a rectangle, then click **Apply Crop**. Odd/even crop rectangles share one size but keep independent positions.
6. Use **Undo** or `Ctrl`+`Z` to revert an edit, then click **Save PDF**.

Crop, mask, and redaction operations apply to every page. Odd/even crop rectangles keep separate positions and apply to their respective page groups.

## Tools

| Tool | Effect |
| --- | --- |
| **Crop Box** | Changes the PDF CropBox. The content outside the visible area remains in the file. |
| **Visual Mask** | Draws a colored rectangle over page content. The covered content remains in the file. |
| **Redact** | Removes text and overlapping images or graphics in the selected rectangle. It does not remove metadata, attachments, or copies of the same information elsewhere in the document. |

Use **Redact** when the content itself must be removed. Crop boxes and visual masks only change presentation.

## Provenance manifest

Each save writes a JSON sidecar manifest unless a different path is supplied with `--manifest-out`. It records source and output SHA-256 hashes, page mapping and deletions, plus the crop, mask, and redaction rectangles applied.

## Development

```bash
pytest
ruff check .
```

Build a standalone executable with PyInstaller:

```bash
pyinstaller --onefile --noconsole --name PyCropPDF run.py
```

## License

[MIT](LICENSE)
