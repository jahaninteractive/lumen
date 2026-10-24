# Lumen

Lumen is a fully local, non-destructive RAW photography workbench for Apple
Silicon Macs. It catalogs Sony `.ARW` files without moving or changing them,
extracts previews, groups bursts, explains a best-shot choice, optionally tags
previews through oMLX, writes portable sidecars, checks astrophotography frames,
and hands exports to local tools such as darktable.

The included defaults target a 48 GB M4 Mac with two oMLX models: a fast
vision model (`Qwen3-VL-8B-Instruct-4bit`) for tagging, problem scans, and
astro triage, and a review model (`Qwen3.8-27B-4bit`) for critique, captions,
digests, and composition advice.

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
- GPS location read from the original and kept in the catalog and Full exports.
- 480 px thumbnail tier for the contact sheet, with Small/Medium/Large tiles.
- Non-destructive crop suggestions with optional local oMLX composition advice.
- SQLite catalog plus atomic JSON and XMP sidecars under the Lumen library.
- Quality-92, full-resolution sRGB JPEG export for the current photo or all
  keepers, with an optional darktable XMP profile; the keepers export runs as a
  cancellable background job with a progress dialog.
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

Optional processors:

- Install the current Apple Silicon darktable disk image from
  <https://www.darktable.org/install/>. Lumen looks for `darktable-cli` on
  `PATH`, then inside a project-local `vendor/apps/darktable.app`. If the
  application bundle does not put it on `PATH`, add
  `/Applications/darktable.app/Contents/MacOS` to `PATH`. Exports keep EXIF and
  GPS, so Lumen pins darktable's metadata flags regardless of your darktable
  preferences.
- Install the Apple Silicon build of Siril from <https://siril.org/download/>.
  Lumen looks for `siril-cli` on `PATH`, then inside a project-local
  `vendor/apps/Siril.app`. The managed stacking workflow is validated against
  Siril 1.4.x only (`lumen doctor` reports `supported`); other versions can still
  run reviewed scripts through the legacy hook below.

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

## Ingest a card

Ingest copies a card to a primary destination and an optional mirror, verifies
every copy by SHA-256, then imports the primary copies. The card is only read.

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
- `Enter` opens the loupe at 100 percent; `Esc` closes compare or loupe.

The **Tile size** select (Small/Medium/Large) is remembered by the browser, as
is the chosen tab. The left rail opens only the selected burst's day; days you
open by hand stay open across reloads.

Culling keeps working while an import or ingest runs. It pauses while tagging,
preview refinement, or a stack runs.

## Filters and paging

The filter bar narrows the sheet by date range, camera, decision, minimum
rating, keepers only, tagged only, and tag text; rejected frames are hidden
unless **Show rejected** is ticked. Filters live in the page URL, so a reload or
bookmark keeps them. Frames are grouped by capture day and shown 200 at a time;
the button after the last frame shows the next page. `groups` and `refine-previews` take the same
filters:

```sh
uv run lumen groups --from 2026-10-21 --to 2026-10-22 --camera ILCE-7M4:123 --min-rating 3 --json
uv run lumen groups --keepers --tagged --text heron --json
```

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
**Re-tag already tagged** toggle. The inspector offers **Add local AI tags** for
one frame, with suggested keywords you can add or remove.

Run the whole assist in one job (tags, problem scan, caption; finished work is
skipped, a failing step is logged and the next still runs):

```sh
uv run lumen assist --keepers        # or --all
```

The UI button is **Run all local AI on keepers** in the Export tab. The other
model actions, each also available from the UI:

```sh
uv run lumen review-rejects --keepers   # Review tab: Scan keepers for problems
uv run lumen critique 12                # inspector: Ask local AI which frame is best
uv run lumen caption 42                 # inspector: Write caption with local AI
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
then start in **Compose** above the contact sheet. Lumen uses Apple Vision face
rectangles and attention saliency as approximate evidence for subject-safe crop
choices. When that evidence is absent or weak, it says so and offers only the
original plus a centered fallback. Non-original candidates below 8 MP are
rejected; choices below 12 MP show the exact remaining-resolution warning.

Crop previews include thirds lines. **Use this crop** stores normalized
coordinates in Lumen's catalog and JSON/XMP sidecars; **Keep original** clears
the active crop while preserving provenance. The RAW is never changed. **Ask
local AI** lets the configured oMLX vision model recommend one of the existing
safe choices and explain why. That advice is optional and never selects a crop
for you, so manual choices still work when oMLX is unavailable. Lumen applies
an approved crop to the full-resolution developed image only during JPEG
export.

Next use the **Export** tab. Choose **Current photo**
or **All keepers**. A keeper is any frame that is not rejected and is either
flagged or the best frame in its burst, so a single frame stays a keeper unless
you reject it. Pick a preset: **Full resolution** exports at full available
resolution, quality 92, sRGB; **Share** resizes to a 2048 px long edge at
quality 85. Capture time, camera/lens details, copyright, ratings,
descriptions, and keywords are retained when available. Full keeps GPS
location; Share drops it.

**Choose XMP…** opens the macOS file picker for an existing `.xmp` processing
profile. The browser remembers your explicit selection for the next export.
**Use camera defaults** clears it. Lumen never automatically selects an XMP
beside an original. If a remembered file has moved, choose it again or use
camera defaults.

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
```

If `darktable-cli` is unavailable, the Export panel explains why and disables
export. Install darktable as described above, add its executable directory to
`PATH` if needed, restart Lumen, and reload the page. Check with
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
3. Tick the confirmation checkbox, then **Prepare stack**. The panel shows the
   job number, counts of lights, excluded lights, and calibration frames, the
   warnings, and the managed manifest and log paths. Nothing runs yet.
4. **Run stack** starts Siril. One heavy job runs at a time; a second run
   returns a clear conflict. Progress is announced and polled only while the job
   is queued or running. **Cancel stack** stops only this job's Siril process and
   can never produce a result.
5. On success the panel shows `result.tif` (16-bit RGB, star-aligned, sigma
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

```sh
uv run lumen groups --json
uv run lumen remove 7 --json
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
- Managed stacking uses one fixed Siril recipe; per-job recipe editing,
  perceptual near-duplicate grouping, and full historical one-click rollback are
  explicit extension points, not pretend-complete stubs.
- The web server is local-only and has no authentication; do not modify it to
  bind to a LAN address without adding authentication and transport security.

