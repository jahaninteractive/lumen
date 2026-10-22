import unittest
import urllib.error
import tempfile
import json
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import lumen.omlx as omlx
from lumen.omlx import (
    CompositionResponseError,
    NoRedirectHandler,
    OmlxError,
    OmlxTagger,
    TagResponseError,
    parse_composition_response,
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
        self.assertEqual("Bearer secret", request.headers["Authorization"])

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


if __name__ == "__main__":
    unittest.main()
