# Implementation plan

Source: [1 November 2026 review](REVIEW.md). Work remains on
`codex/review-and-reliability`. Each milestone should leave a usable application,
pass the static policy, and record its verification before the next begins.

## Completed foundation

- [x] Protect PDF/source paths from manifest collisions.
- [x] Correct crop rendering and covers on rotated/offset CropBoxes.
- [x] Preserve ink from every page in stack composites.
- [x] Use spawned rendering processes and the canonical PyMuPDF import.
- [x] Establish locked dependencies, Ruff/Basedpyright/Vulture/Import Linter, CI,
  and tests against current, minimum, and PyMuPDF 2.0 dependencies.

## Milestone 1 — Bound worker queues and control long operations

Status: **completed and verified, 2026-11-01**.

This removes the redundant retention of every raster result in completed futures
and gives rendering/deskew a safe Cancel path. The viewer still retains page
images until milestone 2; completing this milestone is not a claim of bounded
whole-application memory.

### Exact scope

- [x] Add a pure-Python `processing.py` with a thread-safe cancellation token,
  a distinct cancellation exception, and a bounded completion iterator.
- [x] Admit at most one outstanding task per selected worker process. Consume
  and release futures incrementally; preserve exactly-once page results and spawn.
- [x] Add progress/cancelled signals to render/deskew workers. Stop scheduling
  when cancelled, cancel pending futures, and drain already running native calls
  before announcing completion. Unexpected failures remain errors.
- [x] Check cancellation during deskew detection, between private-document edits,
  and before publishing output. Cancelling must not commit rotated document bytes,
  crop/provenance changes, or an extra Undo entry.
- [x] Enforce the deskew raster bound of 1,400 pixels on its longest side.
- [x] Add a progress indicator and Cancel/Escape to the window. While workers
  drain, show that cancellation is in progress. Save/manual-rotation operations
  remain non-cancellable in this milestone.
- [x] After incomplete rendering, offer Retry previews. Disable coordinate tools
  until previews are complete; keep valid document edits available to save/undo.
- [x] A close request during a cancellable operation requests cancellation, waits
  for worker cleanup, and then uses the normal unsaved-changes confirmation.
- [x] Reject late progress/results from older or cancelled operations. Replace
  a completed rendering counter with a useful ready/status message.

### Ownership and interfaces

The parent owns the window, GUI tests, plan, import-layer configuration, integration,
and acceptance. A specialist implements only the exact non-UI packet in
`processing.py`, `workers.py`, deskew portions of `rotation.py`, and their tests.

`CancellationToken` exposes `cancel()`, `is_cancelled`, and
`raise_if_cancelled()`. `iter_bounded_results()` accepts an executor, task function,
items, positive `max_pending`, and optional token; it yields completion-order values
and cancels unconsumed futures when closed. The caller owns executor shutdown.
No token is sent into child processes. Native page calls finish cooperatively.
An in-progress PDF serialization also completes before cancellation is observed.

`deskew_pdf_bytes()` gains optional keyword-only token/progress parameters; existing
calls retain their results and defaults. Render/deskew workers expose their token,
`progress(completed, total)`, `cancelled()`, and the existing result/error/finished
signals. Cancellation emits no successful output and exactly one finished signal.
The window stages the operation's Undo snapshot outside the bounded history and
admits it only when the edit commits. Cancellation, error, and no-angle results
release that staged file without evicting earlier history, including at both limits.

### Acceptance

- [x] Deterministic 100-item admission test proves the outstanding-future bound;
  early close, cancellation, task errors, and exactly-once completion are covered.
- [x] Serial and process paths report progress and cancellation consistently.
  Real spawned rendering and deskew still work.
- [x] UI tests cover cancel/result races, stale callbacks, crop/page/provenance/Undo
  preservation, retry, error recovery, and close-after-cleanup.
- [x] Actual oversized-page rasters stay within the limit without expensive deskew
  or OOM experiments.
