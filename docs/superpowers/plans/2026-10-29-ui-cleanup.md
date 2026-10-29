# UI Clean-up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the web UI read consistently: one Assist tab, one model/activity strip, prose folded away, advice that runs without blocking culling and is still there when the photographer comes back.

**Architecture:** Nine small rounds on the three static files (`index.html`, `app.js`, `styles.css`) plus two library changes: the mutation lock is released while a model answers, and stored advice is read back through the existing photo and crops payloads. Element ids are kept wherever an element only moves, so `app.js` handlers and most tests survive; only labels, positions and a few ids change.

**Tech Stack:** Python 3 stdlib server, Pillow, vanilla JS, CSS; tests are `unittest` with the node `vm` scenario harness in `tests/test_web.py` (`run_javascript_cleanup_regression`).

**Spec:** `docs/superpowers/specs/2026-10-29-ui-cleanup-design.md`

## Global Constraints

- No AI vendor or model names in tracked files or commit messages; the shared guide is `AGENTS.md`.
- Pillow is the only runtime Python dependency; no new JS or Python dependencies.
- Keep every control at `min-height: 44px`, keep `role="tab"` keyboard behaviour (`wireTablist`), keep `aria-live` announcements, keep the charcoal/amber/cyan tokens.
- Loopback-only server; mutation routes keep host/origin and strict JSON checks; browser responses never carry source paths, fingerprints or raw model output.
- Run tests as `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_web -v` (and `tests.test_library`, `tests.test_library_coordination` where named). Run `node --check src/lumen/static/app.js` after every `app.js` change.
- Work on branch `ui-cleanup` from `main`. One commit per task.
- When a test pins old copy or an old position, change the test to the spec's copy in the same task; never keep old copy to satisfy a test.

## Review Focus

1. A critique request that is answering while the photographer rejects a frame of that burst: the write must re-check the burst and refuse with "burst changed during review" rather than store a pick for a rejected frame. (Task 1 test.)
2. A stored composition review whose candidate no longer exists after the crop set changed: the crops payload must return `review: null`, not a dangling id. (Task 2 test.)
3. A fresh browser with an empty catalog must land on Add photos with the panel open; one with frames must land folded, and a remembered tab must still win. (Task 4 scenario.)
4. The not-loaded sentence must still be readable from inside the frame dialog, where the footer is behind the modal. (Task 8 test: `#frame-ai-availability` inside `#loupe`.)
5. A job that finishes while the footer strip is the only progress surface must still announce and reload, as the top strip did. (Task 5 scenario: `job-strip`.)

---

### Task 1: Release the mutation lock while a model answers

**Files:**
- Modify: `src/lumen/library.py` (`suggest_develop` ~1022, `review_composition` ~1127, `critique_burst` ~1184, `caption_photo` ~1243, `tag_photo` ~1918)
- Test: `tests/test_library_coordination.py`

**Interfaces:**
- Consumes: `Library._mutation()` (re-entrant per thread via `_MUTATION_HELD`), `_coordinated` decorator.
- Produces: the same five public methods with the same signatures and return shapes. Each calls the model with no lock held and wraps its catalog writes (events, `save_tags`, `_save_caption`, `_write_sidecar`) in `with self._mutation():`. `critique_burst` re-reads the group inside the lock before writing, as it already does before the write today.

- [ ] **Step 1: Write the failing test**

In `tests/test_library_coordination.py`, add to the existing test class (reuse its library fixture and a two-frame group; copy the group setup from `test_critique_routes_gate_on_review_model_and_never_change_the_pick` in `tests/test_web.py` if the class lacks one):

