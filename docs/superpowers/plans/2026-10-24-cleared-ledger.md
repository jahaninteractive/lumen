# Cleared-Frame Ledger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remember cleared frames by fingerprint, skip them on import, and remove the ones already in the catalog in one action.

**Architecture:** One new catalog table written by the existing `_cleanup`, a seed routine over trash snapshots, a boolean threaded through import (library, job, web, CLI, dialog), and one new library action + route + Clear-tab control reusing `_cleanup`.

**Tech Stack:** Python stdlib sqlite3, Pillow unchanged, vanilla JS, unittest + Node scenarios.

**Spec:** `docs/superpowers/specs/2026-10-24-cleared-ledger-design.md`

## Global Constraints

- Pillow only; never touch camera originals; derived files stay in the library.
- Loopback server; every POST keeps host/origin checks and rejects unexpected JSON fields (400); new POST routes join the cross-origin and malformed-JSON regression lists in `tests/test_web.py`.
- No source paths or fingerprints in browser responses (counts only).
- Test command `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_<module> -v`; full suite before each commit; `node --check src/lumen/static/app.js` after JS edits.
- Commit per task with a conventional message.

## Review Focus

1. A trash snapshot with a `photos` table missing the `fingerprint` column or a corrupt file must be skipped and counted, not abort seeding (Task 1 test).
2. Importing with `include_cleared=True` must import the frame and still leave the ledger row intact (Task 2 test).
3. Removing cleared frames from a burst that also holds new frames must leave the survivors grouped with a valid `automatic_pick_id` (Task 3 test).
4. The Clear-tab count must be 0 and the button disabled when nothing matches, and must refresh after an import finishes (Task 3 scenario).
5. `_cleanup` ledger insert happens inside the same success path as today's deletions; a failed insert surfaces as a `CleanupError` like any other sqlite error (Task 1 test asserts rows exist after a normal cleanup; no new failure mode added).

---

### Task 1: Ledger table, cleanup writes, seed from trash

**Files:**
- Modify: `src/lumen/catalog.py` (table + `record_cleared(fingerprints: Iterable[str], cleared_at: str) -> int`, `cleared_fingerprints() -> set[str]`, `live_cleared_photo_ids() -> list[int]`)
- Modify: `src/lumen/library.py` (`_cleanup` records before deleting; new `seed_cleared_from_trash() -> dict`)
- Test: `tests/test_catalog.py`, `tests/test_library.py`

**Interfaces:**
- Produces: `Catalog.record_cleared(fingerprints, cleared_at) -> int` (rows newly inserted; `INSERT OR IGNORE`).
- Produces: `Catalog.cleared_fingerprints() -> set[str]`; `Catalog.live_cleared_photo_ids() -> list[int]` (photos whose fingerprint is in the ledger, ordered by id).
- Produces: `Library.seed_cleared_from_trash() -> dict` = `{"snapshots": n_read, "skipped": n_unreadable, "added": rows_inserted}`; reads `<trash>/*/catalog.sqlite3` read-only (`sqlite3.connect(f"file:{path}?mode=ro", uri=True)`), `cleared_at` = folder name up to the first `-` (e.g. `20261024T153936.918862Z`).

- [ ] **Step 1: Failing catalog tests**: `record_cleared(["a","b"], "t") == 2` then `record_cleared(["b","c"], "t") == 1`; `cleared_fingerprints() == {"a","b","c"}`; `live_cleared_photo_ids()` returns the ids of upserted photos whose fingerprint is `a`; `delete_photos` of those photos leaves the ledger untouched.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** in `catalog.py`. **Step 4: Run** → PASS.
- [ ] **Step 5: Failing library tests**: after `remove_group`, the group's fingerprints are in `cleared_fingerprints()`; after `clear_workspace`, all are. `seed_cleared_from_trash` over a temp library whose trash holds (a) a real snapshot made by `clear_workspace` in a second temp library copied in, (b) a folder with a zero-byte `catalog.sqlite3`, (c) a snapshot whose `photos` table lacks `fingerprint` → returns `{"snapshots": 1, "skipped": 2, "added": N}` and is idempotent (second call `added == 0`).
- [ ] **Step 6: Run** → FAIL. **Step 7: Implement.** `_cleanup`: compute `{photo.fingerprint for photo in photos if photo.id in selected}` and call `record_cleared(..., datetime.now(timezone.utc).isoformat())` right before the row deletions it already performs. **Step 8: Run** → PASS; full suite.
- [ ] **Step 9: Commit** `feat: cleared-frame ledger written by cleanup and seeded from trash snapshots`.

---

### Task 2: Import skips cleared frames unless included

