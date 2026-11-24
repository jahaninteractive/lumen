# Lumen Straighten Design

Status: draft, not yet approved.

## Intent

Rotate a tilted frame so its horizon is level, by a fraction of a degree to a
few degrees, and bake that rotation into the preview and the exported JPEG.
Lumen can crop and develop a frame today but cannot level it, and a ship-borne
trip produces a lot of tilted sea horizons.

Straightening must work in three places:

1. As its own control in the Develop panel, usable with no AI engine at all.
2. Inside "Suggest develop" for one frame.
3. Inside "Pick and develop every burst", applied to the chosen frame.

This builds on `2026-10-26-develop-controls-design.md` and
`2026-10-25-frame-view-crop-tabs-filters-design.md`.

## Goals

- A straighten angle per frame, stored with the develop settings, reversible,
  never touching the camera original.
- The preview shows exactly what the export produces.
- Automatic measurement of the tilt offline, with no new dependency.
- The AI engines (local and Online) decide whether to trust the measured
  angle. They never invent the angle.
- The photographer's own angle is never overwritten by an automatic one.

## Non-goals

- Perspective or keystone correction (converging verticals).
- Free rotation beyond +/-15 degrees, or 90 degree turns (orientation is a
  separate concern).
- Filling the empty corners. A straightened frame is always cropped inward.
- Straightening astro frames. The stacker handles rotation there.
- Straightening non-raw frames. Same boundary as the rest of Develop.

## How it behaves

### The setting

Add `straighten_deg` to `DEVELOP_RANGES` in `domain.py`: range -15.0 to 15.0,
positive turns the image clockwise as the photographer sees it. Zero or absent
means not straightened. It then travels through `DevelopSettings`, the
catalogue, sidecars, provenance, "apply develop to burst" and export with no
new plumbing.

### The rotation and the inward crop

Rotating a rectangle leaves empty corners, so the frame is cropped to the
largest rectangle of the same aspect ratio that fits inside the rotated image,
centred. For a frame w by h and angle t:

    s = min( w / (w*cos|t| + h*sin|t|),  h / (w*sin|t| + h*cos|t|) )

The straightened frame is (s*w) by (s*h). On a 3:2 frame this costs about 5%
of the pixels at 1 degree, about 10% at 2 degrees and about 22% at 5 degrees.
The panel shows the remaining megapixels next to the slider, the same way the
crop tab warns below 12 MP.

One function in Python owns this maths (`composition.straightened_size`), with
unit tests. The Swift helper implements the same formula and a test compares
the two on the rendered size.

### RAW path

In `tools/raw_developer.swift`, straight after `rawFilter.outputImage` and
before clarity, the tone curve and the resample: rotate about the centre with
`transformed(by:)`, crop to the inset rectangle, and translate the extent back
to the origin. Doing it before the resample keeps the preview and the full
size export identical. The reported `width` and `height` are the straightened
size. The histogram is taken from the straightened image.

### Camera JPEG path

`pipelines.py` exports some frames from the camera's embedded JPEG. A frame
with `straighten_deg` set must rotate there too (Pillow `rotate` with bicubic
resampling, then the same inset crop), and the `lossless` test at the camera
branch must be false whenever the angle is set, as it already is for a crop.

### Crops

A crop is a normalised rectangle. With straightening it is relative to the
straightened frame: rotate first, then crop. When the angle changes on a frame
that has an active crop, keep the rectangle and announce "Straighten changed;
check the crop". Crop candidates and the megapixel warning must be computed
from the straightened size, not the sensor size.

Open point for the plan: `PhotoEdit` records source dimensions. Decide whether
it records the sensor size or the straightened size, and migrate nothing
either way (old crops have no angle, so both readings agree).

## Measuring the tilt without AI

Add `VNDetectHorizonRequest` to `tools/vision_analyzer.swift`, in the same
`perform` call as the face, saliency and feature-print requests. Report
`horizon_deg` (a number, or null when Vision finds no horizon) and bump
`schema_version` to 4. Store it with the frame's other analysis results.

Rules:

- Measure on the unstraightened image only. A preview already rendered with a
  straighten angle must not be measured again, or the angle would drift.
