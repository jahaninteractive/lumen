# Changelog

Versions follow `MAJOR.MINOR.PATCH`. The version lives in
`src/lumen/__init__.py` and `pyproject.toml`, shows in the header of the web
UI, in `lumen --version`, and as `version` in `lumen doctor --json` and
`GET /api/status`.

## Unreleased

Camera JPEGs are first-class sources. Shoot JPEG in the body and the file is published as the camera made it: export skips every renderer (decoder `camera`): a full export with no crop or watermark is lossless (the camera's bytes, `quality: "lossless"`, with Lumen's EXIF, XMP and IPTC written by exiftool), a cropped or watermarked full export re-encodes once at the camera's own quantization tables and chroma subsampling instead of quality 92 / 4:2:0, and an untagged or sRGB-tagged JPEG needs no ICC profile (a body set to AdobeRGB is refused by name). The full-size view of a JPEG is the file itself rather than its small embedded preview. Version 0.2.1.

A Map tab shows where the frames were taken, offline. Frames with a position draw over an eleven-layer Natural Earth base map (coastline, borders, rivers, lakes, roads, towns, land, seas, terrain regions and peaks), clustered when they crowd and drawn as thumbnails from zoom 9; Shown frames follows the Review filters and Whole catalog shows everything, and clicking a frame opens it (narrowing Review to that day when the frame is off the sheet), while the frame view gains a "Show on map" link. Drop a phone's GPX track into the tab (or into `<library>/map/overlays/`) to draw it with its waypoints, and Geotag places the frames that have no fix by interpolating along the track at each capture time, after a dry-run count and with the camera's clock offset shown first; a camera fix is never touched. `lumen map-region <name> --bbox S,W,N,E` adds park boundaries, roads and water for one area from Overpass, the only part that goes online, and only when run. `scripts/bootstrap.sh` fetches the eleven base layers (about 100 MB) at a pinned commit with SHA-256s into `vendor/data/map/` (`LUMEN_MAP_DIR`); Lumen indexes them with every region and overlay into `<library>/cache/map.sqlite` (4.1 s the first time, about 1 ms when nothing changed), and `lumen doctor` reports the layers it found. The canvas has keyboard panning and zoom and a side panel that lists the same frames and tracks for screen readers. Clicking a cluster fans up to 12 of its frames out on a ring of thumbnails (flagged first, then by rating and time, rejects last), so frames shot at one spot can be told apart, with a "Show all n in Review" button that narrows the Review filter to exactly those frames (a new `ids` filter). A third Frames choice, "Everything ever imported", adds frames cleared from the catalog, read from the trash snapshots, as hollow dots whose card shows the thumbnail, time, place, decision and rating and says to import the folder again with Include cleared to work on them. From zoom 9 a cluster shows its best frame with a count badge, and Frames in view lists frames in the same order. Land is filled apart from the sea, and named mountain ranges, plains, deserts, peaks (with their height from zoom 8) and seas appear as you zoom in, each name once its rank suits the zoom. A Map style select offers Workbench (the charcoal look), Natural dark and Natural light, remembered per browser; frames, clusters and tracks keep their colours in all three, with a charcoal outline so they read on light land. The frame dialog names the terrain round a frame's fix after its place, read from the map index and never stored. Version 0.3.0.

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
