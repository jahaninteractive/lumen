# Workflow Round Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keyword hygiene, near-duplicate clusters inside bursts, resume-on-reload, a loupe filmstrip, AI-expanded search, a second Siril recipe, a web export preset with watermark, and thumbnail pruning.

**Architecture:** Every feature extends an existing seam: `analysis.measure_image` gains a hash, `grouping` gains a second pass, `vocabulary` gains a blocked list, `omlx.OmlxTagger` gains one text-only call, `astro.siril_script` gains a recipe, `pipelines` gains a preset and a watermark step, `app.js` gains small handlers with Node scenarios. No new modules, no new dependencies.

**Tech Stack:** Python 3.14 stdlib + Pillow, vanilla JS, unittest, Node for JS scenarios.

**Spec:** `docs/superpowers/specs/2026-10-24-workflow-round-design.md`

## Global Constraints

- Pillow is the only runtime Python dependency.
- Loopback-only server; every POST keeps host/origin checks and rejects unexpected JSON fields with a 400.
- Browser responses never carry source paths, fingerprints, API keys, or raw model output.
- Models advise only: no route or job may reject, pick, crop, exclude, or delete on a model's say-so.
- Test command: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_<module> -v`; full suite before each commit; `node --check src/lumen/static/app.js` after any JS change.
- Tests never contact oMLX: patch `lumen.library.get_server_status` / `lumen.library.OmlxTagger` as the existing tests do.
- Commit after each task with a conventional message; this folder is a Git repository on `main`.

## Review Focus

1. A burst whose frames all have `dhash = NULL` (catalog migrated before this change, no reimport) must stay one burst, never split and never crash. Pinned in Task 2.
2. Forgetting a word that is also in the packaged starter must still work and still block relearning; Task 1 tests a starter word.
3. `GroupFilters.text` of `" | "` or `"|"` (only separators) must behave like no text filter, not match everything with an empty LIKE. Pinned in Task 5.
4. A watermark string with only whitespace must be rejected at the route and the CLI, not drawn as nothing. Pinned in Task 7.
5. Loupe filmstrip at the first or last visible frame must stay put and announce nothing misleading; `[`/`]` must do nothing while Compare is open. Pinned in Task 4.

---

### Task 1: Keyword hygiene — blocked list, forget route, learned-keywords panel, species confirm

**Files:**
- Modify: `src/lumen/vocabulary.py`
- Modify: `src/lumen/library.py` (`add_suggested_keyword`, new `forget_keyword`, new `vocabulary_report`)
- Modify: `src/lumen/web.py` (`GET /api/vocabulary`, new `POST /api/vocabulary/forget`)
- Modify: `src/lumen/static/index.html` (Review tab), `src/lumen/static/app.js`, `src/lumen/static/styles.css` if needed
- Test: `tests/test_vocabulary.py`, `tests/test_library.py`, `tests/test_web.py`

**Interfaces:**
- Produces: `vocabulary.load_blocked(settings) -> list[str]`; `vocabulary.forget_keyword(settings, word: str) -> bool` (True when the word was in a category or newly blocked); `learn_keywords` skips blocked words (case-insensitive).
- Produces: `Library.vocabulary_report() -> dict` = `{"categories": {name: [word]}, "counts": {word: int}, "blocked": [word]}` where counts are photos whose `tags.data.subjects` contain the word (case-insensitive).
- Produces: `Library.forget_keyword(word: str) -> dict` = `{"word": str, "removed_from": int}`; `@_coordinated`; removes the word from `subjects` of every photo, appends event `keyword_forgotten {"word", "removed_from"}` with `photo_id=None`, rewrites the sidecar of each changed photo.
- Produces: `POST /api/vocabulary/forget {"word": str}` → 200 with the dict above; 400 on missing/blank/non-string word or extra fields.

- [ ] **Step 1: Failing vocabulary tests** in `tests/test_vocabulary.py`:

```python
def test_forget_keyword_blocks_relearning(self):
    settings = ...  # existing fixture pattern in this file
    load_vocabulary(settings)
    self.assertTrue(forget_keyword(settings, "Ice"))          # starter word, case-insensitive
    self.assertNotIn("ice", [w.casefold() for w in all_keywords(load_vocabulary(settings))])
    self.assertEqual(["ice"], load_blocked(settings))
    self.assertEqual([], learn_keywords(settings, {"learned": ["ICE"]}))
    self.assertFalse(forget_keyword(settings, "ice"))         # already blocked, nothing changed

