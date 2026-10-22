# Travel Volume, Culling, and Ingest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Lumen dependable for a three-week offline expedition: verified card ingest, per-frame decisions, background import/tag/ingest/refine jobs, keyboard culling, compare/loupe, filters with day structure, share-size exports, and darktable-refined previews.

**Architecture:** Every long operation becomes a `LocalJobRunner` job with a catalog row carrying `label/processed/total`; workers open their own `Catalog` connection and hold `Library._mutation()` only per item and at finalization so the UI stays responsive. Decisions and ratings live on `photos` and flow into ranking, keepers, sidecars, and JPEG export. New verified ingest lives in `ingest.py`; everything else extends existing modules. All catalog changes are additive `ALTER TABLE` migrations.

**Tech Stack:** Python 3.12+, SQLite, Pillow (only runtime dependency), darktable-cli, exiftool, standard-library threads/processes, vanilla HTML/CSS/JS, unittest with the Node `vm` harness in `tests/test_web.py`.

**Spec:** `docs/superpowers/specs/2026-10-22-travel-volume-culling-design.md`

## Global Constraints

- Camera originals are never edited, moved, renamed, or deleted; ingested copies are new originals the moment they are written and inherit the same rule. Memory cards are never written.
- No network access of any kind; no new runtime Python dependencies.
- Every new mutation route stays loopback-only with host/origin checks and strict JSON validation; responses never include source paths, mirror paths, fingerprints, API keys, or raw model output. Job labels are basenames only.
- Every new decision, job, ingest, and export is visible in provenance and both sidecars.
- All writes are atomic (temporary file plus rename) or explicitly resumable; a power cut mid-job leaves the catalog consistent and the job `interrupted` on restart (existing `recover_running_jobs`).
- One heavy job at a time: every job kind (`import`, `ingest`, `tag`, `refine_previews`, `astro`) goes through the single `LocalJobRunner` slot, so tag and astro can never overlap.
- After a successful ingest job, a catalog snapshot is written to `<primary destination>/lumen-catalog-backups/<UTC-timestamp>.sqlite3`.
- Derived files (manifests, logs, previews, sidecars, JPEGs, snapshots except the ingest backup above) stay inside the Lumen library.
- Tests generate their own small images and never need a personal RAW library; darktable/Siril paths use the fake-executable runner pattern from `tests/test_pipelines.py` / `tests/test_astro.py`.
- Accessibility stays intact: native controls, visible focus, 44 px targets, live announcements, reduced motion, mobile reflow, charcoal/amber/cyan palette, non-colour cue for every state; shortcuts never use macOS/Safari-reserved modifier combinations.
- **Keeper definition (user decision 2026-10-22, overrides the spec sentence "Single-frame groups are no longer keepers by default"):** a photo is a keeper when it is **not rejected** and is **either flagged or the best frame of its group**. A single frame is the best frame of its own group, so single frames remain keepers unless rejected. `keeper_ids()`, **All keepers** export, and the UI keeper count all use this definition, and the Export panel states it in plain words.
- Catalog additions are versioned, additive migrations: `photos.decision`, `photos.rating`, `photos.mirror_path`, `photos.preview_kind`; `jobs.label`, `jobs.processed`, `jobs.total`; `artifacts.settings_json` gains `preset`.
- Run the warning-strict suite with `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest discover -s tests -v` and `node --check src/lumen/static/app.js` before any task is called complete.
- Each task ends with an independent review (fresh reviewer, spec and this plan in hand) before the next task starts; findings are fixed before the task's commit.

## Review Focus

- Typing in the filter text box or the clear-confirmation input must never trigger a culling shortcut (`X` would reject the focused frame); Task 4 tests that keyboard handling ignores events whose target is an input, textarea, select, or dialog form control.
- A rerun after a power cut must ignore and remove a leftover `.lumen-partial` temporary beside a final path, never count it as `already_present`, and never leave it on disk; Task 3 tests this.
- Rejecting every frame of a burst leaves the burst with no keeper: **All keepers** skips it, the UI says so in words, and a manual pick on any rejected frame clears that rejection and restores a keeper; Task 2 tests this.
- A source file replaced on disk while an import job is between fingerprinting and preview creation is recorded as `source_replaced`/`import_failed` for that file without stopping the job or corrupting the group; Task 1 tests this.
- Filter queries with repeated parameters, `date_from` after `date_to`, non-integer ratings, or unknown decisions return a public 400 error and never a 500 or a path; Task 6 tests this.

---

### Task 1: Background jobs for import and tagging

