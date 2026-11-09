# Regroup by key and "Belongs with…" Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mark bursts for merge by key (`G`, `Shift`+`G`), and for a focused frame suggest the four nearby bursts that look most like it, moving it into one with a single action (`B`, "Move here").

**Architecture:** The existing Vision helper also returns an image feature print and learns a batch mode; prints are stored per photo in a new catalog table. `Library.belongs_with` ranks bursts by feature-print distance within ten minutes, computing missing prints in one batch; `Library.move_photo` combines the split and merge mechanisms so the move survives regrouping and pins the old burst. The browser renders suggestions from its own `state.groups` given an ordered list of group IDs.

**Tech Stack:** Python 3 stdlib + Pillow, SQLite, Swift (Vision), vanilla JS, `unittest` with the node-driven JS harness in `tests/test_web.py`.

**Spec:** `docs/superpowers/specs/2026-11-09-regroup-by-key-and-belongs-with-design.md`

## Global Constraints

- Pillow is the only runtime Python dependency; distances in plain Python (`array('f')`, `math`).
- Never touch originals; the feature print is derived data inside the library.
- Mutation routes keep loopback, host/origin and strict JSON validation; responses carry no source paths, fingerprints or feature prints.
- Candidate window: **10 minutes** (`BELONGS_WITH_WINDOW_SECONDS = 600`); at most **4** candidates (`BELONGS_WITH_LIMIT = 4`); same camera only; never the frame's own burst.
- Vision helper JSON `schema_version` becomes **3**; parsers accept 1–3.
- Keys: `G` mark/unmark, `Shift`+`G` mark range, `B` toggle the panel; ignored in text fields and open dialogs; culling keys stay ignored on panel buttons.
- Marked bursts get an outline in `--cyan`; the checkbox stays.
- New provenance event `frame_moved` with details `{group_id, from_group_id}`.
- Bump `__version__` in `src/lumen/__init__.py` and `pyproject.toml` to `0.5.0`, add a `CHANGELOG.md` entry, update `AGENTS.md` (Current tool state) and `docs/HANDOFF.md`. No vendor or model names in tracked files or commit messages; no co-author trailers.
- Tests: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest discover -s tests -v` and `node --check src/lumen/static/app.js`. No test runs the real Swift helper; stub it with an executable script as `tests/test_analysis.py` does.

## Review Focus

1. **A helper compiled before this change** (schema 2, no batch mode) is still in `<library>/cache/bin/` until `scripts/bootstrap.sh` reruns. `belongs_with` must refuse with a message naming `scripts/bootstrap.sh`, not return an empty list. Test in Task 3.
2. **A frame with no capture time** has no time window: return no candidates, no exception. Test in Task 3.
3. **Moving a burst's anchor frame** (the frame other frames' `merged_into` points at) must leave the rest together. Test in Task 4.
4. **Prints of different lengths** (a macOS Vision revision change between imports) are skipped, not compared or raised on. Test in Task 3.
5. **`Shift`+`G` with nothing marked yet** acts as `G`; after the anchor is unmarked, the anchor becomes the most recently marked burst still marked. Test in Task 6.

---

### Task 1: Vision helper returns a feature print and has a batch mode

**Files:**
- Modify: `tools/vision_analyzer.swift`
- Modify: `src/lumen/analysis.py` (`parse_vision_result`, `parse_vision_regions`, `analyze_faces`; new `parse_feature_print`, `feature_prints`)
- Test: `tests/test_analysis.py`

**Interfaces:**
- Produces: `analysis.parse_feature_print(raw: str) -> bytes | None` — float32 bytes (`array('f', values).tobytes()`), `None` when the key is absent (schema 1/2) or invalid (empty, non-finite, non-numeric).
- Produces: `analyze_faces(...)` result dict gains `"feature_print": bytes | None`.
- Produces: `analysis.feature_prints(paths: list[Path], executable: Path) -> dict[Path, bytes] | None` — one helper process for all paths; `None` when the executable is missing, exits non-zero, or prints a line count different from `len(paths)` (an old helper); a path whose line has no valid print is left out of the dict.

Swift output: every result object gains `"feature_print": [Float]` from `VNGenerateImageFeaturePrintRequest` (element type float; omit the key if Vision gives none) and `"schema_version": 3`. One argument: print one JSON object exactly as today. Two or more arguments: print one JSON object per line in argument order; an image that fails prints `{"error": "<reason>"}` on its line and the process continues. Zero arguments: usage, exit 2.

- [ ] **Step 1: Write the failing tests** in `tests/test_analysis.py`:
  - `test_parse_vision_result_accepts_schema_3_and_regions_accept_it_too`: a schema 3 payload with `feature_print: [0.5, -0.25]` parses in both `parse_vision_result` and `parse_vision_regions`; schema 4 raises `ValueError`.
  - `test_parse_feature_print_round_trips_float32_and_rejects_bad_vectors`: `[0.5, -0.25]` → `array('f', [0.5, -0.25]).tobytes()`; absent key → `None`; `[]`, `["x"]`, `[true]` → `None`. (A non-finite float cannot appear in JSON, so no test for it.)
  - `test_analyze_faces_carries_the_feature_print`: a stub executable printing a schema 3 object yields `result["feature_print"] == array('f', [...]).tobytes()`.
  - `test_feature_prints_runs_one_process_for_every_path_and_skips_failures`: a stub script that appends a line to a counter file per run and prints three lines (valid, `{"error": "x"}`, valid) for three paths → dict has paths 1 and 3; counter shows one run.
  - `test_feature_prints_is_none_for_a_helper_without_batch_mode`: a stub that exits 2 when given more than one argument → `None`; a missing executable → `None`.
- [ ] **Step 2: Run** `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_analysis -v` — expected: the five new tests FAIL.
- [ ] **Step 3: Implement** the Swift change and the Python functions above. `feature_prints` passes all paths as arguments in one `subprocess.run` (timeout 120 s, like `analyze_faces`) and catches the same errors. Compile check: `swiftc -O tools/vision_analyzer.swift -o /tmp/va && /tmp/va <some preview>.jpg` prints `schema_version: 3` with 768 floats; with two previews it prints two lines.
- [ ] **Step 4: Run** the module's tests — expected: PASS.
- [ ] **Step 5: Commit** `feat: Vision helper returns a feature print and accepts several images`

### Task 2: Store feature prints in the catalog at import and refine

**Files:**
- Modify: `src/lumen/catalog.py` (schema: new table; two methods)
- Modify: `src/lumen/library.py` (the three `analyze_faces` call sites: import ~line 2623, refine ~1049, ~1395)
- Test: `tests/test_catalog.py`, `tests/test_library.py`

**Interfaces:**
- Consumes: `analyze_faces(...)["feature_print"]` (Task 1).
- Produces: `Catalog.save_feature_print(photo_id: int, data: bytes) -> None` (upsert), `Catalog.feature_prints_for(photo_ids: Iterable[int]) -> dict[int, bytes]` (missing IDs absent).

Table: `CREATE TABLE IF NOT EXISTS feature_prints (photo_id INTEGER PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE, vector BLOB NOT NULL)`. A separate table, not a `metrics` column, so `save_metrics`/`PhotoMetrics` stay as they are.

- [ ] **Step 1: Write the failing tests:**
  - `test_catalog.py::test_feature_prints_upsert_read_back_and_cascade_with_the_photo`: save, overwrite, read back by IDs (unknown ID absent), delete the photo → gone.
  - `test_library.py::test_import_stores_the_feature_print_from_the_vision_helper`: patch `lumen.library.analyze_faces` to return `{"face_count": 0, "face_quality": None, "eye_quality": None, "feature_print": b"\x00\x00\x80?"}` → after import `catalog.feature_prints_for([id]) == {id: b"\x00\x00\x80?"}`; with `feature_print: None` nothing is stored.
- [ ] **Step 2: Run** `tests.test_catalog` and `tests.test_library` — the new tests FAIL.
- [ ] **Step 3: Implement.** At each `analyze_faces` call site, after `save_metrics`, save the print when it is not `None`.
- [ ] **Step 4: Run** both modules — PASS.
- [ ] **Step 5: Commit** `feat: keep each frame's feature print in the catalog`

