# Lumen Finishing, Composition, Astro, and Housekeeping Design

## Intent

Extend Lumen from an import-and-review tool into a complete local finishing
workflow. After Lumen groups and ranks a shoot, the photographer can choose a
keeper, compare non-destructive composition suggestions, merge an astro group
through Siril, export finished JPEGs through darktable, and clear Lumen's
working state without risking an original photograph.

This design builds on `2026-10-21-local-raw-manager-design.md`. Its safety rule
still dominates every feature: Sony `.ARW` originals are never edited, renamed,
moved, or deleted.

## User workflow

The web interface presents one ordered workflow for a selected group:

1. **Review** — inspect measured scores and confirm or override the keeper.
2. **Compose** — compare the original with two non-destructive crop suggestions:
   “subject in scene” and “tighter subject.”
3. **Astro** — when the group is astrophotography, review frame checks, select
   calibration frames, and run a managed Siril stack.
4. **Export** — create JPEGs for the current frame, all group keepers, or a
   finished astro stack.
5. **Clear** — remove a group or reset the whole Lumen workspace while keeping
   originals and, by default, completed exports.

These are progressive tools, not mandatory wizard steps. Ordinary daylight
shoots can go directly from Review to Export. Astro groups can skip Compose
until after stacking.

## JPEG finishing and export

### Default export

The first version deliberately has a small set of opinionated defaults:

- JPEG quality 92.
- sRGB output.
- Full available resolution after any approved crop.
- Output under `<library>/outputs/jpeg/`.
- Filename `<original-stem>-lumen.jpg`; a numeric suffix prevents overwriting an
  existing file.
- Scope choices are current photo, keepers from all visible groups, or a
  selected derived astro result.

darktable develops the RAW and applies the user's optional XMP processing
profile. A profile is selected explicitly with a file browser at export time,
applies to that export scope, and is remembered as the next export's default;
Lumen does not silently adopt an adjacent XMP file. The export pipeline writes to a temporary file first and atomically
moves a successful result into place. If no profile is selected, darktable's
camera-aware defaults are used. Lumen displays a clear unavailable state when
`darktable-cli` cannot be found.

The UI reports the exact output path and provides **Show outputs in Finder**.
Every export records its source fingerprint, crop, profile, command, tool
version, output fingerprint, and timestamp in the catalog, source sidecar, and
derived artifact metadata.

Finished JPEGs preserve capture time, camera/lens details, orientation,
copyright, rating, Lumen description, and keywords. GPS is omitted by default
to make ordinary sharing safer. A future export preset may expose a deliberate
“include location” choice.

### Re-export behavior

Lumen never overwrites an existing JPEG silently. Repeating the same export
creates a uniquely suffixed file. A later dedicated export-management screen
may add replacement policies, but the safe behavior is fixed for this slice.

## Composition and digital zoom

### What “AI crop and zoom” means

Lumen performs a crop, which is a faithful digital zoom. It does not generate
pixels, invent background, or apply generative fill. The crop remains a stored
instruction until export; the RAW stays untouched.

The macOS Vision helper is extended to report face rectangles and
attention-based saliency regions. Lumen uses those measurable regions to make
bounded candidate crop rectangles. It generates:

- **Subject in scene** — keeps environmental context and places space in the
  direction of a face or subject's gaze/motion when that direction is known.
- **Tighter subject** — fills more of the frame while protecting faces, eyes,
  and the padded detected subject region.

Candidate aspect ratios are the camera-native/original ratio, 3:2, 4:5, and
1:1. The UI initially shows the two best meaningfully different candidates and
lets the photographer reveal the others. It overlays thirds lines and reports
the remaining megapixels. Candidates below 8 megapixels are rejected by
default; candidates below 12 megapixels show a resolution warning.

The configured local oMLX vision model may critique the original and candidate
previews for subject emphasis, visual balance, distracting edges, headroom,
lead room, and context. It chooses only among Lumen's valid rectangles and
returns a short explanation. The model does not directly produce final crop
coordinates and cannot override the photographer.

