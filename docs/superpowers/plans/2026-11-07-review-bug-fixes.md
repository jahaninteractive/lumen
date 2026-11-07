# Review Bug Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the ten defects a code review found in the 0.3.0 commits (d3ef6e0, 13a397c): the cull job, burst merges, the burst gap slider, the contact sheet, the model selects, the Overpass fetch and the More filters fold.

**Architecture:** Each fix sits in the shared function every caller routes through. The cull job gets a per-burst guard so one burst can never end the job. A model's pick is stored apart from a photographer's pick through a new `photos.model_pick` column, so "a rerun never undoes a frame you chose by hand" can be enforced. Front-end fixes stay inside the existing handlers in `app.js`.

**Tech Stack:** Python 3 stdlib + Pillow, SQLite, vanilla JS tested through the Node harness in `tests/test_web.py`.

**Spec:** this plan carries its own spec, the review findings below, since no design doc preceded the fixes.

## Findings this plan fixes

1. `_cull_group` matches the model's frame against a burst snapshot with a bare `next()`; a burst that changed mid-job raises `StopIteration` (or `AttributeError` for a removed frame) and ends the whole job (`src/lumen/library.py:770-774`).
2. A fresh critique overrides a hand pick, so a first run or a Redo run replaces the photographer's frame, against the Assist help text (`library.py:773`, `index.html:137`).
3. The model's pick goes through `set_pick`, so it is stored as `manual_pick=1` with a `manual_pick` event, indistinguishable from the photographer's (`library.py:774`).
4. A merge stores `merged_into` as the target burst's stable key; when a wider burst gap changes the target's first frame, every merged frame falls back to a burst of its own (`library.py:2501-2505`).
5. The Burst gap slider disables itself on every `change`, so keyboard focus drops to `<body>` after one arrow press, and each step regroups the catalog (`app.js:3470-3481`).
6. `burstSection` treats a burst as single from the frames on this page, so a filtered multi-frame burst loses its frame count and Best fact (`app.js:409, 420`).
7. The Review and Fast model selects sit in the batch rail, so hiding the rail hides the only model controls (`index.html:24-27`). Ruling: they stay in the rail, and the hidden rail becomes a narrow strip that shows they are there.
8. A truncated gzip body from Overpass raises `EOFError` or `zlib.error`, and a truncated plain body raises `JSONDecodeError`; both escape `fetch_region` as tracebacks (`mapdata.py:627, 640`).
9. `syncFilters` reopens More filters on every `load()` while a folded filter is set, so it cannot be kept closed (`app.js:1573`).
10. The cull job ends with a whole-catalog regroup and sidecar rewrite, though each pick already wrote its own burst's sidecars, and it re-reads every kept frame for `manual_pick` (`library.py:747-748, 772`).

## Global Constraints

- Pillow is the only runtime Python dependency; no new packages.
- Bump `__version__` in `src/lumen/__init__.py` and `version` in `pyproject.toml` together to `0.3.1`, and add a `## 0.3.1` section to `CHANGELOG.md`.
- Red-green tests for every behaviour change. Run the suite warning-strict with `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest discover -s tests -v`, and `node --check src/lumen/static/app.js`.
- No AI vendor or model names in tracked files or commit messages, and no co-author or session trailers on commits.
- Preserve keyboard tab behaviour, live announcements and native controls.
- Never touch camera originals; tests build their own fixtures in temporary directories.

## Review Focus

- **A burst emptied or fully rejected mid-job.** The job logs it and carries on; the Task 1 test removes a frame between snapshot and pick.
- **A catalog that already holds model picks stored as `manual_pick=1` from an earlier 0.3.0 cull run.** They stay treated as hand picks, since nothing records which they were; the CHANGELOG line says so. No migration guesses.
- **A merge chained onto a burst that was itself merged.** The Task 3 test merges C into B into A and widens the gap; all frames stay together.
- **Holding an arrow key on the Burst gap slider.** Many `change` events produce at most one request in flight plus one trailing request with the final value; the Task 4 scenario asserts this.
- **An Overpass body that is valid gzip of truncated JSON.** It is reported as incomplete, not as a traceback; the Task 6 test covers both gzip and plain truncation.

---

### Task 1: The cull job survives a changing burst and never overrides a hand pick

Covers findings 1, 2, 3 and 10.

