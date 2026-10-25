# Frame View Round Two Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the photographer draw their own crop in the frame view, split the frame view's right column into Frame and Crop tabs, and fold the contact sheet's filter form into one row plus a collapsed section.

**Architecture:** The server grows one accepted shape on the existing crop route (`candidate_id: "crop-manual"` plus a `rect`) and one `Library.choose_crop` branch that validates against `MIN_CROP_PIXELS`; everything downstream (`PhotoEdit`, sidecars, export) is unchanged. The browser adds a pointer-drawn overlay on the loupe viewport that yields a normalised rect, a "Your crop" card built from it, a two-tab right column reusing the tab-row key handling, and a `<details>` wrapper in the filter form.

**Tech Stack:** Python stdlib + Pillow, vanilla JS, Node scenario harness in `tests/test_web.py`, warning-strict unittest.

**Spec:** `docs/superpowers/specs/2026-10-25-frame-view-crop-tabs-filters-design.md`

## Global Constraints

- Loopback-only; mutation routes keep same-origin and strict JSON (exact key sets, type checks, `ValueError` → 400); no paths in responses.
- Pillow only. No vendor/model names in tracked files or commit messages; no co-author trailers.
- Native controls, accessible names, announcements via `announce()`; culling keys, `[`/`]`, `z`, Escape keep their meaning.
- Tests: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest ...`, `node --check src/lumen/static/app.js`; red-green per task; commit per task on the feature branch.

## Review Focus

1. A rect that is inside bounds but keeps fewer than 8 MP must be refused with a public message and leave any active crop untouched. → Task 1.
2. A manual crop drawn at full size and the same crop drawn at preview size must produce the same normalised rect (within 1/1000). → Task 2.
3. Starting a drag, pressing Escape, and releasing must leave no rect, no card, and the dialog open. → Task 2.
4. Stepping `]` while a drawn but unsaved rect exists must clear the overlay and card; reopening a frame with a saved manual crop must show its outline and card. → Task 3.
5. Reset must clear every control inside the collapsed section and set the summary back to `More filters`. → Task 5.

---

### Task 1: Manual crop on the server

**Files:**
- Modify: `src/lumen/library.py` (`choose_crop` ~872; `review_composition` ~896-940 candidate id check)
- Modify: `src/lumen/web.py:560-570` (`/api/photos/<id>/crop`), `~296-310` (`/crops` payload gains `source_width`, `source_height`)
- Test: `tests/test_library.py`, `tests/test_web.py`

**Interfaces:**
- Produces: `Library.choose_crop(photo_id: int, candidate_id: str, *, rect: dict | None = None, aspect: str = "free") -> PhotoEdit`; `MANUAL_CROP_ID = "crop-manual"`; `MANUAL_ASPECTS = ("free", "3:2", "2:3", "1:1", "4:5")`; route body `{candidate_id: "crop-manual", rect: {left, top, width, height}, aspect}`; `GET /crops` carries `source_width`/`source_height`.

- [ ] **Step 1: Failing library test** `test_manual_crop_is_stored_with_the_photographer_as_author_and_refuses_tiny_rects`: import a 6000x4000 JPEG (make it with Pillow, it may be mostly black); `choose_crop(id, "crop-manual", rect={"left": .1, "top": .1, "width": .8, "height": .8}, aspect="free")` → `PhotoEdit` with `candidate_id "crop-manual"`, `author "photographer"`, `rationale "drawn by the photographer"`, `aspect "free"`, and `catalog.get_crop(id).rect == CropRect(.1, .1, .8, .8)`; then `rect` of `.3 x .3` (3.6 MP) raises `ValueError` matching `at least 8 MP` and the stored crop is still the first (Review Focus 1); `aspect="7:3"` raises `ValueError`; `rect` with `left .5, width .6` raises (CropRect bounds); `clear_crop` removes it; `review_composition` with a fake tagger still works with the manual crop active (its candidate list excludes `crop-manual`).
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** the `crop-manual` branch in `choose_crop` (compute `pixel_count = round(width*rect.width) * round(height*rect.height)`, compare to `composition.MIN_CROP_PIXELS`), and in `review_composition` treat `active.candidate_id == MANUAL_CROP_ID` as "no generated candidate active" wherever it compares ids.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Failing web test** `test_manual_crop_route_accepts_rect_and_rejects_bad_shapes`: 200 for the good body; 400 for `rect` missing a key, a string number, `aspect` outside the list, extra keys, and `candidate_id "crop-manual"` without `rect`; the existing `{candidate_id}` shape still works; `GET /crops` returns `source_width`/`source_height` ints and, after the manual crop, `active_crop.candidate_id == "crop-manual"`.
- [ ] **Step 6: Run, expect FAIL; implement the route branch; run, expect PASS.**
- [ ] **Step 7: Commit** `feat: photographer-drawn crop rectangles save through the crop route`.

### Task 2: Draw a crop on the frame-view image

**Files:**
- Modify: `src/lumen/static/app.js` (`showPreview` ~2185, `showFrame`, `toggleFullSize`, `panKey` region), `src/lumen/static/index.html` (`#loupe` viewport gains an overlay element and `select#crop-lock`), `src/lumen/static/styles.css`
- Test: `tests/test_web.py` scenario `crop-draw`

