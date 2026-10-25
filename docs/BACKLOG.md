# Backlog

Deferred items with a known reason. Each entry says what, why it was deferred, and the trigger for picking it up. Keep `docs/HANDOFF.md` as the status ledger; this file only holds work nobody is doing yet.

## From the 2026-10-23 UI volume review

- **Landing fold.** With a fresh browser the first contact-sheet tile sits ~940 px down at 1512×982: import bar, tag bar, heading, refine row, and filter form all precede the sheet (crops moved from the Compose panel into the frame view). Everything above now scrolls, so this is layout order, not stickiness. Options: a collapsed filter `<details>`, or moving import and tagging into a tab. Pick up if the photographer keeps collapsing the panel on every visit.
- **Whole-catalog payload.** `/api/groups` returns every photo of every burst on each `load()` (600 KB, 282 ms for 1001 frames; client render 23 ms). Paging is client-side only. Fine to ~5 k frames; add server paging keyed by burst when a real library crosses that or the fetch exceeds ~1 s.
- **Safari and VoiceOver pass.** No manual pass was done for the sticky-layout, thumbnail, and tab changes; the headless checks ran in Chromium only.

## From the 2026-10-25 frame view

- **L-size ARW embedded JPEG size unverified.** The Full size toggle serves `JpgFromRaw`; on M-size raws that is 3504x2336. Run `exiftool -b -JpgFromRaw file.ARW | sips -g pixelWidth -g pixelHeight` on an L-size file; if it is not ~7008 px, decide whether to fall back to a darktable render.
- Empty "Suggested keywords" heading shows on untagged frames.
- Remembering the same GPS fix twice adds duplicate manual place rows.
- The viewport is dark while the full-size JPEG loads.
- The "thumbnails pruned" log count includes full-size files.
