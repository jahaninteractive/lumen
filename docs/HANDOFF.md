# Lumen implementation handoff

Last updated: 2026-10-23 (Africa/Johannesburg) — Local AI Assist merged into `main`

## Resume here

**Local AI Assist merged into `main` (2026-10-23, merge commit `b409055`).** Spec `docs/superpowers/specs/2026-10-22-local-ai-assist-design.md`; plan `docs/superpowers/plans/2026-10-22-local-ai-assist.md`, all 7 tasks done with a per-task review and a whole-branch final review (fix wave applied). Warning-strict suite: 440 tests pass on merged `main` (`env -u LUMEN_OMLX_MODEL`), `node --check` OK; 17 commits plus the merge. No Safari/VoiceOver pass and no live-model end-to-end run were done.

- Roles and gating: `Settings.omlx_fast_model` (`LUMEN_OMLX_FAST_MODEL`, default `Qwen3-VL-8B-Instruct-4bit`) and `Settings.review_model` (`LUMEN_OMLX_REVIEW_MODEL`, else `omlx_model`). `Library.tagger_for(role)` is the only `OmlxTagger` constructor; it reads live `/api/status` and raises `RuntimeError("<role> model <name> is not loaded in oMLX")` (web → 400 verbatim; a refusal never creates a job row). `lumen doctor` / `/api/status` carry `omlx_roles {fast, review: {model, loaded}}`; the browser disables every AI button from it with a text reason naming the model.
- Vocabulary: `src/lumen/data/vocabulary.json` is copied once to `<library>/vocabulary.json` (`vocabulary.load_vocabulary`), then photographer-owned. `match_keywords` is case/whitespace-insensitive, stores the vocabulary spelling, caps free-text suggestions at 3; species < 0.6 confidence are never applied even if the model repeats the name as a subject.
- `tags.data` keys (all optional, read-modify-write merged by every writer): `description, subjects, scene, technical, confidence, species, conditions, suggested, problems{items,kinds,suggest_reject,confidence,reviewed_at,dismissed_at?}, sky{class,confidence}, issues, caption, alt_text`. A row counts as "tagged" only when it has a `description`. **Caveat:** `--retag` replaces `subjects`/`suggested` wholesale, reverting keywords moved with Add/Remove.
- Jobs: `Library._run_photo_job(kind, tagger, scope, retag, per_photo, counters=, manifest=)` backs `tag` (scope `keepers`|`all`|[ids]; long exposures ≥ 1 s are also triaged, failures counted as `triage_failed`), `review_rejects`, and `triage`. One-shot calls (`critique_burst`, `caption_photo`, `digest_day`, `review_composition`) hold the library mutation lock through the model call (codebase pattern, ruled acceptable); every browser button for them sets `state.busy` so culling controls disable instead of hanging.
- Provenance kinds added: `tagged` (+role/applied/suggested), `keyword_added`, `keyword_removed`, `reject_suggested`, `reject_review_failed`, `reject_suggestion_decided`, `burst_reviewed`, `burst_review_failed`, `astro_triaged`, `astro_triage_failed`, `caption_written` (author model|photographer), `caption_failed`, `digest_written` (photo_id None, `path_name` basename). Raw model text lives only in events; browser payloads carry parsed fields.
- Routes (same-origin + strict JSON where they mutate): `GET /api/vocabulary`; `POST /api/photos/tag {scope, retag?}` (`tag-all` alias); `POST /api/photos/<id>/keywords {action: add|remove, keyword}`; `POST /api/photos/review-rejects {scope}`; `GET /api/photos/reject-suggestions`; `POST /api/photos/reject-suggestions/decide {photo_ids, decision: reject|keep}` (ids-not-count; returns `_decided` dicts); `POST|GET /api/groups/<id>/critique`; `POST|GET /api/groups/<id>/triage`; `POST /api/photos/<id>/caption {}` and `/caption/edit {caption, alt_text}` (None = unchanged, "" = clear; limits 200/125, no `#`); `POST /api/digests {date, overwrite?}` (409 `{exists:true}` without overwrite; soft review gate → facts-only file); `/api/groups` photos gain `matched` and groups gain `astro_cue`; `/api/status` gains `digest_dir`.
- CLI: `tag --keepers|--all|--photo`, `review-rejects`, `critique <group>`, `triage <group>`, `caption <photo>`, `digest <YYYY-MM-DD> [--overwrite]`.
- UI: six tabs (Review tab between Export and Astro: scan button, reject rows with Reject/Keep, X/K/arrows from a row button only, "Reject all shown" with a counted dialog); inspector keywords with Add/Remove, burst critique block with "Use this pick" (hidden when the suggested frame was rejected), caption/alt inputs with Save, match reasons; Astro tab per-light triage text and "Exclude flagged lights (N)" behind a counted dialog; burst header "Looks like astro" text cue; rail `#find` (mirrors `#filter-text`, 300 ms debounce, never rewritten while typing) and "Write today's digest" (overwrite dialog on 409).
- Share export uses `caption or description` plus `Iptc4xmpCore:AltTextAccessibility`; full export is unchanged (no alt text). Artifact settings gain `caption_used`.
- Known deferred minors are listed in the plan's ledger history (per-task reviews); the notable ones: `/api/groups` reads tags per photo for `astro_cue`/`matched` (batch with `json_extract` if the sheet slows); `load()` has no stale-response counter; `omlx.py` parser error classes could share one base; `library.py` is ~1600 lines and the assist methods would lift into a `lumen/assist.py` module in one move.