**Interfaces:**
- Consumes: nothing from Task 1 yet (drawing is client-side).
- Produces: `state.manualCrop: {rect: {left, top, width, height}, aspect: string} | null`; `drawCropOverlay(rect | null)`; `cropLockValue()`; a custom DOM event `lumen:manual-crop` is not needed, Task 3 calls `renderComposition()` directly.

- [ ] **Step 1: Failing scenario** `crop-draw`: open the frame view on a 2048x1365 preview; dispatch `pointerdown` at image coords (200,100), `pointermove` to (1200,800), `pointerup` → `state.manualCrop.rect` ≈ `{left: 200/2048, top: 100/1365, width: 1000/2048, height: 700/1365}` and `#crop-overlay` is visible with matching style; toggle full size (image 4096x2730 in the harness) and draw (400,200)→(2400,1600) → the same rect within 0.001 (Review Focus 2); a drag of 10 px does nothing; `pointerdown`, `Escape`, `pointerup` → `state.manualCrop === null`, `openView` still `#loupe` (Review Focus 3); with `#crop-lock` set to `1:1`, drawing (0,0)→(900,300) yields `width*2048 === height*1365` within a pixel; `localStorage` has `lumen-crop-lock = "1:1"`. The harness needs pointer events with `offsetX/offsetY` (or `clientX` plus a fake `getBoundingClientRect`), `naturalWidth/naturalHeight` on the loaded `img`, and `Escape` keydown delivery to the viewport; extend it minimally.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement**: overlay `div#crop-overlay` absolutely positioned inside `#loupe-viewport` over the `img` (the viewport becomes `position: relative`; the overlay sizes to the image's rendered box so it scrolls with it); pointer handlers on the viewport convert to natural-pixel coords via `img.naturalWidth / img.clientWidth`; Escape during a drag calls `cancelDrag()` and `stopPropagation()` so the dialog's handler never sees it; the lock select constrains height from width; `showFrame` clears `state.manualCrop` and the overlay. Announce `Crop drawn: W by H pixels` on pointer-up.
- [ ] **Step 4: Run scenario + `node --check`, expect PASS.**
- [ ] **Step 5: Commit** `feat: draw a crop rectangle on the frame-view image`.

### Task 3: "Your crop" card, save, and restore

**Files:**
- Modify: `src/lumen/static/app.js` (`renderComposition` ~134, `cropCard` ~81, `applyCrop` ~175, `cropLabel` ~72, `showFrame`)
- Test: `tests/test_web.py` scenario `crop-manual-card`

**Interfaces:**
- Consumes: Task 1 route and `source_width`/`source_height`; Task 2 `state.manualCrop`, `drawCropOverlay`.
- Produces: a candidate object `{id: "crop-manual", kind: "manual", aspect, rect, pixel_box, pixel_count, remaining_megapixels, rationale: "drawn by the photographer", resolution_warning}` built client-side by `manualCandidate(rect, aspect, composition)`.

- [ ] **Step 1: Failing scenario** `crop-manual-card`: after a drag (reuse the `crop-draw` steps), `#primary-crops` first card heading is `Your crop`, its details line shows the MP from `source_width*source_height*rect.width*rect.height`, a sub-8 MP rect shows the resolution warning text the server would (`... MP remaining; below 12 MP` style from `cropCard`) and its "Use this crop" is disabled; clicking "Use this crop" posts `{candidate_id: "crop-manual", rect, aspect}`, announces `Your crop selected.`, and the card gains `Current choice`; `]` then `[` back → `state.manualCrop === null` and the card is gone until the crops payload with `active_crop.candidate_id === "crop-manual"` arrives, after which the card returns with `Current choice` and the overlay outline is drawn from `active_crop.rect` (Review Focus 4).
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement**: `renderComposition` prepends `manualCandidate` when `state.manualCrop` exists or `active_crop.candidate_id === "crop-manual"`; `cropLabel('manual') === 'Your crop'`; `applyCrop` sends `rect` and `aspect` for kind `manual`; below 8 MP the button is disabled with `aria-describedby` on the warning.
- [ ] **Step 4: Run scenario + `node --check` + the existing `frame_view`/`crop` tests, expect PASS.**
- [ ] **Step 5: Commit** `feat: your crop card saves and restores a drawn rectangle`.

### Task 4: Frame and Crop tabs in the right column

**Files:**
- Modify: `src/lumen/static/index.html` (`#loupe .frame-panel`), `src/lumen/static/app.js` (`renderInspector` drops the `h3`; tab handling ~1997-2030 generalised; `showFrame` restores `lumen-frame-tab`), `src/lumen/static/styles.css`
- Test: `tests/test_web.py` scenario `frame-tabs`; update `frame-view` if it asserts the `h3`

- [ ] **Step 1: Failing scenario** `frame-tabs`: `#loupe` has `[role=tablist]` with `#frame-tab` (selected by default, controls `#frame-panel` holding `#inspection`) and `#crop-tab` (controls `#crop-panel` holding `#crop-lock`, `#primary-crops`, `#ask-composition-ai`, `#loupe-export`); clicking Crop hides Frame's panel, sets `aria-selected`, stores `lumen-frame-tab = "crop-tab"`; ArrowLeft on the Crop tab focuses and selects Frame; Home/End work; reopening with the stored value opens on Crop; `#inspection` has no `h3` with the frame name; pressing `x` while the Crop panel has focus still rejects.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement**: a `tablist(container, storageKey)` helper that wires selection and arrow keys for a given `[role=tablist]`, used by both the main row and the frame view (replace the module-level `tabs`/`selectTab` wiring with a call; keep `selectTab(tab)` as the main row's exported name so existing callers work); `showFrame` selects the remembered frame tab.
- [ ] **Step 4: Run scenario + `node --check` + `-k tab`, expect PASS.**
- [ ] **Step 5: Commit** `feat: frame view splits into Frame and Crop tabs`.

