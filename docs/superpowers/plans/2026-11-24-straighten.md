# Straighten Implementation Plan

> **For agentic workers:** one lead and one implementer per task. The lead owns this plan, hands out one task
> at a time, reviews every diff and runs the gates. The implementer writes tests and code for that task only.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A frame's horizon can be levelled by hand, by offline measurement, or by the AI engines, and the
preview and the exported JPEG agree.

**Architecture:** One new develop number, `straighten_deg`, rides the existing develop plumbing. The raw helper
and the camera-JPEG export path both rotate and crop inward using one formula. The Vision helper measures the
tilt. The review model may accept or decline the measured angle, never choose its own.

**Tech Stack:** Python 3 stdlib + Pillow, `unittest`, Swift (Core Image, Vision), vanilla JS.

**Spec:** `docs/superpowers/specs/2026-11-24-straighten-design.md`

## Global Constraints

- Read `AGENTS.md` and `docs/HANDOFF.md` first. Work in a worktree on branch `feature/straighten` off `main`.
  Nothing merges to `main` until the final gate passes.
- Run on the Mac itself. The Swift helpers and the Vision test cannot be built or run anywhere else.
- Never write, move or rename a camera original; derived files stay inside the library.
- Pillow is the only runtime dependency. No downloads.
- No AI vendor or model-family names in tracked files or commit messages; no co-author or session trailers.
  Commit messages are one plain sentence saying what changed.
- Red-green: each test is seen failing before the code. Suite:
  `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest discover -s tests -v`; focused:
  `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_<module> -v`; and
  `node --check src/lumen/static/app.js` after any JS edit.
- No test reads `Examples/` or a personal photo. Tests draw their own images with Pillow and inject runners.
- Nothing in a browser response names a source path, fingerprint or serial.
- Preserve native controls, keyboard behaviour and live announcements in the Develop panel.
- One commit per task. A task changes only the files it names.

## How the lead and the implementer work

1. **Baseline (lead, before Task 1).** On a clean `main`: run the full suite and `node --check`, record the
   test count and that it is green. Run `zsh scripts/bootstrap.sh` so both helpers are current. Export one
   untouched Sony frame, one developed frame and one cropped frame from a scratch library and keep the three
   JPEGs and their sidecars outside the repo as the "before" set. If the baseline is red, stop and report.
2. **Per task.** The lead gives the implementer the task text, the Global Constraints and only the named files.
   The implementer shows the failing test, makes it pass, runs the focused module, commits.
3. **Lead review, every task.** Read the whole diff. Check it against the task's "Must not change" list and
   the Review Focus. Run the full suite and `node --check`. The test count may only go up. Any failure or
   out-of-scope edit goes back to the same implementer with the exact output; the lead does not patch it.
4. **Stop rules.** Two failed attempts at a task: reset the worktree to the last good commit, split the task,
   and tell the photographer. Any edit to a file a task did not name: reject the diff.
5. **Final gate (lead).** Full suite, `node --check`, rebuild helpers with bootstrap, then re-export the three
   baseline frames with no straighten angle and compare with the "before" set (Review Focus 1). Then the
   manual checks at the end of this plan. Only then offer the merge to the photographer.

## Review Focus (what must not break)

1. **Zero is invisible.** A frame with no `straighten_deg` behaves exactly as today: the request sent to the
   raw helper has no new key, the camera-JPEG export is still lossless where it was, pixel dimensions and
   sidecars of the three baseline exports are unchanged. `straighten_deg: 0` is normalised to absent.
2. **Preview equals export.** The same angle gives the same framing at preview size and at full size, on both
   the raw path and the camera-JPEG path.
3. **An old helper never lies.** A library whose helper binary predates this change must refuse to save an
   angle with a readable "run bootstrap" message, not show an unrotated preview as if it were straightened.
   A Vision helper that reports schema 3 (no horizon) is read as "no measurement", not as an error.
