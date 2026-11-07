# Astro Debrief Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every astro job that ran gets a debrief (what happened, what to try now, exact settings for next
time for the chosen shooting place) computed by rules with no AI, plus an optional model note from the stacked
preview; the job folder drops Siril's `process/` and names its outputs from the EXIF.

**Architecture:** A new module `src/lumen/astrodebrief.py` holds everything that reads a finished job: the
Siril log parser, the FITS header reader, `frames.json`, the output stem and the debrief rules. `astro.py`
calls it at the end of `execute_job` (frames.json, clean-up); `library.py` renames outputs before recording the
artifact and serves the debrief and the model note; `web.py` adds two routes; `app.js`/`index.html` add the
place select and the debrief section.

**Tech Stack:** Python 3 stdlib + Pillow, exiftool (already required), vanilla JS, `unittest`.

**Spec:** `docs/superpowers/specs/2026-11-07-astro-debrief-design.md` (read it first; this plan argues from
it). Real Siril logs: `<library>/outputs/astro/163`, `165`, `166` (`siril.log`) and
`docs/superpowers/specs/2026-11-07-astro-debrief-untracked-siril.log`.

## Global Constraints

- Pillow is the only runtime Python dependency; no network; no new services.
- Never edit, move or delete camera originals. `lights/` holds symlinks to them: never follow a symlink for a
  delete or rename. Every delete is inside `<library>/outputs/astro/<job>/`.
- Browser payloads carry no absolute paths, fingerprints or raw model output.
- Mutation routes keep host/origin and strict JSON validation (follow `/api/photos/<id>/develop/suggest`).
- Model calls run with the mutation lock released; the lock is taken only to append the event.
- Tests never contact oMLX (patch `lumen.library.OmlxTagger`, `lumen.library.get_server_status`) and never
  read the real library. In astro fixtures `subprocess.Popen` is patched globally, so any exiftool call must be
  stubbed (`patch("lumen.astro.find_tool", return_value=None)` or an injected runner), or it launches the fake
  Siril in the repo root.
- Run tests with `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest ...`; `node --check src/lumen/static/app.js`.
- No AI vendor or model names in tracked files or commit messages; no co-author trailers.
- Version 0.4.0 in `src/lumen/__init__.py` and `pyproject.toml` together, one `CHANGELOG.md` entry, a
  `docs/HANDOFF.md` "Resume here" entry and an `AGENTS.md` Current tool state line (Task 6).
- Branch `feature/astro-debrief` from the current tree. The uncommitted 0.3.3 astro work (preview, metadata
  copy, reveal) is its first commit: `fix: astro stacks write an auto-stretched preview, carry the first light's
  EXIF and reveal the result; release 0.3.3`.

## Review Focus

1. A failed job (165) and a job whose Siril died before `convert` finished: the debrief still answers, with log
   rules only, and never 500s. Owned by Task 3 (`test_debrief_without_frames_uses_log_rules_only`).
2. `process/` clean-up must never touch anything but `<job>/process` (a symlinked `process`, or a job dir
   outside outputs, is refused). Task 2 (`test_remove_process_refuses_a_symlink`).
3. Rename collisions and missing fields: no overwrite, outputs keep Siril names; a frame set with no ISO drops
   the field. Task 2 (`test_output_stem_drops_missing_fields`, `test_rename_refuses_an_existing_name`).
4. The source drive unplugged: `frames.json` was written while it was online, so SteadyShot and max aperture
   still show; if it was offline at write time, the one "originals offline" line appears. Task 2/3.
5. oMLX off: debrief renders fully, button disabled with the reason; a bad model answer stores
   `astro_review_failed` and the debrief is unchanged. Task 5 (`test_review_failure_leaves_debrief_intact`).

---

### Task 1: Siril log parser

**Files:**
- Create: `src/lumen/astrodebrief.py`
- Create: `tests/fixtures/siril/tracked-ok.log`, `tracked-skipped.log`, `untracked-ok.log`
- Test: `tests/test_astrodebrief.py`

