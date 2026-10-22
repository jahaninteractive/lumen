# Architecture and extension guide

Lumen keeps the pipeline deliberately small and replaceable:

```text
RAW/JPEG folder
    │
    ├─ metadata + embedded preview ── ExifTool / sips / darktable
    │                                  │
    │                                  ▼
    │                           managed JPEG cache
    │                                  │
    ├─ SQLite catalog ◀──── analysis / grouping / scoring
    │         │                        │
    │         ├─ provenance            ├─ Pillow measurements
    │         ├─ groups + picks        ├─ macOS Vision faces/eyes
    │         └─ tags                  └─ astro screening
    │
    ├─ JSON + XMP sidecars
    ├─ oMLX loopback vision tags
    ├─ managed JPEG exports ── darktable-cli (crop applied at export)
    └─ managed astro stacks ── jobs table + one worker ── siril-cli
```

## Modules

- `config.py` owns filesystem and local-endpoint configuration.
- `catalog.py` is the SQLite boundary and append-only provenance store.
- `previews.py` fingerprints originals and creates bounded cached previews.
- `analysis.py` contains measurable image and astrophotography signals.
- `grouping.py` owns the replaceable burst policy.
- `scoring.py` owns explainable ranking and missing-signal renormalization.
- `sidecars.py` atomically writes portable JSON/XMP metadata.
- `omlx.py` is the strict local VLM adapter and response validator.
- `pipelines.py` runs allow-listed processors with argument arrays.
- `composition.py` derives deterministic, non-destructive crop candidates.
- `jobs.py` is the persisted job lifecycle: validated transitions, restart
  recovery, one exclusive runner per catalog, one heavy worker, and
  owned-process cancellation.
- `astro.py` probes Siril, builds frozen frame sets and a fingerprinted
  manifest, writes the version-gated `stack.ssf`, and executes/verifies a job.
- `library.py` orchestrates imports while keeping domain modules independent.
- `cli.py` and `web.py` are two interfaces over the same library facade.

## Previews

Imports cache the embedded camera JPEG as `previews/<fingerprint>.jpg`
(`preview_kind = "embedded"`). The `refine_previews` job renders each selected
original with `darktable-cli` (2048 px, `--core --conf
plugins/lighttable/export/metadata_flags=5`) into a temporary folder beside the
cache, normalises it to `previews/<fingerprint>-2048.jpg`, and renames it into
place, so a preview is never half-written. It verifies the source fingerprint
before and after the render, sets `preview_kind = "refined"`, re-runs the
import measurements, records a `preview_refined` event, then re-ranks each
affected group with the normal selection rule. It never writes `manual_pick`,
`decision`, or `rating`. The old embedded file is removed only when no other
photo references it. Preview URLs carry `?v=<fingerprint[:16]>-<preview_kind>`
so browsers refetch after refinement.

## Managed astro stacking

Everything a stack needs lives under `<library>/outputs/astro/<job-id>/`:
read-only symlinks to the lights and calibration frames (fixed ASCII names, so
no source path enters the script), an empty `siril.config`, `stack.ssf`,
`manifest.json`, `siril.log`, Siril's `process/` scratch, and `result.tif`.
`manifest.json` and `stack.ssf` are written once and made read-only.

Flow: `Library.prepare_astro_job` requires `confirmed=True`, re-fingerprints
every selected light against the catalog, probes Siril (`>=1.4.0,<1.5.0`),
inserts a `pending` job row, prepares the folder with no SQLite lock held, then
stores the paths. `Library.run_astro_job` re-loads the manifest and submits it
to the `LocalJobRunner`; the worker revalidates size/mtime/sha256 of every
frame, refuses stale `result.tif`/`process/`, launches the argument array with
the job folder as cwd, streams the log, and requires a 16-bit TIFF even on
exit 0. Success records an `astro_tiff` artifact plus `astro_stacked`
provenance and sidecars; any bookkeeping failure discards the artifact.

Exactly one `LocalJobRunner` may own a catalog: the web server creates it
eagerly (recovering stale `running` rows as `interrupted`) and every request
`Library` borrows it, so a CLI `astro run` against a served library fails
honestly. `Library.job_status` is the only view the CLI and browser see: a
whitelist of status, progress, counts, warnings, a path-scrubbed log tail, and
`{artifact_id, name}`; never the manifest, absolute paths, fingerprints, or the
launch command. The Astro tab polls that view only while a job is queued or
running and keeps the manifest/log locations visible after failure or
cancellation.

## Add a feature

Prefer one of these seams instead of changing the import loop directly:

- Add a preview decoder with the `create_preview(source, destination)` contract.
- Add an analyzer that produces a normalized optional signal in `[0, 1]`.
- Add a grouping policy returning deterministic `list[list[Photo]]` groups.
- Add a local VLM by preserving `TagResult` and loopback-only transport.
- Add a processor that validates its output root and returns `PipelineResult`.

Every new machine decision must be visible in provenance and sidecars. Missing
optional engines must remove their weight from a score, not contribute a silent
zero. Tests should generate their own small images and never require a personal
RAW library.

## Data ownership

The source path plus fingerprint identifies a catalog item. Identical bytes at
different paths remain separate items with independent user state, while cached
pixels may be shared. Automatic grouping is library-wide. Reimport can extend a
group, but a manual pick is preserved. Reversibility means recompute/override
plus audit history in version one; arbitrary historical rollback remains a
future UI and schema feature.

