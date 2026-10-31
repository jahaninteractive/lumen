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

## Conclusion (2026-10-31, agreed with the photographer)

**Keep the current pair: `Qwen3-VL-8B-Instruct-4bit` fast, `Qwen3.8-27B-4bit`
review.** The question above has its answer: the 27B's accuracy at near-8B cost
cannot be had on this machine and oMLX 0.7.2, and the reason is now measured.

- The 27B's time is prefill. About 4.8 ms per token, and oMLX cannot reuse the
  cached keyword prefix on a hybrid-attention model (snapshots only per 2048
  token chunk and at request end). The one real win was dropping the 100
  self-generated `learned` words from the prompt: a tag went from about 12 s
  to about 9.5 s with the same answers (`68007e3`). Dropping the whole list
  saves 2 s more and loses species names, so it stays.
- Nothing server-side helps: the MTP drafter and ANE prefill were neutral,
  int8 activations need an M5, structured output costs a third.
- Nothing smaller replaces the 8B. The 3B breaks every JSON contract; the 4B
  keeps them but wrongly suggests reject on 3 of 10 frames, invents astro
  issues and fails develop validation. Both gain under 2x because image prefill
  dominates. The 8B already restores the keyword prefix from cache.
- No more downloads for this question on the satellite link. The other-family
  candidates are listed below with sizes; the only one with plausible upside
  over the 27B is `gemma-4-26b-a4b-it-4bit` (15.3 GB), for an idle night if the
  photographer ever wants it.

What would reopen this: an oMLX release that snapshots hybrid-model caches per
block (restores the prefix cache on the 27B, worth 2 to 3 s a request), or a
move to an M5.

Next effort goes to step 7, the species confidence gate: a prompt and parser
change, no download, and both models need it (the 8B listed six species on an
empty hill, the 27B a kite on three frames without one). It is in
`docs/BACKLOG.md`.

---

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
vocabulary goes first as a system message (`_vocabulary_system`). oMLX
caches that prefix for the 8B but **not for the 27B** (measured 2026-10-31,
see "27B knobs" below): on the 27B it costs about 2.6 s on every request.

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

## Qwen2.5-VL-3B against Qwen3-VL-8B (2026-10-31)

Question: could the 3B (2.9 GB on disk) serve an 8 GB Mac? Measured with
`compare.py` on snapshot `20261028T092715Z-3inu821z` (safari frames: zebra,
wildebeest, lions, elephant, a vehicle, a flower), 10 fresh frames per task,
both models loaded, all of one model's calls before the other's. Results:
`results/compare-Qwen2.5-VL-3B-vs-Qwen3-VL-8B.json`.

| Task (frames) | 3B median s | 3B parsed | 8B median s | 8B parsed |
|---------------|-------------|-----------|-------------|-----------|
| tag (10) | 2.2 | 6/10 | 2.7 | 10/10 |
| reject scan (10) | 0.9 | 6/10 | 1.4 | 10/10 |
| astro triage (4) | 0.4 | 3/4 | 0.7 | 4/4 |
| caption (6) | 0.5 | 6/6 | 1.0 | 6/6 |
| burst critique (3) | 9.3 | 1/3 | 12.5 | 3/3 |
| develop advice (4) | 2.9 | 0/4 | 3.5 | 4/4 |
| composition (4) | 2.3 | 4/4 | 3.4 | 4/4 |

Verdict: **not usable as a drop-in.** The speed gain is only 1.2 to 2x
because image prefill dominates and both models see the same 1024-pixel
image. The 3B does not follow the JSON contracts: it wraps objects in a
fenced array (`[{...}]`), drops the top-level `confidence` key, pretty-prints
despite the compact request, and on develop emits every control with
`temperature_k: 2000` plus a null or malformed curve (one answer looped on
`"0.0"` to the token limit). Where it parses, the answers are worse: burst
critique picked the first frame every time and described it instead of
comparing; the reject scan flagged `misfire` 0.99 on every frame with
`suggest_reject` false; astro called a spotlit lioness `night_sky`; tagging
listed lion, elephant and giraffe at 0.6 to 0.8 on a frame with no animal,
and copied vocabulary into `technical` ("calm water" on a savanna). Captions
were the one competent task, a sentence shorter than the 8B's and with alt
text identical to the caption.

