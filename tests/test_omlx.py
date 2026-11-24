import base64
import http.client
import io
import unittest
import urllib.error
import tempfile
import json
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageCms

import lumen.omlx as omlx
from lumen.omlx import (
    _vocabulary_system,
    AstroResponseError,
    BurstResponseError,
    CaptionResponseError,
    DigestNarrative,
    DigestResponseError,
    ExpandResponseError,
    CompositionResponseError,
    DevelopAdviceError,
    NoRedirectHandler,
    OmlxError,
    REMOTE_REASONING_TOKENS,
    OmlxTagger,
    PROBLEMS,
    RejectResponseError,
    TagResponseError,
    parse_astro_response,
    parse_burst_response,
    parse_caption_response,
    parse_composition_response,
    parse_develop_response,
    parse_digest_response,
    parse_expand_response,
    parse_reject_response,
    parse_tag_response,
    held_models,
    list_models,
    normalise_engine_url,
    refresh_models,
    reset_models,
    reset_usage,
    usage_snapshot,
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

    def test_composition_response_keeps_long_rationales_and_ignores_extra_fields(self):
        # Real Qwen3.8-27B replies ran past 500 characters and were thrown away (2026-10-23); clip at a word instead.
        words = ("The tighter framing removes the distracting branches on the left and keeps the impala " * 8).strip()
        raw = json.dumps({"candidate_id": "crop-original", "rationale": words, "confidence": 0.9, "left": 0})
        result = parse_composition_response(raw, {"crop-original"})
        self.assertLessEqual(len(result.rationale), 500)
        self.assertTrue(result.rationale.endswith("…"))
        self.assertTrue(words.startswith(result.rationale[:-1].rstrip()))
        self.assertNotIn(" …", result.rationale, "clipped at a word boundary without a dangling space")

    def test_composition_response_rejects_unknown_id_unbounded_text_and_invalid_confidence(self):
        invalid = (
            '{"candidate_id":"crop-invented","rationale":"valid","confidence":0.5}',
            '{"candidate_id":"crop-original","rationale":"","confidence":0.5}',
            '{"candidate_id":"crop-original","rationale":"valid","confidence":NaN}',
            '{"candidate_id":"crop-original","rationale":"valid","confidence":true}',
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
                    {"problems": [{"kind": "misfire", "confidence": 0.5}, {"kind": "misfire", "confidence": 0.6}], "suggest_reject": True, "confidence": 0.5}):
            with self.subTest(bad=bad), self.assertRaises(RejectResponseError):
                parse_reject_response(json.dumps(bad))

    def test_reject_response_accepts_a_bare_problem_list_and_ignores_extra_keys(self):
        # Qwen3-VL-8B answers the scan with just the array; a list means problems only, no reject suggestion.
        result = parse_reject_response('```json\n[{"kind": "out_of_focus", "confidence": 0.7}]\n```')
        self.assertEqual(([{"kind": "out_of_focus", "confidence": 0.7}], False, 0.7),
                         (result.problems, result.suggest_reject, result.confidence))
        self.assertEqual(([], False, 0.0), (parse_reject_response("[]").problems, False, parse_reject_response("[]").confidence))
        extra = json.dumps({"problems": [], "suggest_reject": True, "confidence": 0.9, "note": "x"})
        self.assertTrue(parse_reject_response(extra).suggest_reject)
        partial = json.dumps({"problems": [{"kind": "misfire", "confidence": 0.8}]})
        self.assertEqual((False, 0.8), (parse_reject_response(partial).suggest_reject, parse_reject_response(partial).confidence))

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


    def test_tag_response_accepts_species_conditions_and_ignores_extra_fields(self):
        raw = json.dumps({"description": "Two penguins on ice", "subjects": ["gentoo penguin"], "scene": "ice",
                          "technical": [], "confidence": 0.9,
                          "species": [{"name": "gentoo penguin", "confidence": 0.82}], "conditions": ["overcast"]})
        result = parse_tag_response(raw)
        self.assertEqual([{"name": "gentoo penguin", "confidence": 0.82}], result.species)
        self.assertEqual(["overcast"], result.conditions)
        # Real Qwen3-VL-8B replies add fields such as "activity" (2026-10-23); unknown top-level keys are ignored.
        extra = parse_tag_response(json.dumps({"description": "d", "subjects": ["impala"], "scene": "", "technical": [], "confidence": 0.5, "activity": ["feeding"]}))
        self.assertEqual(["impala"], extra.subjects)
        for bad in ({"species": [{"name": "x"}]}, {"species": [{"name": "x", "confidence": 2}]}):
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
        self.assertNotIn("wildlife", text)  # the stable vocabulary goes first, as a system prefix oMLX can cache
        self.assertIn("wildlife: gentoo penguin", captured["kwargs"]["system"])
        self.assertIn("Do not identify a person.", text)
        self.assertTrue(text.endswith(" Write the JSON compact on one line, no code fence."))
        self.assertEqual("fast", tagger.role)

    def test_vocabulary_system_prefix_leaves_out_the_learned_words(self):
        # Learned words are the model's own earlier answers, so they steer nothing, and on the review model the
        # prefix is prefilled on every request (oMLX cannot cache it for hybrid-attention models): measured 2.5 s per tag.
        system = _vocabulary_system({"wildlife": ["gentoo penguin"], "learned": ["arm", "sleeve"], "places": ["Saint Helena"]})
        self.assertIn("wildlife: gentoo penguin", system)
        self.assertIn("places: Saint Helena", system)
        self.assertNotIn("learned", system)
        self.assertNotIn("sleeve", system)

    def test_complete_sends_system_prefix_before_the_user_turn(self):
        tagger = OmlxTagger("http://127.0.0.1:8000", "fast-model", api_key="k")
        calls = []

        class Opener:
            def open(self, request, timeout):
                calls.append(request)
                return io.BytesIO(json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode())

        with patch("lumen.omlx.urllib.request.build_opener", return_value=Opener()):
            tagger._complete([{"type": "text", "text": "hi"}], 5, system="rules")
            tagger._complete([{"type": "text", "text": "hi"}], 5)
        with_system = json.loads(calls[0].data)["messages"]
        self.assertEqual([{"role": "system", "content": "rules"}], with_system[:1])
        self.assertEqual("user", with_system[1]["role"])
        self.assertEqual(["user"], [m["role"] for m in json.loads(calls[1].data)["messages"]])

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
        # Real Qwen3.8-27B alt text ran past 125 characters (2026-10-23); long text is clipped at a word, never discarded.
        long = parse_caption_response(json.dumps({"caption": "Penguins leap from grey water " * 12, "alt_text": "Three penguins leaping from grey water near a boat " * 4, "hashtags": []}))
        self.assertLessEqual(len(long.caption), 200)
        self.assertLessEqual(len(long.alt_text), 125)
        self.assertTrue(long.caption.endswith("…") and long.alt_text.endswith("…"))
        self.assertNotIn(" …", long.alt_text)
        for bad in ({"caption": "fun #trip", "alt_text": "a"}, {"caption": "", "alt_text": "b"}, {"caption": "a", "alt_text": " "}, {"caption": "a"}):
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
        self.assertEqual(400, captured["kw"]["max_tokens"])

    def test_tag_and_caption_prompts_state_the_place_only_when_given(self):
        tagger = OmlxTagger("http://127.0.0.1:8000", "m", api_key="k", role="fast")
        replies = {"tag": json.dumps({"description": "d", "subjects": [], "scene": "", "technical": [], "confidence": 0.5}),
                   "caption": json.dumps({"caption": "Ice.", "alt_text": "White ice."})}
        place = "Madikwe Game Reserve, South Africa"
        with tempfile.TemporaryDirectory() as directory:
            preview = Path(directory) / "p.jpg"; Image.new("RGB", (8, 8)).save(preview)
            for method in ("tag", "caption"):
                for given in (place, None):
                    captured = {}
                    with self.subTest(method=method, place=given), patch.object(
                            tagger, "_complete", lambda content, timeout, **kw: (captured.update(text=content[0]["text"]), replies[method])[1]):
                        getattr(tagger, method)(preview, place=given)
                        if given:
                            self.assertIn("Taken at Madikwe Game Reserve, South Africa: name this place in the text you write.", captured["text"])
                        else:
                            self.assertNotIn("Taken at", captured["text"])

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

    def test_expand_response_keeps_vocabulary_words_only_in_vocabulary_spelling(self):
        vocabulary = {"wildlife": ["gentoo penguin", "tern", "seal"], "scene": [f"w{i}" for i in range(10)]}
        reply = {"terms": ["Gentoo Penguin", "dragon", "tern", "TERN", 3], "note": "ignored"}
        self.assertEqual(["gentoo penguin", "tern"], parse_expand_response(json.dumps(reply), vocabulary))
        many = json.dumps({"terms": [f"W{i}" for i in range(10)]})
        self.assertEqual([f"w{i}" for i in range(8)], parse_expand_response(many, vocabulary))
        for bad in ({"x": 1}, {"terms": "tern"}):
            with self.subTest(bad=bad), self.assertRaises(ExpandResponseError):
                parse_expand_response(json.dumps(bad), vocabulary)
        self.assertTrue(issubclass(ExpandResponseError, OmlxError))

    def test_expand_query_is_text_only_with_the_vocabulary_as_system_prefix(self):
        tagger = OmlxTagger("http://127.0.0.1:8000", "fast-model", api_key="k", role="fast")
        captured = {}
        reply = json.dumps({"terms": ["tern"]})
        with patch.object(tagger, "_complete", lambda content, timeout, **kw: (captured.update(content=content, kw=kw), reply)[1]):
            terms = tagger.expand_query("seabirds", {"wildlife": ["tern"]})
        self.assertEqual(["tern"], terms)
        self.assertTrue(all(part["type"] == "text" for part in captured["content"]))
        self.assertEqual('Which of these keywords describe photos matching "seabirds"? Answer {"terms": [...]} using only listed keywords.',
                         captured["content"][0]["text"])
        self.assertIn("wildlife: tern", captured["kw"]["system"])
        self.assertEqual(200, captured["kw"]["max_tokens"])

    def test_burst_response_requires_supplied_frame_and_bounded_rationale(self):
        good = json.dumps({"frame_id": "7", "rationale": "Sharpest eye, wing fully extended.", "confidence": 0.72})
        self.assertEqual("7", parse_burst_response(good, {"7", "8"}).frame_id)
        long = parse_burst_response(json.dumps({"frame_id": "7", "rationale": "sharp eye " * 40, "confidence": 0.5, "second": "8"}), {"7", "8"})
        self.assertLessEqual(len(long.rationale), 300)
        self.assertTrue(long.rationale.endswith("…"))
        # Real Qwen3.8-27B answered "frame_id": 93 as a number for frames labelled "93" (2026-10-23); integers are accepted.
        self.assertEqual("7", parse_burst_response(json.dumps({"frame_id": 7, "rationale": "x", "confidence": 0.5}), {"7", "8"}).frame_id)
        for bad in ({"frame_id": "9", "rationale": "x", "confidence": 0.5}, {"frame_id": "7", "rationale": " ", "confidence": 0.5},
                    {"frame_id": 9, "rationale": "x", "confidence": 0.5}, {"frame_id": True, "rationale": "x", "confidence": 0.5},
                    {"frame_id": 7.0, "rationale": "x", "confidence": 0.5}):
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

    def test_image_content_resizes_to_the_model_long_edge_except_for_bursts(self):
        with tempfile.TemporaryDirectory() as directory:
            big = Path(directory) / "big.jpg"; Image.new("RGB", (3000, 2000), "gray").save(big)
            small = Path(directory) / "small.png"; Image.new("RGB", (600, 400), "gray").save(small)
            tagger = OmlxTagger("http://127.0.0.1:8000", "m", api_key="k")
            sent = tagger._image_content(big)["image_url"]["url"]
            kept = tagger._image_content(small)["image_url"]["url"]
            full = tagger._image_content(big, long_edge=0)["image_url"]["url"]
        def size(url):
            return Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).size
        self.assertEqual(1024, omlx.MODEL_IMAGE_LONG_EDGE)
        self.assertEqual((1024, 683), size(sent))
        self.assertTrue(kept.startswith("data:image/png;base64,"))
        self.assertEqual((600, 400), size(kept))
        self.assertEqual((3000, 2000), size(full))

    def test_critique_burst_sends_frames_at_full_size(self):
        tagger = OmlxTagger("http://127.0.0.1:8000", "m", api_key="k")
        captured = {}
        with tempfile.TemporaryDirectory() as directory:
            frames = []
            for index in range(2):
                path = Path(directory) / f"{index}.jpg"; Image.new("RGB", (1600, 1000)).save(path); frames.append((str(index + 1), path))
            with patch.object(tagger, "_complete", lambda content, timeout, **kw: (captured.update(content=content), json.dumps({"frame_id": "1", "rationale": "ok", "confidence": 0.6}))[1]):
                tagger.critique_burst(frames)
        urls = [part["image_url"]["url"] for part in captured["content"] if part["type"] == "image_url"]
        sizes = {Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).size for url in urls}
        self.assertEqual({(1600, 1000)}, sizes)

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