```python
def test_model_calls_do_not_hold_the_mutation_lock(self):
    started, release = threading.Event(), threading.Event()

    class SlowTagger:
        model, role = "review-model", "review"
        def critique_burst(self, frames):
            started.set()
            assert release.wait(5), "test released too late"
            from lumen.omlx import BurstCritique
            return BurstCritique(frame_id=frames[0][0], rationale="sharpest", confidence=0.9, raw_response="{}")

    group_id = self.library.list_groups()[0]["id"]
    other_id = self.library.list_groups()[0]["photos"][1]["id"]
    worker = threading.Thread(target=lambda: self.library.critique_burst(group_id, SlowTagger()))
    worker.start()
    self.assertTrue(started.wait(5))
    done = threading.Event()
    threading.Thread(target=lambda: (self.library.set_decision(other_id, "flag"), done.set())).start()
    self.assertTrue(done.wait(2), "a decision must not wait for the model")
    release.set()
    worker.join(5)
    self.assertEqual("flag", self.library.catalog.get_photo(other_id).decision)
```

Check the real names of the critique result class and the decision method with `grep -n "class BurstCritique\|def set_decision\|def decide" src/lumen/omlx.py src/lumen/library.py` and use those.

- [ ] **Step 2: Run it and watch it fail**

Run: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_library_coordination -k model_calls -v`
Expected: FAIL at "a decision must not wait for the model".

- [ ] **Step 3: Move the lock**

Remove `@_coordinated` from the five methods. In each, wrap only the post-model block in `with self._mutation():`. For `critique_burst` the block starts at `current = self._group(group_id)` and ends after the sidecar loop; the `except` branch's `append_event` also goes under its own `with self._mutation():`. For `tag_photo` the block starts at the `weak = ...` line. For `review_composition` it starts at `self.catalog.append_event(photo_id, "composition_reviewed", ...)`; the failure branch likewise. For `suggest_develop` both `append_event` calls. For `caption_photo` the `_save_caption` call and the failure branch.

- [ ] **Step 4: Run the test and the two library suites**

Run: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_library_coordination tests.test_library tests.test_web -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lumen/library.py tests/test_library_coordination.py
git commit -m "fix: model calls no longer hold the library lock; culling continues while advice is written"
```

---

### Task 2: Stored advice is readable again

**Files:**
- Modify: `src/lumen/library.py` (beside `latest_critique` ~1220)
- Modify: `src/lumen/web.py` (crops route ~363, photo details route that serves `GET /api/photos/<id>`)
- Test: `tests/test_web.py`

**Interfaces:**
- Produces: `Library.latest_composition_review(photo_id: int, candidate_ids: list[str]) -> dict | None` returning `{"candidate_id", "rationale", "confidence"}` from the newest `composition_reviewed` event whose `candidate_id` is in `candidate_ids`, else `None`.
- Produces: `Library.latest_develop_advice(photo_id: int) -> dict | None` returning `{"settings", "rationale"}` from the newest `develop_advised` event, else `None`. (`suggest_develop` must start storing `rationale` in that event; add the key.)
- Produces: `GET /api/photos/<id>/crops` gains `"review"`; `GET /api/photos/<id>` gains `"advice"`. Both are the dicts above or `null`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_web.py` near `test_composition_review_*` tests:

```python
def test_crops_payload_carries_the_stored_review_only_while_its_candidate_exists(self):
    # arrange a photo with crops and a FakeTagger whose review_composition returns the first candidate id
    code, crops = self.get_json(f"/api/photos/{photo_id}/crops")
    self.assertIsNone(crops["review"])
    with patch(...loaded..., FakeTagger):
        self.post_json(f"/api/photos/{photo_id}/composition-review", {})
    code, crops = self.get_json(f"/api/photos/{photo_id}/crops")
    self.assertEqual({"candidate_id", "rationale", "confidence"}, set(crops["review"]))
    with Library(Settings(library_dir=self.root / "library")) as library:
        self.assertIsNone(library.latest_composition_review(photo_id, ["nope"]))

def test_photo_payload_carries_the_last_develop_advice(self):
    self.assertIsNone(self.get_json(f"/api/photos/{photo_id}")[1]["advice"])
    # post /develop/suggest with a FakeTagger whose suggest_develop returns DevelopAdvice(settings={"exposure_ev": 0.5}, rationale="lift")
    advice = self.get_json(f"/api/photos/{photo_id}")[1]["advice"]
    self.assertEqual({"settings": {"exposure_ev": 0.5}, "rationale": "lift"}, advice)