Fixtures: the registration and stack sections of jobs 166 (`tracked-ok`), 165 (`tracked-skipped`) and the
spec's untracked log, ANSI stripped, every absolute path replaced with `<job>`. Keep each under ~200 lines.

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True, slots=True)
  class FrameMatch:  # one "Matching stars in image k: done" block
      image: int; pairs: int | None; rotation: float | None; dx: float | None; dy: float | None
      fwhm: float | None; roundness: float | None

  @dataclass(frozen=True, slots=True)
  class SirilRun:
      recipe: str                      # "tracked" | "untracked" (two-pass method line)
      reference: int | None            # 1 for tracked, N from "choosing image N" for untracked
      reference_stars: int | None
      reference_fwhm_xy: tuple[float, float] | None   # tracked only
      star_counts: tuple[int, ...]     # every "Found N ... stars in image" line, unordered
      matches: tuple[FrameMatch, ...]
      skipped: tuple[tuple[int, int], ...]            # (image, pairs) from "Not enough star pairs"
      registered: int | None; failed: int | None
      aborted: bool                    # "Registration aborted" or "No image was registered"
      stacked: int | None              # "N images have been stacked"
      succeeded: bool                  # "Script execution finished successfully"

  def parse_siril_log(text: str) -> SirilRun
  ```

- [ ] **Step 1: Write failing tests** `test_parses_tracked_success`, `test_parses_tracked_all_skipped`,
  `test_parses_untracked_reference_choice`:
  ```python
  run = parse_siril_log(fixture("tracked-skipped.log"))
  assert (run.recipe, run.reference, run.reference_stars) == ("tracked", 1, 26)
  assert run.reference_fwhm_xy == (13.40, 9.72)
  assert run.skipped == ((2, 7), (3, 7), (4, 6)) and run.aborted and not run.succeeded
  assert sorted(run.star_counts) == [26, 59, 62, 69]   # reference included

  run = parse_siril_log(fixture("tracked-ok.log"))
  assert (run.registered, run.failed, run.stacked, run.succeeded) == (4, 0, 4, True)
  assert [m.image for m in run.matches] == [2, 3, 4] and run.matches[0].roundness == 0.73

  run = parse_siril_log(fixture("untracked-ok.log"))
  assert (run.recipe, run.reference, run.stacked) == ("untracked", 1, 3)
  assert run.reference_fwhm_xy is None and {m.image for m in run.matches} == {2, 3}
  ```
  Also `test_ignores_ansi_and_garbage`: `parse_siril_log("\x1b[31mlog: x\x1b[0m\nnonsense")` returns a run with
  every field empty/None/False and no exception.
- [ ] **Step 2:** run `uv run python -W error -m unittest tests.test_astrodebrief -v`; expect ImportError.
- [ ] **Step 3:** implement with one regex per line kind (reuse `astro._ANSI` by importing it). A `FrameMatch`
  starts at `Matching stars in image k` and takes the following field lines until the next block or a
  non-field line.
- [ ] **Step 4:** tests pass.
- [ ] **Step 5:** commit `feat: parse what Siril's log says about registration and stacking`.

### Task 2: frames.json, process clean-up, output names

**Files:**
- Modify: `src/lumen/astrodebrief.py`, `src/lumen/astro.py` (`execute_job`, `AstroExecution`),
  `src/lumen/library.py` (`_record_astro_result`, `job_status` result block), `src/lumen/web.py`
  (`reveal_astro_job`)
- Test: `tests/test_astrodebrief.py`, `tests/test_astro.py`, `tests/test_web.py`

