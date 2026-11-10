# Changelog

Versions follow `MAJOR.MINOR.PATCH`. The version lives in
`src/lumen/__init__.py` and `pyproject.toml`, shows in the header of the web
UI, in `lumen --version`, and as `version` in `lumen doctor --json` and
`GET /api/status`.

## Unreleased

- Docs: the README and guide link the website, lumen.contact; the README opens with a plain-words description and ends with the licence, warranty disclaimer and photo rights; two sample `.ARW` frames now live in `Examples/`.

## 0.5.0 (2026-11-09)

- Docs: screenshots in `docs/images/` through `README.md` and `docs/GUIDE.md`, a walkthrough recording in `docs/media/`, and two sample `.ARW` frames attached to the release for trying Lumen without a Sony camera.
- Regroup by key: `G` marks or unmarks the focused frame's burst for merging (the same state as its Select for merge box, now outlined in cyan), and `Shift`+`G` marks every burst from the last one marked to this one; `M` merges them as before.
- Belongs with…: `B` on a frame opens a panel under its burst listing up to four bursts from the same camera, shot within 10 minutes, whose frames look most like it (nearest first by Vision feature print, under the sheet's current filters), each with thumbnails, its time and size, and a Move here button. Nothing moves until you choose; a move keeps the frame in its new burst through regrouping and re-import, and pins the burst it left together. `Esc`, `B` again, or moving to another burst closes it.
- The Vision helper returns a feature print (helper schema 3) and takes several images in one run; prints are kept in the catalog and never sent to the browser. Frames imported earlier get theirs the first time Belongs with asks. Rerun `zsh scripts/bootstrap.sh` to rebuild the helper; an older one is refused with that instruction.

## 0.4.1 (2026-11-07)

- Astro layout: the setup stops at reading width, with one row per light and the calibration paths and buttons lined up in one grid instead of pushed to the window's far edge. After a successful stack, its auto-stretched preview sits beside the debrief (`GET /astro/<job>/preview`), stays in view while the debrief scrolls, and opens full size in a new tab; below 980 px it drops under the text. A checkbox label in the panel no longer drops below its box at narrow widths.

## 0.4.0 (2026-11-07)

- Astro debrief: after a stack runs (succeeded or failed) the Astro tab shows What happened, Try now and Next time, written by Lumen's rules from the frames' EXIF and Siril's log, so it needs no AI; a "Where you shot" select (Dark site, City or light pollution, Ship or boat, Handheld; remembered per browser) changes only the advice. An optional "Look at the stack" asks the local review model for notes on the preview; it never changes the settings. A job keeps `frames.json` (what the converted lights' headers and EXIF say), drops Siril's multi-GB `process/` folder after a run, and names its outputs from the EXIF, e.g. `2026-10-18_2129_ILCE-7CM2_28mm_f4_4x15s_ISO3200.tif`.
- Astro debrief names a frame by its camera file name (`frame 1 (DSC02719.ARW)`), as the checklist shows it; a job refused before Siril launched (an original offline or changed) gets no debrief; a stack whose catalog record fails keeps Siril's `result.tif`/`preview.jpg` names.

## 0.3.3 (2026-11-07)

- Astro: a stack also writes `preview.jpg`, Siril's auto-stretch of the linear TIFF; both outputs carry the first light's camera EXIF and maker notes (exiftool; a missing exiftool is logged, not fatal); the Finder button selects the finished TIFF; How this works explains the linear result and how to shoot for a clean stack.

## 0.3.2 (2026-11-07)

The **Burst gap** slider reaches 30 s (it stopped at 10 s), for slow sequences such as a bird working a nest or a long run of handheld frames. A burst merged into a frame you had split off now keeps that merge through every regroup; before, the next regroup at any gap scattered it into single frames. Splitting off the frame a burst was merged into now works: the rest of the burst stays together around its earliest remaining frame, where before the split undid itself (or, in 0.3.1, broke the rest into single frames).

## 0.3.1 (2026-11-07)