Memory: oMLX RSS rose 2.5 GB after the 3B's block and a further 3 GB after
the 8B's, with both resident. Neither is a measurement of the model alone on
an 8 GB machine; that needs a run with one model loaded.

Also seen in the 8B: `exposure_ev: -0.5` with a rationale that calls it a
brighten on a median-9 frame (the sign error the prompt was rewritten to
stop; 1 of 4 here), species at 0.02 to 0.1 on the no-animal frame, and two
composition rationales saying the crop "is not provided" or "is identical to
the original", which suggests the two-image prompt is not landing. Vision
found no regions on any of the 4 composition frames, so each had only the
original plus the centered fallback.

If an 8 GB target matters: the lever is parser tolerance (accept a
single-element array, default a missing `confidence`), which would recover
most of the 3B's tag, reject and astro failures but none of its quality; or a
3B-class model from the Qwen3-VL line, which has not been tried.

## Qwen3-VL-4B against Qwen3-VL-8B (2026-10-31)

Same question for the Qwen3-VL line's own small model
(`lmstudio-community/Qwen3-VL-4B-Instruct-MLX-4bit`, 2.9 GB on disk).
Same snapshot and script, `LUMEN_BENCH_OFFSET=41` (10 fresh frames: zebra
and wildebeest, two rhino pairs, a vehicle at dusk, a fogged frame, three
spotlit lions at night, an empty hill, an elephant by a building). Results:
`results/compare-Qwen3-VL-4B-vs-Qwen3-VL-8B.json`.

| Task (frames) | 4B median s | 4B parsed | 8B median s | 8B parsed |
|---------------|-------------|-----------|-------------|-----------|
| tag (10) | 1.4 | 10/10 | 2.5 | 10/10 |
| reject scan (10) | 0.8 | 10/10 | 1.4 | 10/10 |
| astro triage (4) | 0.5 | 4/4 | 0.7 | 4/4 |
| caption (6) | 0.7 | 6/6 | 1.0 | 6/6 |
| burst critique (3) | 8.0 | 3/3 | 3.1 | 3/3 |
| develop advice (4) | 2.6 | 1/4 | 3.3 | 4/4 |
| composition (4) | 2.4 | 4/4 | 3.3 | 4/4 |

Verdict: **better than the 3B, still not a fast-role replacement.** It
follows the JSON contracts the 3B broke, and tags and captions are as good as
the 8B's (it answers Latin binomials, `Equus quagga`, `Panthera leo`, where
the vocabulary has common names, so fewer vocabulary hits). The gain is 1.4 to
1.8x on the single-image tasks because image prefill dominates, as before.
Where it loses:

- Reject scan, its main job in the fast role: `suggest_reject: true` on 3 of
  10, all wrong. Two spotlit night lions called `severely_under_exposed` at
  0.9 to 0.95, and a sharp landscape `out_of_focus` 0.7. The 8B rejected
  none of the 10 and flagged the same motion blur and fogged frame as
  problems without a reject.
- Astro triage invents issues: `trailing` on a walking lioness, `haze` and
  `light_pollution` on a lion cub; the 8B listed none.
- Develop advice: 3 of 4 answers write the curve in 0 to 100 (`[0, 20]`,
  `[1, 75]`) and fail validation; every answer is the same `+0.5 EV,
  contrast 0.3, clarity 0.6` whatever the histogram.
- Burst critique is 2.5x slower than the 8B (8.0 against 3.1 s) on the three
  full-size frames, and its rationale describes the chosen frame instead of
  comparing. Picks agree on 2 of 3 (it took the frame the 8B called ghosted).
- Composition picks the original 4 of 4; the 8B took the crop on the two
  frames where it removed foreground clutter.

Memory: oMLX RSS rose 4.1 GB after the 4B's block and 0.7 GB more after the
8B's (already warm). The 4B at rest is about 4 GB, which still does not fit
an 8 GB Mac beside the system and the browser.

