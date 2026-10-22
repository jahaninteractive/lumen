# Local vision model validation

## Recommendation

Use the existing `mlx-community/Qwen3.8-27B-4bit`. Do **not** download another
large model for Lumen at this point.

## What was validated on this Mac

Validation date: 21 October 2026.

- Installed oMLX: the CLI reported 0.7.x.
- oMLX discovered `Qwen3.8-27B-4bit` as `type: vlm`, selected the VLM engine,
  loaded its vision tower, and measured about 15.7 GB for the model.
- The checkpoint config contains `vision_config`, vision token IDs, and a
  Qwen3.5-family vision architecture.
- An actual OpenAI-compatible request sent a local PNG as a base64
  `image_url`. The model correctly returned: “A red baseball cap rests on top
  of a large black letter R.”
- Time to first token was about 4.5 seconds in that one-image smoke test.

This is stronger evidence than the model name alone: the exact local weights,
installed oMLX build, request format, and vision engine worked together.

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

## The incomplete Qwen3-VL download

`Qwen3-VL-8B-Instruct-4bit` is a valid MLX vision model, but the local oMLX log
reported four missing weight shards after an interrupted download. Its official
model page is <https://huggingface.co/mlx-community/Qwen3-VL-8B-Instruct-4bit>.

It may be worth finishing later if tagging throughput matters more than the
validated 27B model's quality. It is not a current recommendation because it has
not passed the same on-machine end-to-end test.

## Repeat the check

1. Run `omlx start`.
2. Run `uv run lumen doctor --json` and confirm `vision_config` and `complete`
   are true.
3. Import a JPEG, then run `uv run lumen tag --photo ID`.
4. Confirm the JSON/XMP sidecar contains a description and subjects that are
   visibly correct for the preview.

No photograph is sent to a cloud service by this workflow.

