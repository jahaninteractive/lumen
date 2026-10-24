# Agent project guide

Read `docs/HANDOFF.md` before changing code. It is the resumable status ledger for this project.

## Product contract

Lumen is a fully local, non-destructive macOS Apple Silicon photography manager for Sony `.ARW` files. Never edit, move, rename, or delete camera originals. Derived previews, sidecars, logs, catalogs, crops, JPEGs, and astro outputs must stay inside the configured Lumen library. Crop and oMLX operations are advisory until the photographer explicitly chooses an action.

The local HTTP server must remain loopback-only and mutation routes must retain host/origin and strict JSON validation. Do not expose source paths, fingerprints, API keys, or raw model output in browser responses. Do not add network services or cloud processing.

## Working style

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
- Project-local darktable: `vendor/apps/darktable.app/Contents/MacOS/darktable-cli`, version 5.6.2; `lumen doctor` reports it available. A real Sony `.ARW` export was verified 2026-10-22 (see `docs/HANDOFF.md`).
- Project-local Siril: `vendor/apps/Siril.app/Contents/MacOS/siril-cli`, Apple Silicon, version 1.4.4.
- Both are on the user's shell `PATH` today, but the fallback finds them without it.
- darktable-cli reads the user's `~/.config/darktable` config, so Lumen pins `--core --conf plugins/lighttable/export/metadata_flags=5` (EXIF + geotag). JPEG export keeps all EXIF and GPS by user decision (2026-10-22).
- oMLX status is live external state. Trust `/api/status`/`lumen doctor`, not configured model names, before claiming a model is loaded or vision-capable.
- The user's shell exports `LUMEN_OMLX_MODEL`, which fails `test_cli` doctor-model test; run the suite with `env -u LUMEN_OMLX_MODEL`.

## Plans

- Approved design: `docs/superpowers/specs/2026-10-21-finish-compose-astro-housekeeping-design.md`
- Composition plan: `docs/superpowers/plans/2026-10-21-composition-crops.md`
- Next plan: `docs/superpowers/plans/2026-10-21-astro-stacking.md`

Update `docs/HANDOFF.md` after every reviewed milestone or before stopping.