### Task 5: Condensed filter form

**Files:**
- Modify: `src/lumen/static/index.html:266-294`, `src/lumen/static/app.js` (`filterQuery` ~1211, `syncFilters`, reset handler), `src/lumen/static/styles.css` (`.filter-form`)
- Test: `tests/test_web.py` scenario `filter-more`; update any HTML structure test that walks the form

- [ ] **Step 1: Failing scenario** `filter-more`: `#filter-form` first row contains `#filter-text`, `#expand-search`, the Apply submit, `#reset-filters`, and `details#filter-more > summary` reading `More filters`; the details contains `#filter-from`, `#filter-to`, `#filter-camera`, `#filter-decision`, `#filter-rating`, `#filter-keepers`, `#filter-tagged`, `#show-rejected`; setting rating 3 and ticking keepers makes the summary read `More filters (2 active)`; Reset returns it to `More filters` and clears both (Review Focus 5); toggling the details stores `lumen-filter-more`; `filterQuery()` output is unchanged for the same values.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement**: markup regrouping; `updateFilterSummary()` counting controls whose value differs from `FILTER_DEFAULTS` (flags count when checked), called on `input`/`change` in the form, after `syncFilters`, and after reset; `details` `toggle` event persists the state.
- [ ] **Step 4: Run scenario + `node --check` + whole `tests.test_web`, expect PASS.**
- [ ] **Step 5: Commit** `feat: filter form folds into one row and a More filters section`.

### Task 6: Docs

**Files:**
- Modify: `docs/HANDOFF.md` (new top entry), `README.md` (frame view paragraph: drawing a crop, tabs; filter form sentence), `docs/BACKLOG.md` (nothing to remove; add nothing unless a task deferred something)

- [ ] **Step 1: Update docs.** Run the full suite and `node --check`.
- [ ] **Step 2: Commit** `docs: own crop, frame-view tabs, condensed filters`.