- [x] Repeat the existing 10/100-page text-fixture measurement once. Compare raw
  pipeline retention, worker time, and process-tree RSS with the saved baseline;
  distinguish retained viewer images from temporary worker results.
- [x] Full tests and static checks pass, with no added type-baseline diagnostics.
  Inspect the new controls at small windows and increased display scaling.

## Milestone 2 — Bound retained previews and show useful results earlier

Status: **completed and verified, 2026-11-01**.

- [x] Introduce a byte-bounded page-image cache (current decoded-image budget: 500 MiB),
  separate lightweight thumbnails, and explicit render-generation ownership.
- [x] Stream every page into ink-preserving composites without collecting full raster
  lists. Keep only the page images needed for active page previews/zoom.
- [x] Preserve exact dimensions and crop-coordinate mappings independently of cache
  residency. Invalidate only affected pages after edits.
- [x] Show thumbnails/progress as pages arrive; virtualize the thumbnail view so its
  widget count does not grow with the full document.
- [x] Release caches, temporary storage, and old document generations predictably.

### Implemented contracts

`raster_cache.py` owns exact page metadata, generations, and a **500 MiB** budget
shared by the full-image/thumbnail decoded LRU and reusable overlay groups. Group
memos have a separate LRU capped at a quarter of that budget (125 MiB by default).
Page buffers stay in RAM until eviction or
an individual buffer exceeds the budget. Only then are they compressed into a private
spill file. Promoted buffers keep that backing file for reuse on later eviction.
Under-budget writes and reads perform no cache compression, hashing, or file writes.

Replacing/remapping a generation preserves live RAM buffers and spill files in LRU
order, counting aliases once. An obsolete reader cannot retrieve a newer generation.
Unreferenced buffers/files and old overlays are released; close removes the private
directory. Failed writes preserve readable existing entries, and missing/corrupt spill
data can be regenerated. Pixel operations share one lock; generation changes and
close serialize after active spill IO. Metadata and current-frame asset reads use
a separate lock and do not wait for compression or file IO. The window drains workers
before close. Spill files retain bounded zlib validation and an expected CRC32;
source/output provenance hashes remain SHA-256.

`raster_geometry.py` separates exact 1.5-scale logical scene dimensions from the
display raster. Rasters and composite canvases have a maximum side of 2,048 pixels;
normal A4 previews retain their existing resolution. Scene coordinates and PDF
crop/cover mapping use logical dimensions even after eviction or display resizing.
This is a display-resolution cap, not a change to saved PDF content. Higher-resolution
detail/tile rendering for unusually large pages remains a later improvement.

The cached worker writes full/thumbnail records and emits small metadata signals.
It maintains three bounded all/odd/even canvases. Opaque pages with an unscaled canvas
use versioned groups of up to 32 pages of the same parity. A partial edit reuses
unaffected groups without recovering their individual rasters. Groups are enabled
only when all group memos fit their budget; scaled canvases retain streaming
composition. Progressive raw, lossless overlay bundles replace one in-memory cache
asset atomically, so queued Qt signals do not retain a document's full page buffers. A retry
renders missing pages; deletion remaps retained entries and rebuilds parity without
native rendering. Crop, cover, and rotation refresh affected pages only.
Overlay snapshots are published after the first page, approximately once per second,
and at completion. Encoding time does not consume the interval reserved for page work.

`preview_images.py` holds the Qt image adapters/compositor and bounded raw frame
format; `overlay_groups.py` coordinates derived group reuse. `thumbnails.py` uses
Qt model/view/delegate painting with a separate 4 MiB thumbnail-pixmap LRU. The
sidebar has no per-page widgets. Ready pages can be previewed during rendering;
coordinate tools become available only after a complete final overlay. Enter previews,
Space checks, arrows navigate, and Ctrl/Command/Shift preserve deletion-selection
semantics. Canvas panning now requires page focus. The parent owns these UI changes.

The 500 MiB accounting covers retained decoded RGB buffers and raw group memo bytes.
Active QImages/QPixmaps, the one scratch group canvas,
three display canvases, compressed files, PDF bytes, native libraries, and bounded
process buffers are measured separately rather than included in that cache limit.