**Files:**
- Modify: `src/lumen/domain.py` (`Job` gains `label`, `processed`, `total`)
- Modify: `src/lumen/catalog.py` (migration helper, `create_job`/`update_job` fields, `_job_from_row`)
- Modify: `src/lumen/jobs.py` (`JobContext.update_progress` label/counts)
- Modify: `src/lumen/library.py` (`start_import_job`, `start_tag_job`, `list_job_status`, generic `job_status`)
- Modify: `src/lumen/web.py` (`/api/import` returns a job, `/api/photos/tag-all`, `/api/jobs`, `/api/jobs/<id>`, `/api/jobs/<id>/cancel`)
- Modify: `src/lumen/cli.py` (`import` waits on the job, `tag --all --retag`, `jobs`, `cancel`)
- Modify: `src/lumen/static/index.html`, `styles.css`, `app.js` (job strip under the masthead)
- Test: `tests/test_catalog.py`, `tests/test_jobs.py`, `tests/test_library.py`, `tests/test_web.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `LocalJobRunner.submit/cancel/wait`, `Catalog.create_prepared_job`, `Library._import_one`, `Library._regroup_and_write_sidecars`, `Library.tag_photo`.
- Produces:
  - `Job(..., label: str | None = None, processed: int | None = None, total: int | None = None)`; `Catalog.update_job(job_id, *, status=None, progress=None, result_path=_UNCHANGED, error=_UNCHANGED, label=_UNCHANGED, processed=None, total=None)`; `Catalog._migrate()` that adds missing columns from a `{table: {column: ddl}}` map using `PRAGMA table_info` (reused by Tasks 2, 3, 8).
  - `JobContext.update_progress(progress: float, *, label: str | None = None, processed: int | None = None, total: int | None = None)`.
  - `Library.start_import_job(requested: Path) -> Job` (kind `import`, manifest `<library>/outputs/jobs/<id>/manifest.json` holding `{"kind","path"}`, log `<library>/outputs/jobs/<id>/job.log`).
  - `Library.start_tag_job(tagger, *, retag: bool = False) -> Job` (kind `tag`).
  - `Library.job_status(job_id, *, log_lines=40) -> dict` now returns `job_id, kind, status, progress, label, processed, total, error, result, log_tail, created_at, updated_at` for every kind and the astro counts only when `kind == "astro"`; `Library.list_job_status(limit: int = 50) -> list[dict]`.
  - Worker rule reused by Tasks 3 and 8: the work callable opens `with Library(self.settings, job_runner=self.job_runner) as worker:` (own SQLite connection, injected runner is not shut down on close), holds `worker._mutation()` per item, checks `context.cancelled` between items, and calls `worker._regroup_and_write_sidecars()` once under the lock at the end (also after cancellation, never after an exception).
  - Import skip rule: a candidate whose `photo_by_path` row has the same fingerprint and an existing preview file is counted as `skipped` and gets no preview, metrics, or event work.
  - Tag rule: without `retag`, photos with an existing `tags` row are skipped; a tag failure appends `tag_failed` (existing) and the job continues; the manifest records `{"kind":"tag","retag":bool,"failed_photo_ids":[...]}` at the end.
  - Web: `POST /api/import {"path"}` → 202 `job_status`; `POST /api/photos/tag-all {"retag"?: bool}` → 202; `GET /api/jobs` → `{"jobs":[...]}`; `GET /api/jobs/<id>`; `POST /api/jobs/<id>/cancel {}` → 200 status or 400 when terminal; a second heavy job → 409 `{"error": "another heavy job is already active"}`. `GET /api/astro/jobs/<id>` keeps working.
  - JS: `state.job`, `watchJob(job)`, `startJobPolling()/stopJobPolling()` generic over kind (the astro tab calls the same functions); `#job-strip` with `#job-label`, `#job-progress` (`<progress>`), `#job-status` (`role="status"`), `#cancel-job`; `state.busy` is true while a job is `queued`/`running`.

- [ ] Write failing tests: `test_catalog.test_migration_adds_job_label_and_counts_to_existing_database` (open a pre-migration SQLite file, assert columns appear once and data survives), `test_jobs.test_update_progress_records_label_and_counts`, `test_library.test_import_job_skips_cataloged_matching_photos_without_preview_work` (patch `ExifToolPreviewProvider.create_preview` and assert it is not called for the matching file), `test_library.test_import_job_cancel_between_files_regroups_and_keeps_imported_rows`, `test_library.test_import_job_records_source_replaced_mid_job_and_continues` (Review Focus), `test_library.test_import_job_label_is_basename_only`, `test_library.test_tag_job_skips_tagged_unless_retag_and_records_failures_without_stopping`, `test_library.test_interrupted_import_job_completes_on_rerun_of_same_folder` (mark the row `running`, construct a new runner, assert `interrupted`, rerun, assert all photos cataloged once), `test_web.test_import_returns_job_and_jobs_routes_never_expose_paths_or_fingerprints`, `test_web.test_tag_all_conflicts_with_active_job_and_cancel_is_same_origin_only`, `test_web.test_job_strip_has_progress_status_and_cancel_controls`, JS scenario `job-strip` (import submit → polls `/api/jobs/<id>` → actions disabled → completion announced → `load()` called once), `test_cli.test_import_waits_for_job_and_reports_counts`, `test_cli.test_tag_all_retag_jobs_and_cancel_commands`.
- [ ] Run `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_catalog tests.test_jobs tests.test_library tests.test_web tests.test_cli -v`; expect failures for missing columns, methods, routes, and controls.
- [ ] Implement: `Catalog._migrate()` called from `__init__` after `executescript`; `Job` fields with validation (`processed`/`total` non-negative ints, `processed <= total` when both set, `label` 1–200 chars); `JobContext.update_progress` passthrough; `start_import_job` (reject missing path/unsupported type before creating the job, exactly as `import_path` does today, then `create_prepared_job`, then `submit`); `start_tag_job`; `job_status`/`list_job_status`; web routes with the same allowed-field strictness as `_prepare_astro`; CLI `import` runs the job and `wait_for_job` then prints `job_status`; `jobs`/`cancel` subcommands; job strip markup, CSS (44 px cancel target, non-colour status words), and `watchJob` polling only while `queued`/`running`, announcing start/success/failure/cancel transitions, refreshing groups when a job ends.
- [ ] Run the focused tests above, then the full warning-strict suite and `node --check src/lumen/static/app.js`; expect PASS with no leaked thread/process warnings.
- [ ] Independent review of Task 1 against the spec's feature 3, the Global Constraints, and the Review Focus line it owns; fix findings.
- [ ] Commit: `feat: run import and tagging as background jobs`.