**Interfaces:**
- Consumes: `astro.copy_metadata`, `astro.find_tool`.
- Produces:
  ```python
  def read_fits_header(path: Path) -> dict[str, str | float | int | bool]   # 2880-byte blocks, stop at END
  def write_frames_json(job: Path, manifest, *, runner=subprocess.run) -> dict
      # -> {"frames": [{"position": 1, "name": "light_00001.arw", "date_obs": "...", "exposure": 20.0,
      #     "iso": 3200, "aperture": 4.0, "focal": 28.0, "pixel_um": 5.07, "instrument": "Sony ILCE-7CM2",
      #     "stabilization": "On" | None, "max_aperture": 4.0 | None}], "originals_online": bool}
  def remove_process(job: Path) -> str            # log line; never raises
  def output_stem(frames: list[dict], captured_at: datetime | None) -> str | None
  def rename_outputs(job: Path, stem: str) -> tuple[Path, Path | None]   # (tif, jpg); refuses existing names
  ```
  `AstroExecution` gains `preview: Path | None`. The `astro_tiff` artifact's `settings` gains
  `"preview": "<stem>.jpg"` (or `"preview.jpg"` if not renamed, absent if no preview); `job_status` reads
  `result.preview` from there instead of checking the folder.

- [ ] **Step 1: Write failing tests** in `test_astrodebrief.py`:
  - `test_read_fits_header`: write a minimal FITS header (80-char cards, `EXPTIME = 20.`, `INSTRUME= 'Sony ILCE-7CM2'`,
    `XPIXSZ = 5.07000017166138`, `END`, padded to 2880) and assert the parsed values.
  - `test_write_frames_json_reads_headers_and_exif`: two `process/light_0000N.fit` headers, a runner returning
    exiftool `-json -n` with `ImageStabilization: 1`/`MaxApertureValue: 4`; assert the dict above and that
    `<job>/frames.json` holds it. A third staged light with no `.fit` is absent.
  - `test_write_frames_json_marks_originals_offline` when the runner fails or `find_tool` is None:
    `originals_online` False, `stabilization` and `max_aperture` None.
  - `test_output_stem`: frames 20/13/15/15 s, 28 mm, f/4, ISO 3200, `Sony ILCE-7CM2`, captured
    `2026-10-18 21:29:40` → `"2026-10-18_2129_ILCE-7CM2_28mm_f4_4x13-20s_ISO3200"`; four frames at 15 s →
    `..._4x15s_...`; f/2.8 → `f2.8`.
  - `test_output_stem_drops_missing_fields`: no ISO and no instrument → `"2026-10-18_2129_28mm_f4_4x15s"`;
    no frames → None.
  - `test_remove_process_refuses_a_symlink` (`process` a symlink to a temp dir: target untouched, line says
    refused) and `test_remove_process_deletes_only_process` (`masters/`, `lights/` and `frames.json` stay).
  - `test_rename_refuses_an_existing_name`: `<stem>.tif` already present → `ValueError`, nothing renamed.
- [ ] **Step 2:** run, expect failures.
- [ ] **Step 3:** implement. `write_frames_json` makes one exiftool call `[-json, -n, -ImageStabilization,
  -MaxApertureValue, *staged lights]` (timeout 60). `output_stem` strips the leading make (first word) from
  `INSTRUME`, formats numbers with `f"{value:g}"`, filters each field to `[A-Za-z0-9.-]`.
- [ ] **Step 4: Wire into `execute_job`.** After Siril has exited (returncode not None), in a `finally` that
  covers the timeout, nonzero and missing-result raises and the cancelled return: `note(...)` the outcome of
  `write_frames_json` (failures noted, never raised) then `note(remove_process(job))`. Ordering on success:
  `verify_tiff`, `copy_metadata`, then frames.json and clean-up. Test in `test_astro.py`:
  `test_execute_job_writes_frames_and_removes_process` (fake Siril creates `process/light_00001.fit` with a
  header and `process/r_light_00001.fit`; after success `process/` is gone, `frames.json` lists frame 1) and
  the same assertion for scenario `nonzero`. Update `FAKE_SIRIL` to create `process/light_0000N.fit` headers.
- [ ] **Step 5: Wire into `_record_astro_result`.** Before `save_artifact`: stem from `frames.json` and the
  first light's `captured_at` (catalog photo by manifest source); `rename_outputs`; a `ValueError` is a log
  line and the Siril names stay. The artifact path is the renamed TIFF. `reveal_astro_job` reveals the
  artifact's path (look it up like `job_status` does) instead of hard-coding `result.tif`. Update
  `test_run_then_poll_reports_progress_log_tail_and_result_artifact` to expect the stem names in
  `finished["result"]` and in the `open -R` argument.
