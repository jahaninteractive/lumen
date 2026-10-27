# Agent project guide

Read `docs/HANDOFF.md` before changing code. It is the resumable status ledger for this project.

## Product contract

Lumen is a fully local, non-destructive macOS Apple Silicon photography manager for Sony `.ARW` files. Never edit, move, rename, or delete camera originals. Derived previews, sidecars, logs, catalogs, crops, JPEGs, and astro outputs must stay inside the configured Lumen library. Crop and oMLX operations are advisory until the photographer explicitly chooses an action.

The local HTTP server must remain loopback-only and mutation routes must retain host/origin and strict JSON validation. Do not expose source paths, fingerprints, API keys, or raw model output in browser responses. Do not add network services or cloud processing.

## Working style

- Keep AI vendor and model names out of tracked files and commit messages; this repo is shared and describes itself by what it does, not what built it. Refer to this file as `AGENTS.md`.
- Keep changes small and reuse existing patterns; Pillow is the only runtime Python dependency.
- Use red-green tests for behavior changes and run warning-strict tests.
- Preserve the accessible native-control UI, keyboard tab behavior, live announcements, and charcoal/amber/cyan workbench.
- Keep optional tools under `vendor/apps/`; that directory is intentionally ignored by Git.
- This folder is not currently a Git repository. Do not initialize Git unless the user explicitly asks.
- The `.superpowers/` task snapshots and reviews are local audit material and are ignored by Git.

## Commands

```sh
zsh scripts/bootstrap.sh
uv run python -W error -m unittest discover -s tests -v
node --check src/lumen/static/app.js
uv run lumen doctor --json
uv run lumen serve --open
```

Focused tests use `uv run python -W error -m unittest tests.test_<module> -v`.

## Current tool state