### Task 2: Decisions, ratings, and keeper refinement

**Files:**
- Modify: `src/lumen/domain.py` (`DECISIONS`, `Photo.decision`, `Photo.rating`)
- Modify: `src/lumen/catalog.py` (migration, `set_decision`, `set_rating`, upsert preserves both, `list_groups` rows carry both, `rerank_group`)
- Modify: `src/lumen/library.py` (`keeper_ids`, `set_decision`, `set_rating`, `set_pick` clears rejection, ranking skips rejected frames, export rating)
- Modify: `src/lumen/scoring.py` (no change to weights; `rank_group` unchanged — selection logic lives in library)
- Modify: `src/lumen/sidecars.py` (`decision`/`rating` in JSON, `xmp:Rating` -1 for rejected)
- Modify: `src/lumen/web.py` (`/api/photos/<id>/decision`, `/api/photos/<id>/rating`, groups payload)
- Modify: `src/lumen/cli.py` (`decide`, `rate`)
- Modify: `src/lumen/static/index.html`, `styles.css`, `app.js` (inspector decision/rating controls, frame marks, hide rejected toggle, keeper copy on Export panel)
- Test: `tests/test_catalog.py`, `tests/test_library.py`, `tests/test_sidecars.py`, `tests/test_web.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 1 job strip (`state.busy`), `Catalog.set_manual_pick`, `rank_group`.
- Produces:
  - `DECISIONS = frozenset({"none", "flag", "reject"})`; `Photo(..., decision: str = "none", rating: int = 0)`.
  - `Catalog.set_decision(photo_id, decision) -> Photo` and `Catalog.set_rating(photo_id, rating) -> Photo`, each validating input, appending `decision_changed` / `rating_changed` events with `{"previous", "next"}`; `Catalog.rerank_group(group_id, automatic_pick_id, scores)` updating one group's pick without touching others.
  - Selection rule used by `_regroup_and_write_sidecars` and by `Library._rerank_group(group_id)`: manual non-rejected pick, else highest-ranked non-rejected frame, else highest-ranked frame (so `automatic_pick_id` is never NULL for a non-empty group).
  - `Library.keeper_ids() -> list[int]`: `decision != "reject" and (decision == "flag" or id == automatic_pick_id)`, over every photo in `list_groups()`.
  - `Library.set_decision(photo_id, decision) -> dict` and `Library.set_rating(photo_id, rating) -> dict` returning `{"photo_id", "decision", "rating", "group_id", "automatic_pick_id"}`; rejecting the current pick reranks that group; `set_pick` on a rejected frame first sets decision `none` (event `decision_changed` with `"reason": "manual_pick"`).
  - `SidecarWriter.write(..., decision: str = "none", rating: int = 0)`: JSON `"decision"`, `"rating"`; XMP `xmp:Rating` is `-1` when rejected, else `rating`. The old "5 if selected" rule is gone from sidecars; JPEG export passes `rating=photo.rating` (EXIF 18246 gets `0` for rejected because the tag is unsigned; the embedded XMP keeps `-1`).
  - Groups payload photos carry `decision` and `rating`; `/api/groups` hides rejected photos unless `?show_rejected=1` (Task 6 folds this into the filter set) and reports `rejected_count`.
  - Web: `POST /api/photos/<id>/decision {"decision": "none"|"flag"|"reject"}`, `POST /api/photos/<id>/rating {"rating": 0..5}` (`bool` rejected); 400 with a public message for unknown photos.
  - CLI: `lumen decide <photo-id> --flag|--reject|--clear [--json]`, `lumen rate <photo-id> <0-5> [--json]`.
  - UI: inspector buttons `Flag`, `Reject`, `Clear decision`, and a labelled `<select id="rating">` 0–5; frame cards show text marks `Flagged`, `Rejected`, `Rating 3`; `#show-rejected` checkbox; Export panel copy: "Keepers are frames that are not rejected and are either flagged or best in their burst. A single frame is best in its own burst, so it stays a keeper unless you reject it."; keeper count derived from `keeper_ids` semantics on the client.