- [ ] **Step 6:** `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_astrodebrief tests.test_astro tests.test_web tests.test_cli -v`
  passes, and `git status --short` shows no stray files in the repo root.
- [ ] **Step 7:** commit `feat: astro jobs keep frames.json, drop Siril's process folder and name outputs from the EXIF`.

### Task 3: Debrief rules

**Files:**
- Modify: `src/lumen/astrodebrief.py`
- Test: `tests/test_astrodebrief.py`

**Interfaces:**
- Consumes: `SirilRun`, the `frames.json` dict, `manifest` (lights, darks/flats/biases counts, recipe).
- Produces:
  ```python
  PLACES = ("dark_site", "city", "ship", "handheld")
  def npf_seconds(aperture: float, focal_mm: float, pixel_um: float) -> float
  def debrief(run: SirilRun, frames: dict | None, manifest, place: str) -> dict
      # {"place", "happened": [str], "try_now": [str], "next_time": {"settings": str, "reasons": [str]}}
  ```
  All thresholds are module constants named after the spec: `ODD_STARS_RATIO = 0.5`,
  `ODD_EXPOSURE = 0.25`, `GAP_SECONDS = 120`, `MIXED_EXPOSURE = 1.25`, `SHARP_FWHM_PX = 6`,
  `ROUNDNESS_MIN = 0.8`, `FWHM_XY_MAX = 1.25`, `DRIFT_PX = 20`, `DRIFT_DEG = 0.5`, `FEW_LIGHTS = 10`,
  `SHIP_MAX_S = 3`, `HANDHELD_S = 0.5`, and the per-place table (ISO, lights, darks, also) as one dict.

- [ ] **Step 1: Write failing tests** (frames dicts built inline from the jobs' real values; manifest a
  `SimpleNamespace`):
  - `test_npf`: `round(npf_seconds(4, 28, 5.07), 2) == 4.99`.
  - `test_failed_job_165`: run from `tracked-skipped.log`, frames 6/13/15/13 s at 21:23:43, 21:26:31,
    21:26:59, 21:27:23. `happened` mentions frames 2, 3 and 4 skipped with 6-7 shared stars, that nothing was
    stacked, and frame 1 as the odd one out naming all three reasons (26 stars against a median of 62; 6 s
    against 13 s; 2 min 48 s before frame 2). `try_now[0]` starts `"Exclude frame 1 (light_00001.arw)"`.
  - `test_next_time_per_place` on job 163's frames (20/13/15/15 s, f/4, 28 mm, 5.07 µm, SteadyShot On):
    ```python
    assert d("dark_site")["next_time"]["settings"] == "5 s · f/4 · ISO 6400 · 25 lights · 10 darks · SteadyShot off"
    assert d("city")["next_time"]["settings"] == "5 s · f/4 · ISO 1600 · 25 lights · 10 darks · SteadyShot off"
    assert d("ship")["next_time"]["settings"] == "3 s · f/4 · ISO 12800 · 40 lights · SteadyShot off"
    assert d("handheld")["next_time"]["settings"] == "0.5 s · f/4 · ISO 12800 · 40 lights · SteadyShot on"
    ```
    and `1 <= len(reasons) <= 3`; for 163, `happened` names mixed exposures (13-20 s), exposures over the
    5 s limit, stretched stars (roundness 0.73), fewer than 10 lights and no darks (dark_site only; absent for
    ship and handheld).
  - `test_aperture_uses_max_aperture_then_shot`: `max_aperture` 2.8 → settings start `"... · f/2.8 ·"` and
    NPF uses 2.8; `max_aperture` None → f/4.
  - `test_debrief_without_frames_uses_log_rules_only`: `frames=None` → the log rules still fire and no
    exception. With no focal length or aperture the settings line reads, for example,
    `"3 s · widest aperture · ISO 12800 · 40 lights · SteadyShot off"` (ship) and
    `"star-trail limit · widest aperture · ISO 6400 · 25 lights · 10 darks · SteadyShot off"` (dark_site).
  - `test_originals_offline_line`: `originals_online` False → exactly one `happened` line containing
    `"offline"`, and no SteadyShot line.
  - `test_unknown_place_raises`: `ValueError`.
