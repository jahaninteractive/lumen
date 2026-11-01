# Changelog

Versions follow `MAJOR.MINOR.PATCH`. The version lives in
`src/lumen/__init__.py` and `pyproject.toml`, shows in the header of the web
UI, in `lumen --version`, and as `version` in `lumen doctor --json` and
`GET /api/status`.

## Unreleased

Camera JPEGs are first-class sources. Shoot JPEG in the body and the file is published as the camera made it: export skips every renderer (decoder `camera`): a full export with no crop or watermark is lossless (the camera's bytes, `quality: "lossless"`, with Lumen's EXIF, XMP and IPTC written by exiftool), a cropped or watermarked full export re-encodes once at the camera's own quantization tables and chroma subsampling instead of quality 92 / 4:2:0, and an untagged or sRGB-tagged JPEG needs no ICC profile (a body set to AdobeRGB is refused by name). The full-size view of a JPEG is the file itself rather than its small embedded preview. Version 0.2.1.

CI runs on `macos-15`: GitHub is browning out the `macos-14` image ahead of its retirement on 2026-11-02, which cancelled the 0.2.0 release run.

## 0.2.0 (2026-10-31)

The keyword list sent to the models leaves out the `learned` category (the model's own earlier words; the review model prefilled them on every request, about 2.5 s per tag), and learning now keeps the newest 30 learned words without duplicates.

`scripts/bootstrap.sh` now installs: it `brew install`s a missing `uv` or
ExifTool, starts the command-line tools installer, fetches GeoNames
(`cities500`, `countryInfo.txt`, chosen country files), darktable 5.6.2 and
Siril 1.4.4 into `vendor/` from SHA-256-pinned disk images, offers oMLX 0.7.2
and the two models, and skips anything already present. `--yes` takes every
default; `--no-download` keeps the old check, sync and compile only.
The README gains a "How it was built" section and drops a limit that the
export reply no longer has; `docs/ASSESSMENT.md` records where Lumen stands
against commercial tools and what is still missing.

## 0.1.0 (never tagged; folded into 0.2.0)

First public version. Catalogs Sony `.ARW` files in place, groups bursts with
an explained best-shot proposal, previews, crops and develops through Core
Image, writes sidecars and exports JPEGs with full metadata through darktable
and ExifTool, resolves offline place names from GeoNames, triages and stacks
night-sky frames through Siril, and offers advisory tagging, captions, burst
critique, digests and develop suggestions through a local oMLX model.
