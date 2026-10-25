# Frame Dialog, Typed Keywords, Place Editing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the photographer type keywords, correct places (burst-wide, remembered), and do every per-frame job including crops and a full-size sharpness check from one modal frame view opened on the focused frame.

**Architecture:** Server work is three small additions in the existing shape: a typed path inside the keywords route, a new `POST /api/photos/<id>/place` route backed by `Library.set_place` and a `source='manual'` tier in the GeoNames index, and a `GET /full/<id>` route serving the camera's embedded JPEG from `<library>/cache/full/`. The browser work grows the existing `#loupe` dialog into the frame view by rendering the inspector and the Compose workspace inside it, then removes the Compose tab and the inspector rail.

**Tech Stack:** Python 3 stdlib + Pillow, exiftool (optional, discovered), vanilla JS in `src/lumen/static/app.js`, Node scenario harness in `tests/test_web.py`, warning-strict `unittest`.

**Spec:** `docs/superpowers/specs/2026-10-25-frame-dialog-keywords-places-design.md`

## Global Constraints

- Loopback-only server; every mutation route keeps the same-origin check and strict JSON (`payload.keys() == {...}` and type checks, else `ValueError` → 400).
- No source paths, fingerprints, API keys, or raw model text in browser responses.
- Pillow is the only runtime Python dependency; exiftool stays optional via `shutil.which`.
- Never edit, move, rename, or delete camera originals; everything written goes under the library dir.
- No vendor or model names in tracked files or commit messages; no co-author trailers.
- Native `<dialog>` and native controls; keep the announcer, tab order, and charcoal/amber/cyan workbench.
- Run tests with `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest ...` and `node --check src/lumen/static/app.js`.
- Commit after each task on `main` (this repo works on `main`; the user merges and restarts `lumen serve`).

## Review Focus

Inputs the spec implies but no feature test exercises on its own; each is pinned to a task below.

1. A typed keyword that differs only by case from an applied one (`Lion` vs `lion`) must be refused as already present, not duplicated. → Task 1.
2. A retag after the photographer removed a model word must not resurrect it as *protected*; only `photographer` words are protected. → Task 2.
3. Place edit with `scope: "burst"` on a frame whose burst has frames without GPS must still set the place on all of them, and `remember` must use the edited frame's fix only. → Task 4.
4. `/full/<id>` for a photo whose original is on an unmounted volume must answer 404 without raising, and must never serve a file outside `cache/full`. → Task 6.
5. Stepping `]` quickly through three frames must leave the dialog showing the last frame's inspector and crops, never an earlier response. → Task 8.

---

### Task 1: Typed keyword server path

**Files:**
- Modify: `src/lumen/library.py` (near `add_suggested_keyword`, ~line 1625; `_move_keyword`, ~1668; `remove_keyword`, ~1665)
- Modify: `src/lumen/web.py:532-540` (keywords route)
- Test: `tests/test_library.py`, `tests/test_web.py`

**Interfaces:**
- Produces: `Library.add_keyword(photo_id: int, keyword: str) -> dict` returning `{"photo_id", "subjects", "suggested"}`; replaces `add_suggested_keyword` as the route target. Tags data gains `photographer: list[str]` and `typed: list[str]`.

- [ ] **Step 1: Write the failing library test** in `tests/test_library.py` next to `test_tag_job_explicit_ids_validate_and_keyword_add_remove_round_trip`:

```python
def test_typed_keyword_is_applied_learned_and_recorded_as_the_photographers(self):
    # untagged frame: add_keyword("Lion") -> subjects ["Lion"], suggested [], tags row model "photographer", description ""
    # data["photographer"] == ["Lion"] and data["typed"] == ["Lion"]; "Lion" in load_vocabulary(...)["learned"]
    # add_keyword("lion") raises ValueError (already applied, case-insensitive)
    # add_keyword("#bad"), add_keyword(""), add_keyword("x" * 41) raise ValueError
    # accepted suggestion (save_tags suggested ["krill"]; add_keyword("krill")) -> also lands in data["photographer"]
    # remove_keyword("Lion") -> subjects ["krill"], suggested [] (typed word is dropped, not suggested), photographer ["krill"]
    # remove_keyword("krill") -> suggested ["krill"] (an accepted suggestion goes back to suggested), photographer []
    # events end with: keyword_added {keyword: "Lion", typed: True}, vocabulary_learned, keyword_added {typed: False}, ..., keyword_removed
```

- [ ] **Step 2: Run it, expect FAIL** (`add_keyword` missing).