```

Reuse the fake taggers and the raw fixture from the existing composition-review and develop-suggest tests in the same file.

- [ ] **Step 2: Run them and watch them fail**

Run: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_web -k stored_review -k develop_advice -v`
Expected: FAIL with `KeyError: 'review'` / `KeyError: 'advice'`.

- [ ] **Step 3: Implement the two readers and the two payload keys**

Both readers query `provenance` like `latest_critique` does (`ORDER BY id DESC LIMIT 1`, kind filtered, `photo_id = ?`). Neither returns `raw_response`.

- [ ] **Step 4: Run the suite**

Run: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_web tests.test_library -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lumen/library.py src/lumen/web.py tests/test_web.py
git commit -m "feat: crops and photo payloads carry the last stored crop review and develop advice"
```

---

### Task 3: Advice has a status, does not block, and is there when you come back

**Files:**
- Modify: `src/lumen/static/app.js` (`aiCall` ~64, `burstCritique` ~564, `askCompositionAI` ~247, `loadComposition` ~196, caption `ask` ~980, inspector `tag` ~905, `askDevelopAdvice` ~3178, `fillDevelop`)
- Modify: `src/lumen/static/styles.css`
- Test: `tests/test_web.py` (scenario harness)

**Interfaces:**
- Consumes: Task 2 payload keys `review` and `advice`.
- Produces: `state.aiTasks: Map<string, {phase: 'running'|'done'|'failed', text: string}>`; `aiCall(label, action, key = null)` records under `key` when given; `aiTaskLine(key) -> HTMLElement|null` returning `<p class="ai-task" data-phase="…">` with the spec's `<Label> running…|ready|failed: <reason>` text. Keys: `critique:<groupId>`, `composition:<photoId>`, `develop:<photoId>`, `caption:<photoId>`, `tag:<photoId>`.
- Produces: `#develop-use-advice` button ("Park the handles here") shown when `details.advice` exists and no live suggestion is parked.

- [ ] **Step 1: Write the failing scenario**

Add scenario `advice-status` to the harness in `run_javascript_cleanup_regression`: the fake `/api/groups/1/critique` POST resolves on a deferred promise the scenario controls; the fake `/api/photos/1/crops` returns `review: {candidate_id: <first id>, rationale: 'tighter', confidence: 0.8}`; `/api/photos/1` returns `advice: {settings: {exposure_ev: 0.5}, rationale: 'lift'}`.

```js
if (scenario === 'advice-status') {
  await inspect(photoById(1));
  const ask = element('#ask-burst-ai');
  const pending = ask.listeners.click();            // not awaited: the model is "thinking"
  assert.equal(state.busy, false, 'advice never sets busy');
  assert.equal(element('#burst-critique').querySelector('.ai-task').textContent, 'Burst pick running…');
  assert.equal(control('reject').disabled, false, 'culling stays enabled');
  await inspect(photoById(2));                       // walk away
  resolveCritique({ ...storedCritique, rationale: 'Fresh look.' });
  await pending;
  await inspect(photoById(1));                       // come back
  assert.ok(element('#burst-advice').textContent.includes('Fresh look.'));
  assert.equal(element('#burst-critique').querySelector('.ai-task').textContent, 'Burst pick ready');
  await loadComposition();
  assert.equal(state.recommendedCropId, firstCandidateId, 'stored crop review is applied on load');
  fillDevelop(photoById(1));
  assert.equal(element('#develop-advice').textContent, 'lift');
  assert.equal(element('#develop-use-advice').hidden, false);
  await element('#develop-use-advice').listeners.click();
  assert.equal(state.develop.fields.exposure_ev, 0.5);
}
```

Add the Python wrapper `test_javascript_advice_has_status_and_survives_leaving_the_frame` calling `self.run_javascript_cleanup_regression("advice-status")`. Mirror the existing `critique` scenario's fake fetch for the deferred POST.

- [ ] **Step 2: Run it and watch it fail**