### Task 3: `Library.belongs_with` ranks nearby bursts

**Files:**
- Modify: `src/lumen/library.py` (constants near `NEARBY_PLACE_KM`; new method)
- Test: `tests/test_library.py`

**Interfaces:**
- Consumes: `Catalog.feature_prints_for`, `Catalog.save_feature_print` (Task 2); `analysis.feature_prints` (Task 1).
- Produces: `Library.belongs_with(photo_id: int, filters: GroupFilters | None = None) -> dict` returning `{"photo_id": int, "group_ids": list[int]}`, nearest first, at most `BELONGS_WITH_LIMIT`. Raises `ValueError("unknown photo ID: <id>")` for a bad ID and `ValueError("Belongs with needs the Vision helper's feature prints; run scripts/bootstrap.sh")` when any print it needs is missing and `feature_prints` returns `None`.

Algorithm:
```
listed = groups from self.list_groups(replace(filters or GroupFilters(), show_rejected=True))
photo = focused frame; if photo.captured_at is None: return empty
frames = photos in listed groups, same camera_id, group != photo's group,
         |captured_at - photo.captured_at| <= BELONGS_WITH_WINDOW_SECONDS
prints = catalog.feature_prints_for([photo] + frames)
missing = those without a print whose preview file exists
if missing: computed = feature_prints(missing previews, settings.vision_analyzer_path)   # lock released
            if computed is None: raise the helper ValueError
            with self._mutation(): save each computed print
if photo has no print: return empty
distance(group) = min Euclidean distance from photo's print to its frames' prints of equal length
return groups with a distance, sorted by (distance, group_id), first BELONGS_WITH_LIMIT
```