- [ ] **Step 3: Implement** in `library.py`:
  - `@_coordinated def add_keyword(self, photo_id, keyword)`: if `keyword in data.get("suggested", [])` → existing accept path (what `add_suggested_keyword` does today) plus append to `photographer`; else the typed path: `word = keyword.strip()`; raise `ValueError` unless `1 <= len(word) <= 40` and `"#" not in word`; raise `ValueError("keyword is already applied ...")` on a casefold match in `subjects`; raise `ValueError("unknown photo ID")` if no photo; build `data` (`{"description": "", "subjects": [], "suggested": []}` when no tags row, model `"photographer"`), append to `subjects` and `photographer`, `save_tags`, event `keyword_added {keyword, typed: True}`, `_write_sidecar`, `learn_keywords(self.settings, {"learned": [word]})` with the `vocabulary_learned` event. Keep `add_suggested_keyword` as a one-line alias for existing callers/tests.
  - Two lists in the tags data: `photographer` (every word the photographer typed or accepted; protected from retag in Task 2) and `typed` (words that were never a suggestion). `add_keyword` appends to `photographer` on both paths and to `typed` on the typed path.
  - `remove_keyword`: drop the word from both lists; if it was in `typed`, remove it from `subjects` without adding it to `suggested`; otherwise keep today's move to `suggested`.
  - Route: point `action == "add"` at `library.add_keyword`.

- [ ] **Step 4: Run the test, expect PASS.** Run the existing keyword tests too: `tests.test_library -k keyword`, `tests.test_web -k keyword`.

- [ ] **Step 5: Commit** `feat: typed keywords land on the frame and teach the vocabulary`.

### Task 2: Photographer keywords survive retag

**Files:**
- Modify: `src/lumen/library.py:1564-1615` (`tag_photo`)
- Test: `tests/test_library.py`

- [ ] **Step 1: Write the failing test** `test_retag_keeps_photographer_keywords_first_and_adds_model_words`: tag with a `FakeTagger` returning subjects `["ice"]`; `add_keyword("Lion")`; `remove_keyword("ice")`; retag with a tagger returning `["ice", "sea"]`; assert `subjects == ["Lion", "ice", "sea"]` (photographer first, deduped casefold), `photographer == ["Lion"]`, and that a photographer-typed `"Sea"` would have kept the photographer's casing (tag returns `"sea"`, subjects contain `"Sea"` once). Review Focus 2: the removed `"ice"` returns because it is a model word, not protected.

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement** in `tag_photo`: read `existing` before building `payload`; `kept = existing data.get("photographer", [])`; `applied = kept + [w for w in applied if w.casefold() not in {k.casefold() for k in kept}]`; carry `photographer` and `typed` through (`payload` already does `existing | {...}`; make sure `subjects` is the merged list).

- [ ] **Step 4: Run, expect PASS**; run `tests.test_library` fully.

- [ ] **Step 5: Commit** `feat: tagging adds to the photographer's keywords instead of replacing them`.

### Task 3: Typed keyword input in the inspector

**Files:**
- Modify: `src/lumen/static/app.js` (`keywordLists` ~769, `changeKeyword` ~806, `loadVocabulary` ~1337)
- Modify: `src/lumen/static/index.html` (add `<datalist id="vocabulary-words">` near `#vocabulary-panel`)
- Test: `tests/test_web.py` Node scenario `keyword-typed`

**Interfaces:**
- Consumes: Task 1 route behaviour (`add` with a non-suggested word).
- Produces: `addTypedKeyword(photo, word)` in `app.js`; `state.vocabularyWords: string[]` filled by `loadVocabulary()` and on `load()`.

- [ ] **Step 1: Write the failing Node scenario** `keyword-typed` (pattern: `run_javascript_cleanup_regression("keywords")`, scenario branches in the harness string inside `tests/test_web.py`): inspector renders `input#keyword-input[list="vocabulary-words"]` with label "Add a keyword" and `button#keyword-add`; typing `Lion` + Enter posts `{action: "add", keyword: "Lion"}`; applied list shows `Lion`; input is empty and focused; announcer says `Keyword Lion added to <name>`; typing `lion` again announces `Keyword lion is already on <name>` and posts nothing. Add `test_javascript_typed_keyword_posts_add_and_refuses_duplicates`.

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement**: `keywordLists` appends the labelled input + Add button after the applied list; `addTypedKeyword` reuses `changeKeyword(photo, 'add', word)` after the casefold duplicate check and sets `state.keywordFocus = 'input'` so focus returns to the input; `loadVocabulary` (and `load()`) fill `#vocabulary-words` options from `GET /api/vocabulary` categories.

