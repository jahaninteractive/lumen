# Workflow round: duplicates, resume, loupe, search, keywords, astro recipes, web export

Date: 2026-10-24. Approved by the user as items "6, 1, 3, 4, 5, 7, 8, 9, then housekeeping" from a suggestion list; the user asked for no brainstorming round, so every "how" below is the implementing agent's call unless marked otherwise.

## Intent

Make a day's cull faster on real bursts and keep the local-AI data clean. Nothing here adds a dependency, a network service, or a write to a camera original.

## Goals

1. **Keyword hygiene (items 6 and 7).** A panel lists the words the vocabulary learned from the models, with how many photos use each, and lets the photographer forget a word. A forgotten word is removed from every photo's keywords, never re-learned, and the sidecars are rewritten. Confirming a weak species guess from the inspector learns it as wildlife, not as a free-text word.
2. **Near-duplicate clusters (item 1).** Inside each time burst, frames whose perceptual hash drifts beyond a threshold from the cluster's first frame start a new burst with its own best pick. Time grouping stays the outer rule; the threshold is a calibration knob with 0 as off.
3. **Resume (item 3).** Reloading the page re-selects and refocuses the last inspected frame when it is still in view, paging forward if needed.
4. **Loupe filmstrip (item 4).** `[` and `]` move the loupe to the previous or next visible frame; P/R/X/F/U/0-5 keep working on the shown frame; closing the loupe focuses the frame last shown.
5. **Search expansion (item 5).** The tag-text filter can ask the fast model to expand a query into vocabulary terms; the filter then matches any of the terms (`a | b | c`). Plain substring search is unchanged.
6. **Astro recipes (item 8).** Two Siril recipes: `tracked` (today's script) and `untracked` (two-pass registration with minimum-area framing for drifting wide-field sequences). Same manifest, fingerprinting, and validation.
7. **Web export (item 9).** A third preset `web` (1600 px long edge, quality 82, no GPS) and an optional watermark text drawn bottom-right on any preset. Both remembered by the browser.
8. **Housekeeping.** Stale thumbnails are pruned after cleanup and after preview refinement.

## Non-goals

Server-side paging and the landing fold stay in `docs/BACKLOG.md` (triggers not met). Safari/VoiceOver and real-ARW timing need the user at the keyboard. No perceptual grouping across bursts. No watermark images, fonts, or placement options.

## Anti-goals

A model that rejects, picks, crops, or excludes. A forgotten word that silently comes back. A duplicate split that changes a manual pick, a decision, or a rating. Any source path, fingerprint, or raw model text in a browser response.

## Constraints

Pillow is the only runtime dependency. Loopback-only server, host/origin and strict JSON validation on every mutation route. Warning-strict unittest suite must stay green (`env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest discover -s tests`), `node --check src/lumen/static/app.js` must pass, and every browser behaviour change gets a Node scenario in `tests/test_web.py` following the existing harness.

## Calls made here

- Hash: 9x8 difference hash of the grayscale preview, 64 bits, stored as 16 hex chars in `metrics.dhash`. Threshold `Settings.near_duplicate_distance` (`LUMEN_NEAR_DUP_DISTANCE`, default 12 Hamming bits, 0 disables). Comparison is against the cluster's first frame so a slowly drifting sequence still splits.
- Forgetting a word adds it to a top-level `blocked` list in `vocabulary.json` (schema_version stays 1; the key is optional).
- Search expansion returns at most 8 vocabulary words; the result is written into the filter text as `term | term`; `GroupFilters.text` splits on `|`.
- Untracked recipe: `register <seq> -2pass`, `seqapplyreg <seq> -framing=min`, then the existing `stack r_<seq> …` line. Implementer verifies these commands against the vendored Siril 1.4.4 `siril-cli` help text before committing.
- Watermark: up to 80 printable characters, Pillow default font sized to 1/40 of the image height (minimum 14 px), white with a black stroke, 2 percent inset from the bottom-right corner.
- Resume state lives in `localStorage` key `lumen-selected`; filmstrip keys are `[` and `]` because arrows pan the loupe.
