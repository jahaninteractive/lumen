import unittest
import urllib.error
import tempfile
import json
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import lumen.omlx as omlx
from lumen.omlx import (
    AstroResponseError,
    BurstResponseError,
    CaptionResponseError,
    DigestNarrative,
    DigestResponseError,
    CompositionResponseError,
    NoRedirectHandler,
    OmlxError,
    OmlxTagger,
    PROBLEMS,
    RejectResponseError,
    TagResponseError,
    parse_astro_response,
    parse_burst_response,
    parse_caption_response,
    parse_composition_response,
    parse_digest_response,
    parse_reject_response,
    parse_tag_response,
)


class OmlxTests(unittest.TestCase):
    def test_parses_clean_and_fenced_json(self):
        raw = '{"description":"A heron","subjects":["heron"],"scene":"wetland","technical":["sharp eye"],"confidence":0.92}'
        clean = parse_tag_response(raw)
        fenced = parse_tag_response(f"```json\n{raw}\n```")

        self.assertEqual("A heron", clean.description)
        self.assertEqual(["heron"], fenced.subjects)
        self.assertAlmostEqual(0.92, clean.confidence)

    def test_malformed_or_wrong_shape_is_rejected(self):
        for raw in ("not json", '{"description":"x","subjects":"bird"}', '{"description":"x","subjects":[],"confidence":2}'):
            with self.subTest(raw=raw):
                with self.assertRaises(TagResponseError):
                    parse_tag_response(raw)

    def test_composition_response_accepts_fenced_json_and_only_supplied_ids(self):
        raw = '{"candidate_id":"crop-original","rationale":"The wider frame preserves context.","confidence":0.8}'

        result = parse_composition_response(f"```json\n{raw}\n```", {"crop-original", "crop-other"})

        self.assertEqual("crop-original", result.candidate_id)
        self.assertEqual("The wider frame preserves context.", result.rationale)
        self.assertEqual(0.8, result.confidence)

    def test_composition_response_rejects_unknown_id_unbounded_text_and_invalid_confidence(self):
        invalid = (
            '{"candidate_id":"crop-invented","rationale":"valid","confidence":0.5}',
            json.dumps({"candidate_id": "crop-original", "rationale": "x" * 501, "confidence": 0.5}),
            '{"candidate_id":"crop-original","rationale":"valid","confidence":NaN}',
            '{"candidate_id":"crop-original","rationale":"valid","confidence":true}',
            '{"candidate_id":"crop-original","rationale":"valid","confidence":0.5,"left":0}',
            '```json\n{"candidate_id":"crop-original","rationale":"valid","confidence":0.5}\n``` trailing',
        )
        for raw in invalid:
            with self.subTest(raw=raw):
                with self.assertRaises(CompositionResponseError) as raised:
                    parse_composition_response(raw, {"crop-original"})
                self.assertEqual(raw, raised.exception.raw_response)

    def test_composition_review_sends_one_labeled_multimodal_request(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            def read(self):
                return json.dumps({"choices": [{"message": {"content": (
                    '{"candidate_id":"crop-blue","rationale":"Cleaner balance.","confidence":0.9}'
                )}}]}).encode()

        class Opener:
            def __init__(self):
                self.calls = []

            def open(self, request, timeout):
                self.calls.append((request, timeout))
                return Response()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.jpg"
            blue = root / "blue.jpg"
            square = root / "square.png"
            Image.new("RGB", (20, 10), "gray").save(original)
            Image.new("RGB", (10, 10), "blue").save(blue)
            Image.new("RGB", (10, 10), "green").save(square)
            opener = Opener()
            with patch("lumen.omlx.urllib.request.build_opener", return_value=opener):
                result = OmlxTagger(
                    "http://127.0.0.1:8000", "vision-model", api_key="secret"
                ).review_composition(
                    original,
                    (("crop-original", original), ("crop-blue", blue), ("crop-square", square)),
                    timeout=17,
                )

        self.assertEqual("crop-blue", result.candidate_id)
        self.assertEqual(1, len(opener.calls))
        request, timeout = opener.calls[0]
        payload = json.loads(request.data)
        content = payload["messages"][0]["content"]
        self.assertEqual(17, timeout)
        images = [item["image_url"]["url"] for item in content if item["type"] == "image_url"]
        self.assertEqual(3, len(images))
        self.assertTrue(images[0].startswith("data:image/jpeg;base64,"))
        self.assertTrue(images[1].startswith("data:image/jpeg;base64,"))
        self.assertTrue(images[2].startswith("data:image/png;base64,"))
        labels = " ".join(item["text"] for item in content if item["type"] == "text")
        self.assertIn("crop-original", labels)
        self.assertIn("crop-blue", labels)
        self.assertIn("crop-square", labels)
        self.assertIn("Do not return coordinates", labels)
        self.assertEqual(0, payload["temperature"])
        self.assertEqual({"enable_thinking": False}, payload["chat_template_kwargs"], "thinking models must answer, not reason into the token budget")
        self.assertEqual("Bearer secret", request.headers["Authorization"])

    def test_reasoning_only_reply_is_reported_clearly(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            def read(self):
                return json.dumps({"choices": [{"finish_reason": "length", "message": {"role": "assistant", "reasoning_content": "Let me think..."}}]}).encode()

        class Opener:
            def open(self, request, timeout):
                return Response()

        with tempfile.TemporaryDirectory() as directory:
            preview = Path(directory) / "preview.jpg"
            Image.new("RGB", (20, 10), "gray").save(preview)
            with patch("lumen.omlx.urllib.request.build_opener", return_value=Opener()):
                with self.assertRaisesRegex(OmlxError, "reasoning") as caught:
                    OmlxTagger("http://127.0.0.1:8000", "vision-model").tag(preview)
        self.assertNotIn("'content'", str(caught.exception))

    def test_composition_review_validates_candidates_before_request(self):
        with tempfile.TemporaryDirectory() as directory:
            preview = Path(directory) / "preview.jpg"
            Image.new("RGB", (10, 10)).save(preview)
            tagger = OmlxTagger("http://127.0.0.1:8000", "model", api_key="secret")
            cases = (
                (),
                (("", preview),),
                (("same", preview), ("same", preview)),
                (("missing", preview.with_name("missing.jpg")),),
            )
            with patch("lumen.omlx.urllib.request.build_opener") as build:
                for candidates in cases:
                    with self.subTest(candidates=candidates), self.assertRaises((ValueError, OSError)):
                        tagger.review_composition(preview, candidates)
            build.assert_not_called()

    def test_reject_response_requires_known_problem_kinds_and_exact_fields(self):
        good = json.dumps({"problems": [{"kind": "motion_blur", "confidence": 0.84}], "suggest_reject": True, "confidence": 0.8})
        result = parse_reject_response(good)
        self.assertEqual(([{"kind": "motion_blur", "confidence": 0.84}], True), (result.problems, result.suggest_reject))
        for bad in ({"problems": [{"kind": "ugly", "confidence": 0.5}], "suggest_reject": False, "confidence": 0.5},
                    {"problems": [], "suggest_reject": "yes", "confidence": 0.5},
                    {"problems": [], "suggest_reject": False, "confidence": 0.5, "note": "x"},
                    {"problems": [{"kind": "misfire", "confidence": 0.5}, {"kind": "misfire", "confidence": 0.6}], "suggest_reject": True, "confidence": 0.5}):
            with self.subTest(bad=bad), self.assertRaises(RejectResponseError):
                parse_reject_response(json.dumps(bad))

    def test_review_rejects_sends_one_image_listing_every_problem_with_small_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            preview = Path(directory) / "p.jpg"
            Image.new("RGB", (8, 8)).save(preview)
            tagger = OmlxTagger("http://127.0.0.1:8000", "fast-model", "key", role="fast")
            reply = '{"problems": [], "suggest_reject": false, "confidence": 0.7}'
            with patch.object(tagger, "_complete", return_value=reply) as complete:
                result = tagger.review_rejects(preview)
        content = complete.call_args.args[0]
        self.assertEqual(["text", "image_url"], [part["type"] for part in content])
        self.assertTrue(all(kind in content[0]["text"] for kind in PROBLEMS))
        self.assertIn("Do not identify a person.", content[0]["text"])
        self.assertEqual(300, complete.call_args.kwargs["max_tokens"])
        self.assertFalse(result.suggest_reject)

    def test_remote_endpoint_is_rejected(self):
        with self.assertRaises(ValueError):
            OmlxTagger("https://example.com", "model", api_key="secret")

    def test_loopback_endpoint_is_allowed(self):
        tagger = OmlxTagger("http://127.0.0.1:8000", "Qwen3.8-27B-4bit", api_key="secret")
        self.assertEqual("Qwen3.8-27B-4bit", tagger.model)

    def test_model_request_uses_an_opener_that_refuses_redirects(self):
        class RedirectingOpener:
            def open(self, request, timeout):
                raise urllib.error.HTTPError(request.full_url, 302, "Found", {}, None)

        with tempfile.TemporaryDirectory() as directory:
            preview = Path(directory) / "preview.jpg"
            Image.new("RGB", (10, 10)).save(preview)
            with patch("lumen.omlx.urllib.request.build_opener", return_value=RedirectingOpener()) as build:
                with self.assertRaises(OmlxError):
                    OmlxTagger("http://127.0.0.1:8000", "model", api_key="secret").tag(preview)

        self.assertIsInstance(build.call_args.args[0], NoRedirectHandler)

    def test_live_status_reports_only_models_loaded_in_memory(self):
        get_server_status = getattr(omlx, "get_server_status", None)
        self.assertIsNotNone(get_server_status, "live oMLX status reader is missing")

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            def read(self):
                return json.dumps(
                    {
                        "status": "ok",
                        "version": "0.7.2",
                        "default_model": "Qwen3.6-35B-A3B-4bit",
                        "loaded_models": ["Qwen3.5-9B-4bit"],
                    }
                ).encode()

        class Opener:
            def open(self, request, timeout):
                self.request = request
                return Response()

        opener = Opener()
        with patch("lumen.omlx.urllib.request.build_opener", return_value=opener):
            status = get_server_status("http://127.0.0.1:8000", api_key="secret")

        self.assertEqual(["Qwen3.5-9B-4bit"], status["loaded_models"])
        self.assertEqual("Qwen3.6-35B-A3B-4bit", status["default_model"])
        self.assertEqual("Bearer secret", opener.request.headers["Authorization"])


    def test_tag_response_accepts_species_and_conditions_and_rejects_extra_fields(self):
        raw = json.dumps({"description": "Two penguins on ice", "subjects": ["gentoo penguin"], "scene": "ice",
                          "technical": [], "confidence": 0.9,
                          "species": [{"name": "gentoo penguin", "confidence": 0.82}], "conditions": ["overcast"]})
        result = parse_tag_response(raw)
        self.assertEqual([{"name": "gentoo penguin", "confidence": 0.82}], result.species)
        self.assertEqual(["overcast"], result.conditions)
        for bad in ({"species": [{"name": "x"}]}, {"species": [{"name": "x", "confidence": 2}]}, {"people": ["a"]}):
            with self.subTest(bad=bad), self.assertRaises(TagResponseError):
                parse_tag_response(json.dumps({"description": "d", "subjects": [], "scene": "", "technical": [], "confidence": 0.5} | bad))

    def test_tag_prompt_lists_vocabulary_and_forbids_identifying_people(self):
        tagger = OmlxTagger("http://127.0.0.1:8000", "fast-model", api_key="k", role="fast")
        captured = {}

        def fake_complete(content, timeout, **kwargs):
            captured["content"] = content
            captured["kwargs"] = kwargs
            return json.dumps({"description": "d", "subjects": [], "scene": "", "technical": [], "confidence": 0.5, "species": [], "conditions": []})

        with tempfile.TemporaryDirectory() as directory:
            preview = Path(directory) / "p.jpg"
            Image.new("RGB", (8, 8)).save(preview)
            with patch.object(tagger, "_complete", fake_complete):
                tagger.tag(preview, vocabulary={"wildlife": ["gentoo penguin"], "scene": ["ice"]})
        text = captured["content"][0]["text"]
        self.assertIn("wildlife: gentoo penguin", text)
        self.assertIn("Do not identify a person.", text)
        self.assertEqual("fast", tagger.role)

    def test_astro_response_requires_known_sky_class_and_issues(self):
        good = json.dumps({"sky": "milky_way", "confidence": 0.91, "issues": ["haze"]})
        self.assertEqual(("milky_way", ["haze"]), (parse_astro_response(good).sky, parse_astro_response(good).issues))
        for bad in ({"sky": "day", "confidence": 0.5, "issues": []}, {"sky": "cloud", "confidence": 0.5, "issues": ["rain"]},
                    {"sky": "cloud", "confidence": 0.5, "issues": ["haze", "haze"]}, {"sky": "cloud", "confidence": 0.5}):
            with self.subTest(bad=bad), self.assertRaises(AstroResponseError):
                parse_astro_response(json.dumps(bad))

    def test_caption_response_enforces_lengths_and_no_hashtags(self):
        good = json.dumps({"caption": "Gentoo penguins porpoise beside the zodiac.", "alt_text": "Three penguins leaping from grey water near a black inflatable boat."})
        self.assertEqual("Gentoo penguins porpoise beside the zodiac.", parse_caption_response(good).caption)
        for bad in ({"caption": "x" * 201, "alt_text": "a"}, {"caption": "fun #trip", "alt_text": "a"}, {"caption": "a", "alt_text": "x" * 126},
                    {"caption": "a", "alt_text": "b", "hashtags": []}, {"caption": "", "alt_text": "b"}):
            with self.subTest(bad=bad), self.assertRaises(CaptionResponseError):
                parse_caption_response(json.dumps(bad))

    def test_caption_prompt_forbids_identification_with_short_budget(self):
        tagger = OmlxTagger("http://127.0.0.1:8000", "review-model", api_key="k", role="review")
        captured = {}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "c.jpg"; Image.new("RGB", (8, 8)).save(path)
            reply = json.dumps({"caption": "Ice.", "alt_text": "White ice."})
            with patch.object(tagger, "_complete", lambda content, timeout, **kw: (captured.update(content=content, kw=kw), reply)[1]):
                result = tagger.caption(path)
        self.assertEqual(("Ice.", "White ice."), (result.caption, result.alt_text))
        self.assertIn("Do not identify a person.", captured["content"][0]["text"])
        self.assertEqual(200, captured["kw"]["max_tokens"])

    def test_digest_response_is_a_single_bounded_narrative_and_request_sends_no_image(self):
        self.assertEqual("Calm.", parse_digest_response(json.dumps({"narrative": "Calm."})))
        for bad in ({"narrative": ""}, {"narrative": "x" * 1501}, {"narrative": "a", "title": "b"}, ["a"]):
            with self.subTest(bad=bad), self.assertRaises(DigestResponseError):
                parse_digest_response(json.dumps(bad))
        tagger = OmlxTagger("http://127.0.0.1:8000", "review-model", api_key="k", role="review")
        captured = {}
        with patch.object(tagger, "_complete", lambda content, timeout, **kw: (captured.update(content=content), json.dumps({"narrative": "Calm."}))[1]):
            result = tagger.write_digest("| Frames | 2 |")
        self.assertEqual(DigestNarrative("Calm.", json.dumps({"narrative": "Calm."})), result)
        self.assertTrue(all(part["type"] == "text" for part in captured["content"]))
        self.assertIn("Do not invent species, places, or counts.", captured["content"][0]["text"])

    def test_burst_response_requires_supplied_frame_and_bounded_rationale(self):
        good = json.dumps({"frame_id": "7", "rationale": "Sharpest eye, wing fully extended.", "confidence": 0.72})
        self.assertEqual("7", parse_burst_response(good, {"7", "8"}).frame_id)
        for bad in ({"frame_id": "9", "rationale": "x", "confidence": 0.5}, {"frame_id": "7", "rationale": "x" * 301, "confidence": 0.5},
                    {"frame_id": "7", "rationale": "x", "confidence": 0.5, "second": "8"}, {"frame_id": 7, "rationale": "x", "confidence": 0.5}):
            with self.subTest(bad=bad), self.assertRaises(BurstResponseError):
                parse_burst_response(json.dumps(bad), {"7", "8"})

    def test_critique_burst_sends_labeled_frames_and_validates_count(self):
        tagger = OmlxTagger("http://127.0.0.1:8000", "review-model", api_key="k", role="review")
        captured = {}
        with tempfile.TemporaryDirectory() as directory:
            frames = []
            for index in range(3):
                path = Path(directory) / f"{index}.jpg"; Image.new("RGB", (8, 8)).save(path); frames.append((str(index + 1), path))
            with patch.object(tagger, "_complete", lambda content, timeout, **kw: (captured.update(content=content, kw=kw), json.dumps({"frame_id": "2", "rationale": "ok", "confidence": 0.6}))[1]):
                result = tagger.critique_burst(frames)
            with self.assertRaises(ValueError):
                tagger.critique_burst(frames[:1])
            with self.assertRaises(ValueError):
                tagger.critique_burst(frames * 3)
        self.assertEqual("2", result.frame_id)
        texts = [part["text"] for part in captured["content"] if part["type"] == "text"]
        self.assertIn("Do not identify a person.", texts[0])
        self.assertEqual(["Frame 1", "Frame 2", "Frame 3"], texts[1:])
        self.assertEqual(3, sum(part["type"] == "image_url" for part in captured["content"]))
        self.assertEqual(400, captured["kw"]["max_tokens"])

    def test_triage_astro_sends_one_image_with_classes_and_short_budget(self):
        tagger = OmlxTagger("http://127.0.0.1:8000", "fast-model", api_key="k", role="fast")
        captured = {}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "l.jpg"; Image.new("RGB", (8, 8)).save(path)
            reply = json.dumps({"sky": "aurora", "confidence": 0.7, "issues": ["trailing"]})
            with patch.object(tagger, "_complete", lambda content, timeout, **kw: (captured.update(content=content, kw=kw), reply)[1]):
                result = tagger.triage_astro(path)
        self.assertEqual(("aurora", ["trailing"], reply), (result.sky, result.issues, result.raw_response))
        prompt = captured["content"][0]["text"]
        self.assertIn("Do not identify a person.", prompt)
        self.assertTrue(all(name in prompt for name in omlx.SKY_CLASSES + omlx.SKY_ISSUES))
        self.assertEqual(1, sum(part["type"] == "image_url" for part in captured["content"]))
        self.assertEqual(200, captured["kw"]["max_tokens"])


if __name__ == "__main__":
    unittest.main()
