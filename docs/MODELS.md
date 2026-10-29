# Local vision model validation

## Recommendation

Lumen uses two oMLX model roles. The defaults, and what the header dropdowns
can switch each role to while `lumen serve` runs:

| Role | Default | Env var | Jobs |
|------|---------|---------|------|
| fast | `mlx-community/Qwen3-VL-8B-Instruct-4bit` | `LUMEN_OMLX_FAST_MODEL` | tags, problem scan (suggested rejects), astro triage |
| review | `mlx-community/Qwen3.8-27B-4bit` when oMLX has it loaded, else the fast model | `LUMEN_OMLX_REVIEW_MODEL` (pins it), else `LUMEN_OMLX_MODEL` | burst critique, captions and alt text, day digests, composition advice, develop suggestions |

The fast model handles the per-photo volume work; the review model handles the
few calls where reading the frame closely matters more than throughput.

Memory decides which to load. The 8B takes about 6 GB loaded and alone covers
every job, which is why a 16 GB Apple Silicon Mac is the minimum (not yet
tested; the measurements here are from a 48 GB M4). The 27B takes about 16 GB
loaded, so both together are about 21 GB; on a machine with that headroom it
earns its place with fuller descriptions and bolder, frame-specific develop
advice, and it stays useful outside Lumen for anything else you run through
oMLX, such as its chat. On the 8B alone you give up those two things and
nothing else. The **Review model** and **Fast model** dropdowns move either
role between the two for the running server only; the fast role always starts
on the 8B. How to use the switch is in [GUIDE.md](GUIDE.md#choosing-the-models).

Models tried and not offered:

| Model | Disk | Why not |
|-------|------|---------|
| `Qwen3.5-9B-4bit` | 5.6 GB | captions slightly richer than the 8B and burst picks similar, but 3 to 4 times slower, and it never returned develop settings in the required JSON shape (0 of 6) |
| `Qwen3.6-35B-A3B-8bit` | download never finished | untested; a mixture-of-experts model is the one remaining idea for 27B quality at 8B cost |

Do not download another large model for Lumen until the 35B-A3B has been
tried.

## What was validated on this Mac

### Qwen3.8-27B-4bit (review role), 21 October 2026

- Installed oMLX: the CLI reported 0.7.x.
- oMLX discovered `Qwen3.8-27B-4bit` as `type: vlm`, selected the VLM engine,
  loaded its vision tower, and measured about 15.7 GB for the model.
- The checkpoint config contains `vision_config`, vision token IDs, and a
  Qwen3.5-family vision architecture.
- An actual OpenAI-compatible request sent a local PNG as a base64
  `image_url`. The model correctly returned: “A red baseball cap rests on top
  of a large black letter R.”
- Time to first token was about 4.5 seconds in that one-image smoke test.

### Qwen3-VL-8B-Instruct-4bit (fast role), 23 October 2026

The earlier interrupted download was completed and the model now loads in oMLX
next to the 27B model. On a real Sony library it answered tag, problem-scan,
and triage requests; the only failures were Lumen's own parsers being stricter
than the model (an extra `activity` key in a tag reply), which `omlx.py` now
tolerates. Burst critique, captions, composition advice, and digests from the
27B model were exercised the same day (one rationale over 500 characters is now
clipped rather than rejected; a numeric `frame_id` is accepted).

Both roles are checked live: `Library.tagger_for(role)` reads oMLX's
`/api/status` and refuses an unloaded model, `lumen doctor --json` and
`/api/status` report `omlx_roles {fast, review: {model, loaded}}`, and the UI
disables each AI button with a reason naming the missing model.

### Speed and accuracy on real frames, 21 to 28 October 2026

Small samples from one day of seabird ARWs and a few safari frames, so the
numbers are directional. Timings are per frame with both models loaded and no
alternation between them; raw outputs and the scripts are in
`model-speed/results/` and `model-speed/`.

| Task | 8B | 27B | `Qwen3.5-9B-4bit` |
|------|----|-----|-------------------|
| Captions | about 1-4 s; 4 of 5 parsed | not timed | about 4 s; 5 of 5, slightly richer |
| Burst pick | about 6 s; agreed with the 9B on 4 of 5 | not timed | about 7 s |
| Where to start | about 2 s; 6 of 6 parsed, 5 of 6 brightened by a cautious +0.3 EV | about 13 s; 6 of 6 brightened by +0.5 to +1.0 EV | 0 of 6 in the required JSON shape |

Tagging, before and after the 1024-pixel resize and the compact one-line JSON
prompt (both changed together, so their separate effects are not known):

| Fast model | Before (1616 px, fenced JSON) | Now | What you get |
|------------|-------------------------------|-----|--------------|
| 8B | about 7 s | about 3 s | one plain sentence; sometimes names species that are not in the frame |
| 27B | about 18 s | 9 to 11 s | where the subject sits, the light, the background, secondary animals |

Where the 27B's tagging time went, from the `usage` block on single requests:
a 1616-pixel preview is 2,286 prompt tokens and 10 to 12 s of prefill alone,
a 768-pixel one 970 tokens and about 5.3 s; the answer was 160 to 210 tokens
of pretty-printed JSON, about 7 s at roughly 28 tokens per second.

- **Tagging.** The 27B is clearly better at description (where the subject
  sits, plumage, horizon, light, secondary birds) and subjects (`seabird`,
  `ocean`, sometimes `albatross`, where the 8B says `bird`). Species: no
  winner, and nothing checked against ground truth; the 8B often names two
  species for one bird, and listed three on a frame with no animal. Both
  write boilerplate technical notes. Every image is now sent at a 1024-pixel
  long edge and the tag prompt asks for compact one-line JSON: 27B about 18 s
  to 9 to 11 s, 8B about 7 s to about 3 s, descriptions unchanged. At 768 the
  27B started naming second species like the 8B, so 1024 stays. A JSON-schema
  `response_format` made the 27B about a third slower and fixed nothing.
  Four requests in parallel gave about 20% more 27B throughput; prefill is
  compute-bound.
- **Develop advice ("Where to start").** With the original prompt the 8B
  proposed -0.5 EV on six underexposed frames while calling it a lift. The
  prompt now states each control's direction, that a histogram median near 50
  is balanced, and the JSON shape. After it: 8B 6 of 6 parsed, 5 of 6
  brightened (a timid +0.3 EV), about 2 s; 27B 6 of 6 brightened by +0.5 to
  +1.0 EV, about 13 s. The 8B also repeats near-identical values across
  frames; the 27B reads each one.
- **Captions and burst picks.** The 8B parsed 4 of 5 captions at 1 to 4 s;
  burst picks about 6 s. The 27B was not timed on these.
- **Thinking is already off** (`enable_thinking: false` in every request);
  disabling it in oMLX changed nothing.

### Prompt caching

Tagging sends the keyword vocabulary as a system message ahead of the per-photo
turn so the prefix can be cached. With oMLX's shipped settings
(`cache.hot_cache_only: true`, `hot_cache_max_size: "0"`) nothing is cached.
After setting `hot_cache_only: false` and `hot_cache_max_size: "4GB"` in
`~/.omlx/settings.json`, text-only requests to Qwen3-VL-8B reuse the prefix,
but requests that carry an image go through oMLX's separate vision-feature
cache with no prefix lookup, so `cached_tokens` stays 0 for tagging. The
system-message layout costs nothing and will benefit if oMLX adds prefix
caching for vision requests. Beware the opposite effect: oMLX answers a
repeated identical request (same prompt, same image bytes) from cache in under
a second with byte-identical text, so speed tests need frames the server has
not seen.

## Why images may have appeared unsupported


The checkpoint is not text-only. A client that sends a filesystem attachment,
plain text path, or unsupported message shape can still make a VLM appear blind.
Lumen uses the format accepted by the current oMLX API: a message content array
containing text plus a base64 data URI in `image_url`.

Relevant primary sources:

- oMLX's API schema defines base64 `image_url` parts:
  <https://github.com/jundot/omlx/blob/main/omlx/api/openai_models.py>
- oMLX's VLM engine documents automatic model selection and OpenAI vision input:
  <https://github.com/jundot/omlx/blob/main/omlx/engine/vlm.py>
- The model's published config includes its vision tower:
  <https://huggingface.co/mlx-community/Qwen3.8-27B-4bit/blob/main/config.json>
- MLX-VLM is the upstream Apple Silicon vision-language runtime:
  <https://github.com/Blaizzy/mlx-vlm>

## Repeat the check

1. Run `omlx start` and load both models.
2. Run `uv run lumen doctor --json` and confirm `vision_config` and `complete`
   are true and both entries in `omlx_roles` report `loaded: true`.
3. Import a JPEG, then run `uv run lumen tag --photo ID` (fast role) and
   `uv run lumen caption ID` (review role).
4. Confirm the JSON/XMP sidecar contains a description and subjects that are
   visibly correct for the preview, and the caption is a sensible draft.

No photograph is sent to a cloud service by this workflow.