This slice does not include a freeform draggable crop editor. “Manual” means the
photographer chose one of Lumen's validated candidates rather than accepting
the model's recommendation. A full editor can be added after the candidate
workflow proves useful.

The approved crop is stored as normalized `left`, `top`, `width`, and `height`
coordinates plus aspect ratio, source dimensions, author (`manual`, `vision`,
or `omlx-assisted`), and rationale. The JSON/XMP sidecars include that edit and
its provenance. **Keep original** removes the active crop while preserving the
history event.

### Crop fallback

If Vision or oMLX is unavailable, the original remains exportable. When a face
rectangle exists, face-safe candidates still work without oMLX. When no reliable
subject region exists, Lumen does not pretend to understand the composition; it
offers the original and a centered crop only.

Version one supports people, wildlife, pets, and other general subjects through
attention saliency plus an optional oMLX semantic critique. Saliency is an
approximation: Lumen pads the detected region and never claims reliable limb,
eye, gaze, or motion detection for non-human subjects. When that evidence is
weak, it falls back instead of presenting a precise-sounding crop rationale.

## Astrophotography stacking

### Classification and checks

The photographer explicitly marks a group as an astro stack; automatic metrics
may suggest this but never silently change the workflow. Lights are the selected
group. Optional dark, flat, and bias frame folders are chosen with the native
macOS folder picker.

Before running Siril, Lumen shows:

- light and calibration frame counts;
- median background and clipping warnings;
- likely trailing/elongation outliers;
- frames proposed for rejection;
- missing calibration-frame warnings.

The user can include or exclude individual lights. Missing flats, darks, or
biases are warnings rather than blockers.

### Managed Siril job

Lumen creates a self-contained job under
`<library>/outputs/astro/<job-id>/`. It writes a manifest with source paths and
fingerprints and generates a reviewed Siril script for RAW conversion,
calibration when matching frames exist, star registration, quality-based frame
rejection, and stacking. Defaults are global-star alignment and a robust
sigma-based rejection mode supported by the installed Siril version.

Before implementation, the Siril command and generated script syntax must be
validated against the installed local version. Lumen invokes only the detected
local executable, uses argument arrays, captures logs, and never permits a job
to write outside its managed directory.

The primary result is a 16-bit TIFF. Lumen catalogs it as a derived artifact,
shows it in the contact sheet, and offers darktable finishing followed by JPEG
export. A failed or cancelled job keeps its manifest and log, is visibly marked
failed, and never creates a success artifact.

## Housekeeping and recovery

### Remove a group

**Remove batch from Lumen** removes the group's catalog photos, measurements,
tags, grouping state, and managed preview/sidecar files. It does not touch any
source path. Cached previews shared by another catalog photo are retained.
Completed exports are retained.

Retained JPEGs and successful astro TIFFs remain as files on disk but disappear
from the active Lumen workspace when their source group is removed. Artifact
records therefore carry denormalized source names and fingerprints and may use
`ON DELETE SET NULL`; active catalog rows are not required to preserve a file.

### Clear the workspace

**Clear workspace** resets all cataloged photos, groups, measurements, tags,
active edits, jobs, previews, and Lumen sidecars. Completed exports remain.
The confirmation screen states exact counts and requires the user to type
`CLEAR` before the action is enabled.

For recovery, removed managed files and a catalog snapshot are moved into
`<library>/trash/<UTC-timestamp>/` rather than immediately erased. The UI
reports that recovery location. Automatic trash expiry is not part of this
slice.

Failed and cancelled astro job directories and logs move into the same
recoverable trash when their group is removed or the workspace is cleared.
Successful TIFFs are treated as completed exports and stay in `outputs/`.

### Export cleanup

Generated exports are outside normal cleanup. **Review exports** opens their
Finder location. Deleting exports from within Lumen is deferred; Finder and the
recoverable macOS Trash remain the explicit deletion tools for now.

## Interface changes