- Discovery (`lumen.tools.find_tool`) checks `PATH` first, then falls back to `vendor/apps/darktable.app` / `vendor/apps/Siril.app` `Contents/MacOS/<tool>`, rejecting any resolved executable outside its bundle.
- `<library>/cache/full/` holds the camera's embedded full-size JPEGs by fingerprint (served at `GET /full/<id>`); manual place rows (`source='manual'`) outrank every GeoNames and online row and survive index rebuilds.
- Project-local darktable: `vendor/apps/darktable.app/Contents/MacOS/darktable-cli`, version 5.6.2; `lumen doctor` reports it available. A real Sony `.ARW` export was verified 2026-10-22 (see `docs/HANDOFF.md`).
- Project-local Siril: `vendor/apps/Siril.app/Contents/MacOS/siril-cli`, Apple Silicon, version 1.4.4.
- Native helpers are compiled by `scripts/bootstrap.sh` into `<library>/cache/bin/` (`vision_analyzer`, `raw_developer`). `raw_developer` (`tools/raw_developer.swift`, `Settings.raw_developer_path`, reported by `lumen doctor` and `/api/status`) renders raws through Core Image with the photographer's develop settings and decodes every raw JPEG export without a profile, because darktable's rawspeed refuses Sony reduced-size lossless ARW. It is required for those exports (a missing helper refuses the request naming `scripts/bootstrap.sh`); `--profile` and non-raw exports still use darktable; `sips` is no longer in the export path. Develop settings live in `photo_develop`; saving re-renders the preview but never re-ranks the burst. The preview render also returns a 256-bucket four-channel histogram (`y` Rec. 709 luma, plus `r`, `g`, `b`), clamped to 0...1 before binning so clipped pixels land in the end buckets instead of being dropped; the widget scales to the tallest bucket outside the outer 2% of the range, so a clipping spike cannot flatten the chart. Previews decode at native scale and resample afterwards: Core Image skips raw sharpening entirely at `scaleFactor <= 0.5` and scales noise reduction with it, so a small decode previewed neither (measured 0.37s against 0.35s, because the decode dominates). The helper also reports `defaults` — the decoder's own `contrast`, `sharpness`, `luminance_nr` and `color_nr` for that file, which are not mid-range (sharpness decodes at 1.0) — and the panel parks each unset handle there. Beyond the raw amounts the helper applies `clarity` (a wide unsharp mask, radius 0.8% of the frame, slider travel capped at 0.5 intensity because wider rings a blown highlight) and `lens_correction` (a boolean; the render reports `lens_correction_supported` and the panel disables the box when the file carries no profile). Post-decode filters run at native scale with the resample last, so clarity means the same thing in the preview and the export. Core Image's `detailAmount` is deliberately not exposed: measured on these raws, 0.0 and 1.0 differ by 0.0001 mean. The noise-reduction Off/Auto/Manual mode is derived from the two amounts (unset, both zero, or anything else) rather than stored, so the mode and the amounts cannot disagree. On the Develop tab the full-size button re-renders the raw at its own size (`long_edge: 0`, which skips the viewport clamp) instead of swapping in the camera's embedded JPEG, because sharpening and noise reduction act on single pixels and a render scaled to the viewport averages them away whatever scale it decoded at; `state.develop.zoomShown` records whose full-size view the viewport holds, so leaving the tab hands it back without disturbing an embedded-JPEG zoom.
- Both darktable and Siril are on the user's shell `PATH` today, but the fallback finds them without it.
- darktable-cli reads the user's `~/.config/darktable` config, so Lumen pins `--core --conf plugins/lighttable/export/metadata_flags=5` (EXIF + geotag). JPEG export keeps all EXIF and GPS by user decision (2026-10-22). Every export then makes one `exiftool` pass over the finished JPEG that copies `-MakerNotes` **from the staged original** and writes IPTC IIM `Caption-Abstract` and `Keywords` (with `CodedCharacterSet=UTF8`, or IIM reads as Latin-1); `exiftool` is therefore required for every export, not only raws. The MakerNote cannot come from the render: Core Image's `writeTIFFRepresentation` carries none, and darktable's only has one because of the pin above. IPTC exists because Pillow cannot write IIM and agency ingests still read it rather than XMP.
- oMLX status is live external state. Trust `/api/status`/`lumen doctor`, not configured model names, before claiming a model is loaded or vision-capable.
- Two model roles: `LUMEN_OMLX_FAST_MODEL` (default `Qwen3-VL-8B-Instruct-4bit`; tagging, reject scan, astro triage) and `LUMEN_OMLX_REVIEW_MODEL` (defaults to `LUMEN_OMLX_MODEL`; critique, captions, digest, composition review). Every tagger is built by `Library.tagger_for(role)`, which reads live `/api/status` and refuses an unloaded model; `lumen doctor` and `/api/status` report `omlx_roles`. Tests patch `lumen.library.get_server_status`, `lumen.diagnostics.get_server_status`, and `lumen.library.OmlxTagger`; no test contacts oMLX.
- `LUMEN_NEAR_DUP_DISTANCE` (default 12, `0` disables) splits time bursts into near-duplicate clusters by `metrics.dhash` bit distance.
- `lumen tag --keepers` (default scope of `POST /api/photos/tag`; `tag-all` remains an alias for scope `all`). `<library>/vocabulary.json` is copied once from the packaged starter and then owned by the photographer; digests live in `<library>/digests/`.
- Remove/clear write removed frames' fingerprints to `cleared_fingerprints` (also seeded from trash snapshots); import and ingest skip them unless `--include-cleared`, and the Clear tab / `lumen cleared --remove` removes any already in the catalog.
- Places: `Settings.geonames_dir` (env `LUMEN_GEONAMES_DIR`, default `vendor/data/geonames/`, `None` for plain `Settings(...)`) feeds `<library>/cache/geonames.sqlite`; `Library._resolve_place` stores `photos.place`/`place_country`. Tests build GeoNames fixtures and never read `vendor/data`.
- `LUMEN_GEOCODE_ONLINE=1` (`Settings.geocode_online`, default off) lets `_resolve_place` ask Nominatim when the local result is missing or "near"; answers are cached into the index as `source='online'` rows. Tests patch `urllib.request.urlopen`; no test reaches the internet.
- The user's shell exports `LUMEN_OMLX_MODEL`, which fails `test_cli` doctor-model test; run the suite with `env -u LUMEN_OMLX_MODEL`.

## Plans

- Approved design: `docs/superpowers/specs/2026-10-21-finish-compose-astro-housekeeping-design.md`
- Composition plan (complete): `docs/superpowers/plans/2026-10-21-composition-crops.md`
- Astro plan (complete): `docs/superpowers/plans/2026-10-21-astro-stacking.md`
- Approved design: `docs/superpowers/specs/2026-10-22-travel-volume-culling-design.md`
- Travel plan (complete, on `main`): `docs/superpowers/plans/2026-10-22-travel-volume-culling.md`
- Approved design: `docs/superpowers/specs/2026-10-22-local-ai-assist-design.md`
- Local AI assist plan (complete, on `main`): `docs/superpowers/plans/2026-10-22-local-ai-assist.md`
- Implemented design: `docs/superpowers/specs/2026-10-26-develop-controls-design.md`
- Develop controls plan (complete, on branch `develop-controls`): `docs/superpowers/plans/2026-10-26-develop-controls.md`

`GET /api/status` carries `stale`: reasons the running server no longer matches the tree, shown first in `#system-status` and marked `data-stale`. `diagnostics.source_digest()` hashes the package's `*.py` at import and again per request, so an edit after startup is caught while a `git checkout` that only touches mtimes is not; the Swift helper is compared by mtime against its source. The reasons reach the browser, so they name no absolute paths. This exists because static files are served from disk on every request while the Python stays as imported, which makes a stale server look current and fail as whatever validation error fires first.

Update `docs/HANDOFF.md` after every reviewed milestone or before stopping. Deferred work goes in `docs/BACKLOG.md`.
