from __future__ import annotations

import base64
import io
import json
import math
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image

from .domain import DEVELOP_RANGES, DevelopSettings


ROLES = {"fast": "omlx_fast_model", "review": "review_model"}  # role -> Settings attribute naming its model
PROBLEMS = ("eyes_closed", "motion_blur", "out_of_focus", "subject_cut_off", "obstructed", "misfire",
            "severely_under_exposed", "severely_over_exposed")
SKY_CLASSES = ("night_sky", "aurora", "milky_way", "twilight", "cloud", "not_sky")
SKY_ISSUES = ("haze", "light_pollution", "trailing", "cloud_band")
CAPTION_MAX = 200
ALT_TEXT_MAX = 125
NARRATIVE_MAX = 1500
MODEL_IMAGE_LONG_EDGE = 1024  # measured: the 27B tags as well at 1024 as at 1616 in two-thirds of the time; 768 starts inventing species
DEVELOP_RATIONALE_MAX = 600  # two or three sentences: the explanation is the product, not the numbers


class OmlxError(RuntimeError):
    pass


class TagResponseError(OmlxError):
    def __init__(self, message: str, raw_response: str | None = None):
        super().__init__(message)
        self.raw_response = raw_response


class DevelopAdviceError(OmlxError):
    def __init__(self, message: str, raw_response: str | None = None):
        super().__init__(message)
        self.raw_response = raw_response


class CompositionResponseError(OmlxError):
    def __init__(self, message: str, raw_response: str | None = None):
        super().__init__(message)
        self.raw_response = raw_response


class RejectResponseError(OmlxError):
    def __init__(self, message: str, raw_response: str | None = None):
        super().__init__(message)
        self.raw_response = raw_response


class BurstResponseError(OmlxError):
    def __init__(self, message: str, raw_response: str | None = None):
        super().__init__(message)
        self.raw_response = raw_response


class AstroResponseError(OmlxError):
    def __init__(self, message: str, raw_response: str | None = None):
        super().__init__(message)
        self.raw_response = raw_response


class CaptionResponseError(OmlxError):
    def __init__(self, message: str, raw_response: str | None = None):
        super().__init__(message)
        self.raw_response = raw_response


class DigestResponseError(OmlxError):
    def __init__(self, message: str, raw_response: str | None = None):
        super().__init__(message)
        self.raw_response = raw_response


class ExpandResponseError(OmlxError):
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
    species: list[dict] | tuple = ()  # [{"name": str, "confidence": float}]
    conditions: list[str] | tuple = ()


@dataclass(frozen=True, slots=True)
class CompositionReview:
    candidate_id: str
    rationale: str
    confidence: float


@dataclass(frozen=True, slots=True)
class RejectReview:
    problems: list[dict]  # [{"kind": one of PROBLEMS, "confidence": float}]
    suggest_reject: bool
    confidence: float
    raw_response: str


@dataclass(frozen=True, slots=True)
class BurstCritique:
    frame_id: str
    rationale: str
    confidence: float
    raw_response: str


@dataclass(frozen=True, slots=True)
class AstroTriage:
    sky: str  # one of SKY_CLASSES
    confidence: float
    issues: list[str]  # unique SKY_ISSUES
    raw_response: str


@dataclass(frozen=True, slots=True)
class DigestNarrative:
    narrative: str
    raw_response: str


@dataclass(frozen=True, slots=True)
class DevelopAdvice:
    settings: dict  # a DevelopSettings-valid subset of the renderer's controls; empty means "needs nothing"
    rationale: str
    raw_response: str


@dataclass(frozen=True, slots=True)
class Caption:
    caption: str
    alt_text: str
    raw_response: str


def _json_object(raw: str, error_type) -> dict:
    """Strip an optional Markdown fence and parse one JSON object, raising error_type(message, raw)."""
    payload = _json_value(raw, error_type)
    if not isinstance(payload, dict):
        raise error_type("oMLX response must be a JSON object", raw)
    return payload


def _json_value(raw: str, error_type):
    """Strip an optional Markdown fence and parse any JSON value, raising error_type(message, raw)."""
    text = raw.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        final_fence = text.rfind("```")
        if first_newline == -1 or final_fence <= first_newline or not text.endswith("```"):
            raise error_type("oMLX returned an incomplete JSON fence", raw)
        text = text[first_newline + 1 : final_fence].strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise error_type(f"oMLX did not return valid JSON: {error.msg}", raw) from error
    return payload


