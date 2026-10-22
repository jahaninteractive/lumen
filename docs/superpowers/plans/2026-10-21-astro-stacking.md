# Managed Astro Stacking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn a confirmed astro group plus optional calibration frames into a reproducible managed Siril job, run it locally, import its successful 16-bit TIFF, and make that result available to the existing finishing/export workflow.

**Architecture:** `astro.py` owns immutable manifests and version-aware Siril script generation; a single-worker local job runner owns process state and cancellation. Catalog jobs and artifacts survive enough state for restart recovery while all job writes stay under `outputs/astro/<job-id>/`.

**Tech Stack:** Python 3.12+, SQLite, Siril CLI, existing Pillow astro metrics, standard-library worker thread/process control, vanilla web UI, unittest.

**Spec:** `docs/superpowers/specs/2026-10-21-finish-compose-astro-housekeeping-design.md`

## Global Constraints

- Astro mode requires explicit photographer confirmation.
- Selected original light/calibration frames are read-only and fingerprinted in the manifest.
- Jobs write only under their managed astro directory.
- Missing calibration types warn but do not block execution.
- Run one heavy job at a time; an oMLX-loaded 48 GB Mac must not be overcommitted.
- Validate generated script syntax against the installed Siril version before implementation is called complete.
- The directory is not yet a Git repository; checkpoints name intended commits only.

## Review Focus

- Spaces and non-ASCII characters in source paths are represented safely without shell interpolation; Task 2 tests this.
- Missing or changed source fingerprints block a stale prepared job; Task 3 tests this.
- Cancellation terminates only the owned Siril process and cannot mark success; Task 3 tests this.
- Restart changes orphaned `running` jobs to `interrupted` and keeps logs/manifests; Task 1 tests this.
- Siril exit code zero without the declared TIFF is failure, not success; Task 3 tests this.

---

### Task 1: Job catalog and single-worker lifecycle

**Files:**
- Modify: `src/lumen/domain.py`
- Modify: `src/lumen/catalog.py`
- Create: `src/lumen/jobs.py`
- Test: `tests/test_catalog.py`
- Create: `tests/test_jobs.py`

**Interfaces:**
- Produces: `Job`, `Catalog.create_job(...)`, `Catalog.update_job(...)`, `Catalog.get_job(...)`, `Catalog.list_jobs(...)`, and `LocalJobRunner.submit/cancel/shutdown`.

- [ ] Write failing tests for valid state transitions, one active heavy job, cancellation, captured progress/log path, and startup recovery from `running` to `interrupted`.
- [ ] Run job/catalog tests; expect missing interfaces.
- [ ] Add `jobs` with `kind`, `status`, `progress`, `manifest_path`, `log_path`, `result_path`, timestamps, and error; implement a single daemon worker with explicit shutdown and owned-process cancellation.
- [ ] Run focused tests with resource warnings as errors; expect PASS and no leaked thread/process.
- [ ] Checkpoint: intended commit `feat: add managed local job runner`.

### Task 2: Reproducible Siril job preparation

**Files:**
- Create: `src/lumen/astro.py`
- Modify: `src/lumen/pipelines.py`
- Modify: `src/lumen/diagnostics.py`
- Create: `tests/test_astro.py`
- Modify: `tests/test_pipelines.py`

**Interfaces:**
- Produces: `AstroFrameSet`, `AstroManifest`, `SirilCapabilities`, `probe_siril(executable)`, and `prepare_siril_job(...) -> AstroManifest`.

- [ ] Record the installed Siril path/version/help output and validate the exact batch command plus script primitives against that version's official/local documentation.
- [ ] Write failing tests for managed job paths, source fingerprints, Unicode/space-safe manifests, selected/excluded lights, optional dark/flat/bias sets, trailing warnings, missing calibration warnings, and version-gated script generation.
- [ ] Run astro/pipeline tests; expect missing module.
- [ ] Implement JSON manifest and script generation with global-star registration and the installed version's supported robust sigma rejection command; never concatenate a shell command.
- [ ] Run focused tests and one generated-script dry validation against installed Siril; expect PASS without processing user photos.
- [ ] Checkpoint: intended commit `feat: prepare reproducible Siril stacks`.

### Task 3: Execute, cancel, and import stack results

**Files:**
- Modify: `src/lumen/astro.py`
- Modify: `src/lumen/jobs.py`
- Modify: `src/lumen/library.py`
- Modify: `src/lumen/catalog.py`
- Modify: `src/lumen/sidecars.py`
- Test: `tests/test_astro.py`
- Test: `tests/test_jobs.py`
- Test: `tests/test_library.py`

**Interfaces:**
- Produces: `Library.prepare_astro_job(...) -> Job`, `Library.run_astro_job(job_id)`, `Library.cancel_job(job_id)`, and successful TIFF `Artifact` records.

- [ ] Write failing tests for changed/missing frames, non-managed script/result paths, mocked successful TIFF import, timeout, nonzero exit, zero exit without TIFF, cancellation, logs, artifact fingerprint, and unchanged source bytes/mtimes.
- [ ] Run focused tests; expect missing library operations.
- [ ] Validate manifest fingerprints immediately before launch, stream Siril output into the managed log, update progress at coarse stages, require the declared 16-bit TIFF, fingerprint/save the artifact, and append group/photo provenance only after success.
- [ ] Run focused tests and expect PASS.
- [ ] Checkpoint: intended commit `feat: run and import managed astro stacks`.

### Task 4: CLI and protected astro API

**Files:**
- Modify: `src/lumen/cli.py`
- Modify: `src/lumen/web.py`
- Test: `tests/test_cli.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Produces: CLI `astro prepare`, `astro run`, `astro status`, `astro cancel`; API prepare/run/status/cancel routes under `/api/astro/jobs`.

- [ ] Write failing tests for explicit group confirmation, calibration folders, selected/excluded light IDs, missing Siril, job conflict, polling, cancellation, and cross-origin mutation rejection.
- [ ] Run CLI/web tests; expect unknown commands/routes.
- [ ] Implement thin CLI/API adapters over `Library` and the singleton job runner; status is read-only and returns progress, warnings, log tail, and result artifact.
- [ ] Run focused tests and expect PASS.
- [ ] Checkpoint: intended commit `feat: expose managed astro jobs`.

### Task 5: Astro web workflow and end-to-end verification

**Files:**
- Modify: `src/lumen/static/index.html`
- Modify: `src/lumen/static/styles.css`
- Modify: `src/lumen/static/app.js`
- Modify: `README.md`
- Modify: `docs/ARCHITECTURE.md`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: Task 4 API.
- Produces: Astro tab with confirmation, light inclusion toggles, calibration-folder browse controls, warnings, recipe summary, progress/cancel, result preview, and finish/export action.

- [ ] Write failing semantic UI tests for explicit astro confirmation, labeled calibration controls, non-color warnings, per-frame inclusion, progress status, cancellation, and successful result action.
- [ ] Run web tests; expect missing controls.
- [ ] Implement the Astro tab using existing batch/photo data; poll only while a job is active, stop polling on terminal states, announce transitions, and retain useful manifest/log links on failure.
- [ ] Document the installed Siril version assumptions, calibration workflow, managed paths, and recovery steps.
- [ ] Run JavaScript syntax check, all unit tests with resource warnings as errors, CLI doctor, and one tiny generated-fixture Siril smoke stack when installed; expect all applicable checks pass and skipped optional checks are explicit.
- [ ] Checkpoint: intended commit `feat: add guided local astro stacking`.
