# Lumen Develop Controls Design

## Intent

Give the photographer IEDT-style develop controls inside the frame dialog —
histogram with an editable tone curve, brightness, white balance, contrast,
sharpness and noise reduction — and bake those adjustments into the exported
JPEG so the export gets the most out of the raw file rather than shipping a
default decode.

Today a frame can be rated, keyworded, placed and cropped, but never
*developed*: the exported JPEG is whatever the decoder produces with default
settings. This design closes that gap without adding a second application to
the workflow.

This builds on `2026-10-25-frame-view-crop-tabs-filters-design.md` (the Frame
and Crop tabs in the loupe dialog) and reuses the background job and progress
dialog introduced in `2026-10-22-travel-volume-culling-design.md`.

## Goals

- Adjust a frame's exposure, white balance, contrast, sharpness, noise
  reduction and tone curve from the frame dialog, seeing the result on the
  real raw within about a second.
- Bake those adjustments into the exported JPEG.
- Keep every adjustment non-destructive and reversible: the camera original is
  never touched, and clearing the settings restores the default decode.
- Show the developed version in the contact sheet, so the grid agrees with
  what the photographer saw in the dialog.
- Apply one frame's settings to the rest of its burst in one action.
- Optionally export the untouched version alongside the developed one.

## Non-goals

- Masks, local adjustments, brushes, gradients, healing or cloning.
- Lens correction, chromatic aberration, perspective, vignetting, dehaze.
- Colour grading beyond white balance and the tone curve (no HSL, no creative
  looks, no split toning).
- Batch develop across the whole library, or copying settings between bursts.
- Editing non-raw formats. Develop applies to raw frames only.

## Anti-goals

Things that would make this a failure even if it worked:

- Any change that writes to, moves or renames a camera original.
- A preview that shows one thing and an export that produces another.
- A develop panel that makes the frame dialog slow to open or scroll for
  frames the photographer has not developed.
- Exposing source paths, fingerprints or decoder internals to the browser.

## Why Core Image, not darktable

Export currently decodes raws two ways: darktable where it can, and macOS
`sips` where it cannot. The obvious design was to turn the sliders into a
darktable XMP sidecar, which `DarktablePipeline.export_jpeg` already accepts
as `profile`.

That route is dead for this library. Every one of the 154 frames in the
photographer's catalogue is Sony's reduced-size lossless ARW, which rawspeed
refuses (`Unsupported photometric interpretation: 6`). A darktable-based
develop feature would have been dormant on the entire library, and would only
have come alive if the camera were switched to full-size lossless L.

Apple's Core Image raw pipeline — the same engine Sony's own Imaging Edge
Desktop sits on — decodes these files, and `CIRAWFilter` exposes almost
exactly the IEDT control set. Verified against
`/Volumes/external/Photos/Imports/19th/DSC02733.ARW` on 2026-10-26:

- decodes the reduced-size ARW at its native 3504×2336
- reports the as-shot neutral white balance (3490K, tint −9.4)
- exposes exposure, temperature, tint, contrast, sharpness, detail,
  luminance NR and colour NR
- renders a 1100px preview with all settings applied in ~0.34 s
- carries EXIF, GPS, lens and shutter through to the render
- writes an sRGB IEC61966-2.1 ICC profile, which `export_jpeg` requires, and
  carries the XMP packet (tag 700) that `_descriptive_xmp` reads
- does **not** carry the MakerNote (tag 37500) — but neither does `sips`,
  which it replaces, so no metadata that export keeps today is lost. The
  `exiftool` MakerNote rebase in `export_jpeg` stays for the darktable route
- `CIFilter.toneCurve` chains onto the output for the editable curve
- `CIFilter.areaHistogram` returns the histogram from the same render

