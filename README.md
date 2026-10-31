# <img src="src/lumen/static/mark.svg" width="32" height="32" alt=""> Lumen

The Python package is `lumen-raw` and the command is `lumen`.

Lumen is a fully local, non-destructive RAW photography workbench for Apple
Silicon Macs. It catalogs Sony `.ARW` files without moving or changing them,
extracts previews, groups bursts, explains a best-shot choice, optionally tags
previews through oMLX, writes portable sidecars, checks astrophotography frames,
and hands exports to local tools such as darktable.

Lumen runs on any Apple Silicon Mac with 16 GB of memory using one oMLX
model, the fast vision model (`Qwen3-VL-8B-Instruct-4bit`, about 6 GB loaded),
which covers every job: tagging, problem scans, astro triage, critique,
captions, digests, composition advice, and develop suggestions. With more
memory, such as the 48 GB M4 it was built on, add the review model
(`Qwen3.8-27B-4bit`, about 16 GB loaded) for closer reads of a frame and bolder
develop advice, and keep it loaded for whatever else you use oMLX for, such as
its chat. **Review model** and **Fast model** dropdowns in the footer strip move
either role between the two while Lumen runs (see
[Choosing the models](docs/GUIDE.md#choosing-the-models)).

Install below; the workflow is in [docs/GUIDE.md](docs/GUIDE.md).

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

It is a personal tool built for one photographer's workflow and shared as is
under the [MIT License](LICENSE). Fixes are welcome; see
[CONTRIBUTING.md](CONTRIBUTING.md), and [SECURITY.md](SECURITY.md) for
reporting anything sensitive. Versions are listed in [CHANGELOG.md](CHANGELOG.md).

## Install

Prerequisites:

1. macOS 13.5 or newer on an Apple Silicon Mac with at least 16 GB of memory.
   Lumen was built and measured on a 48 GB M4; a 16 GB machine has not been
   tested, but the fast model needs about 6 GB and nothing else in Lumen is
   memory-hungry.
2. oMLX installed with `Qwen3-VL-8B-Instruct-4bit` (fast, vision) loaded.
   `Qwen3.8-27B-4bit` (review) is optional and wants about 16 GB more: with
   only the 8B loaded, Lumen starts the review role on it too. `lumen doctor`
   reports which roles are loaded (see [Local AI](docs/GUIDE.md#local-ai)).
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
- **GeoNames** (optional, needed for place names): see [Places](docs/GUIDE.md#places) for
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

The header reports tool health ("Local AI online" or "Local AI offline", then
"<tool> missing" for each unavailable tool), and reports first when the
running server is older than the files on disk: the browser is served fresh
from disk on every request while the Python stays as it was imported, so an
edit after startup otherwise leaves a stale server looking current and failing
as whatever validation error fires first. Restart `lumen serve` and reload when
you see it. Checking out a different revision without changing content does not
trip it.

## Workflow at a glance

Each step is a section of [docs/GUIDE.md](docs/GUIDE.md):

- [Ingest a card](docs/GUIDE.md#ingest-a-card): copy to a primary and a
  mirror, verify every byte, then import. Or
  [import](docs/GUIDE.md#ingest-or-import) frames already on a drive.
- [Cull](docs/GUIDE.md#cull): bursts as rows, one key per decision, an
  explained best-shot proposal you can override.
- [Frame view](docs/GUIDE.md#frame-view): full-size check, crop, develop,
  EXIF, keywords, caption, and place in one dialog.
- [Filters and paging](docs/GUIDE.md#filters-and-paging), and
  [Places](docs/GUIDE.md#places): offline place names from GeoNames, typed
  GPS for frames without a fix.
- [Local AI](docs/GUIDE.md#local-ai): tags, problem scan, captions, burst
  critique, digests, and develop suggestions through oMLX, all advisory.
- [Finish and clear a shoot](docs/GUIDE.md#finish-and-clear-a-shoot): crop,
  export presets, and recoverable removal.
- [Refine previews](docs/GUIDE.md#refine-previews) and
  [Stack a night sky](docs/GUIDE.md#stack-a-night-sky).
- [Batch and automation commands](docs/GUIDE.md#batch-and-automation-commands),
  with the module map in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

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