Acceptance: all-page markers survive cache eviction and arbitrary completion order;
crop/cover/rotation pixel tests remain accurate; active page preview is correct;
100- and 500-page fixtures obey the image-cache budget; cancellation and retry leave
no obsolete generation accessible. Measure startup time and total RSS separately
from the cache's byte accounting. The exact pure cache/geometry packet was designed
before delegation; the parent audited all four implementation/test files.

## Milestone 3 — Extract document geometry/state and qualify fidelity

Status: **queued**. Depends on stable preview interfaces.

- Extract coordinate conversions behind explicit crop, scene, and drawing-space
  contracts; keep Qt adapters at the window boundary.
- Give the loaded document, source identity, page map, crops, edit history, and
  transactional Undo a clear document-session owner.
- Add alignment tests for annotations/forms on offset CropBoxes and tests for
  incoming internal-link destinations under fine rotation. Fix reproduced defects
  or retain clearly documented limitations.
- Remove the canvas attribute that shadows Qt's `scene()` API and reduce relevant
  type-baseline entries. Do not enlarge the baseline to accommodate a refactor.

Acceptance: existing geometry, rollback, scope, save/provenance, and cancellation
tests pass; a session has no window/widget dependency; each extraction is reviewed
and verified before another. Keep already cohesive rotation/provenance logic together.

## Milestone 4 — Make scope and precision obvious

Status: **queued**. Parent-only UI work.

- Add an Open/drop empty state, filename/unsaved indicator, scope labels/page counts,
  consistent Apply Crop copy, and concise errors with expandable technical details.
- Add focusable thumbnail previews, keyboard page navigation, accessible deletion
  labels, visible zoom/fit controls, and usable selection handles at fit-to-window.
- Add numeric crop margins in points/mm and reusable odd/even presets. Add Redo
  through the document-session history rather than a separate UI-only mechanism.
- Extract toolbar construction/theme as ownership becomes clear.

Acceptance: mouse and keyboard crop/cover/rotate flows remain consistent; destructive
selection and preview selection stay distinguishable; inspect 800×600, 1024×768,
1280×800 and increased scaling, plus a native Windows pass when available.

## Milestone 5 — Qualify distribution and the next dependency major

Status: **queued**, after relevant source changes stabilize.

- Keep dependency metadata, lock resolution, minimum support, and CI aligned.
- Assess PyMuPDF 2.0's added packages, startup/memory cost, and standalone size;
  source-test success alone does not justify changing the major-version bound.
- Review third-party notices and bundled distribution terms; document build inputs.
- Build and smoke-test the Windows executable and Linux AppImage with deskew,
  process spawning, cancellation, saving, and learner-sized/scanned documents.
- Execute GitHub CI when changes are pushed. A local check is not a remote CI result.

Publication/release is a separate action from preparing and verifying local changes.

## Later features

Password entry for encrypted PDFs, headless/batch processing, and a separate secure
redaction tool remain later candidates. They are not dependencies of the current
crop/rotate/cover improvements. Reassess them against actual workflow needs before
adding their APIs or dependencies.

## Progress record

- 2026-11-01: Plan created; milestone 1 implementation started. The 77-test review
  baseline and existing static-check inventory are the comparison point.
- 2026-11-01: Milestone 1 completed. **107 tests and 11 subtests passed** in each
  of the locked Python 3.13/PyMuPDF 1.28.2 environment, Python 3.11 with minimum
  PyMuPDF 1.24.3/PyQt6 6.5.0/deskew 1.6.0, and the isolated PyMuPDF 2.0 environment.
  The minimum Qt binding reports four upstream SIP deprecation warnings.
- Ruff lint/format, Basedpyright, Vulture, both import contracts, and
  `git diff --check` pass. Type-baseline diagnostics fell from **113 to 64**;
  no new suppressions or diagnostic exclusions were added.