4. **Existing data loads.** Catalogues, sidecars and stored develop advice written before this change open
   unchanged. Any catalogue change is additive and nullable.
5. **The photographer's angle wins.** No automatic path replaces an angle or develop settings the
   photographer saved or cleared. The existing rule in `_develop_from_advice` still holds.
6. **Crops stay valid.** A frame with a crop and no angle exports the same crop as before. With an angle, the
   crop is taken from the straightened frame.
7. **Astro is untouched.** No angle is measured into, suggested for, or applied to astro frames, and the
   stacking pipeline's inputs are unchanged.
8. **Burst copy stays safe.** "Apply develop to burst" carries the angle like any other setting and still
   fails one frame with a reason rather than the job.

---

## Instructions to the review model

These are the only new words either engine (local or Online) is sent. Both engines get the same text. The
model is a judge of a measurement, never the source of an angle.

**Case 1: a usable measurement exists and the frame has no angle yet.** `offer_straighten` is true and
`tilt_note` is:

> The horizon detector measured this frame as tilted by {abs(measured):.1f} degrees, {direction}. Setting
> straighten_deg to {corrective:.1f} would level it.

where `{direction}` is "clockwise" or "anticlockwise" and `{corrective}` is the angle from Task 5's function.
The sentence appended after the controls list is:

> straighten_deg rotates the frame to level the horizon and trims the edges to fit. You may set it only to
> the value the horizon detector gives below, or omit it. Set it when the frame shows a real horizon or
> waterline that should be level. Omit it when there is no true horizon in the frame, when the measured line
> is something else (a rail, a shoreline running away from the camera, a slope, a roof), or when the tilt is
> clearly a deliberate choice. Never choose a different angle. Say in the rationale whether you levelled the
> frame or why you left it.

**Case 2: the frame already has an angle** (the render the model sees is already straightened).
`offer_straighten` is false and `tilt_note` is:

> This frame has already been straightened by {current:.1f} degrees; leave that alone.

The current angle is carried through unchanged by the caller, whatever the model returns.

**Case 3: no usable measurement** (none stored, outside 0.3 to 8 degrees, or an astro frame).
`offer_straighten` is false, `tilt_note` is empty, and the prompt is byte for byte what it is today.

Placement: `{tilt_note}` goes immediately after `{histogram_note}` in the existing prompt. Nothing else in
the prompt changes, and the reply shape stays `{"settings": {...}, "rationale": "..."}`.

**Pick and develop** sends no extra instruction. It reuses Suggest develop for the chosen frame, so the same
three cases apply. The pick itself (which frame of a burst is kept) is not told about tilt and must not weigh
it: a tilted keeper is fixable, a soft one is not.

**What the model is never asked:** to estimate an angle, to judge perspective, or to straighten astro frames.

**Checking the wording (lead, during manual check C):** run Suggest develop on six real frames with each
engine: two clear sea horizons, one with a ship's rail across it, one mountain or ice scene with no true
horizon, one deliberate tilt, one already level. Record accept or decline for each. The wording passes when
both horizons are levelled and the other four are left alone or declined with a reason. If an engine fails
this, change the wording here first, then the code, and re-run.

---

### Task 1: The setting and the geometry

**Files:** `src/lumen/domain.py`, `src/lumen/composition.py`, `tests/test_composition.py`, the domain tests'
home (`tests/test_catalog.py` or wherever `DevelopSettings` is tested today).

- [ ] Add `straighten_deg` to `DEVELOP_RANGES`, -15.0 to 15.0. A value of exactly 0 is dropped on
      construction so "zero" and "absent" are one state.
- [ ] Add `composition.straightened_size(width, height, degrees) -> tuple[int, int]` and
      `straightened_box(...)` using the spec's formula, centred, never larger than the source.
- [ ] Tests: identity at 0; same result for +t and -t; 3:2 and 2:3 at 1, 2 and 5 degrees; round trip of the
      setting through `DevelopSettings.to_dict`; out-of-range rejected.