def test_blocked_key_is_optional_and_schema_version_kept(self):
    ... assert json payload keeps "schema_version": 1 and "blocked" list after forget
```

- [ ] **Step 2: Run** `tests.test_vocabulary` → FAIL (ImportError).
- [ ] **Step 3: Implement** `load_blocked`, `forget_keyword`, and the blocked check in `learn_keywords` (`src/lumen/vocabulary.py`). `forget_keyword` writes atomically via `SidecarWriter._atomic_write` like `learn_keywords`; stores the casefolded word in `blocked`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Failing library tests** in `tests/test_library.py` (use the existing tagged-photo fixtures):

```python
def test_forget_keyword_strips_every_photo_and_rewrites_sidecars(self):
    # two photos with subjects ["ice", "bay"] and ["Ice"]
    result = library.forget_keyword("ice")
    self.assertEqual({"word": "ice", "removed_from": 2}, result)
    for photo_id in (a, b): self.assertNotIn("ice", library.catalog.get_tags(photo_id)["data"]["subjects"])
    # sidecar JSON for a no longer lists "ice"; event keyword_forgotten exists with photo_id None

def test_vocabulary_report_counts_photos_per_word(self):
    report = library.vocabulary_report()
    self.assertEqual(2, report["counts"]["ice"]); self.assertIn("blocked", report)

def test_add_suggested_species_learns_as_wildlife(self):
    # photo tags: species [{"name": "skua chick", "confidence": 0.4}], suggested ["skua chick"]
    library.add_suggested_keyword(photo_id, "skua chick")
    self.assertIn("skua chick", load_vocabulary(settings)["wildlife"])
```

- [ ] **Step 6: Run** → FAIL.
- [ ] **Step 7: Implement** in `library.py`. `add_suggested_keyword` picks category `"wildlife"` when the keyword casefold-matches a name in `tags.data.species`, else `"learned"`. `vocabulary_report` scans `catalog.list_photos()` + `get_tags` (ponytail: full scan per request, fine to ~10k photos).
- [ ] **Step 8: Run** → PASS.
- [ ] **Step 9: Failing web tests** in `tests/test_web.py`: `GET /api/vocabulary` includes `counts` and `blocked`; `POST /api/vocabulary/forget` happy path; add the route to the existing cross-origin and malformed-JSON regression lists; `{"word": "  "}` → 400; `{"word": "x", "extra": 1}` → 400.
- [ ] **Step 10: Run** → FAIL. **Step 11: Implement** the routes in `web.py` (strict `allowed = {"word"}` pattern as other routes). **Step 12: Run** → PASS.
- [ ] **Step 13: UI.** In the Review tab (`index.html` after `#reject-list`) add:

```html
<details id="vocabulary-panel"><summary>Learned keywords <span id="vocabulary-count" class="quiet"></span></summary>
  <p class="quiet">Words the local models added to your vocabulary. Forget removes a word from every frame and stops it being learned again.</p>
  <ul id="vocabulary-list" aria-label="Learned keywords"></ul>
</details>
```

In `app.js`: `loadVocabulary()` fetches `/api/vocabulary` when the Review tab is selected (same hook as `loadRejectSuggestions`) and on `toggle` of the details; renders one `<li>` per word of category `learned` plus any word in `wildlife` not in the packaged starter is NOT needed — list only `learned` (ponytail: one category; add wildlife when asked). Each item: `word (N frames)` + button **Forget** with `aria-label="Forget keyword <word>"`. Click → `post('/api/vocabulary/forget', {word})`, re-render, `announce('<word> forgotten; removed from N frames')`, and if `state.selectedId` is set re-run `inspect` for it. In `keywordLists`, for the suggested list drop the separate "Species:" text lines and instead append ` (species, 40%)` to the suggested item's label when the word matches a species name. Node scenario `vocabulary` in `test_web.py`: mock `/api/vocabulary` with two learned words and counts; assert two list items, click Forget on the first, assert the POST body and that the list re-renders with one item and the announcer text.

