# Local model speed and accuracy: handoff

Written 2026-10-28. Read `AGENTS.md` and `docs/HANDOFF.md` first; the rules
there (loopback only, no test contacts oMLX, Pillow the only runtime
dependency, warning-strict red-green tests, no AI vendor names in tracked
files) apply to everything below.

## The question

The photographer likes the 27B's descriptions and develop advice but not its
speed. The goal is the 27B's accuracy at a cost close to the 8B's, or a clear
reason why that cannot be had.

Models on this Mac (48 GB M4, oMLX 0.7.2, all 4-bit MLX):

| Name | Disk | Role today |
|------|------|------------|
| `Qwen3-VL-8B-Instruct-4bit` | 5.4 GB | fast role (tags, reject scan, astro triage) |
| `Qwen3.8-27B-4bit` | 15 GB | review role when loaded (critique, captions, digest, composition, develop advice) |
| `Qwen3.5-9B-4bit` | 5.6 GB | tested and rejected (see below) |
| `Qwen3.6-35B-A3B-8bit` | 96 KB | download never finished; untested |

## What is already done (on `main`)

- `af20533`: the **Review model** dropdown in the page header. `lumen serve`
  (`cli.serve_settings`) starts review on the 27B when oMLX has it loaded,
  else on the 8B, unless `LUMEN_OMLX_REVIEW_MODEL` is set. `POST
  /api/review-model {model}` accepts only the two names in `review_models` on
  `/api/status` and rebinds the server's `settings` (`nonlocal` in
  `web.do_POST`) until restart. The fast role has no switch yet.
- Same commit: the develop ("Where to start") prompt in
  `OmlxTagger.suggest_develop` now states each control's direction, that a
  histogram median near 50 is balanced, and the exact JSON shape.
- `607aaef`: README "Choosing the review model" section.

## Done 2026-10-28 (steps 1 to 3 below, uncommitted in the tree; step 6 measured and declined)

- `OmlxTagger._image_content(path, long_edge=MODEL_IMAGE_LONG_EDGE)`
  (`omlx.py`, 1024) resizes with Pillow before encoding and re-encodes as
  JPEG quality 90; images already inside the edge are sent as they are (PNG
  stays PNG). Every caller gets it: tagging, captions, composition, reject
  scan, triage; the develop render was already 1024. `critique_burst` passes
  `long_edge=0` because it compares sharpness between frames.
- The tag prompt ends "Write the JSON compact on one line, no code fence."
  Only `tag` asks for it; it has the longest answers.
- Measured through the unmodified tagger (`live_check.py`, safari frames,
  both models loaded, no alternation, 3 fresh frames per cell):

  | Change | 27B per frame | 8B per frame |
  |--------|---------------|--------------|
  | before (1616, fenced JSON) | about 18 s | about 7 s |
  | 1024 only | 13.5 to 15.7 s | 3.0 to 3.9 s |
  | 1024 and compact | 8.7 to 11.1 s | 3.1 to 3.3 s |

  So the size is most of the 8B's gain and the compact answer is a further
  quarter for the 27B. Descriptions still place the subject, name the light
  and the background. Unchanged 8B habit, now with a sample: on a frame of
  hillside with no animal it listed three species at high confidence
  (`results/live-check-1024.json`).

## Measurements

All on frames from one day of seabird ARWs (open ocean, albatrosses and
petrels), read from a library trash snapshot. One subject type, small samples:
treat them as directional, not a benchmark. Raw outputs are in `results/`.

### Develop advice (6 frames, all underexposed: histogram medians 30 to 38)

| Model and prompt | Parsed | Right direction (brighten) | Time |
|------------------|--------|----------------------------|------|
| 27B, old prompt | 6/6 | 6/6, +0.5 to +1.0 EV | about 15 s |
| 8B, old prompt | 5/6 | 0/6: -0.5 EV every time, rationale calls it a lift | about 3 s |
| 9B, old prompt | 0/6 | settings not wrapped, 6-point curve, rationale outside JSON | about 5 s |
| **8B, new prompt** | 6/6 | 5/6, mostly a timid +0.3 | about 2 s |
| **27B, new prompt** | 6/6 | 6/6, +0.5 to +1.0 | about 13 s |