- [ ] **Step 2:** run, expect failures.
- [ ] **Step 3:** implement. NPF from the spec formula at declination 0 with N the recommended aperture; pixel
  pitch from `pixel_um`, else 4 µm with a reason line "pixel size unknown; assumed 4 µm". Exposure display:
  nearest whole second, minimum 1 s; under 1 s, one decimal. Sentence wording is the builder's; each sentence
  names frames as `frame N` and, where it names one, the staged file name.
- [ ] **Step 4:** tests pass.
- [ ] **Step 5:** commit `feat: astro debrief rules turn the log and EXIF into what happened and settings for next time`.

### Task 4: Debrief route

**Files:**
- Modify: `src/lumen/library.py`, `src/lumen/web.py`
- Test: `tests/test_web.py` (`AstroWebTests`)

**Interfaces:**
- Consumes: `parse_siril_log`, `debrief`, `PLACES`.
- Produces: `Library.astro_debrief(job_id: int, place: str) -> dict` = `debrief(...)` plus
  `"review": Library.latest_astro_review(job_id)` (Task 5 adds it; return None until then).
  `GET /api/astro/jobs/<id>/debrief?place=<place>`.

- [ ] **Step 1: Write failing tests**: `test_debrief_route_after_a_run` (run the success scenario, GET with
  `place=ship`, 200, keys `place, happened, try_now, next_time, review`, `assert_safe`);
  `test_debrief_refuses_unrun_and_unknown` (queued job → 400 "no debrief until the stack has run"; unknown
  place → 400; unknown job → 404 like `/api/astro/jobs/<id>`).
- [ ] **Step 2:** run, expect failures.
- [ ] **Step 3:** implement. "Has run" is the job status (`succeeded` or `failed`, or `cancelled`/`interrupted`
  with a `frames.json` present), never the log file's existence. Reads `siril.log` and `frames.json` from the
  job folder; a missing or unreadable `frames.json` passes `None`.
- [ ] **Step 4:** tests pass.
- [ ] **Step 5:** commit `feat: GET the debrief of an astro job for a shooting place`.

### Task 5: Model note

**Files:**
- Modify: `src/lumen/omlx.py`, `src/lumen/library.py`, `src/lumen/web.py`
- Test: `tests/test_omlx.py`, `tests/test_library.py`, `tests/test_web.py`

**Interfaces:**
- Produces:
  ```python
  STACK_NOTES_MAX = 5; STACK_NOTE_CHARS = 200
  @dataclass(frozen=True, slots=True)
  class StackReview: notes: list[str]; raw_response: str
  class StackReviewResponseError(...)          # same base as the other response errors
  def parse_stack_review(raw: str) -> StackReview
  OmlxTagger.review_stack(self, preview: Path, debrief_text: str, timeout: float = 240) -> StackReview
  Library.review_astro_stack(job_id: int, tagger, *, place: str) -> StackReview
  Library.latest_astro_review(job_id: int) -> dict | None      # {"notes": [...]}
  POST /api/astro/jobs/<id>/review  {place}  -> {"notes": [...]}
  ```
  Events: `astro_reviewed` (photo_id None, `{job_id, place, notes, model, role}`), `astro_review_failed`
  (`{job_id, place, model, role, error, raw_response}`).