- [ ] **Step 14: Run** `node --check`, `tests.test_web`, then the full suite → PASS.
- [ ] **Step 15: Commit** `feat: learned-keyword panel with forget; species confirm learns as wildlife`.

---

### Task 2: Near-duplicate clusters inside bursts

**Files:**
- Modify: `src/lumen/analysis.py` (new `difference_hash`), `src/lumen/domain.py` (`PhotoMetrics.dhash`), `src/lumen/catalog.py` (migration + save/get), `src/lumen/config.py` (`near_duplicate_distance`), `src/lumen/grouping.py` (new `split_near_duplicates`), `src/lumen/library.py` (`_regroup_and_write_sidecars`)
- Test: `tests/test_analysis.py`, `tests/test_catalog.py`, `tests/test_grouping.py`, `tests/test_library.py`

**Interfaces:**
- Produces: `analysis.difference_hash(path: Path) -> str` — 16 lowercase hex chars from a 9×8 grayscale resize (`Image.Resampling.LANCZOS`), bit = left pixel > right pixel, row-major.
- Produces: `analysis.hamming(a: str, b: str) -> int`.
- Produces: `PhotoMetrics.dhash: str | None = None` (set by `measure_image`); `metrics.dhash TEXT` via `_MIGRATIONS["metrics"]`; `save_metrics` writes it, `get_metrics` reads it.
- Produces: `Settings.near_duplicate_distance: int = 12`, env `LUMEN_NEAR_DUP_DISTANCE` (int; invalid or negative → `ValueError` at `from_environment`).
- Produces: `grouping.split_near_duplicates(group: list[Photo], hashes: dict[int, str | None], max_distance: int) -> list[list[Photo]]` — keeps order; starts a new cluster when a frame's hash differs from the current cluster's first frame by more than `max_distance`; a frame with `None` hash never starts a split and joins the current cluster; `max_distance <= 0` returns `[group]`.

- [ ] **Step 1: Failing analysis tests** (`tests/test_analysis.py`): a flat grey image hashes to `"0000000000000000"`; an image with a left-to-right gradient hashes to all ones per row; `hamming("00ff", "0f0f") == 8`; `measure_image(path).dhash` is 16 hex chars.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** in `analysis.py`; set `dhash` inside `measure_image` reusing the already-opened grayscale (resize the bounded image, do not reopen). **Step 4: Run** → PASS.
- [ ] **Step 5: Failing catalog test**: `save_metrics` then `get_metrics` round-trips `dhash`; opening a catalog created without the column migrates it (`PRAGMA table_info(metrics)` contains `dhash`).
- [ ] **Step 6: Run** → FAIL. **Step 7: Implement** migration + SQL. **Step 8: Run** → PASS.
- [ ] **Step 9: Failing grouping tests** (`tests/test_grouping.py`):

```python
def test_split_near_duplicates_by_anchor_distance(self):
    photos = [p1, p2, p3, p4]  # ids 1..4, same camera, 1 s apart
    hashes = {1: "0"*16, 2: "0"*15 + "1", 3: "f"*16, 4: None}
    self.assertEqual([[p1, p2], [p3, p4]], split_near_duplicates(photos, hashes, 12))
    self.assertEqual([photos], split_near_duplicates(photos, hashes, 0))
    self.assertEqual([photos], split_near_duplicates(photos, {1: None, 2: None, 3: None, 4: None}, 12))  # Review Focus 1
```