- Real Qt/process runs cancelled a 20-page render after its first result, retained
  the unchanged document, drained workers, and completed a retry. Controls were
  inspected at 800×600, 1024×768, and 1280×800, including 150% scaling. Native Windows
  and standalone executable qualification remain in milestone 5.

### Milestone 1 measurements

The same synthetic A4 text fixtures and retained-image protocol were run once per
size, comparing the prior spawned worker with the bounded worker:

| Pages | Worker time, prior → bounded | Peak process-tree RSS, prior → bounded | Retained image pixels |
| ---: | ---: | ---: | ---: |
| 10 | 1.195 → 0.972 s | 480.1 → 441.5 MiB | 32.3 MiB |
| 100 | 2.511 → 2.236 s | 1,073.7 → 736.4 MiB | 322.8 MiB |

The 100-page peak fell **31.4%** and final process RSS fell **33.9%**. These are
single-run measurements, not statistical timing claims; aggregate RSS counts shared
pages in each process. The Python/library versions match the earlier run; a temporary
uv overlay supplied pinned psutil 7.2.2 without changing the locked project environment.
The worker/processing/rotation sources stayed unchanged during measurement.

The retained image total is deliberately unchanged: milestone 2 addresses that cost.
The milestone 1 measurements do not include milestone 2's subsequent cache changes.

Local ignored evidence: `tmp/review/performance/results-bounded.json`, previous
`results-after.json`, `implementation-tests.log`, `implementation-minimum.log`,
`implementation-pymupdf2.log`, `implementation-static.log`, `undo-limit-before.log`,
`undo-limit-after.log`, and `implementation-ui*/` screenshots/results. The Undo-limit
regressions reproduced the prior loss before the staged-snapshot fix.

The non-UI implementer and measurement verifier used the configured
`gpt-6-luna/max` profiles; the read-only lifecycle reviewer used `gpt-6.1-sol/high`.
No material lifecycle defect was reported; explicit iterator cleanup was added and
tested to avoid relying on generator finalization. These are configured assignments,
not independently verified runtime model metadata. The parent designed the milestone,
implemented and tested the UI/state integration, audited the edits, and accepted it.

### Milestone 2 verification

- **226 tests and 11 subtests passed** in each of the locked Python 3.13/current
  dependency environment, Python 3.11/minimum dependency environment, and isolated
  PyMuPDF 2.0 environment. The minimum binding reports six upstream SIP warnings.
- Ruff lint/format, Basedpyright, Vulture, both import contracts, and
  `git diff --check` pass. Resolved legacy type diagnostics fell from **64 to 60**;
  no new baseline entries or type exclusions were added. Qt delegate callbacks
  have explicit, type-checked Vulture allowlist references.
- Cache tests cover LRU recency/oversized buffers, full/thumbnail accounting,
  500 entries, remapping and stale reads, file loss/corruption, failed staged
  writes, and generation changes during compression and file writes.
- Geometry tests compare 64 CropBox/rotation/crop combinations with actual rasters.
  Oversized page rasters stay capped while logical geometry and round trips stay
  accurate. All-page/parity markers survive eviction and reversed completion order.
- Window tests exercise progressive read-only page previews, cancellation/retry,
  partial covers, deletion without native rendering, cache recovery, keyboard
  selection/navigation, and 500 thumbnail rows with bounded widget/pixmap counts.
  A progressive preview does not read PDF coordinates while a worker is active.
- Actual Qt/process runs cancelled a 20-page render after its first result and
  completed a retry without changing the loaded PDF. The sidebar had **five
  widgets**, including when traversing its rows; no per-page widgets are created.
  Screenshots at 800×600, 1024×768, 1280×800 and 150% scaling were inspected.
  A stack-to-page fitting defect found in those screenshots was fixed and tested.