class DevelopAdviceTests(unittest.TestCase):
    """The review model proposes develop settings; the panel parks the sliders and the photographer decides."""

    def test_develop_response_validates_against_the_real_develop_settings(self):
        reply = json.dumps({"settings": {"exposure_ev": 0.45, "clarity": 0.2},
                            "rationale": "Nothing in the frame reaches white.", "confidence": 0.8})
        advice = parse_develop_response(reply)
        self.assertEqual({"exposure_ev": 0.45, "clarity": 0.2}, advice.settings)
        self.assertEqual("Nothing in the frame reaches white.", advice.rationale)
        self.assertEqual(reply, advice.raw_response)
        # An empty object is a legitimate answer: the frame needs nothing.
        self.assertEqual({}, parse_develop_response(json.dumps({"settings": {}, "rationale": "Already there."})).settings)

    def test_develop_response_refuses_unknown_keys_bad_ranges_and_missing_text(self):
        for bad in ({"settings": {"vibrance": 0.5}, "rationale": "r"},      # not a knob the renderer has
                    {"settings": {"exposure_ev": 9}, "rationale": "r"},      # outside the slider's range
                    {"settings": {"exposure_ev": "0.5"}, "rationale": "r"},  # a string, not a number
                    {"settings": {"curve": [[0, 0], [1, 1]]}, "rationale": "r"},  # wrong point count
                    {"settings": {"exposure_ev": 0.5}, "rationale": "  "},   # the teaching text is the product
                    {"settings": [], "rationale": "r"},
                    {"rationale": "r"},
                    {"settings": {}}):
            with self.subTest(bad=bad), self.assertRaises(DevelopAdviceError):
                parse_develop_response(json.dumps(bad))

    def test_develop_rationale_is_clipped_rather_than_discarded(self):
        long = parse_develop_response(json.dumps({"settings": {}, "rationale": "The right third is empty " * 60}))
        self.assertLessEqual(len(long.rationale), 600)
        self.assertTrue(long.rationale.endswith("…"))

    def test_suggest_develop_prompt_carries_the_knobs_the_histogram_and_the_feedback(self):
        tagger = OmlxTagger("http://127.0.0.1:8000", "review-model", api_key="k", role="review")
        captured = {}
        reply = json.dumps({"settings": {"exposure_ev": 0.3}, "rationale": "Lift it."})
        with tempfile.TemporaryDirectory() as directory:
            preview = Path(directory) / "p.jpg"
            Image.new("RGB", (8, 8)).save(preview)
            with patch.object(tagger, "_complete", lambda content, timeout, **kw: (captured.update(content=content, kw=kw), reply)[1]):
                advice = tagger.suggest_develop(preview, settings={"contrast": 0.7},
                                                histogram_note="5.0% are blown at white.", feedback="too warm")
        self.assertEqual({"exposure_ev": 0.3}, advice.settings)
        prompt = captured["content"][0]["text"]
        self.assertIn("exposure_ev", prompt)
        self.assertIn("-3.0 through 3.0", prompt)  # the range, so the model cannot propose a value the panel rejects
        self.assertIn('"contrast": 0.7', prompt)
        self.assertIn("5.0% are blown at white.", prompt)
        self.assertIn("too warm", prompt)
        self.assertIn("Do not identify a person.", prompt)
        self.assertIn("positive brightens", prompt)  # small models otherwise call -0.5 EV a lift
        self.assertIn('{"settings": {', prompt)  # the shape, so a flat settings object is not returned
        self.assertEqual("image_url", captured["content"][-1]["type"])

    def test_suggest_develop_prompt_is_unchanged_by_controls_it_does_not_offer(self):
        """straighten_deg is a develop range the prompt does not offer yet: the text stays byte for byte."""
        tagger = OmlxTagger("http://127.0.0.1:8000", "review-model", api_key="k", role="review")
        captured = {}
        reply = json.dumps({"settings": {}, "rationale": "Nothing needed."})
        with tempfile.TemporaryDirectory() as directory:
            preview = Path(directory) / "p.jpg"
            Image.new("RGB", (8, 8)).save(preview)
            with patch.object(tagger, "_complete", lambda content, timeout, **kw: (captured.update(content=content), reply)[1]):
                tagger.suggest_develop(preview, settings={}, histogram_note="", feedback=None)
        self.assertEqual(
            "You are helping a photographer develop one raw frame. Propose develop settings and explain them so "
            "that they learn to read the histogram. Every control is optional: omit one to leave this file's own "
            "default alone. Controls: exposure_ev -3.0 through 3.0; temperature_k 2000 through 12000; tint -150 "
            "through 150; contrast 0.0 through 1.0; sharpness 0.0 through 1.0; luminance_nr 0.0 through 1.0; "
            "color_nr 0.0 through 1.0; clarity 0.0 through 1.0; lens_correction true or false; curve as five "
            "[x, y] pairs in 0 to 1 ascending in x, where y above x brightens that tone and y below x darkens it. "
            "exposure_ev is in stops: positive brightens, negative darkens, +1 doubles the light. A histogram "
            "median near 50 is a balanced exposure; well below 50 is dark and wants positive exposure_ev unless "
            "the scene is meant to be dark. No control has been changed yet; each one sits at this file's own "
            "default.  Return JSON only, shaped {\"settings\": {\"<control>\": <value>}, \"rationale\": \"<text>\"}, "
            "with settings (an object of the controls above, absolute values, empty when the frame needs nothing) "
            "and rationale (at most 600 characters, two or three plain sentences naming what you read in the frame "
            "and in the histogram and what each change does about it). Do not identify a person.",
            captured["content"][0]["text"])

    def test_suggest_develop_places_the_tilt_note_and_offers_straighten_only_when_asked(self):
        tagger = OmlxTagger("http://127.0.0.1:8000", "review-model", api_key="k", role="review")
        reply = json.dumps({"settings": {}, "rationale": "Nothing needed."})
        offer = ("straighten_deg rotates the frame to level the horizon and trims the edges to fit. You may set it "
                 "only to the value the horizon detector gives below, or omit it. Set it when the frame shows a real "
                 "horizon or waterline that should be level. Omit it when there is no true horizon in the frame, when "
                 "the measured line is something else (a rail, a shoreline running away from the camera, a slope, a "
                 "roof), or when the tilt is clearly a deliberate choice. Never choose a different angle. Say in the "
                 "rationale whether you levelled the frame or why you left it.")
        measured = ("The horizon detector measured this frame as tilted by 2.9 degrees, anticlockwise. Setting "
                    "straighten_deg to 2.9 would level it.")
        already = "This frame has already been straightened by 2.0 degrees; leave that alone."

        def prompt(**kwargs):
            captured = {}
            with tempfile.TemporaryDirectory() as directory:
                preview = Path(directory) / "p.jpg"
                Image.new("RGB", (8, 8)).save(preview)
                with patch.object(tagger, "_complete", lambda content, timeout, **kw: (captured.update(content=content), reply)[1]):
                    tagger.suggest_develop(preview, settings={}, histogram_note="Median 40.", **kwargs)
            return captured["content"][0]["text"]

        base = prompt()
        self.assertEqual(base, prompt(tilt_note="", offer_straighten=False), "the defaults add nothing, not even a space")
        self.assertEqual(base.replace("darkens it. ", "darkens it. " + offer + " ")
                             .replace("Median 40. Return", "Median 40. " + measured + " Return"),
                         prompt(tilt_note=measured, offer_straighten=True))
        self.assertEqual(base.replace("Median 40. Return", "Median 40. " + already + " Return"),
                         prompt(tilt_note=already))

    def test_suggest_develop_prompt_omits_the_feedback_line_on_the_first_attempt(self):
        tagger = OmlxTagger("http://127.0.0.1:8000", "review-model", api_key="k", role="review")
        captured = {}
        reply = json.dumps({"settings": {}, "rationale": "Nothing needed."})
        with tempfile.TemporaryDirectory() as directory:
            preview = Path(directory) / "p.jpg"
            Image.new("RGB", (8, 8)).save(preview)
            with patch.object(tagger, "_complete", lambda content, timeout, **kw: (captured.update(content=content), reply)[1]):
                tagger.suggest_develop(preview, settings={}, histogram_note="", feedback=None)
        prompt = captured["content"][0]["text"]
        self.assertNotIn("The photographer says", prompt)
        self.assertIn("No control has been changed", prompt)