Also seen in the 8B: `confidence: 0.0` on every clean reject answer (7 of 10;
the parser accepts it, the UI shows nothing for it), and on the empty-hill
frame it put six vocabulary species into `subjects` at 0.1 to 0.6 with no
animal present: the same gate material as the 27B's "Kite" (step 7).

Removed afterwards: the 4.4 MB stub `~/Models/mlx-community/Qwen3-VL-4B-Instruct-4bit`
left by a cancelled download, which oMLX listed as a model with no weights.

---
## 27B knobs in oMLX, and why the vocabulary prefix is not cached (2026-10-31)

The photographer's goal is the 27B's quality closer to the 8B's time, so the
server-side levers oMLX 0.7.2 offers for `Qwen3.8-27B-4bit` were measured
with the 27B loaded alone (8B unloaded), tagging 3 fresh safari frames per
cell through `live_check.py` (offsets 400, 403, 406). Results:
`results/27b-knobs.json`.

| Cell | Per frame | Note |
|------|-----------|------|
| as shipped | 12.3, 11.2, 10.9 s | prompt 1355 tokens, 98 to 142 generated |
| MTP speculative decode | 11.7, 9.7, 10.6 s | drafter `mlx-community/Qwen3.8-27B-MTP-4bit` (265 MB, now in `~/Models/mlx-community/`); accepted 75 to 79% of drafted tokens, 2.5 tokens per verify round, but a round costs about as much as 2.5 plain steps |
| ANE prefill (`qwen35_ane_prefill_enabled`) | 16.8 (warm-up), 11.5, 11.9 s | neutral once warm |
| int8 activations (`qwen35_oq_a8_enabled`) | refused | oMLX: needs M5-series Metal INT8 |

Both knobs are back off; the drafter name stays in the 27B's oMLX settings
with `vlm_mtp_enabled` false. Where the time goes (`max_tokens` 1, 27B):

- A 690-token image prompt prefills in 4.0 to 5.1 s; text prefills at about
  4.8 ms per token, so the vision encoder itself is under 1 s. Prefill is
  compute-bound on the language model, which is why neither knob helped.
- **The vocabulary system message (581 tokens) is prefilled on every 27B
  request**: alone it costs 3.0 s the first time and 2.9 s the second;
  system plus a fresh image costs 6.4 to 6.6 s, the sum of the two. On the
  8B the same message costs 0.64 s once and 0.17 s after (log: `Prefix cache
  restore ... cached=512`), and system plus image costs the same as image
  alone (1.2 s).
- Cause, from oMLX's `scheduler.py`: Qwen3.5-family models have recurrent
  (GDN) layers whose state cannot be sliced, so the prefix cache can only
  restore at a "boundary snapshot". Snapshots are taken per prefill chunk
  (`prefill_step_size`, 2048, not exposed in settings) and at the end of
  the request, so a 1355-token prompt gets one snapshot at its own end
  (log: `storing 1348/1497 tokens ... 0 intermediate snapshots`) and the
  next frame, which differs after token 581, matches nothing. Qwen3-VL (the
  8B) is plain attention and restores at 256-token block boundaries.

What this means for the 27B: about 2.6 of its 11 s per tag is the keyword
list, and the `learned` section (100 words) is 53% of its characters, about
1.5 s. Shortening that list, or capping how many learned words are sent to
the review model, is the one lever left that is bigger than noise without
changing the image size.

Measured the same afternoon (27B, 3 fresh frames per cell, offsets 436 and
439): the curated categories alone (`wildlife`, `scene`, `conditions`,
`activity`, `places`; 1086 characters, prompt 1081 tokens) tagged in 9.3,
9.5 and 10.5 s; the full list (2121 characters, 1355 tokens) in 12.4, 12.2
and 11.8 s. Subjects, species and descriptions were as good without the
learned words (kudu 0.98, African elephant 0.95 to 0.98; one frame listed
"elephant" twice). The learned list in the photographer's library is 100 of
the model's own earlier words with duplicates and junk ("arm", "sleeve",
"bracelet", "phone", "table", "glass", "person (not identified)"), and
`learn_keywords` has no cap, so it will keep growing.