**Files:**
- Modify: `src/lumen/catalog.py` (`_MIGRATIONS["photos"]`, `set_manual_pick` at 980, `merge_groups` at 675, `restore_work` at 754, `_photo_from_row` at 1129)
- Modify: `src/lumen/domain.py:117` (`Photo`)
- Modify: `src/lumen/library.py` (`start_cull_job` 726-752, `_cull_group` 754-784, `set_pick` 2388)
- Modify: `src/lumen/sidecars.py:50`
- Modify: `src/lumen/static/index.html:137` only if the help text no longer matches
- Test: `tests/test_library.py`, `tests/test_catalog.py`, `tests/test_sidecars.py`

**Interfaces:**
- Produces: `Photo.model_pick: bool = False`; `Catalog.set_manual_pick(photo_id: int, *, by_model: bool = False) -> int`; `Library.set_pick(photo_id: int, *, by_model: dict | None = None) -> None`, where `by_model` is the provenance detail `{"job_id", "model", "role"}` and a non-None value records a `model_pick` event instead of `manual_pick`.
- Rule: a burst is hand-picked when some kept frame has `manual_pick` true and `model_pick` false. `_select` is unchanged, so a model pick stays sticky through regroups exactly as a hand pick does.

- [ ] **Step 1: Write the failing tests**

In `tests/test_library.py`, change `test_cull_job_picks_each_burst_with_the_review_model_and_advises_develop_for_the_pick`:
- After the first run assert `"model_pick"` is in the last frame's event kinds and `"manual_pick"` is not, and `library.catalog.get_photo(last).model_pick` is true.
- The Redo run after the hand pick must keep the hand pick: assert `burst["photos"][0]["id"] == burst["automatic_pick_id"]` after it, `redone == {"picked": 0, "advised": 2, "skipped": 0, "failed": 0}`, and `(FakeReview.critiques, FakeReview.advice) == (1, 4)` (a hand-picked burst is never critiqued; its hand pick is the frame advised).

Add `test_cull_job_logs_a_burst_that_changed_and_carries_on`: two 3-frame bursts and a lone frame. Before the job, reject frame X of the second burst. The fake's first `critique_burst` call (first burst) un-rejects X through a second `Library` on the same settings, so the job's snapshot of the second burst no longer matches the catalog; its second call returns X's id. Today this raises `StopIteration` and the job fails. Assert the job status is `succeeded`, `result["failed"] == 1`, the lone frame is still advised, and the job log names "changed". (A frame rejected mid-job is already caught inside `critique_burst`, so it would not make this test fail first.)

Add `test_cull_job_does_not_regroup_the_catalog`: patch `Library._regroup_and_write_sidecars` with a `Mock` after import, run a cull job, assert the mock was not called.

In `tests/test_catalog.py`, add `test_set_manual_pick_by_model_marks_model_pick_and_a_hand_pick_clears_it`: `set_manual_pick(a, by_model=True)` gives `a` `manual_pick and model_pick`; `set_manual_pick(b)` in the same group leaves `b` `manual_pick and not model_pick` and `a` neither. Add a `merge_groups` case asserting a dropped pick clears `model_pick` too.

In `tests/test_sidecars.py`, assert a model pick writes `"selection": {"best_in_group": True, "manual": False, "model": True}`.

- [ ] **Step 2: Run them to verify they fail**

Run: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_library tests.test_catalog tests.test_sidecars -v`
Expected: the new and changed tests FAIL (`model_pick` attribute missing, `StopIteration` in the job log, regroup mock called).

- [ ] **Step 3: Implement**

- `catalog.py`: add `"model_pick": "INTEGER NOT NULL DEFAULT 0"` to `_MIGRATIONS["photos"]` and to the `CREATE TABLE photos` columns. `set_manual_pick` clears both flags across the group, then sets `manual_pick = 1, model_pick = ?`. `merge_groups` clears `model_pick` wherever it clears `manual_pick`. `restore_work` copies `model_pick` when the snapshot row has the column, else 0. `_photo_from_row` reads it.
- `domain.py`: add `model_pick: bool = False` to `Photo`.
- `sidecars.py`: `"selection": {"best_in_group": selected, "manual": photo.manual_pick and not photo.model_pick, "model": photo.model_pick}`.
- `library.set_pick`: pass `by_model=by_model is not None` to the catalog and append `model_pick` with `{"group_id": group_id} | by_model`, else `manual_pick` as now.
- `_cull_group`:
  - Read `manual_pick` and `model_pick` from the snapshot rows (`group["photos"]` carries `p.*`), never `get_photo`.
  - When a kept frame is a hand pick, it is `chosen` and the burst is not critiqued, with or without `redo`.
  - Otherwise critique as now. `chosen = next((... ), None)`; when None, log `"<name>: pick skipped: the burst changed while the job ran"` and return `names + ["failed"]`.
  - Call `self.set_pick(chosen["id"], by_model={"job_id": job.id, "model": review.model, "role": getattr(review, "role", None)})` when the pick differs, inside `try ... except ValueError` that logs and counts `failed` (a frame removed since the snapshot).
- `start_cull_job.work`: wrap each `_cull_group` call in `try ... except Exception as error`, log `"<label>: {type(error).__name__}: {error}"` and count `failed`, so no burst ends the job. Delete the closing `_regroup_and_write_sidecars()` block; `set_pick` and `critique_burst` already write the touched sidecars.
- `index.html:137`: keep the sentence "a rerun never undoes a frame you chose by hand"; it is now true. Add nothing.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_library tests.test_catalog tests.test_sidecars -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lumen/catalog.py src/lumen/domain.py src/lumen/library.py src/lumen/sidecars.py tests/test_library.py tests/test_catalog.py tests/test_sidecars.py
git commit -m "fix: the cull job survives a burst that changes mid-run, never overrides a hand pick, and records its pick as the model's"
```

