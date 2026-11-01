# Map Tab Round 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clusters fan out into a ring of up to 12 thumbs with a "Show all in Review" button, and frames from earlier, cleared imports appear on the map from the trash snapshots.

**Architecture:** An `ids` group filter lets the browser narrow Review to a cluster's frames. `Library.map_cleared()` reads every trash snapshot read-only, dedupes by fingerprint and returns map rows keyed by an opaque digest; `/thumb/cleared/<key>` serves a thumb from the trashed preview. The canvas gains a fan-out state and a card for cleared frames.

**Tech Stack:** Python stdlib (`sqlite3` read-only URIs, `hashlib`), Pillow via the existing `thumbnail_for`, vanilla JS canvas.

**Spec:** `docs/superpowers/specs/2026-11-01-map-tab-design.md`, section "Round 2".

## Global Constraints

- Same as the round-1 plan: originals untouched; loopback; strict JSON; no paths or fingerprints in browser responses (the cleared `key` is a 16-hex sha256 prefix of the fingerprint, never the fingerprint); Pillow-only; warning-strict tests with `env -u LUMEN_OMLX_MODEL`; `node --check`; no AI vendor names or scratchpad paths in tracked files or commit messages; the guide is `AGENTS.md`.
- Trash snapshots are opened read-only (`file:...?mode=ro`, as `Catalog.restore_work` does) and never modified; an unreadable snapshot is skipped and counted.
- Branch `feature/map-tab`, no version bump (still unreleased 0.3.0); add to the CHANGELOG paragraph and `docs/HANDOFF.md`.

## Review Focus

- A trash snapshot whose `files/previews/` lacks the preview (older cleanups, or a `.jpg` source frame): the row still appears with `thumb_url: null` and the dot draws without a thumb. (Task 1 test `test_map_cleared_without_preview_has_no_thumb`.)
- A fingerprint cleared twice with different ratings: the newest snapshot's row wins. (Task 1 test `test_map_cleared_dedupes_by_fingerprint_newest_wins`.)
- A fingerprint present in both the trash and the live catalog (re-imported): only the live frame appears. (Task 1 test `test_map_cleared_skips_frames_back_in_the_catalog`.)
- `ids` with a non-integer, a duplicate or more than 200 entries: 400 "invalid filter: ids". (Task 1 test in `tests/test_web.py`.)
- A cluster larger than 12: the ring shows 12 and the button says "Show all 33 in Review". (Task 2, driven headlessly; markup test pins the button id.)

---

### Task 1: `ids` filter, cleared frames from the trash, cleared thumbs

**Files:**
- Modify: `src/lumen/catalog.py` (`GroupFilters`: `ids: tuple[int, ...] | None = None`, validated in `__post_init__`, `where()` adds `p.id IN (...)`), `src/lumen/web.py` (`parse_group_filters` parses `ids=1,2,3`; `GET /api/map/photos` reads and strips `include_cleared` before `parse_group_filters`; new `GET /thumb/cleared/<key>` beside `/thumb/<id>`), `src/lumen/library.py` (`# Map` section)
- Test: `tests/test_catalog.py` or `tests/test_library.py`, `tests/test_web.py`

**Interfaces:**
- Produces: `GroupFilters.ids` (tuple of distinct positive ints, at most 200; `ValueError("invalid filter: ids")`); query form `ids=3,5,8`.
- `Library.map_cleared() -> list[dict]`: `{"key", "latitude", "longitude", "captured_at", "place", "decision", "rating", "thumb_url": "/thumb/cleared/<key>" | None}` over every `trash/*/catalog.sqlite3` (newest first, `sorted(..., reverse=True)` like `_restore_cleared`), rows with a fix only, dedupe by fingerprint, skip fingerprints in the live catalog; `key = hashlib.sha256(fingerprint.encode()).hexdigest()[:16]`; `thumb_url` only when `trash/<stamp>/files/previews/<fingerprint>.jpg` exists in the winning snapshot. Unreadable snapshots skipped.
- `Library.cleared_thumb(key: str) -> Path | None`: resolves the key back to the winning snapshot's preview (rebuild the list; cache it on the instance) and returns `thumbnail_for(preview, settings.cache_dir / "thumbs")`.
- Route: `GET /api/map/photos?include_cleared=1&<filters>` adds `"cleared": [...]` (empty list without the flag); `GET /thumb/cleared/<16 hex>` serves the JPEG with the same headers as `/thumb/<id>`, 404 when unknown.

- [ ] Tests red (the five named above plus `test_ids_filter_narrows_groups` and the 400 case), implement, green, full suite, commit `feat: ids filter, cleared frames and their thumbs for the map`.

### Task 2: Cluster fan-out, "Everything ever imported", cleared card, docs

**Files:**
- Modify: `src/lumen/static/index.html` (third radio `value="ever"` "Everything ever imported"; `<button type="button" id="map-show-all" hidden>` inside the map stage; `<div id="map-card" role="dialog" aria-modal="false" hidden>` with an image, three `<p>` and a Close button), `src/lumen/static/app.js` (`// Map tab`), `src/lumen/static/styles.css`, `CHANGELOG.md`, `docs/HANDOFF.md`, `AGENTS.md` map bullet
- Test: `tests/test_web.py` markup assertions (`#map-show-all`, `#map-card`, the `ever` radio)

**Behaviour:**
- Scope `ever` = whole catalog plus `cleared` from `include_cleared=1`; cleared frames are hollow cyan circles (2 px stroke, no fill), clustered together with live frames; a cluster's count includes them.
- Click on a cluster: `state.map.fan = {at: {lat, lon}, members: [...]}`; members sorted by `decision === 'flag'`, then rating desc, then `captured_at`; the first 12 drawn as 48 px thumbs (or dots until loaded) evenly spaced on a ring of radius `max(56, 12 * members.length / 2)` px around the cluster point, each with a hit area; `#map-show-all` positioned under the ring with text `Show all <n> in Review` (hidden when `n <= 12`? No: always shown, it is the only way to see them all when `n > 12`, and harmless below). Button click: `applyFilters(new URLSearchParams({ids: liveIds.join(',')}).toString())` then `selectTab(document.querySelector('#review-tab'))`; cleared members cannot be shown in Review, and the button text says `Show all <live> in Review` with `(<cleared> cleared)` appended when any are cleared. Pan, zoom, scope change or a click on empty map folds the fan. Escape folds it too.
- Lone-frame thumbs from zoom 9 (constant rename from 11).
- Click on a cleared frame (dot or ring thumb): `#map-card` appears beside the point (clamped inside the stage) with the thumb (`thumb_url`, or a placeholder box), `captured_at` formatted like the overlay span, `place`, `decision` and `rating` as "flagged · ★★★" style text, and the hint "Import its folder again with Include cleared to work on it". Close button and Escape dismiss; focus moves to the card on open and back to the canvas on close. The frames-in-view list includes cleared frames as buttons labelled with "(cleared)" that open the card.
- Keyboard: on the canvas, Enter on the selected cluster fans it out; arrows still pan.
- Docs: CHANGELOG paragraph gains two sentences; HANDOFF milestone gains a "Round 2" bullet; AGENTS.md map bullet gains `ids` filter, `include_cleared`, `/thumb/cleared/<key>`.

- [ ] Markup test red, markup, styles, JS, `node --check`, tests, full suite, headless drive (fan-out on a 3-frame cluster at one coordinate, Show all narrows Review, cleared card opens and closes), commit `feat: map clusters fan out into thumbs, earlier imports appear from the trash` (screenshot paths go in the report only).