- [ ] Write failing tests: `test_catalog.test_migration_adds_decision_and_rating_and_upsert_preserves_them`, `test_catalog.test_set_decision_and_rating_validate_and_record_events`, `test_library.test_keepers_are_unrejected_flagged_or_group_best_including_single_frames` (single frame keeper; rejected single frame not a keeper; flagged alternate is a keeper beside the pick), `test_library.test_rejecting_the_pick_promotes_next_unrejected_frame_and_manual_pick_clears_rejection`, `test_library.test_all_rejected_burst_has_no_keeper_and_pick_restores_one` (Review Focus), `test_library.test_keepers_export_uses_new_definition_and_stored_rating`, `test_sidecars.test_sidecars_carry_decision_and_minus_one_xmp_rating_for_rejected`, `test_web.test_decision_and_rating_routes_validate_strictly_and_hide_rejected_by_default`, `test_web.test_export_panel_states_keeper_definition_and_inspector_has_native_decision_controls`, JS scenario `decisions` (reject from inspector updates the card in place without `load()`, keeper count text updates), `test_cli.test_decide_and_rate_commands_persist_and_report_json`.
- [ ] Run the focused modules; expect failures for missing columns, methods, routes, and copy.
- [ ] Implement the catalog columns and setters, selection rule, `keeper_ids`, library setters with sidecar rewrites for the affected group, sidecar fields, web routes (strict `set(payload) == {"decision"}` / `{"rating"}`), CLI commands, and UI controls with announcements ("DSC01234 rejected", "Rating 3 set for DSC01234").
- [ ] Run focused tests, the full warning-strict suite, and `node --check`; expect PASS.
- [ ] Independent review of Task 2 against feature 2, the keeper definition in Global Constraints, and its Review Focus line; fix findings.
- [ ] Commit: `feat: add reject, flag, rating, and keeper refinement`.

### Task 3: Verified card ingest

