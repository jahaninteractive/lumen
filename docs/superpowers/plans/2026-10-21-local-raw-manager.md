# Local RAW Photography Manager Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a runnable, tested, fully local Sony RAW manager with previews, grouping, explainable best-shot selection, local oMLX tagging, sidecars, astro checks, processing hooks, CLI, and accessible UI.

**Architecture:** A dependency-light Python package uses SQLite as the catalog and a `Library` facade over focused domain modules. Pillow and macOS-native tools provide image analysis; oMLX and editing applications are optional loopback/command adapters. A standard-library HTTP server serves a static contact-sheet interface and JSON API.

**Tech Stack:** Python 3.12+, SQLite, Pillow, macOS Vision (Swift helper), ExifTool, optional darktable/Siril, vanilla HTML/CSS/JavaScript, `unittest`.

**Spec:** `docs/superpowers/specs/2026-10-21-local-raw-manager-design.md`

## Global Constraints

- Never change, rename, move, or delete source photographs.
- Bind networking to `127.0.0.1`; oMLX calls must use a loopback URL.
- Use subprocess argument arrays without a shell.
- Persist generated data only under the configured library directory.
- Python 3.12+ and Apple Silicon macOS are the supported runtime.
- Missing optional tools degrade visibly and safely.

## Review Focus

- Reimporting the same file must update rather than duplicate it.
- Files with missing/invalid EXIF dates must still import and group deterministically.
- Malformed oMLX JSON must not create misleading tags.
- HTTP asset/preview paths must not escape approved roots.
- A failing external command must leave sidecars/catalog valid and originals unchanged.

---

### Task 1: Catalog, domain records, and provenance

**Files:**
- Create: `pyproject.toml`
- Create: `src/lumen/config.py`
- Create: `src/lumen/catalog.py`
- Create: `src/lumen/domain.py`
- Test: `tests/test_catalog.py`

**Interfaces:**
- Produces: `Settings`, `Catalog`, `Photo`, `PhotoMetrics`, `append_event()` and schema version 1.

- [ ] Write tests for schema creation, idempotent photo upsert, and ordered provenance.
- [ ] Run the tests and confirm missing-package/module failures.
- [ ] Implement the minimal records and SQLite catalog.
- [ ] Run the tests and confirm they pass.

### Task 2: Preview, metadata, measurements, grouping, and selection

**Files:**
- Create: `src/lumen/tools.py`
- Create: `src/lumen/previews.py`
- Create: `src/lumen/analysis.py`
- Create: `src/lumen/grouping.py`
- Create: `src/lumen/scoring.py`
- Create: `tools/vision_analyzer.swift`
- Test: `tests/test_analysis.py`
- Test: `tests/test_grouping.py`
- Test: `tests/test_scoring.py`

**Interfaces:**
- Consumes: `Photo`, `PhotoMetrics`, `Settings`.
- Produces: `ExifToolPreviewProvider`, `measure_image()`, `analyze_astro()`, `TimeGapGroupingPolicy.group()`, and `rank_group()`.

- [ ] Write tests using generated JPEG fixtures for hand-derived exposure/sharpness behavior, astro bounds, grouping gaps, missing dates, and weight renormalization.
- [ ] Run them and confirm failures because implementations are absent.
- [ ] Implement bounded preview generation and deterministic analysis/grouping/scoring.
- [ ] Compile the Swift helper and verify it returns versioned JSON or a clear unavailable result.
- [ ] Run all task tests and confirm they pass.

### Task 3: Portable sidecars and library workflow

**Files:**
- Create: `src/lumen/sidecars.py`
- Create: `src/lumen/library.py`
- Test: `tests/test_library.py`
- Test: `tests/test_sidecars.py`

**Interfaces:**
- Consumes: catalog, preview, analysis, grouping, scoring interfaces.
- Produces: `SidecarWriter.write()`, `Library.import_path()`, `Library.list_groups()`, and `Library.get_photo()`.

- [ ] Write tests for atomic JSON/XMP output, import idempotency, unsupported files, and unchanged source bytes/mtime.
- [ ] Run and confirm expected missing behavior.
- [ ] Implement the import transaction and sidecar synchronization.
- [ ] Run all task tests and confirm they pass.

### Task 4: oMLX tagging and non-destructive processing adapters

**Files:**
- Create: `src/lumen/omlx.py`
- Create: `src/lumen/pipelines.py`
- Test: `tests/test_omlx.py`
- Test: `tests/test_pipelines.py`

**Interfaces:**
- Consumes: preview paths and photo records.
- Produces: `OmlxTagger.tag()`, `parse_tag_response()`, `PipelineRegistry`, and capability/result records.

- [ ] Write tests for fenced/clean/malformed model JSON, non-loopback rejection, capability detection, exact command arguments, and failed-command provenance.
- [ ] Run and confirm expected failures.
- [ ] Implement the local VLM adapter and darktable/Siril-safe hooks.
- [ ] Run all task tests and confirm they pass.

### Task 5: CLI, doctor, HTTP API, and accessible contact sheet

**Files:**
- Create: `src/lumen/cli.py`
- Create: `src/lumen/web.py`
- Create: `src/lumen/static/index.html`
- Create: `src/lumen/static/styles.css`
- Create: `src/lumen/static/app.js`
- Test: `tests/test_cli.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `Library`, `OmlxTagger`, `PipelineRegistry`.
- Produces: `lumen` command, loopback HTTP server, `/api/status`, `/api/groups`, `/api/photos/{id}`, and protected preview delivery.

- [ ] Write CLI/API tests, including traversal attempts and missing optional tools.
- [ ] Run and confirm expected failures.
- [ ] Implement commands, APIs, and the responsive semantic UI.
- [ ] Run all task tests and confirm they pass.

### Task 6: Installation, fixtures, smoke test, and documentation

**Files:**
- Create: `README.md`
- Create: `docs/MODELS.md`
- Create: `docs/ARCHITECTURE.md`
- Create: `scripts/bootstrap.sh`
- Create: `scripts/smoke_test.sh`
- Create: `.gitignore`

**Interfaces:**
- Consumes: complete application.
- Produces: one-command setup, documented oMLX validation, sample workflow, extension guide, and local smoke test.

- [ ] Document Homebrew prerequisites, `uv` setup, actual Qwen3.8 validation, no-download recommendation, darktable/Siril installation, and commands.
- [ ] Add bootstrap and smoke-test scripts that exercise behavior rather than source text.
- [ ] Run the full unit suite, package install, Swift compile, CLI doctor, and smoke test.
- [ ] Re-read the spec and record any gaps or deliberate limits.

