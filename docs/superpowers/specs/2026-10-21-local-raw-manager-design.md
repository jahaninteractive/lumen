# Local RAW Photography Manager Design

## Intent

Build a fully local photography workbench for one photographer using a 48 GB
Apple Silicon Mac and Sony `.ARW` files. The first useful workflow is: point it
at a folder, obtain previews without altering originals, group related frames,
rank the best shot, optionally add visual tags with oMLX, and keep every machine
decision explainable and reversible.

The application is named **Lumen** in the UI and package metadata.

## Goals

- Never modify or relocate a RAW original.
- Generate and cache JPEG previews from embedded RAW previews, with darktable as
  an optional fallback.
- Import folders recursively in repeatable batches.
- Group bursts by capture time and camera, with an extensible grouping policy.
- Rank each group from measurable sharpness, exposure, and face/eye quality;
  allow optional oMLX judgment without making it authoritative.
- Tag preview images through the local oMLX OpenAI-compatible vision endpoint.
- Persist metadata, decisions, and append-only provenance locally in SQLite and
  portable JSON/XMP sidecars.
- Provide basic astrophotography checks and hooks for local processors.
- Offer a keyboard-friendly, high-contrast browser UI and a scriptable CLI.
- Run on macOS Apple Silicon with clear diagnostics and automated tests.

## Non-goals for the first version

- Replacing Lightroom/darktable as a pixel editor.
- Training or fine-tuning vision models.
- Cloud sync, accounts, remote access, or collaborative catalogs.
- Destructive deletion, renaming, or movement of photographs.
- Inventing a universal aesthetic score. Automated ranking is a shortlist with
  visible component scores and can be overridden by the photographer.
- RAW stacking inside Lumen. Siril/darktable hooks hand work to mature tools.

## Constraints and safety

- Python 3.12+ on Apple Silicon macOS.
- Runtime data stays beneath a user-selected library directory; the default is
  `~/Pictures/Lumen Library`.
- Network calls are limited to the configured loopback oMLX endpoint.
- Originals are opened read-only and identified by content fingerprint plus
  path. Generated files carry source fingerprints and command provenance.
- Builder call: identical bytes at different paths are separate catalog photos
  with independent user state, while immutable cached preview pixels may be
  shared by fingerprint. Sidecars include the photo ID to prevent collisions.
- External commands are argument arrays, never shell strings.
- The server binds to `127.0.0.1` by default.

## Validated model decision

On 21 October 2026, this Mac's oMLX 0.7.x discovered
`Qwen3.8-27B-4bit` as a VLM, loaded a roughly 15.7 GB vision model, and
correctly described a local test image sent using the OpenAI `image_url` data
URI format. Its config contains a `vision_config` and vision token IDs.

Therefore Lumen defaults to the already-installed `Qwen3.8-27B-4bit`. No model
download is required. The partially downloaded
`Qwen3-VL-8B-Instruct-4bit` is not recommended until its four missing weight
shards finish downloading and it passes the same image smoke test. A smaller
Qwen3-VL checkpoint remains a fallback option only if memory pressure or batch
throughput proves more important than the validated model's quality.

## Architecture

### Library core

SQLite stores photos, groups, jobs, scores, tags, and provenance. Domain modules
own scanning, preview generation, measurements, grouping, selection, sidecars,
oMLX calls, and processing pipelines. The CLI and HTTP service call the same
`Library` facade.

### RAW preview path

1. Read metadata with ExifTool.
2. Extract `PreviewImage`, then `JpgFromRaw`, then `ThumbnailImage`.
3. If those are unavailable, ask darktable to export a JPEG when installed.
4. Cache under `<library>/previews/<fingerprint>.jpg` and record the tool,
   version, source fingerprint, and timestamp.

### Analysis and best shot

Pillow computes a normalized Laplacian sharpness score and histogram-based
exposure score on a bounded preview. A small Swift helper uses macOS Vision to
detect faces and face quality; eye landmarks contribute an eye score. If Vision
is unavailable, scoring continues with that component marked unavailable rather
than silently treating it as zero.