Run: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_web -k advice_has_status -v`
Expected: FAIL at "advice never sets busy".

- [ ] **Step 3: Implement**

In `aiCall`, when `key` is given write `state.aiTasks.set(key, {phase, text})` at each phase using the spec's labels, and call `refreshAiTask(key)` which replaces any `.ai-task[data-key="<key>"]` element's text and phase in place. `aiTaskLine(key)` builds that element. Insert it: first child of `#burst-critique`; into `#compose-status`'s sibling slot in the Crop tab; before `#develop-advice`; first child of `#caption-controls`. Remove `state.busy = true/false` and the matching `updateWorkflow()` pairs from the five advice callers; keep `ask.disabled` while running. In `askCompositionAI` and `askDevelopAdvice` drop the `state.selectedId !== photoId` early returns that discard the answer: the server stores it; the UI applies it only when the frame is still shown. `loadComposition` sets `state.recommendedCropId = composition.review?.candidate_id ?? null` and `#composition-advice` from its rationale. `fillDevelop` shows `details.advice.rationale` in `#develop-advice` and unhides `#develop-use-advice`, whose click sets `develop.beforeAdvice` (if null) and `develop.fields = advice.settings`, then `syncDevelopControls(); renderDevelopPreview()`. Add `<button type="button" id="develop-use-advice" class="secondary" hidden>Park the handles here</button>` to the `develop-suggest` button row in `index.html`. CSS: `.ai-task[data-phase="running"] { color: var(--cyan) } .ai-task[data-phase="failed"] { color: var(--danger) }`.

- [ ] **Step 4: Run, check syntax, run the suite**

Run: `node --check src/lumen/static/app.js && env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_web -v`
Expected: PASS. The existing `critique` and `decisions` scenarios asserting `state.busy` during a critique must be updated to the new behaviour (busy stays false).

- [ ] **Step 5: Commit**

```bash
git add src/lumen/static tests/test_web.py
git commit -m "feat: frame-view advice shows a status, never blocks culling, and is restored on return"
```

---

### Task 4: Add photos tab, Assist tab, default tab from state

**Files:**
- Modify: `src/lumen/static/index.html` (tab rail 50-57, panels 61-275)
- Modify: `src/lumen/static/app.js` (`selectTab` ~2250, startup restore ~2269, `load()` ~1479, `updateWorkflow`)
- Modify: `src/lumen/static/styles.css`
- Test: `tests/test_web.py` (`test_finishing_tabs_have_accessible_controls_and_separate_cleanup` ~778, Import/Ingest scenarios ~4105, `test_export_panel_has_keeper_tag_button…` ~1268, any test asserting `#export-tab` is selected by default)

**Interfaces:**
- Produces: tabs in order `Add photos`, `Review`, `Assist`, `Astro`, `Export`, `Clear` with ids `add-tab`, `review-tab`, `assist-tab`, `astro-tab`, `export-tab`, `clear-tab`; panels `add-panel`, `review-panel`, `assist-panel`, `astro-panel`, `export-panel`, `clear-panel`. `add-tab` is `aria-selected="true"` in markup.
- Produces: inside `#add-panel` a `fieldset.segmented#add-source` with radios `name="add-source"` values `ingest` (label "Still on the memory card") and `import` (label "Already on a drive", checked); sections `#ingest-panel` and `#import-panel` keep their ids and contents and lose `role="tabpanel"`; the unchecked one is `hidden`. Choice remembered under `lumen-add-source`.
- Produces: `#assist-panel` contains, in order: `#assist-keepers`, `#tag-keepers`, `#redo-keepers`, `#tag-all` + `#retag-all`, `#vocabulary-panel`, `#write-digest` + `#digest-result`. All ids unchanged.
- Produces: `chooseDefaultTab(photoCount)` called once from the first `load()`: if `localStorage` has no `lumen-tab`, select `add-tab` when `photoCount === 0`, else `collapsePanel(true)`.

- [ ] **Step 1: Update the markup tests first**

