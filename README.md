# Lumen

Lumen is a fully local, non-destructive RAW photography workbench for Apple
Silicon Macs. It catalogs Sony `.ARW` files without moving or changing them,
extracts previews, groups bursts, explains a best-shot choice, optionally tags
previews through oMLX, writes portable sidecars, checks astrophotography frames,
and hands exports to local tools such as darktable.

The included defaults target a 48 GB M4 Mac and the already-installed
`Qwen3.8-27B-4bit` model.

## What is working

- Recursive `.ARW`, JPEG, PNG, TIFF, and HEIC import.
- Embedded Sony RAW preview extraction through ExifTool, with `sips` and
  darktable fallbacks.
- Deterministic 2-second burst grouping by camera and capture time.
- Explainable best-shot scoring from sharpness, exposure, and optional macOS
  Vision face/eye quality.
- Approximate sky background, highlight clipping, star count, and trailing
  checks for astrophotography frames.
- Local oMLX image tagging using the OpenAI-compatible vision request format.
- Non-destructive crop suggestions with optional local oMLX composition advice.
- SQLite catalog plus atomic JSON and XMP sidecars under the Lumen library.
- Quality-92, full-resolution sRGB JPEG export for the current photo or all
  group keepers, with an optional darktable XMP profile.
- Recoverable batch removal and workspace clearing that retain completed exports.
- Keyboard-friendly local contact-sheet UI and JSON-capable CLI.

## Install on the M4 Mac

Prerequisites:

1. macOS 13.5 or newer on Apple Silicon.
2. oMLX installed with `Qwen3.8-27B-4bit` available.
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
  <https://www.darktable.org/install/>. Lumen looks for `darktable-cli`; if the
  application bundle does not put it on `PATH`, add
  `/Applications/darktable.app/Contents/MacOS` to `PATH`.
- Install the Apple Silicon build of Siril from <https://siril.org/download/>.
  Lumen runs only reviewed scripts that you place in its managed outputs
  directory; recipe generation remains an extension point.

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

## Local vision tags

Start oMLX, then tag one frame or the whole catalog:

```sh
omlx start
uv run lumen tag --photo 1
uv run lumen tag --all
```

Lumen reads the local oMLX API key from `LUMEN_OMLX_API_KEY`, or from the local
`~/.omlx/settings.json` written by oMLX. It rejects non-loopback model URLs.
Override the defaults only when needed:

```sh
export LUMEN_OMLX_URL="http://127.0.0.1:8000"
export LUMEN_OMLX_MODEL="Qwen3.8-27B-4bit"
```

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
or **All keepers**; the latter exports the chosen best frame from every batch.
Export uses full available resolution, quality 92, and sRGB. Capture time,
camera/lens details, copyright, ratings, descriptions, and keywords are retained
when available; GPS is omitted.

**Choose XMP…** opens the macOS file picker for an existing `.xmp` processing
profile. The browser remembers your explicit selection for the next export.
**Use camera defaults** clears it. Lumen never automatically selects an XMP
beside an original. If a remembered file has moved, choose it again or use
camera defaults.

The panel shows the managed destination and every successful output's exact
path. **Show outputs in Finder** opens only `<library>/outputs/jpeg/`, whose
default location is `~/Pictures/Lumen Library/outputs/jpeg/`. Filenames such as
`photo-lumen.jpg`, `photo-lumen-1.jpg`, and `photo-lumen-2.jpg` avoid overwriting
earlier exports. Partial failures keep successful JPEGs and report each failed
photo so you can resolve the problem and retry that photo.

Equivalent CLI commands:

```sh
uv run lumen export --photo 42 --profile "/path/to/sony-high-iso.xmp" --json
uv run lumen export --keepers --json
```

If `darktable-cli` is unavailable, the Export panel explains why and disables
export. Install darktable as described above, add its executable directory to
`PATH` if needed, restart Lumen, and reload the page. Check with
`uv run lumen doctor --json`. Import and review remain usable.

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
- Siril stack execution, perceptual near-duplicate grouping, and full historical
  one-click rollback are explicit extension points, not pretend-complete stubs.
- The web server is local-only and has no authentication; do not modify it to
  bind to a LAN address without adding authentication and transport security.