**Done the same day, in the tree:** `_vocabulary_system` leaves `learned`
out of the prompt (it stays in the file for matching and the UI), and
`learn_keywords` keeps the newest `LEARNED_CAP` (30) learned words without
casefolded duplicates. Tests in `test_omlx` and `test_vocabulary`; suite 723
warning-strict OK.

### No keyword list at all (step 3, measured, declined)

27B, alternating frames between the curated list (the code above) and no
system message (species still requested), 5 frames per cell per set.
Results: `results/vocab-none-vs-curated.json`.

| Set | Cell | Median s | Words matching the curated vocabulary |
|-----|------|----------|----------------------------------------|
| safari | curated | 10.7 | 11 of 27 |
| safari | none | 8.2 | 2 of 35 |
| seabird | curated | 9.5 | 15 of 22 |
| seabird | none | 7.7 | 5 of 26 |

Another 2 s per frame, but the list is doing its job: with it the seabird
frames come back as wandering albatross, black-browed albatross and southern
giant petrel; without it the same birds are "gull", "bird", "Shearwater" and
"Petrel", scenes become "outdoor" instead of "open ocean", and subjects fill
with free text ("coffee table", "cushion", "backpack") that matches nothing.
Keep the curated list. Residual oddities seen with it: "Kite" at 0.1 to 0.6
on three frames (two seabirds, one dung beetle), and a species list on a
distant herd at 0.6 and 0.4; both are gate material for step 7.

## Smaller and other-family candidates (2026-10-31; only the 4B measured)

The connection here is a slow satellite link (about 400 KB/s measured; a
3 GB model is about 2 hours). Ask before queueing anything, one at a time,
through oMLX's own downloader (`POST /admin/api/hf/download`), which resumes.
Sizes are the `.safetensors` totals on `mlx-community`; all have a vision
tower in `config.json`.

| Model | Size | Why it is interesting |
|-------|------|----------------------|
| `Qwen3-VL-4B-Instruct-4bit` | 3.1 GB | measured above as the lmstudio-community copy: contracts hold, reject scan over-rejects, not a replacement |
| `gemma-4-e4b-it-4bit` | 5.1 GB | 8B-class alternative from another family |
| `GLM-4.6V-Flash-4bit` | 7.1 GB | small vision model built for structured output |
| `gemma-3-12b-it-qat-4bit` | 8.0 GB | between the 8B and the 27B |
| `gemma-4-26b-a4b-it-4bit` | 15.3 GB | MoE, about 4B active: 27B-class quality at near-8B decode, same disk as the 27B |
| `Qwen3-VL-30B-A3B-Instruct-4bit` | 18.3 GB | MoE from the line that already follows the contracts; plain attention, so the prefix cache would work |
| `Qwen3.6-35B-A3B-4bit` | 20.4 GB | step 4's MoE; a Qwen3.5-family hybrid, so the prefix-cache limit above applies |
| `gemma-3-27b-it-qat-4bit` | 16.8 GB | same class as the 27B, no size win |

Not candidates: `Mistral-Small-3.2-24B` (same class, no vision win here),
`Qwen3.8-Flash-Next-4bit` (111 GB). The MoE prefill is still proportional to
the active path per token, so expect the gain on decode, not on the 7 s of
image-and-vocabulary prefill.

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
4. **Declined (see Conclusion): the MoE model.** `Qwen3.6-35B-A3B` is a mixture of experts with
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
shape), the thinking toggle, and on the 27B the MTP drafter and ANE prefill
(both neutral, see "27B knobs").

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
| `compare.py <model>...` | every task (tag, reject scan, astro triage, caption, burst, develop, composition) on the same fresh frames per model, all of one model before the next; skips frames with any model event in the snapshot catalog; records oMLX RSS and model disk size under `meta` |
| `live_check.py <model>...` | tagging through the unmodified tagger, 3 fresh frames per model from `LUMEN_BENCH_OFFSET` (default 100), for confirming an in-tree change or an oMLX setting (it overwrites `results/live-check-1024.json`; copy it aside per cell) |

`speed2.py` and `parallel.py` override `_image_content` and `_complete` in a
subclass to change the image size and prompt without touching `src/`.