**Full-height layout + bursts on the sheet (2026-10-22, UI only).** `body` is a non-scrolling 100 vh flex column; the masthead is sticky, and the batch rail, `main`, and inspector each scroll independently (`.batch-rail, main, .inspector { overflow-y: auto }`). Below 680 px the workspace itself scrolls instead.

- Main column: `div.main-top` (import bar, tag bar, `div.tab-row` = tablist + `#collapse-panel`) is sticky at its top; `section#workflow-panels.workflow` holds the tab panels and `#collapse-panel` (native button, `aria-pressed`, `aria-controls`) hides it so the sheet gets the space. State is remembered in `localStorage` `lumen-panel-collapsed` (try/catch); choosing any tab expands it again. A `ResizeObserver` writes `--sticky-top` so `scroll-margin-top` on bursts keeps `scrollIntoView` clear of the sticky header.
- Contact sheet: one `section.burst[data-burst=<id>]` per burst with frames on the page (`aria-labelledby` its `h3#burst-<id>-heading`), header = “Burst N”, `frame count · local start (toLocaleString medium/short) · best <name>`, a `span.burst-selected` text cue (“Selected”), a labelled `input.burst-select` “Select for merge”, “Remove batch” (same `cleanup` route as the Clear tab) and “Astro” (selects the burst, switches to the Astro tab). Frames are the unchanged `button.frame` elements inside `div.burst-frames` (the grid moved from `.contact-sheet`). Selected burst: `data-selected="true"` → 2 px amber outline. Culling keys now ignore every non-`.frame` button, so header buttons never cull.
- Rail: each `div.batch-row` is `img.batch-thumb` (best frame preview, lazy, empty alt) + `input.batch-select` + `button.batch-button` whose text now includes the local start time (`batchText`). Checkboxes in rail and sheet share `data-merge-group` and are synced from `state.mergeSelection` in `updateWorkflow`. `#rail-header` (sticky) holds the heading, `#merge-count`, `#merge-batches`, and `#jump-selected`. A rail click selects the burst and scrolls its section into view (`block: 'start'`, `behavior` honours `prefers-reduced-motion`) without moving focus. Merge also scrolls to the merged burst.
- Inspector: `#back-to-top` scrolls `#main-content` to the top (the workflow tabs). Both jump buttons announce.
- Tests: `test_layout_is_full_height_with_collapse_toggle_burst_sections_and_rail_merge_controls` (HTML structure, CSS regexes, JS source) and Node scenario `bursts` (`test_javascript_contact_sheet_groups_frames_into_burst_sections`): sections, labelling, rail row shape, checkbox sync both ways, rail click scroll + focus, jump/back-to-top, collapse persistence, Astro and Remove buttons. The Node harness now walks the sheet tree for `.frame`, records `scrollIntoView`/`scrollTo`, stores `localStorage` writes, and reports reduced motion. Suite: 379 passed, `node --check` OK. No Safari/VoiceOver pass was done for the new layout.

**Exposure-aware grouping + manual merge (2026-10-22, on `main`).** Long-exposure astro lights (15–30 s) were never grouped because `TimeGapGroupingPolicy` compared capture timestamps only.

- `previews.read_metadata` now runs exiftool with `-n` and reads `ExposureTime` as seconds (`exposure_seconds`, `None` when absent or non-numeric). `Photo.exposure_seconds` is persisted through the existing `_MIGRATIONS` table (`photos.exposure_seconds REAL`, `photos.merged_into TEXT`) so older catalogs upgrade on open; `upsert_photo` and `_photo_from_row` carry it.
- Grouping rule: adjacent same-camera frames group when `next.captured_at − previous.captured_at ≤ gap_seconds + 2 × previous.exposure_seconds` (`None` → 0). The doubled exposure covers in-camera long-exposure noise reduction. Short exposures keep the 2-second behaviour; the existing grouping tests are unchanged. Already-imported photos get an exposure only on re-import (upsert refreshes it).
- `Library.merge_groups(group_ids)`: moves every photo into the earliest group (list order: `started_at`, then id), keeps at most one manual pick (the target's first, else the first source's), appends a `groups_merged` event (`{group_id, merged_group_ids}`) on each photo of the merged group, re-ranks it, rewrites sidecars, and deletes the emptied `photo_groups` rows. Moved photos store the target's `stable_key` in `merged_into`; `_regroup_and_write_sidecars` groups only un-merged photos automatically, then appends merged photos to the group whose stable key matches (a merge whose target burst vanished falls back to its own burst). Reimport therefore keeps manual merges.
- CLI `lumen merge <group-id> <group-id>... [--json]` (exit 2 with `{"error": ...}` on bad ids). API `POST /api/groups/merge` `{"group_ids": [int, int, ...]}` with the shared host/origin + strict-JSON prelude: exactly that key, ≥2 distinct positive ints, else 400. Response `{group_id, merged_group_ids, photo_count, automatic_pick_id}`; it is in both cross-origin/malformed-JSON regression lists.
- Batch rail: each batch row is `div.batch-row` holding a labelled `input.batch-select` checkbox (`Select Burst N · … for merging`) and the batch button (now also a thumbnail, see above); `#merge-batches` ("Merge selected batches") sits in the sticky rail header, enabled with ≥2 ticked batches, posts, reloads, selects the merged batch's first frame, clears the selection, and announces `Merged N batches into Burst X …`. `state.mergeSelection` is pruned to existing groups on every load. The astro job code is untouched.
**Unsupported Sony raw flag + oMLX thinking fix (2026-10-22).** Three of the user's frames (DSC00712–00714) are Sony *reduced-size* (M) lossless ARW: the raw IFD is YCbCr lossless JPEG (`SubIFD:PhotometricInterpretation` 6) and darktable 5.6.2's rawspeed refuses it (`ArwDecoder::DecodeLJpeg … Unsupported photometric interpretation: 6`), so JPEG export and refined previews fail; full-size `Sony Compressed RAW` frames export fine. macOS `sips` does decode them (3504×2336, 16-bit TIFF), so JPEG export falls back to it for flagged frames (below).