### Task 2: The model pick is visible as the model's in the browser

Covers the user-facing half of finding 3. Depends on Task 1.

**Files:**
- Modify: `src/lumen/web.py` (wherever the photo payload exposes `manual_pick`; grep it)
- Modify: `src/lumen/static/app.js` (where the best/pick label is rendered; grep `manual_pick` and `bestFrame`)
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `Photo.model_pick` and the `model_pick` column in `p.*` rows from Task 1.
- Produces: `model_pick: bool` on each photo in `GET /api/groups` and `GET /api/photos/<id>`.

- [ ] **Step 1: Write the failing test** `test_photo_payload_marks_a_model_pick`: set a pick through `library.set_pick(id, by_model={"job_id": 1, "model": "m", "role": "review"})`, fetch `/api/photos/<id>`, assert `photo["model_pick"] is True`; then a plain `set_pick` gives `False`. Assert the model name is not in the response body.
- [ ] **Step 2: Run** `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_web -k model_pick -v`. Expected: FAIL (key missing).
- [ ] **Step 3: Implement.** Expose the flag. Where the frame view or tile names the best frame, say "best (model's pick)" when `model_pick` is true; skip the label change if no such text exists today, and say so in the commit body.
- [ ] **Step 4: Run** the same command plus `node --check src/lumen/static/app.js`. Expected: PASS, no output from node.
- [ ] **Step 5: Commit** `fix: the browser can tell the model's pick from the photographer's`.

### Task 3: Merges survive any burst gap

Covers finding 4.

**Files:**
- Modify: `src/lumen/library.py:2493-2505` (`_regroup_and_write_sidecars`)
- Test: `tests/test_library.py`

**Interfaces:**
- No signature changes. `merged_into` keeps meaning "the stable key of the target burst's first frame when merged", so existing catalogs need no migration.

- [ ] **Step 1: Write the failing tests**

`test_merge_survives_a_wider_burst_gap_that_moves_the_target_start`: frames at 0, 1 s (burst A0), 4, 5 s (burst A), 20, 21 s (burst B) with gap 2 s. Merge B into A. Set the gap to 4 s so A joins A0 and A's first frame changes. Assert B's two frames share a burst with A's frames. Set the gap back to 2 s; assert B's frames are still with A's.

`test_chained_merges_survive_a_regroup`: merge C into B, then B into A, then `set_burst_gap` to a new value; assert all frames of A, B and C share one burst.

- [ ] **Step 2: Run** `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_library -k merge -v`. Expected: the new tests FAIL with B's frames in one-frame bursts.
- [ ] **Step 3: Implement.** Resolve `merged_into` against every frame's own key, not only each group's first frame:

```python
by_key = {_stable_key(photo): group for group in groups for photo in group}
pending = sorted((p for p in photos if p.merged_into and p.merged_into != Catalog.SPLIT_KEY), key=lambda p: p.path)
while pending:  # chains (C into B into A) resolve once their target frame is placed
    placed = [p for p in pending if p.merged_into in by_key]
    if not placed:
        break
    for photo in placed:
        by_key[photo.merged_into].append(photo)
        by_key[_stable_key(photo)] = by_key[photo.merged_into]
    pending = [p for p in pending if p not in placed]
groups += [[photo] for photo in pending]  # a target frame that left the catalog; split frames go here too
```

Split frames (`merged_into == SPLIT_KEY`) still get a burst of their own. Keep the existing `ponytail:` note on the fallback.

- [ ] **Step 4: Run** the full library and catalog tests. Expected: PASS, including the existing split and merge tests.
- [ ] **Step 5: Commit** `fix: merged frames follow their target burst through any burst gap`.