The existing contact-sheet identity stays intact: charcoal viewing surface,
amber selection, cyan technical status, visible focus, 44-pixel targets, and
plain-language explanations.

The selected group's main area gains Review, Compose, Astro, Export, and Clear
tabs. Tabs are keyboard-operable and preserve the current photo. Long-running
exports and stacks expose progress and a cancellable job state. Destructive
controls never share color or placement with primary finishing actions.

The current compact inspector remains the explanation surface. It shows crop
rationale, output paths, warnings, and recovery locations rather than opening
generic modal dashboards.

## Data and API changes

The catalog receives versioned migrations and three focused records:

- `photo_edits`: one active crop per source or derived photo, with normalized
  coordinates and append-only edit provenance;
- `artifacts`: JPEG/TIFF results with source relation, settings, fingerprints,
  paths, and tool provenance;
- `jobs`: queued/running/succeeded/failed/cancelled export or Siril work with
  progress and logs.

New library operations provide crop candidates, save/clear crop, export one or
many photos, prepare/run an astro job, remove a group, and clear the workspace.
The local HTTP API exposes those operations through same-origin protected
routes. The CLI receives equivalent `export`, `astro prepare/run`, `remove`, and
`clear` commands so UI behavior remains scriptable.

Long-running processes execute outside request threads through a small local job
runner. App restart marks orphaned running jobs interrupted; the user may rerun
them. This version processes one heavy job at a time to avoid exhausting the
48 GB Mac while oMLX is loaded.

## Delivery order

1. JPEG export for the current photo and all keepers, with output provenance.
2. Group removal and recoverable workspace clearing.
3. Vision-based crop candidates, crop approval, and cropped export.
4. Optional oMLX composition critique.
5. Managed Siril job generation, execution, result import, and finishing.

Each step is independently usable and tested. The first two close the immediate
“finish and start again” loop before composition and astro automation add more
surface area.

## Non-goals

- Replacing darktable's full pixel-editing interface.
- AI upscaling, generative fill, object removal, or fabricated pixels.
- Automatic deletion of originals or completed exports.
- Fully automatic astro calibration-frame matching without user confirmation.
- Scientific photometry or guaranteed aesthetic judgment.
- Parallel heavy processing jobs.

## Acceptance criteria

- A selected `.ARW` or all group keepers can become full-resolution, quality-92
  sRGB JPEGs without changing original bytes or mtimes.
- Existing output files are never overwritten silently.
- Composition suggestions remain within image bounds, protect detected subject
  regions, report resolution, and can be accepted or cleared.
- oMLX failure never blocks manual crop or export.
- A managed Siril job uses only selected frames, contains a reproducible
  manifest, keeps its output inside the job directory, and imports a successful
  TIFF as derived data.
- Removing a group or clearing the workspace cannot delete or modify any source
  photograph or completed export.
- Cleanup has a recoverable library trash location and reports it to the user.
- Every external command, crop decision, derived file, and cleanup action has
  provenance.
- Keyboard-only use, visible focus, screen-reader labels, reduced motion, and
  mobile reflow remain intact.

## Builder calls

- The first export UI uses fixed defaults rather than a large preferences
  screen; configurable presets can follow real usage.
- Crop “zoom” is a crop only; invented pixels are excluded.
- The first crop UI chooses among validated candidates; it is not a freeform
  crop editor.
- An XMP profile is always selected explicitly and reused as a remembered
  default; adjacent XMP files are not adopted implicitly.
- Sharing-oriented JPEG metadata omits GPS by default while preserving useful
  photographic and descriptive metadata.
- General-subject crop protection uses padded saliency evidence and honest
  fallback behavior rather than promising semantic body-part detection.
- Lumen-managed trash is used for recoverability and has no automatic expiry.
- Exports are kept during ordinary cleanup and are not deleted in-app in this
  slice.
- Astro groups are explicitly confirmed by the photographer.
- Installed darktable and Siril versions are probed before invoking version-
  specific options or scripts.
