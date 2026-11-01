# PyCropPDF review — 1 November 2026

Reviewed baseline: `b2f96d71cd532369f2644ad2aafa84a5f4eb8404` (version 0.4.4).
Changes are local on `codex/review-and-reliability`; no release or remote CI run was performed.

This is the original review snapshot. Subsequent work and current verification are
tracked in [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).

The app has useful foundations: native vector-preserving PDF edits, separate background
workers, operation IDs to reject stale callbacks, rollback around destructive edits,
disk-backed undo, and source-snapshot hashes in provenance. The main priorities are
geometry correctness, bounded preview memory, and clearer editing scope. A wholesale
rewrite would discard useful regression coverage without solving those problems.

## Implemented and verified

| Problem | Result | Evidence |
| --- | --- | --- |
| A manifest destination could alias the output or source PDF. JSON could replace a PDF while saving reported success. | Reject aliases before writes, including normalized paths, symlinked parents, and existing hardlinks. Intentional PDF-over-source saving remains supported with a separate manifest. | `tests/test_workers_safety.py`: destination preservation, signals, valid output hashes. |
| Pending crop rectangles were passed directly to the rasterizer in the wrong coordinate space. Existing CropBoxes and page rotations produced incorrect previews. | Convert the validated crop to displayed page coordinates before rasterizing. | Pixel and dimension checks at 0/90/180/270 degrees, including asymmetric subcrops and colored markers. |
| Covers on existing CropBoxes could land away from the selection. Rotated, offset CropBoxes also expose a different PyMuPDF drawing transform. | Remove the CropBox offset for drawing; temporarily clear rotation and restore its metadata in `finally`. Provenance retains the original crop-space rectangle. | Rendered pixels and preserved rotation checked for all four right-angle rotations. |
| Translucent white page backgrounds washed out earlier pages' marks. After 20 pages, an isolated black marker became RGB 251/251/251. | Use an ink-preserving Darken composite. The all-page, odd/even, and rotation-preview paths share the compositor. | A 100-page test preserves first/last markers in either order; mixed page sizes remain centered. |
| Rendering processes inherited Linux's `fork` context from a Qt worker thread. The baseline emitted nine multithreaded-fork warnings. | Use explicit `spawn`, matching deskew. | Real guarded subprocess test plus the complete GUI regression suite. A deadlock was not reproduced. |
| The legacy `fitz` import is deprecated by the current PDF library. | Import `pymupdf` under the existing internal alias throughout application code and tests. | Current, minimum, and PyMuPDF 2.0 test environments all pass. |

The overlay is deliberately denser now: marks on any page remain visible. Individual
page preview remains the way to inspect a page without the stack composite.

## Static-check policy

[CONTRIBUTING.md](CONTRIBUTING.md) documents the policy; [AGENTS.md](AGENTS.md) applies it
to future agent work. Run:

```sh
uv sync --locked --extra dev --extra deskew
uv run --no-sync python scripts/check.py
uv run --no-sync python -m pytest -q
```

The command gates Ruff lint/format, Basedpyright, Vulture, and Import Linter.
Vulture includes 60%-confidence unused functions/methods, with explicit references for
Qt callbacks and compatibility aliases. Import Linter checks both cycles and layer
direction; its exhaustive contract requires new modules to be assigned a layer.
CI configuration adds Linux static checks and Linux/Windows tests on Python 3.11/3.13.
The workflow passes Actionlint locally; execution on GitHub remains unverified.

Basedpyright uses `standard` mode and checks every application module. Its initial
inventory contains **113 legacy diagnostics**: 76 in `main_window.py`, 31 in
`widgets.py`, and 6 in `rotation.py`. This includes genuine typing gaps, the canvas
attribute shadowing Qt's `scene()` method, and Qt/PyMuPDF typing friction. A green
check means no diagnostics beyond that inventory; it does not mean fully typed code.
The normal check never rewrites the baseline. See the documented rules for shrinking
it and reviewing deliberate rule changes.

In a disposable copy, injected lint, type, unused-function, circular-import, and
layer-direction violations each failed their corresponding check. The baseline was
unchanged by these checks.

## Next priorities

