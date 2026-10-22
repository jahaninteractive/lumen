# JPEG Export and Housekeeping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Export one photo or every group keeper as a provenance-tracked JPEG, then safely remove a group or reset Lumen's working catalog without touching originals or completed outputs.

**Architecture:** Extend the existing catalog with derived artifacts, keep darktable invocation in `pipelines.py`, and expose operations through the `Library` facade. Cleanup first snapshots the catalog and moves Lumen-managed working files into timestamped library trash; HTTP and CLI use the same operations.

**Tech Stack:** Python 3.12+, SQLite, Pillow, darktable-cli, vanilla HTML/CSS/JavaScript, unittest.

**Spec:** `docs/superpowers/specs/2026-10-21-finish-compose-astro-housekeeping-design.md`

## Global Constraints

- Never modify, rename, move, or delete a source photograph.
- Export JPEG quality is 92, output color space is sRGB, and GPS is omitted.
- Exports stay under `<library>/outputs/jpeg/` and are never overwritten silently.
- Cleanup moves recoverable working data to `<library>/trash/<UTC-timestamp>/` and keeps completed exports.
- External commands are argument arrays and remain inside managed output paths.
- HTTP mutations retain existing loopback Host and Origin protection.
- The directory is not yet a Git repository; do not initialize Git without user authorization. Treat each commit step as a checkpoint until Git exists.

## Review Focus

- Two exports with the same filename produce distinct output paths rather than overwriting; Task 2 tests this.
- A darktable failure leaves no successful artifact or partial JPEG; Task 2 tests this.
- Two photos sharing a preview fingerprint retain that preview until both are removed; Task 3 tests this.
- Cleanup failure restores staged managed files and leaves catalog rows usable; Task 3 tests this.
- A forged cross-origin clear/export request is rejected before mutation; Task 4 tests this.

---

### Task 1: Artifact catalog records

**Files:**
- Modify: `src/lumen/domain.py`
- Modify: `src/lumen/catalog.py`
- Test: `tests/test_catalog.py`

**Interfaces:**
- Produces: `Artifact`, `Catalog.save_artifact(...) -> Artifact`, `Catalog.list_artifacts() -> list[Artifact]`, `Catalog.group_photo_ids(group_id: int) -> list[int]`, and transactional deletion helpers.

- [ ] **Step 1: Write failing artifact and migration tests**

Add `test_artifact_survives_source_row_deletion_with_denormalized_identity` and `test_existing_catalog_gains_artifacts_table` asserting `photo_id` becomes `None` while source name, source fingerprint, path, settings, and output fingerprint remain.

- [ ] **Step 2: Run the catalog tests and verify failure**

Run: `uv run python -m unittest tests.test_catalog -v`

Expected: FAIL because artifact interfaces do not exist.

- [ ] **Step 3: Add the artifact schema and typed record**

Implement `Artifact` and an `artifacts` table whose optional `photo_id` uses `ON DELETE SET NULL`; store `source_name`, `source_fingerprint`, `kind`, unique `path`, `settings_json`, `output_fingerprint`, and `created_at`. Add group/photo deletion helpers that require explicit IDs and one transaction.

- [ ] **Step 4: Run tests and verify pass**

Run: `uv run python -m unittest tests.test_catalog -v`

Expected: PASS.

- [ ] **Step 5: Checkpoint**

Intended commit: `feat: track derived export artifacts`

### Task 2: Reproducible JPEG export

**Files:**
- Modify: `src/lumen/config.py`
- Modify: `src/lumen/pipelines.py`
- Modify: `src/lumen/library.py`
- Modify: `src/lumen/sidecars.py`
- Test: `tests/test_pipelines.py`
- Test: `tests/test_library.py`

**Interfaces:**
- Consumes: `Catalog.save_artifact(...)` from Task 1 and existing `DarktablePipeline.run(...)`.
- Produces: `ExportRequest(photo_ids: tuple[int, ...], profile: Path | None)`, `ExportResult`, `Library.export_jpegs(request: ExportRequest) -> list[ExportResult]`, and `Library.keeper_ids() -> list[int]`.

- [ ] **Step 1: Write failing export tests**

Add tests asserting quality `92`, managed `outputs/jpeg` paths, numeric collision suffixes, no partial artifact on runner failure, source bytes/mtime unchanged, explicit profile propagation, GPS omission, useful EXIF/description preservation, and export provenance in JSON sidecars.

- [ ] **Step 2: Run focused tests and verify failure**

Run: `uv run python -m unittest tests.test_pipelines tests.test_library -v`

Expected: FAIL because JPEG export interfaces do not exist.

- [ ] **Step 3: Implement export preparation and metadata policy**

Add `Settings.jpeg_output_dir`. Extend the pipeline to create a full-resolution temporary darktable render, apply any future normalized crop through Pillow, convert/save JPEG with quality 92 and sRGB, copy approved EXIF/descriptive fields, remove GPS, fingerprint the output, and atomically place it at a collision-safe path.

- [ ] **Step 4: Implement library orchestration**

