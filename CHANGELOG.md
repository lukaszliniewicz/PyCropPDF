# Changelog

## 0.5.0 — 2026-11-01

- Keep page previews in RAM first, sharing a 500 MiB budget with thumbnails and
  reusable overlay groups; spill older page buffers to private compressed files.
- Refresh affected overlay groups after an edit, replace internal PNG transfers
  with lossless raw frames, and keep metadata reads independent of cache I/O.
- Show rendering/deskew progress, support cancellation and preview retry, and
  preserve committed edits and Undo history when an operation is cancelled.
- Use a virtualized thumbnail sidebar with keyboard preview and selection, and
  allow ready pages to be opened while other previews are rendering.
- Correct preview/crop/cover coordinates for existing CropBoxes and rotated pages;
  retain ink from every page in stack overlays.
- Reject manifest destinations that alias a source or output PDF, use bounded
  spawned worker processes, and cap unusually large preview/deskew rasters.
- Add a locked development/build environment and required Ruff, Basedpyright,
  Vulture, and import-cycle/layer checks, with Linux/Windows source CI.
- Add repeatable Windows/Linux release builds, dependency notices, build records,
  and packaged window/rendering/deskew checks.

Synthetic A4 text benchmarks with the same 500 MiB cache in both versions measured
median one-page refresh times of 1.55 to 0.45 seconds for 100 pages and 20.32 to 1.32
seconds for 500 pages. Initial 500-page loading fell from 28.74 to 17.49 seconds.
These are two alternating-order offscreen trials per case; scanned books and real
screen latency need separate measurement. See [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).

Crop and Cover change presentation and do not securely redact content. Fine-angle
rotation of pages with interactive objects still carries the existing fidelity
warning. The 500 MiB cache limit excludes active display buffers, PDFs, and workers.