### Task 4: The Burst gap slider keeps focus and sends one request at a time

Covers finding 5.

**Files:**
- Modify: `src/lumen/static/app.js:3470-3481`
- Test: `tests/test_web.py` (new scenario in the `run_javascript_cleanup_regression` harness)

**Interfaces:**
- No new globals beyond two module-level `let` flags next to `burstGap`.

- [ ] **Step 1: Write the failing scenario** `burst-gap`: focus `#burst-gap`, fire `change` three times with values 3, 4, 5 while the first `/api/burst-gap` POST is held unresolved. Assert `#burst-gap` is never `disabled`, `document.activeElement` is still the slider, exactly one POST is in flight, and after it resolves exactly one more POST is sent with `{seconds: 5}`. Add `test_javascript_burst_gap_keeps_focus_and_coalesces_steps` calling it.
- [ ] **Step 2: Run** `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_web -k burst_gap -v`. Expected: FAIL on `disabled`.
- [ ] **Step 3: Implement.** Never disable the slider. Keep `gapInFlight` and `gapPending`. On `change`, if a request is in flight set `gapPending = true` and return. Otherwise post the slider's current value; in `finally`, if `gapPending`, clear it and post again when the value differs from the one just applied. Set `aria-busy="true"` on the slider's label pair while posting, so the regroup is announced without moving focus. Keep the existing announcement.
- [ ] **Step 4: Run** the same command and `node --check src/lumen/static/app.js`. Expected: PASS.
- [ ] **Step 5: Commit** `fix: the Burst gap slider keeps keyboard focus and coalesces arrow steps into one regroup`.

### Task 5: Bursts are laid out by their size, not by how many frames show

Covers finding 6.

**Files:**
- Modify: `src/lumen/static/app.js:409, 420` (`burstSection`)
- Test: `tests/test_web.py`