In `test_finishing_tabs_have_accessible_controls_and_separate_cleanup` change the expected list to `["Add photos", "Review", "Assist", "Astro", "Export", "Clear"]` and the default to "Add photos". Add assertions: `by_id["assist-panel"]` is an ancestor of `by_id["assist-keepers"]`, `by_id["tag-all"]`, `by_id["vocabulary-panel"]`, `by_id["write-digest"]`; `by_id["export-panel"]` is an ancestor of none of them; `by_id["add-panel"]` is an ancestor of both `ingest-panel` and `import-panel`; the `add-source` radios exist with `import` checked. Add scenario `default-tab`: with `groups = []` and `photo_count: 0`, after `load()` `element('#add-panel').hidden === false`; with frames, `element('#workflow-panels').hidden === true`; scenario `tabs-remembered` (exists, remembers `export-tab`) still shows Export open.

- [ ] **Step 2: Run and watch them fail**

Run: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_web -k finishing_tabs -k default_tab -v`
Expected: FAIL on the tab list.

- [ ] **Step 3: Restructure the markup and wire it**

Move the blocks; do not retype them. In `selectTab`'s `onSelect`, `assist-tab` calls `loadVocabulary()`, `review-tab` only `loadRejectSuggestions()`. Wire the `add-source` radios: `change` toggles the two sections' `hidden` and stores the choice; restore on startup. The rail's `#write-digest` and `#digest-result` move into `#assist-panel`; the handler at ~3483 is unchanged. Add `chooseDefaultTab`.

- [ ] **Step 4: Run the suite and syntax check**

Run: `node --check src/lumen/static/app.js && env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_web -v`
Expected: PASS after fixing tests that reach `#import-tab`/`#ingest-tab` (they now tick the `add-source` radio after selecting `#add-tab`).

- [ ] **Step 5: Commit**

```bash
git add src/lumen/static tests/test_web.py
git commit -m "feat: Add photos and Assist tabs; the first visit lands on the sheet or on adding photos"
```

---

### Task 5: One strip for model, availability, activity and jobs; a quiet header

**Files:**
- Modify: `src/lumen/static/index.html` (header 11-29, footer 343-345)
- Modify: `src/lumen/static/app.js` (`load()` status text ~1483, `syncRoleModels` ~1444, `watchJob` ~1757)
- Modify: `src/lumen/static/styles.css` (`.masthead`, `.job-strip`, `.ai-bar`)
- Test: `tests/test_web.py` (tests asserting `#system-status` text, `job-strip` placement, `role-model-pair`)

**Interfaces:**
- Produces: `<footer class="ai-bar" aria-label="Local AI">` containing, in order, `#role-model-pair` (moved, same selects), `#ai-availability` (`class="quiet"`), `#ai-status` (unchanged), `#job-strip` (moved, same children, still `hidden` until a job).
- Produces: `#system-status` text = `[...stale, health].join(' · ')` where `health = (status.omlx_live?.online ? 'Local AI online' : 'Local AI offline')` followed by `` `${name} missing` `` for every `status.tools[name].available === false`, joined by ` · `. The older-server sentence stays as today when `omlx_roles` is absent.
- Produces: `#ai-availability.textContent = roleUnavailable('fast') || roleUnavailable('review') || ''` set in `updateWorkflow`.

- [ ] **Step 1: Write the failing assertions**

Markup test: `by_id["role-model-pair"]`, `by_id["job-strip"]` and `by_id["ai-availability"]` all have the `footer` node among parents; the `header` has none of them. Scenario `status-health` (fake `/api/status` with `omlx_live.online: true`, `tools: { 'darktable-cli': { available: false }, 'siril-cli': { available: true } }`): `element('#system-status').textContent === 'Local AI online · darktable-cli missing'` and does not include the model name. Keep `stale-server` scenario's expectation that the stale reason comes first.

- [ ] **Step 2: Run and watch them fail**

Run: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_web -k status_health -k footer -v`

- [ ] **Step 3: Move the markup, rewrite the status text, lay out the strip**

CSS: `.ai-bar { display: grid; grid-template-columns: auto 1fr; gap: .35rem 1rem; align-items: center; }` with `#job-strip` spanning both columns (`grid-column: 1 / -1`) and keeping its flex row; delete the `.job-strip` border-bottom and background so it reads as part of the footer. `.masthead` loses nothing else.