**Must not change:** crop candidate generation, any existing `DEVELOP_RANGES` entry, the prompt text (the
prompt builds its control list from `DEVELOP_RANGES`, so Task 1 must exclude the new key from that list until
Task 6; add the exclusion and a test that the prompt is byte-identical to before).

### Task 2: Raw helper rotation

**Files:** `tools/raw_developer.swift`, `src/lumen/rawdevelop.py`, `tests/test_rawdevelop.py`.

- [ ] Helper: read optional `straighten_deg`; after `rawFilter.outputImage` and before clarity, rotate about
      the centre, crop to the inset rectangle, move the extent to the origin. Report the new width and height.
      Report `"straighten": true` in the `supported` map.
- [ ] Python: `_supported` reads `straighten` (absent means false). `render_raw` raises `DevelopError` with a
      "rebuild the helper with scripts/bootstrap.sh" message when an angle is asked of a helper that does not
      report it. `DevelopRender.supported` carries the flag to the panel.
- [ ] Tests with an injected runner: no angle sends no key (Review Focus 1); angle is sent; old-helper payload
      plus an angle raises; old-helper payload with no angle renders as before.
- [ ] Lead builds the helper and checks by hand that a rendered size matches `straightened_size` within one
      pixel at 2 degrees, at preview size and full size.

**Must not change:** the order or behaviour of exposure, white balance, lens correction, clarity, tone curve,
resample or histogram for a request without the key.

### Task 3: Camera-JPEG export path and crops

**Files:** `src/lumen/pipelines.py`, `tests/test_pipelines.py`, and the one place in `src/lumen/library.py`
that computes crop candidates and megapixels for a frame (name the function in the task hand-off).

- [ ] In the camera branch, rotate with Pillow (bicubic, `expand=False`), crop to `straightened_box`, then
      apply the existing crop. `lossless` is false whenever an angle is set.
- [ ] Crop candidates and the megapixel warning use the straightened size when an angle is set.
- [ ] Tests: synthetic JPEG with a tilted line exports level at the expected size; no angle keeps the
      lossless branch (assert the existing lossless test still passes untouched); crop plus angle crops the
      straightened frame; crop without angle gives the same box as before.

**Must not change:** metadata, provenance, watermark and resize behaviour; the darktable and `sips` fallbacks.

### Task 4: Slider and Reset in the Develop panel

**Files:** `src/lumen/static/index.html`, `src/lumen/static/app.js`, `src/lumen/web.py` only if the develop
reply needs the `straighten` supported flag passed through, `tests/test_web.py`.

- [ ] A "Straighten" group: range input -15 to 15 step 0.1, a number field, Reset, remaining megapixels.
      Disabled with an explanation when the render says straighten is unsupported.
- [ ] Saves through the existing develop save route; announces through `announce()`.
- [ ] When the angle changes on a frame with an active crop, announce "Straighten changed; check the crop".

**Must not change:** any other control's id, order, keyboard behaviour or saved value. After this task, manual
straightening works end to end; the lead does manual check A below before going on.

### Task 5: Measure the tilt offline, and the Auto button

**Files:** `tools/vision_analyzer.swift`, `src/lumen/analysis.py`, `src/lumen/catalog.py` (one nullable
column or the existing analysis payload, whichever stores Vision results today), `src/lumen/library.py`
(reading it), `src/lumen/static/app.js`, tests in `test_analysis.py`, `test_catalog.py`, `test_library.py`.

- [ ] Add `VNDetectHorizonRequest` to the existing `perform` call. Emit `horizon_deg` (number or null) and
      `schema_version` 4.
- [ ] Parser accepts schema 3 and 4; missing or null means no measurement.
- [ ] One function converts the measurement to a corrective `straighten_deg` and applies the usable band
      (0.3 to 8 degrees); astro frames return none. Measurement is only ever taken from an unstraightened
      image.
- [ ] Sign test (macOS only, skipped elsewhere, the one test allowed to run the helper): a Pillow image with
      a line tilted a known way must yield an angle that levels it.