def _clip_text(value, limit: int, error_type, raw: str, name: str = "rationale") -> str:
    """A non-empty text field, clipped at a word boundary with an ellipsis when the model runs past the limit."""
    if not isinstance(value, str) or not (text := value.strip()):
        raise error_type(f"oMLX {name} must contain 1 to {limit} characters", raw)
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    return (cut[: cut.rfind(" ")] if " " in cut else cut).rstrip() + "…"


def _confidence(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1


def parse_reject_response(raw: str) -> RejectReview:
    """A bare array is taken as the problems alone (no reject suggestion); unknown keys are ignored."""
    payload = _json_value(raw, RejectResponseError)
    if isinstance(payload, list):
        payload = {"problems": payload}
    if not isinstance(payload, dict) or "problems" not in payload:
        raise RejectResponseError("oMLX reject response must contain problems", raw)
    problems = payload["problems"]
    if (not isinstance(problems, list)
            or not all(isinstance(p, dict) and set(p) == {"kind", "confidence"} and p["kind"] in PROBLEMS
                       and _confidence(p["confidence"]) for p in problems)
            or len({p["kind"] for p in problems}) != len(problems)):
        raise RejectResponseError("oMLX problems must be unique known kinds with confidence", raw)
    suggest_reject = payload.get("suggest_reject", False)
    confidence = payload.get("confidence", max((p["confidence"] for p in problems), default=0.0))
    if type(suggest_reject) is not bool or not _confidence(confidence):
        raise RejectResponseError("oMLX suggest_reject must be boolean and confidence 0 to 1", raw)
    return RejectReview([{"kind": p["kind"], "confidence": float(p["confidence"])} for p in problems],
                        suggest_reject, float(confidence), raw)


def parse_burst_response(raw: str, frame_ids: set[str]) -> BurstCritique:
    payload = _json_object(raw, BurstResponseError)
    if not {"frame_id", "rationale", "confidence"} <= set(payload):  # extra keys are ignored
        raise BurstResponseError("oMLX burst response must contain frame_id, rationale, and confidence", raw)
    frame_id, rationale = payload["frame_id"], payload["rationale"]
    if type(frame_id) is int:  # models answer the numeric label "93" as 93
        frame_id = str(frame_id)
    if not isinstance(frame_id, str) or frame_id not in frame_ids:
        raise BurstResponseError("oMLX returned a frame that was not supplied", raw)
    rationale = _clip_text(rationale, 300, BurstResponseError, raw)
    if not _confidence(payload["confidence"]):
        raise BurstResponseError("oMLX confidence must be a finite number between 0 and 1", raw)
    return BurstCritique(frame_id, rationale, float(payload["confidence"]), raw)


def parse_astro_response(raw: str) -> AstroTriage:
    payload = _json_object(raw, AstroResponseError)
    if set(payload) != {"sky", "confidence", "issues"}:
        raise AstroResponseError("oMLX astro response must contain only sky, confidence, and issues", raw)
    if payload["sky"] not in SKY_CLASSES or not _confidence(payload["confidence"]):
        raise AstroResponseError("oMLX sky must be a known class and confidence 0 to 1", raw)
    issues = payload["issues"]
    if (not isinstance(issues, list) or not all(isinstance(i, str) and i in SKY_ISSUES for i in issues)
            or len(set(issues)) != len(issues)):
        raise AstroResponseError("oMLX issues must be unique known sky issues", raw)
    return AstroTriage(payload["sky"], float(payload["confidence"]), list(issues), raw)


def parse_caption_response(raw: str) -> Caption:
    payload = _json_object(raw, CaptionResponseError)
    if not {"caption", "alt_text"} <= set(payload):  # extra keys are ignored
        raise CaptionResponseError("oMLX caption response must contain caption and alt_text", raw)
    caption, alt_text = payload["caption"], payload["alt_text"]
    caption = _clip_text(caption, CAPTION_MAX, CaptionResponseError, raw, "caption")
    if "#" in caption:
        raise CaptionResponseError("oMLX caption must contain no hashtags", raw)
    alt_text = _clip_text(alt_text, ALT_TEXT_MAX, CaptionResponseError, raw, "alt_text")
    return Caption(caption, alt_text, raw)


def parse_develop_response(raw: str) -> DevelopAdvice:
    """Proposed develop settings, validated by the same DevelopSettings the panel and the renderer use.

    Reusing that validator is the point: a value the model invents outside a slider's range, or a
    control the renderer does not have, fails here rather than at the save the photographer makes
    after they have already looked at a preview.
    """
    payload = _json_object(raw, DevelopAdviceError)
    if not {"settings", "rationale"} <= set(payload):  # extra keys are ignored
        raise DevelopAdviceError("oMLX develop response must contain settings and rationale", raw)
    proposed = payload["settings"]
    if not isinstance(proposed, dict):
        raise DevelopAdviceError("oMLX develop settings must be an object", raw)
    try:
        settings = DevelopSettings.from_dict(proposed)
    except (ValueError, TypeError) as error:
        raise DevelopAdviceError(f"oMLX proposed develop settings Lumen cannot apply: {error}", raw) from error
    rationale = _clip_text(payload["rationale"], DEVELOP_RATIONALE_MAX, DevelopAdviceError, raw)
    return DevelopAdvice(settings.to_dict(), rationale, raw)


def parse_digest_response(raw: str) -> str:
    payload = _json_object(raw, DigestResponseError)
    narrative = payload.get("narrative")
    if set(payload) != {"narrative"} or not isinstance(narrative, str) or not 1 <= len(narrative := narrative.strip()) <= NARRATIVE_MAX:
        raise DigestResponseError(f"oMLX digest response must contain only a narrative of 1 to {NARRATIVE_MAX} characters", raw)
    return narrative


def parse_expand_response(raw: str, vocabulary: dict[str, list[str]]) -> list[str]:
    """Only words from the vocabulary survive, in its spelling, deduped and capped at 8; extra keys are ignored."""
    terms = _json_object(raw, ExpandResponseError).get("terms")
    if not isinstance(terms, list):
        raise ExpandResponseError("oMLX expand response must contain a terms list", raw)
    known = {word.casefold(): word for words in vocabulary.values() for word in words}
    found = [known[term.casefold()] for term in terms if isinstance(term, str) and term.casefold() in known]
    return list(dict.fromkeys(found))[:8]


def _place_sentence(place: str | None) -> str:
    """The place is stated context from the GPS fix; the model never infers it."""
    return f" Taken at {place}: name this place in the text you write." if place else ""


def _vocabulary_system(vocabulary: dict[str, list[str]]) -> str:
    return ("Keyword list by category: "
            + "; ".join(f"{name}: {', '.join(words)}" for name, words in vocabulary.items())
            + ". Choose keywords from the list where one fits; add at most three others.")


def parse_tag_response(raw: str, *, allow_species: bool = True) -> TagResult:
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
    allowed = {"description", "subjects", "scene", "technical", "confidence"}
    if allow_species:
        allowed |= {"species", "conditions"}
    payload = {key: value for key, value in payload.items() if key in allowed}  # models add fields such as "activity"; ignore them
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
    species = payload.get("species", [])
    if not isinstance(species, list) or not all(
            isinstance(item, dict) and set(item) == {"name", "confidence"}
            and isinstance(item["name"], str) and item["name"].strip()
            and isinstance(item["confidence"], (int, float)) and not isinstance(item["confidence"], bool)
            and math.isfinite(item["confidence"]) and 0 <= item["confidence"] <= 1 for item in species):
        raise TagResponseError("oMLX species must be a list of {name, confidence} objects", raw)
    conditions = payload.get("conditions", [])
    if not isinstance(conditions, list) or not all(isinstance(value, str) for value in conditions):
        raise TagResponseError("oMLX conditions must be a list of strings", raw)
    return TagResult(
        description=description.strip(),
        subjects=[value.strip() for value in subjects if value.strip()],
        scene=scene.strip(),
        technical=[value.strip() for value in technical if value.strip()],
        confidence=float(confidence),
        raw_response=raw,
        species=[{"name": item["name"].strip(), "confidence": float(item["confidence"])} for item in species],
        conditions=[value.strip() for value in conditions if value.strip()],
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
    if not isinstance(payload, dict) or not required <= set(payload):  # extra keys are ignored
        raise CompositionResponseError("oMLX composition response must contain the required fields", raw)
    candidate_id = payload["candidate_id"]
    rationale = payload["rationale"]
    confidence = payload["confidence"]
    if not isinstance(candidate_id, str) or candidate_id not in candidate_ids:
        raise CompositionResponseError("oMLX returned an unavailable crop candidate", raw)
    rationale = _clip_text(rationale, 500, CompositionResponseError, raw)
    if (not isinstance(confidence, (int, float)) or isinstance(confidence, bool)
            or not math.isfinite(confidence) or not 0 <= confidence <= 1):
        raise CompositionResponseError("oMLX confidence must be a finite number between 0 and 1", raw)
    return CompositionReview(candidate_id, rationale, float(confidence))


class OmlxTagger:
    def __init__(self, base_url: str, model: str, api_key: str | None = None, *, role: str = "review"):
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("oMLX endpoint must be a loopback http(s) URL")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.role = role
        self.api_key = api_key or self._local_api_key()

    def tag(self, preview: Path, timeout: float = 240, *, vocabulary: dict[str, list[str]] | None = None,
            place: str | None = None) -> TagResult:
        prompt = (
            "Analyze this photograph for a local photo catalog. Return JSON only with: "
            'description (string), subjects (string array), scene (string), '
            "technical (string array of visible focus/exposure/motion observations), "
            "and confidence (number 0 to 1). Do not identify a person."
        )
        system = None
        if vocabulary:  # Stable across a job, so it goes first as a system prefix oMLX can cache; the image changes per call.
            system = _vocabulary_system(vocabulary)
            prompt += (
                " Also return species (array of {name, confidence 0 to 1} for animals you can name) and "
                "conditions (string array of light and weather). Do not identify a person."
            )
        prompt += _place_sentence(place) + " Write the JSON compact on one line, no code fence."  # measured: 2-3 s less decode on the 27B
        content = self._complete([
            {"type": "text", "text": prompt},
            self._image_content(preview),
        ], timeout, system=system)
        return parse_tag_response(content)

    def expand_query(self, query: str, vocabulary: dict[str, list[str]], timeout: float = 60) -> list[str]:
        """Text only: which vocabulary keywords fit a free-text search."""
        prompt = f'Which of these keywords describe photos matching "{query}"? Answer {{"terms": [...]}} using only listed keywords.'
        raw = self._complete([{"type": "text", "text": prompt}], timeout, max_tokens=200, system=_vocabulary_system(vocabulary))
        return parse_expand_response(raw, vocabulary)

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

    def review_rejects(self, preview: Path, timeout: float = 240) -> RejectReview:
        prompt = ("Look for technical failures only: " + ", ".join(PROBLEMS) + ". Return one JSON object only, shaped "
                  '{"problems": [{"kind": "...", "confidence": 0.0}], "suggest_reject": false, "confidence": 0.0}; '
                  "problems may be an empty array. Do not identify a person.")
        content = self._complete([{"type": "text", "text": prompt}, self._image_content(preview)], timeout, max_tokens=300)
        return parse_reject_response(content)

    def triage_astro(self, preview: Path, timeout: float = 240) -> AstroTriage:
        prompt = ("Classify the sky in this long exposure as one of: " + ", ".join(SKY_CLASSES) + ". List any issues "
                  "from: " + ", ".join(SKY_ISSUES) + ". Return JSON only with sky, confidence (0 to 1), issues "
                  "(array, may be empty). Do not identify a person.")
        content = self._complete([{"type": "text", "text": prompt}, self._image_content(preview)], timeout, max_tokens=200)
        return parse_astro_response(content)

    def caption(self, preview: Path, timeout: float = 240, *, place: str | None = None) -> Caption:
        prompt = ("Write caption (under 200 characters, no hashtags, no emoji) and alt_text (under 125 characters, a "
                  "literal description of what is visible for a screen reader). Return JSON only. Do not identify a person."
                  + _place_sentence(place))
        content = self._complete([{"type": "text", "text": prompt}, self._image_content(preview)], timeout, max_tokens=400)
        return parse_caption_response(content)

    def suggest_develop(self, preview: Path, *, settings: dict, histogram_note: str = "",
                        feedback: str | None = None, timeout: float = 240) -> DevelopAdvice:
        """Propose develop settings for one frame, with the rationale that teaches the histogram.

        The frame comes in rendered with `settings` already applied, so the model critiques what the
        photographer is looking at and answers with absolute values, not deltas.
        """
        controls = "; ".join(f"{key} {low} through {high}" for key, (low, high) in DEVELOP_RANGES.items())
        current = (f"The photographer's current settings are {json.dumps(settings, sort_keys=True)}."
                   if settings else "No control has been changed yet; each one sits at this file's own default.")
        prompt = (
            "You are helping a photographer develop one raw frame. Propose develop settings and explain them so "
            "that they learn to read the histogram. Every control is optional: omit one to leave this file's own "
            f"default alone. Controls: {controls}; lens_correction true or false; curve as five [x, y] pairs in 0 "
            "to 1 ascending in x, where y above x brightens that tone and y below x darkens it. exposure_ev is in "
            "stops: positive brightens, negative darkens, +1 doubles the light. A histogram median near 50 is a "
            "balanced exposure; well below 50 is dark and wants positive exposure_ev unless the scene is meant to "
            f"be dark. {current} {histogram_note} Return JSON only, shaped "
            '{"settings": {"<control>": <value>}, "rationale": "<text>"}, with settings (an object of the '
            "controls above, absolute values, empty when the frame needs nothing) and rationale (at most "
            f"{DEVELOP_RATIONALE_MAX} characters, two or three plain sentences naming what you read in the frame "
            "and in the histogram and what each change does about it). Do not identify a person."
        )
        if feedback:
            prompt += f" The photographer says of your previous attempt: {feedback}"
        content = [{"type": "text", "text": prompt}, self._image_content(preview)]
        return parse_develop_response(self._complete(content, timeout, max_tokens=600))

    def write_digest(self, facts_markdown: str, timeout: float = 240) -> DigestNarrative:
        """Text only: the facts table goes to the model, never an image."""
        prompt = ("Write a short field-notes narrative (3 to 6 sentences, plain prose, no lists, no hashtags) from these "
                  "facts only. Do not invent species, places, or counts. Return JSON only with narrative.")
        content = [{"type": "text", "text": prompt}, {"type": "text", "text": facts_markdown}]
        raw = self._complete(content, timeout, max_tokens=600)
        return DigestNarrative(parse_digest_response(raw), raw)

    def critique_burst(self, frames: list[tuple[str, Path]], timeout: float = 240) -> BurstCritique:
        ids = [frame_id for frame_id, _ in frames]
        if not 2 <= len(frames) <= 8:
            raise ValueError("burst critique needs 2 to 8 frames")
        if any(not isinstance(frame_id, str) or not frame_id.strip() for frame_id in ids) or len(set(ids)) != len(ids):
            raise ValueError("burst frame IDs must be unique, non-empty strings")
        content = [{"type": "text", "text": (
            "Compare these frames from one burst for sharpness, expression or pose, timing, framing, and distracting "
            f"elements. Select exactly one frame_id from: {', '.join(ids)}. Return JSON only with frame_id, rationale "
            "(at most 300 characters), confidence (0 to 1). Do not identify a person.")}]
        for frame_id, path in frames:
            content.extend(({"type": "text", "text": f"Frame {frame_id}"}, self._image_content(path, long_edge=0)))  # sharpness is compared per pixel
        return parse_burst_response(self._complete(content, timeout, max_tokens=400), set(ids))

    @staticmethod
    def _image_content(path: Path, *, long_edge: int = MODEL_IMAGE_LONG_EDGE) -> dict:
        if not isinstance(path, Path) or not path.is_file():
            raise ValueError(f"composition preview is unavailable: {path}")
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        data = path.read_bytes()
        if long_edge:
            with Image.open(io.BytesIO(data)) as image:
                if max(image.size) > long_edge:
                    image.thumbnail((long_edge, long_edge))
                    buffer = io.BytesIO()
                    image.convert("RGB").save(buffer, "JPEG", quality=90)
                    data, mime = buffer.getvalue(), "image/jpeg"
        image = base64.b64encode(data).decode("ascii")
        return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image}"}}

    def _complete(self, content: list[dict], timeout: float, *, max_tokens: int = 500, system: str | None = None) -> str:
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": content}]
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},  # Qwen-style thinking otherwise eats the budget and returns no content.
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
            message = result["choices"][0]["message"]
            content = message.get("content")
        except urllib.error.HTTPError as error:
            error.close()
            raise OmlxError(f"Local oMLX request failed: {error}") from error
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError, json.JSONDecodeError) as error:
            raise OmlxError(f"Local oMLX request failed: {error}") from error
        if not isinstance(content, str):
            if isinstance(message, dict) and message.get("reasoning_content"):
                raise OmlxError("Local oMLX model spent its token budget on reasoning and returned no answer; use a model that answers directly")
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