- [ ] **Step 4: Run the suite**

Run: `node --check src/lumen/static/app.js && env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_web -v`
Expected: PASS; the `job-strip` scenario still sees announcements and the reload on finish.

- [ ] **Step 5: Commit**

```bash
git add src/lumen/static tests/test_web.py
git commit -m "feat: model selects, availability, activity and job progress share the footer strip; the header reports health only"
```

---

### Task 6: One line per panel, the rest behind "How this works"

**Files:**
- Modify: `src/lumen/static/index.html` (every tabpanel; Develop panel quiet paragraphs 526, 539, 564, 589, 620, 622; Review panel 124; shortcuts dialog 363-379)
- Modify: `src/lumen/static/styles.css`
- Test: `tests/test_web.py` (tests pinning panel copy: ~283 ingest, ~5120 import, develop order ~4616, any `assertIn` on removed sentences)

**Interfaces:**
- Produces: each tabpanel starts with `<h2>` then one `<p class="lead">` with the spec's first line, then `<details class="how"><summary>How this works</summary>…</details>` holding the former paragraphs, lists and the Export glossary is deleted outright. Develop: the six quiet paragraphs move into one `<details class="how">` after the Noise Reduction section; the sentence "Drag a curve point, or focus one and nudge it with the arrow keys" becomes a Shortcuts row. Review: the `X`/`K`/arrow sentence becomes a Shortcuts row.
- Produces: two new rows in `#shortcuts dl`: `<dt><kbd>X</kbd> <kbd>K</kbd> <kbd>↑</kbd> <kbd>↓</kbd></dt><dd>In Review, with a row's button focused: reject, keep, previous or next row</dd>` and `<dt><kbd>↑</kbd> <kbd>↓</kbd> <kbd>←</kbd> <kbd>→</kbd> on a curve point</dt><dd>Nudge the tone curve point</dd>`.

- [ ] **Step 1: Write the failing assertions**

Markup test `test_panels_lead_with_one_line_and_fold_the_rest`: for each tabpanel id, the first `p` child has class `lead` and its text equals the spec's line; exactly one `details.how` per panel except Clear's lead may stand alone if it has nothing to fold; no element with class `terms` exists; the Develop panel's `summary` list from the existing order test gains `"How this works"` at the end; the shortcuts `dl` contains "Nudge the tone curve point".

- [ ] **Step 2: Run and watch it fail**

- [ ] **Step 3: Edit the markup**

Move text; do not rewrite the folded paragraphs. CSS: `.lead { margin: .25rem 0 .5rem; color: var(--muted); }` and `.how summary { min-height: 44px; color: var(--cyan); font-weight: 700; cursor: pointer; }` (extend the existing `summary` rule at styles.css:188 instead of adding a second one).

- [ ] **Step 4: Run the suite**

Run: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_web -v`
Expected: PASS after updating the ingest/import copy assertions to look inside `details.how`.

- [ ] **Step 5: Commit**

```bash
git add src/lumen/static tests/test_web.py
git commit -m "feat: every panel leads with one line; explanations fold under How this works"
```

---

### Task 7: Rail and contact-sheet heading show what applies

**Files:**
- Modify: `src/lumen/static/index.html` (rail 33-43, heading 279-301)
- Modify: `src/lumen/static/app.js` (`updateWorkflow` lines for `#merge-batches`, `#merge-count`, `#open-compare`; refine handlers ~3337)
- Modify: `src/lumen/static/styles.css`
- Test: `tests/test_web.py` (`merge-batches` and compare scenarios)

**Interfaces:**
- Produces: `#merge-count` and `#merge-batches` `hidden` while `state.mergeSelection.size === 0`; `#jump-selected` `hidden` while `state.selectedGroupId === null`; `#back-to-top` unchanged.
- Produces: `#open-compare` `hidden` while `state.compare.length === 0`.
- Produces: `<details id="refine-fold"><summary>Refine previews</summary>…</details>` placed directly after `#filter-form` inside `.contact-top`, containing the `refine-scope` pair, `#refine-previews`, `#refine-availability`, `#refine-status`. Ids unchanged. `.view-controls` keeps only the Tiles pair.