**Files:**
- Modify: `src/lumen/library.py` (`ImportSummary.previously_cleared`, `_import_candidate(..., include_cleared=False)`, `import_path(requested, *, include_cleared=False)`, `start_import_job(requested, *, include_cleared=False)`; job manifest + result counts gain `previously_cleared`)
- Modify: `src/lumen/web.py` (`/api/import` accepts optional `include_cleared: bool`), `src/lumen/cli.py` (`import --include-cleared`; text output line), `src/lumen/static/index.html` (dialog checkbox), `src/lumen/static/app.js` (payload + `jobText`)
- Test: `tests/test_library.py`, `tests/test_web.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `Catalog.cleared_fingerprints()` from Task 1 (load once per import run, not per file).
- Produces: `ImportSummary.previously_cleared: int`; import job `result.previously_cleared`; web payload key `include_cleared` (must be `bool` if present; default False); CLI flag `--include-cleared`.
- Skip rule: a candidate whose fingerprint is in the ledger and is not already in the catalog at this path is counted `previously_cleared` and not imported (no row, no preview); `include_cleared=True` imports it normally. A photo already in the catalog at that path is handled by the existing unchanged-file path first.

- [ ] **Step 1: Failing library tests**: ledger holds fingerprint of `a.jpg`; `import_path(folder)` → `previously_cleared == 1`, `imported` counts only the others, no catalog row for `a.jpg`; `import_path(folder, include_cleared=True)` imports it and the ledger still contains the fingerprint (Review Focus 2); `start_import_job` result carries `previously_cleared`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Failing web + cli tests**: `POST /api/import {"path", "include_cleared": true}` reaches `start_import_job` with the flag (patch it); `"include_cleared": "yes"` → 400; CLI `lumen import <dir> --include-cleared --json` output has `previously_cleared`.
- [ ] **Step 6: Run** → FAIL. **Step 7: Implement.** **Step 8: Run** → PASS.
- [ ] **Step 9: UI.** Dialog: `<label class="toggle" for="import-include-cleared"><input type="checkbox" id="import-include-cleared"> Include frames cleared in earlier sessions</label>` under the path row with a `quiet` line "Frames you removed or cleared before are skipped unless ticked." `app.js` sends `include_cleared: true` only when ticked; `jobText` import summary appends `, N previously cleared` when `result.previously_cleared`. Node scenario `import-cleared` asserts the payload with and without the tick and the job text. Layout test required strings gain the label.
- [ ] **Step 10: Run** `node --check`, `tests.test_web`, full suite → PASS. **Step 11: Commit** `feat: import skips frames cleared in earlier sessions unless included`.

---

### Task 3: Remove previously cleared frames from the Clear tab, docs

**Files:**
- Modify: `src/lumen/library.py` (`cleared_in_catalog() -> dict`, `remove_cleared() -> CleanupResult`), `src/lumen/web.py` (`GET /api/library/cleared`, `POST /api/library/remove-cleared`), `src/lumen/cli.py` (`lumen cleared [--remove] [--json]`), `src/lumen/static/index.html`, `src/lumen/static/app.js`
- Modify docs: `README.md` (Ingest/Finish-and-clear sections), `docs/HANDOFF.md`, `AGENTS.md` one line
- Test: `tests/test_library.py`, `tests/test_web.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 1 `seed_cleared_from_trash`, `live_cleared_photo_ids`; `_cleanup(photo_ids, group_ids)`.
- Produces: `Library.cleared_in_catalog() -> dict` = `{"frames": int, "batches": int, "seed": {...}}` — seeds first (idempotent), then counts live matches and the bursts that would be emptied entirely.
- Produces: `Library.remove_cleared() -> CleanupResult`, `@_coordinated`, raises `ValueError("no previously cleared frames in the catalog")` when none; `group_ids` = groups whose every photo is selected; after cleanup, if any burst was only partly removed, re-run `_regroup_and_write_sidecars()` so survivors are re-ranked (Review Focus 3).
- Produces: `GET /api/library/cleared` → the dict (no fingerprints); `POST /api/library/remove-cleared {}` → same payload shape as `/api/groups/<id>/remove` (reuse the cleanup response/error helpers), 400 on non-empty body or when nothing matches.

- [ ] **Step 1: Failing library tests**: two bursts, one fully in the ledger, one half; `cleared_in_catalog()["frames"]` and `["batches"]` correct; `remove_cleared()` removes exactly those photos, the half burst keeps its survivors with a valid `automatic_pick_id`, the trash folder has a catalog snapshot, and a second `remove_cleared()` raises.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Failing web + cli tests**: GET shape; POST happy path through a real library; POST with `{"x":1}` → 400; routes in both regression lists; `lumen cleared --json` prints the counts and `--remove` performs it.
- [ ] **Step 6: Run** → FAIL. **Step 7: Implement.** **Step 8: Run** → PASS.
- [ ] **Step 9: UI.** Clear tab, new `cleanup-group` between "Selected batch" and "Whole workspace": `<h3>Previously cleared frames</h3><p id="cleared-count">Checking…</p><p class="quiet">Frames whose content you removed or cleared in an earlier session. Removing them here is recoverable like any batch removal; a frame you re-imported on purpose with "Include frames cleared in earlier sessions" counts too.</p><button type="button" id="remove-cleared" class="danger" disabled>Remove previously cleared frames</button>`. `app.js`: `loadCleared()` fetches the GET when the Clear tab is selected and after any job reaches a terminal state while that tab is selected; text `N frames in M batches` or `None`; button disabled when `frames === 0` or `state.busy`; click → existing `cleanup('/api/library/remove-cleared', {})` then `loadCleared()`. Node scenario `remove-cleared` asserts the count text, the disabled state at 0, the POST, and the refresh.
- [ ] **Step 10: Docs.** README: in "Ingest a card" (or the import section) two sentences on skipped cleared frames and the checkbox/flag; in "Finish and clear a shoot" the new control and `lumen cleared --remove`. HANDOFF "Resume here" block for this feature (branch name, what shipped, seeded-from-trash note). AGENTS.md tool-state: one line that cleanup writes `cleared_fingerprints` and import skips them.
- [ ] **Step 11: Run** `node --check`, full suite → PASS. **Step 12: Commit** `feat: remove previously cleared frames from the Clear tab; docs`.