- `read_metadata` also reads `-SubIFD:PhotometricInterpretation` and returns `raw_warning` (set only for `.arw` with value 6). The warning is stored in the photo's `imported` event (`raw_warning`, `None` otherwise); `Library.get_photo()["warnings"]` and `GET /api/photos/<id>` `warnings` come from the *latest* `imported` event, so a reimport after a decoder upgrade clears it. Export of a flagged frame renders through `DarktablePipeline.export_jpeg(..., decoder="macos")`: `/usr/bin/sips -s format tiff --matchTo <sRGB Profile.icc> <staged source> --out render.tif` (default-settings render, no XMP profile — a profile with the macOS decoder is refused), `tool_version` = `macOS <sw_vers> sips`, `settings["decoder"]` = `macos` in the artifact, the `exported` event, and the JPEG's XMP provenance; the result message says it used the macOS decoder. Everything after the render (EXIF copy, sRGB conversion, crop, resize, MakerNote, collision-safe link) is the shared darktable path. Verified live on DSC00807.ARW through `POST /api/export`: 3504×2336 sRGB JPEG with camera EXIF. Unflagged frames still never touch sips. The inspector renders each as `p.warning` starting with `Warning: ` (amber). Background import jobs skip unchanged files, so already-imported photos are flagged only by a synchronous `lumen import <file>` (server stopped) or a changed file.
- oMLX: both loaded models (Qwen3.5-9B / Qwen3.8-27B) are thinking models; with `max_tokens` 500 they filled the budget with `reasoning_content` and returned no `content`, which surfaced as `Local oMLX request failed: 'content'` (`tag_failed`). `_complete` now sends `chat_template_kwargs: {"enable_thinking": false}` (verified live on both models: zero reasoning tokens, direct JSON) and reports a reasoning-only reply in plain words. The server inherits the shell's `LUMEN_OMLX_MODEL=Qwen3.5-9B-4bit`.
- Inspector ranking explanation (2026-10-22): below the verdict line, `p.ranking` says e.g. `Ranked 1 of 3 in this burst: sharpness 62% (highest), exposure 90%, overall 69%. Overall weighs sharpness 50% and exposure 25%, plus faces and eyes 25% when detected.` Rank is the row's position in the `/api/groups` burst (server order = total_score desc), and the numbers come from the burst rows, the same values the ranking used; "(highest)" is per metric against burst siblings. Node scenario `rank-explain`.
- Tests: `test_previews` raw flag, `test_library` import flag + export fast-fail + reimport clear, `test_web` `warnings` field + Node scenario `raw-warning`, `test_omlx` payload flag + reasoning-only error.

- Tests: `test_grouping` (31 s / 62 s / 90 s spacing, `None` exposure), `test_previews` exposure parsing, `test_catalog` migration + upsert round trip, `test_library` merge/pick/reimport, `test_cli` merge, `test_web` route validation + Node scenario `merge-batches`. The `filters` scenario now expects `batch-row` children under each day heading.


**Branch `travel-volume-culling` (13 commits, `6236e7a..1f8a8a3`) implements the whole of `docs/superpowers/plans/2026-10-22-travel-volume-culling.md`** (spec: `docs/superpowers/specs/2026-10-22-travel-volume-culling-design.md`). Every task had an independent review and fix round; the whole-branch final review came back "ready with fixes", the single fix wave (`1f8a8a3`) was re-reviewed clean. The branch is **not merged into `main`**; merging is the user's call. The warning-strict suite passes **360 tests** at `1f8a8a3` (`env -u LUMEN_OMLX_MODEL`), `node --check src/lumen/static/app.js` passes, and `lumen doctor` reports darktable-cli, siril-cli, and exiftool available. The old astro plan is complete and committed as `0923f08`.

