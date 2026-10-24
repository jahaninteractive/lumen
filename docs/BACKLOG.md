# Backlog

Deferred items with a known reason. Each entry says what, why it was deferred, and the trigger for picking it up. Keep `docs/HANDOFF.md` as the status ledger; this file only holds work nobody is doing yet.

## From the 2026-10-23 UI volume review

- **Thumbnail cache pruning.** `<library>/cache/thumbs/` is written lazily by `GET /thumb/<id>` (`previews.thumbnail_for`) and never pruned; cleanup, trash, and refined previews leave stale files behind. Harmless disk at ~30 KB each. Pick up when the cache folder grows past a few hundred MB, or fold a sweep into the Clear tab's cleanup.
- **Landing fold.** With a fresh browser the first contact-sheet tile sits ~940 px down at 1512×982: import bar, tag bar, Compose panel, heading, refine row, and filter form all precede the sheet. Everything above now scrolls, so this is layout order, not stickiness. Options: a collapsed filter `<details>`, or moving import and tagging into a tab. Pick up if the photographer keeps collapsing the panel on every visit.
- **Whole-catalog payload.** `/api/groups` returns every photo of every burst on each `load()` (600 KB, 282 ms for 1001 frames; client render 23 ms). Paging is client-side only. Fine to ~5 k frames; add server paging keyed by burst when a real library crosses that or the fetch exceeds ~1 s.
- **Safari and VoiceOver pass.** No manual pass was done for the sticky-layout, thumbnail, and tab changes; the headless checks ran in Chromium only.
- **Crop-load latency on real ARW.** Compose fetches `/api/photos/<id>/crops` on every frame click while the tab is active; synthetic JPEGs answered in 3 ms, but real ARW files run the Vision analyzer. Measure on the user's library before deciding whether to debounce or move the load behind a button.
