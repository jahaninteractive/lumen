# Changelog

Versions follow `MAJOR.MINOR.PATCH`. The version lives in
`src/lumen/__init__.py` and `pyproject.toml`, shows in the header of the web
UI, in `lumen --version`, and as `version` in `lumen doctor --json` and
`GET /api/status`.

## 0.2.0 (unreleased)

`scripts/bootstrap.sh` now installs: it `brew install`s a missing `uv` or
ExifTool, starts the command-line tools installer, fetches GeoNames
(`cities500`, `countryInfo.txt`, chosen country files), darktable 5.6.2 and
Siril 1.4.4 into `vendor/` from SHA-256-pinned disk images, offers oMLX 0.7.2
and the two models, and skips anything already present. `--yes` takes every
default; `--no-download` keeps the old check, sync and compile only.
The README gains a "How it was built" section and drops a limit that the
export reply no longer has; `docs/ASSESSMENT.md` records where Lumen stands
against commercial tools and what is still missing.

## 0.1.0 (unreleased)

First public version. Catalogs Sony `.ARW` files in place, groups bursts with
an explained best-shot proposal, previews, crops and develops through Core
Image, writes sidecars and exports JPEGs with full metadata through darktable
and ExifTool, resolves offline place names from GeoNames, triages and stacks
night-sky frames through Siril, and offers advisory tagging, captions, burst
critique, digests and develop suggestions through a local oMLX model.