- [ ] **Step 1: Write the failing tests** (helper stubbed by patching `lumen.library.feature_prints`; prints seeded with `save_feature_print`; frames given capture times by EXIF `36867` as `test_web.py` does):
  - `test_belongs_with_orders_nearby_bursts_by_feature_print_distance`: five single-frame bursts within 10 minutes plus one 11 minutes away that is closest of all → four IDs in distance order, the far one absent, the frame's own burst absent.
  - `test_belongs_with_respects_filters`: a candidate excluded by a `GroupFilters` (e.g. `date_to` before it) is absent.
  - `test_belongs_with_computes_missing_prints_in_one_batch_and_stores_them`: no prints seeded; the stub is called once with every preview path and its results are stored.
  - `test_belongs_with_refuses_when_the_helper_cannot_batch` (Review Focus 1): stub returns `None` → `ValueError` whose message contains `scripts/bootstrap.sh`.
  - `test_belongs_with_is_empty_without_a_capture_time` (Review Focus 2).
  - `test_belongs_with_skips_prints_of_another_length` (Review Focus 4): one candidate has a 3-float print, the focus 2 floats → that candidate absent, no exception.
  - `test_belongs_with_rejects_unknown_ids`: `0`, `"1"`, `True`, `99999` → `ValueError`.