- [ ] **Step 10: Run** → FAIL. **Step 11: Implement** in `grouping.py` (pure function; `hamming` imported from analysis). **Step 12: Run** → PASS.
- [ ] **Step 13: Failing library test** (`tests/test_library.py`): import three synthetic frames 1 s apart where the third is visually different (e.g. inverted gradient), assert two groups after import and that each group has its own `automatic_pick_id`; a second test sets `near_duplicate_distance=0` on the Settings and asserts one group. Also assert that a `manual_pick`/decision on a frame survives the split (set pick, reimport, check).
- [ ] **Step 14: Run** → FAIL. **Step 15: Implement** in `_regroup_and_write_sidecars`: after time grouping and merge handling, fetch metrics once per photo, build `hashes`, replace `groups` with `[cluster for g in groups for cluster in split_near_duplicates(g, hashes, self.settings.near_duplicate_distance)]`, then rank as today. Merged-in photos (`merged_into`) are appended after splitting so a manual merge is never split.
- [ ] **Step 16: Run full suite.** If an existing test builds a burst from deliberately different synthetic images and now expects one group, give that test's `Settings` `near_duplicate_distance=0` and say so in the commit body; do not lower the default.
- [ ] **Step 17: Commit** `feat: split time bursts into near-duplicate clusters with their own best pick`.

---

### Task 3: Resume the last selected frame on reload

**Files:**
- Modify: `src/lumen/static/app.js` (`inspect`, `load`)
- Test: `tests/test_web.py` (Node scenario `resume`)

**Interfaces:**
- Produces: `localStorage['lumen-selected']` = photo id string, written in `inspect`, removed when `state.selectedId` becomes null in `load`.

- [ ] **Step 1: Failing Node scenario `resume`**: seed `localStorage` with `lumen-selected = "2"` where photo 2 is the 250th frame of the mocked groups (beyond `PAGE`), call `load()`, assert `state.selectedId === 2`, `state.shown >= 250`, the frame card for 2 has `aria-pressed="true"` and `tabindex="0"`, and `/api/photos/2` was requested. Second branch: id not in groups → `state.selectedId === null` and the key is removed.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement**: in `load()`, when `state.selectedId === null` and no selection resolved, read the key; if the id is in `pagedPhotos()`, set `state.shown` to cover its index (round up to a multiple of `PAGE`), call `renderGroups()`, then `await inspect(photo)` and `focusFrame(id)`. Wrap storage access in try/catch like the other keys. **Step 4: Run** → PASS; `node --check`.
- [ ] **Step 5: Commit** `feat: reload re-selects the last inspected frame`.

---

### Task 4: Loupe filmstrip