**Files:**
- Create: `src/lumen/ingest.py`
- Modify: `src/lumen/domain.py` (`Photo.mirror_path`)
- Modify: `src/lumen/catalog.py` (migration `photos.mirror_path`, `set_mirror_path`)
- Modify: `src/lumen/library.py` (`start_ingest_job`, mirror path in sidecars, snapshot)
- Modify: `src/lumen/sidecars.py` (`source.mirror_path`)
- Modify: `src/lumen/web.py` (`/api/ingest`, `ingest_output_dir` in `/api/status`, strip `mirror_path`)
- Modify: `src/lumen/cli.py` (`ingest`)
- Modify: `src/lumen/static/index.html`, `styles.css`, `app.js` (Ingest tab)
- Create: `tests/test_ingest.py`
- Test: `tests/test_catalog.py`, `tests/test_library.py`, `tests/test_web.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 1 job runner/worker rule and job strip; `read_metadata`, `fingerprint_file`, `SidecarWriter._atomic_write`, `Catalog.backup`.
- Produces:
  - `ingest.IngestItem(relative: str, source: Path, size: int, captured_date: str, destinations: tuple[Path, ...])` and `ingest.plan_ingest(card: Path, destinations: Sequence[Path], *, library_dir: Path, today: date) -> list[IngestItem]`: walks supported files under the card (no symlink following; rejects a card or destination inside the library, a destination inside the card or the card inside a destination, duplicate destinations, and a missing card), date from `read_metadata` → file mtime → `today`, destination `<dest>/<YYYY-MM-DD>/<card-relative path>`.
  - `ingest.copy_verified(source: Path, destination: Path, *, chunk_size: int = 1 << 20, hasher=hashlib.sha256) -> str`: streams to `destination.with_name(f".{destination.name}.lumen-partial")`, fsyncs, re-reads the temporary to compare SHA-256 with the source hash computed during the copy, renames into place, returns the hash; on mismatch deletes the temporary and retries once, then raises `IngestVerifyError`; never opens the source for writing.
  - `ingest.classify_existing(destination: Path, source_hash: str) -> str | None`: returns `"already_present"` when the final file exists with a matching hash, `None` otherwise; a leftover `.lumen-partial` beside the final path is removed and ignored (Review Focus).
  - `ingest.write_manifest(path: Path, manifest: dict)` using the atomic write; manifest schema `{"schema_version": 1, "card", "destinations", "started_at", "finished_at", "files": [{"relative", "size", "sha256", "captured_date", "outcomes": {"<destination>": "copied"|"already_present"|"failed"}, "error"?}], "summary": {"copied", "verified", "already_present", "failed", "card_verified_everywhere": bool}}`.
  - `Library.start_ingest_job(card: Path, primary: Path, mirror: Path | None = None) -> Job` (kind `ingest`, manifest at `<library>/outputs/ingest/<id>/manifest.json`, log `<library>/outputs/ingest/<id>/ingest.log`): the worker copies each item to primary then mirror, updates progress with the basename label, checks cancellation between files, writes the manifest after every file (resumable), and only when every primary copy is `copied` or `already_present` imports the primary copies with the Task 1 per-file import rule (progress continues over the same job), records `mirror_path` per photo, regroups once, then writes the catalog snapshot to `<primary>/lumen-catalog-backups/<YYYYMMDDTHHMMSSZ>.sqlite3` and sets `result_path` to the manifest path.
  - `Catalog.set_mirror_path(photo_id, mirror_path)`; `Photo.mirror_path: str | None = None` preserved by upsert; sidecar JSON `source.mirror_path`.
  - `job_status` for `ingest` adds `"ingest": {"copied", "verified", "already_present", "failed", "card_verified_everywhere", "manifest": "manifest.json", "snapshot": "<name or null>"}`; the browser composes the exact manifest path from `ingest_output_dir` in `/api/status`, as the astro tab does.
  - Web: `POST /api/ingest {"card", "primary", "mirror"?: str|null}` → 202; CLI `lumen ingest <card> --to <primary> [--mirror <secondary>] [--json]` runs and waits, exit 0 only when `card_verified_everywhere`.
  - UI: Ingest tab with three labelled path rows plus `Choose…` buttons reusing `/api/browse`, `Start ingest`, result counts, the manifest path, and one of two sentences: "Every file verified on every destination." or "Do not format the card: N files were not verified." Never claims the card is safe otherwise.

- [ ] Write failing tests in `tests/test_ingest.py`: `test_plan_rejects_library_nested_and_duplicate_destinations_before_copying`, `test_plan_uses_exif_date_then_mtime_then_today`, `test_copy_verified_writes_partial_fsyncs_and_renames_into_place`, `test_copy_verified_retries_once_then_fails_without_final_file` (patch the hasher to return a wrong digest on the first attempt, then on both attempts), `test_leftover_partial_is_removed_and_never_counted_as_present` (Review Focus), `test_rerun_reports_already_present_and_copies_only_missing_files`, `test_card_bytes_mtimes_and_listing_are_unchanged_after_ingest`; in `test_library.py`: `test_ingest_job_imports_primary_copies_not_card_and_records_mirror_path`, `test_ingest_job_cancel_between_files_leaves_no_partial_or_final_half_file`, `test_ingest_job_mirror_failure_blocks_card_verified_everywhere_but_still_imports_primary`, `test_ingest_job_writes_catalog_snapshot_beside_primary_copies`, `test_ingest_job_with_primary_failure_skips_import_and_snapshot`; in `test_web.py`: `test_ingest_route_validates_fields_and_never_returns_paths_or_fingerprints`, `test_ingest_panel_has_labeled_pickers_and_honest_format_copy`, JS scenario `ingest` (start → poll → result text with counts and manifest path); in `test_cli.py`: `test_ingest_command_exit_code_follows_verification`.
- [ ] Run `tests.test_ingest tests.test_library tests.test_web tests.test_cli`; expect missing module and routes.
- [ ] Implement `ingest.py` with `os.fsync` on the file and on the destination directory, the library worker, catalog column, sidecar field, status payload, web route, CLI command, and the Ingest tab (reusing `watchJob`).
- [ ] Run focused tests, the full warning-strict suite, and `node --check`; expect PASS. Additionally run one manual ingest of a scratch folder of generated JPEGs to two temporary folders and confirm the snapshot and manifest appear where documented.
- [ ] Independent review of Task 3 against feature 1, the atomic/resumable rule, the snapshot rule, and its Review Focus line; fix findings.
- [ ] Commit: `feat: add verified card ingest`.

### Task 4: Keyboard culling

**Files:**
- Modify: `src/lumen/static/index.html` (Shortcuts button, `<dialog id="shortcuts">` with a `<dl>` legend)
- Modify: `src/lumen/static/styles.css` (roving focus ring, dialog)
- Modify: `src/lumen/static/app.js` (`handleCullingKey`, roving tabindex, in-place card updates)
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: Task 2 decision/rating routes and in-place card rendering, `/api/photos/<id>/pick`.
- Produces:
  - `CULLING_KEYS = {p: 'pick', x: 'reject', f: 'flag', u: 'clear', c: 'compare', Enter: 'loupe', Escape: 'close'}` plus digits `0`–`5` for rating and `ArrowLeft/Right/Up/Down`, `Home`, `End`, `PageUp`, `PageDown` for movement; `sheetColumns()` returns the grid column count from `getComputedStyle` when available, else 1.
  - `handleCullingKey(event)` attached to `#contact-sheet` (and, in Task 5, to the compare dialog): returns without action when `event.metaKey || event.ctrlKey || event.altKey`, or when `event.target` is an `input`, `textarea`, `select`, or `button` inside a dialog form (Review Focus); otherwise `preventDefault` and dispatches.
  - `renderFrame(photo, group)` builds one frame button; `refreshFrame(photoId)` replaces the existing node in place; `state.focusedId` with roving `tabindex` (`0` on the focused frame, `-1` elsewhere).
  - `state.compare: number[]` is populated by `C` here (max two, oldest replaced, announced) so Task 5 only adds the view.
  - `#shortcuts-button` opens `#shortcuts` with `showModal()` when available (the Node harness lacks it; guard and set `hidden=false` instead); Escape closes it.