Default ranking weights are sharpness 50%, exposure 25%, and face/eye quality
25% when faces exist. Missing optional components are removed and remaining
weights are renormalized. The selected frame and component scores are written to
the catalog and sidecar. Manual selection always wins.

Bursts are grouped by camera identity and capture-time gap, defaulting to 2.0
seconds. Future perceptual-similarity grouping can implement the same policy
interface.

Builder call: grouping is library-wide rather than limited to one import run.
Later imports may extend an automatic group, but an existing manual best-shot
choice is preserved until the photographer explicitly changes it.

### Vision tagging

The oMLX adapter sends the cached JPEG preview to the loopback
`/v1/chat/completions` endpoint, requesting strict JSON with description,
subjects, scene, technical observations, and confidence. It reads an API key
from `LUMEN_OMLX_API_KEY`, falling back to the local oMLX settings file. Malformed
responses are preserved in provenance and reported, never guessed into tags.

Optional group comparison sends contact-sheet previews only when the user asks
for visual AI review. Measured score remains visible beside the AI suggestion.

### Astrophotography

An analyzer reports background brightness, highlight clipping, approximate star
count, and elongation/trailing warning from a downsampled preview. Processing
adapters can create darktable exports and Siril scripts/manifests; hooks are
capability-checked and show the exact output path and command in provenance.

### Sidecars and provenance

Every photo gets `<library>/sidecars/<photo-id>-<fingerprint>.lumen.json` and an XMP file
with portable rating, label, title, description, and keywords. JSON contains the
source path/fingerprint, measurements, group/selection state, tags, generated
artifacts, and a chronological provenance array. Sidecar replacement is atomic.

Builder call: reversibility in version one means originals never change,
machine results can be overridden/recomputed, and prior decisions remain in the
audit history. A one-click restoration UI for arbitrary historical states is a
future feature, not a version-one requirement.

### Interfaces

- `lumen doctor`: report available tools, local model readiness, and fixes.
- `lumen import PATH`: import/reimport a batch, create previews, analyze, group,
  rank, and write sidecars.
- `lumen tag [--photo ID | --all]`: add local oMLX tags.
- `lumen process PHOTO_ID --pipeline NAME`: run an optional non-destructive
  export pipeline.
- `lumen serve`: open the local contact-sheet UI.

### UI direction and accessibility

The UI resembles a digital contact sheet, not a generic dashboard: charcoal
viewing surface, graphite controls, paper-white type, amber selection, and cyan
technical status. System fonts keep it native and offline. A left batch rail, a
large central contact sheet, and a right inspection panel adapt to one column on
small screens. Semantic landmarks, labeled controls, visible focus rings,
44-pixel targets, text equivalents for scores, reduced-motion support, and no
color-only states are required.

## Extension points

- `PreviewProvider` for additional RAW decoders.
- `GroupingPolicy` for perceptual or event-aware clustering.
- `Analyzer` for animal eyes, smiles, motion, or domain-specific quality.
- `VisionTagger` for other OpenAI-compatible local VLMs.
- `Pipeline` for darktable styles, DxO exports, Siril stacks, or custom tools.
- Versioned sidecar schema and database migrations preserve future compatibility.

## Acceptance criteria

- Importing a folder with supported images creates catalog rows, cached previews,
  deterministic groups, scores, selected best shots, and sidecars without
  changing original file bytes or mtimes.
- Reimport is idempotent.
- The doctor explains missing optional tools and verifies oMLX with a real image
  only when explicitly requested.
- A failed preview, VLM, Vision, or processor call is recorded and does not
  corrupt the catalog or originals.
- The UI works with keyboard-only navigation and exposes why a frame won.
- Automated tests cover grouping, scoring, sidecar atomicity, oMLX parsing,
  astrophotography metrics, idempotent import, HTTP traversal protection, and
  original-file preservation.