- [ ] **Step 1: Write failing tests**:
  - `test_omlx`: `parse_stack_review('{"notes": ["Cloud on the left."]}').notes == ["Cloud on the left."]`;
    six notes, an empty note, a non-string, or a missing key raise `StackReviewResponseError`; a 300-char note
    is trimmed to 200. The prompt (capture via a fake `_complete`) contains the debrief text and says not to
    give camera settings.
  - `test_library`: `test_review_astro_stack_stores_and_reads_back` (fake tagger; event appended;
    `latest_astro_review` returns only `notes`); `test_review_failure_leaves_debrief_intact` (tagger raises;
    `astro_review_failed` stored; `astro_debrief(...)["review"]` still None; other fields unchanged);
    `test_review_needs_a_preview` (job without the artifact's preview → `ValueError`).
  - `test_web`: POST with `{"place": "ship"}` returns notes; extra keys or a bad place → 400; review model not
    loaded → the `tagger_for` refusal verbatim, like develop suggest; response carries no `raw_response`.
- [ ] **Step 2:** run, expect failures.
- [ ] **Step 3:** implement. The preview is the artifact's `settings["preview"]` in the job folder, sent at
  `MODEL_IMAGE_LONG_EDGE`. The debrief text is the three sections joined as plain lines. Model call outside
  `self._mutation()`, events inside it, as `suggest_develop` does.
- [ ] **Step 4:** tests pass.
- [ ] **Step 5:** commit `feat: optional local-model note on an astro stack's preview`.

### Task 6: Astro panel, docs and release

**Files:**
- Modify: `src/lumen/static/index.html`, `src/lumen/static/app.js`, `src/lumen/static/app.css` (only if the
  section needs spacing), `tests/test_web.py` (JS harness), `src/lumen/__init__.py`, `pyproject.toml`,
  `CHANGELOG.md`, `docs/HANDOFF.md`, `AGENTS.md`

**Interfaces:**
- Consumes: the two routes above; `aiCall`, `aiTask`, `aiRunning`, `roleReady` in `app.js`; add `astro` to
  `AI_TASK_LABELS` as `'Stack review'`.

- [ ] **Step 1: Write failing JS harness scenario `astro-debrief`** (follow the existing `astro-*`
  scenarios): the fake server answers `/api/astro/jobs/7/debrief?place=dark_site` and `?place=ship` with
  distinct payloads; after the job succeeds the panel shows three headed lists ("What happened", "Try now",
  "Next time") with the payload's text; changing `#astro-place` to `ship` refetches and shows the ship
  settings; `localStorage['lumen-astro-place'] === 'ship'`; `#review-astro` is disabled when
  `roleReady('review')` is false and its `aria-describedby` is `ai-availability`; clicking it (ready) posts
  `{place: 'ship'}` and renders the notes under the debrief; a queued job shows no debrief section.
- [ ] **Step 2:** run the scenario, expect failure.
- [ ] **Step 3:** implement. `index.html`: a labelled `<select id="astro-place">` ("Where you shot": Dark site,
  City or light pollution, Ship or boat, Handheld) beside `#astro-recipe`; `<section id="astro-debrief"
  aria-labelledby=...>` under `#astro-summary` with the three lists, `#review-astro` ("Look at the stack",
  class `secondary ai`) and a notes list; not a live region. "How this works" gains: "After a stack, the
  debrief reads the frames' EXIF and Siril's log; Lumen's rules write it, so it needs no AI. Look at the stack
  asks the local model for notes on what it sees in the preview; it never changes the settings." `app.js`:
  fetch in `setAstroJob` when status turns `succeeded`/`failed`, on resume, and on place change; read/write
  `lumen-astro-place` in try/catch.
- [ ] **Step 4:** full suite `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest discover -s tests`
  OK and `node --check src/lumen/static/app.js` OK; no stray files in `git status`.
- [ ] **Step 5:** version 0.4.0 in both files; `CHANGELOG.md` entry; `docs/HANDOFF.md` "Resume here" entry
  (what landed, tests, not done: no browser pass unless one is run); one `AGENTS.md` Current tool state line
  naming `astrodebrief`, `frames.json`, the clean-up, the stem and the two routes.
- [ ] **Step 6:** commit `feat: astro debrief in the Astro panel; release 0.4.0`.