Performance qualification caught nearly uncompressed overlay PNGs and an overly
frequent snapshot cadence. Qt's PNG compression setting uses a 0–100 scale;
the selected setting now keeps a plain A4 snapshot below 50 KB, with a lossless
round-trip regression. See [Qt's compression API](https://doc.qt.io/qt-6/qimagewriter.html#setCompression).
Snapshots now leave more time for page work. Timing controls also exposed that the
initial benchmark placed caches on the project drive rather than the normal system
temporary drive. Those initial data remain preserved as diagnostics; the final
qualification uses an owned directory on the normal temporary filesystem.

Local ignored evidence: `tmp/review/milestone2-*.log`, `milestone2-ui*/`,
`preview-diagnostics/`, `dimension-probe/`, and
`performance/cache-milestone2/`. The pure cache/geometry implementer and measurement
verifier used configured `gpt-6-luna/max` profiles; runtime model metadata was not
independently exposed. The parent designed the contracts, audited every delegated
edit, and owns all Qt adapters, rendering integration, UI changes/tests, and acceptance.

### Initial milestone 2 measurements — disk-first, 64 MiB

These historical measurements describe the initial policy. The user subsequently
requested RAM-first storage and a 500 MiB decoded-image budget; the current contract
above supersedes that policy. Its application-level comparison is recorded separately.

Final runs used the original A4 text fixtures, fresh processes, a 20 ms process-tree
RSS monitor, and owned cache parents on the normal `/tmp` filesystem. Every result
callback also read the full cached page, and a subsequent full/thumbnail traversal
exercised cold reads and LRU eviction. All raster, exactly-once, budget, and cleanup
checks passed; all four owned cache parents were removed, including the timing control.

| Pages | Worker time | First overlay ready | Peak process-tree RSS | Parent RSS after worker | Peak decoded cache | Cache disk |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 10 | 1.03 s | 0.55 s | 322.6 MiB | 122.7 MiB | 33.3 MiB | 2.0 MiB |
| 100 | 4.76 s | 0.49 s | 392.9 MiB | 156.8 MiB | 63.4 MiB | 16.1 MiB |
| 500 | 28.31 s | 0.58 s | 577.0 MiB | 166.5 MiB | 63.4 MiB | 78.7 MiB |

The cache remained below **64 MiB** at every observation, including 1,501 observations
in the 500-page run. Parent-process memory stayed similar between 100 and 500 pages;
native worker/PDF structures still contribute to the process-tree growth. There is
no claim that total application RSS is bounded at 64 MiB. These measurements use a
worker/cache harness without the window; the actual GUI adds display/widget memory.
The first-overlay number measures the ready signal, not a screen-paint timestamp.

The endpoint-only 100-page timing control took **4.64 s**, versus 4.76 s with periodic
monitoring. It makes no peak-RSS claim. These are single trials on a shared host;
aggregate RSS can count shared pages more than once, and host load was not isolated.
The five worker/cache/adapter source hashes stayed unchanged during all final runs.

This pipeline now includes thumbnail creation, all three composites, compressed
storage, and the stress consumer's full-page reads. Milestone 1 measured rendering
with retained QImages and omitted the old window's later thumbnail/composite work.
Consequently its worker timings are not an end-to-end UI speed comparison. There is
a real bulk-throughput cost to materializing the disk cache; the measured benefit is
bounded retained pixels, a useful first preview, and reuse after partial edits/retry.
Further throughput work should use end-to-end representative scanned-book measurements
rather than assume that the memory improvement makes every operation faster.

Final reports: `tmp/review/performance/cache-milestone2/final/results.json` and
`final/latency-control.json`; the exact coordinator and per-run logs are alongside
them. Earlier project-drive and uncompressed-frame results remain preserved for
diagnosis and are not substituted for the final qualification.

### RAM-first follow-up — completed, 2026-11-01

The user requested RAM-first storage and a **500 MiB** decoded-image budget after
reviewing the initial throughput cost. The current implementation allocates this
budget on demand, spills only on eviction/oversize, and keeps overlay bundles in
memory. Preserved pages retain their resident buffers across render generations.

The exact non-UI cache/test packet was implemented by the configured
`gpt-6-luna/max` specialist and audited by the parent. The parent updated integration
tests, documentation, and all UI/performance probes. **227 tests and 11 subtests
passed** in each of the locked/current, minimum, and PyMuPDF 2.0 environments.
The final cache/UI subset passed 27 tests after adding metadata-contract assertions.
All static gates and `git diff --check` pass; the type baseline remains **60**.

Tests demonstrate no cache compression/hashing/file writes while data fits, exact
byte accounting, preserved RAM identities after edits/deletion, spill/promotion with
one reusable backing file, failure recovery, and serialized generation/close races.
RAM assets replace disk assets; corrupt-file recovery is explicitly exercised with
a zero-residency cache. A fitting document and its partial edit create no preview files.

The parent ran the actual Qt window at 800×600 with the same **500 MiB limit in both
policies**. Each fresh process opened an identical A4 text fixture, waited for the
final rendered UI, previewed the first and last pages, and applied one cover to the
last page. Only that page was natively re-rendered. RSS sampling ran in an external
coordinator rather than a monitor thread sharing the GUI process's GIL. Fixture
generation and application construction are outside the load timer.

| Pages | Complete load, disk-first → RAM-first | One-page cover refresh, disk-first → RAM-first | First-page preview, disk-first → RAM-first |
| ---: | ---: | ---: | ---: |
| 100 | 4.96 → 4.13 s | 2.45 → 0.90 s | 23.0 → 10.2 ms |
| 500 | 21.54 → 20.85 s | 17.86 → 10.98 s | 19.7 → 47.9 ms |

These are single offscreen trials on a shared host. The 500-page initial-load
difference is small and should not be treated as a reliable speedup. Its first page
had spilled; promoting a cold page while evicting another can add latency. Last-page
preview was approximately 11–12 ms with RAM-first versus 22–26 ms with disk-first.
The clearer observed benefit is the partial-edit path, which reuses resident data.

The 100-page RAM-first case created **zero preview files**, compared with 201 in the
disk-first case. At 500 pages it spilled 699 buffers (54.7 MiB), compared with 1,001
files/78.7 MiB previously. Its decoded cache peaked at **333.0/499.85 MiB** for
100/500 pages, below the 500 MiB budget. Total process-tree RSS was higher by design:
approximately **931/1,469 MiB**, versus **455/660 MiB** with disk-first. This includes
workers and repeated shared pages; it is not a measurement of unique physical RAM.

The old cache source was recovered verbatim from this chat's saved tool output and
matched its previously measured SHA-256 (`d85a67ce…e23d45`). The benchmark loader
shares only the unchanged public value/error types with current adapters; the old
cache logic remains unmodified. All five application source hashes stayed fixed
during the four comparisons, and owned temporary directories were removed.

Evidence: `tmp/review/ram-first/qt-results.json`, `qt_measure.py`,
`qt_coordinator.py`, `baseline/provenance.json`, test/static logs, and `ui*/` captures.
The application and benchmark remain local; standalone/native Windows qualification
and representative scanned-book throughput remain later work.

### Performance diagnosis after RAM-first — 2026-11-01

The parent profiled the actual 800×600 Qt load, first/last page previews, and one-page
cover refresh using the same A4 text fixtures. Timers record wall and caller-thread
CPU separately; nested timers are not additive. Iterator waits measure only `next()`
calls, not time while the generator is suspended. Native rasterization/transfer and
codec controls were executed by the configured `gpt-6-luna/max` verifier without UI
inspection. Product source was unchanged throughout these diagnostics.

**Finding: avoidable computation and coordination in the derived-preview pipeline
dominate these workloads; actual cache-file IO is comparatively small.**

- A one-page edit still blends every page into all/odd/even overlays and repeatedly
  PNG-encodes all three canvases. With 100 pages fully in RAM, overlay blending and
  encoding used **55% + 38%** of render-worker wall time; the one native page render
  took about **0.018 s**, versus **2.57 s** in that worker. Cache hits were under 1 ms
  cumulatively. This is an algorithm/work-selection issue, not slow cache lookup.
- At 500 pages, a partial refresh read 350 spilled page buffers. Recovery took
  **8.64 s**, but the actual compressed-file reads took only **0.122 s** (nested in
  that recovery time). Blending all 500 pages took **5.34 s** and encoding 17 overlay
  bundles **4.05 s**. These are roughly 45%, 27%, and 21% of worker time.
- During the 500-page initial load, cache publication took **19.35 s**, including
  **18.57 s** of compression/hash/spill work. Actual file writes were **0.619 s**.
  PNG encoding took **10.91 s**, thumbnail generation **8.96 s**, and blending
  **7.07 s**. Waiting for raster results/startup/transfer was **4.45 s**. The cache's
  repeated whole-record bookkeeping scans are a secondary cost, below 1 s here.
- A separate 100-iteration control on one 3,383,577-byte RGB page measured mean CPU
  costs of **10.0 ms SHA-256**, **9.4 ms zlib level-1 compression**, and **4.7 ms
  decompression**. The cryptographic digest on every spill and cold read is a
  significant avoidable cost for regenerable temporary pixels. Source PDF and
  provenance hashes serve a different purpose and must be retained. Zlib already
  validates compressed-stream integrity; any lighter external checksum should retain
  cache-file identity/shape validation and corruption recovery. See the
  [zlib manual](https://www.zlib.net/manual.html).
- GUI page-result callbacks accumulate substantial wall time with very little CPU
  when spilled rasters are processed. Source inspection shows metadata getters share
  the lock held during compression/decompression/IO. This strongly indicates cache
  lock contention affecting responsiveness; separating metadata reads from slow
  buffer operations is worth addressing without weakening generation ownership.

The native-only control rendered/transferred **1.69 GB** of pixel bytes for 500 pages:
the full-payload path took 6.75 s, versus 4.29 s when only metadata was returned.
Those controls are not end-to-end app measurements, and native CPU timings also varied;
they identify interprocess copying as additional overhead rather than establish an
exact isolated penalty. The 100-page full-transfer case was a large scheduling/startup
outlier and is excluded from conclusions.

A bounded Qt compositor control tested RGB888 versus RGB32 and the default versus
one-thread global pool. All tested pixels matched, but there was no consistent large
speed/CPU benefit. Thread-count or pixel-format tweaks should not displace the larger
work-reduction opportunities.

Recommended next executable priorities:

1. Replace PNG serialization between the in-process worker and GUI with a bounded,
   generation-owned lossless frame transfer, keeping queued signals small.
2. Use lighter integrity verification for temporary raster spill files; retain
   bounded decoding, corrupt-file recovery, and all source/provenance SHA-256 hashes.
3. Reuse intermediate composites so a one-page edit does not recover/blend every
   unchanged page. Deriving the combined stack from parity stacks is another candidate
   to validate against alpha/rounding and mixed-page regression cases.
4. Separate metadata access from long cache operations to keep the GUI responsive.
   Revisit raw pixel transfer/shared buffers after the larger serial work is reduced.

Absolute profiled times were substantially slower than the preceding uninstrumented
comparison: host scheduling varied, instrumentation adds overhead, and slower runs
also generate more periodic overlay snapshots. The phase shares and call counts are
diagnostic evidence, not normal-latency promises. These conclusions apply to the
synthetic text fixtures; scanned/image-heavy books need their own representative pass.

Evidence: `tmp/review/bottleneck/profile-results.json`, `instrumentation.py`,
`qt-composite-control.json`, and `backend/{results,codec-microcontrol}.json`. All
diagnostic checks and pixel comparisons passed; owned temporary directories were
removed and application source hashes remained unchanged. No runtime code was changed
as part of this diagnosis.

### Preview serial-work reduction — completed, 2026-11-01

All four follow-up priorities are implemented:

- Internal frames use bounded, versioned raw premultiplied pixels instead of PNG
  encoding/decoding. One current-generation immutable bundle is replaced atomically;
  queued signals still carry metadata only. Decoders reject invalid dimensions,
  formats, strides, and lengths before constructing an image.
- Temporary spills use expected CRC32 plus the existing bounded zlib stream/size
  validation. Source and output provenance retain SHA-256.
- Metadata snapshots and RAM-only frame assets have a short, separate lock. Pixel
  operations publish through that lock; metadata readers never acquire the pixel
  lock. Generation changes and close still serialize after active pixel operations.
- `overlay_groups.py` reuses versioned groups of up to 32 pages of the same parity.
  Keys include canvas geometry, page positions/dimensions, and page versions.
  Preserved generations retain valid groups; edits, remapping, corruption, and layout
  changes invalidate affected keys. Memo bytes share the **500 MiB** total cache
  budget, with a protected LRU capped at **125 MiB** by default.

Opaque, integer-aligned pages permit exact regrouping and derivation of the combined
stack from the parity stacks. Scaled canvases retain the original streaming path;
alpha-bearing temporary rotation previews retain the general compositor. Grouping
is disabled when all its memos would exceed their allowance. Only one scratch group
canvas is allocated at a time, including out-of-order native completions. An optional
memo failure falls back to rebuilding; unavailable raw pages trigger Retry.

The 128-page regression reuses 96 pages without reading their individual rasters
after one edit. At most 64 full-raster reads serve the dirty 32-page group, including
its memo rebuild. Full/parity pixels match ordinary full recomposition exactly,
including mixed dimensions and reverse completion order. Native cropped pages with
nonzero CropBoxes and all four right-angle rotations also match the grouped path.

The parent compared the frozen preceding RAM-first implementation against the new
version using the real Qt window at 800×600, synthetic A4 text PDFs, **500 MiB in
both cases**, and fresh subprocesses. Two trials per case ran in alternating order;
the table gives medians in seconds:

| Pages | Initial load, before → after | One-page cover refresh, before → after |
| --- | --- | --- |
| 100 | 7.69 → 5.66 | 1.55 → 0.45 |
| 500 | 28.74 → 17.49 | 20.32 → 1.32 |

The 500-page edit improved by about **15.3×**; its two new timings were 1.31/1.34 s,
against 23.47/17.16 s previously. Every edit rendered exactly one native page.
Application source hashes stayed unchanged during measurement; input hashes matched,
all cases passed, and owned temporary directories/caches were removed.

Combined cache peaks were **350.24 MiB** for 100 pages and **499.94 MiB** for 500.
The extra group storage causes some additional raw-page spilling: 500-page cache
disk usage after editing rose from 54.89 to 58.17 MiB. Raw current-frame bytes and
the scratch canvas are display/work buffers outside the retained-cache limit.
For 100 pages, externally sampled process-tree RSS rose from 945–950 to 1,007–1,032
MiB. For 500 pages it ranged from 1,173–1,518 MiB before and 1,267–1,275 MiB after;
host variation prevents a dependable total-memory reduction claim. RSS also counts
shared process pages more than once. These are offscreen text-fixture measurements,
not physical screen latency or qualification of scanned/image-heavy books.

The configured GPT-6 Luna/max implementer owned the pure cache and cache tests;
the parent audited those edits and implemented/tested all Qt/UI integration. A
configured GPT-6.1 Sol/high reviewer found no material lock-ordering or failure-
recovery issue in the bounded non-UI comparison.

Final validation: **255 tests and 11 subtests pass in each** of the locked/current,
minimum-dependency, and isolated PyMuPDF 2.0 environments. The minimum environment
reports the same six upstream SIP deprecation warnings. All static gates pass;
the Basedpyright baseline remains **60**. Parent-owned actual-window cancellation,
Retry, source-preservation, keyboard selection/preview, and 800/1,024/1,280-wide
layout checks pass at 100% and 150% scale, with five sidebar widgets.

Evidence: `tmp/review/preview-optimisation/{qt-results,summary}.json`, frozen
`baseline/source-hashes.json`, per-case benchmark logs, and `ui*/results.json`.