Validate every photo ID before starting; export serially; save successful artifacts and `exported` provenance only after the final JPEG exists; write updated sidecars. A failed item returns a failure result and leaves other requested photos processable.

- [ ] **Step 5: Run focused tests and verify pass**

Run: `uv run python -m unittest tests.test_pipelines tests.test_library -v`

Expected: PASS.

- [ ] **Step 6: Checkpoint**

Intended commit: `feat: export selected RAW photos as JPEG`

### Task 3: Recoverable group and workspace cleanup

**Files:**
- Modify: `src/lumen/config.py`
- Modify: `src/lumen/catalog.py`
- Modify: `src/lumen/library.py`
- Test: `tests/test_catalog.py`
- Test: `tests/test_library.py`

**Interfaces:**
- Produces: `CleanupResult`, `Library.remove_group(group_id: int) -> CleanupResult`, and `Library.clear_workspace(confirmation: str) -> CleanupResult`.

- [ ] **Step 1: Write failing cleanup tests**

Add tests that import originals outside the library, remove one group, and clear all groups. Assert originals and outputs remain byte-for-byte unchanged; catalog rows disappear; previews/sidecars move to timestamped trash; shared previews remain while referenced; a catalog snapshot exists; `confirmation != "CLEAR"` rejects global cleanup; and an injected move/database failure restores staged files and usable rows.

- [ ] **Step 2: Run focused tests and verify failure**

Run: `uv run python -m unittest tests.test_catalog tests.test_library -v`

Expected: FAIL because cleanup interfaces do not exist.

- [ ] **Step 3: Implement recoverable cleanup**

Add `Settings.trash_dir`. Build an explicit manifest of managed preview/sidecar/job files, back up SQLite into the trash timestamp, stage file moves with reversible source/destination pairs, delete only the selected rows in one catalog transaction, restore staged paths on failure, and never enumerate or mutate source paths.

- [ ] **Step 4: Run focused tests and verify pass**

Run: `uv run python -m unittest tests.test_catalog tests.test_library -v`

Expected: PASS.

- [ ] **Step 5: Checkpoint**

Intended commit: `feat: add recoverable workspace cleanup`

### Task 4: CLI and protected HTTP operations

**Files:**
- Modify: `src/lumen/cli.py`
- Modify: `src/lumen/web.py`
- Test: `tests/test_cli.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: export and cleanup operations from Tasks 2–3.
- Produces: CLI `export`, `remove`, and `clear`; POST `/api/export`, `/api/groups/{id}/remove`, and `/api/library/clear`.

- [ ] **Step 1: Write failing CLI/API tests**

Test current-photo and all-keepers export payloads, exact `CLEAR` confirmation, missing IDs, unavailable darktable, non-overwrite output names, and cross-origin rejection for all three mutation routes.

- [ ] **Step 2: Run interface tests and verify failure**

Run: `uv run python -m unittest tests.test_cli tests.test_web -v`

Expected: FAIL with unknown commands/routes.

- [ ] **Step 3: Implement CLI commands and API routes**

Map interface inputs directly to `Library`; return JSON-safe result records and HTTP 400 for validation/tool failures. Do not duplicate export or cleanup behavior in either interface.

- [ ] **Step 4: Run interface tests and verify pass**

Run: `uv run python -m unittest tests.test_cli tests.test_web -v`

Expected: PASS.

- [ ] **Step 5: Checkpoint**

Intended commit: `feat: expose export and cleanup operations`

### Task 5: Export and cleanup web UI

**Files:**
- Modify: `src/lumen/static/index.html`
- Modify: `src/lumen/static/styles.css`
- Modify: `src/lumen/static/app.js`
- Modify: `README.md`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: Task 4 routes.
- Produces: Export and Clear tabs, current/all-keepers controls, XMP file browser, result paths, Finder reveal, group removal, and typed global confirmation.

- [ ] **Step 1: Write failing semantic UI tests**

Extend the HTML parser tests to assert labeled tab controls, explicit button types, an accessible confirmation field, status live regions, and no destructive action inside the primary export control group.

- [ ] **Step 2: Run web tests and verify failure**

Run: `uv run python -m unittest tests.test_web -v`

Expected: FAIL because controls are absent.

- [ ] **Step 3: Implement the UI**

Preserve the contact-sheet design. Export shows quality 92, sRGB, full resolution, profile path, and destination; cleanup states exact counts, keeps exports, requires `CLEAR`, reports trash location, and reloads the group list. Finder reveal uses a same-origin POST that validates the managed directory before invoking macOS `open` with an argument array.

- [ ] **Step 4: Document the workflow**

Update README with UI and equivalent CLI steps, output/trash locations, GPS policy, and darktable-unavailable recovery.

- [ ] **Step 5: Run full verification**

Run: `node --check src/lumen/static/app.js`

Run: `uv run python -W error::ResourceWarning -m unittest discover -s tests -v`

Expected: JavaScript check succeeds and all tests pass without resource warnings.

- [ ] **Step 6: Checkpoint**

Intended commit: `feat: finish and clear shoots from the web UI`

