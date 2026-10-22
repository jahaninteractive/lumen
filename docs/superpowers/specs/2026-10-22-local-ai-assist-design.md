# Lumen Local AI Assist Design

## Intent

Use the two vision models already running in oMLX to take the repetitive
judgement work off the photographer during a long trip: tagging keepers,
naming wildlife, suggesting the best frame of a burst, flagging obvious
failures, triaging astro frames, writing a daily digest, and producing
captions. Every model output is advisory, recorded in provenance, and
applied only when the photographer acts.

This design builds on `2026-10-22-travel-volume-culling-design.md` and
assumes its Task 1 (background jobs) and Task 2 (decisions) are complete.
It reuses `omlx.py`'s loopback-only transport, strict response parsing, and
the `tag_failed`/`composition_review_failed` provenance pattern.

## Models and roles

Two local models are available in oMLX:

- `Qwen3-VL-8B-Instruct-4bit`: fast. Use for bulk work: tagging, species
  identification, reject scanning, astro triage.
- `Qwen3.8-27B-4bit`: slower, stronger. Use for judgement: burst critique,
  captions, daily digest, composition review (already implemented).

Settings gain two roles, each an oMLX model name:

- `LUMEN_OMLX_FAST_MODEL`, default `Qwen3-VL-8B-Instruct-4bit`
- `LUMEN_OMLX_REVIEW_MODEL`, default the existing `LUMEN_OMLX_MODEL`

Before any job starts, Lumen reads `/api/status` and refuses to run a role
whose model is not in `loaded_models`, with a public message naming the role
and model. Configured names are never trusted over live status. `lumen
doctor` reports both roles and whether each is loaded.

Both models receive images as base64 `image_url` parts using the request
shape already validated in `docs/MODELS.md`. Every request asks for JSON
only, and every response goes through a strict parser that rejects missing
or extra fields; raw model text is stored in provenance only, never
returned to the browser.

## Controlled vocabulary

A JSON file at `<library>/vocabulary.json` lists allowed keywords grouped
by category, for example `wildlife`, `scene`, `conditions`, `activity`.
Lumen ships a starter file for this trip (penguin, seal, whale and seabird
species of the Antarctic Peninsula; ice, zodiac, ship, station, aurora,
Milky Way, twilight) and copies it into the library on first use if absent.
The photographer edits it freely.

Models are asked to choose keywords from the vocabulary where one fits and
may add at most three free-text keywords, which are stored separately as
`suggested_keywords` so the vocabulary can be extended deliberately. Species
identification always carries a confidence; below 0.6 the keyword is
stored as suggested, not applied.

## Features

### 0. Keeper tagging (prerequisite)

The tag job gains a scope: `keepers` (default), `all`, or explicit photo
IDs; untagged only unless retagging. Uses the fast model. CLI
`lumen tag --keepers`, API `POST /api/photos/tag`, and a "Tag keepers with
local AI" button in the Export panel with the keeper count in its label.
Per-photo failures are recorded and skipped.

### 1. Wildlife and scene identification

Runs inside the tag job. The prompt supplies the vocabulary and asks for
`species` (list of {name, confidence}), `scene` keywords, `conditions`
keywords, and the existing description/subjects/technical fields. Applied
keywords go to `tags.data.subjects` and the XMP `dc:subject` bag; suggested
ones go to `tags.data.suggested`. The inspector shows applied keywords as
text and suggested ones with an "Add" button each. A filter (travel spec
feature 7) can select by any vocabulary keyword.

### 2. Burst critique

For a selected multi-frame group, send the previews (capped at 8 frames,
chosen by current rank) to the review model with their frame labels and
ask for one `frame_id`, a rationale under 300 characters, and confidence.
Shown in the inspector as "Local AI suggests frame N: rationale" with a
native "Use this pick" button that calls the existing manual pick. Never
changes the pick on its own. Event `burst_reviewed`; failure event
`burst_review_failed`. CLI `lumen critique <group-id>`.

### 3. Reject suggestions

A job over keepers (fast model) that asks, per frame, for `problems`: a
list drawn from a fixed set (`eyes_closed`, `motion_blur`, `out_of_focus`,
`subject_cut_off`, `obstructed`, `misfire`, `severely_under_exposed`,
`severely_over_exposed`) each with confidence, plus `suggest_reject`
boolean. Results are stored as `tags.data.problems` and shown in a
**Review suggestions** panel listing suggested rejects with thumbnails,
problems, and two native buttons per row: "Reject" and "Keep". Keyboard:
X rejects, K keeps, arrows move. Nothing is rejected until the photographer
presses a button; "Reject all shown" exists but requires a confirmation
step that states the exact count. Event `reject_suggested`.

### 4. Astro triage

Runs in the tag job for frames whose exposure is 1 s or longer, or on
demand for a group. The fast model classifies `sky` as one of
`night_sky`, `aurora`, `milky_way`, `twilight`, `cloud`, `not_sky`, with
confidence, and `issues` from `haze`, `light_pollution`, `trailing`,
`cloud_band`. Combined with the existing star_count and trailing metrics,
the Astro tab shows per-light triage before preparation and offers
"Exclude flagged lights" as a single native action the photographer
confirms. A group where most frames are `night_sky`, `aurora` or
`milky_way` gets a "Looks like astro" cue in the burst header; it never
switches mode by itself.