- [ ] **Step 4: Run scenario + `node --check`, expect PASS.**

- [ ] **Step 5: Commit** `feat: type your own keyword in the inspector`.

### Task 4: Place editing server route and remembered places

**Files:**
- Modify: `src/lumen/geonames.py` (`_tier` ~75, `ensure` ~113, `lookup` ~173, `add` ~145)
- Modify: `src/lumen/library.py` (new `set_place` near `_resolve_place` ~174)
- Modify: `src/lumen/web.py` (new route after `/caption/edit` ~628; add to the regression lists in `test_new_mutations_reject_cross_origin_and_host_before_reading_body` and `..._nonobject_and_malformed_json`)
- Test: `tests/test_geonames.py`, `tests/test_library.py`, `tests/test_web.py`

**Interfaces:**
- Produces: `PlaceIndex.add(..., source="manual", feature_code="MANUAL")` rows that win `lookup` and survive `ensure()`; `Library.set_place(photo_id: int, name: str, country: str, *, scope: str, remember: bool) -> dict` returning `{"photo_ids": [...], "name", "country", "remembered": bool}`; route `POST /api/photos/<id>/place {name, country, scope, remember}`.

- [ ] **Step 1: Write the failing GeoNames test** `test_manual_place_beats_geonames_and_survives_rebuild` (mirror `test_online_place_is_found_and_survives_rebuild`): `add("Our camp", "South Africa", -24.74, 26.26, source="manual", feature_code="MANUAL")`; `lookup(-24.745, 26.257)` returns `("Our camp", "manual")` even though Madikwe Game Reserve is nearer tier 1; after touching `ZA.txt`, `ensure()` keeps it.

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement** in `geonames.py`: `_tier(name, feature_class, feature_code, source="geonames")` returns 0 for `source == "manual"`; pass `i[4]` in `lookup`'s key; `ensure()` preserves rows `WHERE source IN ('online', 'manual')`.

- [ ] **Step 4: Run, expect PASS.**

- [ ] **Step 5: Write the failing library test** `test_set_place_covers_burst_and_remembers_the_fix` using the GeoNames fixture pattern from `test_import_resolves_place_from_the_local_index_and_backfills_unchanged_files`: a burst of two frames, only the first with GPS (Review Focus 3); `set_place(first, "Our camp", "South Africa", scope="burst", remember=True)` → both photos' `place == "Our camp"`, both get `place_edited` events and rewritten sidecars (XMP contains `Our camp`), `remembered is True`, and `library.places.lookup(lat, lon).source == "manual"`; `set_place(second, ..., scope="frame", remember=True)` → `remembered is False` (no fix); bad `scope` raises `ValueError`.

- [ ] **Step 6: Run, expect FAIL.**

- [ ] **Step 7: Implement** `set_place` (`@_coordinated`): validate `scope in ("frame", "burst")`, `1 <= len(name.strip()) <= 80`, `len(country.strip()) <= 60`, photo exists; targets = `[photo]` or the burst's photos via `list_groups`; `catalog.update_place` + `append_event("place_edited", {name, country, scope, remember})` + `_write_sidecar` per target; remember only when `self.places is not None` and the edited frame has both coordinates. Country stored as `None` when blank.

- [ ] **Step 8: Run, expect PASS.**

- [ ] **Step 9: Write the failing web test** `test_place_route_validates_and_sets_place`: 400 for each of `{}`, missing `remember`, `scope: "all"`, `name: ""`, `name` of 81 chars, `remember: "yes"`; 200 round trip then `GET /api/photos/<id>` shows the place; add `("/api/photos/1/place", {...})` to both regression lists.

- [ ] **Step 10: Run, expect FAIL; implement the route; run, expect PASS.**

- [ ] **Step 11: Commit** `feat: edit a frame or burst place and remember it for nearby frames`.

### Task 5: Place editor in the inspector

**Files:**
- Modify: `src/lumen/static/app.js` (`locationLine` ~580)
- Test: `tests/test_web.py` Node scenario `place-edit`

**Interfaces:**
- Consumes: Task 4 route.
- Produces: `placeEditor(photo, record)` returning the elements `locationLine` appends after the Apple Maps link.