- Convert Vision's radians to `straighten_deg` in one place, and fix the sign
  with a test: draw a synthetic line tilted a known direction with Pillow,
  analyse it, and assert the corrective angle levels it. Do not trust the sign
  from documentation.
- Usable band: ignore a measured tilt under 0.3 degrees (already level), and
  ignore one over 8 degrees (probably not a horizon, or deliberate). Both
  limits are constants beside `MIN_CROP_PIXELS` style constants.
- Frames flagged as astro are never offered an angle.

Known limits, stated in the guide: Vision finds one dominant line. It is good
on sea and flat land, and can lock onto a ship's rail, a shoreline running
away from the camera, or an iceberg edge. That is why the automatic result is
always visible and one click from undone.

## The three entry points

### 1. Develop panel (no AI needed)

A "Straighten" group in the Develop panel:

- A slider, -15 to 15 in 0.1 steps, with a numeric field.
- "Auto" button: sets the slider to the measured angle when one is in the
  usable band, otherwise announces "No clear horizon found; set it by hand".
- "Draw horizon" tool: the photographer drags a line along what should be
  level, and the angle of that line becomes the setting. Lines nearer vertical
  than horizontal level a vertical instead (a mast, a building edge).
- "Reset" returns to zero.
- A grid overlay while the slider or the line tool is active.

It saves through the existing develop save route and re-renders the preview.

### 2. Suggest develop

`Library.suggest_develop` already passes `histogram_note`. Add `tilt_note`:
when a usable measured angle exists, "Apple Vision measured the horizon tilted
N degrees; straighten_deg of M would level it." The prompt in `omlx.py` gains
one rule: include `straighten_deg` at that value unless the frame has no real
horizon or the tilt is clearly deliberate, and say which in the rationale. The
model may only return the measured value or omit the control.
Lumen drops any other angle the model returns and keeps the rest of the
advice. The exact wording sent to the model is in the plan.

When no usable angle exists, the control is left out of the prompt's control
list so the model cannot propose one.

The Online engine uses the same prompt, so it behaves the same way.

### 3. Pick and develop every burst

After the chosen frame takes its develop advice in the cull job, the angle
rides in with it, because it is part of the advice settings. Two additions:

- `_develop_from_advice` returns early when advice has no settings. A frame
  whose only advice is the angle still has settings, so no change is needed,
  but add a test for "angle only".
- With no AI engine configured, or when advice failed, the cull job still
  applies the measured angle on its own when it is in the usable band. This is
  the "works without AI" path for bulk use. It follows the existing rule: it
  never touches a frame the photographer has developed or cleared.

Provenance records who set the angle: `photographer`, `vision`, or the model.

## Safety and invariants

- The camera original is never written. The existing guard in `render_raw`
  already covers this.
- No preview/export mismatch: one formula, both render paths tested against
  it.
- An automatic angle never replaces one the photographer set.
- No new runtime dependency. Pillow and the two Swift helpers only.

## Tests

- `straightened_size`: zero angle is identity, symmetry in sign, known values
  at 1, 2 and 5 degrees on 3:2 and 2:3, never exceeds the source.
- Domain: range validation and round trip of `straighten_deg`.
- Camera path: a synthetic JPEG with a tilted line exports level, at the
  expected size, and is not marked lossless.
- Vision sign test (macOS only, skipped elsewhere), as above.
- Advice parsing: accepts the measured angle, drops any other.
- Cull job: angle applied with AI, applied without AI, skipped on a frame the
  photographer has touched, skipped on astro frames.
- `node --check src/lumen/static/app.js`.

## Build order

1. `straighten_deg` in the domain, `straightened_size`, tests.
2. RAW helper rotation and the camera JPEG path. Slider and Reset in the
   panel. At this point manual straightening works end to end.
3. Vision measurement, schema 4, the Auto button.
4. Draw-horizon tool and grid overlay.
5. Tilt note in Suggest develop and the parser rule.
6. Cull job: angle with advice, and the no-AI fallback.
7. Guide and changelog.

Steps 1 to 3 are the minimum useful feature. Each step is small enough to be
an offline task in `docs/OFFLINE-PROMPTS.md`.