**Files:**
- Modify: `src/lumen/static/app.js` (`handleCullingKey`, `openLoupe`, new `stepLoupe`), `src/lumen/static/index.html` (Shortcuts legend: `[` and `]`)
- Test: `tests/test_web.py` (Node scenario `loupe-strip`; the layout test's required-string list gains the new legend text)

**Interfaces:**
- Produces: `stepLoupe(delta: -1 | 1)` — moves to the adjacent id in `Array.from(document.querySelectorAll('.frame'))`, updates `#loupe-heading`, the viewport preview, `state.focusedId` via `focusFrame(id, false)`, `viewOrigin`, and announces `<name>, N of M`. At an edge it announces `First frame` / `Last frame` and does nothing else.

- [ ] **Step 1: Failing scenario `loupe-strip`**: open the loupe on frame 1 of 3, dispatch `]` twice and `[` once on the dialog → heading shows frame 2's name, `state.focusedId === 2`; dispatch `[` at frame 1 → announcer `First frame`; press `x` → the reject POST targets frame 2; close → focus lands on frame 2's card. Open Compare instead and dispatch `]` → `state.focusedId` unchanged (Review Focus 5).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement**: in `handleCullingKey`, before the arrow table, `if ((key === '[' || key === ']') && openView?.id === 'loupe') { event.preventDefault(); return stepLoupe(key === ']' ? 1 : -1); }`. Add `<kbd>[</kbd> <kbd>]</kbd> previous or next frame in the loupe` to the legend. **Step 4: Run** → PASS; `node --check`; `tests.test_web` layout test updated.
- [ ] **Step 5: Commit** `feat: loupe filmstrip with [ and ]`.

---

### Task 5: Search expansion with the fast model

**Files:**
- Modify: `src/lumen/catalog.py` (`GroupFilters.where`, `match_terms`), `src/lumen/omlx.py` (`parse_expand_response`, `OmlxTagger.expand_query`), `src/lumen/library.py` (`expand_search`), `src/lumen/web.py` (`POST /api/search/expand`), `src/lumen/static/index.html`, `src/lumen/static/app.js`
- Test: `tests/test_catalog.py`, `tests/test_omlx.py`, `tests/test_library.py`, `tests/test_web.py`

**Interfaces:**
- Produces: `GroupFilters.text` split on `|`; each non-blank term becomes its own LIKE pattern and the field conditions are ORed over all terms; blank-only text is treated as `None` in `__post_init__` (normalise with `object.__setattr__`). `match_terms` returns labels matching any term.
- Produces: `omlx.parse_expand_response(raw: str, vocabulary: dict[str, list[str]]) -> list[str]` — JSON `{"terms": [...]}`, keeps only strings that casefold-match a vocabulary word (vocabulary spelling), dedupes, max 8; unknown keys ignored; raises `ExpandResponseError(OmlxError)` when `terms` is missing or not a list.
- Produces: `OmlxTagger.expand_query(query: str, vocabulary, timeout=60) -> list[str]` — text-only `_complete` with the vocabulary as `system=` (same text as `tag`) and user text `Which of these keywords describe photos matching "<query>"? Answer {"terms": [...]} using only listed keywords.`, `max_tokens=200`.
- Produces: `Library.expand_search(query: str, tagger) -> dict` = `{"query", "terms": [...], "text": "q | t1 | t2"}`; `query` must be 1–80 chars after strip.
- Produces: `POST /api/search/expand {"query"}` → 200 dict above; 400 on bad query, extra fields, or an unloaded fast model (`tagger_for("fast")` refusal).

- [ ] **Step 1: Failing catalog tests**: `GroupFilters(text="heron | tern")` matches a photo with subjects `["tern"]`; `GroupFilters(text=" | ")` behaves like `text=None` (Review Focus 3: `where()` has no LIKE); `match_terms(id, "heron | tern")` returns the tern label.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Failing omlx tests**: parse keeps vocabulary spelling (`"Gentoo Penguin"` → `"gentoo penguin"`), drops unknown words, caps at 8, ignores extra keys, raises on `{"x": 1}`; `expand_query` sends a `system` message and text-only content (patch `_complete` as the existing tests do).
- [ ] **Step 6: Run** → FAIL. **Step 7: Implement.** **Step 8: Run** → PASS.
- [ ] **Step 9: Failing library + web tests**: `expand_search` builds `text` with the original query first and no duplicates; route happy path with a patched tagger; 400 for `{"query": ""}`, 81 chars, extra field; route added to the cross-origin and malformed-JSON regression lists; a `tagger_for` refusal → 400 and no job row.
- [ ] **Step 10: Run** → FAIL. **Step 11: Implement.** **Step 12: Run** → PASS.
- [ ] **Step 13: UI.** Next to `#filter-text` add `<button type="button" id="expand-search" class="secondary" aria-describedby="expand-search-availability">Expand with local AI</button><p id="expand-search-availability" class="quiet"></p>`. In `app.js`: button disabled with the fast-role reason via the same `roleUnavailable('fast')` pattern as `#tag-keepers`, and disabled when the field is blank; click → `aiCall('Search expansion', ...)` posting `{query}`, then set both `#filter-text` and `#find` to `result.text`, `applyFilters()`, announce `Expanded to N terms`. Node scenario `expand-search`: mock the route, click, assert URL `text=` param equals the joined text and the announcer line.
- [ ] **Step 14: Run** `node --check`, `tests.test_web`, full suite → PASS.
- [ ] **Step 15: Commit** `feat: expand tag search with the fast model; OR terms in the text filter`.

---

### Task 6: Astro recipe `untracked`

**Files:**
- Modify: `src/lumen/astro.py` (`siril_script`, `prepare_siril_job`, `AstroManifest`, `_manifest_payload`, `load_manifest`), `src/lumen/library.py` (`prepare_astro_job`), `src/lumen/web.py` (`_prepare_astro`), `src/lumen/cli.py` (`astro prepare --recipe`), `src/lumen/static/index.html`, `src/lumen/static/app.js`
- Test: `tests/test_astro.py`, `tests/test_web.py`, `tests/test_cli.py`

**Interfaces:**
- Produces: `RECIPES = ("tracked", "untracked")` in `astro.py`; `siril_script(siril, *, darks, flats, biases, recipe="tracked")`; `prepare_siril_job(..., recipe="tracked")`; `AstroManifest.recipe: str` (default `"tracked"` when absent from an older manifest.json); `Library.prepare_astro_job(..., recipe="tracked")`; web payload key `recipe` optional, must be in `RECIPES`.
- Untracked lines replace `register {sequence}` with `register {sequence} -2pass` and `seqapplyreg {sequence} -framing=min`; the stack line is unchanged.

- [ ] **Step 1: Verify the commands** against the vendored binary: `vendor/apps/Siril.app/Contents/MacOS/siril-cli -h` and Siril's command reference for 1.4 (`register … -2pass`, `seqapplyreg … -framing=`). Record the exact output lines proving both flags exist in the commit body. If either is absent, stop and report instead of guessing.
- [ ] **Step 2: Failing astro tests**: `siril_script(..., recipe="untracked")` contains the two lines above in that order before the stack line and `tracked` output is byte-identical to today; `recipe="x"` → `ValueError("unknown recipe")`; manifest round-trip stores `recipe`; an older manifest without the key loads as `tracked`.
- [ ] **Step 3: Run** → FAIL. **Step 4: Implement.** **Step 5: Run** → PASS.
- [ ] **Step 6: Failing web + cli tests**: `POST /api/astro/jobs` with `recipe: "untracked"` reaches `prepare_astro_job` with that recipe (patch like the existing prepare tests); `recipe: "fast"` → 400; CLI `lumen astro prepare <group> --recipe untracked --confirm` passes it through.
- [ ] **Step 7: Run** → FAIL. **Step 8: Implement.** **Step 9: Run** → PASS.
- [ ] **Step 10: UI.** In the Astro tab, above the confirm checkbox: `<fieldset id="astro-recipe"><legend>Recipe</legend><div class="scope-options"><label><input type="radio" name="astro-recipe" value="tracked" checked> Tracked (global star alignment)</label><label><input type="radio" name="astro-recipe" value="untracked"> Untracked wide-field (two-pass alignment, keeps the common area)</label></div></fieldset>`. `app.js` sends the checked value in the prepare payload; `setAstroJob` shows `Recipe: <name>` in `#astro-summary` when the job status carries it (add `recipe` to `job_status`'s whitelist for astro jobs). Node scenario `astro-recipe` asserts the payload.
- [ ] **Step 11: Run** `node --check`, full suite → PASS. **Step 12: Commit** `feat: untracked wide-field Siril recipe`.

---

### Task 7: Web export preset and watermark

**Files:**
- Modify: `src/lumen/pipelines.py` (`EXPORT_PRESETS`, export signature, watermark draw), `src/lumen/library.py` (`ExportRequest.watermark`, `_export_one`, `_prepare_export`), `src/lumen/web.py` (`/api/export`), `src/lumen/cli.py` (`export --preset web --watermark`), `src/lumen/static/index.html`, `src/lumen/static/app.js`
- Test: `tests/test_pipelines.py`, `tests/test_library.py`, `tests/test_web.py`, `tests/test_cli.py`

**Interfaces:**
- Produces: `EXPORT_PRESETS["web"] = {"long_edge": 1600, "quality": 82, "include_gps": False}`; output stem suffix `-web` follows the existing rule.
- Produces: `pipelines.validate_watermark(value) -> str | None` — `None`/`""` → `None`; must be `str`, stripped length 1–80, `str.isprintable()`; else `ValueError("watermark must be 1 to 80 printable characters")`.
- Produces: export function keyword `watermark: str | None = None`; drawn after resize with `ImageDraw.Draw(image).text(...)`, font `ImageFont.load_default(size=max(14, image.height // 40))`, fill white, `stroke_width=max(1, size // 12)`, `stroke_fill` black, anchor `"rd"` at `(width - inset, height - inset)` with `inset = round(0.02 * min(image.size))`; provenance/export record gains `"watermark": bool`.
- Produces: `ExportRequest.watermark: str | None = None`; web `allowed` sets gain `"watermark"`; CLI `--watermark TEXT`.

- [ ] **Step 1: Failing pipeline tests**: `web` preset output long edge is 1600 and has no GPS IFD; watermark changes bottom-right pixels of an otherwise flat mid-grey render and leaves the top-left untouched; export record has `watermark: true`; `validate_watermark("  ")` raises (Review Focus 4); `validate_watermark("x" * 81)` raises.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Failing library/web/cli tests**: `_prepare_export` rejects a bad watermark before creating a job row; route accepts `{"scope": "keepers", "preset": "web", "watermark": "© N. David"}` and a 400 message `preset must be full, share, or web` for `"preset": "x"`; CLI `export --keepers --preset web --watermark "x"` passes through.
- [ ] **Step 6: Run** → FAIL. **Step 7: Implement.** **Step 8: Run** → PASS.
- [ ] **Step 9: UI.** Third radio `Web (1600 px long edge, quality 82, no GPS)`; after the preset fieldset `<div><label for="export-watermark">Watermark text (optional)</label><input type="text" id="export-watermark" maxlength="80" autocomplete="off"></div>`; remembered in `localStorage['lumen-watermark']` with the same try/catch pattern as `lumen-xmp-profile`; included in the export payload when non-blank. Update the Export tab's intro sentence to mention Web. Node scenario `export-watermark` asserts the payload carries `preset: 'web'` and the watermark, and that a reload restores the field. Layout test's required-string list gains the new label.
- [ ] **Step 10: Run** `node --check`, full suite → PASS. **Step 11: Commit** `feat: web export preset with optional watermark`.

---

### Task 8: Thumbnail pruning

**Files:**
- Modify: `src/lumen/previews.py` (new `prune_thumbnails`), `src/lumen/library.py` (`_cleanup`, refine job completion)
- Test: `tests/test_previews.py`, `tests/test_library.py`

**Interfaces:**
- Produces: `previews.prune_thumbnails(cache_dir: Path, previews: Iterable[Path]) -> int` — keeps exactly the files named `f"{p.stem}-{p.stat().st_mtime_ns}.jpg"` for each existing preview `p`; unlinks every other `*.jpg` in `cache_dir`; returns the count removed; a missing `cache_dir` returns 0.

- [ ] **Step 1: Failing previews test**: with two previews, a live thumb for each, a stale-mtime thumb for one, and a thumb for a deleted preview → returns 2, the two live files remain. Missing dir → 0.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Failing library tests**: after `remove_group` the removed photo's thumb is gone; after a refine job (existing fake-darktable fixture) the embedded-preview thumb is gone and the refined one's thumb (if requested) survives. Prune is called with `settings.cache_dir / "thumbs"` and `{Path(p.preview_path) for p in catalog.list_photos() if p.preview_path}`, logged as `thumbnails pruned: N` on the job where one exists.
- [ ] **Step 6: Run** → FAIL. **Step 7: Implement.** **Step 8: Run** → PASS.
- [ ] **Step 9: Docs.** Remove the thumbnail-pruning entry from `docs/BACKLOG.md`; add one line to `docs/HANDOFF.md` "Resume here" per task shipped in this round (keep it to a bullet each) and the new env var/preset/recipe to `README.md` (Local AI, Export, Astro, Cull sections) and `AGENTS.md` tool-state list where relevant.
- [ ] **Step 10: Run** full suite → PASS. **Step 11: Commit** `feat: prune stale thumbnails after cleanup and refine; docs for the workflow round`.