The decision (photographer's, 2026-10-26) is that this decoder replaces `sips`
for **all** JPEG export, so frames that were never developed also stop going
through the 8-bit `sips` path.

### What "16-bit" does and does not mean here

The helper writes a genuine 16-bit TIFF (`BitsPerSample (16,16,16,16)`), but
Pillow caps RGB TIFFs at 8 bits per channel when `export_jpeg` reads it back,
and JPEG is 8-bit regardless. The gain is not a 16-bit output file. The gain
is that exposure, white balance, the tone curve, sharpening and noise
reduction are all computed in Core Image's float pipeline **before** anything
is quantised — which is where banding in a smooth sunset sky would otherwise
come from.

Moving crop and resize into the helper so the 8-bit truncation happens after
them would be marginally better still. It is deliberately not done here: the
Pillow crop, resize, watermark, EXIF and XMP assembly is working, tested code
with EXIF-orientation subtleties, and moving it earns less than it risks.
Recorded in `docs/BACKLOG.md` as the upgrade path if banding is ever seen.

## The native helper

`tools/raw_developer.swift`, compiled by `scripts/bootstrap.sh` into
`<library>/cache/bin/raw_developer`, exactly as `tools/vision_analyzer.swift`
is today. `Settings` gains `raw_developer_path`; `lumen doctor` and
`/api/status` report its availability beside `vision_analyzer`.

It reads one JSON request on stdin and writes one JSON response on stdout,
rather than taking a dozen argv flags:

```json
{"source": "/abs/path.ARW", "destination": "/abs/out.tif",
 "format": "tiff16" | "jpeg", "long_edge": 1100,
 "histogram": true,
 "settings": {"exposure_ev": 0.85, "temperature_k": 4090, "tint": -9.4,
              "contrast": 0.55, "sharpness": 0.9,
              "luminance_nr": 0.6, "color_nr": 0.7,
              "curve": [[0,0.06],[0.25,0.16],[0.5,0.5],[0.75,0.86],[1,0.96]]}}
```

The response carries the rendered size, the as-shot neutral temperature and
tint, and the 64-bucket luminance histogram. Any key absent from `settings`
leaves Core Image's own per-file default in place — which matters, because the
neutral white balance and the baseline noise reduction differ per frame.

A non-zero exit with a message on stderr is a failure; the caller surfaces a
public message and never the raw stderr.

## Develop settings

A new `DevelopSettings` value object in `domain.py`, validated the same way
`CropRectangle` is. It stores **only the fields the photographer changed**, so
that "not set" and "set to the default" stay distinguishable and Reset is a
deletion rather than a guess at the right value.

| Control | Field | Range | Maps to |
|---|---|---|---|
| Brightness | `exposure_ev` | −3.0 … +3.0 | `CIRAWFilter.exposure` |
| White Balance | `temperature_k` | 2000 … 12000 | `neutralTemperature` |
| White Balance | `tint` | −150 … +150 | `neutralTint` |
| Contrast | `contrast` | 0.0 … 1.0 | `contrastAmount` |
| Sharpness | `sharpness` | 0.0 … 1.0 | `sharpnessAmount` |
| Noise Reduction | `luminance_nr` | 0.0 … 1.0 | `luminanceNoiseReductionAmount` |
| Noise Reduction | `color_nr` | 0.0 … 1.0 | `colorNoiseReductionAmount` |
| Tone Curve | `curve` | five points, x ascending, both axes 0…1 | `CIFilter.toneCurve` |

White balance is stored as absolute Kelvin and tint, not as an offset from the
frame's own neutral, so that applying one frame's settings across a burst shot
under the same light produces the same rendering on every frame.

An identity curve is stored as absent, and the helper skips the filter.

### Storage

A `photo_develop` table — `photo_id` primary key, `settings_json`,
`source_fingerprint`, `updated_at` — alongside `photo_edits`, following the
crop precedent in `catalog.py` rather than overloading it.

Provenance events: `develop_changed` (with the new settings),
`develop_cleared` (with the previous settings),
`develop_applied_to_group`, and `develop_invalidated_source_replaced`, which
mirrors `invalidate_crop` when a source file's fingerprint changes under a
saved edit.

## The Develop tab

A third tab in the existing `#frame-tabs` rail, after Frame and Crop. It is
built lazily: a frame the photographer never develops costs one more tab
button and nothing else.

At the top, pinned, a single widget combining the histogram and the tone
curve — the histogram drawn as the backdrop, the curve over it, five points
dragged directly with the mouse. Beneath it, collapsible `<details>` sections
in IEDT's order: Brightness, White Balance, Contrast, Sharpness, Noise
Reduction. Each holds a native `<input type="range">` with a numeric field
beside it, matching the existing controls, and a Reset that deletes the
field rather than setting it to zero.

Dragging the curve is a mouse gesture, by the photographer's decision
(2026-10-26). Because the drag maths exists anyway, each point also carries
`tabindex` and responds to arrow keys, with the live region announcing the
new position — the project's keyboard parity is kept as a fallback without
designing the panel around it.

A footer holds "Reset all", "Apply to every frame in this burst", and the
existing "Export this frame".

### Rendering

`POST /api/photos/<id>/develop/preview` takes the current settings and the
viewport's long edge, and responds with JSON carrying a base64 `data:` JPEG
and the histogram buckets — one round trip, no temporary files to clean up,
no cache keys to invalidate. The route resolves the source path server-side
from the photo id; no path reaches the browser.

The browser renders on `change` rather than `input`, matching how the contact
sheet filters already behave, and on pointer release for a curve drag, so one
gesture costs one render. An in-flight request is aborted when a newer one
starts.

The develop preview shows the **whole frame**, uncropped — tone and colour are
judged across the picture, and the crop is a separate decision. The Crop tab
draws its rectangle over the developed render, so the crop is composed on the
pixels that will actually be exported. This matches export, which develops and
then crops. **Builder-check call.**

### When the original is offline

Originals live on an external volume, and `/full/<id>` already serves the
cached camera JPEG with an `offline` flag when it is not mounted. The Develop
tab follows that precedent: it is present but disabled, with the same offline
message, because every render needs the raw. Burst apply skips frames whose
originals are unreachable, counts them, and reports the count in the job
result rather than failing the whole job. **Builder-check call.**

`POST /api/photos/<id>/develop` saves, `DELETE` clears. Both are mutation
routes and keep the existing host/origin checks and strict JSON validation:
known keys only, finite floats, each inside its range, curve points ascending
in x.

### Contact sheet

Saving re-renders that frame's preview through the helper, written atomically
the way `previews.py` already replaces a preview, and sets `preview_kind` to a
new value `developed`. `refine-previews` skips frames whose `preview_kind` is
`developed`, so a refine pass cannot silently throw the photographer's work
away.

Clearing re-renders the same way with no settings — a plain Core Image decode
— and sets `preview_kind` to `refined`, whose meaning widens from "a darktable
render" to "rendered from the raw, not the embedded JPEG". The superseded file
is cleaned up by the existing rule, so Clear cannot restore the previous image
and must re-render; `embedded` would be untrue and keeping `developed` would
lock the frame out of refine forever. **Builder-check call.**

Saving develop settings **re-measures metrics but does not re-rank the
group** — unlike `_refine_one`, which does both. The scores under each
thumbnail then describe the pixels the photographer is actually looking at,
while the group's pick stays where it is.

Measured on an ILCE-7CM2 frame, 2026-10-26: `vision_analyzer` 210 ms and
`measure_image` 9 ms per frame, against the ~340 ms render that happens
anyway. Burst 2's three frames cost ~1.0 s without re-measuring and ~1.7 s
with. Cost is not what decides this.

What decides it is that re-ranking would let developing a frame silently
promote it to Best mid-cull. A score changing while the Best badge stays put
is already an ordinary state in this app — a manual pick outranks the measured
ranking, which is why a burst can show 36, 33, 32 with Best on the 32.
Photographer's decision, 2026-10-26.

### Applying to a burst

"Apply to every frame in this burst" posts to
`/api/photos/<id>/develop/apply-to-group`, which starts a background job of
kind `develop_group` and shows the existing progress dialog. Each frame in the
group gets the same stored settings, a re-rendered preview and fresh metrics,
and one `develop_applied_to_group` event records the source frame and the
frames written. Frames already carrying their own develop settings are
overwritten; the dialog says so before it starts.

"This burst" is the `photo_groups` row — precisely the frames under the
"Burst N" heading in the contact sheet, which is already what near-duplicate
clustering produced. No sub-selection, no separate cluster concept: the
photographer pointed at Burst 2 on screen and meant its three frames.
Rejected frames in the group are included, because a rejected frame can be
un-rejected later and should not be left with a different rendering from its
neighbours. Non-raw members are skipped and counted, since develop is
raw-only. Photographer's decision, 2026-10-26.

## Export

`DarktablePipeline.export_jpeg` gains a `develop` argument. The render step
chooses, in order:

- `--profile` given on the CLI → darktable, as today. This keeps the existing
  XMP profile feature reachable rather than silently dropping it.
- a non-raw source (`.jpg/.png/.tif/.heic`, which `SUPPORTED_EXTENSIONS`
  admits and import ingests) → darktable, exactly as today. `CIRAWFilter`
  only takes raws, and develop is raw-only, so these frames keep their
  current path untouched. **Builder-check call.**
- otherwise → the raw developer helper, with the frame's stored settings if
  it has any.

`sips` is no longer used for JPEG export and its code goes. `DECODERS` gains
`coreimage`, the `decoder` field in export settings and provenance records
which one ran, and the public message no longer mentions a macOS fallback for
raws darktable cannot read. `lumen process` and `refine-previews` keep using
darktable unchanged.

If a frame has saved develop settings *and* an XMP profile is given, export
refuses with a message naming both, rather than silently rendering through
darktable with none of the develop settings applied — that would be the stated
anti-goal of a preview disagreeing with the export. **Builder-check call.**

If `raw_developer` is missing — a library bootstrapped before this feature, or
a machine where `swiftc` failed — raw export refuses up front in
`_prepare_export` with a message naming `scripts/bootstrap.sh`, rather than
degrading silently the way `analyze_faces` does for metrics. `lumen doctor`
and `/api/status` report it as required, not optional. A missing helper is a
setup problem with an obvious fix; a silent fallback to a decoder the
photographer did not choose is worse than a clear refusal. **Builder-check
call.**

Crop still applies after the render, in Pillow, exactly as now.

### Keeping the untouched version

A checkbox, "Also export the untouched version", in the export panel and in
the frame dialog's export. When ticked, a developed frame exports twice: the
developed JPEG under its usual name, and a second from the same source with no
develop settings, named `<stem>-lumen-original.jpg` for the full preset and
`<stem>-lumen-<preset>-original.jpg` otherwise, so that exporting share and
then web does not collide into `-original.jpg` and `-original-1.jpg`. Both are
recorded as `exported` events with their own settings blocks, and both land in
the same output directory, so a burst keeps them together. The checkbox does
nothing for frames with no develop settings. **Builder-check call.**

### Sidecars

`sidecars.py` already writes `lumen:crop` and the crop's provenance into every
XMP sidecar, so develop settings follow the same precedent: a `lumen:develop`
block in the sidecar and in the exported JPEG's XMP packet, listing only the
fields the photographer set. The untouched companion's XMP carries no
`lumen:develop` block, which is what makes it identifiable as the straight
version. **Builder-check call.**

## Constraints

- The camera original is never written to, moved or renamed. The helper only
  ever reads its source, and its destination is always inside the library.
- Previews, renders and exports stay inside the configured library.
- The server stays loopback-only. Mutation routes keep host/origin checks and
  strict JSON validation.
- No source path, fingerprint or helper stderr reaches the browser.
- Pillow remains the only runtime Python dependency; the helper is Swift
  compiled at bootstrap, like `vision_analyzer`.
- Apple Silicon macOS only, which is already the supported target.

## Testing

Red-green, warning-strict, and no test invokes Core Image. The helper is faked
the way `vision_analyzer` is: tests point `raw_developer_path` at a stub script
that returns canned JSON, and assert on the request Lumen sends it and what it
does with the response.

Behaviour worth a failing test first: range and curve validation rejecting bad
input at the route; only-changed-fields storage round-tripping; clearing
restoring the default decode and the `refined` kind; a source fingerprint
change invalidating saved settings; `refine-previews` skipping a `developed`
preview; burst apply writing every frame in the group; export passing the
stored settings to the helper; and the untouched-version checkbox producing
exactly two outputs with distinct names.

The builder check turned up four regressions a naive implementation would
ship, so each gets a test of its own: a non-raw frame still exporting through
darktable once `sips` is gone; export refusing with an actionable message when
`raw_developer` is absent, rather than crashing; export refusing when an XMP
profile and saved develop settings are both present; and a develop save
re-measuring metrics while leaving the group's pick untouched.

## Left to the builder

- The shape of the histogram and curve widget's DOM — canvas versus inline SVG
  — and how the curve is hit-tested. Either is fine; keyboard focus on the
  points has to work.
- Whether `develop_group` reuses the existing job worker directly or wraps it.
- The exact debounce threshold for the preview request.
- Whether the Develop tab's sections remember which was last open, as the
  Crop tab's `<details>` already does or does not.
- The preview cache key. The builder check noted that repeated develop saves
  need an mtime in the `?v=` parameter, or the contact sheet serves a stale
  tile; the existing fingerprint alone will not change.
- Reconciling the preview filename. This document says
  `<library>/previews/<fingerprint>.jpg`; the code writes
  `<fingerprint>-2048.jpg`. Follow the code.
- Updating the now-stale `raw_warning` text in `previews.read_metadata`, which
  tells the photographer that export will use the macOS decoder with default
  settings. After this change that sentence is wrong.