- [ ] **Step 2: Run** `tests.test_library` — the new tests FAIL.
- [ ] **Step 3: Implement** per the algorithm.
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat: suggest the bursts a frame most resembles`

### Task 4: `Library.move_photo` moves one frame and pins its old burst

**Files:**
- Modify: `src/lumen/library.py` (new `@_coordinated` method next to `split_photo`)
- Modify: `src/lumen/catalog.py` only if a one-transaction helper is needed (builder's call)
- Test: `tests/test_library.py`

**Interfaces:**
- Produces: `Library.move_photo(photo_id: int, group_id: int) -> dict` returning `{"photo_id", "group_id", "from_group_id", "automatic_pick_id"}` (`group_id` is the burst the frame is in after regrouping). Raises `ValueError` for invalid IDs, an unknown burst, or the frame's own burst (`"photo <id> is already in burst <group_id>"`).

Behaviour: re-anchor merges naming the old burst as `split_photo` does (`_reanchor_merges`); pin every remaining frame of the old burst that has no `merged_into` (other than the burst's earliest remaining frame) to that earliest frame's `_stable_key`; set the moved frame's `merged_into` to the target burst's `stable_key`; clear the moved frame's `manual_pick` and `model_pick`; `_regroup_and_write_sidecars()`; append `frame_moved` `{group_id, from_group_id}`; return the target's automatic pick.

- [ ] **Step 1: Write the failing tests:**
  - `test_move_photo_joins_another_burst_and_survives_reimport`: frame from burst A into burst B → B holds it, the event is recorded, and `import_path` again keeps it there.
  - `test_move_photo_keeps_the_old_burst_together_when_the_frame_bridged_a_gap` (the spec's pinning): three frames 0 s, 15 s, 30 s with a 20 s burst gap form one burst; move the 15 s frame out → the 0 s and 30 s frames are still one burst after the move and after reimport.
  - `test_move_photo_of_the_anchor_frame_leaves_the_rest_together` (Review Focus 3): use the `burst()` fixture (frames merged into the first frame); move the first frame → the others remain one burst.
  - `test_move_photo_of_the_last_frame_removes_the_old_burst`.
  - `test_move_photo_rejects_bad_input`: bad photo IDs, bad/unknown group IDs, and its own burst → `ValueError`; nothing changes.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `tests.test_library` — PASS, including the existing split and merge tests.
- [ ] **Step 5: Commit** `feat: move one frame into another burst`

### Task 5: Routes

**Files:**
- Modify: `src/lumen/web.py` (GET block near `/api/groups`; POST block near `/api/photos/<id>/split`)
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `Library.belongs_with`, `Library.move_photo`; `parse_group_filters`.
- Produces: `GET /api/photos/<id>/belongs-with?<the /api/groups filter query>` → `{"photo_id", "group_ids"}`; `POST /api/photos/<id>/move` body exactly `{"group_id": <int>}` → `move_photo`'s dict. A `ValueError` is a 400 `{"error"}`.

- [ ] **Step 1: Write the failing tests:**
  - `test_belongs_with_route_returns_group_ids_only`: seed prints; response keys are exactly `photo_id`, `group_ids`; the body contains no part of `self.root`; a malformed filter (`?nope=1`) → 400.
  - `test_move_route_validates_and_moves`: `{}`, `{"group_id": "2"}`, `{"group_id": 2, "x": 1}` and an unknown ID → 400 with no path in the error; a valid move → 200 with the four keys; a cross-origin POST is refused as other mutation routes are (reuse that test's helper).
- [ ] **Step 2: Run** `tests.test_web` — FAIL.
- [ ] **Step 3: Implement.** Add `/move` to whatever list marks mutation routes, if one applies (see the tuple near line 1143).
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat: belongs-with and move routes`

### Task 6: `G` and `Shift`+`G` mark bursts for merge