- [ ] **Step 1: Write the failing scenario** `place-edit`: for a frame with coordinates, `button#edit-place` "Edit place" is present; clicking reveals `form#place-form` with `input#place-name` (prefilled), `input#place-country`, `input#place-burst[checked]`, `input#place-remember[checked]`, Save, Cancel; Save posts `{name, country, scope: "burst", remember: true}` and announces `Place set to Our camp on 2 frames, remembered`; for a frame without coordinates `#place-remember` is disabled with `aria-describedby` text "no GPS fix". Add `test_javascript_place_editor_posts_burst_scope_and_disables_remember_without_a_fix`.

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement** `placeEditor`; on success update `state.details.photo.place/place_country` and the matching `state.groups` photos, re-render the inspector, and refocus `#edit-place`. The "Edit place" button also appears for frames without coordinates (the spec allows setting a place by hand) with remember disabled.

- [ ] **Step 4: Run scenario + `node --check`, expect PASS.**

- [ ] **Step 5: Commit** `feat: inline place editor with burst scope and remember`.

### Task 6: Full-size image route

**Files:**
- Modify: `src/lumen/previews.py` (new `full_size_for`, generalise `prune_thumbnails`)
- Modify: `src/lumen/library.py:1248` (`_prune_thumbnails` also prunes `cache/full`)
- Modify: `src/lumen/web.py:329` (add `full` to the `(preview|thumb)` route)
- Test: `tests/test_previews.py`, `tests/test_web.py`

**Interfaces:**
- Produces: `full_size_for(source: Path, cache_dir: Path, fingerprint: str) -> Path | None` (None when no embedded JPEG or no exiftool; raises `OSError` when the source is unreadable); `GET /full/<id>?v=` → `image/jpeg` or 404; `prune_cache(cache_dir, live_names)` used for both thumbs and full.

- [ ] **Step 1: Write the failing previews test** `test_full_size_for_extracts_jpg_from_raw_first_and_caches_by_fingerprint`: patch `lumen.previews.shutil.which` to return `"exiftool"` and `lumen.previews.subprocess.run` to return a `CompletedProcess` whose stdout is a real JPEG for `-JpgFromRaw` and empty for other tags; assert the call order asks `-JpgFromRaw` before `-PreviewImage`, the result path is `cache_dir / f"{fingerprint}.jpg"`, the saved image keeps its dimensions, and a second call does not run exiftool. A `which` returning `None` gives `None`. Also `prune` removes a stale `cache/full/*.jpg` whose fingerprint no photo carries.

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement** `full_size_for`: order `("JpgFromRaw", "PreviewImage")`, `exif_transpose`, save JPEG quality 92 with no `thumbnail` call, temp-then-replace like `_normalize`. Refactor `prune_thumbnails` into `prune_cache(cache_dir, live: set[str]) -> int` and keep `prune_thumbnails` as the thin caller; `_prune_thumbnails` adds a second call for `cache_dir / "full"` with `{f"{photo.fingerprint}.jpg"}`.

- [ ] **Step 4: Run, expect PASS.**

- [ ] **Step 5: Write the failing web test** `test_full_route_serves_cached_embedded_jpeg_or_404`: with the same patches, `GET /full/1` is 200 `image/jpeg`; with `which` → `None` it is 404; with the photo's `path` pointing at a missing file it is 404 (Review Focus 4); `GET /full/999` is 404; the response never includes a path.

- [ ] **Step 6: Run, expect FAIL; implement the route branch** (resolve the cached path, `relative_to(settings.cache_dir / "full")` guard like the preview branch); **run, expect PASS.**

- [ ] **Step 7: Commit** `feat: serve the camera's full-size embedded JPEG for the loupe`.

### Task 7: Loupe becomes the frame view

