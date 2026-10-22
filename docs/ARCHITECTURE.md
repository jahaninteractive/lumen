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
    └─ managed exports ── darktable now; Siril and other hooks next
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
- `library.py` orchestrates imports while keeping domain modules independent.
- `cli.py` and `web.py` are two interfaces over the same library facade.

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