class StackReviewTests(unittest.TestCase):
    def test_parse_stack_review_keeps_up_to_five_short_notes(self):
        from lumen.omlx import STACK_NOTE_CHARS, StackReviewResponseError, parse_stack_review
        self.assertEqual(["Cloud on the left."], parse_stack_review('{"notes": ["Cloud on the left."]}').notes)
        self.assertEqual([], parse_stack_review('{"notes": []}').notes)
        long = parse_stack_review(json.dumps({"notes": ["word " * 60]})).notes[0]
        self.assertLessEqual(len(long), STACK_NOTE_CHARS)
        self.assertEqual(200, STACK_NOTE_CHARS)
        self.assertTrue(issubclass(StackReviewResponseError, OmlxError))
        for bad in ({"notes": ["a"] * 6}, {"notes": [""]}, {"notes": [" "]}, {"notes": [3]}, {"notes": "a"}, {}, ["a"]):
            with self.subTest(bad=bad), self.assertRaises(StackReviewResponseError) as caught:
                parse_stack_review(json.dumps(bad))
            self.assertEqual(json.dumps(bad), caught.exception.raw_response)

    def test_review_stack_prompt_carries_the_debrief_and_forbids_settings(self):
        tagger = OmlxTagger("http://127.0.0.1:8000", "review-model", api_key="k", role="review")
        captured = {}
        with tempfile.TemporaryDirectory() as directory:
            preview = Path(directory) / "stack.jpg"
            Image.new("RGB", (2048, 1024)).save(preview)
            reply = '{"notes": ["The horizon tilts down to the right."]}'
            with patch.object(tagger, "_complete", lambda content, timeout, **kw: (captured.update(content=content), reply)[1]):
                review = tagger.review_stack(preview, "What happened:\n- No darks.")
        self.assertEqual(["The horizon tilts down to the right."], review.notes)
        self.assertEqual(reply, review.raw_response)
        prompt = captured["content"][0]["text"]
        self.assertIn("What happened:\n- No darks.", prompt)
        self.assertIn("Do not give camera settings", prompt)
        data = base64.b64decode(captured["content"][1]["image_url"]["url"].split(",", 1)[1])
        with Image.open(io.BytesIO(data)) as sent:
            self.assertEqual(omlx.MODEL_IMAGE_LONG_EDGE, max(sent.size))