- [ ] **Step 1: Write the failing assertions**

Scenario `rail-visibility`: after `load()` with no selection, `element('#merge-batches').hidden === true` and `element('#jump-selected').hidden === true`; tick one batch (`[data-merge-group]` change) and `#merge-batches` is shown but disabled; select a burst and `#jump-selected` shows; `#open-compare` hidden until a frame is marked for compare (`C`). Markup: `by_id["refine-previews"]` has `by_id["refine-fold"]` among parents, which has `by_id["filter-form"]` as a previous sibling (or simply: both are children of `.contact-top` and the fold comes after the form in document order).

- [ ] **Step 2: Run and watch it fail**

- [ ] **Step 3: Implement**

Three `hidden` assignments in `updateWorkflow`; move the refine controls; drop `#merge-batches, #jump-selected, #back-to-top { width: 100% }` to `.rail-header button { width: 100% }`.

- [ ] **Step 4: Run the suite**

Run: `node --check src/lumen/static/app.js && env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_web tests.test_cli -v`
Expected: PASS (`test_cli` mentions `refine-previews` in a doc string check; confirm it still finds the id).

- [ ] **Step 5: Commit**

```bash
git add src/lumen/static tests/test_web.py
git commit -m "feat: rail and heading controls appear only when they apply; refine previews folds under the filters"
```

---

### Task 8: AI button grammar, one availability line, frame-view hierarchy, one export button

**Files:**
- Modify: `src/lumen/static/index.html` (buttons listed below; loupe footer 635-641; remove `#loupe-export` from the Crop panel 497-499 and `#develop-export` 626; the seven `*-availability` paragraphs 77, 78, 84, 129, 151, 487, 538; add `<p id="frame-ai-availability" class="quiet"></p>` as the first child of `.frame-panel` in `#loupe`)
- Modify: `src/lumen/static/app.js` (`updateWorkflow` availability lines, `burstCritique`, `captionControls`, inspector `tag`, `selectFrameTab` ~2689 and `exportCurrentFrame` wiring, `#develop-export` listener ~3215)
- Modify: `src/lumen/static/styles.css`
- Test: `tests/test_web.py` (label assertions, `loupe-export-options` visibility test, `tag-keepers-availability` assertions ~1268)

**Interfaces:**
- Produces: every AI-driven button carries `class` containing `ai` and `aria-describedby="ai-availability"` (or `frame-ai-availability` inside `#loupe`). Labels exactly as the spec's list; counts keep the `(N keepers)` suffix pattern already built in `updateWorkflow`, e.g. `Tag keepers (3 keepers)`.
- Produces: CSS `button.ai::before { content: "✦ "; color: var(--cyan); }` (`button.ai:not(.secondary)::before` uses `var(--black)`).
- Produces: in the inspector, "Choose as best" has no `secondary` class; "Tag this frame" has `secondary`.
- Produces: one `#loupe-export` button plus `#loupe-export-options` in the loupe footer row, shown on every frame tab; `exportCurrentFrame()` is its only handler; `#develop-export` no longer exists.
- Produces: `#frame-ai-availability.textContent` mirrors `#ai-availability` in `updateWorkflow`.

- [ ] **Step 1: Write the failing assertions**

Markup test `test_ai_buttons_share_grammar_and_one_availability_line`: for ids `assist-keepers, tag-keepers, tag-all, scan-rejects, triage-astro, expand-search, ask-composition-ai, develop-ask, write-digest`: class includes `ai`, `aria-describedby` is `ai-availability`, and the static label (ignoring any `(…)` suffix) is the spec's; no element id ends with `-availability` except `ai-availability`, `frame-ai-availability`, `export-availability`, `refine-availability`, `astro-availability`; `develop-export` absent; exactly one `loupe-export` and it has `by_id["loupe"]` among parents and sits after `exif-panel`. Scenario `frame-view`: `element('#loupe-export-options').hidden === false` on the Frame tab; inspector `Choose as best` button has no `secondary` class; `Tag this frame` does.