**Files:**
- Modify: `src/lumen/static/app.js` (`CULLING_KEYS`/`CULLING_ACTIONS`, `setMergeSelection`, `burstSection`, `updateWorkflow`'s merge sync)
- Modify: `src/lumen/static/styles.css` (`.burst.marked { outline: 2px solid var(--cyan); }` or equivalent)
- Modify: `src/lumen/static/index.html` (shortcuts dialog: `G`, `Shift`+`G`)
- Test: `tests/test_web.py` (new scenario in the JS harness)

**Interfaces:**
- Produces: `toggleMergeMark(photo, { range: boolean })` in `app.js`; `state.mergeAnchor` (group ID of the most recently marked burst, or `null`).

`handleCullingKey` lowercases keys, so read `event.shiftKey` for the range. The range is every burst in `state.groups` order between the anchor and the focused frame's burst, inclusive, all marked. Unmarking the anchor moves it to the most recently marked burst still marked (keep insertion order of `state.mergeSelection`), or `null`. Every change goes through `setMergeSelection` so the checkboxes, `#merge-count` and `#merge-batches` follow; `updateWorkflow` sets `marked` on `[data-burst]` sections. Announce `Burst N marked for merge (k marked)` / `Burst N unmarked (k marked)`; for a range, `k bursts marked for merge`.

- [ ] **Step 1: Write the failing test** `test_javascript_g_marks_bursts_for_merge_and_shift_g_marks_a_range` (scenario `merge-keys`, three single-frame bursts): `g` on frame 1 → selection `{1}`, section 1 has `marked`, announcement as above; `g` again → empty; `Shift`+`g` on frame 3 with nothing marked → `{3}` (Review Focus 5); `g` on frame 1 then `Shift`+`g` on frame 3 → `{1, 2, 3}`; `g` in a text input does nothing.
- [ ] **Step 2: Run** the test — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the test, the existing `test_javascript_batch_rail_merges_selected_batches` and `test_javascript_culling_keys_move_focus_and_decide_in_place`, and `node --check src/lumen/static/app.js` — PASS.
- [ ] **Step 5: Commit** `feat: G marks a burst for merge, Shift+G a run of them`

### Task 7: `B` opens "Belongs with…"; "Move here" moves the frame; release

**Files:**
- Modify: `src/lumen/static/app.js`, `src/lumen/static/styles.css`, `src/lumen/static/index.html` (shortcuts dialog: `B`)
- Modify: `src/lumen/__init__.py`, `pyproject.toml` (`0.5.0`), `CHANGELOG.md`, `AGENTS.md`, `docs/HANDOFF.md`
- Test: `tests/test_web.py` (JS harness scenario)

**Interfaces:**
- Consumes: `GET /api/photos/<id>/belongs-with`, `POST /api/photos/<id>/move` (Task 5).
- Produces: `toggleBelongsWith(photo)` in `app.js`, bound to `b` in `CULLING_KEYS`.

The request sends the same filter query string the sheet used for `/api/groups`. The panel (`section.belongs-with`, `aria-label="Belongs with"`) goes after the focused frame's `.burst-frames` grid inside its burst section. A heading says `Belongs with…`, then one row per returned group ID found in `state.groups`: up to three thumbnails from that group's photos, `Burst N · HH:MM · k frames` (reuse `batchText`/`startTime`/`frameCount`), and a `<button>Move here</button>`. It shows `No similar bursts within 10 minutes.` when the list is empty, and the server's error text on a 400. It closes on `b`, `Escape`, or focus moving to a frame of another burst. "Move here" follows `splitFrame`'s pattern: `post(.../move, {group_id})`, `load()`, `focusFrame(photo.id)`, `scrollToGroup(result.group_id)`, announce `<name> moved to Burst N`. While busy or `state.cullingBlocked`, it announces as `splitFrame` does.

- [ ] **Step 1: Write the failing test** `test_javascript_b_suggests_bursts_and_move_here_moves_the_frame` (scenario `belongs-with`; the harness's fetch answers `/belongs-with` with `{"photo_id": 3, "group_ids": [2, 1]}` and `/move` with `{"photo_id": 3, "group_id": 2, "from_group_id": 3, "automatic_pick_id": 2}`): `b` on frame 3 renders two rows in order `Burst 2`, `Burst 1`; the request URL carries the sheet's filter query; activating the first row's button POSTs `{"group_id": 2}` to `/api/photos/3/move` and announces `… moved to Burst 2`; `b` again closes the panel; an empty list shows the empty text; pressing `3` with focus on a Move here button does not rate.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement**, then the release: bump to `0.5.0` in both files, add `## 0.5.0 (2026-11-09)` to `CHANGELOG.md` (G/Shift+G marking, Belongs with…, feature prints, helper batch mode, rerun `scripts/bootstrap.sh` to rebuild the helper), add a Current tool state bullet to `AGENTS.md` (the routes, `feature_prints` table, window/limit constants, helper schema 3 and batch mode, the pinning rule), and update `docs/HANDOFF.md`.
- [ ] **Step 4: Run** the full suite `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest discover -s tests -v` and `node --check src/lumen/static/app.js` — all PASS.
- [ ] **Step 5: Commit** `feat: Belongs with… suggests and moves a frame; release 0.5.0`
