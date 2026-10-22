# Lumen Travel Volume, Culling, and Ingest Design

## Intent

Make Lumen dependable for a three-week expedition with no network, large daily
card volumes, and astrophotography sessions. Lumen already imports, groups,
ranks, tags, crops, exports, and clears. This design closes the gaps that only
show up at volume: getting cards safely onto disk, deciding quickly which
frames matter, keeping the interface responsive while heavy work runs, and
judging sharpness honestly.

This design builds on `2026-10-21-local-raw-manager-design.md` and
`2026-10-21-finish-compose-astro-housekeeping-design.md`. It assumes the astro
stacking plan (`docs/superpowers/plans/2026-10-21-astro-stacking.md`) is
complete, in particular the single-worker local job runner in `jobs.py` and
project-local discovery of darktable and Siril under `vendor/apps/`.

The safety rule still dominates every feature: camera originals are never
edited, renamed, moved, or deleted. Ingested copies on the photographer's SSD
are new originals and inherit the same rule the moment they are written.

## Operating context

- Apple Silicon Mac, 48 GB, running fully offline for the whole trip.
- Several hundred to a few thousand Sony `.ARW` frames per day on busy days.
- Cards are offloaded to one or two external SSDs each evening; cards are
  not formatted until both copies are verified.
- Power can drop without warning; every write must be atomic or resumable.
- Astro sessions produce small light-frame counts with optional calibration
  frames; the existing astro workflow must keep working alongside daytime
  culling.
- oMLX may be loaded (about 16 GB); only one heavy job runs at a time.

## Features

### 1. Verified card ingest

**Purpose:** copy a card to one or two destinations, verify every byte, then
import the copies. The card itself is never written.

Behaviour:

- CLI `lumen ingest <card-path> --to <primary> [--mirror <secondary>]
  [--json]` and an **Ingest** panel in the web UI with the existing macOS
  folder picker for card, primary, and mirror.
- Destinations must be outside the Lumen library. Files land under
  `<destination>/<YYYY-MM-DD>/<card-relative path>` using capture date from
  EXIF, falling back to file modification date, then the ingest date.
- Each file is copied to a temporary name beside its final path, fsynced,
  SHA-256 compared against the source, then renamed into place. A mismatch
  is retried once, then reported as a failure for that file.
- Re-running an interrupted ingest skips files whose destination already
  exists with a matching fingerprint and reports them as `already_present`.
- Ingest runs as a job (feature 3) with per-file progress, is cancellable
  between files, and never leaves a half-written file under a final name.
- A manifest under `<library>/outputs/ingest/<job-id>/manifest.json`
  records source, destinations, per-file fingerprints, sizes, and outcome.
- After a successful primary copy, Lumen imports the primary copies, not the
  card. Mirror copies are verified but not imported; the catalog records the
  mirror path as provenance for each photo.
- The result screen states counts: copied, verified, already present,
  failed, and the exact manifest path. It never claims the card is safe to
  format unless every file verified on every requested destination.

Non-goals: formatting cards, deleting from cards, deduplicating across
different cards.

### 2. Decisions: reject, flag, and rating

**Purpose:** culling needs a decision per frame that is independent of burst
grouping.

Behaviour:

- Each photo gains `decision` (`none`, `flag`, `reject`) and `rating`
  (0 to 5). Both are stored in the catalog, written to JSON and XMP sidecars
  (`xmp:Rating`; rejected frames use rating `-1` in XMP, the convention
  other catalog tools read), and recorded as provenance events.
- Keepers are redefined: a photo is a keeper when it is not rejected and is
  either flagged or the best frame of a multi-frame burst. Single-frame
  groups are no longer keepers by default. `keeper_ids()`, **All keepers**
  export, and the keeper count in the UI use this definition.
- Rejecting the best frame of a burst promotes the next-ranked non-rejected
  frame; a manual pick on a rejected frame clears the rejection.
- Rejected frames are hidden from the contact sheet by default and shown by
  a filter (feature 7). They remain in the catalog, keep previews and
  sidecars, and are included in remove/clear like any other frame.
- CLI: `lumen decide <photo-id> --flag|--reject|--clear` and
  `lumen rate <photo-id> <0-5>`; both support `--json`.
- API: same-origin `POST /api/photos/<id>/decision` and
  `POST /api/photos/<id>/rating` with strict JSON validation.

### 3. Background jobs for import, ingest, and tagging

**Purpose:** keep the UI responsive and make long work resumable.

Behaviour:

- Import, ingest, and tag-all run through the existing `LocalJobRunner` as
  job kinds `import`, `ingest`, and `tag`. Request handlers create the job
  and return immediately with the job ID.
