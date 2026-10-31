# Backlog

Deferred items with a known reason. Each entry says what, why it was deferred, and the trigger for picking it up. Keep `docs/HANDOFF.md` as the status ledger; this file only holds work nobody is doing yet.

## From the model-speed research (closed 2026-10-31)

- **Species confidence gate.** Both models list species on frames with no animal: the 8B put six vocabulary species into `subjects` at 0.1 to 0.6 on an empty hill, the 27B a kite at 0.1 to 0.6 on three frames without one. A cheap rule to test first: drop species when `subjects` names no animal, and drop any species under a floor (0.5 looked right on the safari and seabird runs). Needs a few frames with ground truth (ledger step 8) so the floor is judged on a score. Trigger: the next tagging change, or the first time a wrong species reaches an export's IPTC keywords.
- **Other-family review models.** `gemma-4-26b-a4b-it-4bit` (15.3 GB) is the one candidate with plausible upside over the 27B; Gemma 3 12B QAT, Gemma 4 e4b and GLM-4.6V-Flash are sized in `docs/model-speed/HANDOFF.md`. Deferred because each is hours on the satellite link and the expected gain is small. Trigger: an idle night on a faster connection, or an oMLX release that caches hybrid-model prefixes.

## From the 2026-10-31 assessment (`docs/ASSESSMENT.md`)

- **Screenshots and a demo recording.** The README has no pictures; nobody installs to find out what a tool does. Needs a real cull on a loaded library and a 60 to 90 second screen recording. Trigger: the next trip's card, or any day with a few hundred frames imported.
- **Sample ARW files as a release asset.** The quick start needs a Sony camera today. Needs one or two frames the photographer is happy to publish. Trigger: same as above.
- **16 GB test.** The README says the fast model needs about 6 GB and 16 GB is the minimum, but it has only run on a 48 GB M4. Needs another machine; confirm or change the claim to "untested".

## From the 2026-10-23 UI volume review

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

Goal: a `brew install jahaninteractive/lumen/lumen` that pulls Lumen and its tools without an Apple Developer account. A signed DMG is the later step and needs a Developer ID (99 USD/yr) plus notarization; do this first.

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

## README and GUIDE screenshots (parked 2026-10-30)

Goal: real screenshots in `docs/images/` wired into `README.md` and `docs/GUIDE.md` so the repo reads as a working tool, not a spec.

- **Waiting on:** a set of South Georgia `.ARW` frames from the photographer (penguins, ice landscapes): a few bursts of 5-10 near-identical frames for culling and the burst picker, one or two GPS-tagged scenes for Places, one under-exposed or flat-sky frame for a Develop before/after. Nothing in the set should be unwelcome on a public GitHub page.
- **Plan:** import into a scratch library, run tagging and one burst critique against the loaded oMLX model, then drive the UI with the headless Chromium + stdlib CDP harness (no new dependency) to capture the contact sheet, frame view with burst advice, Crop, Develop, EXIF, Places, and the first-run card. Type any place name or caption wording the photographer wants shown rather than leaving it to the model.
- **Trigger:** the photographer drops a folder path in the chat.