### 5. Daily digest

CLI `lumen digest <YYYY-MM-DD>` and a "Write today's digest" button in the
rail. Lumen assembles the day's facts itself (frame count, keepers,
rejects, bursts, astro jobs and results, applied keywords by frequency,
top five frames by score) and asks the review model only to write a short
narrative from those facts, with no images sent. Output is a Markdown file
at `<library>/digests/<date>.md` containing the facts table first and the
narrative second, so the file is useful even if the model is unavailable.
Event `digest_written`. Digests are plain files; re-running overwrites
after a confirmation.

### 6. Captions and alt text

For keepers, on demand or as part of share export: the review model
returns `caption` (under 200 characters, no hashtags) and `alt_text`
(under 125 characters, literal description). Stored in `tags.data`,
editable in the inspector with native text inputs, and written to the
share-preset JPEG as the EXIF/XMP description (caption) with alt text in
`Iptc4xmpCore:AltTextAccessibility` in the XMP packet. Full-preset export
keeps the existing description behaviour.

### 7. Natural-language find

No model call. The text filter from travel spec feature 7 searches
description, subjects, suggested keywords, species names, caption and
problems. A small "Find" input in the rail header applies it. Matching
terms are shown in the inspector so the photographer can see why a frame
matched.

## Cross-cutting rules

- Advisory only: no feature changes a pick, decision, crop, keyword, or
  export without an explicit photographer action, except keyword
  application inside the tag job, which the photographer requested by
  running it and can undo per keyword.
- All model calls go through `OmlxTagger`-style classes in `omlx.py` with
  loopback-only URLs, JSON-only prompts, strict parsers, and bounded
  `max_tokens`. Prompts include "Do not identify a person."
- Every call records model name, role, confidence, and raw response in
  provenance; browser responses carry parsed fields only.
- Jobs run through the local job runner, one heavy job at a time, with
  progress, cancel, and per-item failure capture.
- Vocabulary, digests, and all outputs live inside the library.
- Tests use fake taggers and generated images; no test calls oMLX.

## Data and API summary

- Settings: `omlx_fast_model`, `omlx_review_model`.
- `tags.data` gains `species`, `suggested`, `problems`, `sky`, `issues`,
  `caption`, `alt_text`; all optional.
- New provenance kinds: `burst_reviewed`, `burst_review_failed`,
  `reject_suggested`, `astro_triaged`, `digest_written`,
  `caption_written`.
- Routes, same-origin where they mutate: `POST /api/photos/tag`
  (scope), `POST /api/groups/<id>/critique`,
  `POST /api/photos/review-rejects`, `POST /api/groups/<id>/triage`,
  `POST /api/digests`, `POST /api/photos/<id>/caption`,
  `GET /api/vocabulary`.
- CLI: `tag --keepers`, `critique`, `review-rejects`, `triage`,
  `digest`, `caption`.

## Delivery order

1. Model roles, live-status gating, vocabulary file, keeper tagging with
   species and scene fields (features 0 and 1).
2. Reject suggestions and the review panel (feature 3).
3. Burst critique (feature 2).
4. Astro triage (feature 4).
5. Captions and alt text (feature 6).
6. Daily digest (feature 5).
7. Natural-language find (feature 7), which only needs the filter from the
   travel plan to exist.

Steps 1 and 2 give the most time back per day; 3 and 4 improve the pick
and the stacks; 5 to 7 are quality of life.

## Non-goals

- Automatic rejection, picking, or keyword removal.
- Face recognition or identifying people.
- Any cloud model or network call.
- Training or fine-tuning.
- Scientific species certainty; identification is a labelled suggestion.

## Acceptance criteria

- With only the fast model loaded, tagging runs and features needing the
  review model report a clear unavailable message naming the model.
- Tagging 50 generated keepers applies only vocabulary keywords or
  high-confidence species, stores the rest as suggested, and records every
  response in provenance without exposing raw text to the browser.
- The reject review panel lists suggestions, applies a rejection only on
  button or key press, and "Reject all shown" states the count first.
- Burst critique returns a frame from the supplied set or fails cleanly;
  the pick never changes without "Use this pick".
- Astro triage marks lights and "Exclude flagged lights" changes the job's
  selected set only after confirmation.
- A digest file is written with the facts table even when the model call
  fails, and the narrative is appended when it succeeds.
- Captions and alt text appear in the share JPEG metadata and nowhere in
  the full-preset export unless the photographer set a description.
- Full warning-strict suite and node --check pass; no test contacts oMLX.

## Builder calls

- Two model roles rather than one, gated by live status, because the 8B
  makes bulk work practical and the 27B makes judgement credible.
- A vocabulary file the photographer owns, so keywords stay consistent for
  the eventual Lightroom import.
- Facts first, narrative second in digests, so the file is never empty.
- Reject review is a confirm-list, never a silent sweep.