- [ ] **Step 1: Write the failing scenario** `filtered-burst`: render a group with `frame_count: 8` and one frame passed in (as the Keepers filter does). Assert the section lacks `burst-single` and its facts text contains "8 frames" and "best". A group with `frame_count: 1` still gets `burst-single`.
- [ ] **Step 2: Run** `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_web -k filtered_burst -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Use `const single = group.frame_count === 1;` for both the class and the facts line.
- [ ] **Step 4: Run** the same command and `node --check`. Expected: PASS.
- [ ] **Step 5: Commit** `fix: a filtered burst keeps its frame count and best frame`.

### Task 6: A dropped Overpass download fails cleanly

Covers finding 8.

**Files:**
- Modify: `src/lumen/mapdata.py:624-640` (`fetch_region`)
- Test: `tests/test_mapdata.py:637` (`test_fetch_region_reports_http_and_network_failures`)

- [ ] **Step 1: Write the failing cases.** Add to the `cases` tuple: a `FakeResponse(gzip.compress(json.dumps(OVERPASS).encode())[:-20], headers={"Content-Encoding": "gzip"})` expecting `"Overpass answered incomplete"`; a `FakeResponse(b'{"elements": [')` expecting `"Overpass answered incomplete"`. The directory must stay empty.
- [ ] **Step 2: Run** `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_mapdata -k fetch_region -v`. Expected: FAIL with `EOFError` and `JSONDecodeError`.
- [ ] **Step 3: Implement.** Catch `(EOFError, zlib.error)` around `gzip.decompress` and `ValueError` around `json.loads(body)`, each raising `RuntimeError(f"Overpass answered incomplete: the connection dropped after {len(body)} bytes") from None`. Import `zlib` from the stdlib.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `fix: a truncated Overpass answer is reported, not a traceback`.

### Task 7: The rail collapses to a narrow strip, and More filters stays closed

Covers findings 7 and 9. Both are small markup, CSS or `syncFilters` changes reviewed together.

**Ruling (photographer, 2026-11-07):** the model selects stay at the top of the rail. Hiding the rail no longer removes it: it collapses to a narrow strip with small buttons, so the photographer can see that the model selects and the batches are there and open the rail again from either. `AGENTS.md` is updated to say the selects live in the rail.

**Files:**
- Modify: `src/lumen/static/index.html:22-33` (add the collapsed strip inside `#batch-rail`)
- Modify: `src/lumen/static/styles.css:101-102` (and the 680 px media block near 446)
- Modify: `src/lumen/static/app.js:3405-3423` (`showRail` and the toggle), `app.js:1598-1601` (role-model pair visibility), `app.js:1571-1574` (`syncFilters`)
- Modify: `AGENTS.md:46` ("the footer strip's Review model and Fast model selects" becomes the rail's, and one sentence on the collapsed strip)
- Test: `tests/test_web.py`

**Interfaces:**
- Markup: `<div id="rail-strip" class="rail-strip">` as the first child of `#batch-rail`, holding `<button type="button" id="rail-show-models" class="secondary">Models</button>` and `<button type="button" id="rail-show-batches" class="secondary">Batches</button>`. Each has `aria-controls="batch-rail"`, `aria-expanded="false"` and a `title` ("Show the model selects", "Show the batch list").
- Behaviour: `showRail(shown: boolean)` stays the one function that changes `data-rail` and `localStorage` key `lumen-rail-hidden`. It now also syncs `aria-expanded` on all three toggles. `#rail-show-models` calls `showRail(true)` and focuses `#review-model`; `#rail-show-batches` calls `showRail(true)` and focuses the first `.batch-button`, else `#batch-list`.
- `#rail-show-models` is hidden whenever `#role-model-pair` is hidden (fewer than two models), set in the same place as the pair's own `hidden`.
- Layout: with `data-rail="hidden"` the workspace grid is `2.5rem minmax(0, 1fr)`; the strip shows, and the rail header and batch list are `display: none`. With the rail shown the strip is `display: none`. The strip's buttons are compact (small font, tight padding) and stack vertically with `writing-mode: vertical-rl`, keeping the charcoal/amber/cyan tokens and visible focus. Below 680 px the strip is one horizontal row above the sheet, with horizontal text.

- [ ] **Step 1: Write the failing tests.**
  - `test_rail_strip_offers_models_and_batches`: parse `/` with `LandmarkParser`; assert `#rail-strip` is inside `#batch-rail` and holds `#rail-show-models` and `#rail-show-batches`, both buttons with `aria-controls="batch-rail"`; assert `#role-model-pair` is still inside `#batch-rail`.
  - Scenario `rail-strip`: with two review models in the status, click `#rail-toggle`; assert `data-rail="hidden"`, all three toggles report `aria-expanded="false"`, and `#rail-show-models` is not hidden. Click `#rail-show-models`; assert `data-rail` is not hidden, the active element is `#review-model`, and `localStorage` no longer holds `lumen-rail-hidden`. Hide again, click `#rail-show-batches`; assert focus lands on the first `.batch-button`. With one model, `#rail-show-models` is hidden.
  - Scenario `filter-fold`: load with `?date_from=2026-01-01`; assert `#filter-more` is open. Set `open = false`, run `load()` again; assert it stays closed and the label still reads "More filters (1 set)". Then load with no folded filter, then with `camera=X`; assert it opens again.
- [ ] **Step 2: Run** `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_web -k "rail_strip or filter_fold" -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the markup, CSS and handlers above. In `syncFilters`, keep a module-level `let foldedShown = 0;`; open the fold only when `folded.length && !foldedShown`, then set `foldedShown = folded.length`. The fold then opens when a folded filter becomes set and is otherwise left as the photographer put it. Update `AGENTS.md:46`.
- [ ] **Step 4: Run** the same command, `node --check src/lumen/static/app.js`, and the full `tests.test_web`. Expected: PASS. Then take a headless screenshot at 1456x830 with the rail hidden and shown, and one at 390 px wide, and check the strip by eye.
- [ ] **Step 5: Commit** `fix: a hidden rail leaves a strip that opens the model selects or the batches, and More filters stays closed once closed`.

### Task 8: Release 0.3.1

**Files:**
- Modify: `src/lumen/__init__.py:3`, `pyproject.toml:7`, `CHANGELOG.md`, `docs/HANDOFF.md`, `AGENTS.md` (only the cull job's pick; Task 7 already fixed the model-select line)

- [ ] **Step 1:** Set both versions to `0.3.1`. Add `## 0.3.1 (<date>)` to `CHANGELOG.md` with one sentence per fix, including that model picks made by a 0.3.0 cull run still count as hand picks because nothing recorded the difference.
- [ ] **Step 2: Run the full suite.**

```sh
env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest discover -s tests -v
node --check src/lumen/static/app.js
```

Expected: every test passes; node prints nothing.

- [ ] **Step 3: Commit** `fix: release 0.3.1`.

## Out of scope

The eight project-wide speed findings from the earlier backend-efficiency pass (missing `photos(group_id)` index, whole-catalog scans in `_write_sidecar` and `_group`, the double `list_groups` in `/api/groups`, per-request tool probes in `/api/status`, two exiftool launches per import, the full decode on lossless JPEG export) belong in their own plan; they change performance, not behaviour, and want measurements before and after.