| Priority | Finding | Evidence and next action |
| --- | --- | --- |
| High | Preview storage grows with every full-resolution page. | `reloadImages()` retains all `QImage`s; `updateThumbnails()` creates every thumbnail widget; the render worker retains all submitted futures. A 100-page text fixture retains 322.8 MiB of image pixels alone. Bound in-flight results and page-image caching, render thumbnails separately, and maintain composites incrementally without losing marks from any page. |
| High | Long operations offer no cancellation and block closing. | `setUIProcessing()` disables the interface; `closeEvent()` requires waiting. Add progress and safe cancellation to rendering/deskew, with operation-generation checks and unchanged document state after cancellation. |
| Medium | The nominal deskew raster limit is not a hard limit. | `rotation.py:366` clamps the scale to at least 0.5. A side above 2,800 points therefore exceeds `DESKEW_RENDER_MAX_PIXELS=1400`. Worker count is CPU-based. Enforce actual raster dimensions and qualify memory on oversized/scanned inputs. This is source-confirmed; no OOM experiment was run. |
| Medium | Fine-rotation fidelity needs stronger qualification for interactive PDFs. | Object counts and text survival are tested, but annotation/form alignment on offset CropBoxes and incoming internal-link destinations are not. The app already warns before fine rotation of interactive pages. Add geometric marker/destination tests before strengthening fidelity claims; these are unresolved risks, not reproduced failures. |
| Medium | The window mixes too many responsibilities. | Baseline `main_window.py` had 2,658 lines and owns GUI construction, document edits, undo/provenance state, rendering coordination, and saving. Extract by ownership and invariants rather than moving arbitrary methods into helper files. |
| Medium | Static typing remains an incremental adoption. | Work down the recorded baseline as the relevant surfaces are changed. Rename the canvas scene attribute to preserve Qt's `scene()` API; make state transitions and rectangle types explicit. Avoid disabling optional-access checking across the application. |

### Performance measurements

One bounded before/after run per size, using synthetic A4 pages with 24 text lines and
no images. Each subprocess retained the returned images and had a 45-second timeout.

| Pages | Worker time, fork → spawn | Peak process-tree RSS, fork → spawn | Retained image pixels |
| ---: | ---: | ---: | ---: |
| 10 | 0.132 → 1.195 s | 350.6 → 480.1 MiB | 32.3 MiB |
| 100 | 1.010 → 2.511 s | 892.1 → 1,073.7 MiB | 322.8 MiB |

Spawn has a measured startup/memory cost here. It removes the unsafe fork-from-thread
configuration; the next performance work should bound data retention. Aggregate RSS
includes shared pages in each process and is not unique physical-memory usage. These
single runs are indicative, not a statistical benchmark or large scanned-book qualification.

### Decomposition order

1. **Geometry:** collect crop/scene/drawing conversions behind explicit coordinate-space
   contracts. The reproduced preview and cover bugs justify this first extraction.
2. **Document session:** give the loaded document, source identity, page mapping, crop
   state, operation history, and transactional undo a clear owner. Keep Qt controls at
   the window boundary.
3. **Preview pipeline:** separate raster scheduling/cache/compositing from widget
   construction and from save serialization. Preserve stale-result rejection.
4. **UI construction/theme:** extract the toolbar builders and stylesheet once their
   scope/state behavior is covered. `rotation.py` and `provenance.py` are already
   reasonably cohesive; line count alone is not a reason to split them.

Do one extraction at a time with existing pixel, provenance, rollback, and scope tests
as acceptance evidence. No new architecture framework is needed.

## UI and UX review

Parent inspection used the actual Qt widgets, screenshots, and interactions with a
six-page fixture at 1280×800, 1024×768, and 800×600, including 150% scaling. The main
toolbar controls remained visible at those sizes. Native Windows behavior and screen
readers were not tested.

The Crop/Cover/Rotate grouping, selected-tool indication, prominent Save button,
per-page overrides, and explicit warning that covers retain content are useful.
The following changes would improve discoverability and reduce scope mistakes:

- Add an empty state with an Open PDF button and drop hint. Currently startup presents
  two blank panes and disabled editing controls; opening is hidden in the File menu.
- Label the panes with their scope and page counts, for example “Odd pages · 3 pages”.
  Show the document filename and unsaved state in the title or header.
- Make the action label and instructions consistent: the crop button says “Apply”
  while startup guidance says “Apply Crop”. Show a useful ready/scope message after
  rendering rather than leaving “Rendering page previews: 6/6...” visible.
- Add keyboard page navigation and focusable thumbnail previews. Give deletion
  checkboxes accessible page-specific names. Numeric crop controls would also provide
  an accessible alternative to drawing and resizing with a mouse.
- Add a visible zoom/fit control and a concise pan/zoom hint. Four scene-unit resize
  handles become difficult targets when the page is fitted into a small viewport.
- Present a short error message with expandable technical details. Current generic
  background errors place a full traceback directly in the message box.

## Useful feature additions