Fixes from a review of 0.3.0. **Pick and develop every burst** no longer stops when a burst changes while it runs: that burst is logged as failed and the rest carry on. A frame you chose by hand is never critiqued or replaced, Redo or not, and the job's own pick is recorded as the model's (`model_pick` in the catalog, a `model_pick` event, `"model": true` in the sidecar, "(model's pick)" in the burst line), so it no longer passes for yours. Picks a 0.3.0 cull run made still count as hand picks, because nothing recorded the difference. The job no longer rewrites every sidecar at the end. Merged frames now stay with their burst when the **Burst gap** moves that burst's first frame, chains of merges included. The Burst gap slider keeps keyboard focus, and arrow steps taken during a regroup become one more request with the last value. A burst shown with only some of its frames (under Keepers, or at a page edge) keeps its frame count and best frame. A dropped Overpass download in `lumen map-region` is reported as incomplete instead of a traceback. Hiding the rail now leaves a narrow strip whose **Models** and **Batches** buttons reopen it at the model selects or the batch list (a row of full-height buttons on a phone). **More filters** opens when a folded filter becomes set and otherwise stays as you left it.

## 0.3.0 (2026-11-05)

The contact sheet gets more of the window. The rail's Find field is gone (it was the filter bar's tag text under another name) and a **Batches** button in the sheet heading hides the rail altogether, remembered per browser; **Jump to selected** sits in the heading and **Back to top** floats at the foot of the sheet. The Review model and Fast model selects sit at the top of the rail, above Batches, so the footer is one status line. Clicking the version in the masthead opens **About Lumen**: what Lumen promises, what the status call says is installed (never a path), and credits for GeoNames (CC BY 4.0), Natural Earth, OpenStreetMap (ODbL), darktable, Siril, ExifTool, Pillow, oMLX and the Qwen models. The filter bar keeps one row (tag text, Expand search, status, rating, Reset) and folds the date range, camera, **Tagged only**, **Export failed** and **Refine previews** under **More filters**, which opens itself and counts the filters set whenever one is. One-frame bursts flow side by side like tiles with a one-line header instead of each taking a header and a row. Below 680 px the whole workspace scrolls as one column, and **Back to top** now watches that scroll too, so a phone-width sheet gets the button as well. Scrolling a burst or frame into view (Jump to selected, a rail click, opening a frame) no longer nudges the whole window along with the sheet, which left a dark band under the footer strip.

A background job (tagging, assist, cull, reject scan, triage, export) reports only in the job strip with its Cancel/Clear button; the Local AI line above it no longer repeats the same sentence, so the footer says each thing once.

`lumen map-region` also fetches the OpenStreetMap coastline (`natural=coastline`), drawn as a `coastline` line over the base map, so a bay the 1:10m Natural Earth shoreline generalises away is outlined where frames were taken; the land fill underneath stays the coarse polygon. A region download cut short mid-body is reported as `Overpass answered incomplete: the connection dropped after <n> bytes` instead of a traceback, and the request asks for gzip so the body is several times smaller on a slow link.

A **Burst gap** slider beside Tiles (0.5 to 10 s, default 2 s) regroups every burst at the new gap on release; the value is saved in the catalog (`preferences` table), so imports and restarts group the same way, and merges, splits and hand picks survive as they do on import (`POST /api/burst-gap` `{seconds}`, `burst_gap_seconds` on `GET /api/groups`).

The README and `docs/GUIDE.md` now cover the Map tab (a `Map` section: frames, clusters, overlays, placing frames on a track, `lumen map-region`, the Natural Earth data) and the Assist tab's "Pick and develop every burst" job.

The Assist tab's "Pick and develop every burst" runs one job over every burst: the review model names the best frame, that frame becomes the pick, and the model then suggests develop settings for it, parked on its Develop tab. Bursts already picked and advised are skipped unless Redo is ticked, and a rerun never undoes a frame you chose by hand (`POST /api/photos/cull`, job kind `cull`).

The Map keeps a fixed height beside a side column that scrolls on its own, so ten GPX overlays no longer stretch the canvas; each overlay lists its name over a short day and time range ("Sat 1 Nov, 07:12 to 11:40") with Remove beside it.

Map thumbnails carry their preview's version, so a frame cleared and imported again never shows another frame's cached thumbnail when SQLite reuses its id.

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