- Jobs report progress as processed/total and a short current-item label
  that never includes a source path or fingerprint in API responses.
- Import is resumable: a photo whose path and fingerprint are already
  cataloged is skipped quickly; a restart marks the job `interrupted` and
  the next import of the same folder completes the remainder.
- Tag-all tags only untagged photos by default, with `--retag` to force,
  and records per-photo failures without stopping the job.
- `GET /api/jobs` and `GET /api/jobs/<id>` return status, progress, label,
  and a bounded log tail. The web UI shows a single job strip under the
  masthead, polls only while a job is active, announces start, completion,
  failure, and cancellation, and disables conflicting actions while a job
  runs. `POST /api/jobs/<id>/cancel` cancels.
- The one-heavy-job rule stays. Import may run while oMLX is loaded; tag
  jobs and astro jobs are mutually exclusive with each other.
- Grouping and sidecar writes happen once at the end of an import job, not
  per photo, exactly as today.

### 4. Compare and loupe

**Purpose:** make the pick decision trustworthy.

Behaviour:

- From the contact sheet, mark up to two frames for comparison; the
  **Compare** view shows them side by side at the same zoom with synced
  panning, each with its name, overall score, sharpness, exposure, eye
  quality when present, decision, and rating.
- A single-frame loupe shows the preview at 100 percent of its stored size
  with a draggable or keyboard-pannable viewport.
- Actions in Compare: pick as best, flag, reject, rate, swap sides, close.
- No new rendering on the server; the view uses the stored preview. When
  feature 8 has refined a preview, the loupe uses the refined one.
- Fully keyboard operable, announced, and reflows to a stacked layout on
  narrow widths.

### 5. Keyboard culling

**Purpose:** cull a thousand frames without touching the mouse.

Behaviour, active only when focus is inside the contact sheet or Compare:

- Arrow keys move between frames; Home/End jump; Page Up/Down move a row.
- `P` pick as best in burst, `X` reject, `F` flag, `U` clear decision,
  `0` to `5` rating, `C` add to compare, `Enter` open loupe, `Escape` close.
- Every action is announced through the existing live region and updates
  the sheet in place without a full reload.
- A visible shortcut legend is available from a **Shortcuts** button and
  is read by screen readers; shortcuts never use modifier combinations that
  macOS or Safari reserve.

### 6. Export presets

**Purpose:** a small file for sharing over slow connections.

Behaviour:

- Presets: `full` (current behaviour: full resolution, quality 92, sRGB)
  and `share` (long edge 2048 px, quality 85, sRGB). Both apply the active
  crop, keep the same metadata rules, and omit GPS.
- Filenames: `<stem>-lumen.jpg` for full, `<stem>-lumen-share.jpg` for
  share, with the existing collision-safe numeric suffix.
- CLI `lumen export --preset share`; web Export panel gets a labelled preset
  radio group. The preset is recorded in artifact settings and provenance.
- Share exports are written by resizing the darktable full render with
  Lanczos in Pillow; no second darktable run.

### 7. Filter and day structure

**Purpose:** find frames once the catalog holds thousands.

Behaviour:

- The batch rail groups bursts under capture-date headings; each heading
  shows frame and keeper counts and can collapse.
- Filters: date range, camera, decision (any/flagged/rejected/undecided),
  minimum rating, keepers only, tagged only, and a text filter over oMLX
  description and subjects. Filters combine with AND.
- `GET /api/groups` accepts the same filters as query parameters and
  returns counts for the filtered and total sets. Invalid parameters are
  rejected with a public error.
- Filter state lives in the URL query so a reload keeps it; nothing is
  stored server side.
- The contact sheet renders in pages of 200 frames with a visible
  "show more" control so the DOM stays small; lazy image loading stays.

### 8. Refined previews

**Purpose:** sharpness decisions should not rest on a 1616 by 1080 embedded
preview.

Behaviour:

- Import keeps using the fast embedded preview so a card is browsable
  quickly.
- A **Refine previews** job renders a 2048 px long-edge preview through
  `darktable-cli` for the selected burst, filtered set, or whole catalog.
  It replaces the preview file under a new name keyed by fingerprint and
  size, updates `preview_path`, recomputes sharpness, exposure, clipping,
  astro screening, and Vision scores, re-ranks affected groups, and
  rewrites sidecars.
- The preview URL version changes so browsers refetch; the contact sheet
  marks refined frames with a non-colour indicator.
- When darktable is unavailable the job is unavailable and the UI says
  why; nothing else changes.
- Refinement never changes a manual pick, decision, or rating.

## Cross-cutting rules

- No network access of any kind; no new runtime Python dependencies.
- Every new mutation route stays loopback-only with host/origin checks and
  strict JSON validation; responses never include source paths,
  fingerprints, API keys, or raw model output.
