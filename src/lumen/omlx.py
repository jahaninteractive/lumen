from __future__ import annotations

import base64
import json
import math
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class OmlxError(RuntimeError):
    pass


class TagResponseError(OmlxError):
    def __init__(self, message: str, raw_response: str | None = None):
        super().__init__(message)
        self.raw_response = raw_response


class CompositionResponseError(OmlxError):
    def __init__(self, message: str, raw_response: str | None = None):
        super().__init__(message)
        self.raw_response = raw_response


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True, slots=True)
class TagResult:
    description: str
    subjects: list[str]
    scene: str
    technical: list[str]
    confidence: float
    raw_response: str


@dataclass(frozen=True, slots=True)
class CompositionReview:
    candidate_id: str
    rationale: str
    confidence: float


def parse_tag_response(raw: str) -> TagResult:
    text = raw.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        final_fence = text.rfind("```")
        if first_newline == -1 or final_fence <= first_newline:
            raise TagResponseError("oMLX returned an incomplete JSON fence", raw)
        text = text[first_newline + 1 : final_fence].strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise TagResponseError(f"oMLX did not return valid JSON: {error.msg}", raw) from error
    if not isinstance(payload, dict):
        raise TagResponseError("oMLX tag response must be a JSON object", raw)
    description = payload.get("description")
    subjects = payload.get("subjects")
    scene = payload.get("scene", "")
    technical = payload.get("technical", [])
    confidence = payload.get("confidence")
    if not isinstance(description, str) or not description.strip():
        raise TagResponseError("oMLX tag response is missing a description", raw)
    if not isinstance(subjects, list) or not all(isinstance(value, str) for value in subjects):
        raise TagResponseError("oMLX subjects must be a list of strings", raw)
    if not isinstance(scene, str):
        raise TagResponseError("oMLX scene must be a string", raw)
    if not isinstance(technical, list) or not all(isinstance(value, str) for value in technical):
        raise TagResponseError("oMLX technical observations must be strings", raw)
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
        raise TagResponseError("oMLX confidence must be between 0 and 1", raw)
    return TagResult(
        description=description.strip(),
        subjects=[value.strip() for value in subjects if value.strip()],
        scene=scene.strip(),
        technical=[value.strip() for value in technical if value.strip()],
        confidence=float(confidence),
        raw_response=raw,
    )