class _Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class _Opener:
    """Answers every request with the next reply (bytes or an exception) and keeps the requests."""

    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []

    def open(self, request, timeout):
        self.requests.append(request)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return _Reply(reply)


def _chat(text="ok", **extra):
    return json.dumps({"choices": [{"message": {"content": text}}], **extra}).encode()


def _http_error(code, body: bytes):
    return urllib.error.HTTPError("https://api.example.com/v1/chat/completions", code, "Failed", {}, io.BytesIO(body))


class EngineTests(unittest.TestCase):
    """The same client talks to this Mac's oMLX, the photographer's Base server, or an Online service."""

    def setUp(self):
        reset_usage()
        reset_models()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.preview = self.root / "p.jpg"
        Image.new("RGB", (8, 8)).save(self.preview)

    def tearDown(self):
        self._tmp.cleanup()
        reset_usage()
        reset_models()

    def taggers(self):
        return {"mac": OmlxTagger("http://127.0.0.1:8000", "m", api_key="test-key-0123"),
                "base": OmlxTagger("http://pc.local:11434", "m", api_key="test-key-0123", engine="base"),
                "online": OmlxTagger("https://api.example.com/v1beta/compat", "m", api_key="test-key-0123", engine="online")}

    def send(self, tagger, *replies):
        opener = _Opener(*(replies or (_chat(),)))
        with patch("lumen.omlx.urllib.request.build_opener", return_value=opener):
            tagger._complete([{"type": "text", "text": "hi"}], 5)
        return opener.requests

    def test_a_remote_parse_error_names_the_engine_not_omlx(self):
        for engine, start in (("base", "Base "), ("online", "Online "), ("mac", "oMLX ")):
            opener = _Opener(_chat("not json"))
            with self.subTest(engine=engine), patch("lumen.omlx.urllib.request.build_opener", return_value=opener), \
                    self.assertRaises(TagResponseError) as caught:
                self.taggers()[engine].tag(self.preview)
            self.assertTrue(str(caught.exception).startswith(start + "did not return valid JSON"), str(caught.exception))
            self.assertEqual("not json", caught.exception.raw_response)

    def test_url_rules_per_engine(self):
        self.assertEqual(normalise_engine_url("base", " http://pc.local:11434/v1/ "), "http://pc.local:11434/v1")
        self.assertEqual(normalise_engine_url("base", "http://pc.local:11434"), "http://pc.local:11434/v1")
        self.assertEqual(normalise_engine_url("online", "https://api.example.com/v1beta/compat/"), "https://api.example.com/v1beta/compat")
        self.assertEqual(normalise_engine_url("mac", "http://127.0.0.1:8000/v1"), "http://127.0.0.1:8000")
        self.assertEqual(normalise_engine_url("online", "https://api.example.com/v1/chat/completions"), "https://api.example.com/v1")
        for engine, url in (("online", "http://api.example.com"), ("base", "ftp://pc"), ("mac", "http://192.168.1.5:8000"),
                            ("base", "http://user:pw@pc.local"), ("online", "https://api.example.com/v1?key=1"),
                            ("online", "https://api.example.com/v1#x"), ("mac", "http://user@127.0.0.1:8000"),
                            ("base", "http://"), ("base", "pc.local")):
            with self.subTest(engine=engine, url=url), self.assertRaises(ValueError):
                normalise_engine_url(engine, url)
        with self.assertRaisesRegex(ValueError, "^Online needs an https address$"):
            normalise_engine_url("online", "http://api.example.com")
        with self.assertRaisesRegex(ValueError, "^Base needs an http or https address$"):
            normalise_engine_url("base", "ftp://pc")
        with self.assertRaisesRegex(ValueError, "^oMLX endpoint must be a loopback http\\(s\\) URL$"):
            normalise_engine_url("mac", "http://192.168.1.5:8000")

    def test_mac_and_base_ask_for_no_thinking_and_online_is_not_sent_the_field(self):
        payloads = {engine: json.loads(self.send(tagger)[0].data) for engine, tagger in self.taggers().items()}
        self.assertEqual({"enable_thinking": False}, payloads["mac"]["chat_template_kwargs"])
        self.assertEqual({"enable_thinking": False}, payloads["base"]["chat_template_kwargs"])
        self.assertNotIn("chat_template_kwargs", payloads["online"])

    def test_remote_engines_get_room_to_reason_before_the_answer(self):
        payloads = {engine: json.loads(self.send(tagger)[0].data) for engine, tagger in self.taggers().items()}
        self.assertEqual(payloads["mac"]["max_tokens"] + REMOTE_REASONING_TOKENS, payloads["base"]["max_tokens"])
        self.assertEqual(payloads["base"]["max_tokens"], payloads["online"]["max_tokens"])

    def test_an_empty_answer_after_reasoning_says_so(self):
        reply = json.dumps({"choices": [{"finish_reason": "length", "message": {"content": "", "reasoning_content": "Let me examine each frame"}}]}).encode()
        with patch("lumen.omlx.urllib.request.build_opener", return_value=_Opener(reply)), \
                self.assertRaisesRegex(OmlxError, "^Base model spent its token budget on reasoning") as caught:
            self.taggers()["base"].tag(self.preview)
        self.assertNotIn("Let me examine", str(caught.exception))

    def test_online_omits_temperature_and_base_and_mac_send_zero(self):
        payloads = {engine: json.loads(self.send(tagger)[0].data) for engine, tagger in self.taggers().items()}
        self.assertEqual(0, payloads["mac"]["temperature"])
        self.assertEqual(0, payloads["base"]["temperature"])
        self.assertNotIn("temperature", payloads["online"])

    def test_requests_go_to_the_api_base(self):
        urls = {engine: self.send(tagger)[0].full_url for engine, tagger in self.taggers().items()}
        self.assertEqual("http://127.0.0.1:8000/v1/chat/completions", urls["mac"])
        self.assertEqual("http://pc.local:11434/v1/chat/completions", urls["base"])
        self.assertEqual("https://api.example.com/v1beta/compat/chat/completions", urls["online"])

    def test_remote_never_reads_the_local_omlx_key(self):
        with patch.object(OmlxTagger, "_local_api_key", return_value="local") as local:
            base = OmlxTagger("http://pc.local:11434", "m", engine="base")
            online = OmlxTagger("https://api.example.com", "m", engine="online")
            mac = OmlxTagger("http://127.0.0.1:8000", "m")
        self.assertIsNone(base.api_key)
        self.assertIsNone(online.api_key)
        self.assertEqual("local", mac.api_key)
        self.assertEqual(1, local.call_count)
        self.assertIsNone(self.send(base)[0].get_header("Authorization"))

    def test_remote_image_is_always_reencoded_without_metadata(self):
        path = self.root / "exif.jpg"
        exif = Image.Exif()
        exif[0xA431] = "SERIAL-12345"  # BodySerialNumber
        exif[0x8825] = {1: "N", 2: (51.0, 30.0, 0.0)}  # GPS IFD
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        xmp = b'<x:xmpmeta xmlns:x="adobe:ns:meta/">PLANTED-XMP-PACKET</x:xmpmeta>'
        Image.new("RGB", (640, 480), "gray").save(path, exif=exif, comment=b"PLANTED-COMMENT", xmp=xmp, icc_profile=icc)
        with Image.open(path) as original:
            self.assertEqual(2, len(original.getexif()))
            self.assertTrue({"comment", "xmp", "icc_profile"} <= set(original.info))
        tagger = self.taggers()
        for engine in ("base", "online"):
            url = tagger[engine]._image_content(path)["image_url"]["url"]
            self.assertTrue(url.startswith("data:image/jpeg;base64,"))
            raw = base64.b64decode(url.split(",", 1)[1])
            self.assertFalse(b"PLANTED-COMMENT" in raw, b"PLANTED-COMMENT")
            self.assertFalse(b"PLANTED-XMP" in raw, b"PLANTED-XMP")
            self.assertFalse(b"adobe:ns:meta" in raw, b"adobe:ns:meta")
            sent = Image.open(io.BytesIO(raw))
            self.assertEqual(len(sent.getexif()), 0)
            for key in ("comment", "xmp", "icc_profile", "exif"):
                self.assertNotIn(key, sent.info)
            self.assertEqual((640, 480), sent.size)
        self.assertEqual(tagger["mac"]._image_content(path)["image_url"]["url"].split(",", 1)[1],
                         base64.b64encode(path.read_bytes()).decode())

    def test_remote_png_is_sent_as_jpeg(self):
        path = self.root / "small.png"
        Image.new("RGBA", (60, 40)).save(path)
        url = self.taggers()["base"]._image_content(path)["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/jpeg;base64,"))

    def test_remote_burst_frames_are_capped(self):
        path = self.root / "big.jpg"
        Image.new("RGB", (3000, 2000)).save(path)
        tagger = self.taggers()
        def size(engine):
            url = tagger[engine]._image_content(path, long_edge=0)["image_url"]["url"]
            return max(Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).size)
        self.assertEqual(1024, size("online"))
        self.assertEqual(1024, size("base"))
        self.assertEqual(3000, size("mac"))

    def test_remote_error_carries_the_services_reason_clipped(self):
        body = json.dumps({"error": {"message": "Incorrect API key provided.\x1b[31m " + "x" * 500}}).encode()
        opener = _Opener(_http_error(401, body))
        with patch("lumen.omlx.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(OmlxError) as caught:
                self.taggers()["online"].caption(self.preview)
        message = str(caught.exception)
        self.assertTrue(message.startswith("Online request failed:"))
        self.assertIn("Incorrect API key provided.", message)
        self.assertNotIn("\x1b", message)
        self.assertLess(len(message), 320)

    def test_remote_error_reason_may_be_a_plain_string(self):
        opener = _Opener(_http_error(400, json.dumps({"error": "model not found"}).encode()))
        with patch("lumen.omlx.urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(OmlxError, "^Base request failed: .*model not found$"):
                self.taggers()["base"].caption(self.preview)

    def test_remote_html_error_body_is_not_passed_on(self):
        for body in (b"<html><body>Bad gateway</body></html>", b"{\"error\": ", b"[1]"):
            opener = _Opener(_http_error(502, body))
            with self.subTest(body=body), patch("lumen.omlx.urllib.request.build_opener", return_value=opener):
                with self.assertRaises(OmlxError) as caught:
                    self.taggers()["online"].caption(self.preview)
                self.assertEqual("Online request failed: HTTP Error 502: Failed", str(caught.exception))

    def test_unreadable_error_body_falls_back_to_the_status_line(self):
        class Broken:
            def read(self, *_):
                raise OSError("connection reset")

            def close(self):
                pass

        error = urllib.error.HTTPError("https://api.example.com", 500, "Server Error", {}, Broken())
        with patch("lumen.omlx.urllib.request.build_opener", return_value=_Opener(error)):
            with self.assertRaises(OmlxError) as caught:
                self.taggers()["online"].caption(self.preview)
        self.assertEqual("Online request failed: HTTP Error 500: Server Error", str(caught.exception))

    def test_a_key_echoed_in_a_reason_is_masked(self):
        body = json.dumps({"error": {"message": "Incorrect API key provided: sk-SECRET-123."}}).encode()
        tagger = OmlxTagger("https://api.example.com/v1", "m", api_key="sk-SECRET-123", engine="online")
        with patch("lumen.omlx.urllib.request.build_opener", return_value=_Opener(_http_error(401, body))):
            with self.assertRaises(OmlxError) as caught:
                tagger.caption(self.preview)
        self.assertNotIn("sk-SECRET-123", str(caught.exception))
        self.assertIn("Incorrect API key provided: <key>.", str(caught.exception))
        with patch("lumen.omlx.urllib.request.build_opener", return_value=_Opener(_http_error(401, body))):
            with self.assertRaises(OmlxError) as caught:
                list_models("online", "https://api.example.com/v1", "sk-SECRET-123")
        self.assertNotIn("sk-SECRET-123", str(caught.exception))
        self.assertIn("<key>", str(caught.exception))
        failure = urllib.error.URLError("refused for sk-SECRET-123")
        with patch("lumen.omlx.urllib.request.build_opener", return_value=_Opener(failure)):
            with self.assertRaises(OmlxError) as caught:
                tagger.caption(self.preview)
        self.assertNotIn("sk-SECRET-123", str(caught.exception))

    def test_network_failures_while_reading_become_engine_errors(self):
        class Unreadable(_Reply):
            def read(self, *_):
                raise ConnectionResetError(54, "Connection reset by peer")

        class Opener(_Opener):
            def open(self, request, timeout):
                reply = self.replies[0]
                if isinstance(reply, type):
                    return reply(b"")
                return super().open(request, timeout)

        failures = (("reset", Opener(Unreadable)), ("incomplete", _Opener(http.client.IncompleteRead(b""))),
                    ("status line", _Opener(http.client.BadStatusLine("x"))), ("not utf-8", _Opener(b"\xff\xfe\xfa")))
        for name, opener in failures:
            for engine, prefix in (("online", "Online "), ("mac", "Local oMLX ")):
                with self.subTest(failure=name, engine=engine), \
                        patch("lumen.omlx.urllib.request.build_opener", return_value=opener):
                    with self.assertRaisesRegex(OmlxError, "^" + prefix + "request failed"):
                        self.taggers()[engine]._complete([{"type": "text", "text": "hi"}], 5)
                    with self.assertRaisesRegex(OmlxError, "^" + prefix + "model list request failed"):
                        list_models(engine, self.taggers()[engine].base_url, "k")
        with patch("lumen.omlx.list_models", return_value=["a"]):
            refresh_models("base", "http://pc.local:11434", "k")
        with patch("lumen.omlx.urllib.request.build_opener", return_value=_Opener(http.client.IncompleteRead(b""))):
            with self.assertRaises(OmlxError):
                refresh_models("base", "http://pc.local:11434", "k")
        self.assertIsNone(held_models("base"))

    def test_remote_chat_refuses_redirects(self):
        redirect = urllib.error.HTTPError("https://elsewhere.example", 302, "Found", {}, None)
        with patch("lumen.omlx.urllib.request.build_opener", return_value=_Opener(redirect)) as build:
            with self.assertRaisesRegex(OmlxError, "^Online request failed: HTTP Error 302"):
                self.taggers()["online"]._complete([{"type": "text", "text": "hi"}], 5)
        self.assertIsInstance(build.call_args.args[0], NoRedirectHandler)

    def test_mac_error_text_is_unchanged(self):
        opener = _Opener(_http_error(404, json.dumps({"error": {"message": "nope"}}).encode()))
        with patch("lumen.omlx.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(OmlxError) as caught:
                self.taggers()["mac"].caption(self.preview)
        self.assertEqual("Local oMLX request failed: HTTP Error 404: Failed", str(caught.exception))

    def test_usage_is_tallied_and_missing_usage_is_unreported(self):
        online = self.taggers()["online"]
        self.send(online, _chat(usage={"prompt_tokens": 1200, "completion_tokens": 80}))
        self.send(online, _chat())
        self.assertEqual(usage_snapshot()["online"], {"prompt_tokens": 1200, "completion_tokens": 80, "calls": 2, "unreported": 1})
        self.assertNotIn("base", usage_snapshot())

    def test_malformed_usage_never_raises_and_counts_as_unreported(self):
        for usage in (None, [], "lots", {"prompt_tokens": -1, "completion_tokens": 2},
                      {"prompt_tokens": True, "completion_tokens": 2}, {"prompt_tokens": 1.5, "completion_tokens": 2},
                      {"prompt_tokens": 3}):
            omlx.record_usage("base", usage)
        self.assertEqual({"prompt_tokens": 0, "completion_tokens": 0, "calls": 7, "unreported": 7}, usage_snapshot()["base"])
        usage_snapshot()["base"]["calls"] = 99
        self.assertEqual(7, usage_snapshot()["base"]["calls"], "the snapshot is a copy")

    def test_list_models_reads_ids_and_refuses_redirects(self):
        opener = _Opener(json.dumps({"data": [{"id": "b"}, {"id": "a"}, {"object": "x"}]}).encode())
        with patch("lumen.omlx.urllib.request.build_opener", return_value=opener) as build:
            models = list_models("online", "https://api.example.com/v1beta/compat", "secret")
        self.assertEqual(["a", "b"], models)
        self.assertIsInstance(build.call_args.args[0], NoRedirectHandler)
        request = opener.requests[0]
        self.assertEqual("https://api.example.com/v1beta/compat/models", request.full_url)
        self.assertEqual("Bearer secret", request.get_header("Authorization"))
        redirect = urllib.error.HTTPError("https://api.example.com", 302, "Found", {}, None)
        with patch("lumen.omlx.urllib.request.build_opener", return_value=_Opener(redirect)):
            with self.assertRaisesRegex(OmlxError, "^Online "):
                list_models("online", "https://api.example.com", "secret")

    def test_list_models_sends_a_presets_listing_headers_to_that_host_only(self):
        from lumen import preferences
        preset = next(p for p in preferences.SERVICE_PRESETS if p.get("listing_headers"))
        extra = {**preset["listing_headers"], preset["listing_key_header"]: "secret"}
        opener = _Opener(json.dumps({"data": [{"id": "a"}]}).encode())
        with patch("lumen.omlx.urllib.request.build_opener", return_value=opener):
            self.assertEqual(["a"], list_models("online", preset["url"], "secret"))
        request = opener.requests[0]
        self.assertEqual("Bearer secret", request.get_header("Authorization"))
        for name, value in extra.items():
            self.assertEqual(value, request.get_header(name.capitalize()), name)
        others = [("online", "https://api.example.com/v1"), ("base", "http://pc.local:11434/v1"),
                  ("mac", "http://127.0.0.1:8000/v1"), ("base", preset["url"])]
        for engine, url in others:
            opener = _Opener(json.dumps({"data": []}).encode())
            with self.subTest(engine=engine, url=url), patch("lumen.omlx.urllib.request.build_opener", return_value=opener):
                list_models(engine, url, "secret")
                sent = {name.lower() for name, _ in opener.requests[0].header_items()}
                self.assertFalse(sent & {name.lower() for name in extra}, sent)
        with patch("lumen.omlx.urllib.request.build_opener", return_value=_Opener(json.dumps({"data": []}).encode())) as build:
            list_models("online", preset["url"], None)
            sent = {name.lower() for name, _ in build.return_value.requests[0].header_items()}
            self.assertNotIn(preset["listing_key_header"], sent, "no key, no key header")

    def test_refresh_models_holds_the_last_listing_and_a_failure_clears_it(self):
        self.assertIsNone(held_models("base"))
        with patch("lumen.omlx.list_models", return_value=["a", "b"]) as listing:
            self.assertEqual(["a", "b"], refresh_models("base", "http://pc.local:11434", "k"))
        listing.assert_called_once_with("base", "http://pc.local:11434", "k", 10)
        self.assertEqual(["a", "b"], held_models("base"))
        self.assertIsNone(held_models("online"))  # held per engine
        with patch("lumen.omlx.list_models", side_effect=OmlxError("Base model list request failed: timed out")):
            with self.assertRaisesRegex(OmlxError, "timed out"):
                refresh_models("base", "http://pc.local:11434", "k")
        self.assertIsNone(held_models("base"))
        with patch("lumen.omlx.list_models", return_value=[]):
            refresh_models("base", "http://pc.local:11434", None)
        self.assertEqual([], held_models("base"))  # reachable, no listing: not the same as never listed
        reset_models()
        self.assertIsNone(held_models("base"))

    def test_list_models_without_a_listing_is_empty_and_other_failures_raise(self):
        with patch("lumen.omlx.urllib.request.build_opener", return_value=_Opener(_http_error(404, b"not here"))):
            self.assertEqual([], list_models("base", "http://pc.local:11434", None))
        for code in (400, 405):  # a service whose listing wants its own headers, or has none: the typed model name stands
            with self.subTest(code=code), patch("lumen.omlx.urllib.request.build_opener", return_value=_Opener(_http_error(code, b'{"error": {"message": "a header is required"}}'))):
                self.assertEqual([], list_models("online", "https://api.example.com/v1", "sk-test-key"))
        body = json.dumps({"error": {"message": "bad key"}}).encode()
        with patch("lumen.omlx.urllib.request.build_opener", return_value=_Opener(_http_error(401, body))):
            with self.assertRaisesRegex(OmlxError, "^Base .*bad key$"):
                list_models("base", "http://pc.local:11434", None)
        with patch("lumen.omlx.urllib.request.build_opener", return_value=_Opener(b"[1, 2]")):
            with self.assertRaisesRegex(OmlxError, "^Base "):
                list_models("base", "http://pc.local:11434", None)
        with patch("lumen.omlx.urllib.request.build_opener", return_value=_Opener(b'{"data": []}')) as build:
            opener = build.return_value
            list_models("mac", "http://127.0.0.1:8000", "k")
        self.assertEqual("http://127.0.0.1:8000/v1/models", opener.requests[0].full_url)


if __name__ == "__main__":
    unittest.main()