Rulings made during execution (all recorded in `.superpowers/sdd/2026-10-22-travel-volume-culling/progress.md`, which is local audit material):

- Keepers = not rejected AND (flagged OR best in group); single frames stay keepers unless rejected (user decision, overrides the spec sentence).
- `full` export keeps GPS; only `share` drops it (user decision).
- Ingest copies and verifies every visible regular file on the card regardless of extension (videos, Sony housekeeping files) and imports only supported image types; dot-prefixed entries are `skipped` and do not block the verdict. `card_verified_everywhere` requires planned > 0 and zero read errors; destinations on the card's own volume are rejected.
- `/api/groups` keeps all-rejected bursts (empty `photos` by default) with per-group `frame_count`/`rejected_count` and a library-wide `keeper_count`; a burst emptied only by a filter is omitted.
- Decisions, ratings, picks, and Compare work during `import`/`ingest` jobs; they pause during `tag`, `refine_previews`, and `astro` jobs.
- Per-photo `imported`/`tagged`/`tag_failed`/`import_failed`/`ingested`/`preview_refined` events carry `job_id`.

Deferred minors worth picking up after merge (full list in the ledger): tag and refine jobs hold the mutation lock during the oMLX call / darktable render; a cancelled job keeps the runner slot until its current item and regroup finish (409 in that gap); `list_job_status` loads every job row; `Catalog.delete_photos` re-pick ignores the selection rule (unreachable today); text filter misses non-ASCII tags (`ensure_ascii` JSON); `#contact-sheet` is `aria-live="polite"` and may double-announce in-place refills; ingest rerun after a date/timezone change lands no-EXIF files in a new date folder; refined render is renamed before the post-render fingerprint check; synchronous `Library.import_path` (tests only) reverts refined previews; README feature list still says quality-92 only; `library.py` (~1,180 lines) and `app.js` (~1,400 lines) deserve a split, starting with the job workers.

## Working features

- Background jobs (`import`, `tag`, `ingest`, `refine_previews`, `astro`) through one `LocalJobRunner` slot with a job strip, progress labels (basenames only), cancel, `GET /api/jobs`, and CLI `jobs`/`cancel`.
- Decisions (`none`/`flag`/`reject`) and ratings 0–5 in catalog, JSON and XMP sidecars (`xmp:Rating` −1 for rejected), provenance; keeper rule shared by `keeper_ids`, SQL filters, the client, and export.
- Verified card ingest (`lumen ingest`, Ingest tab): per-file SHA-256, `.lumen-partial` temporaries, `F_FULLFSYNC` + `F_NOCACHE` verify, one retry, resumable manifest under `outputs/ingest/<job>/`, mirror provenance, catalog snapshot beside the primary copies, and exactly two verdict sentences.
- Keyboard culling with roving focus and a Shortcuts legend; Compare (two panes, synced panning) and loupe dialogs.
- Filters (date, camera, decision, rating, keepers, tagged, text) in the URL query, day headings in the batch rail, 200-frame paging.
- Export presets `full` and `share` (2048 px, q85, no GPS) with preset recorded in artifact settings and provenance.
- Refined previews: a `refine_previews` job renders 2048 px previews through darktable-cli, recomputes metrics, re-ranks, and never changes picks, decisions, or ratings.
- Non-destructive Sony `.ARW`/image import, managed previews, SQLite catalog, JSON/XMP sidecars, and provenance.
- Camera/time burst grouping, explainable best-shot ranking, manual keeper override, sharpness/exposure/face/eye metrics, and basic astro screening.
- Truthful live oMLX model status plus local preview tagging.
- Recoverable group removal and workspace clearing with catalog snapshots; originals and completed exports are retained.
- Quality-92 sRGB JPEG export through darktable for current photo or all keepers, collision-safe naming, optional explicit XMP profile, metadata filtering, and Finder reveal.
- Deterministic non-destructive crop candidates from macOS Vision regions with honest centered fallback, 8 MP rejection, and below-12-MP warnings.
- Persisted crop choices in catalog/sidecars and exact normalized full-resolution crop application during JPEG export.
- Optional single-request oMLX composition critique that may select only a supplied candidate, records provenance, and never activates a crop.
- Accessible Compose → Export → Clear web workflow with CSS crop viewports, thirds overlays, native action buttons, active/recommended state, request-race protection, safe public API errors, and advisory-only AI rationale.
- Persisted local job records with restart recovery and one exclusive heavy worker that can cancel/reap only its owned process.
- Astro tab: explicit confirmation checkbox, per-light inclusion, calibration folder pickers, text-prefixed warnings, native progress, cancel, polling only while active, retained manifest/log paths on failure, Finder reveal of the job folder.

## Latest reviewed fix evidence

Composition Task 5 review originally found six issues. Current code addresses all of them:

1. Crop/review API errors use stable public messages and do not expose source/preview paths.
2. Monotonic load/review tokens prevent stale same-photo and cross-photo responses; new loads immediately clear old candidates/advice.
3. Crop buttons have distinct accessible names containing crop type and aspect.
4. The Node harness executes successful choose/clear, AI failure, stale-card, request-race, and mismatched-recommendation flows.
5. Active+recommended cards retain amber selection with a separate cyan cue.
6. No-selection copy is restored after a failed crop load.

