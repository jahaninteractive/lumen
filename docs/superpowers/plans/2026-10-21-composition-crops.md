# Composition Crop Suggestions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate safe non-destructive crop candidates, let the photographer approve or clear one, optionally obtain an oMLX critique, and apply the approved crop during JPEG export.

**Architecture:** macOS Vision supplies normalized face and attention-saliency rectangles; a deterministic Python engine creates bounded crops before oMLX ranks valid candidates. The active crop is stored in SQLite and sidecars, while the export pipeline consumes normalized coordinates at full resolution.

**Tech Stack:** Swift Vision, Python 3.12+, Pillow, SQLite, local oMLX, vanilla web UI, unittest.

**Spec:** `docs/superpowers/specs/2026-10-21-finish-compose-astro-housekeeping-design.md`

## Global Constraints

- Cropping is non-destructive and never changes the source or cached original preview.
- No generative fill or AI upscaling is allowed.
- Show original, “subject in scene,” and “tighter subject”; the user remains authoritative.
- Reject candidates below 8 MP and warn below 12 MP.
- oMLX may rank only deterministic in-bounds candidates and may not supply final coordinates.
- The directory is not yet a Git repository; checkpoints name intended commits only.

## Review Focus

- Vision rectangles use bottom-left coordinates while Pillow uses top-left; Task 1 tests conversion.
- Edge subjects never produce negative/out-of-bounds crops; Task 2 tests all four corners.
- Portrait/landscape inputs retain exact requested aspect ratios within pixel rounding; Task 2 tests both.
- Weak/no saliency produces only original and centered fallback, not invented rationale; Task 2 tests this.
- Malformed or unavailable oMLX critique leaves deterministic candidates usable; Task 4 tests this.

---

### Task 1: Vision subject-region output

**Files:**
- Modify: `tools/vision_analyzer.swift`
- Modify: `src/lumen/analysis.py`
- Test: `tests/test_analysis.py`

**Interfaces:**
- Produces: vision schema version 2 with `image_width`, `image_height`, `faces[]`, and `saliency_objects[]`; `analyze_subject_regions(...) -> SubjectRegions | None`.

- [ ] Write failing parser tests for valid normalized rectangles, invalid bounds/types, schema 1 backward compatibility for scoring, and Vision-to-image coordinate conversion.
- [ ] Run `uv run python -m unittest tests.test_analysis -v`; expect failure for missing region interfaces.
- [ ] Extend the Swift helper with `VNGenerateAttentionBasedSaliencyImageRequest`, face rectangles, and image dimensions; keep existing face/eye quality fields.
- [ ] Implement strict Python parsing that drops invalid optional observations but rejects an invalid envelope.
- [ ] Compile the helper with `zsh scripts/bootstrap.sh`, run analysis tests, and expect PASS.
- [ ] Checkpoint: intended commit `feat: detect composition subject regions`.

### Task 2: Deterministic crop candidate engine

**Files:**
- Create: `src/lumen/composition.py`
- Test: `tests/test_composition.py`

**Interfaces:**
- Consumes: `SubjectRegions` from Task 1.
- Produces: `CropRect`, `CropCandidate`, and `generate_candidates(width: int, height: int, regions: SubjectRegions | None) -> list[CropCandidate]`.

- [ ] Write failing tests for subject-in-scene and tight candidates, original/3:2/4:5/1:1 ratios, corner subjects, face padding, 8 MP rejection, 12 MP warning, duplicate removal, and no-region centered fallback.
- [ ] Run `uv run python -m unittest tests.test_composition -v`; expect import failure.
- [ ] Implement normalized rectangles with clamp-and-shift behavior: preserve aspect ratio, contain padded evidence, maximize retained pixels, deduplicate near-identical candidates, and return at most two primary suggestions plus revealable alternatives.
- [ ] Run composition tests and expect PASS.
- [ ] Checkpoint: intended commit `feat: generate safe crop suggestions`.

### Task 3: Persist crop decisions and apply them on export

**Files:**
- Modify: `src/lumen/domain.py`
- Modify: `src/lumen/catalog.py`
- Modify: `src/lumen/library.py`
- Modify: `src/lumen/sidecars.py`
- Modify: `src/lumen/pipelines.py`
- Test: `tests/test_catalog.py`
- Test: `tests/test_library.py`
- Test: `tests/test_pipelines.py`

**Interfaces:**
- Produces: `PhotoEdit`, `Catalog.save_crop(...)`, `Catalog.clear_crop(...)`, `Library.crop_candidates(photo_id)`, `Library.choose_crop(photo_id, candidate_id)`, and crop-aware export.

- [ ] Write failing tests for crop round-trip, candidate-ID validation, clear-with-history, sidecar coordinates/author/rationale, exact full-resolution pixel crop, source preservation, and output dimension/aspect ratio.
- [ ] Run the three focused test modules; expect missing edit APIs.
- [ ] Add `photo_edits` with one active crop per photo and append provenance for selection/clear.
- [ ] Wire candidate generation through `Library`, include edit data in sidecars, and apply normalized crop after full-resolution development but before final JPEG encoding.
- [ ] Run focused tests and expect PASS.
- [ ] Checkpoint: intended commit `feat: persist and export approved crops`.

### Task 4: Optional oMLX composition critique

**Files:**
- Modify: `src/lumen/omlx.py`
- Modify: `src/lumen/library.py`
- Test: `tests/test_omlx.py`
- Test: `tests/test_library.py`

**Interfaces:**
- Produces: `CompositionReview(candidate_id: str, rationale: str, confidence: float)`, `OmlxTagger.review_composition(...)`, and `Library.review_composition(photo_id, tagger)`.

- [ ] Write failing tests that provide original/candidate previews and assert the model may return only a supplied candidate ID, rationale is bounded text, malformed JSON is preserved in failure provenance, and model failure leaves candidates unchanged.
- [ ] Run focused tests; expect missing review interfaces.
- [ ] Implement one local multimodal request with labeled candidates and strict JSON parsing; validate candidate ID membership before persistence.
- [ ] Run focused tests and expect PASS.
- [ ] Checkpoint: intended commit `feat: add local composition critique`.

### Task 5: Compose web workflow

**Files:**
- Modify: `src/lumen/web.py`
- Modify: `src/lumen/static/index.html`
- Modify: `src/lumen/static/styles.css`
- Modify: `src/lumen/static/app.js`
- Modify: `README.md`
- Test: `tests/test_web.py`

**Interfaces:**
- Produces: GET `/api/photos/{id}/crops`; POST `/api/photos/{id}/crop`, `/crop/clear`, and `/composition-review`; accessible Compose tab.

- [ ] Write failing API and semantic-control tests for candidate retrieval, allowed-ID selection, clearing, local AI failure, keyboard-operable candidate buttons, megapixel warning text, and original fallback.
- [ ] Run `uv run python -m unittest tests.test_web -v`; expect missing routes/controls.
- [ ] Implement crop preview overlays with thirds lines, “Use this crop,” “Keep original,” and optional “Ask local AI”; announce every selection and preserve selected photo across tabs.
- [ ] Update README with truthful saliency limits and crop/export interaction.
- [ ] Run `node --check src/lumen/static/app.js` and the full unittest suite; expect all pass.
- [ ] Checkpoint: intended commit `feat: add non-destructive composition workflow`.