- [ ] Write failing tests: `test_web.test_index_has_shortcuts_button_and_readable_legend_without_reserved_modifiers` (parse the `<dl>`, assert each listed key is a single key or digit and none contains "Cmd", "Ctrl", "Option"), JS scenario `culling-keys` (synthetic keydown events: ArrowRight moves focus and `tabindex`, `x` posts `reject` and the card text gains "Rejected" without `load()`, `3` posts rating 3, `p` posts pick and re-renders the group's "Best" mark, `c` twice marks two frames and the third press replaces the oldest, Home/End jump, `Meta+x` does nothing, keydown whose target is the filter/clear text input does nothing).
- [ ] Run `tests.test_web`; expect failures for the missing button, dialog, and handler.
- [ ] Implement the handler, roving focus, in-place updates, announcements through `announce()`, and the legend.
- [ ] Run `tests.test_web`, the full warning-strict suite, and `node --check`; expect PASS.
- [ ] Independent review of Task 4 against feature 5, the accessibility rule, and its Review Focus line; fix findings.
- [ ] Commit: `feat: add keyboard culling shortcuts`.

### Task 5: Compare and loupe

**Files:**
- Modify: `src/lumen/static/index.html` (`<dialog id="compare">`, `<dialog id="loupe">`, `#open-compare` button in the contact-sheet heading)
- Modify: `src/lumen/static/styles.css` (two-pane grid, stacked under 680 px, 100 percent `img` inside a scrollable viewport, reduced motion)
- Modify: `src/lumen/static/app.js` (`openCompare`, `openLoupe`, synced panning, actions)
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `state.compare` and `handleCullingKey` from Task 4, `/api/photos/<id>` (metrics), groups payload (`decision`, `rating`, `name`, `total_score`, `preview_url`).
- Produces:
  - `openCompare()` fills `#compare-left` and `#compare-right` panes, each with a `<figure>`: `.loupe-viewport` (scrollable, `tabindex="0"`, `aria-label="<name> viewport"`), `<img>` at its stored pixel size (`width`/`height` from `naturalWidth` when loaded; both panes use scale 1 so zoom matches), and a `<dl>` listing name, overall score, sharpness, exposure, eye quality when present, decision, rating.
  - Synced panning: a `scroll` listener on each viewport copies `scrollLeft/scrollTop` as fractions of scrollable range to the other; arrow keys inside a viewport scroll by 40 px; `syncing` flag prevents feedback loops.
  - Pane actions: `Pick as best`, `Flag`, `Reject`, rating `<select>`, `Swap sides`, `Close`; each decision reuses Task 2 routes and `refreshFrame` so the contact sheet stays current without reload.
  - `openLoupe(photoId)` shows one viewport at 100 percent; `Enter` on a frame opens it, `Escape` closes; focus returns to the originating frame on close.
  - Both dialogs use the stored preview URL (`preview_url` from the groups payload), so Task 8's refined previews appear automatically through the version query.

- [ ] Write failing tests: `test_web.test_compare_and_loupe_dialogs_have_labeled_viewports_and_actions` (HTML parse), JS scenario `compare` (mark two frames, `#open-compare` enabled, open → both panes list name/score/decision/rating, set left `scrollTop` and dispatch `scroll` → right matches, `Swap sides` exchanges pane content, `Reject` from the right pane posts and updates both the pane and the sheet card, `Escape` closes and focus returns), JS scenario `loupe` (Enter opens the single viewport for the focused frame, arrow pans, Escape closes).
- [ ] Run `tests.test_web`; expect missing dialogs and functions.
- [ ] Implement markup, CSS (stack under 680 px, `prefers-reduced-motion` disables smooth scroll), and JS.
- [ ] Run `tests.test_web`, the full warning-strict suite, and `node --check`; expect PASS.
- [ ] Independent review of Task 5 against feature 4 and the accessibility rule; fix findings.
- [ ] Commit: `feat: add compare view and loupe`.

### Task 6: Filters and day structure

**Files:**
- Modify: `src/lumen/catalog.py` (`GroupFilters`, `list_groups(filters)`, `group_counts`)
- Modify: `src/lumen/library.py` (`list_groups(filters=None)`, `cameras()`)
- Modify: `src/lumen/web.py` (`parse_group_filters`, `/api/groups?...`)
- Modify: `src/lumen/cli.py` (`groups` filter flags)
- Modify: `src/lumen/static/index.html`, `styles.css`, `app.js` (filter form, day headings, paging)
- Test: `tests/test_catalog.py`, `tests/test_web.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 2 `decision`/`rating`, keeper rule, tags table.
- Produces:
  - `GroupFilters(date_from: str | None, date_to: str | None, camera: str | None, decision: str = "any", min_rating: int = 0, keepers_only: bool = False, tagged_only: bool = False, text: str | None = None, show_rejected: bool = False)` (frozen dataclass in `catalog.py`; `decision` in `{"any","flag","reject","none"}`; dates are `YYYY-MM-DD` validated with `date.fromisoformat`; `date_from <= date_to`).
  - `Catalog.list_groups(filters: GroupFilters | None = None) -> list[dict]` applies all conditions with AND in SQL (keepers: `decision != 'reject' AND (decision = 'flag' OR p.id = g.automatic_pick_id)`; text: case-insensitive `LIKE` over `json_extract(tags.data_json, '$.description')` and `'$.subjects'`; rejected hidden unless `show_rejected` or `decision == "reject"`); groups keep only matching photos and omit empty groups; `Catalog.group_counts(filters) -> {"filtered": int, "total": int}`.
  - `web.parse_group_filters(query: str) -> GroupFilters` via `urllib.parse.parse_qs(..., keep_blank_values=False)`, rejecting unknown keys, repeated keys, and bad values with `ValueError("invalid filter: <name>")` → 400 public JSON (Review Focus); the existing Task 2 `show_rejected` parameter becomes one of these filters.
  - `GET /api/groups` response adds `"counts": {"filtered", "total"}` and `"cameras": [...]`.
  - CLI `groups --from --to --camera --decision --min-rating --keepers --tagged --text --show-rejected`.
  - UI: `<form id="filter-form">` with native `type="date"` inputs, camera `<select>`, decision `<select>`, min-rating `<select>`, three checkboxes, a text input, `Apply` and `Reset`; `applyFilters()` writes the query with `history.replaceState` and `load()` reads `location.search` (the Node harness gains `location`/`history` stubs in the test file); batch rail renders `<details open>` per capture date with a `<summary>` "2026-10-22 · 340 frames · 12 keepers"; contact sheet renders 200 frames per page with `#show-more` ("Show 200 more · 540 remaining") and lazy `img` loading.

- [ ] Write failing tests: `test_catalog.test_group_filters_each_condition_and_combination_with_counts`, `test_catalog.test_keepers_only_filter_matches_library_keeper_definition`, `test_web.test_groups_filters_round_trip_and_reject_invalid_repeated_or_inverted_params_publicly` (Review Focus), `test_web.test_filter_form_and_day_headings_are_native_and_labeled`, JS scenario `filters` (submit form → URL query → `/api/groups?` request → rail day headings with counts → show-more paging reveals the next 200 → reload keeps state), `test_cli.test_groups_filter_flags_narrow_output`.
- [ ] Run focused modules; expect missing dataclass, parser, and controls.
- [ ] Implement SQL filters with parameters only (never string-format values), parser, routes, CLI flags, form, day structure, and paging.
- [ ] Run focused tests, the full warning-strict suite, and `node --check`; expect PASS.
- [ ] Independent review of Task 6 against feature 7 and its Review Focus line; fix findings.
- [ ] Commit: `feat: add filters, day structure, and paged contact sheet`.

### Task 7: Export presets

**Files:**
- Modify: `src/lumen/pipelines.py` (`export_jpeg(..., preset="full")`)
- Modify: `src/lumen/library.py` (`ExportRequest.preset`, settings/provenance)
- Modify: `src/lumen/web.py` (`/api/export` accepts `preset`)
- Modify: `src/lumen/cli.py` (`export --preset`)
- Modify: `src/lumen/static/index.html`, `app.js` (preset radio group)
- Test: `tests/test_pipelines.py`, `tests/test_library.py`, `tests/test_web.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: existing `DarktablePipeline.export_jpeg`, `fingerprint_file`, keeper rule from Task 2.
- Produces:
  - `EXPORT_PRESETS = {"full": {"long_edge": None, "quality": 92, "include_gps": True}, "share": {"long_edge": 2048, "quality": 85, "include_gps": False}}` in `pipelines.py`; `export_jpeg(..., preset: str = "full")` validates the name, applies the active crop first, then `image.thumbnail((2048, 2048), Image.Resampling.LANCZOS)` for `share` on the darktable render in Pillow (no second darktable run), writes with the preset quality, records `settings["preset"]`, `settings["quality"]`, `settings["long_edge"]`, `settings["include_gps"]`, and for `share` drops the GPS IFD (`34853`) while keeping every other metadata rule, including the MakerNote copy.
  - **Builder call to confirm:** the spec says both presets omit GPS, but `full` is defined as current behaviour and the user decided on 2026-10-22 that JPEG export keeps GPS. This plan keeps GPS for `full` and omits it for `share`; the Export panel states that difference. Change here if the user wants `full` to drop GPS too.
  - Filenames `<stem>-lumen.jpg` / `<stem>-lumen-share.jpg`, collision suffix `<stem>-lumen-share-<n>.jpg`.
  - `ExportRequest(photo_ids, profile=None, preset="full")`; `/api/export` allowed fields gain `preset` (string in the preset set); CLI `export --preset {full,share}`; the `exported` provenance event and artifact settings carry `preset`.
  - UI: `<fieldset>` "Size" with radios `Full resolution (quality 92, keeps GPS)` and `Share (2048 px long edge, quality 85, no GPS)`; export status names the preset.

- [ ] Write failing tests: `test_pipelines.test_share_preset_resizes_render_to_2048_long_edge_at_quality_85_without_gps` (fake darktable runner writes a 4000×2600 TIFF with an sRGB profile and a GPS IFD; assert output long edge 2048, GPS absent, description/keywords/rating kept, `settings["preset"] == "share"`), `test_pipelines.test_full_preset_is_unchanged_and_keeps_gps`, `test_pipelines.test_share_filenames_use_share_suffix_and_collision_numbering`, `test_pipelines.test_share_applies_crop_before_resize`, `test_library.test_export_records_preset_in_artifact_settings_sidecar_and_event`, `test_web.test_export_accepts_known_presets_only_and_ui_has_labeled_preset_radios`, `test_cli.test_export_preset_flag`.
- [ ] Run focused modules; expect unknown keyword/flag.
- [ ] Implement the preset table, resize path, naming, request field, routes, CLI flag, and radios.
- [ ] Run focused tests, the full warning-strict suite, and `node --check`; expect PASS.
- [ ] Independent review of Task 7 against feature 6 and the metadata rules; fix findings.
- [ ] Commit: `feat: add full and share export presets`.

### Task 8: Refined previews

**Files:**
- Modify: `src/lumen/previews.py` (`ExifToolPreviewProvider.create_refined_preview`)
- Modify: `src/lumen/domain.py` (`Photo.preview_kind`)
- Modify: `src/lumen/catalog.py` (migration `photos.preview_kind`, `update_preview`, `photos_for_filters`)
- Modify: `src/lumen/library.py` (`start_refine_job`, `_rerank_group` reuse, preview retention)
- Modify: `src/lumen/web.py` (`/api/previews/refine`, preview URL version, `preview_kind` in payloads)
- Modify: `src/lumen/cli.py` (`refine-previews`, `_serializable_groups` version string)
- Modify: `src/lumen/static/index.html`, `app.js`, `styles.css` (Refine control, "Refined" mark)
- Modify: `README.md`, `docs/ARCHITECTURE.md`
- Test: `tests/test_previews.py`, `tests/test_catalog.py`, `tests/test_library.py`, `tests/test_web.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 1 worker rule and job strip, Task 2 `_rerank_group` and selection rule, Task 6 `GroupFilters`, `find_tool("darktable-cli")`, `measure_image`, `analyze_astro`, `analyze_faces`, `apply_vision_result`.
- Produces:
  - `ExifToolPreviewProvider.create_refined_preview(source: Path, destination: Path, *, darktable: str, runner=subprocess.run) -> Path`: runs `[darktable, source, <tmp>, "--width", "2048", "--height", "2048", "--core", "--conf", "plugins/lighttable/export/metadata_flags=5"]` into a temporary beside the destination, normalises with the existing `_normalize`, renames atomically; raises `PreviewError` on failure or missing output.
  - Preview naming: `<fingerprint>-2048.jpg` under `preview_dir`; `Photo.preview_kind: str = "embedded"` (`"embedded" | "refined"`); `Catalog.update_preview(photo_id, preview_path: str, preview_kind: str)`; the old embedded file is unlinked only when no other photo references it (same rule as cleanup's shared-preview retention).
  - `Library.start_refine_job(*, group_id: int | None = None, filters: GroupFilters | None = None, all_photos: bool = False) -> Job` (kind `refine_previews`; exactly one selector; raises `RuntimeError("darktable-cli is not installed")` before creating the job). Worker per photo under the mutation lock: verify the source fingerprint is unchanged (skip and log otherwise), render, update preview and kind, recompute `measure_image`, `analyze_astro`, `analyze_faces`/`apply_vision_result`, `save_metrics`; afterwards `_rerank_group` for each affected group and rewrite their sidecars. `manual_pick`, `decision`, and `rating` are never written by this job.
  - Preview URL version becomes `?v=<fingerprint[:16]>-<preview_kind>` in `_serializable_groups`, `/api/photos/<id>`, and `/api/photos/<id>/crops` so browsers refetch; groups/photo payloads carry `preview_kind`.
  - Web `POST /api/previews/refine {"scope": "group", "group_id"} | {"scope": "filtered", ...filter fields} | {"scope": "all"}` → 202, or 400 `{"error": "darktable-cli is unavailable"}`; CLI `refine-previews (--group ID | --all | filter flags)` runs and waits.
  - UI: `Refine previews` button beside the contact-sheet heading with scope `<select>` (selected burst / filtered frames / whole catalog), disabled with the reason text when darktable is unavailable; frame cards show the text mark "Refined"; the job strip reports progress.

- [ ] Write failing tests: `test_previews.test_refined_preview_uses_darktable_and_renames_atomically` (fake darktable script writes a 2048-long-edge JPEG; assert no `.tmp` remains and `PreviewError` on nonzero exit), `test_catalog.test_migration_adds_preview_kind_and_update_preview_round_trips`, `test_library.test_refine_job_changes_sharpness_reranks_group_and_keeps_pick_decision_and_rating` (fake darktable renders a sharper image for the alternate frame; assert the automatic pick changes while `manual_pick`, `decision`, and `rating` are untouched, and a manual pick is honoured), `test_library.test_refine_job_skips_changed_sources_and_retains_shared_embedded_previews`, `test_library.test_refine_job_scopes_group_filtered_and_all`, `test_web.test_refine_route_scope_validation_and_unavailable_darktable_reason`, `test_web.test_preview_url_version_changes_after_refinement_and_cards_show_refined_mark`, `test_cli.test_refine_previews_command_waits_and_reports`.
- [ ] Run focused modules; expect missing method, column, route, and flag.
- [ ] Implement the provider method, catalog column and updater, job worker, URL versioning, route, CLI, UI control and mark, and document the refine workflow, preview naming, and darktable requirement in `README.md` and `docs/ARCHITECTURE.md`.
- [ ] Run focused tests, the full warning-strict suite, `node --check`, and `uv run lumen doctor --json`; expect PASS. If the project-local darktable is present, refine one generated JPEG in a scratch library and confirm the 2048 px preview and changed sharpness; state explicitly if this optional check was skipped.
- [ ] Independent review of Task 8 against feature 8, the "never changes a manual pick, decision, or rating" rule, and the atomic-write rule; fix findings.
- [ ] Commit: `feat: add darktable-refined previews`.
