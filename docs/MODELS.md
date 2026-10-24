# Local vision model validation

## Recommendation

Lumen uses two oMLX model roles. Keep both loaded:

| Role | Model | Default env var | Jobs |
|------|-------|-----------------|------|
| fast | `mlx-community/Qwen3-VL-8B-Instruct-4bit` | `LUMEN_OMLX_FAST_MODEL` | tags, problem scan (suggested rejects), astro triage |
| review | `mlx-community/Qwen3.8-27B-4bit` | `LUMEN_OMLX_REVIEW_MODEL`, else `LUMEN_OMLX_MODEL` | burst critique, captions and alt text, day digests, composition advice |

The fast model handles the per-photo volume work; the review model handles the
few calls where reasoning quality matters more than throughput. Do not download
another large model for Lumen at this point.

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

### Prompt caching

Tagging sends the keyword vocabulary as a system message ahead of the per-photo
turn so the prefix can be cached. With oMLX's shipped settings
(`cache.hot_cache_only: true`, `hot_cache_max_size: "0"`) nothing is cached.
After setting `hot_cache_only: false` and `hot_cache_max_size: "4GB"` in
`~/.omlx/settings.json`, text-only requests to Qwen3-VL-8B reuse the prefix,
but requests that carry an image go through oMLX's separate vision-feature
cache with no prefix lookup, so `cached_tokens` stays 0 for tagging. The
system-message layout costs nothing and will benefit if oMLX adds prefix
caching for vision requests.

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