Prioritize numeric crop margins in points/mm, reusable odd/even crop presets, keyboard
page navigation, and Redo. Progress/cancellation and bounded previews should come
before adding more editing tools. Password entry for encrypted PDFs is a reasonable
later addition; the current loader explicitly rejects them. A batch/headless command
would help repeated processing if that becomes a real workflow requirement.

A separate secure-redaction tool could serve confidential-document work; the current
Cover tool correctly describes itself as visual covering. Preserve that distinction.
OCR, AI layout analysis, and a full PDF editing suite are not required for this app's
current crop/rotate/cover purpose.

## Dependency update review

Live PyPI metadata checked on 1 November 2026. `uv.lock` records the resolution within
the declared compatibility ranges; supported minimums were not raised unnecessarily.

| Dependency | Locked/tested version | Latest available | Decision |
| --- | --- | --- | --- |
| PyMuPDF | 1.28.2 | 2.0 | Keep `<2` pending standalone-build qualification. The full source suite also passes on 2.0. |
| PyQt6 | 6.12.0 | 6.12.0 | Current. |
| deskew | 1.6.1 | 1.6.1 | Current; optional extra remains optional. |
| pytest | 9.2.0 | 9.2.0 | Current. |
| Ruff | 0.17.3 | 0.17.3 | Current. |
| Basedpyright | 1.40.2 | 1.40.2 | Added to the development extra. |
| Vulture | 2.16 | 2.16 | Added to the development extra. |
| Import Linter | 2.15 | 2.15 | Added to the development extra. |
| PyInstaller | 6.22.3 in the optional build resolution | 6.22.3 | No executable build performed. |
| setuptools | 84.2.1 in the optional build resolution | 84.2.1 | Build backend retains its compatibility range. |

PyMuPDF 2.0 adds layout/LLM-related packages and ONNX Runtime to the dependency graph.
Source compatibility alone does not establish an acceptable standalone size, startup
cost, or freezer configuration. The canonical `pymupdf` import is supported since the
existing minimum, 1.24.3. See [PyMuPDF's changelog](https://pymupdf.readthedocs.io/en/latest/changes.html)
and [2.0 package metadata](https://pypi.org/pypi/PyMuPDF/2.0/json).

Before another public standalone build, review bundled dependency notices and
distribution terms. The project's MIT source license does not replace the dependency
licenses: PyMuPDF offers AGPL/commercial terms, and PyQt offers GPL/commercial terms.
This review does not establish that a particular distribution is compliant or
noncompliant. See the [PyMuPDF license information](https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright)
and [Riverbank's PyQt licensing description](https://www.riverbankcomputing.com/software/pyqt/intro#license).

## Verification and remaining limits

- Locked environment: Python 3.13.14, PyMuPDF 1.28.2, PyQt6 6.12.0, deskew 1.6.1:
  **77 tests and 8 subtests passed**.
- Declared runtime minimums: Python 3.11.15, PyMuPDF 1.24.3, PyQt6/Qt 6.5.0, deskew
  1.6.0: **77 tests and 8 subtests passed**; four upstream SIP deprecation warnings.
- Isolated PyMuPDF 2.0 environment: **77 tests and 8 subtests passed**.
- Full static command, five fault-injection gates, Actionlint, and `git diff --check`:
  **passed**.
- Baseline coverage was 78% overall, including only 43% in `widgets.py`. No updated
  final coverage percentage is claimed. Pointer drags, resizing, panning, and keyboard
  accessibility merit additional representative tests alongside their next UI changes.
- Windows/native executable and Linux AppImage qualification, large scanned books,
  interactive-PDF fidelity, and real screen-reader use remain unverified.

Local ignored evidence is under `tmp/review/`: test/static logs, dependency metadata,
policy fault-injection logs, before/after performance JSON, and UI screenshots. A
baseline structural map is in `graphify-out/graph.html` and `GRAPH_REPORT.md`.
Graphify reported 88 dangling-endpoint edges and 9 collapsed directed edges, so use
that graph for navigation and verify call relationships in source. It describes the
baseline, not the final modified tree.

Specialists used: researcher (`gpt-6-luna/max`) for dependency evidence and a separate
researcher for documentation graph extraction; deep-researcher (`gpt-6.1-sol/high`)
for engine traces; verifier (`gpt-6-luna/max`) for the fixed performance protocol;
implementer (`gpt-6-luna/max`) for the exact worker/test packet. These are configured
assignments; independent runtime model/effort metadata was unavailable. UI inspection,
product decisions, overlay/cover edits, policy design, integration, and acceptance were
performed by the parent.