- Every new decision, job, ingest, and export is visible in provenance and
  sidecars.
- All writes are atomic (temporary file plus rename) or explicitly
  resumable; a power cut mid-job must leave the catalog consistent and the
  job marked `interrupted` on restart.
- After a successful ingest job, Lumen writes a catalog snapshot to
  `<primary destination>/lumen-catalog-backups/<UTC-timestamp>.sqlite3` so
  the SSD carries a recoverable catalog alongside the photos.
- Tests generate their own small images and never require a personal RAW
  library; darktable- and Siril-dependent paths are mocked with the runner
  pattern already used in `pipelines.py`.
- Accessibility rules from the earlier designs stay intact: native
  controls, visible focus, 44 px targets, live announcements, reduced
  motion, mobile reflow, charcoal/amber/cyan palette, and non-colour cues
  for every state.

## Data and API summary

Catalog additions (versioned migrations, additive only):

- `photos.decision TEXT NOT NULL DEFAULT 'none'`,
  `photos.rating INTEGER NOT NULL DEFAULT 0`,
  `photos.mirror_path TEXT`, `photos.preview_kind TEXT NOT NULL DEFAULT
  'embedded'`.
- `jobs.kind` gains `import`, `ingest`, `tag`, `refine_previews`;
  `jobs.label TEXT`, `jobs.processed INTEGER`, `jobs.total INTEGER`.
- `artifacts.settings_json` gains `preset`.

New or changed routes, all same-origin protected where they mutate:

- `POST /api/ingest`, `POST /api/import` (now returns a job),
  `POST /api/photos/tag-all`, `POST /api/previews/refine`.
- `GET /api/jobs`, `GET /api/jobs/<id>`, `POST /api/jobs/<id>/cancel`.
- `POST /api/photos/<id>/decision`, `POST /api/photos/<id>/rating`.
- `GET /api/groups?...filters`.
- `POST /api/export` accepts `preset`.

New CLI commands: `ingest`, `decide`, `rate`, `jobs`, `cancel`,
`refine-previews`; `export --preset`; `tag --all --retag`; `groups` accepts
filter flags.

## Delivery order

1. Background jobs for import and tagging (feature 3). Everything after
   this depends on it, and it removes the biggest usability risk at volume.
2. Decisions and the keeper redefinition (feature 2), with sidecar and
   export changes.
3. Verified card ingest (feature 1).
4. Keyboard culling (feature 5).
5. Compare and loupe (feature 4).
6. Filter and day structure (feature 7).
7. Export presets (feature 6).
8. Refined previews (feature 8).

Each step is independently usable and tested. Steps 1 to 3 are the minimum
for the trip; 4 to 8 improve speed and judgement.

## Non-goals

- Formatting or writing to memory cards.
- Cloud sync, sharing services, or any network feature.
- A freeform crop editor, pixel editing, or generative features.
- Perceptual near-duplicate grouping (still an extension point).
- Automatic deletion of rejected frames or any original.
- Video files.

## Acceptance criteria

- A full card can be ingested to two SSDs with every file fingerprint
  verified, interrupted midway by killing the process, and completed on
  rerun without re-copying verified files or leaving partial files.
- Importing 1,000 generated frames leaves the web UI responsive, shows
  progress, and survives a server restart as `interrupted` with a
  completing rerun.
- Rejecting, flagging, and rating are reachable by keyboard, announced,
  persisted in catalog and both sidecars, and change which frames **All
  keepers** exports. Single frames are not exported unless flagged.
- Compare shows two frames at matched zoom with synced panning and allows
  pick, flag, reject, and rate without leaving the view.
- Filters narrow the sheet and rail, survive reload, and reject invalid
  parameters with a public error.
- A share export is no larger than 2048 px on its long edge, carries the
  same metadata rules as full export, and is recorded with its preset.
- Refining previews changes measured sharpness for affected frames and
  re-ranks groups without altering any manual pick, decision, or rating.
- No test, job, or route can modify, move, rename, or delete a source file
  or an ingested copy.

## Builder calls

- Ingested copies live where the photographer chooses, outside the library,
  because they are originals; only manifests and catalog snapshots are
  Lumen-managed.
- Import stays fast with embedded previews; refinement is an explicit job,
  not an import default.
- Keepers exclude unflagged single frames; this is the change most likely
  to surprise, so the Export panel states the definition in plain words.
- Rejection is a decision, never a deletion.
- One heavy job at a time remains the rule; import is allowed alongside a
  loaded oMLX model because it is not memory-heavy.
- Share exports resize the darktable render in Pillow rather than invoking
  darktable twice.