- [ ] **Step 2: Run and watch it fail**

- [ ] **Step 3: Implement**

Rename labels in markup and in the three `textContent` assignments in `updateWorkflow`, the inspector's tag button, `burstCritique`'s `ask`, and `captionControls`'s `ask`. Delete the seven availability paragraphs and their `textContent` lines; add the two shared lines. Remove the per-tab `#loupe-export-options` toggle in `selectFrameTab`. Move the export button; delete the Develop listener.

- [ ] **Step 4: Run the suite**

Run: `node --check src/lumen/static/app.js && env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_web -v`
Expected: PASS after updating label assertions (grep tests for `local AI`, `Run all local AI`, `Ask local AI`, `Write caption with local AI`, `Add local AI tags`, `Expand with local AI`).

- [ ] **Step 5: Commit**

```bash
git add src/lumen/static tests/test_web.py
git commit -m "feat: AI buttons share one grammar and one availability line; the frame view has one export button"
```

---

### Task 9: Astro calibration grid

**Files:**
- Modify: `src/lumen/static/index.html` (152-172)
- Modify: `src/lumen/static/styles.css`
- Test: `tests/test_web.py` (astro markup test referencing `astro-darks`)

**Interfaces:**
- Produces: inside the calibration fieldset, three `<div class="calibration-row">` each holding `<p id="astro-<kind>-label">`, `<p id="astro-<kind>-path" class="path">`, `#browse-astro-<kind>`, `#reset-astro-<kind>` for kinds `darks`, `flats`, `biases`. Ids unchanged.
- Produces: CSS `.calibration-row { display: grid; grid-template-columns: 7rem minmax(0, 1fr) auto auto; align-items: center; gap: .5rem; margin: .35rem 0; }` with a 680 px rule collapsing it to one column.

- [ ] **Step 1: Write the failing assertion**

In the existing astro markup test: `by_id["browse-astro-darks"]`, `by_id["astro-darks-path"]` share a parent whose class is `calibration-row`; three such rows exist.

- [ ] **Step 2: Run and watch it fail**

- [ ] **Step 3: Restructure the markup and add the CSS**

- [ ] **Step 4: Run the suite**

Run: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_web tests.test_astro -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lumen/static tests/test_web.py
git commit -m "feat: astro calibration folders sit in one three-row grid"
```

---

### Task 10: Screenshots, docs and handoff

**Files:**
- Modify: `docs/HANDOFF.md` (new "Resume here" entry), `docs/GUIDE.md` and `README.md` (tab names, header/footer description), `AGENTS.md` ("Current tool state": the `/api/status` line about `omlx_roles` in the header, the `develop_advised` note now stores `rationale` and is read back, the crops payload `review`), `docs/BACKLOG.md` (remove the "Landing fold" entry).

- [ ] **Step 1: Full suite and syntax**

Run: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest discover -s tests -v 2>&1 | tail -5 && node --check src/lumen/static/app.js`
Expected: `OK`.

- [ ] **Step 2: Restart the server and screenshot**

Restart `lumen serve` (the Python changed in Tasks 1 and 2). Screenshot with the headless Chromium at 1440×1100 and 390×844 (`--headless --screenshot=<scratchpad>/after-<width>.png --window-size=<w>,<h> --virtual-time-budget=6000 http://127.0.0.1:8765/`) and read both: the contact sheet heading must be visible without scrolling at 1440 with frames present; no horizontal scroll at 390.

- [ ] **Step 3: Update the docs**

Grep `docs/ README.md AGENTS.md` for `Ingest tab`, `Import tab`, `Export tab`, `Review tab`, `job strip`, `Review model`, `Fast model`, `Tag keepers with local AI`, `Run all local AI` and fix each sentence to the new names. Add the handoff entry in the existing style (date, branch, suite count, what changed, what was verified in the browser, what was not).

- [ ] **Step 4: Commit**

```bash
git add docs AGENTS.md README.md
git commit -m "docs: UI clean-up round recorded; tab, strip and advice behaviour described"
```