The 8B also repeats near-identical values across frames (contrast 0.7,
sharpness 0.4); the 27B reads each frame. Results: `develop-*.json`.

### Captions and burst picks, 8B against 9B

The 9B wrote slightly richer captions (5/5 parsed against the 8B's 4/5) and
similar burst picks (agreed on 4 of 5), at roughly 3 to 4 times the 8B's
caption time. It was not offered in the switch. Results:
`captions-bursts-develop-8b-vs-9b.json`, `9b-fresh-frames.json`.

### Tagging, 8B against 27B (8 frames, full-size preview)

- **Description:** the 27B is clearly better: position in frame, plumage, the
  horizon, the light, secondary birds. The 8B writes one generic sentence.
- **Subjects:** the 27B gives `seabird`, `ocean`, sometimes `albatross`;
  the 8B often only `bird`.
- **Species:** no clear winner. Both say wandering albatross on most frames.
  The 8B often names two species for one bird; the 27B named dark birds
  "cape petrel", which looks wrong. Nobody has checked species against ground
  truth.
- **Technical notes:** boilerplate from both ("motion blur, shallow depth of
  field" from the 8B on every frame; "sharp focus, balanced exposure" from the
  27B).
- **Time:** 8B about 13 s, 27B about 31 s in this run, which alternated
  models per frame. A later run without alternation measured the 8B at about
  7 s and the 27B at about 18 s for the same task. **Open question:** why
  alternating models was so much slower (memory pressure with both loaded,
  or the oMLX scheduler).

Results: `tags-8b-vs-27b.json`.

### Where the 27B's tagging time goes

From the `usage` block on single requests:

| Preview long edge | Prompt tokens | Prefill alone (`max_tokens` 1) |
|-------------------|---------------|--------------------------------|
| 1616 (library previews today) | 2,286 | 10 to 12 s |
| 768 | 970 | about 5.3 s |

The answer is 160 to 210 tokens of pretty-printed JSON (often inside a
```` ```json ```` fence), about 7 s at roughly 28 tokens per second. The
vocabulary goes first as a system message (`_vocabulary_system`) and oMLX
caches that prefix across a job, so it costs little.

### Shrinking the image and compacting the answer (4 fresh frames each)

"Compact" appends: "Write the JSON compact on one line, no code fence."

| Setting | 27B per frame | 8B per frame | 27B quality |
|---------|---------------|--------------|-------------|
| 1616, normal JSON | about 18 s | about 7 s | baseline |
| **1024, compact** | **about 12 s** | **about 3 s** | descriptions, subjects and species as at full size |
| 768, compact | about 11.5 s | about 2.5 s | starts naming a second species, like the 8B |

Size and compact were changed together, so their separate effects are not
known. Results: `tag-speed-by-size.json`.

### Requests in parallel (27B, 1024-pixel images, compact JSON)

One at a time: 11.8 s per frame. Four at once: 9.1 to 9.5 s per frame. About
20% more throughput; prefill seems to be compute-bound on this GPU.

## Traps found while measuring

- **oMLX answers repeated requests from cache.** A rerun of identical prompts
  and images came back in 0.8 s with byte-identical text, which looked like a
  5x speedup. Always use image bytes the server has not seen: new frames, or
  resize by a pixel (`parallel.py` uses edges 1000, 1001, 1002).
- **Thinking is already off.** `_complete` sends
  `chat_template_kwargs: {"enable_thinking": false}`; turning it off in oMLX
  changed nothing.
- **Both models loaded together** may slow each other; record what was loaded
  with every number.
- The library was cleared on the day of these tests. The frames came from
  `<library>/trash/<snapshot>/` (its `catalog.sqlite3` plus
  `files/previews/`), with the raws still on the external disk. Housekeeping
  may prune that snapshot; any import works as a source instead.

## Suggested next steps

In order of expected payoff for the effort:

1. **Done: smaller images** (see above). Resize to a 1024 long edge in
   `OmlxTagger._image_content` (Pillow, already a dependency) for tagging,
   captions, composition, the reject scan and triage. Keep burst critique at
   full size until measured: it compares sharpness between frames. Develop
   advice already renders at `DEVELOP_ADVICE_LONG_EDGE` (1024). Expected: 27B
   tagging about 18 s to about 12 s, 8B about 7 s to about 3 s. Test: the
   encoded image's size, with no oMLX contact.
2. **Done: measured separately; compact JSON asked for in `tag`.** Captions,
   composition and critique answers are short enough that it was not applied
   there; apply it if a measurement says otherwise.
3. **Done: fast-role switch.** `/api/review-model` became `POST
   /api/role-model {role, model}` (`web.role_fields` maps the role to the
   Settings field it rebinds; the two-model choice is captured at startup so
   a switch cannot shrink it). The header has a **Fast model** select next to
   **Review model**. Fast still starts on the 8B.
4. **Try the MoE model.** `Qwen3.6-35B-A3B` is a mixture of experts with
   about 3B active parameters per token, so it could decode near the 8B's
   speed with better quality. The 8-bit download (about 35 GB) is incomplete
   and too large to share 48 GB with the others; look for a 4-bit build (about
   18 GB). This is the most promising unknown.
5. **Split the work across models.** Tag everything with the 8B; ask the 27B
   only for keepers' descriptions, or on demand per frame. The 27B still has
   to read each image, so this saves time only on frames it skips.
6. **Measured, not adopted: structured output.** oMLX 0.7.2 accepts
   `response_format: {type: json_schema, json_schema: {name, schema}}` and
   enforces it (a required key the prompt never mentioned was emitted). On the
   tag prompt (`schema.py`, 3 fresh frames per cell, both models loaded) the
   27B took 13.0 to 13.7 s against 9.1 to 10.5 s plain, with longer answers
   (the constrained decoder ignores the compact-JSON request and lists more
   subjects); the 8B was unchanged at about 3 s. No parse failures in either
   cell. So the schema costs the 27B about a third for nothing it needed;
   reconsider only if parse failures become a problem, and then for the 8B's
   captions, where it is free. Results: `results/schema.json`.
7. **Species from the 8B need a gate.** Twice in these runs the 8B listed
   three to six species at high confidence on frames with no animal (an empty
   hillside, a dusk landscape, a person in a vehicle); the 27B never did. A
   cheap rule to test: drop the 8B's species when `subjects` names no animal,
   or only keep species the 27B confirms on keepers (step 5).
8. **Build a small scored set before tuning quality further.** About 30 frames
   with the photographer's own descriptions, subjects and species, so changes
   are judged on a score rather than by eye. Species especially needs ground
   truth.
9. **Parallel requests: low priority.** About 20% more throughput, but the
   job runner would have to tag frames concurrently.

Not worth retrying: `Qwen3.5-9B-4bit` (slower than the 8B, fails the develop
shape), and the thinking toggle.

## Scripts

Run from the repo root with `uv run python docs/model-speed/<script>`, and set
`LUMEN_BENCH_SNAPSHOT` to a folder holding `catalog.sqlite3` and
`files/previews/`, such as a `<library>/trash/<snapshot>/`. They read through
Lumen's own `OmlxTagger`, write nothing to the library, and save into
`results/`.

| Script | Measures |
|--------|----------|
| `develop.py <label> <model>...` | develop advice on six frames (needs the raws) |
| `tags.py` | tagging, 8B against 27B |
| `speed2.py <model>...` | tagging at 1616, 1024 and 768 pixels, with and without compact JSON |
| `parallel.py` | 27B tagging one at a time against four at once |
| `schema.py <model>...` | tagging plain against a JSON-schema `response_format`, 3 fresh frames per cell from `LUMEN_BENCH_OFFSET` (default 300) |
| `live_check.py <model>...` | tagging through the unmodified tagger, 3 fresh frames per model from `LUMEN_BENCH_OFFSET` (default 100), for confirming an in-tree change |

`speed2.py` and `parallel.py` override `_image_content` and `_complete` in a
subclass to change the image size and prompt without touching `src/`.