Verification commands last run:

```sh
uv run python -W error -m unittest tests.test_web -v       # 37 passed
node --check src/lumen/static/app.js                       # exit 0
env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest discover -s tests -v   # 269 passed (2026-10-22)
```

The latest whole-plan fixes prove the default candidates include one contextual and one tighter crop, source replacement invalidates an old crop before the fingerprint-upsert failure boundary, and every export rejects source bytes not successfully cataloged after an interrupted replacement. See `.superpowers/sdd/2026-10-21-composition-crops/plan-final-fix-report.md`.

No manual Safari/VoiceOver pass, real Sony RAW crop visual check, Apple Vision hardware pass, or live oMLX composition request has been performed. Those remain integration risks, not known automated failures.

## Real darktable export check (2026-10-22)

`test-image/DSC01027.ARW` (Sony ILCE-7CM2, untracked, not committed) exported via `lumen --library <scratch> export --keepers` in ~12 s:

- Original unchanged: sha256 `21a7428d…ac3d8`, mtime `1792167197.79`, size 34263040 identical before/after; no sidecar written beside it.
- JPEG 7032×4688 (darktable's full uncropped-border render, `--width 0 --height 0`; raw 7040×4688), embedded sRGB ICC + EXIF ColorSpace sRGB, quantization tables exactly Pillow quality 92.
- No camera EXIF in that first JPEG; fixed afterwards (see known limitations). The source has no GPS, so GPS was verified on a scratch copy with injected coordinates.

## Next milestone: managed Siril astro stacking

Use `docs/superpowers/plans/2026-10-21-astro-stacking.md` task by task. Do not jump straight to the UI.

1. **Job catalog and single-worker lifecycle** — complete and independently approved.
2. **Reproducible Siril preparation** — complete; see "Astro Task 2" below.
3. **Execute/cancel/import** — complete; see "Astro Task 3" below.
4. **CLI and protected API** — explicit group confirmation, selection/exclusion, calibration folders, status/log tail, run, and cancellation.
5. **Astro web workflow** — explicit confirmation, per-light inclusion, calibration folder pickers, warnings, progress/cancel, retained failure context, and finish/export action.

The installed local executable is:

```text
vendor/apps/Siril.app/Contents/MacOS/siril-cli
siril 1.4.4
```

It currently prints `Siril is started as macOS application` followed by the version. `lumen.tools.find_tool` (used by doctor, `PipelineRegistry`, and the darktable preview fallback) checks `PATH` first, then `vendor/apps/<Bundle>.app/Contents/MacOS/<tool>` for `darktable-cli`/`siril-cli`, and rejects any resolved executable outside that bundle. Covered by `tests/test_tools.py`; verified with vendor dirs stripped from `PATH`.

### Astro Task 2 — what exists (2026-10-22)

Evidence (exact `--version`/`--help`, help sources, per-branch dry runs): `.superpowers/sdd/2026-10-21-astro-stacking/task-2-siril-validation.md`.

- `src/lumen/astro.py`: `probe_siril` (parses the macOS banner; argument array), frozen `SirilCapabilities`/`AstroFrame`/`AstroFrameSet`/`AstroManifest`, `siril_script` (gated to Siril `>=1.4.0,<1.5.0`; the script also starts `requires 1.4.0 1.5.0`), `prepare_siril_job`, `load_manifest(path, output_root)`.
- Job layout `outputs/astro/<job-id>/`: `lights|darks|flats|biases/<kind>_NNNNN.<ext>` read-only-source symlinks with fixed ASCII names (source paths never enter the script), empty `siril.config`, `stack.ssf` and `manifest.json` written once and chmod 0444. The job dir is created exclusively; a mid-prep failure removes only that dir.
- Batch command (`SirilPipeline.job_command`): `[siril-cli, -d <job>, -i <job>/siril.config, -o, -s <job>/stack.ssf]`. `-i` needs an existing file (missing → exit 1); `-o` is offline; without `-i` Siril writes `~/Library/Application Support/org.siril.Siril` (created once by a `help` probe on 2026-10-22, not by jobs).
- Script: optional bias/flat/dark masters, light calibration (`-dark` preferred over `-bias`, `-cc=dark -cfa` only with darks, `-equalize_cfa` only with flats), `register` (global star alignment), `stack ... rej w 3 3 -norm=addscale -output_norm -rgb_equal -filter-wfwhm=3k -filter-round=3k -out=result`, `mirrorx -bottomup`, `savetif ../result` (16-bit).
- Rules: ≥2 selected lights; excluded lights recorded, not linked, and cannot reappear as calibration; calibration folders non-recursive, hidden files skipped, `.arw/.fit(s)/.fts/.tif(f)` only; missing, empty, or single-frame calibration sets warn and are skipped (Siril 1.4.4 builds no sequence from one frame); trailing warnings only for selected lights; full sha256 + size + mtime_ns, rejected if the source changes during hashing.
- `lumen doctor` reports Siril `version: "1.4.4"`, `supported: true` (was the banner line).
- Dry-validated with the installed 1.4.4 on synthetic RGGB CFA FITS (no user photos), library path with space/non-ASCII: every calibration branch exit 0 with 16-bit RGB `result.tif`; sources unchanged. **Not yet run on real Sony ARW lights.**

### Astro Task 3 — what exists (2026-10-22)

- `Library.prepare_astro_job(group_id, *, confirmed, excluded_photo_ids, darks, flats, biases) -> Job`: requires `confirmed is True`; refuses lights whose bytes differ from their catalog fingerprint; probes Siril via `find_tool`+`probe_siril`; `Catalog.create_prepared_job` inserts a `pending` placeholder, prepares the job dir with no SQLite lock held (frame hashing can take seconds), then stores the paths or deletes the placeholder on failure. Runner startup purges placeholders left by a crash.
- `Library.run_astro_job(job_id, *, timeout_seconds=7200)`: verifies the manifest on the caller thread, submits to the lazily created `LocalJobRunner` (one per `Library`; `close()` shuts it down). The worker re-loads the manifest, then `astro.execute_job`: refuses if `result.tif`/`process/`/`masters/` already exist, revalidates every frame (exists, link still points at it, size, mtime_ns, sha256), launches the manifest command via Popen argument array with cwd=job dir, streams stdout+stderr (ANSI-stripped) into `<job>/siril.log` from a reader thread, writes coarse progress 0.05→0.1→0.3 (calibrate)→0.5 (register)→0.9 (savetif)→0.95 (TIFF verified) from the polling thread, enforces the timeout (terminate→kill), and requires a 16-bit TIFF (`BitsPerSample` all 16) even on exit 0.
- Success bookkeeping (`_record_astro_result`, worker thread, own `Catalog` under the library mutation lock): artifact kind `astro_tiff` (photo_id None, settings include job_id, siril_version, command, counts, light_fingerprints, light_photo_ids, exit_code, bits_per_sample), one global and one per-light `astro_stacked` event, light sidecars rewritten, then `result_path`; any failure discards the artifact/events, rewrites sidecars, and fails the job. Cancellation returns no result; a recorded result is never undone by a late cancel (`LocalJobRunner.cancel` returns False once `result_path` is set — a two-line Task 1 change made for this).
- `Library.cancel_job`: queued jobs cancel without a runner; running jobs raise RuntimeError if this `Library` owns no runner. `wait_for_job(job_id, timeout)` for callers/tests.
- Verified: fake Siril as a real child process (changed/missing frames, stale outputs, timeout, exit 3, exit 0 without TIFF, 8-bit TIFF, mid-run cancel, cancel racing a clean exit, bookkeeping failure compensation, unchanged source bytes/mtimes in every case); mutation checks on revalidation, exit/TIFF, cancel, and late-cancel guards; `execute_job` against real Siril 1.4.4 on synthetic CFA FITS (16×16×16 TIFF, sources unchanged). **Still not run on real Sony ARW lights.**

### Astro Task 4 (2026-10-22): CLI and protected API

- CLI: `lumen astro prepare GROUP --confirm [--exclude PHOTO_ID]... [--darks DIR] [--flats DIR] [--biases DIR]`, `lumen astro run JOB` (blocks until the job ends; exit 2 on failure), `lumen astro status JOB`, `lumen astro cancel JOB`; all take `--json`. Without `--confirm`, prepare refuses.
- API (same host/origin checks and strict JSON as every other mutation): `POST /api/astro/jobs` `{group_id:int, confirmed:true, excluded_photo_ids?:[int], darks?/flats?/biases?: path|null}` → 201; `POST /api/astro/jobs/<id>/run` `{}` → 202, or 409 when another heavy job is active; `GET /api/astro/jobs/<id>` → status; `POST /api/astro/jobs/<id>/cancel` `{}` → 200, 400 once terminal. Unknown jobs: GET 404 JSON, POST 400 JSON.
- Status payload (`Library.job_status`, shared by CLI and web) is a whitelist: `job_id, kind, status, progress, error, result {artifact_id, name}|null, log_tail, warnings, lights, excluded_lights, darks, flats, biases, created_at, updated_at`. Never the manifest, `result_path`, `manifest_path`, fingerprints, or the launch command (the `lumen: command` log line is dropped; the library path is replaced by `<library>` in the tail). `error` and every `/api/astro/*` or CLI astro error string go through `library.public_message`, which replaces absolute paths with `<path>`. Calibration-folder warnings now use the folder basename (astro.py change; test_astro updated).
- One runner per process: `web.LumenServer` creates the `LocalJobRunner` eagerly (so stale `running` rows are recovered at server start) and shuts it down in `server_close`; per-request `Library(settings, job_runner=server.job_runner())` no longer owns or stops it (`Library._owns_runner`). Consequence: while `lumen serve` runs, `lumen astro run` in another process fails with "catalog already has a job runner", and CLI `astro cancel` of a job the server is running reports "running under another job runner; cancel it there" (exit 2). That is honest, not a bug.
- `LocalJobRunner.cancel` now waits up to 2 s for the worker to release the heavy slot after stopping an owned process, so cancel-then-run no longer spuriously 409s.
- `job_status` degrades when a job directory was deleted/tampered: status/log still return with a "Job manifest is unavailable" warning; run then fails 400 without paths; cancel still works.
- Deferred minors: 409 is chosen by matching the "another heavy job" message; `/run` during `server_close` may race; CLI `astro status` exits 2 for failed jobs; status rereads the full log and artifact list per poll; no adapter test for CLI cancel of a running job.
- Verified: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest discover -s tests` → 281 passed; `node --check src/lumen/static/app.js` OK; mutation checks (origin bypass on astro routes, `manifest_path` leak in status) both caught by tests.

### Astro Task 5 (2026-10-22): Astro web tab and verification

- `index.html`: `Astro` tab between Export and Clear (same tablist/arrow-key behaviour, `aria-selected`, roving tabindex). Panel: Siril availability line, `fieldset#astro-lights-group` with one checkbox per light of the selected batch (all included by default, ≥2 required), three calibration folder rows (`#astro-<kind>-path` + `Choose … folder…` via the existing `/api/browse` macOS picker + `No …` reset), `#astro-confirm` checkbox with a `for` label, Prepare / Run / Cancel / Show job folder buttons, `#astro-summary`, `ul#astro-warnings` (each item starts with `Warning: `), native `<progress id="astro-progress">` with `aria-valuetext`, `#astro-status` (`role=status`, polite), `#astro-paths` (manifest, log, result under `<astro_output_dir>/<job-id>/`), `pre#astro-log` tail.
- `app.js`: `state.astro = {job, timer, folders, lightsKey}`; lights re-render only when the selected batch's photo ids change; `setAstroJob` renders everything, announces every status transition through the existing assertive announcer, remembers an active job id in `localStorage` (`lumen-astro-job`) so a reload resumes watching it, and starts `setInterval(pollAstro, 1000)` only while `running`, clearing it on any terminal state or poll error. Prepare is disabled unless Siril is available, a batch is selected, the confirmation box is checked, ≥2 lights are included, and no job is queued/running. All actions go through `astroAction` (busy flag + status/announce on error).
- `web.py`: `/api/status` adds `astro_output_dir` (managed library path, like `jpeg_output_dir`); `POST /api/astro/jobs/<id>/reveal` `{}` opens that job's existing folder in Finder via `reveal_outputs(settings, destination)` and refuses unknown jobs or missing folders before touching `/usr/bin/open` (never creates folders).
- Tests: `test_finishing_tabs…` now expects four tabs and `astro-status` live region; `test_astro_panel_has_explicit_confirmation_and_labeled_controls` (semantic HTML + JS source checks); `test_status_exposes_configured_jpeg_destination` also checks `astro_output_dir`; `AstroWebTests.test_reveal_opens_only_an_existing_managed_job_folder`; Node harness scenarios `astro`, `astro-fail`, `astro-cancel` (`test_javascript_astro_tab_confirms_includes_polls_and_retains_failure_context`) drive the real `app.js`: confirmation gating, <2-light gating, exclusion payload `{group_id, confirmed:true, excluded_photo_ids:[3], darks:'/cal/darks', flats:null, biases:null}`, picker, warning prefix, timer count 1 while running and 0 after success/failure/cancel, failure keeps manifest/log paths and error text, success shows result path and reveal.
- Verification (2026-10-22): `node --check` OK; `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest discover -s tests` → **284 passed**; `uv run lumen doctor --json` → siril-cli 1.4.4 supported, darktable-cli 5.6.2 available. **Real Siril fixture stack:** three Pillow-generated 256×256 16-bit star fields (40 synthetic stars, sub-pixel shifts) in a scratch library → `lumen astro prepare 1 --confirm` → `lumen astro run 1` → `succeeded`, Siril found 32 stars per frame, `3 registered, 0 failed`, `result.tif` 256×256 RGB 16/16/16, source SHA-256s unchanged. The same fixture was then driven through a live `lumen serve` (`POST /api/astro/jobs` with one exclusion → `run` → poll → `succeeded`, result artifact 2) with zero path/fingerprint leaks in the status JSON and a cross-origin `reveal` rejected. Not run: real Sony ARW lights, a Safari/VoiceOver pass, and the Finder reveal itself (it would open a window on the user's desktop).
- Final fresh-context review (Task 5): 1 Critical, 3 Important, 4 Minor. Fixed red-green in one pass: (C) `/run` answers with the still-`queued` row, so the panel never started polling — polling now starts on an accepted run and stops only on terminal states; the Node fake returns `queued` from `/run` like the real server, and a reload-resume scenario (`astro-resume`) was added; (I) the polite `#astro-status` region was rewritten every poll — text is now written only when it changes and the running text steps by 10 %; (I) lights, confirmation, and folder buttons stay disabled while a job is queued/running and the queued text says to cancel to change the selection; the reviewer's "Minor" stale-poll race turned out to re-arm polling after cancel, so a `state.astro.request` counter now drops in-flight poll responses older than the latest state. Deferred minors: the confirmation box stays ticked after a job ends; `/api/astro/jobs/<id>/reveal` is not yet in the cross-origin/strict-JSON regression lists (the handler shares the same prelude and has its own tests); the "No darks/flats/biases" reset buttons read as status rather than action; a job's batch is not shown, so selecting another batch while a job is displayed shows that batch's (locked) lights under the job summary.
- Rulings: tab order Compose → Export → Astro → Clear (Clear stays last as the destructive tab); no job-listing route — the browser remembers the active job id instead; the Finder "finish" action reveals the job folder rather than converting the TIFF (darktable JPEG export of a stacked TIFF is a separate feature); `/api/status` `astro_output_dir` is the managed outputs path, mirroring `jpeg_output_dir`, so the panel can show manifest/log locations without the API returning per-job paths.

### Task 4 must handle (from Task 3 review) — now implemented

- The web server creates a `Library` per request; `Library.close()` shuts down its runner and would cancel a running job. Task 4 needs one process-level runner (module-level, keyed by database path, or an injected `job_runner`) shared by all request `Library` objects, and `cancel_job` must route to it.
- Status/log-tail must whitelist fields: never serialize `artifact.settings` (contains `command` with absolute paths and `light_fingerprints`), manifests, or job `error` strings containing source basenames without review; job `result_path` is authoritative over status races.
- `run_astro_job` rejects a `pending` placeholder (`load_manifest` ValueError); listings may filter `manifest_path == 'pending'`.
- A manifest tampered between submit and launch is caught by the worker's re-load (untested race).

### Task 3 inherited notes (now implemented)

- Revalidate every frame's size/mtime_ns/sha256 from `load_manifest(path, settings.output_dir)` immediately before launch; `load_manifest` raises `ValueError` for tampered/foreign manifests and may raise `OSError` for unreadable files.
- Refuse to launch if `result.tif` (or stale `process/`) already exists so a rerun can't mistake an old TIFF for success; exit 0 without the TIFF is failure.
- Write the log to `<job>/siril.log`; `prepare_siril_job` raises `FileExistsError` for a reused job ID — map it to a job-conflict error.
- Task 4: never serialize the raw manifest to the browser (it holds absolute source paths and fingerprints).

## Safety and data rules

- Original photo paths are read-only inputs, even during cleanup and astro calibration.
- All job writes must remain below `<library>/outputs/astro/<job-id>/`.
- Explicit photographer confirmation is required before an astro group is prepared.
- Missing dark/flat/bias frames warn but do not block.
- Only one heavy job may run at once on the 48 GB Mac.
- Cancellation targets only the process owned by that job and can never mark success.
- Exit code zero without the declared TIFF is failure.
- Failed/cancelled job manifests and logs remain recoverable.
- Do not download another oMLX model until its oMLX/MLX vision compatibility is validated; loaded status must come from the live status API.

## Project state and known limitations

- The folder is not a Git repository yet. The user intends to push it eventually, but Git initialization still requires explicit approval.
- `vendor/apps/`, `.superpowers/`, local environments, and build outputs are ignored. Do not remove the project-local Siril bundle.
- darktable 5.6.2 is present at `vendor/apps/darktable.app` and `lumen doctor` reports `darktable-cli` available (on `PATH` and via the project-local fallback).
- JPEG metadata (user decision 2026-10-22): keep all EXIF including GPS. darktable-cli read the user's `~/.config/darktable/darktablerc` (empty `metadata_flags`), which stripped all EXIF; Lumen now pins `metadata_flags=5` (EXIF + geotag) and copies the full EXIF and GPS IFDs. Verified on a scratch ARW with injected GPS: every ExifIFD/GPS value identical to darktable's render.
- MakerNote: Pillow alone breaks Sony's absolute MakerNote offsets, so export runs `exiftool -TagsFromFile render.tif -MakerNotes` on the finished JPEG when the render has one; missing/failed exiftool fails the export and publishes nothing. Real export: all 117 Sony tags (shutter count 554), no MakerNote warnings.
- The JPEG's XMP `lumen:export` record omits `command` and `source_fingerprint` and keeps only the profile file name; the catalog and sidecars keep the full settings.
- Sony IFD0 lens-correction params (0x7032/0x7035/0x7037) are deliberately not copied; they describe the RAW, and applying them to a corrected render would double-correct.
- There are historical directories named `-p` and `mkdir` at the project root. They were not removed because their ownership/purpose was not established; do not delete them without checking with the user.
- Do not trust README phrases such as “already-installed model” over live diagnostics; update documentation if implementation changes the truth.

## Useful architecture pointers

- `src/lumen/library.py`: coordinated domain workflows and mutation locking.
- `src/lumen/catalog.py`: SQLite schema and transactional persistence.
- `src/lumen/composition.py`: deterministic candidate geometry and identities.
- `src/lumen/omlx.py`: loopback-only tagging and composition-review transport/parsers.
- `src/lumen/pipelines.py`: darktable/Siril process boundaries and managed-path checks.
- `src/lumen/web.py`: loopback/same-origin local JSON API.
- `src/lumen/static/`: dependency-free accessible UI.
- `docs/ARCHITECTURE.md`: module map plus the managed astro stacking flow (updated in astro Task 5).
- `src/lumen/jobs.py` / `src/lumen/astro.py`: job lifecycle and Siril preparation/execution.
