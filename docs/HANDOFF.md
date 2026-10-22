# Lumen implementation handoff

Last updated: 2026-10-22 (Africa/Johannesburg)

## Resume here

The active repository is `/Users/nasir/Developer/localimagemanip`. The project is runnable and its complete warning-strict test suite currently passes **223 tests** (run with `env -u LUMEN_OMLX_MODEL`; the user's shell override fails the doctor default-model assertion in `test_cli`). `node --check src/lumen/static/app.js` also passes.

The composition implementation is complete through Task 5 and its independent re-review is approved with no open findings. Read:

- `.superpowers/sdd/2026-10-21-composition-crops/progress.md`
- `.superpowers/sdd/2026-10-21-composition-crops/task-5-report.md`
- `.superpowers/sdd/2026-10-21-composition-crops/task-5-review.md`
- `.superpowers/sdd/2026-10-21-composition-crops/task-5-rereview.md` if present
- `.superpowers/sdd/2026-10-21-composition-crops/plan-final-rereview-2.md`

The composition plan is complete. Its final independent re-review is approved with 0 Critical, 0 Important, and 0 Minor findings. Astro Task 1 is the active next milestone.

Astro Task 1 is complete and independently approved with no open findings. It adds validated persisted jobs, strict transitions/recovery, exclusive per-catalog runner ownership across local processes, one heavy worker, and owned-process cancellation/reaping with retryable shutdown. Read `.superpowers/sdd/2026-10-21-astro-stacking/task-1-rereview-2.md`; Task 2 Siril preparation is now the active milestone.

## Working features

- Non-destructive Sony `.ARW`/image import, managed previews, SQLite catalog, JSON/XMP sidecars, and provenance.
- Camera/time burst grouping, explainable best-shot ranking, manual keeper override, sharpness/exposure/face/eye metrics, and basic astro screening.
- Truthful live oMLX model status plus local preview tagging.
- Recoverable group removal and workspace clearing with catalog snapshots; originals and completed exports are retained.
- Quality-92 sRGB JPEG export through darktable for current photo or all keepers, collision-safe naming, optional explicit XMP profile, metadata filtering, and Finder reveal.
- Deterministic non-destructive crop candidates from macOS Vision regions with honest centered fallback, 8 MP rejection, and below-12-MP warnings.
- Persisted crop choices in catalog/sidecars and exact normalized full-resolution crop application during JPEG export.
- Optional single-request oMLX composition critique that may select only a supplied candidate, records provenance, and never activates a crop.
- Accessible Compose → Export → Clear web workflow with CSS crop viewports, thirds overlays, native action buttons, active/recommended state, request-race protection, safe public API errors, and advisory-only AI rationale.
- Persisted local job records with restart recovery and one exclusive heavy worker that can cancel/reap only its owned process.

## Latest reviewed fix evidence

Composition Task 5 review originally found six issues. Current code addresses all of them:

1. Crop/review API errors use stable public messages and do not expose source/preview paths.
2. Monotonic load/review tokens prevent stale same-photo and cross-photo responses; new loads immediately clear old candidates/advice.
3. Crop buttons have distinct accessible names containing crop type and aspect.
4. The Node harness executes successful choose/clear, AI failure, stale-card, request-race, and mismatched-recommendation flows.
5. Active+recommended cards retain amber selection with a separate cyan cue.
6. No-selection copy is restored after a failed crop load.

Verification commands last run:

```sh
uv run python -W error -m unittest tests.test_web -v       # 37 passed
node --check src/lumen/static/app.js                       # exit 0
env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest discover -s tests -v   # 223 passed (2026-10-22)
```

The latest whole-plan fixes prove the default candidates include one contextual and one tighter crop, source replacement invalidates an old crop before the fingerprint-upsert failure boundary, and every export rejects source bytes not successfully cataloged after an interrupted replacement. See `.superpowers/sdd/2026-10-21-composition-crops/plan-final-fix-report.md`.

No manual Safari/VoiceOver pass, real Sony RAW crop visual check, Apple Vision hardware pass, or live oMLX composition request has been performed. Those remain integration risks, not known automated failures.

## Real darktable export check (2026-10-22)

`test-image/DSC01027.ARW` (Sony ILCE-7CM2, untracked, not committed) exported via `lumen --library <scratch> export --keepers` in ~12 s:

- Original unchanged: sha256 `21a7428d…ac3d8`, mtime `1792167197.79`, size 34263040 identical before/after; no sidecar written beside it.
- JPEG 7032×4688 (darktable's full uncropped-border render, `--width 0 --height 0`; raw 7040×4688), embedded sRGB ICC + EXIF ColorSpace sRGB, quantization tables exactly Pillow quality 92.
- No GPS tags in the JPEG — but the source has no GPS either, so GPS stripping was not exercised by this file.
- No camera EXIF in the JPEG (see known limitations).

## Next milestone: managed Siril astro stacking

Use `docs/superpowers/plans/2026-10-21-astro-stacking.md` task by task. Do not jump straight to the UI.

1. **Job catalog and single-worker lifecycle** — complete and independently approved.
2. **Reproducible Siril preparation** — add immutable frame sets/manifests, safe job paths, fingerprints, warnings, capability probing, and Siril 1.4.4 script generation validated against the local executable.
3. **Execute/cancel/import** — revalidate fingerprints before launch, stream logs, require declared 16-bit TIFF even on exit 0, catalog the result artifact, and never mutate sources.
4. **CLI and protected API** — explicit group confirmation, selection/exclusion, calibration folders, status/log tail, run, and cancellation.
5. **Astro web workflow** — explicit confirmation, per-light inclusion, calibration folder pickers, warnings, progress/cancel, retained failure context, and finish/export action.

The installed local executable is:

```text
vendor/apps/Siril.app/Contents/MacOS/siril-cli
siril 1.4.4
```

It currently prints `Siril is started as macOS application` followed by the version. `lumen.tools.find_tool` (used by doctor, `PipelineRegistry`, and the darktable preview fallback) checks `PATH` first, then `vendor/apps/<Bundle>.app/Contents/MacOS/<tool>` for `darktable-cli`/`siril-cli`, and rejects any resolved executable outside that bundle. Covered by `tests/test_tools.py`; verified with vendor dirs stripped from `PATH`.

Before generating Siril scripts, inspect the installed 1.4.4 help/resources and validate the exact batch invocation and script commands locally. Use argument arrays only; never shell-concatenate user paths. Source paths with spaces and non-ASCII characters need regression coverage.

## Safety and data rules

- Original photo paths are read-only inputs, even during cleanup and astro calibration.
- All job writes must remain below `<library>/outputs/astro/<job-id>/`.
- Explicit photographer confirmation is required before an astro group is prepared.
- Missing dark/flat/bias frames warn but do not block.
- Only one heavy job may run at once on the 48 GB Mac.
- Cancellation targets only the process owned by that job and can never mark success.
- Exit code zero without the declared TIFF is failure.
- Failed/cancelled job manifests and logs remain recoverable.
- Do not download another oMLX model until its oMLX/MLX vision compatibility is validated; loaded status must come from the live status API.

## Project state and known limitations

- The folder is not a Git repository yet. The user intends to push it eventually, but Git initialization still requires explicit approval.
- `vendor/apps/`, `.superpowers/`, local environments, and build outputs are ignored. Do not remove the project-local Siril bundle.
- darktable 5.6.2 is present at `vendor/apps/darktable.app` and `lumen doctor` reports `darktable-cli` available (on `PATH` and via the project-local fallback).
- Known export gap (unfixed, awaiting user approval): darktable-cli uses the user's `~/.config/darktable/darktablerc`, whose `plugins/lighttable/export/metadata_flags=` is empty, so the render TIFF carries no camera EXIF and Lumen JPEGs have no Make/Model/exposure/capture date. The JPEG XMP `lumen:Export` provenance also embeds absolute library/vendor paths and the source fingerprint.
- There are historical directories named `-p` and `mkdir` at the project root. They were not removed because their ownership/purpose was not established; do not delete them without checking with the user.
- Do not trust README phrases such as “already-installed model” over live diagnostics; update documentation if implementation changes the truth.

## Useful architecture pointers

- `src/lumen/library.py`: coordinated domain workflows and mutation locking.
- `src/lumen/catalog.py`: SQLite schema and transactional persistence.
- `src/lumen/composition.py`: deterministic candidate geometry and identities.
- `src/lumen/omlx.py`: loopback-only tagging and composition-review transport/parsers.
- `src/lumen/pipelines.py`: darktable/Siril process boundaries and managed-path checks.
- `src/lumen/web.py`: loopback/same-origin local JSON API.
- `src/lumen/static/`: dependency-free accessible UI.
- `docs/ARCHITECTURE.md`: original architecture overview; update during astro Task 5.