**Files:**
- Modify: `src/lumen/static/index.html` (`#loupe` markup ~453; move `#compose-workspace` and `#compose-status` into it; remove `#compose-panel` and `#compose-tab`; remove `aside.inspector`; move `#back-to-top` beside the jump controls; add `<div id="inspection">` inside the dialog's right column; add `button#loupe-full` "Full size" with `aria-pressed="false"`; add `button#loupe-export` "Export this frame")
- Modify: `src/lumen/static/styles.css` (`#loupe` two-column grid; `.inspector` rules move to `#loupe .frame-panel`; `.workspace` loses the 300px column)
- Modify: `src/lumen/static/app.js` (`openLoupe` ~2131, `stepLoupe` ~2143, `handleCullingKey` ~929 guard, `selectTab` ~1863 and the remembered-tab fallback ~1893, `inspect` ~494, `loadComposition` ~148, `CULLING_KEYS` add `z: 'full'`)
- Test: `tests/test_web.py` (update `test_frames_tab_row_and_caption_inputs_fit_the_sticky_layout` to use `export-tab`; update `test_layout_is_full_height_...` for the removed rail; new scenarios `frame-view`, `frame-view-full`)

**Interfaces:**
- Consumes: Task 6 `/full/<id>` route, Tasks 3 and 5 inspector blocks (they render wherever `#inspection` lives).
- Produces: `openLoupe(photoId)` selects, inspects, loads crops; `toggleFullSize()`; `exportCurrentFrame()`.

- [ ] **Step 1: Write the failing scenarios.**
  - `frame-view`: Enter on a focused frame opens `#loupe`, `state.selectedId` becomes that frame, `GET /api/photos/<id>` and `GET /api/photos/<id>/crops` are requested once each, `#inspection` inside the dialog has the frame's heading, `#compose-workspace` is inside `#loupe` and visible, `]` steps to the next frame and requests its details and crops once each with the announcement `<name>, 2 of N`, pressing `x` from `#loupe-export` (a dialog button) rejects the frame, Escape closes and refocuses the origin frame. The document has no `#compose-tab` and no `.inspector`; a remembered `lumen-tab` of `compose-tab` selects `export-tab`.
  - `frame-view-full`: `#loupe-full` toggles `aria-pressed`, swaps the viewport `img.src` to `/full/<id>?v=...`, and on image load its text becomes `Full size, 3504 by 2336`; a 404 disables the button with the text "no full-size image in this file".
  Add `test_javascript_frame_view_gathers_inspector_and_crops` and `test_javascript_frame_view_full_size_toggle`.

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement.** `openLoupe`/`stepLoupe` call a shared `showFrame(photo)` that sets `state.selectedId`/`selectedGroupId` (as a sheet click does), runs `inspect(photo)` and `loadComposition()`, and resets the full-size toggle to the preview. `inspect` and `focusFrame` stop referencing `#compose-tab`; `loadComposition` runs only from `showFrame` and after a crop decision while the loupe is open. In `handleCullingKey`, lift the non-`.frame` button guard when `openView?.id === 'loupe'`. `toggleFullSize` sets `img.src` to `/full/${id}?v=${version}`, centres scroll on load, and on `error` disables the button with the reason text. `exportCurrentFrame` posts `scope: 'current'` with the Export tab's preset and watermark and writes to `#export-status`, mirrored into `#compose-status`. Update the Shortcuts legend: Enter "Open the frame view", Esc "Close compare or the frame view", add `Z` "Full size on or off in the frame view".

- [ ] **Step 4: Run both scenarios, the updated layout tests, `node --check`, and the full `tests.test_web`; expect PASS.**

- [ ] **Step 5: Commit** `feat: frame view gathers inspector, crops, export, and full-size zoom in the loupe`.

### Task 8: Stale-response guard when stepping fast

**Files:**
- Modify: `src/lumen/static/app.js` (`showFrame` from Task 7)
- Test: `tests/test_web.py` scenario `frame-view-race`

- [ ] **Step 1: Write the failing scenario** `frame-view-race`: the harness delays the `/api/photos/1` and `/api/photos/1/crops` responses until after `]` `]` has selected frame 3 and its responses arrived; the dialog heading, `#inspection` heading, and crop cards all belong to frame 3 (Review Focus 5). Add `test_javascript_frame_view_ignores_stale_frame_responses`.

- [ ] **Step 2: Run, expect FAIL if the guard is missing** (the existing `state.selectedId !== photo.id` checks in `inspect` and the `compositionRequest` id in `loadComposition` may already pass; if it passes immediately, keep the test as the pin and skip Step 3).

- [ ] **Step 3: Implement** any missing guard in `showFrame`.

- [ ] **Step 4: Run, expect PASS.**

- [ ] **Step 5: Commit** `test: frame view ignores stale responses while stepping`.

### Task 9: Docs and handoff

**Files:**
- Modify: `docs/HANDOFF.md` (milestone entry: routes, keys, cache dir, restart note), `docs/BACKLOG.md` (remove "Crop-load latency" and "Hand-editing places"; add "L-size ARW embedded JPEG size unverified" with the `exiftool -b -JpgFromRaw` check as the trigger), `AGENTS.md` "Current tool state" (one line: `cache/full/` holds embedded full-size JPEGs by fingerprint; manual place rows win lookup), `README.md` if it lists the inspector rail or Compose tab.

- [ ] **Step 1: Update the four documents.**
- [ ] **Step 2: Run the whole suite** `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest discover -s tests` and `node --check src/lumen/static/app.js`; expect all pass.
- [ ] **Step 3: Commit** `docs: frame view, typed keywords, place editing in handoff and backlog`.
