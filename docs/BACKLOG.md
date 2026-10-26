# Backlog

Deferred items with a known reason. Each entry says what, why it was deferred, and the trigger for picking it up. Keep `docs/HANDOFF.md` as the status ledger; this file only holds work nobody is doing yet.

## From the 2026-10-23 UI volume review

- **Landing fold.** With a fresh browser the first contact-sheet tile sits ~940 px down at 1512×982: import bar, tag bar, heading, refine row, and filter form all precede the sheet (crops moved from the Compose panel into the frame view). Everything above now scrolls, so this is layout order, not stickiness. Options: a collapsed filter `<details>`, or moving import and tagging into a tab. Pick up if the photographer keeps collapsing the panel on every visit.
- **Whole-catalog payload.** `/api/groups` returns every photo of every burst on each `load()` (600 KB, 282 ms for 1001 frames; client render 23 ms). Paging is client-side only. Fine to ~5 k frames; add server paging keyed by burst when a real library crosses that or the fetch exceeds ~1 s.
- **Safari and VoiceOver pass.** No manual pass was done for the sticky-layout, thumbnail, and tab changes; the headless checks ran in Chromium only.

## From the 2026-10-25 frame view

- **L-size ARW embedded JPEG size unverified.** The Full size toggle serves `JpgFromRaw`; on M-size raws that is 3504x2336. Run `exiftool -b -JpgFromRaw file.ARW | sips -g pixelWidth -g pixelHeight` on an L-size file; if it is not ~7008 px, decide whether to fall back to a darktable render.
- Empty "Suggested keywords" heading shows on untagged frames.
- Correcting a remembered place now replaces manual rows within 1 km; rows further apart than that but inside the 25 km radius still coexist and the nearer one wins.
- The viewport is dark while the full-size JPEG loads.
- The "thumbnails pruned" log count includes full-size files.

## From the 2026-10-25 own crop

- The 8 MP crop error passes through the route by string match on the message; a typed error would be sturdier.
- Reopening the same frame keeps the previous crop outline until its crops arrive.

## Distribution: Homebrew tap + GitHub Release (planned 2026-10-26)

Goal: a `brew install nasir/lumen/lumen` that pulls Lumen and its tools without an Apple Developer account. A signed DMG is the later step and needs a Developer ID (99 USD/yr) plus notarization; do this first.

What ships today and how it would move:

| Piece | Today | Plan |
|---|---|---|
| Lumen package | `uv sync` from source | GitHub Release tarball, formula runs `uv tool install` on it |
| `vision_analyzer` | built on the user's Mac with `swiftc` (needs Xcode CLT) | prebuilt in CI on a `macos-14` runner, attached to the release, installed by the formula |
| exiftool | expected on PATH | `depends_on "exiftool"` |
| darktable, Siril | `vendor/apps/`, gitignored, 535 MB | `depends_on cask: "darktable"`, `depends_on cask: "siril"`; extend `find_tool` fallback to `/Applications/*.app` |
| GeoNames | `vendor/data/geonames/`, gitignored, 70 MB | new `lumen setup`: downloads the dump into `<library>/cache/` on first run, SHA-256 pinned in the repo |
| oMLX | separate server | unchanged; `lumen doctor` already reports it |

Steps:

1. Tag `v0.1.0`; GitHub Actions workflow on tag push builds `vision_analyzer`, attaches it and the sdist to the release.
2. Add `lumen setup` (explicit, user-triggered download with checksum check). Note the one-off outbound fetch in `AGENTS.md`, since the contract currently says no network.
3. Create `homebrew-lumen` repo with `Formula/lumen.rb`: `uv tool install` the tarball, install the prebuilt analyzer, declare the dependencies above. Caveat text points at `lumen setup` and `lumen doctor`.
4. Verify on a clean account: `brew install`, `lumen setup`, `lumen doctor --json` all green.

Trigger: when someone other than the author wants to run Lumen. Pick up the DMG (PyInstaller bundle, `codesign --options runtime`, `notarytool`, `stapler`, DMG via `hdiutil`) when non-terminal users appear and a Developer ID exists.

## Keep the export 16-bit through crop and resize

`raw_developer` writes a genuine 16-bit TIFF, but Pillow caps RGB TIFFs at
8 bits per channel when `export_jpeg` reads it back, so the crop, resize and
watermark all run on 8-bit pixels. The develop maths itself happens in Core
Image's float pipeline, which is where banding would otherwise have come from,
so this is a refinement rather than a defect.

Moving the crop and the long-edge resize into the helper — both are Core Image
one-liners — would push the truncation to after the resample. Deliberately not
done in `2026-10-26-develop-controls-design.md`: the Pillow assembly is
working, tested code with EXIF-orientation subtleties, and the risk of
double-applying orientation outweighs a difference nobody has seen yet.

Trigger: visible banding in a smooth gradient (a sunset sky) in an exported
JPEG that looked clean in the develop preview.

## Carry the camera MakerNote into exported JPEGs

Neither `sips` nor Core Image carries tag 37500 through the render, so the
MakerNote rebase in `export_jpeg` only ever fires for darktable renders — and
darktable cannot decode this camera's reduced-size raws. Exports from this
library therefore ship without the Sony MakerNote. Sourcing it with
`exiftool -TagsFromFile <original>` instead of from the render would fix it
for every decoder.

Trigger: wanting Sony-specific capture data (lens corrections, creative look,
focus point) to survive into the JPEG.