- [ ] "Auto" button sets the slider from the measurement or announces "No clear horizon found; set it by
      hand". It does not save until the photographer saves, as the other controls do.

**Must not change:** face, saliency, feature-print output, grouping and scoring results for existing frames.
Existing frames are not re-analysed automatically; say in the guide how to refresh them.

### Task 6: Suggest develop accepts or declines the angle

**Files:** `src/lumen/omlx.py`, `src/lumen/library.py` (`suggest_develop`), `tests/test_omlx.py`,
`tests/test_library.py`.

Use the exact wording in "Instructions to the review model" below. Do not paraphrase it.

- [ ] `tagger.suggest_develop` gains `tilt_note: str = ""` and `offer_straighten: bool = False`. The control
      list is built from `DEVELOP_RANGES` minus `straighten_deg`; the straighten sentence is appended only
      when `offer_straighten` is true.
- [ ] `Library.suggest_develop` builds the note from the stored measurement and the current settings, using
      the three cases below, and passes it in beside `histogram_note`.
- [ ] The angle is checked in `Library.suggest_develop`, next to the existing `render.supported` filter, not
      in the parser (the parser does not know the measurement): keep `straighten_deg` only when a usable
      measurement exists and the value is within 0.05 of it; otherwise drop that key and keep the rest.
- [ ] Tests: no measurement gives the pre-change prompt byte for byte; each of the three cases produces its
      note; measured angle accepted; invented angle dropped with the other settings kept; an angle returned
      when none was offered is dropped; advice stored before this change still parses.

**Must not change:** the JSON shape of advice, the histogram note, feedback handling, either engine's
transport, `parse_develop_response`.

### Task 7: Pick and develop every burst

**Files:** `src/lumen/library.py` (the cull job's per-frame step and `_develop_from_advice`),
`tests/test_library.py`.

- [ ] Advice that carries only the angle is applied.
- [ ] With no engine, or when advice failed, the measured angle alone is saved for the chosen frame, under the
      same "never touched by the photographer" rule. Provenance records the source as `vision`.
- [ ] Tests: with AI, without AI, photographer-touched frame skipped, astro frame skipped, a failing frame
      does not fail the job, job counters unchanged in name.

**Must not change:** which frame a burst picks, rejection behaviour, job names and counters.

### Task 8: Draw-horizon tool and grid

**Files:** `src/lumen/static/app.js`, `src/lumen/static/index.html`, the stylesheet.

- [ ] Drag a line on the preview; its angle becomes the slider value (nearer-vertical lines level a
      vertical). Keyboard users keep the slider and number field as the equivalent path.
- [ ] Grid overlay while straightening is active.

**Must not change:** the crop tab's pointer handling.

### Task 9: Guide, changelog, handoff

**Files:** `docs/GUIDE.md`, `CHANGELOG.md`, `docs/HANDOFF.md`, `docs/BACKLOG.md`.

- [ ] Document the three entry points, the pixel cost, where Auto misses, and that older libraries need
      bootstrap run once and frames re-analysed to get a measurement.

---

## Manual checks (lead, on real frames, nothing committed)

- **A. After Task 4:** a tilted sea horizon straightened by hand looks level in the preview and in the
  exported JPEG, and the two match. Reset returns the original framing.
- **B. After Task 5:** Auto on a clear horizon, on a frame with no horizon, and on an astro frame.
- **C. After Task 7:** Pick and develop on a small scratch library, once with the local engine, once with
  the Online engine, once with no engine. Check a frame developed by hand beforehand is left alone.
- **D. Final:** the three baseline exports are unchanged; the app build (`scripts/build_app.sh`) still
  compiles all three Swift tools.

## Rollback

Everything lives on `feature/straighten`. Until the merge, `main` and the installed app are untouched. After
the merge, a frame with no angle behaves as before (Review Focus 1), and clearing a frame's develop settings
removes its angle.
