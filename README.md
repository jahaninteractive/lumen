# Lumen

Lumen is the program in this repository (`offline-raw-culler`); the Python
package is `lumen-raw` and the command is `lumen`.

Lumen is a fully local, non-destructive RAW photography workbench for Apple
Silicon Macs. It catalogs Sony `.ARW` files without moving or changing them,
extracts previews, groups bursts, explains a best-shot choice, optionally tags
previews through oMLX, writes portable sidecars, checks astrophotography frames,
and hands exports to local tools such as darktable.

The included defaults target a 48 GB M4 Mac with two oMLX models: a fast
vision model (`Qwen3-VL-8B-Instruct-4bit`) for tagging, problem scans, and
astro triage, and a review model (`Qwen3.8-27B-4bit`) for critique, captions,
digests, and composition advice. Both roles can point at the one 8B model if
you would rather free the memory (see [Local AI](#local-ai)).

## Why Lumen exists

I shoot nature and wildlife, often on trips to places like the South African
bush and Antarctica, and come home with thousands of RAW frames per day from a
Sony body that fires long bursts. Nothing I tried fit how that work actually
goes:

- **Volume first.** A day's cull has to be fast: load a card, see every burst
  as one row, keep the one frame that matters, reject the rest with a single
  key, and move on. Lumen groups bursts, splits near-duplicates, measures
  sharpness, exposure, and eye/face quality, and explains why it proposes a
  pick. The pick is advice; my choice always wins.
- **Originals are sacred.** Years of RAW files are the archive. Lumen never
  moves, renames, or writes into them; everything it makes lives in its own
  library and can be thrown away.
- **Rich metadata without typing.** Capture time, camera, lens, GPS, and a
  real place name ("Neko Harbour, Antarctica") land in the catalog and in
  portable sidecars, so the frames stay searchable years later in any tool.
- **Travel-safe ingest.** Cards get copied to a primary disk and a mirror and
  every byte is verified before I am told it is safe to format.
- **Easy export.** One click from a kept frame to a full-resolution JPEG for
  print, a 2048 px version for sharing, or a web version with a watermark.
- **AI where there is no internet.** Lodges, ships, and field camps have no or
  painfully slow connections, and I do not want my frames uploaded anywhere.
  Tagging, caption drafts, problem scans, burst critique, and day digests run
  on local models through oMLX on the same Mac, offline, and only suggest.
- **Night skies too.** Long trips mean dark skies, so sky-frame triage and a
  reproducible Siril stack live in the same tool.
- **Accessible and keyboard-driven.** Native controls, live announcements, and
  full keyboard operation are part of the design, not an afterthought.

It is a personal tool built for one photographer's workflow and shared as is.

## What is working

- Recursive `.ARW`, JPEG, PNG, TIFF, and HEIC import.
- Embedded Sony RAW preview extraction through ExifTool, with `sips` and
  darktable fallbacks.
- Deterministic 2-second burst grouping by camera and capture time.
- Explainable best-shot scoring from sharpness, exposure, and optional macOS
  Vision face/eye quality.
- Approximate sky background, highlight clipping, star count, and trailing
  checks for astrophotography frames.
- Local oMLX image tagging using the OpenAI-compatible vision request format,
  against a photographer-owned keyword vocabulary that learns from the model.
- One-button local AI assist for keepers: tags, a problem scan with suggested
  rejects, and a draft caption plus alt text, all advisory and skipped when
  already done.
- Burst critique (which frame is best and why), per-day digests, and sky-frame
  triage from the local models; none of them pick, reject, or exclude for you.
- GPS location read from the original and kept in the catalog and Full exports,
  resolved offline to a place name and country from a local GeoNames index.
- Verified card ingest (SHA-256 on every copy, optional mirror) before import.
- Near-duplicate clusters inside a burst from a perceptual difference hash.
- 480 px thumbnail tier for the contact sheet, with Small/Medium/Large tiles.
- A frame view (Enter on any frame) that gathers the full-size image, decisions,
  scores, place, keywords, caption, and crops in one dialog with Frame, Crop, and Develop tabs.
- Non-destructive crop suggestions with optional local oMLX composition advice,
  plus your own drawn crop with an aspect lock.
- Typed keywords that survive re-tagging, and place correction for one frame,
  a whole burst, or every frame within a radius you choose, optionally
  remembered for nearby frames.
- Non-destructive develop controls (brightness, white balance, contrast,
  sharpness, clarity, lens correction, noise reduction, tone curve) that show
  on the sheet and are baked into exported JPEGs.
- SQLite catalog plus atomic JSON and XMP sidecars under the Lumen library.
- sRGB JPEG export for the current photo, all keepers, or the frames the contact sheet filters show, with Full (quality 92,
  full resolution), Share (2048 px), and Web (1600 px) presets, optional
  watermark and darktable XMP profile; the keepers export runs as a cancellable
  background job with a progress dialog. Each frame lands in a subfolder named
  for the date it was taken, and every export carries the camera's MakerNote
  plus IPTC caption and keywords alongside the EXIF and XMP.
- A status bar that says when the running server is older than the files on
  disk, so a stale `lumen serve` cannot look current.
- Guided, reproducible Siril stacking of a confirmed night-sky batch with
  optional darks/flats/biases, progress, cancellation, and retained job logs.
- Recoverable batch removal and workspace clearing that retain completed exports.
- Keyboard-friendly local contact-sheet UI and JSON-capable CLI.

## Install on the M4 Mac

Prerequisites:

1. macOS 13.5 or newer on Apple Silicon.
2. oMLX installed with `Qwen3-VL-8B-Instruct-4bit` (fast, vision) and
   `Qwen3.8-27B-4bit` (review) available. Both must be loaded in oMLX for the
   **Run all local AI** button; `lumen doctor` reports which roles are loaded.
   With only the 8B model loaded, set `LUMEN_OMLX_REVIEW_MODEL` to it (see
   [Local AI](#local-ai)) and skip the 27B download.
3. `uv`, ExifTool, and the Apple command-line developer tools:

   ```sh
   brew install uv exiftool
   xcode-select --install
   ```

From this project folder:

```sh
zsh scripts/bootstrap.sh
uv run lumen doctor
```

The bootstrap creates a private Python environment and compiles the small
macOS Vision helper into `~/Pictures/Lumen Library/cache/bin`. Set
`LUMEN_LIBRARY` before bootstrap if the library should live elsewhere.

## The `vendor/` folder

Everything under `vendor/` is ignored by Git, so a fresh clone starts without
it and nothing in it is ever committed. It holds the optional tools and data
that Lumen looks for locally:

| Path | What goes there | Used by |
|------|-----------------|---------|
| `vendor/apps/darktable.app` | the darktable application bundle; Lumen runs `Contents/MacOS/darktable-cli` | JPEG export, refined previews, RAW preview fallback |
| `vendor/apps/Siril.app` | the Siril application bundle; Lumen runs `Contents/MacOS/siril-cli` | Astro stacking |
| `vendor/data/geonames/` | GeoNames `<CC>.txt`, `cities500.txt`, `countryInfo.txt` | Places (see below) |
| `vendor/downloads/` | installers you keep around (`.dmg`, `.zip`); Lumen never reads it | nothing |

Tool discovery checks `PATH` first and then the project-local bundle, and
refuses a bundle executable that resolves outside its `.app`. `uv run lumen
doctor --json` prints the exact path each tool resolved to under `tools`.
Installing into `/Applications` works too; add the bundle's `Contents/MacOS`
folder to `PATH` if the installer does not. ExifTool is not vendored: it comes
from Homebrew and is required.

- **darktable** (optional, 5.6.2 verified): download the Apple Silicon disk
  image from <https://www.darktable.org/install/>, open it, and drag
  `darktable.app` into `vendor/apps/`. Exports keep EXIF and GPS, so Lumen pins
  darktable's metadata flags regardless of your darktable preferences.
- **Siril** (optional, 1.4.4 verified): download the Apple Silicon build from
  <https://siril.org/download/> and drag `Siril.app` into `vendor/apps/`. The
  managed stacking workflow is validated against Siril 1.4.x only
  (`lumen doctor` reports `supported`); other versions can still run reviewed
  scripts through the legacy hook below.
- **GeoNames** (optional, needed for place names): see [Places](#places) for
  which files to download.

## First run

Check the machine:

```sh
uv run lumen doctor
```

Import a card or folder. Originals stay in place:

```sh
uv run lumen import "/Volumes/PHOTO_CARD/DCIM"
```

Open the local UI:

```sh
uv run lumen serve --open
```

The server listens only on `127.0.0.1:8765`. To use another library:

```sh
uv run lumen --library "/Volumes/Photo Catalog/Lumen" serve --open
```

The line under the header reports the local tools, and reports first when the
running server is older than the files on disk: the browser is served fresh
from disk on every request while the Python stays as it was imported, so an
edit after startup otherwise leaves a stale server looking current and failing
as whatever validation error fires first. Restart `lumen serve` and reload when
you see it. Checking out a different revision without changing content does not
trip it.

## Ingest or import?

- **Ingest** is for a memory card that is still the only copy. It copies every
  file to a primary folder and an optional mirror on a second drive, reads each
  copy back and compares its SHA-256 checksum with the card, then imports the
  verified primary copies. The card is only read.
- **Import** is for photos already on a drive. Lumen reads them where they are
  and adds them to the catalog; nothing is copied or moved. The drive must be
  plugged in to export or refine previews.

Both are tabs in the UI, side by side.

## Ingest a card

```sh
uv run lumen ingest "/Volumes/PHOTO_CARD" --to "/Volumes/Travel SSD/2026-10" --mirror "/Volumes/Backup/2026-10"
```

In the UI, open the **Ingest** tab, choose the card, primary, and optional
mirror folders, and start. The panel ends with one of two verdicts:
*Every file verified on every destination.* or *Do not format the card: N files
were not verified.* The CLI exits 0 only in the first case. Never format a card
until every file is verified on every destination.

Each run writes `<library>/outputs/ingest/<job-id>/manifest.json` and
`ingest.log`. After a verified import, a catalog snapshot is saved beside the
primary originals in `lumen-catalog-backups/<UTC-timestamp>.sqlite3`. Rerunning
the same ingest skips files that are already present and verified.

Import and ingest skip frames you removed or cleared in an earlier session
(matched by content, and still copied and verified by ingest) and report them as
*previously cleared*. To bring them back on purpose, tick **Include frames
cleared in earlier sessions** in the Import tab or pass
`lumen import --include-cleared`.

## Jobs

Import, ingest, tag-all, preview refinement, and astro stacks run as background
jobs, one at a time. The web page shows progress in the job strip at the top
with a **Cancel job** button. From the CLI:

```sh
uv run lumen jobs --json
uv run lumen cancel 12 --json
```

While `lumen serve` is running it owns the job runner; job commands from a
second CLI process exit 2 and say the catalog is in use. Stop the server or use
the web page.

## Cull

Each frame can be flagged, rejected, or cleared, and rated 0 to 5 stars.
Rejecting is a decision, never a deletion. From the CLI:

```sh
uv run lumen decide 42 --flag      # or --reject, --clear
uv run lumen rate 42 4             # 0 clears the rating
```

In the contact sheet, with a frame focused (**Shortcuts** lists these):

- Arrow keys move between frames; Up/Down and Page Up/Page Down move by row;
  Home and End jump to the first or last frame.
- `P` chooses the frame as best in its burst; `R` keeps it and rejects every
  other frame of the burst; `X` rejects; `F` flags; `U` clears the decision;
  `0`–`5` set the rating.
- `C` marks or unmarks a frame for compare (two at most); **Compare marked** opens
  both at 100 percent with panes that pan together.
- `Enter` opens the frame view (see below); `Esc` closes compare or the frame
  view.
- In the frame view, `[` and `]` step to the previous or next frame along the
  filmstrip, `Z` toggles full size, and the culling keys above keep working.

The **Tile size** select (Small/Medium/Large) is remembered by the browser, as
is the chosen tab. The left rail opens only the selected burst's day; days you
open by hand stay open across reloads, and a reload re-selects the last frame
you inspected.

A time burst is split further into near-duplicate clusters, each with its own
best pick, when a frame's difference hash is more than 12 bits from its
cluster's first frame. Set `LUMEN_NEAR_DUP_DISTANCE` to tune the threshold, or
to `0` to keep whole bursts together; frames imported earlier are hashed on
their next reimport.

Culling keeps working while an import or ingest runs. It pauses while tagging,
preview refinement, or a stack runs.

## Frame view

Press `Enter` on a focused frame to open it in a dialog over the sheet. The
image fills two thirds of the dialog, fitted so the whole frame shows; **Full
size** (`Z`) swaps in the largest JPEG the camera embedded in the raw at 1:1
pixels for a sharpness check, and the button names the real pixel size. Arrow
keys pan the full-size image. `[` and `]` step through the sheet without
closing; `Esc` returns to the frame you started on.

The right column has three remembered tabs:

- **Frame**: why the frame ranked where it did, its scores, the decision
  buttons, the location line with **Edit place**, keywords with a box to type
  your own (with vocabulary autocomplete), the caption and alt-text fields,
  and the local AI buttons.
- **Crop**: your own crop, the suggested crops, **Ask local AI**, and
  **Export this frame**, which uses the Export tab's size and watermark.
- **Develop**: adjust the picture itself, described below.

**Develop.** The Develop tab adjusts a raw frame without touching the file.
At the top, a histogram of the current render shares one box with a tone curve;
drag any of its five points, or focus one and nudge it with the arrow keys
(each nudge is announced). Below are sections for Brightness (exposure in
stops), White Balance (temperature and tint), Contrast, Sharpness (sharpness
and clarity, a wide unsharp mask for mid-tone structure), Lens Correction (one
box, disabled when the file carries no profile), and Noise Reduction, which
offers **Auto** (the decoder's own judgement), **Off**, and **Manual** with
luminance and colour amounts. Every slider has a number box and
its own Reset; **Reset all** and **Reset the curve** undo more at once. A box
that reads "not set" means Lumen is leaving that setting to the camera's own
look, which is different from setting it to its middle value. Your changes
save when you leave the tab or step to another frame, and the frame's tile on
the sheet is re-rendered to match (the Best badge in a burst does not move).
Full size re-renders the raw at its own size rather than swapping in the
camera's embedded JPEG, because sharpness, clarity, and noise reduction act on
single pixels and a render scaled to the window averages them away. Leaving the
tab hands the viewer back whatever it was showing before. **Apply to every
frame in this burst** copies the settings to each raw in the burst, rejected
ones included, as a background job; frames that are not raw or whose original is offline are
skipped and counted. **Export this frame** bakes the settings into the JPEG;
tick **Also export the untouched version** (here or in the Export tab) to get
a second file named `-lumen-original.jpg` beside it. **Reset all**, then leaving the frame, clears the saved settings and returns
the tile to its pre-develop render. Develop is unavailable while the original
is offline, or when the frame is not a raw file, and the tab says which. A profile export (darktable
XMP) cannot be combined with develop settings.

**Your own crop.** Drag on the image to draw a rectangle; the outside dims and
the view switches to the Crop tab, where a **Your crop** card shows the
megapixels it keeps. Drag inside the outline to move it; hold Shift to draw a
new one over it; `Esc` during a drag cancels only the drag. The **Crop lock**
menu (Free, 3:2, 2:3, 1:1, 4:5) is remembered. **Use this crop** saves it like
any crop and the JPEG export honours it. A small crop shows the resolution
warning but is yours to keep; only the AI suggestions have an 8 MP floor.

**Keywords.** Type a word and press Enter or **Add**. Your words and the
suggestions you accept are kept in front of the model's words when a frame is
re-tagged; **Remove** drops a typed word and returns an accepted suggestion to
the suggested list. **Forget** in the Review tab still strips a word from every
frame.

**Place.** **Edit place** opens a small form with the name and country, **Apply
to whole burst** (on by default), and **Remember for nearby frames**, which
adds your name to the local place index so later imports within 25 km resolve
to it without any online lookup. A later correction at the same spot replaces
the remembered name. Remember is disabled on a frame with no GPS fix.

## Filters and paging

The filter bar has two rows and every control applies as soon as it changes;
there is no Apply button. Row one holds tag text with Expand, a status choice
(**All**, **Keepers**, **Flagged**, **Unsorted**, **Rejected**), minimum rating,
and Reset. Row two holds the date range, camera, and **Tagged only**. **All**
hides rejected frames; choose **Rejected** to review them. The heading shows
"N of M frames" while a filter is on, and carries Tiles, Refine, Compare
marked, and Shortcuts. The heading and filter bar stay pinned under the tabs
while the sheet scrolls; on a phone-width window they scroll with it. Clicking
the open tab again (Export, Review, …) folds its panel away, and a further
click brings it back. Filters live in the page URL, so a reload or
bookmark keeps them. Frames are grouped by capture day and shown 200 at a time;
the button after the last frame shows the next page. Separate tag-text terms
with `|` to match any of them (`heron | egret`). **Expand with local AI** asks
the fast model for related keywords and fills the text filter with them as `|`
terms. `groups` and `refine-previews` take the same
filters:

```sh
uv run lumen groups --from 2026-10-21 --to 2026-10-22 --camera ILCE-7M4:123 --min-rating 3 --json
uv run lumen groups --keepers --tagged --text heron --json
```

## Places

Every frame with a GPS fix gets a place name and country, resolved on import
and backfilled for earlier frames on the next import: "Madikwe Game Reserve,
South Africa" or "Neko Harbour, Antarctica". Protected areas, parks, islands,
bays, and other landmarks within 25 km win over towns; past that the frame view
says "near <town>" (populated places within 200 km, then any feature within
500 km for open sea). The place shows in the frame view beside the coordinates,
the tag-text filter searches it, and tag and caption prompts, keywords (a
`places` vocabulary category), and day digests use it.

**Edit place** in the frame view corrects a name the index got wrong, or
supplies one it could not find at all: open sea, a drifting boat, or anywhere
the nearest named feature is hundreds of kilometres off. Type the name and
country, then choose how far the correction reaches:

- the frame alone;
- **Apply to whole burst** (on by default), every frame in the burst;
- **Apply to every frame within a radius**, every frame in the catalog whose
  GPS fix is within the distance set by the 1-20 km slider beside it (5 km by
  default), across bursts and imports. Use it when a whole anchorage or a
  morning's drifting shares one name.

Both the radius and **Remember for nearby frames** need a GPS fix on the frame
you are editing and are disabled without one; the name itself can still be set
by hand. Remembering adds the name to the local index at this frame's fix,
where it outranks every looked-up name and survives an index rebuild, so later
imports from the same spot resolve to it offline.

Names come from a local GeoNames index built from the `.txt` files in
`vendor/data/geonames/` (or `LUMEN_GEONAMES_DIR`) into
`<library>/cache/geonames.sqlite`; it rebuilds itself when those files change
(the `.zip` archives beside them are ignored). The folder is not in Git, so
download the files from <https://download.geonames.org/export/dump/>:

- `countryInfo.txt`, which maps country codes to names (always needed);
- `cities500.zip`, towns of 500+ people worldwide, unzipped to `cities500.txt`;
- one `<CC>.zip` per country you shoot in, unzipped to `<CC>.txt`, for parks,
  reserves, bays, and other landmarks. `AQ.txt` (Antarctica) and `ZA.txt`
  (South Africa) are in place on this Mac; add the next country before the trip.

`lumen doctor --json` reports the folder, feature-file count (`countryInfo.txt`
is not counted), place count, and whether online lookup is on under `geonames`.
Without the folder, frames keep their coordinates and simply have no place.
GeoNames data is CC BY 4.0; keep its `readme.txt` in the folder.

The place is written to the JSON sidecar (`place`, `place_country`), to the XMP
sidecar as `Iptc4xmpCore:Location` and `photoshop:Country`, and to exported
JPEG XMP.

Online lookup is off by default. With `LUMEN_GEOCODE_ONLINE=1` (or `true`,
`yes`), a frame with no local place within 25 km asks OpenStreetMap Nominatim
instead, at most one request per second. Privacy: only then do coordinates leave
the Mac. Each answer is cached into the local index, so later frames nearby
resolve offline without asking again.

## Local AI

Everything below runs through your local oMLX server and is advisory: the
models tag, suggest, draft, and explain, but never pick, reject, crop, export,
or delete. Two model roles are used:

| Role | Env var | Default | Used for |
|------|---------|---------|----------|
| fast | `LUMEN_OMLX_FAST_MODEL` | `Qwen3-VL-8B-Instruct-4bit` | tags, problem scan, astro triage |
| review | `LUMEN_OMLX_REVIEW_MODEL` (falls back to `LUMEN_OMLX_MODEL`) | `Qwen3.8-27B-4bit` | burst critique, captions, digests, composition advice |

Every action checks oMLX's live status first and refuses with a message naming
the unloaded model; the UI disables the matching buttons with the same reason.
Trust `lumen doctor` (`omlx_roles`) over the configured names.

To run everything on the 8B model and free about 16 GB, point the review role
at it. Captions and critique get plainer, nothing else changes. The variable
must reach the server process, so set it where you start Lumen:

```sh
LUMEN_OMLX_REVIEW_MODEL=Qwen3-VL-8B-Instruct-4bit uv run lumen serve --open
```

`lumen doctor` then reports both roles as that model, and the `omlx_model`
block still describes the configured 27B default; only `omlx_roles` says what
the actions will actually use.

Start oMLX, then tag:

```sh
omlx start
uv run lumen tag --photo 1
uv run lumen tag --keepers           # frames that are flagged or best and not rejected
uv run lumen tag --all
uv run lumen tag --keepers --retag   # also re-tag already tagged frames
```

Tagging skips frames that already have a description unless `--retag` is
given. Keywords come from `<library>/vocabulary.json`, copied once from the
packaged starter and then yours to edit; confident model words are added back
to it so the vocabulary grows with your shoots. In the UI the **Export** tab has
**Tag keepers with local AI** and, below it, **Tag all untagged** with a
**Re-tag already tagged** toggle. The frame view offers **Add local AI tags** for
one frame and a keyword box for words of your own (they are kept through re-tagging), with suggested keywords you can add or remove; adding a suggested
species teaches it to the vocabulary as wildlife. The **Learned keywords**
panel in the **Review** tab lists the words the models added, and **Forget**
removes a word from every frame and stops it being learned again.

Run the whole assist in one job (tags, problem scan, caption; finished work is
skipped, a failing step is logged and the next still runs):

```sh
uv run lumen assist --keepers        # or --all
```

The UI button is **Run all local AI on keepers** in the Export tab. The other
model actions, each also available from the UI:

```sh
uv run lumen review-rejects --keepers   # Review tab: Scan keepers for problems
uv run lumen critique 12                # frame view: Ask local AI which frame is best
uv run lumen caption 42                 # frame view: Write caption with local AI
uv run lumen digest 2026-10-23          # rail: Write today's digest → <library>/digests/
uv run lumen triage 12                  # Astro tab: Triage lights with local AI
```

Suggested rejects appear in the **Review** tab for you to confirm or dismiss;
nothing is rejected until you say so. A caption is a draft until you save it,
and a caption you wrote is never replaced. Captions and alt text are used only
by the Share export preset.

Lumen reads the local oMLX API key from `LUMEN_OMLX_API_KEY`, or from the local
`~/.omlx/settings.json` written by oMLX. It rejects non-loopback model URLs.
Override the defaults only when needed:

```sh
export LUMEN_OMLX_URL="http://127.0.0.1:8000"
export LUMEN_OMLX_MODEL="Qwen3.8-27B-4bit"
export LUMEN_OMLX_FAST_MODEL="Qwen3-VL-8B-Instruct-4bit"
```

Tagging sends the vocabulary as a stable system prefix so oMLX can cache it.
oMLX's default settings (`hot_cache_only: true`, `hot_cache_max_size: "0"`)
cache nothing; set `hot_cache_only: false` and a real `hot_cache_max_size` in
`~/.omlx/settings.json` and restart oMLX to benefit.

See [docs/MODELS.md](docs/MODELS.md) for the validation evidence and model
recommendation.

## Finish and clear a shoot

In the contact sheet, select a frame (or select its batch in the left rail),
press Enter on it to open the frame view, and use the **Crop** tab (see
[Frame view](#frame-view) for drawing your own). Lumen uses Apple Vision face
rectangles and attention saliency as approximate evidence for subject-safe crop
choices. When that evidence is absent or weak, it says so and offers only the
original plus a centered fallback. Suggested crops below 8 MP are not offered;
any choice below 12 MP shows the exact remaining-resolution warning.

Crop previews include thirds lines. **Use this crop** stores normalized
coordinates in Lumen's catalog and JSON/XMP sidecars; **Keep original** clears
the active crop while preserving provenance. The RAW is never changed. **Ask
local AI** lets the configured oMLX vision model recommend one of the existing
safe choices and explain why. That advice is optional and never selects a crop
for you, so manual choices still work when oMLX is unavailable. Lumen applies
an approved crop to the full-resolution developed image only during JPEG
export.

Next use the **Export** tab. Choose **Current photo**, **All keepers**, or
**Frames the contact sheet filters show**, which exports exactly what the
filter bar matches (all of it, not only the page on screen) as a cancellable
job. A keeper is any frame that is not rejected and is either
flagged or the best frame in its burst, so a single frame stays a keeper unless
you reject it. The tab also explains **Flagged** (marked to keep with F),
**Unsorted** (no decision yet), and **Tagged** (described with keywords by the
local AI). Pick a preset: **Full resolution** exports at full available
resolution, quality 92, sRGB; **Share** resizes to a 2048 px long edge at
quality 85; **Web** resizes to a 1600 px long edge at quality 82. Capture
time, camera/lens details, copyright, ratings, descriptions, and keywords are
retained when available. Full keeps GPS location; Share and Web drop it. One
`exiftool` pass over the finished JPEG copies the camera's MakerNote from the
original (the renderers write none of their own) and writes the description and
keywords as IPTC IIM `Caption-Abstract` and `Keywords` in UTF-8, which agency
ingests and Photo Mechanic read in preference to XMP. `exiftool` is therefore
required for every export, raw or not.
**Watermark text** (optional, up to 80 characters) is drawn bottom-right. The
browser remembers the preset and the watermark.

**Choose XMP…** opens the macOS file picker for an existing `.xmp` processing
profile. The browser remembers your explicit selection for the next export.
**Use camera defaults** clears it. Lumen never automatically selects an XMP
beside an original. If a remembered file has moved, choose it again or use
camera defaults.

**Put each frame in a subfolder named for the date it was taken** is on by
default: the JPEG lands in `<library>/outputs/jpeg/<Mon-DD-YYYY>/`, dated from
the capture time stored for the frame, so one export spanning several days
splits into one folder per day. A frame the camera dated nowhere goes to
`Unknown-Date/`. Untick it to write straight into `jpeg/`.

The panel shows the managed destination and every successful output's exact
path. **Show outputs in Finder** opens only `<library>/outputs/jpeg/`, whose
default location is `~/Pictures/Lumen Library/outputs/jpeg/`. Filenames such as
`photo-lumen.jpg`, `photo-lumen-1.jpg`, and `photo-lumen-2.jpg` avoid overwriting
earlier exports. A keepers export runs as a background job with a progress
dialog and a **Cancel export** button; cancelling keeps the JPEGs already
written. Partial failures keep successful JPEGs and report each failed
photo so you can resolve the problem and retry that photo.

Equivalent CLI commands:

```sh
uv run lumen export --photo 42 --profile "/path/to/sony-high-iso.xmp" --json
uv run lumen export --keepers --json
uv run lumen export --keepers --preset share --json
uv run lumen export --keepers --preset web --watermark "© Your Name" --json
```

Raw frames are rendered by a small native helper that `scripts/bootstrap.sh`
builds; if it is missing, exporting a raw without a darktable profile is refused
with a message telling you to run the script again. If `darktable-cli` is
unavailable, the Export panel explains why and disables export. Install
darktable as described above, add its executable directory to `PATH` if needed, restart Lumen, and reload the page. Check with
`uv run lumen doctor --json`. Import and review remain usable.

## Refine previews

Import shows the camera's small embedded JPEG. **Refine previews** (beside the
contact-sheet heading) re-renders frames from the RAW with darktable at 2048 px,
then re-measures sharpness, exposure, astro, and face/eye signals and re-ranks
each affected burst. Choose the scope — the selected burst, the frames matching
the current filters, or the whole catalog — and follow progress in the job
strip; refined frames carry a **Refined** mark. From the CLI:

```sh
uv run lumen refine-previews --group 3
uv run lumen refine-previews --keepers --from 2026-10-21
uv run lumen refine-previews --all
```

It needs `darktable-cli`; without it the control is disabled with the reason.
Refining never changes a manual pick, decision, or rating, and never touches the
original. Frames whose original changed since import are skipped and logged.

## Stack a night sky

The **Astro** tab turns the selected batch into one managed Siril stack. Lumen
never changes the originals: it links the chosen lights into
`<library>/outputs/astro/<job-id>/`, writes a fixed `stack.ssf` script plus a
`manifest.json` that records every frame's size, modification time, and SHA-256,
and runs the vendored or installed `siril-cli` with that folder as its only
working directory.

1. Select a frame from the night's batch and open **Astro**. Every light in the
   batch is listed with a checkbox; untick frames with trails or clouds. At
   least two lights must remain.
2. Optionally choose dark, flat, and bias folders with the macOS folder picker.
   Folders are read non-recursively; missing, empty, or single-frame sets add a
   warning but never block the stack (Siril 1.4 cannot build a sequence from one
   frame). Warnings always start with the word *Warning*, so they do not rely on
   colour.
3. Choose a **Recipe**: **Tracked** (global star alignment, the default) or
   **Untracked wide-field** (two-pass alignment that keeps only the area common
   to every frame, for untracked tripod shots). The CLI takes
   `--recipe tracked|untracked`.
4. Tick the confirmation checkbox, then **Prepare stack**. The panel shows the
   job number, counts of lights, excluded lights, and calibration frames, the
   warnings, and the managed manifest and log paths. Nothing runs yet.
5. **Run stack** starts Siril. One heavy job runs at a time; a second run
   returns a clear conflict. Progress is announced and polled only while the job
   is queued or running. **Cancel stack** stops only this job's Siril process and
   can never produce a result.
6. On success the panel shows `result.tif` (16-bit RGB, star-aligned, sigma
   rejection, `-rgb_equal`), and **Show job folder in Finder** opens that job's
   folder. Provenance (`astro_stacked`) is recorded for every light and in each
   light's sidecar.

Failed, cancelled, and interrupted jobs keep their `manifest.json` and
`siril.log` in the job folder so you can review the exact command and Siril
output; the log tail is shown in the panel. If Lumen restarts mid-job the job is
marked *interrupted*; prepare a new one. The browser remembers an active job
number so reloading the page resumes watching it.

Equivalent CLI commands:

```sh
uv run lumen astro prepare 3 --confirm --exclude 41 --darks "/Volumes/CAL/darks" --json
uv run lumen astro run 1 --json       # blocks until the job ends
uv run lumen astro status 1 --json
uv run lumen astro cancel 1 --json
```

While `lumen serve` is running it owns the job runner, so run and cancel jobs
from the web page rather than from a second CLI process. To recover a job's
files, look under `~/Pictures/Lumen Library/outputs/astro/<job-id>/`; the
`lights/` entries there are read-only links to your originals, never copies or
moves.

The separate **Clear** tab states the exact selected-batch and whole-workspace
frame counts. **Remove batch from Lumen** removes the selected batch;
**Clear workspace** requires the exact typed word `CLEAR`. Both retain camera
originals and completed exports. After success, batches reload and the panel
shows exact recovery folder and catalog snapshot paths under
`<library>/trash/<UTC-timestamp>/` (normally
`~/Pictures/Lumen Library/trash/`). Previews, Lumen sidecars, and eligible working
files move there with a pre-cleanup SQLite catalog snapshot. There is no
automatic trash expiry or one-click restore; keep that folder for manual
recovery. Export provenance remains recorded even after its source catalog row
is removed.

**Remove previously cleared frames** lists how many frames in the catalog were
already removed or cleared in an earlier session (for example frames imported
before Lumen skipped them, or ones you re-imported on purpose) and removes them
the same recoverable way; bursts they only partly fill are re-ranked.

```sh
uv run lumen groups --json
uv run lumen remove 7 --json
uv run lumen cleared --json
uv run lumen cleared --remove --json
uv run lumen clear CLEAR --json
```

The numeric argument to `remove` is a group ID from `groups`, not a photo ID.
The web XMP picker and Finder reveal use protected same-origin POST requests.

## Legacy darktable denoising/export hook

Create an XMP in darktable containing the denoise, lens, color, and sharpening
modules you want, then run:

```sh
uv run lumen process 1 \
  --pipeline darktable \
  --profile "/path/to/sony-high-iso.xmp"
```

The result goes to the library's `outputs/` directory. Lumen passes an argument
array directly to `darktable-cli`, records the command/result in provenance, and
never overwrites the RAW file.

## Batch and automation commands

All inspection commands can emit JSON:

```sh
uv run lumen import /path/to/shoot --json
uv run lumen groups --json
uv run lumen doctor --json
```

Override the measured winner at any time; the manual pick survives reimport and
regrouping:

```sh
uv run lumen pick 42
```

For astrophotography automation, place a reviewed Siril `.ssf` script inside the
library `outputs/` directory and run it through the allow-listed hook:

```sh
uv run lumen siril "~/Pictures/Lumen Library/outputs/stack.ssf"
```

The hook refuses scripts outside the managed output directory and reports the
exact argument array in its result. Project-specific stack recipe generation
can be added without weakening that boundary.

These stable boundaries are intended for future launchd jobs, folder watchers,
animal/eye analyzers, similarity grouping, Siril stacking, and additional RAW
processors. The module map is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Tests

```sh
uv run python -m unittest discover -s tests -v
zsh scripts/smoke_test.sh
```

The HTTP tests bind an ephemeral loopback port. A locked-down CI sandbox must
allow `127.0.0.1` socket binding. The macOS Vision runtime also needs ordinary
user-session access to Apple's Vision model service.

## Safety and current limits

- Lumen never deletes, renames, moves, or writes into originals.
- Version one uses measured quality to shortlist; it does not claim to know the
  photographer's aesthetic preference. Manual picks are authoritative.
- Star counts and trailing warnings are screening signals, not scientific
  photometry.
- Managed stacking uses two fixed Siril recipes; per-job recipe editing and
  full historical one-click rollback are explicit extension points, not
  pretend-complete stubs.
- The web server is local-only and has no authentication; do not modify it to
  bind to a LAN address without adding authentication and transport security.
- Some API responses assume that loopback-only binding. `POST /api/export`
  serialises its whole provenance record, which carries the source file's
  SHA-256 fingerprint, the render command line, and absolute paths inside the
  library and the configured profile. The browser never reads those fields, and
  `_public_provenance` in `src/lumen/pipelines.py` already strips them from the
  copy embedded in exported JPEGs, but the HTTP reply is unscrubbed. On
  `127.0.0.1` that is disclosure to software the photographer already runs; on
  a LAN or public address it hands library internals to anyone who can reach
  the port. Treat `AGENTS.md`'s "do not expose source paths, fingerprints, API
  keys, or raw model output in browser responses" as the rule to restore before
  any change to where the server binds. Tracked in `docs/BACKLOG.md`.

