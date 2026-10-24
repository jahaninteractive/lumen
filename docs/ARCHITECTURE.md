# Architecture and extension guide

Lumen keeps the pipeline deliberately small and replaceable:

```text
RAW/JPEG folder
    │
    ├─ metadata + embedded preview ── ExifTool / sips / darktable
    │                                  │
    │                                  ▼
    │                           managed JPEG cache ── 480 px thumbnail tier
    │                                  │
    ├─ SQLite catalog ◀──── analysis / grouping / scoring
    │         │                        │
    │         ├─ provenance            ├─ Pillow measurements
    │         ├─ groups + picks        ├─ macOS Vision faces/eyes
    │         ├─ tags + location       └─ astro screening
    │         └─ jobs
    │
    ├─ JSON + XMP sidecars
    ├─ oMLX loopback (fast + review roles) ── tags, suggested rejects,
    │      critique, captions, digests, triage, composition advice
    ├─ vocabulary.json (photographer-owned, learns from tags)
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
- `omlx.py` is the strict local VLM adapter and response validator: one
  `OmlxTagger` per model role, lenient on unknown keys, clips over-long text.
- `vocabulary.py` loads the photographer-owned keyword list (copied once from
  `data/vocabulary.json`), matches model words to it, and appends learned words.
- `digest.py` computes one capture day's facts and renders them as Markdown
  before any model narrates them.
- `diagnostics.py` builds the `lumen doctor` / `/api/status` report, including
  live `omlx_roles`.
- `tools.py` resolves `darktable-cli` and `siril-cli` from `PATH`, then the
  project-local `vendor/apps/` bundles, never outside the bundle.
- `domain.py` holds the shared dataclasses (`Photo`, `TagResult`, requests).
- `pipelines.py` runs allow-listed processors with argument arrays.
- `composition.py` derives deterministic, non-destructive crop candidates.
- `jobs.py` is the persisted job lifecycle: validated transitions, restart
  recovery, one exclusive runner per catalog, one heavy worker, and
  owned-process cancellation.
- `astro.py` probes Siril, builds frozen frame sets and a fingerprinted
  manifest, writes the version-gated `stack.ssf`, and executes/verifies a job.
- `ingest.py` plans a card copy, copies with SHA-256 verification to each
  destination, classifies reruns, and writes the resumable manifest.
- `library.py` orchestrates imports and every job while keeping domain modules
  independent; `Library.tagger_for(role)` is the only `OmlxTagger` constructor
  and refuses a model oMLX has not loaded.
- `cli.py` and `web.py` are two interfaces over the same library facade.

## Previews

Imports cache the embedded camera JPEG as `previews/<fingerprint>.jpg`
(`preview_kind = "embedded"`). `GET /thumb/<id>` serves a lazily built 480 px
JPEG from `cache/thumbs/<preview stem>-<preview mtime_ns>.jpg` for the contact
sheet; a refined preview gets a fresh thumb and the old file is stale cache. The `refine_previews` job renders each selected
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

The same runner carries the generic job kinds `import`, `ingest`,
`refine_previews`, `export` (keepers), and the per-photo model jobs `tag`,
`review_rejects`, `assist`, and `triage`, all built on
`Library._run_photo_job(kind, tagger, scope, per_photo, counters=)`. Each job's
work opens its own `Library` on the worker thread and takes `_mutation()`
around every catalog write (one photo per lock for export), so request handlers
only create the job and return its ID. One-shot model calls (`critique_burst`,
`caption_photo`, `digest_day`, `review_composition`) run inline under the
mutation lock; the browser marks itself busy for their duration.

## Local AI contract

Model output is advisory. `tags.data` is a read-modify-write JSON blob with
optional keys (`description, subjects, scene, technical, confidence, species,
conditions, suggested, problems, sky, issues, caption, alt_text`); a photo is
"tagged" only when it has a `description`, and `assist` skips each step whose
key is already present. A caption typed by the photographer is never
overwritten. Suggested rejects live under `problems` until confirmed or
dismissed; critique and triage never change picks or exclusions. Browser
responses carry public messages only, never raw model text.

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