def parse_composition_response(raw: str, candidate_ids: set[str]) -> CompositionReview:
    text = raw.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        final_fence = text.rfind("```")
        if first_newline == -1 or final_fence <= first_newline or not text.endswith("```"):
            raise CompositionResponseError("oMLX returned an incomplete JSON fence", raw)
        text = text[first_newline + 1 : final_fence].strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise CompositionResponseError(f"oMLX did not return valid JSON: {error.msg}", raw) from error
    required = {"candidate_id", "rationale", "confidence"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise CompositionResponseError("oMLX composition response must contain only the required fields", raw)
    candidate_id = payload["candidate_id"]
    rationale = payload["rationale"]
    confidence = payload["confidence"]
    if not isinstance(candidate_id, str) or candidate_id not in candidate_ids:
        raise CompositionResponseError("oMLX returned an unavailable crop candidate", raw)
    if not isinstance(rationale, str) or not (rationale := rationale.strip()) or len(rationale) > 500:
        raise CompositionResponseError("oMLX rationale must contain 1 to 500 characters", raw)
    if (not isinstance(confidence, (int, float)) or isinstance(confidence, bool)
            or not math.isfinite(confidence) or not 0 <= confidence <= 1):
        raise CompositionResponseError("oMLX confidence must be a finite number between 0 and 1", raw)
    return CompositionReview(candidate_id, rationale, float(confidence))


class OmlxTagger:
    def __init__(self, base_url: str, model: str, api_key: str | None = None):
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("oMLX endpoint must be a loopback http(s) URL")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key or self._local_api_key()

    def tag(self, preview: Path, timeout: float = 240) -> TagResult:
        prompt = (
            "Analyze this photograph for a local photo catalog. Return JSON only with: "
            'description (string), subjects (string array), scene (string), '
            "technical (string array of visible focus/exposure/motion observations), "
            "and confidence (number 0 to 1). Do not identify a person."
        )
        content = self._complete([
            {"type": "text", "text": prompt},
            self._image_content(preview),
        ], timeout)
        return parse_tag_response(content)

    def review_composition(
        self,
        original: Path,
        candidates: tuple[tuple[str, Path], ...] | list[tuple[str, Path]],
        timeout: float = 240,
    ) -> CompositionReview:
        choices = tuple(candidates)
        if not choices:
            raise ValueError("composition review requires at least one candidate")
        ids = [item[0] for item in choices if isinstance(item, tuple) and len(item) == 2]
        if (len(ids) != len(choices) or any(not isinstance(candidate_id, str) or not candidate_id.strip()
                                           for candidate_id in ids) or len(set(ids)) != len(ids)):
            raise ValueError("composition candidate IDs must be unique, non-empty strings")
        original_ids = [candidate_id for candidate_id, path in choices if path == original]
        original_label = "Original reference photograph"
        if original_ids:
            original_label += "; labeled candidate " + ", ".join(original_ids)
        content = [{
            "type": "text",
            "text": (
                "Critique the original photograph and compare the labeled crop candidates for visual balance, "
                "subject emphasis, context, distracting edges, headroom, and lead room. Select exactly one of "
                f"these candidate IDs: {', '.join(ids)}. Return JSON only with candidate_id, rationale (at most "
                "500 characters), and confidence (0 to 1). Do not return coordinates."
            ),
        }, {"type": "text", "text": original_label}, self._image_content(original)]
        for candidate_id, path in choices:
            if path == original:
                continue
            content.extend((
                {"type": "text", "text": f"Candidate {candidate_id}"},
                self._image_content(path),
            ))
        response = self._complete(content, timeout)
        return parse_composition_response(response, set(ids))

    @staticmethod
    def _image_content(path: Path) -> dict:
        if not isinstance(path, Path) or not path.is_file():
            raise ValueError(f"composition preview is unavailable: {path}")
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        image = base64.b64encode(path.read_bytes()).decode("ascii")
        return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image}"}}

    def _complete(self, content: list[dict], timeout: float) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0,
            "max_tokens": 500,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers=headers,
        )
        try:
            opener = urllib.request.build_opener(NoRedirectHandler())
            with opener.open(request, timeout=timeout) as response:
                result = json.load(response)
            content = result["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as error:
            error.close()
            raise OmlxError(f"Local oMLX request failed: {error}") from error
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError, json.JSONDecodeError) as error:
            raise OmlxError(f"Local oMLX request failed: {error}") from error
        if not isinstance(content, str):
            raise OmlxError("Local oMLX response did not contain text")
        return content

    @staticmethod
    def _local_api_key() -> str | None:
        if value := os.environ.get("LUMEN_OMLX_API_KEY"):
            return value
        settings_path = Path.home() / ".omlx" / "settings.json"
        try:
            settings = json.loads(settings_path.read_text())
            value = settings.get("auth", {}).get("api_key")
            return value if isinstance(value, str) and value else None
        except (OSError, json.JSONDecodeError, TypeError):
            return None


def get_server_status(base_url: str, api_key: str | None = None, timeout: float = 2) -> dict:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("oMLX endpoint must be a loopback http(s) URL")
    headers = {}
    key = api_key or OmlxTagger._local_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(f"{base_url.rstrip('/')}/api/status", headers=headers)
    try:
        opener = urllib.request.build_opener(NoRedirectHandler())
        with opener.open(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        error.close()
        raise OmlxError(f"Local oMLX status request failed: {error}") from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise OmlxError(f"Local oMLX status request failed: {error}") from error
    if not isinstance(payload, dict):
        raise OmlxError("Local oMLX status response must be an object")
    loaded = payload.get("loaded_models", [])
    if not isinstance(loaded, list) or not all(isinstance(item, str) for item in loaded):
        raise OmlxError("Local oMLX status returned invalid loaded models")
    default_model = payload.get("default_model")
    return {
        "version": payload.get("version") if isinstance(payload.get("version"), str) else None,
        "default_model": default_model if isinstance(default_model, str) else None,
        "loaded_models": loaded,
    }

