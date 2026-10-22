# Lumen implementation handoff

Last updated: 2026-10-22 (Africa/Johannesburg) — astro Task 3 complete

## Resume here

The active repository is `/Users/nasir/Developer/localimagemanip`. The project is runnable and its complete warning-strict test suite currently passes **269 tests** (run with `env -u LUMEN_OMLX_MODEL`; the user's shell override fails the doctor default-model assertion in `test_cli`). `node --check src/lumen/static/app.js` also passes.

The composition implementation is complete through Task 5 and its independent re-review is approved with no open findings. Read:

- `.superpowers/sdd/2026-10-21-composition-crops/progress.md`
- `.superpowers/sdd/2026-10-21-composition-crops/task-5-report.md`
- `.superpowers/sdd/2026-10-21-composition-crops/task-5-review.md`
- `.superpowers/sdd/2026-10-21-composition-crops/task-5-rereview.md` if present
- `.superpowers/sdd/2026-10-21-composition-crops/plan-final-rereview-2.md`

The composition plan is complete. Its final independent re-review is approved with 0 Critical, 0 Important, and 0 Minor findings. Astro Task 1 is the active next milestone.

Astro Task 1 is complete and independently approved with no open findings. It adds validated persisted jobs, strict transitions/recovery, exclusive per-catalog runner ownership across local processes, one heavy worker, and owned-process cancellation/reaping with retryable shutdown. Read `.superpowers/sdd/2026-10-21-astro-stacking/task-1-rereview-2.md`; Astro Task 2 (Siril preparation) is committed (`deab8bd`). Astro Task 3 (execute/cancel/import) is complete and independently re-reviewed with 0 Critical / 0 Important open; see "Astro Task 3" below. **Task 4 (CLI and protected API) is the next milestone.** Task 3 is uncommitted unless `git log` shows a commit after `deab8bd`.

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
env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest discover -s tests -v   # 269 passed (2026-10-22)
```

The latest whole-plan fixes prove the default candidates include one contextual and one tighter crop, source replacement invalidates an old crop before the fingerprint-upsert failure boundary, and every export rejects source bytes not successfully cataloged after an interrupted replacement. See `.superpowers/sdd/2026-10-21-composition-crops/plan-final-fix-report.md`.

No manual Safari/VoiceOver pass, real Sony RAW crop visual check, Apple Vision hardware pass, or live oMLX composition request has been performed. Those remain integration risks, not known automated failures.

## Real darktable export check (2026-10-22)

`test-image/DSC01027.ARW` (Sony ILCE-7CM2, untracked, not committed) exported via `lumen --library <scratch> export --keepers` in ~12 s:

- Original unchanged: sha256 `21a7428d…ac3d8`, mtime `1792167197.79`, size 34263040 identical before/after; no sidecar written beside it.
- JPEG 7032×4688 (darktable's full uncropped-border render, `--width 0 --height 0`; raw 7040×4688), embedded sRGB ICC + EXIF ColorSpace sRGB, quantization tables exactly Pillow quality 92.
- No camera EXIF in that first JPEG; fixed afterwards (see known limitations). The source has no GPS, so GPS was verified on a scratch copy with injected coordinates.

## Next milestone: managed Siril astro stacking

Use `docs/superpowers/plans/2026-10-21-astro-stacking.md` task by task. Do not jump straight to the UI.

1. **Job catalog and single-worker lifecycle** — complete and independently approved.
2. **Reproducible Siril preparation** — complete; see "Astro Task 2" below.
3. **Execute/cancel/import** — complete; see "Astro Task 3" below.
4. **CLI and protected API** — explicit group confirmation, selection/exclusion, calibration folders, status/log tail, run, and cancellation.
5. **Astro web workflow** — explicit confirmation, per-light inclusion, calibration folder pickers, warnings, progress/cancel, retained failure context, and finish/export action.

The installed local executable is:

```text
vendor/apps/Siril.app/Contents/MacOS/siril-cli
siril 1.4.4
```

It currently prints `Siril is started as macOS application` followed by the version. `lumen.tools.find_tool` (used by doctor, `PipelineRegistry`, and the darktable preview fallback) checks `PATH` first, then `vendor/apps/<Bundle>.app/Contents/MacOS/<tool>` for `darktable-cli`/`siril-cli`, and rejects any resolved executable outside that bundle. Covered by `tests/test_tools.py`; verified with vendor dirs stripped from `PATH`.

### Astro Task 2 — what exists (2026-10-22)

Evidence (exact `--version`/`--help`, help sources, per-branch dry runs): `.superpowers/sdd/2026-10-21-astro-stacking/task-2-siril-validation.md`.

- `src/lumen/astro.py`: `probe_siril` (parses the macOS banner; argument array), frozen `SirilCapabilities`/`AstroFrame`/`AstroFrameSet`/`AstroManifest`, `siril_script` (gated to Siril `>=1.4.0,<1.5.0`; the script also starts `requires 1.4.0 1.5.0`), `prepare_siril_job`, `load_manifest(path, output_root)`.
- Job layout `outputs/astro/<job-id>/`: `lights|darks|flats|biases/<kind>_NNNNN.<ext>` read-only-source symlinks with fixed ASCII names (source paths never enter the script), empty `siril.config`, `stack.ssf` and `manifest.json` written once and chmod 0444. The job dir is created exclusively; a mid-prep failure removes only that dir.
- Batch command (`SirilPipeline.job_command`): `[siril-cli, -d <job>, -i <job>/siril.config, -o, -s <job>/stack.ssf]`. `-i` needs an existing file (missing → exit 1); `-o` is offline; without `-i` Siril writes `~/Library/Application Support/org.siril.Siril` (created once by a `help` probe on 2026-10-22, not by jobs).
- Script: optional bias/flat/dark masters, light calibration (`-dark` preferred over `-bias`, `-cc=dark -cfa` only with darks, `-equalize_cfa` only with flats), `register` (global star alignment), `stack ... rej w 3 3 -norm=addscale -output_norm -rgb_equal -filter-wfwhm=3k -filter-round=3k -out=result`, `mirrorx -bottomup`, `savetif ../result` (16-bit).
- Rules: ≥2 selected lights; excluded lights recorded, not linked, and cannot reappear as calibration; calibration folders non-recursive, hidden files skipped, `.arw/.fit(s)/.fts/.tif(f)` only; missing, empty, or single-frame calibration sets warn and are skipped (Siril 1.4.4 builds no sequence from one frame); trailing warnings only for selected lights; full sha256 + size + mtime_ns, rejected if the source changes during hashing.
- `lumen doctor` reports Siril `version: "1.4.4"`, `supported: true` (was the banner line).
- Dry-validated with the installed 1.4.4 on synthetic RGGB CFA FITS (no user photos), library path with space/non-ASCII: every calibration branch exit 0 with 16-bit RGB `result.tif`; sources unchanged. **Not yet run on real Sony ARW lights.**

### Astro Task 3 — what exists (2026-10-22)

- `Library.prepare_astro_job(group_id, *, confirmed, excluded_photo_ids, darks, flats, biases) -> Job`: requires `confirmed is True`; refuses lights whose bytes differ from their catalog fingerprint; probes Siril via `find_tool`+`probe_siril`; `Catalog.create_prepared_job` inserts a `pending` placeholder, prepares the job dir with no SQLite lock held (frame hashing can take seconds), then stores the paths or deletes the placeholder on failure. Runner startup purges placeholders left by a crash.
- `Library.run_astro_job(job_id, *, timeout_seconds=7200)`: verifies the manifest on the caller thread, submits to the lazily created `LocalJobRunner` (one per `Library`; `close()` shuts it down). The worker re-loads the manifest, then `astro.execute_job`: refuses if `result.tif`/`process/`/`masters/` already exist, revalidates every frame (exists, link still points at it, size, mtime_ns, sha256), launches the manifest command via Popen argument array with cwd=job dir, streams stdout+stderr (ANSI-stripped) into `<job>/siril.log` from a reader thread, writes coarse progress 0.05→0.1→0.3 (calibrate)→0.5 (register)→0.9 (savetif)→0.95 (TIFF verified) from the polling thread, enforces the timeout (terminate→kill), and requires a 16-bit TIFF (`BitsPerSample` all 16) even on exit 0.
- Success bookkeeping (`_record_astro_result`, worker thread, own `Catalog` under the library mutation lock): artifact kind `astro_tiff` (photo_id None, settings include job_id, siril_version, command, counts, light_fingerprints, light_photo_ids, exit_code, bits_per_sample), one global and one per-light `astro_stacked` event, light sidecars rewritten, then `result_path`; any failure discards the artifact/events, rewrites sidecars, and fails the job. Cancellation returns no result; a recorded result is never undone by a late cancel (`LocalJobRunner.cancel` returns False once `result_path` is set — a two-line Task 1 change made for this).
- `Library.cancel_job`: queued jobs cancel without a runner; running jobs raise RuntimeError if this `Library` owns no runner. `wait_for_job(job_id, timeout)` for callers/tests.
- Verified: fake Siril as a real child process (changed/missing frames, stale outputs, timeout, exit 3, exit 0 without TIFF, 8-bit TIFF, mid-run cancel, cancel racing a clean exit, bookkeeping failure compensation, unchanged source bytes/mtimes in every case); mutation checks on revalidation, exit/TIFF, cancel, and late-cancel guards; `execute_job` against real Siril 1.4.4 on synthetic CFA FITS (16×16×16 TIFF, sources unchanged). **Still not run on real Sony ARW lights.**

### Task 4 must handle (from Task 3 review)

- The web server creates a `Library` per request; `Library.close()` shuts down its runner and would cancel a running job. Task 4 needs one process-level runner (module-level, keyed by database path, or an injected `job_runner`) shared by all request `Library` objects, and `cancel_job` must route to it.
- Status/log-tail must whitelist fields: never serialize `artifact.settings` (contains `command` with absolute paths and `light_fingerprints`), manifests, or job `error` strings containing source basenames without review; job `result_path` is authoritative over status races.
- `run_astro_job` rejects a `pending` placeholder (`load_manifest` ValueError); listings may filter `manifest_path == 'pending'`.
- A manifest tampered between submit and launch is caught by the worker's re-load (untested race).

### Task 3 inherited notes (now implemented)

- Revalidate every frame's size/mtime_ns/sha256 from `load_manifest(path, settings.output_dir)` immediately before launch; `load_manifest` raises `ValueError` for tampered/foreign manifests and may raise `OSError` for unreadable files.
- Refuse to launch if `result.tif` (or stale `process/`) already exists so a rerun can't mistake an old TIFF for success; exit 0 without the TIFF is failure.
- Write the log to `<job>/siril.log`; `prepare_siril_job` raises `FileExistsError` for a reused job ID — map it to a job-conflict error.
- Task 4: never serialize the raw manifest to the browser (it holds absolute source paths and fingerprints).

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
- JPEG metadata (user decision 2026-10-22): keep all EXIF including GPS. darktable-cli read the user's `~/.config/darktable/darktablerc` (empty `metadata_flags`), which stripped all EXIF; Lumen now pins `metadata_flags=5` (EXIF + geotag) and copies the full EXIF and GPS IFDs. Verified on a scratch ARW with injected GPS: every ExifIFD/GPS value identical to darktable's render.
- MakerNote: Pillow alone breaks Sony's absolute MakerNote offsets, so export runs `exiftool -TagsFromFile render.tif -MakerNotes` on the finished JPEG when the render has one; missing/failed exiftool fails the export and publishes nothing. Real export: all 117 Sony tags (shutter count 554), no MakerNote warnings.
- The JPEG's XMP `lumen:export` record omits `command` and `source_fingerprint` and keeps only the profile file name; the catalog and sidecars keep the full settings.
- Sony IFD0 lens-correction params (0x7032/0x7035/0x7037) are deliberately not copied; they describe the RAW, and applying them to a corrected render would double-correct.
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
