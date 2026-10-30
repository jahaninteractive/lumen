import contextlib
import io
import json
import socket
import struct
import subprocess
from io import BytesIO
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from html.parser import HTMLParser
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageCms

from lumen.config import Settings
from lumen.domain import DEVELOP_FLAGS, DEVELOP_RANGES, DevelopSettings
from lumen.library import Library
from lumen.omlx import DevelopAdvice
from lumen.previews import PreviewError
from lumen.rawdevelop import DevelopRender
from lumen.omlx import CompositionReview, OmlxError
from lumen.pipelines import DarktablePipeline
from lumen.web import make_server

try:
    from tests.test_astro import FAKE_SIRIL, SIRIL
    from tests.test_catalog import seed_filter_photos
    from tests.test_previews import write_fake_darktable
except ModuleNotFoundError:  # discovered with -s tests
    from test_astro import FAKE_SIRIL, SIRIL
    from test_catalog import seed_filter_photos
    from test_previews import write_fake_darktable


REVIEW_LOADED = {"version": None, "default_model": None, "loaded_models": ["Qwen3.8-27B-4bit"]}


class LandmarkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.labels = 0
        self.nodes = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag == "label":
            self.labels += 1
        node = {"tag": tag, "attrs": dict(attrs), "parents": list(self.stack), "text": ""}
        self.nodes.append(node)
        if tag not in {"input", "img", "meta", "link", "br", "hr"}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag:
                self.stack = self.stack[:index]
                break

    def handle_data(self, data):
        for node in self.stack:
            node["text"] += data


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        settings = Settings(library_dir=self.root / "library")
        source = self.root / "photo.jpg"
        Image.new("RGB", (120, 90), (80, 100, 120)).save(source)
        with Library(settings) as library:
            library.import_path(source)
        self.server = make_server(settings, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def test_a_cancelled_request_does_not_print_a_stack_trace(self):
        """Moving a slider aborts the render in flight, which leaves a dead socket mid-reply.

        That is routine, not a fault. It used to reach socketserver as an unhandled
        BrokenPipeError and print two stack traces per cancelled render, which buried real
        errors in the log.
        """
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            client = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=3)
            # app.js is far larger than the socket buffer, so closing without reading a byte
            # guarantees the server's write fails rather than racing it.
            client.sendall(b"GET /app.js HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            # SO_LINGER with a zero timeout makes close() send RST rather than FIN, so the
            # server's write fails outright instead of filling a buffer nobody drains.
            client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            client.close()
            # The next request proves the server survived, and gives the failing thread time
            # to have printed anything it was going to print.
            self.assertEqual(200, self.get_json("/api/status")[0])
        self.assertNotIn("Traceback", captured.getvalue())
        self.assertNotIn("BrokenPipe", captured.getvalue())

    def post_json(self, path, payload, headers=None, raw=None):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode() if raw is None else raw,
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        try:
            response = urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            self.assertIn("application/json", response.headers.get("Content-Type", ""), f"{path} must return JSON (HTTP {response.code})")
            return response.code, json.load(response)

    def render(self, command, **kwargs):
        if command[1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "darktable test", "")
        if command[0].endswith("exiftool"):
            return subprocess.CompletedProcess(command, 0, "", "")
        Image.new("RGB", (40, 30), "red").save(
            command[-8], format="TIFF",
            icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes(),
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    def get_json(self, path, headers=None):
        try:
            response = urllib.request.urlopen(urllib.request.Request(self.base + path, headers=headers or {}), timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            self.assertIn("application/json", response.headers.get("Content-Type", ""))
            return response.code, json.load(response)

    def wait_job(self, job_id, timeout=10):
        import time
        deadline = time.monotonic() + timeout
        while True:
            code, payload = self.get_json(f"/api/jobs/{job_id}")
            self.assertEqual(200, code, payload)
            if payload["status"] not in ("queued", "running"):
                return payload
            self.assertLess(time.monotonic(), deadline, f"job stayed {payload['status']}")
            time.sleep(0.05)

    def test_import_include_cleared_must_be_a_bool_and_reaches_the_job(self):
        Image.new("RGB", (60, 40), (90, 20, 20)).save(self.root / "DSC0009.jpg")
        code, body = self.post_json("/api/import", {"path": str(self.root / "DSC0009.jpg"), "include_cleared": "yes"})
        self.assertEqual((400, True), (code, "include_cleared" in body["error"]))
        real = Library.start_import_job
        with patch.object(Library, "start_import_job", autospec=True, side_effect=real) as start:
            code, started = self.post_json("/api/import", {"path": str(self.root / "DSC0009.jpg"), "include_cleared": True})
            self.wait_job(started["job_id"])
            self.post_json("/api/import", {"path": str(self.root / "DSC0009.jpg")})
        self.assertEqual(202, code)
        self.assertEqual([True, False], [call.kwargs["include_cleared"] for call in start.call_args_list])

    def test_import_returns_job_and_jobs_routes_never_expose_paths_or_fingerprints(self):
        folder = self.root / "Card Folder"
        folder.mkdir()
        for index in range(2):
            Image.new("RGB", (60, 40), (index * 90, 20, 20)).save(folder / f"DSC000{index}.jpg")
        (self.root / "notes.txt").write_text("not a photo")
        for payload in ({}, {"path": ""}, {"path": 5}, {"path": str(folder), "extra": 1},
                        {"path": str(self.root / "missing")}, {"path": str(self.root / "notes.txt")}):
            with self.subTest(payload=payload):
                code, body = self.post_json("/api/import", payload)
                self.assertEqual(400, code)
                self.assertNotIn(str(self.root), json.dumps(body))
        code, started = self.post_json("/api/import", {"path": str(folder)})
        self.assertEqual(202, code)
        self.assertEqual(("import", None), (started["kind"], started["result"]))
        self.assertIn(started["status"], ("queued", "running", "succeeded"))
        finished = self.wait_job(started["job_id"])
        self.assertEqual("succeeded", finished["status"])
        self.assertEqual({"discovered": 2, "imported": 2, "updated": 0, "skipped": 0, "failed": 0, "previously_cleared": 0, "restored": 0}, finished["result"])
        self.assertEqual((2, 2, "DSC0001.jpg"), (finished["processed"], finished["total"], finished["label"]))
        code, listing = self.get_json("/api/jobs")
        self.assertEqual(200, code)
        self.assertEqual(finished, listing["jobs"][0])
        self.assertEqual((404, True), (self.get_json("/api/jobs/999")[0], "error" in self.get_json("/api/jobs/999")[1]))
        with Library(Settings(library_dir=self.root / "library")) as library:
            secrets = [str(self.root), "fingerprint", "manifest_path", "log_path", "result_path"]
            secrets += [photo.fingerprint for photo in library.catalog.list_photos()]
        for body in (started, finished, listing):
            text = json.dumps(body)
            for secret in secrets:
                self.assertNotIn(secret, text)
        self.assertEqual(3, json.load(urllib.request.urlopen(self.base + "/api/groups", timeout=3))["photo_count"])

    def test_ingest_route_validates_fields_and_never_returns_paths_or_fingerprints(self):
        card = self.root / "CARD" / "DCIM"
        card.mkdir(parents=True)
        for index in range(2):
            Image.new("RGB", (60, 40), (index * 90, 20, 20)).save(card / f"DSC000{index}.JPG")
        primary, mirror = self.root / "primary", self.root / "mirror"
        primary.mkdir()
        mirror.mkdir()
        valid = {"card": str(self.root / "CARD"), "primary": str(primary), "mirror": str(mirror)}
        self.assertEqual(400, self.post_json("/api/ingest", valid)[0], "one temp volume: destinations share the card's volume")
        separate_volumes = patch("lumen.ingest._device", side_effect=str)
        separate_volumes.start()
        self.addCleanup(separate_volumes.stop)
        for payload in ({}, {"card": valid["card"]}, {"card": "", "primary": str(primary)}, {"card": 5, "primary": str(primary)},
                        {**valid, "extra": 1}, {**valid, "mirror": 5}, {**valid, "mirror": ""}, {**valid, "card": str(self.root / "missing")},
                        {**valid, "primary": str(self.root / "library")}, {**valid, "mirror": str(primary)}):
            with self.subTest(payload=payload):
                code, body = self.post_json("/api/ingest", payload)
                self.assertEqual(400, code)
                self.assertNotIn(str(self.root), json.dumps(body))
        self.assertEqual(403, self.post_json("/api/ingest", valid, headers={"Origin": "https://attacker.example"})[0])
        code, started = self.post_json("/api/ingest", valid)
        self.assertEqual((202, "ingest"), (code, started["kind"]))
        finished = self.wait_job(started["job_id"])
        self.assertEqual("succeeded", finished["status"])
        self.assertEqual((True, 2, "manifest.json"), (finished["ingest"]["card_verified_everywhere"], finished["ingest"]["verified"], finished["ingest"]["manifest"]))
        code, status = self.get_json("/api/status")
        self.assertEqual(str((self.root / "library" / "outputs" / "ingest").resolve()), status["ingest_output_dir"])
        _, groups = self.get_json("/api/groups")
        photo_ids = [photo["id"] for group in groups["groups"] for photo in group["photos"]]
        self.assertEqual(3, len(photo_ids))
        photos = [self.get_json(f"/api/photos/{photo_id}")[1] for photo_id in photo_ids]
        with Library(Settings(library_dir=self.root / "library")) as library:
            secrets = [str(self.root), str(self.root.resolve()), "mirror_path", "fingerprint", "manifest_path"]
            secrets += [photo.fingerprint for photo in library.catalog.list_photos()]
            self.assertEqual(2, sum(photo.mirror_path is not None for photo in library.catalog.list_photos()))
        for body in (started, finished, groups, *photos, self.get_json("/api/jobs")[1]):
            text = json.dumps(body)
            for secret in secrets:
                self.assertNotIn(secret, text)

    def test_ingest_log_tail_never_shows_destination_paths_after_failures(self):
        card = self.root / "CARD" / "DCIM"
        card.mkdir(parents=True)
        for index in range(2):
            Image.new("RGB", (60, 40), (index * 90, 20, 20)).save(card / f"DSC000{index}.JPG")
        primary, mirror = self.root / "primary drive", self.root / "mirror drive"
        primary.mkdir()
        mirror.mkdir()
        from lumen import ingest
        real_copy = ingest.copy_verified

        def mirror_full(source, destination, **kwargs):
            if "mirror drive" in str(destination):
                raise OSError(28, "No space left on device", str(destination))
            return real_copy(source, destination, **kwargs)

        def import_fails(library, source, *args, **kwargs):
            raise OSError(5, f"Input/output error: '{source}'")

        with patch("lumen.ingest._device", side_effect=str), patch("lumen.ingest.copy_verified", side_effect=mirror_full), \
                patch.object(Library, "_import_one", import_fails):
            code, started = self.post_json("/api/ingest", {"card": str(self.root / "CARD"), "primary": str(primary), "mirror": str(mirror)})
            self.assertEqual(202, code)
            finished = self.wait_job(started["job_id"])
        self.assertEqual("succeeded", finished["status"])
        self.assertFalse(finished["ingest"]["card_verified_everywhere"])
        tail = "\n".join(finished["log_tail"])
        self.assertIn("No space left on device", tail)
        self.assertIn("[Errno 5]", tail)
        for secret in (str(self.root), str(self.root.resolve()), "primary drive", "mirror drive", "/2026-"):
            self.assertNotIn(secret, tail)

    def test_ingest_panel_has_labeled_pickers_and_honest_format_copy(self):
        html = urllib.request.urlopen(self.base + "/", timeout=3).read().decode()
        parser = LandmarkParser()
        parser.feed(html)
        by_id = {node["attrs"]["id"]: node for node in parser.nodes if "id" in node["attrs"]}
        panel = by_id["ingest-panel"]
        self.assertIn(by_id["add-panel"], panel["parents"], "Ingest is one source in the Add photos tab")
        for kind in ("card", "primary", "mirror"):
            browse = by_id[f"browse-ingest-{kind}"]
            self.assertEqual(("button", "button"), (browse["tag"], browse["attrs"].get("type")))
            self.assertIn(panel, browse["parents"])
            path = by_id[f"ingest-{kind}-path"]
            self.assertEqual("path", path["attrs"].get("class"))
            self.assertTrue(by_id[path["attrs"]["aria-labelledby"]]["text"].strip())
        self.assertEqual(("button", "button"), (by_id["start-ingest"]["tag"], by_id["start-ingest"]["attrs"].get("type")))
        self.assertEqual("Start ingest", by_id["start-ingest"]["text"].strip())
        self.assertIn("disabled", by_id["start-ingest"]["attrs"])
        self.assertEqual("polite", by_id["ingest-status"]["attrs"].get("aria-live"))
        self.assertTrue(by_id[by_id["ingest-manifest"]["attrs"]["aria-labelledby"]]["text"].strip())
        javascript = (Path(__file__).resolve().parents[1] / "src" / "lumen" / "static" / "app.js").read_text()
        self.assertIn("Every file verified on every destination.", javascript)
        self.assertIn("Do not format the card: ", javascript)
        self.assertNotRegex(javascript.lower(), r"safe to (format|erase|wipe)|card is safe")
        self.assertNotRegex(javascript, r"ingest[^\n]*\.innerHTML")
        fold = next(node for node in parser.nodes if node["tag"] == "details" and node["parents"][-1] is panel)
        self.assertEqual("how", fold["attrs"].get("class"), "the explanation folds under How this works")
        copy = " ".join(fold["text"].split())
        for text in ("Copy.", "Verify.", "Import.", "never writes to or erases it", "Every file verified on every destination."):
            self.assertIn(text, copy)

    def test_javascript_ingest_tab_starts_polls_and_reports_verification(self):
        self.run_javascript_cleanup_regression("ingest")

    def test_tag_all_conflicts_with_active_job_and_cancel_is_same_origin_only(self):
        release = threading.Event()
        self.addCleanup(release.set)
        original = Library._import_one

        def held(library, source, *args, **kwargs):
            release.wait(5)
            return original(library, source, *args, **kwargs)

        class FakeTagger:
            model = "test-vlm"

            def __init__(self, *args, **kwargs):
                pass

            def tag(self, preview, *, vocabulary=None, place=None):
                from lumen.omlx import TagResult
                return TagResult("A frame", ["frame"], "room", [], 0.9, "{}")

        changed = self.root / "photo.jpg"
        Image.new("RGB", (120, 90), (1, 2, 3)).save(changed)
        live = {"version": None, "default_model": None, "loaded_models": ["Qwen3-VL-8B-Instruct-4bit"]}
        with patch.object(Library, "_import_one", held), patch("lumen.library.OmlxTagger", FakeTagger), patch(
                "lumen.library.get_server_status", return_value=live):
            code, started = self.post_json("/api/import", {"path": str(changed)})
            self.assertEqual(202, code)
            code, body = self.post_json("/api/photos/tag-all", {})
            self.assertEqual((409, {"error": "another heavy job is already active"}), (code, body))
            code, body = self.post_json("/api/import", {"path": str(changed)})
            self.assertEqual(409, code)
            for payload in ({"retag": "yes"}, {"retag": 1}, {"all": True}):
                with self.subTest(payload=payload):
                    self.assertEqual(400, self.post_json("/api/photos/tag-all", payload)[0])
            cancel = f"/api/jobs/{started['job_id']}/cancel"
            for header in ({"Origin": "https://attacker.example"}, {"Host": f"attacker.example:{self.server.server_port}"}):
                with self.subTest(header=header):
                    self.assertEqual(403, self.post_json(cancel, {}, headers=header)[0])
            self.assertEqual(400, self.post_json(cancel, {"why": "x"})[0])
            self.assertIn(self.get_json(f"/api/jobs/{started['job_id']}")[1]["status"], ("queued", "running"))
            code, cancelled = self.post_json(cancel, {})
            self.assertEqual((200, "cancelled"), (code, cancelled["status"]))
            release.set()
            self.assertTrue(self.server.job_runner().wait(started["job_id"], 5), "the worker finishes its current item")
            self.assertEqual("cancelled", self.wait_job(started["job_id"])["status"])
            self.assertEqual(400, self.post_json(cancel, {})[0])
            code, tagging = self.post_json("/api/photos/tag-all", {"retag": True})
            self.assertEqual((202, "tag"), (code, tagging["kind"]))
            finished = self.wait_job(tagging["job_id"])
        self.assertEqual(("succeeded", {"tagged": 1, "skipped": 0, "failed": 0}), (finished["status"], finished["result"]))
        self.assertNotIn(str(self.root), json.dumps(finished))

    def test_job_strip_has_progress_status_and_cancel_controls(self):
        html = urllib.request.urlopen(self.base + "/", timeout=3).read().decode()
        parser = LandmarkParser()
        parser.feed(html)
        by_id = {node["attrs"]["id"]: node for node in parser.nodes if "id" in node["attrs"]}
        strip = by_id["job-strip"]
        self.assertTrue(strip["attrs"].get("aria-label"))
        self.assertEqual("progress", by_id["job-progress"]["tag"])
        self.assertEqual("job-label", by_id["job-progress"]["attrs"].get("aria-labelledby"))
        self.assertEqual(("status", "polite"), (by_id["job-status"]["attrs"].get("role"), by_id["job-status"]["attrs"].get("aria-live")))
        self.assertEqual(("button", "button"), (by_id["cancel-job"]["tag"], by_id["cancel-job"]["attrs"].get("type")))
        for name in ("job-label", "job-progress", "job-status", "cancel-job"):
            self.assertIn(strip, by_id[name]["parents"])
            self.assertNotIn(by_id["main-content"], by_id[name]["parents"])
        self.assertEqual(("button", "button", "Tag every untagged frame"), (by_id["tag-all"]["tag"], by_id["tag-all"]["attrs"].get("type"), by_id["tag-all"]["text"].strip()))
        self.assertEqual(("input", "checkbox"), (by_id["retag-all"]["tag"], by_id["retag-all"]["attrs"].get("type")))
        retag_label = next(node for node in parser.nodes if node["tag"] == "label" and node["attrs"].get("for") == "retag-all")
        self.assertEqual("Re-tag already tagged", retag_label["text"].strip())
        styles = (Path(__file__).resolve().parents[1] / "src" / "lumen" / "static" / "styles.css").read_text()
        self.assertRegex(styles, r"#cancel-job\s*\{[^}]*min-width:\s*44px")
        javascript = (Path(__file__).resolve().parents[1] / "src" / "lumen" / "static" / "app.js").read_text()
        for word in ("finished", "failed", "cancelled", "interrupted"):
            self.assertIn(word, javascript)
        self.assertNotRegex(javascript, r"job[^\n]*\.innerHTML")

    def test_footer_strip_holds_model_selects_availability_activity_and_jobs(self):
        html = urllib.request.urlopen(self.base + "/", timeout=3).read().decode()
        parser = LandmarkParser()
        parser.feed(html)
        by_id = {node["attrs"]["id"]: node for node in parser.nodes if "id" in node["attrs"]}
        footer = next(node for node in parser.nodes if node["tag"] == "footer")
        header = next(node for node in parser.nodes if node["tag"] == "header")
        for name in ("role-model-pair", "ai-availability", "ai-status", "job-strip"):
            self.assertIn(footer, by_id[name]["parents"], name)
            self.assertNotIn(header, by_id[name]["parents"], name)
        order = [node["attrs"]["id"] for node in parser.nodes
                 if node["attrs"].get("id") in {"role-model-pair", "ai-availability", "ai-status", "job-strip"}]
        self.assertEqual(["role-model-pair", "ai-availability", "ai-status", "job-strip"], order)
        self.assertIn("hidden", by_id["job-strip"]["attrs"])
        self.assertEqual("quiet", by_id["ai-availability"]["attrs"].get("class"))

    def test_javascript_header_reports_health_without_model_names(self):
        self.run_javascript_cleanup_regression("status-health")

    def test_javascript_job_strip_polls_import_disables_actions_and_refreshes_once(self):
        self.run_javascript_cleanup_regression("job-strip")

    def test_export_current_and_keepers_return_artifacts_without_overwrite(self):
        profile = self.root / "profile.xmp"
        profile.write_text("<xmp/>")
        pipeline = DarktablePipeline("darktable-cli", self.root / "library" / "outputs")
        with patch("lumen.library.PipelineRegistry.darktable", return_value=pipeline), patch(
            "lumen.pipelines.subprocess.run", side_effect=self.render
        ):
            code, first = self.post_json("/api/export", {"scope": "current", "photo_id": 1, "profile": str(profile)})
            self.assertEqual(200, code, first)
            first_bytes = Path(first["results"][0]["output"]).read_bytes()
            code, second = self.post_json("/api/export", {"scope": "keepers", "profile": None})
            self.assertEqual((202, "export"), (code, second["kind"]), second)
            finished = self.wait_job(second["job_id"])
        self.assertEqual(("succeeded", {"exported": 1, "failed": 0, "skipped": 0}), (finished["status"], finished["result"]))
        self.assertNotIn(str(self.root), json.dumps(finished))
        one = first["results"][0]
        self.assertEqual(1, one["photo_id"])
        self.assertTrue(one["success"])
        self.assertEqual("photo-lumen.jpg", Path(one["output"]).name)
        with Library(Settings(library_dir=self.root / "library")) as library:
            self.assertEqual(["photo-lumen.jpg", "photo-lumen-1.jpg"], [Path(artifact.path).name for artifact in library.catalog.list_artifacts()])
        self.assertEqual(first_bytes, Path(one["output"]).read_bytes())
        self.assertEqual(one["output"], one["artifact"]["path"])
        self.assertEqual(str(profile.resolve()), one["artifact"]["settings"]["profile"])
        datetime.fromisoformat(one["artifact"]["created_at"])

    def test_export_filtered_renders_the_frames_the_contact_sheet_filters_show(self):
        pipeline = DarktablePipeline("darktable-cli", self.root / "library" / "outputs")
        with patch("lumen.library.PipelineRegistry.darktable", return_value=pipeline), patch(
            "lumen.pipelines.subprocess.run", side_effect=self.render
        ):
            code, job = self.post_json("/api/export", {"scope": "filtered", "profile": None, "preset": "web", "min_rating": "0"})
            self.assertEqual((202, "export"), (code, job["kind"]), job)
            finished = self.wait_job(job["job_id"])
        self.assertEqual(("succeeded", {"exported": 1, "failed": 0, "skipped": 0}), (finished["status"], finished["result"]))
        with patch("lumen.library.PipelineRegistry.darktable", side_effect=AssertionError("render attempted")):
            for payload, error in (({"scope": "filtered", "decision": "flag"}, "no frames match these filters"),
                                   ({"scope": "filtered", "min_rating": 3}, "filter values must be strings"),
                                   ({"scope": "filtered", "colour": "red"}, "invalid filter: colour"),
                                   ({"scope": "filtered", "photo_id": "1"}, "invalid filter: photo_id")):
                with self.subTest(payload=payload):
                    self.assertEqual((400, {"error": error}), self.post_json("/api/export", payload))

    def test_export_rejects_invalid_or_ambiguous_payload_before_render(self):
        payloads = [
            {}, {"scope": "all"}, {"scope": "current"},
            *({"scope": "current", "photo_id": value} for value in (None, "1", True, 1.0, [], {})),
            {"scope": "current", "photo_id": 99999},
            {"scope": "current", "photo_id": 1, "profile": 42},
            {"scope": "keepers", "profile": []},
            {"scope": "keepers", "photo_id": 1},
            {"scope": "current", "photo_id": 1, "photo_ids": [1]},
            {"scope": "current", "photo_id": 1, "keepers": True},
            {"scope": "keepers", "all": True},
        ]
        with patch("lumen.library.PipelineRegistry.darktable", side_effect=AssertionError("render attempted")):
            for payload in payloads:
                with self.subTest(payload=payload):
                    code, result = self.post_json("/api/export", payload)
                    self.assertEqual(400, code)
                    self.assertIn("error", result)
        self.assertFalse((self.root / "library" / "outputs" / "jpeg").exists())

    def test_export_passes_keep_original_through_and_refuses_a_non_boolean(self):
        with patch("lumen.library.PipelineRegistry.darktable", side_effect=AssertionError("render attempted")):
            for value in ("yes", 1, None, []):
                with self.subTest(value=value):
                    code, result = self.post_json("/api/export", {"scope": "current", "photo_id": 1, "keep_original": value})
                    self.assertEqual((400, "keep_original must be true or false"), (code, result.get("error")))
        requests = []
        with patch("lumen.library.Library.export_jpegs", new=lambda _self, request: requests.append(request) or []):
            for payload in ({"scope": "current", "photo_id": 1, "keep_original": True},
                            {"scope": "current", "photo_id": 1}):
                with self.subTest(payload=payload):
                    code, _ = self.post_json("/api/export", payload)
                    self.assertEqual(200, code)
        self.assertEqual([True, False], [request.keep_original for request in requests])

    def test_export_defaults_to_a_dated_folder_and_refuses_a_non_boolean(self):
        with patch("lumen.library.PipelineRegistry.darktable", side_effect=AssertionError("render attempted")):
            for value in ("yes", 1, None, []):
                with self.subTest(value=value):
                    code, result = self.post_json("/api/export", {"scope": "current", "photo_id": 1, "dated_folder": value})
                    self.assertEqual((400, "dated_folder must be true or false"), (code, result.get("error")))
        requests = []
        with patch("lumen.library.Library.export_jpegs", new=lambda _self, request: requests.append(request) or []):
            for payload in ({"scope": "current", "photo_id": 1}, {"scope": "current", "photo_id": 1, "dated_folder": False}):
                with self.subTest(payload=payload):
                    code, _ = self.post_json("/api/export", payload)
                    self.assertEqual(200, code)
        self.assertEqual([True, False], [request.dated_folder for request in requests])
        markup = (Path(__file__).parent.parent / "src/lumen/static/index.html").read_text()
        self.assertIn('id="export-dated-folder" checked', markup)
        self.assertIn('id="loupe-dated-folder" checked', markup)

    def test_export_accepts_known_presets_only_and_ui_has_labeled_preset_radios(self):
        with patch("lumen.library.PipelineRegistry.darktable", side_effect=AssertionError("render attempted")):
            for payload in ({"scope": "current", "photo_id": 1, "preset": "x"}, {"scope": "keepers", "preset": 2},
                            {"scope": "keepers", "preset": None}, {"scope": "current", "photo_id": 1, "preset": ["share"]}):
                with self.subTest(payload=payload):
                    code, result = self.post_json("/api/export", payload)
                    self.assertEqual(400, code)
                    self.assertIn("error", result)
        pipeline = DarktablePipeline("darktable-cli", self.root / "library" / "outputs")
        with patch("lumen.library.PipelineRegistry.darktable", return_value=pipeline), patch(
            "lumen.pipelines.subprocess.run", side_effect=self.render
        ):
            code, share = self.post_json("/api/export", {"scope": "current", "photo_id": 1, "preset": "share"})
            self.assertEqual(200, code, share)
            code, full = self.post_json("/api/export", {"scope": "keepers", "preset": "full"})
            self.assertEqual(202, code, full)
            self.assertEqual("succeeded", self.wait_job(full["job_id"])["status"])
        self.assertEqual("photo-lumen-share.jpg", Path(share["results"][0]["output"]).name)
        self.assertEqual("share", share["results"][0]["artifact"]["settings"]["preset"])
        with Library(Settings(library_dir=self.root / "library")) as library:
            full_artifact = library.catalog.list_artifacts()[-1]
        self.assertEqual(("photo-lumen.jpg", "full"), (Path(full_artifact.path).name, full_artifact.settings["preset"]))
        html = urllib.request.urlopen(self.base + "/", timeout=3).read().decode()
        parser = LandmarkParser()
        parser.feed(html)
        radios = [node for node in parser.nodes if node["tag"] == "input" and node["attrs"].get("name") == "export-preset"]
        self.assertEqual([("radio", "full"), ("radio", "share"), ("radio", "web")], [(node["attrs"].get("type"), node["attrs"].get("value")) for node in radios])
        self.assertIn("checked", radios[0]["attrs"])
        for radio, label in zip(radios, ("Full resolution (quality 92, keeps GPS)", "Share (2048 px long edge, quality 85, no GPS)",
                                         "Web (1600 px long edge, quality 82, no GPS)"), strict=True):
            self.assertTrue(any(node["tag"] == "label" and label in " ".join(node["text"].split()) for node in radio["parents"]))
            self.assertTrue(any(node["tag"] == "fieldset" and "Size" in " ".join(node["text"].split()) for node in radio["parents"]))
        self.assertNotIn("Quality 92 · sRGB · Full resolution", html)
        for text in ('<label for="export-watermark">Watermark text (optional)</label>',
                     '<input type="text" id="export-watermark" maxlength="80" autocomplete="off">'):
            self.assertIn(text, html)
        javascript = (Path(__file__).resolve().parents[1] / "src" / "lumen" / "static" / "app.js").read_text()
        self.assertIn('input[name="export-preset"]:checked', javascript)
        self.assertIn("Used only by the Share and Web exports.", javascript)
        self.assertRegex(javascript, r"payload = \{ scope, profile: state.profile, preset \}")

    def test_export_accepts_web_preset_with_watermark_and_rejects_bad_ones(self):
        with patch("lumen.library.PipelineRegistry.darktable", side_effect=AssertionError("render attempted")):
            code, result = self.post_json("/api/export", {"scope": "keepers", "preset": "x"})
            self.assertEqual((400, "preset must be full, share, or web"), (code, result["error"]))
            for watermark in ("  ", "x" * 81, 7, "a\nb"):
                with self.subTest(watermark=watermark):
                    code, result = self.post_json("/api/export", {"scope": "keepers", "preset": "web", "watermark": watermark})
                    self.assertEqual((400, "watermark must be 1 to 80 printable characters"), (code, result["error"]))
        pipeline = DarktablePipeline("darktable-cli", self.root / "library" / "outputs")
        with patch("lumen.library.PipelineRegistry.darktable", return_value=pipeline), patch(
            "lumen.pipelines.subprocess.run", side_effect=self.render
        ):
            code, current = self.post_json("/api/export", {"scope": "current", "photo_id": 1, "preset": "web", "watermark": "\u00a9 N. David"})
            self.assertEqual(200, code, current)
            code, job = self.post_json("/api/export", {"scope": "keepers", "preset": "web", "watermark": "\u00a9 N. David"})
            self.assertEqual(202, code, job)
            self.assertEqual("succeeded", self.wait_job(job["job_id"])["status"])
        self.assertEqual("photo-lumen-web.jpg", Path(current["results"][0]["output"]).name)
        self.assertEqual(("web", True), (current["results"][0]["artifact"]["settings"]["preset"], current["results"][0]["artifact"]["settings"]["watermark"]))
        self.assertNotIn("N. David", json.dumps(current))
        with Library(Settings(library_dir=self.root / "library")) as library:
            self.assertEqual(("web", True), (library.catalog.list_artifacts()[-1].settings["preset"], library.catalog.list_artifacts()[-1].settings["watermark"]))

    def test_export_unavailable_darktable_returns_bad_request(self):
        with patch("lumen.library.PipelineRegistry.darktable", side_effect=RuntimeError("darktable-cli is unavailable")):
            code, payload = self.post_json("/api/export", {"scope": "keepers", "profile": None})
        self.assertEqual(400, code)
        self.assertIn("darktable-cli", payload["error"])

    def test_export_out_of_sqlite_range_photo_ids_return_json_without_export_effects(self):
        source = self.root / "photo.jpg"
        before = source.read_bytes()
        with patch("lumen.library.PipelineRegistry.darktable", side_effect=AssertionError("render attempted")):
            for photo_id in (9223372036854775808, -9223372036854775809):
                with self.subTest(photo_id=photo_id):
                    code, payload = self.post_json("/api/export", {"scope": "current", "photo_id": photo_id, "profile": None})
                    self.assertEqual(400, code)
                    self.assertEqual({"error"}, set(payload))
                    self.assertIsInstance(payload["error"], str)
                    self.assertTrue(payload["error"])
        self.assertFalse((self.root / "library" / "outputs" / "jpeg").exists())
        self.assertEqual(before, source.read_bytes())
        with Library(Settings(library_dir=self.root / "library")) as library:
            self.assertEqual([], library.catalog.list_artifacts())
            self.assertFalse(any(event.kind == "exported" for event in library.catalog.events_for(1)))

    def test_export_batch_failure_reports_successful_item(self):
        second = self.root / "second.jpg"
        Image.new("RGB", (40, 30), "blue").save(second)
        with Library(Settings(library_dir=self.root / "library")) as library:
            library.import_path(second)
        pipeline = DarktablePipeline("darktable-cli", self.root / "library" / "outputs")
        def render(command, **kwargs):
            if command[1:] != ["--version"] and Path(command[1]).name == "photo.jpg":
                return subprocess.CompletedProcess(command, 3, "", "bad render")
            return self.render(command, **kwargs)
        with patch("lumen.library.PipelineRegistry.darktable", return_value=pipeline), patch(
            "lumen.pipelines.subprocess.run", side_effect=render
        ):
            code, payload = self.post_json("/api/export", {"scope": "keepers", "profile": None})
            self.assertEqual(202, code, payload)
            finished = self.wait_job(payload["job_id"])
        self.assertEqual(("succeeded", {"exported": 1, "failed": 1, "skipped": 0}), (finished["status"], finished["result"]))
        self.assertEqual(1, len(finished["log_tail"]))
        self.assertTrue(finished["log_tail"][0].startswith("photo.jpg: "), finished["log_tail"])
        self.assertNotIn(str(self.root), json.dumps(finished))
        with Library(Settings(library_dir=self.root / "library")) as library:
            artifacts = library.catalog.list_artifacts()
        self.assertEqual(1, len(artifacts))
        self.assertTrue(Path(artifacts[0].path).is_file())

    def test_remove_group_returns_recovery_counts_and_preserves_original(self):
        source = self.root / "photo.jpg"
        before = source.read_bytes()
        code, payload = self.post_json("/api/groups/1/remove", {})
        self.assertEqual(200, code, payload)
        self.assertEqual(1, payload["photos_removed"])
        self.assertGreater(payload["files_removed"], 0)
        self.assertTrue(Path(payload["trash_path"]).is_dir())
        self.assertTrue(Path(payload["snapshot_path"]).is_file())
        self.assertEqual(before, source.read_bytes())
        with Library(Settings(library_dir=self.root / "library")) as library:
            self.assertEqual([], library.list_groups())

    def test_photo_details_carry_import_warnings(self):
        code, payload = self.get_json("/api/photos/1")
        self.assertEqual(200, code)
        self.assertEqual([], payload["warnings"])
        with Library(Settings(library_dir=self.root / "library")) as library:
            library.catalog.append_event(1, "imported", {"raw_warning": "Sony reduced-size lossless raw; darktable cannot decode it"})
        self.assertEqual(["Sony reduced-size lossless raw; darktable cannot decode it"], self.get_json("/api/photos/1")[1]["warnings"])

    def test_javascript_inspector_explains_rank_within_burst(self):
        self.run_javascript_cleanup_regression("rank-explain")

    def test_javascript_inspector_shows_import_warnings(self):
        self.run_javascript_cleanup_regression("raw-warning")

    def test_split_route_moves_one_frame_into_its_own_burst(self):
        second = self.root / "second.jpg"
        Image.new("RGB", (40, 30), "blue").save(second)
        with Library(Settings(library_dir=self.root / "library")) as library:
            library.import_path(second)
            library.merge_groups([group["id"] for group in library.list_groups()])
            self.assertEqual(1, len(library.list_groups()))
        for path, payload in (("/api/photos/2/split", {"photo_id": 2}), ("/api/photos/99999/split", {})):
            with self.subTest(path=path, payload=payload):
                code, result = self.post_json(path, payload)
                self.assertEqual(400, code, result)
                self.assertIn("error", result)
                self.assertNotIn(str(self.root), result["error"])
        code, result = self.post_json("/api/photos/2/split", {})
        self.assertEqual(200, code, result)
        self.assertEqual({"photo_id": 2, "group_id": result["group_id"], "from_group_id": 1}, result)
        with Library(Settings(library_dir=self.root / "library")) as library:
            groups = library.list_groups()
            self.assertEqual([[1], [2]], sorted([photo["id"] for photo in group["photos"]] for group in groups))
        code, result = self.post_json("/api/photos/2/split", {})
        self.assertEqual((400, "photo 2 is already in a burst of its own"), (code, result["error"]))

    def test_merge_route_validates_ids_and_merges_into_earliest_group(self):
        second = self.root / "second.jpg"
        Image.new("RGB", (40, 30), "blue").save(second)
        with Library(Settings(library_dir=self.root / "library")) as library:
            library.import_path(second)
        for payload in ({}, {"group_ids": [1]}, {"group_ids": [1, 1]}, {"group_ids": [1, "2"]}, {"group_ids": [1, True]},
                        {"group_ids": [1, 99999]}, {"group_ids": [1, 2], "extra": 1}, {"group_ids": "1,2"}, {"group_ids": [1, 0]}):
            with self.subTest(payload=payload):
                code, result = self.post_json("/api/groups/merge", payload)
                self.assertEqual(400, code, result)
                self.assertIn("error", result)
                self.assertNotIn(str(self.root), result["error"])
        with Library(Settings(library_dir=self.root / "library")) as library:
            self.assertEqual(2, len(library.list_groups()))
        code, result = self.post_json("/api/groups/merge", {"group_ids": [2, 1]})
        self.assertEqual(200, code, result)
        self.assertEqual({"group_id": 1, "merged_group_ids": [2], "photo_count": 2, "automatic_pick_id": 1}, result)
        with Library(Settings(library_dir=self.root / "library")) as library:
            self.assertEqual([[1, 2]], [sorted(photo["id"] for photo in group["photos"]) for group in library.list_groups()])

    def test_cleanup_rejects_unknown_group_and_nonempty_remove_payload(self):
        for path, payload in (("/api/groups/99999/remove", {}), ("/api/groups/1/remove", {"confirmation": "CLEAR"})):
            with self.subTest(path=path, payload=payload):
                code, result = self.post_json(path, payload)
                self.assertEqual(400, code)
                self.assertIn("error", result)
        with Library(Settings(library_dir=self.root / "library")) as library:
            self.assertEqual(1, len(library.catalog.list_photos()))

    def test_cleanup_sqlite_delete_failure_returns_json_and_restores_workspace(self):
        settings = Settings(library_dir=self.root / "library")
        source = self.root / "photo.jpg"
        output = settings.output_dir / "completed.jpg"
        output.write_bytes(b"completed export")
        protected = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (source, output)}
        with Library(settings) as library:
            photo = library.catalog.get_photo(1)
            library.catalog.save_artifact(photo.id, source.name, photo.fingerprint, "jpeg", str(output), {}, "output")
            managed = {path: path.read_bytes() for path in (Path(photo.preview_path), *library.sidecars.paths(photo))}
            library.catalog.connection.execute("CREATE TRIGGER fail_cleanup AFTER DELETE ON photos BEGIN SELECT RAISE(ABORT, 'cleanup deletion failed'); END")
            library.catalog.connection.commit()
        for route, body in (("/api/groups/1/remove", {}), ("/api/library/clear", {"confirmation": "CLEAR"})):
            with self.subTest(route=route):
                code, payload = self.post_json(route, body)
                self.assertEqual(400, code)
                self.assertIn("cleanup deletion failed", payload["error"])
                self.assertTrue(payload["recovery_complete"])
                self.assertEqual([], payload["restoration_errors"])
                recovery = Path(payload["recovery_path"])
                self.assertTrue(recovery.is_dir())
                self.assertEqual(recovery / "catalog.sqlite3", Path(payload["snapshot_path"]))
                self.assertTrue(Path(payload["snapshot_path"]).is_file())
                self.assertEqual(managed, {path: path.read_bytes() for path in managed})
                with Library(settings) as library:
                    self.assertIsNotNone(library.get_photo(photo.id)["metrics"])
                    self.assertEqual([photo.id], library.keeper_ids())
                    self.assertEqual(photo.id, library.catalog.list_artifacts()[0].photo_id)
        self.assertEqual(protected, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in protected})

    def test_cleanup_compound_move_restore_failure_returns_json_recovery_context(self):
        settings = Settings(library_dir=self.root / "library")
        with Library(settings) as library:
            photo = library.catalog.get_photo(1)
            preview = Path(photo.preview_path)
            sidecar = library.sidecars.paths(photo)[0]
        before = preview.read_bytes()
        rename = Path.rename
        def fail_move_and_restore(path, target):
            if path.resolve() == sidecar.resolve():
                raise OSError("staging move failed")
            if Path(target).resolve() == preview.resolve():
                raise OSError("preview restore failed")
            return rename(path, target)
        with patch.object(Path, "rename", fail_move_and_restore):
            code, payload = self.post_json("/api/library/clear", {"confirmation": "CLEAR"})
        self.assertEqual(400, code)
        self.assertIn("staging move failed", payload["error"])
        self.assertFalse(payload["recovery_complete"])
        self.assertTrue(any("preview restore failed" in error for error in payload["restoration_errors"]))
        recovery = Path(payload["recovery_path"])
        self.assertEqual(before, (recovery / "files" / "previews" / preview.name).read_bytes())
        self.assertTrue(Path(payload["snapshot_path"]).is_file())
        self.assertTrue(sidecar.is_file())
        with Library(settings) as library:
            self.assertIsNotNone(library.get_photo(photo.id)["metrics"])

    def test_cleared_routes_count_and_remove_previously_cleared_frames(self):
        self.assertEqual((200, {"frames": 0, "batches": 0, "seed": {"snapshots": 0, "skipped": 0, "added": 0}}),
                         self.get_json("/api/library/cleared"))
        self.assertEqual(400, self.post_json("/api/library/remove-cleared", {})[0])
        with Library(Settings(library_dir=self.root / "library")) as library:
            fingerprint = library.catalog.get_photo(1).fingerprint
            library.catalog.record_cleared([fingerprint], "2026-10-23T00:00:00+00:00")
        code, payload = self.get_json("/api/library/cleared")
        self.assertEqual((200, {"frames": 1, "batches": 1}), (code, {key: payload[key] for key in ("frames", "batches")}))
        self.assertNotIn(fingerprint, json.dumps(payload))
        code, result = self.post_json("/api/library/remove-cleared", {"x": 1})
        self.assertEqual(400, code, result)
        code, result = self.post_json("/api/library/remove-cleared", {})
        self.assertEqual(200, code, result)
        self.assertEqual(1, result["photos_removed"])
        self.assertTrue(Path(result["snapshot_path"]).is_file())
        code, result = self.post_json("/api/library/remove-cleared", {})
        self.assertEqual((400, "no previously cleared frames in the catalog"), (code, result["error"]))
        with Library(Settings(library_dir=self.root / "library")) as library:
            self.assertEqual([], library.catalog.list_photos())

    def test_clear_requires_exact_confirmation_then_returns_recovery_paths(self):
        for payload in ({}, {"confirmation": "clear"}, {"confirmation": "CLEAR "}, {"confirmation": None}, {"confirmation": True}, {"confirmation": ["CLEAR"]}):
            with self.subTest(payload=payload):
                code, result = self.post_json("/api/library/clear", payload)
                self.assertEqual(400, code)
                self.assertIn("error", result)
                with Library(Settings(library_dir=self.root / "library")) as library:
                    self.assertEqual(1, len(library.catalog.list_photos()))
        code, payload = self.post_json("/api/library/clear", {"confirmation": "CLEAR"})
        self.assertEqual(200, code)
        self.assertEqual(1, payload["photos_removed"])
        self.assertTrue(Path(payload["snapshot_path"]).is_file())

    def test_new_mutations_reject_cross_origin_and_host_before_reading_body(self):
        routes = (
            "/api/export", "/api/groups/1/remove", "/api/library/clear", "/api/browse-xmp",
            "/api/outputs/reveal", "/api/photos/1/crop", "/api/photos/1/crop/clear",
            "/api/photos/1/composition-review", "/api/astro/jobs", "/api/astro/jobs/1/run", "/api/astro/jobs/1/cancel",
            "/api/import", "/api/photos/tag-all", "/api/jobs/1/cancel", "/api/groups/merge", "/api/groups/1/triage", "/api/photos/assist",
            "/api/vocabulary/forget", "/api/search/expand", "/api/library/remove-cleared", "/api/photos/1/place",
            "/api/photos/1/develop/suggest", "/api/photos/1/location", "/api/photos/1/split",
        )
        headers = ({"Origin": "https://attacker.example"}, {"Host": f"attacker.example:{self.server.server_port}"})
        with patch("lumen.web.subprocess.run", side_effect=AssertionError("macOS tool invoked")):
            for route in routes:
                for header in headers:
                    with self.subTest(route=route, header=header):
                        code, payload = self.post_json(route, None, headers=header, raw=b"invalid json")
                        self.assertEqual(403, code)
                        self.assertIn("cross-origin", payload["error"])
        with Library(Settings(library_dir=self.root / "library")) as library:
            self.assertEqual(1, len(library.catalog.list_photos()))
            self.assertEqual([], library.catalog.list_artifacts())

    def test_new_mutations_reject_nonobject_and_malformed_json(self):
        for route in (
            "/api/export", "/api/groups/1/remove", "/api/library/clear", "/api/browse-xmp",
            "/api/outputs/reveal", "/api/photos/1/crop", "/api/photos/1/crop/clear",
            "/api/photos/1/composition-review", "/api/astro/jobs", "/api/astro/jobs/1/run", "/api/astro/jobs/1/cancel",
            "/api/import", "/api/photos/tag-all", "/api/jobs/1/cancel", "/api/groups/merge", "/api/groups/1/triage", "/api/photos/assist",
            "/api/vocabulary/forget", "/api/search/expand", "/api/library/remove-cleared", "/api/photos/1/place",
            "/api/photos/1/develop/suggest", "/api/photos/1/location", "/api/photos/1/split",
        ):
            for raw in (b"[]", b"null", b"{", b""):
                with self.subTest(route=route, raw=raw):
                    code, payload = self.post_json(route, None, raw=raw)
                    self.assertEqual(400, code)
                    self.assertIn("error", payload)

    def test_finishing_tabs_have_accessible_controls_and_separate_cleanup(self):
        with urllib.request.urlopen(self.base + "/", timeout=3) as response:
            parser = LandmarkParser()
            parser.feed(response.read().decode())
        by_id = {node["attrs"]["id"]: node for node in parser.nodes if "id" in node["attrs"]}
        tabs = [node for node in parser.nodes if node["attrs"].get("role") == "tab" and by_id["loupe"] not in node["parents"]]
        self.assertEqual(["Add photos", "Review", "Assist", "Astro", "Export", "Clear"], [node["text"].strip() for node in tabs], "tabs follow the workflow")
        self.assertTrue(any(node["attrs"].get("role") == "tablist" and node["attrs"].get("aria-label") for node in parser.nodes))
        for tab in tabs:
            self.assertEqual("button", tab["tag"])
            default = tab["text"].strip() == "Add photos"  # the markup default; the first load folds it away when the catalog has frames
            self.assertEqual(str(default).lower(), tab["attrs"]["aria-selected"])
            self.assertEqual("0" if default else "-1", tab["attrs"].get("tabindex"))
            panel = by_id[tab["attrs"]["aria-controls"]]
            self.assertEqual("tabpanel", panel["attrs"].get("role"))
            self.assertEqual(tab["attrs"]["id"], panel["attrs"].get("aria-labelledby"))
            self.assertEqual(not default, "hidden" in panel["attrs"])
        assist_ids = ["assist-keepers", "tag-keepers", "redo-keepers", "tag-all", "retag-all", "vocabulary-panel", "write-digest", "digest-result"]
        self.assertEqual(assist_ids, [node["attrs"]["id"] for node in parser.nodes if node["attrs"].get("id") in assist_ids], "Assist holds them in this order")
        for name in assist_ids:
            self.assertIn(by_id["assist-panel"], by_id[name]["parents"], f"{name} lives in the Assist tab")
            self.assertNotIn(by_id["export-panel"], by_id[name]["parents"], f"{name} left the Export tab")
        for name in ("ingest-panel", "import-panel"):
            self.assertIn(by_id["add-panel"], by_id[name]["parents"])
            self.assertNotIn("role", by_id[name]["attrs"], "the add-source radios, not the tablist, show these sections")
        self.assertEqual(("fieldset", "segmented"), (by_id["add-source"]["tag"], by_id["add-source"]["attrs"].get("class")))
        radios = [node for node in parser.nodes if node["attrs"].get("name") == "add-source"]
        self.assertEqual([("ingest", False), ("import", True)], [(node["attrs"]["value"], "checked" in node["attrs"]) for node in radios])
        for radio, label in zip(radios, ("Still on the memory card", "Already on a drive")):
            self.assertTrue(any(node["tag"] == "label" and node["text"].strip() == label for node in radio["parents"]), label)
        self.assertEqual((True, False), ("hidden" in by_id["ingest-panel"]["attrs"], "hidden" in by_id["import-panel"]["attrs"]))
        for node in parser.nodes:
            if node["tag"] == "button":
                self.assertIn(node["attrs"].get("type"), {"button", "submit"})
        field = by_id["clear-confirmation"]
        self.assertEqual("input", field["tag"])
        self.assertTrue(any(node["tag"] == "label" and node["attrs"].get("for") == "clear-confirmation" and node["text"].strip() for node in parser.nodes))
        self.assertTrue(by_id[field["attrs"]["aria-describedby"]]["text"].strip())
        for name in ("system-status", "compose-status", "export-status", "astro-status", "clear-status", "import-status"):
            self.assertEqual("polite", by_id[name]["attrs"].get("aria-live"))
        self.assertEqual("assertive", by_id["announcer"]["attrs"].get("aria-live"))
        export = by_id["export-controls"]
        for name in ("remove-group", "remove-cleared", "clear-workspace"):
            self.assertNotIn(export, by_id[name]["parents"])
            self.assertIn(by_id["clear-panel"], by_id[name]["parents"])

    def test_refine_route_scope_validation_and_unavailable_darktable_reason(self):
        with patch("lumen.library.find_tool", return_value=None):
            self.assertEqual((400, {"error": "darktable-cli is unavailable"}), self.post_json("/api/previews/refine", {"scope": "all"}))
        with patch("lumen.library.find_tool", return_value=write_fake_darktable(self.root)):
            for payload in ({}, {"scope": "everything"}, {"scope": "group"}, {"scope": "group", "group_id": "1"},
                            {"scope": "group", "group_id": True}, {"scope": "group", "group_id": 1, "extra": 1},
                            {"scope": "group", "group_id": 999}, {"scope": "all", "group_id": 1},
                            {"scope": "filtered", "bogus": "1"}, {"scope": "filtered", "min_rating": 3},
                            {"scope": "filtered", "min_rating": "9"}, {"scope": "filtered", "date_from": "2026-13-01"},
                            {"scope": "filtered", "group_id": 1}):
                with self.subTest(payload=payload):
                    code, body = self.post_json("/api/previews/refine", payload)
                    self.assertEqual(400, code, body)
                    self.assertIsInstance(body["error"], str)
                    self.assertNotIn(str(self.root), json.dumps(body))
            self.assertEqual(400, self.post_json("/api/previews/refine", None, raw=b"[]")[0])
            self.assertEqual(403, self.post_json("/api/previews/refine", {"scope": "all"}, headers={"Origin": "http://evil.example"})[0])
            self.assertEqual([], self.get_json("/api/jobs")[1]["jobs"], "rejected requests never create a job")
            code, started = self.post_json("/api/previews/refine", {"scope": "filtered", "camera": "nobody"})
            self.assertEqual((202, "refine_previews"), (code, started["kind"]))
            self.assertEqual({"refined": 0, "skipped": 0, "failed": 0}, self.wait_job(started["job_id"])["result"])
            code, started = self.post_json("/api/previews/refine", {"scope": "all"})
            self.assertEqual(202, code, started)
            finished = self.wait_job(started["job_id"])
        self.assertEqual(("succeeded", {"refined": 1, "skipped": 0, "failed": 0}), (finished["status"], finished["result"]))
        with Library(Settings(library_dir=self.root / "library")) as library:
            fingerprint = library.catalog.list_photos()[0].fingerprint
        for body in (started, finished):
            self.assertNotIn(str(self.root), json.dumps(body))
            self.assertNotIn(fingerprint, json.dumps(body))

    def preview_version(self, kind):
        with Library(Settings(library_dir=self.root / "library")) as library:
            photo = library.catalog.list_photos()[0]
            return f"{photo.fingerprint[:16]}-{kind}-{Path(photo.preview_path).stat().st_mtime_ns}"

    def test_preview_url_version_changes_after_refinement_and_cards_show_refined_mark(self):
        with Library(Settings(library_dir=self.root / "library")) as library:
            version = self.preview_version("embedded")
            group_id = library.list_groups()[0]["id"]
        before = self.get_json("/api/groups")[1]["groups"][0]["photos"][0]
        self.assertEqual((f"/preview/1?v={version}", "embedded"), (before["preview_url"], before["preview_kind"]))
        with patch("lumen.library.find_tool", return_value=write_fake_darktable(self.root)):
            code, started = self.post_json("/api/previews/refine", {"scope": "group", "group_id": group_id})
            self.assertEqual(202, code, started)
            self.assertEqual("succeeded", self.wait_job(started["job_id"])["status"])
        refined_url = f"/preview/1?v={self.preview_version('refined')}"
        after = self.get_json("/api/groups")[1]["groups"][0]["photos"][0]
        self.assertEqual((refined_url, "refined"), (after["preview_url"], after["preview_kind"]))
        photo = self.get_json("/api/photos/1")[1]["photo"]
        self.assertEqual((refined_url, "refined"), (photo["preview_url"], photo["preview_kind"]))
        with patch("lumen.library.read_image_dimensions", return_value=(4000, 3000)), patch(
            "lumen.library.analyze_subject_regions", return_value=None
        ):
            self.assertEqual(refined_url, self.get_json("/api/photos/1/crops")[1]["preview_url"])
        with urllib.request.urlopen(self.base + refined_url, timeout=3) as response:
            with Image.open(BytesIO(response.read())) as preview:
                self.assertEqual(2048, max(preview.size))
        self.run_javascript_cleanup_regression("refine")

    def test_preview_url_changes_after_a_develop_save(self):
        """A second develop save keeps the fingerprint and the kind, so only the file's mtime can bust the tile."""
        def render(source, destination, executable, *, settings=None, long_edge=0,
                   image_format="tiff16", histogram=False, runner=None):
            Image.effect_noise((2048, 1536), 64).convert("RGB").save(destination, format="JPEG", quality=95)
            return DevelopRender(path=Path(destination), width=2048, height=1536, histogram={}, defaults={}, lens_correction_supported=False,
                                 neutral_temperature=5500.0, neutral_tint=0.0)

        seen = []
        with Library(Settings(library_dir=self.root / "library")) as library:
            photo_id = library.catalog.list_photos()[0].id
            with patch("lumen.library.render_raw", side_effect=render):
                for exposure in (0.5, 1.0):
                    library.save_develop(photo_id, DevelopSettings({"exposure_ev": exposure}))
                    tile = self.get_json("/api/groups")[1]["groups"][0]["photos"][0]
                    frame = self.get_json("/api/photos/1")[1]["photo"]
                    self.assertEqual(tile["preview_url"], frame["preview_url"], "sheet and inspector agree")
                    seen.append(tile["preview_url"])

        keys = [url.split("?v=")[1].split("-") for url in seen]
        self.assertEqual([key[:2] for key in keys[:1]], [key[:2] for key in keys[1:]],
                         "fingerprint and kind are identical, so only the mtime can bust the tile")
        self.assertEqual(["developed", "developed"], [key[1] for key in keys])
        self.assertGreater(int(keys[1][2]), int(keys[0][2]), "the preview file's mtime advanced")
        self.assertNotEqual(seen[0], seen[1], "the second save of an already-developed frame still busts the tile")

    def test_crop_api_returns_safe_candidates_and_active_edit(self):
        with patch("lumen.library.read_image_dimensions", return_value=(4000, 3000)), patch(
            "lumen.library.analyze_subject_regions", return_value=None
        ):
            with urllib.request.urlopen(self.base + "/api/photos/1/crops", timeout=3) as response:
                payload = json.load(response)
            self.assertEqual(1, payload["photo_id"])
            self.assertRegex(payload["preview_url"], r"^/preview/1\?v=[0-9a-f]{16}-embedded-\d+$")
            self.assertIsNone(payload["active_crop"])
            self.assertGreaterEqual(len(payload["candidates"]), 2)
            self.assertEqual("original", payload["candidates"][0]["kind"])
            self.assertEqual("centered-fallback", payload["candidates"][1]["kind"])
            self.assertEqual("8.003241 MP remaining; below 12 MP", payload["candidates"][1]["resolution_warning"])
            self.assertNotIn("path", json.dumps(payload))
            self.assertNotIn("fingerprint", json.dumps(payload))
            candidate = next(item for item in payload["candidates"] if item["kind"] != "original")
            code, chosen = self.post_json("/api/photos/1/crop", {"candidate_id": candidate["id"]})
            self.assertEqual(200, code, chosen)
            self.assertEqual(candidate["id"], chosen["candidate_id"])
            with urllib.request.urlopen(self.base + "/api/photos/1/crops", timeout=3) as response:
                refreshed = json.load(response)
            self.assertEqual(candidate["id"], refreshed["active_crop"]["candidate_id"])
            code, cleared = self.post_json("/api/photos/1/crop/clear", {})
            self.assertEqual((200, {"photo_id": 1, "active_crop": None, "preview_url": payload["preview_url"]}), (code, cleared))

    def test_manual_crop_route_accepts_rect_and_rejects_bad_shapes(self):
        with patch("lumen.library.read_image_dimensions", return_value=(6000, 4000)), patch(
            "lumen.library.analyze_subject_regions", return_value=None
        ):
            payload = self.get_json("/api/photos/1/crops")[1]
            self.assertEqual((6000, 4000), (payload["source_width"], payload["source_height"]))
            self.assertIs(int, type(payload["source_width"]))
            rect = {"left": .1, "top": .1, "width": .8, "height": .8}
            invalid = (
                {"candidate_id": "crop-manual", "rect": {"left": .1, "top": .1, "width": .8}, "aspect": "free"},
                {"candidate_id": "crop-manual", "rect": {**rect, "width": "0.8"}, "aspect": "free"},
                {"candidate_id": "crop-manual", "rect": {**rect, "width": True}, "aspect": "free"},
                {"candidate_id": "crop-manual", "rect": rect, "aspect": "7:3"},
                {"candidate_id": "crop-manual", "rect": rect, "aspect": "free", "author": "vision"},
                {"candidate_id": "crop-manual", "rect": {**rect, "depth": 1}, "aspect": "free"},
                {"candidate_id": "crop-manual", "rect": rect},
                {"candidate_id": "crop-manual"},
            )
            for body in invalid:
                with self.subTest(body=body):
                    code, result = self.post_json("/api/photos/1/crop", body)
                    self.assertEqual(400, code, result)
                    self.assertIn("error", result)
            code, result = self.post_json("/api/photos/1/crop", {"candidate_id": "crop-manual", "rect": {**rect, "width": .3, "height": .3}, "aspect": "free"})
            self.assertEqual(200, code, result)  # a tight crop is the photographer's call; no resolution floor
            code, chosen = self.post_json("/api/photos/1/crop", {"candidate_id": "crop-manual", "rect": rect, "aspect": "4:5"})
            self.assertEqual(200, code, chosen)
            self.assertEqual(("crop-manual", "photographer", "4:5"), (chosen["candidate_id"], chosen["author"], chosen["aspect"]))
            active = self.get_json("/api/photos/1/crops")[1]["active_crop"]
            self.assertEqual("crop-manual", active["candidate_id"])
            self.assertEqual(rect, active["rect"])
            generated = next(item for item in payload["candidates"] if item["kind"] != "original")
            code, chosen = self.post_json("/api/photos/1/crop", {"candidate_id": generated["id"]})
            self.assertEqual((200, generated["id"]), (code, chosen["candidate_id"]))

    def test_a_selected_crop_versions_the_preview_url_and_crops_the_thumbnail(self):
        """The contact sheet shows what export will bake in: the thumb follows the crop and its URL busts the cache."""
        def thumb_size(url):
            with urllib.request.urlopen(self.base + url.replace("/preview/", "/thumb/"), timeout=3) as response:
                with Image.open(BytesIO(response.read())) as image:
                    return image.size

        before = self.get_json("/api/groups")[1]["groups"][0]["photos"][0]["preview_url"]
        whole = thumb_size(before)
        with patch("lumen.library.read_image_dimensions", return_value=(6000, 4000)), patch(
            "lumen.library.analyze_subject_regions", return_value=None
        ):
            code, chosen = self.post_json("/api/photos/1/crop", {"candidate_id": "crop-manual", "aspect": "free",
                                                                 "rect": {"left": .25, "top": 0, "width": .5, "height": 1}})
        self.assertEqual(200, code, chosen)
        after = self.get_json("/api/groups")[1]["groups"][0]["photos"][0]["preview_url"]
        self.assertNotEqual(before, after)
        self.assertEqual(after, chosen["preview_url"])
        self.assertEqual(after, self.get_json("/api/photos/1")[1]["photo"]["preview_url"])
        width, height = thumb_size(after)
        self.assertAlmostEqual(whole[0] / 2 / whole[1], width / height, places=1)
        code, cleared = self.post_json("/api/photos/1/crop/clear", {})
        self.assertEqual((200, before), (code, cleared["preview_url"]))
        self.assertEqual(whole, thumb_size(before))

    def test_manual_crop_route_snaps_edge_overshoot_rounds_halves_up_and_rejects_nan(self):
        with patch("lumen.library.read_image_dimensions", return_value=(4096, 3999)), patch(
            "lumen.library.analyze_subject_regions", return_value=None
        ):
            flush = {"left": .5, "top": 0, "width": .5 + 2 ** -52, "height": 1}  # an edge-flush drag one float step past 1
            self.assertEqual(1 + 2 ** -52, flush["left"] + flush["width"])
            code, chosen = self.post_json("/api/photos/1/crop", {"candidate_id": "crop-manual", "rect": flush, "aspect": "free"})
            self.assertEqual(200, code, chosen)
            self.assertEqual(.5, self.get_json("/api/photos/1/crops")[1]["active_crop"]["rect"]["width"])
            # 2000.5 x 3999 source px: half up, as the browser's Math.round, keeps 2001 x 3999 = 8,001,999 px.
            half = {"left": 0, "top": 0, "width": 2000.5 / 4096, "height": 1}
            code, chosen = self.post_json("/api/photos/1/crop", {"candidate_id": "crop-manual", "rect": half, "aspect": "free"})
            self.assertEqual(200, code, chosen)
            for value in ("NaN", "Infinity"):
                with self.subTest(value=value):
                    raw = ('{"candidate_id": "crop-manual", "rect": {"left": 0, "top": 0, "width": %s, "height": 1}, "aspect": "free"}' % value).encode()
                    code, result = self.post_json("/api/photos/1/crop", None, raw=raw)
                    self.assertEqual(400, code, result)
                    self.assertIn("error", result)

    def test_crop_mutations_reject_invalid_stale_original_and_extra_fields_without_change(self):
        with patch("lumen.library.read_image_dimensions", return_value=(4000, 3000)) as dimensions, patch(
            "lumen.library.analyze_subject_regions", return_value=None
        ):
            with urllib.request.urlopen(self.base + "/api/photos/1/crops", timeout=3) as response:
                candidate = next(item for item in json.load(response)["candidates"] if item["kind"] != "original")
            self.assertEqual(200, self.post_json("/api/photos/1/crop", {"candidate_id": candidate["id"]})[0])
            invalid = (
                {}, {"candidate_id": None}, {"candidate_id": 1}, {"candidate_id": "crop-original"},
                {"candidate_id": "crop-0000000000000000"},
                {"candidate_id": candidate["id"], "author": "omlx-assisted"},
            )
            for payload in invalid:
                with self.subTest(payload=payload):
                    code, result = self.post_json("/api/photos/1/crop", payload)
                    self.assertEqual(400, code)
                    self.assertIn("error", result)
            dimensions.return_value = (5000, 3000)
            code, result = self.post_json("/api/photos/1/crop", {"candidate_id": candidate["id"]})
            self.assertEqual(400, code, result)
            self.assertIn("error", result)
            dimensions.return_value = (4000, 3000)
            for route, payload in (
                ("/api/photos/1/crop/clear", {"candidate_id": candidate["id"]}),
                ("/api/photos/1/composition-review", {"candidate_id": candidate["id"]}),
            ):
                code, result = self.post_json(route, payload)
                self.assertEqual(400, code)
                self.assertIn("error", result)
            with Library(Settings(library_dir=self.root / "library")) as library:
                self.assertEqual(candidate["id"], library.catalog.get_crop(1).candidate_id)

    def test_composition_review_is_advisory_and_never_exposes_raw_response(self):
        class Tagger:
            model = "local-vision"

            def review_composition(self, original, candidates):
                return CompositionReview(candidates[0][0], "The original keeps useful context.", 0.75)

        with patch("lumen.library.OmlxTagger", return_value=Tagger()), patch("lumen.library.get_server_status", return_value=REVIEW_LOADED):
            code, payload = self.post_json("/api/photos/1/composition-review", {})
        self.assertEqual(200, code, payload)
        self.assertEqual({"candidate_id", "rationale", "confidence"}, set(payload))
        self.assertEqual("crop-original", payload["candidate_id"])
        with Library(Settings(library_dir=self.root / "library")) as library:
            self.assertIsNone(library.catalog.get_crop(1))

    def test_crops_payload_carries_the_stored_review_only_while_its_candidate_exists(self):
        class Tagger:
            model = "local-vision"

            def review_composition(self, original, candidates):
                return CompositionReview(candidates[0][0], "The original keeps useful context.", 0.75)

        code, crops = self.get_json("/api/photos/1/crops")
        self.assertEqual(200, code, crops)
        self.assertIsNone(crops["review"])
        with patch("lumen.library.OmlxTagger", return_value=Tagger()), patch("lumen.library.get_server_status", return_value=REVIEW_LOADED):
            self.assertEqual(200, self.post_json("/api/photos/1/composition-review", {})[0])
        code, crops = self.get_json("/api/photos/1/crops")
        self.assertEqual({"candidate_id": "crop-original", "rationale": "The original keeps useful context.",
                          "confidence": 0.75}, crops["review"])
        with Library(Settings(library_dir=self.root / "library")) as library:
            self.assertIsNone(library.latest_composition_review(1, ["nope"]))

    def test_composition_review_failure_is_json_and_preserves_active_crop(self):
        class BrokenTagger:
            model = "local-vision"

            def review_composition(self, original, candidates):
                raise OmlxError("vision model unavailable")

        with patch("lumen.library.read_image_dimensions", return_value=(4000, 3000)), patch(
            "lumen.library.analyze_subject_regions", return_value=None
        ):
            with urllib.request.urlopen(self.base + "/api/photos/1/crops", timeout=3) as response:
                candidate = next(item for item in json.load(response)["candidates"] if item["kind"] != "original")
            self.assertEqual(200, self.post_json("/api/photos/1/crop", {"candidate_id": candidate["id"]})[0])
            with patch("lumen.library.OmlxTagger", return_value=BrokenTagger()), patch("lumen.library.get_server_status", return_value=REVIEW_LOADED):
                code, payload = self.post_json("/api/photos/1/composition-review", {})
        self.assertEqual(400, code, payload)
        self.assertEqual("local composition review is unavailable for this photo", payload["error"])
        self.assertNotIn("raw_response", payload)
        with Library(Settings(library_dir=self.root / "library")) as library:
            self.assertEqual(candidate["id"], library.catalog.get_crop(1).candidate_id)

    def test_develop_suggest_is_advisory_and_validates_its_payload(self):
        class Tagger:
            model, role = "local-vision", "review"

            def suggest_develop(self, preview, *, settings, histogram_note, feedback=None):
                self.feedback = feedback
                return DevelopAdvice({"exposure_ev": 0.4}, "Nothing in the frame reaches white.", '{"secret": 1}')

        tagger = Tagger()
        render = lambda source, destination, executable, **kwargs: (
            Image.new("RGB", (64, 48), (90, 90, 90)).save(destination, format="JPEG"),
            DevelopRender(path=Path(destination), width=64, height=48, histogram={}, defaults={},
                          lens_correction_supported=False, neutral_temperature=5500.0, neutral_tint=0.0))[1]
        with patch("lumen.library.render_raw", side_effect=render), patch(
                "lumen.library.OmlxTagger", return_value=tagger), patch(
                "lumen.library.get_server_status", return_value=REVIEW_LOADED):
            code, payload = self.post_json("/api/photos/1/develop/suggest",
                                           {"settings": {"contrast": 0.6}, "feedback": "too warm"})
            self.assertEqual(200, code, payload)
            self.assertEqual({"settings", "rationale"}, set(payload), "no raw model output reaches the browser")
            self.assertEqual({"exposure_ev": 0.4}, payload["settings"])
            self.assertEqual("too warm", tagger.feedback)

            for bad in ({"settings": {}}, {"feedback": None}, {"settings": {}, "feedback": 3},
                        {"settings": {}, "feedback": "x" * 201}, {"settings": {"vibrance": 1}, "feedback": None},
                        {"settings": [], "feedback": None}, {"settings": {}, "feedback": None, "long_edge": 512}):
                with self.subTest(bad=bad):
                    code, result = self.post_json("/api/photos/1/develop/suggest", bad)
                    self.assertEqual(400, code, result)
                    self.assertIn("error", result)

        with Library(Settings(library_dir=self.root / "library")) as library:
            self.assertIsNone(library.develop_for(1), "a suggestion saves nothing")

    def test_photo_payload_carries_the_last_develop_advice(self):
        class Tagger:
            model, role = "local-vision", "review"

            def suggest_develop(self, preview, *, settings, histogram_note, feedback=None):
                return DevelopAdvice({"exposure_ev": 0.5}, "lift", '{"secret": 1}')

        render = lambda source, destination, executable, **kwargs: (
            Image.new("RGB", (64, 48), (90, 90, 90)).save(destination, format="JPEG"),
            DevelopRender(path=Path(destination), width=64, height=48, histogram={}, defaults={},
                          lens_correction_supported=False, neutral_temperature=5500.0, neutral_tint=0.0))[1]
        self.assertIsNone(self.get_json("/api/photos/1")[1]["advice"])
        with patch("lumen.library.render_raw", side_effect=render), patch(
                "lumen.library.OmlxTagger", return_value=Tagger()), patch(
                "lumen.library.get_server_status", return_value=REVIEW_LOADED):
            code, payload = self.post_json("/api/photos/1/develop/suggest", {"settings": {}, "feedback": None})
        self.assertEqual(200, code, payload)
        advice = self.get_json("/api/photos/1")[1]["advice"]
        self.assertEqual({"settings": {"exposure_ev": 0.5}, "rationale": "lift"}, advice)

    def test_develop_suggest_failure_is_json_without_the_model_text(self):
        class BrokenTagger:
            model, role = "local-vision", "review"

            def suggest_develop(self, preview, *, settings, histogram_note, feedback=None):
                raise OmlxError("oMLX did not return valid JSON: Expecting value")

        render = lambda source, destination, executable, **kwargs: (
            Image.new("RGB", (64, 48), (90, 90, 90)).save(destination, format="JPEG"),
            DevelopRender(path=Path(destination), width=64, height=48, histogram={}, defaults={},
                          lens_correction_supported=False, neutral_temperature=5500.0, neutral_tint=0.0))[1]
        with patch("lumen.library.render_raw", side_effect=render), patch(
                "lumen.library.OmlxTagger", return_value=BrokenTagger()), patch(
                "lumen.library.get_server_status", return_value=REVIEW_LOADED):
            code, payload = self.post_json("/api/photos/1/develop/suggest", {"settings": {}, "feedback": None})
        self.assertEqual(400, code, payload)
        self.assertNotIn("raw_response", payload)
        self.assertNotIn(str(self.root), payload["error"])

    def test_crop_routes_report_unknown_photos_as_json(self):
        for method, path, payload in (
            ("GET", "/api/photos/999/crops", None),
            ("POST", "/api/photos/999/crop", {"candidate_id": "crop-0000000000000000"}),
            ("POST", "/api/photos/999/crop/clear", {}),
            ("POST", "/api/photos/999/composition-review", {}),
        ):
            with self.subTest(path=path):
                if method == "GET":
                    request = urllib.request.Request(self.base + path)
                    try:
                        response = urllib.request.urlopen(request, timeout=3)
                    except urllib.error.HTTPError as error:
                        response = error
                    with response:
                        self.assertEqual(400, response.code)
                        self.assertIn("error", json.load(response))
                else:
                    code, result = self.post_json(path, payload)
                    self.assertEqual(400, code)
                    self.assertIn("error", result)

    def test_crop_errors_never_expose_source_or_preview_paths(self):
        source = self.root / "photo.jpg"
        preview = None
        with patch("lumen.library.read_image_dimensions", return_value=(4000, 3000)), patch(
            "lumen.library.analyze_subject_regions", return_value=None
        ):
            with urllib.request.urlopen(self.base + "/api/photos/1/crops", timeout=3) as response:
                candidate = next(item for item in json.load(response)["candidates"] if item["kind"] != "original")
            self.assertEqual(200, self.post_json("/api/photos/1/crop", {"candidate_id": candidate["id"]})[0])
        with Library(Settings(library_dir=self.root / "library")) as library:
            preview = Path(library.catalog.get_photo(1).preview_path)
        source.unlink()

        requests = []
        try:
            requests.append(("GET", "/api/photos/1/crops", None, "crop choices are unavailable for this photo"))
            requests.append(("POST", "/api/photos/1/crop", {"candidate_id": candidate["id"]}, "crop could not be selected for this photo"))
            requests.append(("POST", "/api/photos/1/composition-review", {}, "local composition review is unavailable for this photo"))
            for method, path, body, message in requests:
                with self.subTest(path=path), patch("lumen.library.get_server_status", return_value=REVIEW_LOADED):
                    if method == "POST":
                        code, payload = self.post_json(path, body)
                    else:
                        try:
                            response = urllib.request.urlopen(self.base + path, timeout=3)
                        except urllib.error.HTTPError as error:
                            response = error
                        with response:
                            code, payload = response.code, json.load(response)
                    self.assertEqual(400, code)
                    self.assertEqual(message, payload["error"])
                    rendered = json.dumps(payload)
                    self.assertNotIn(source.name, rendered)
                    self.assertNotIn(str(source.parent), rendered)
                    self.assertNotIn(str(preview), rendered)

            with patch("lumen.web.Library.clear_crop", side_effect=OSError(str(preview))):
                code, payload = self.post_json("/api/photos/1/crop/clear", {})
            self.assertEqual(400, code)
            self.assertEqual("crop could not be cleared for this photo", payload["error"])
            self.assertNotIn(str(preview), json.dumps(payload))
        finally:
            Image.new("RGB", (120, 90), (80, 100, 120)).save(source)

    def test_compose_panel_has_native_controls_fallback_and_crop_treatment(self):
        html = urllib.request.urlopen(self.base + "/", timeout=3).read().decode()
        parser = LandmarkParser()
        parser.feed(html)
        by_id = {node["attrs"]["id"]: node for node in parser.nodes if "id" in node["attrs"]}
        self.assertIn(by_id["loupe"], by_id["compose-workspace"]["parents"])
        self.assertIn("the RAW stays untouched", by_id["loupe"]["text"])
        self.assertEqual("button", by_id["ask-composition-ai"]["tag"])
        self.assertEqual("button", by_id["ask-composition-ai"]["attrs"].get("type"))
        self.assertEqual("details", by_id["more-crop-shapes"]["tag"])
        self.assertEqual("polite", by_id["compose-status"]["attrs"].get("aria-live"))
        self.assertIn("Keep original", html)
        self.assertIn("Use this crop", html)
        self.assertIn("More crop shapes", html)

        javascript = (Path(__file__).resolve().parents[1] / "src" / "lumen" / "static" / "app.js").read_text()
        styles = (Path(__file__).resolve().parents[1] / "src" / "lumen" / "static" / "styles.css").read_text()
        self.assertIn("rule-of-thirds", javascript)
        self.assertIn(".rule-of-thirds", styles)
        self.assertRegex(styles, r"\.crop-card\.active\.recommended\s*\{[^}]*border-color:\s*var\(--amber\)[^}]*var\(--cyan\)")
        self.assertIn("resolution_warning", javascript)
        self.assertNotRegex(javascript, r"composition[^\n]*\.innerHTML|rationale[^\n]*\.innerHTML")
        self.assertIn("loadComposition", javascript)

    def test_tag_scope_route_gates_on_live_model_and_never_creates_a_job_when_refused(self):  # Review Focus
        class FakeTagger:
            model, role = "fast-model", "fast"
            def __init__(self, *args, **kwargs): self.role = kwargs.get("role", "review")
            def tag(self, preview, *, vocabulary=None, place=None):
                from lumen.omlx import TagResult
                return TagResult("A frame", ["ice"], "", [], 0.9, "{}", species=[], conditions=[])

        offline = {"version": None, "default_model": None, "loaded_models": []}
        with patch("lumen.library.get_server_status", return_value=offline):
            code, body = self.post_json("/api/photos/tag", {"scope": "keepers"})
            self.assertEqual((400, {"error": "fast model Qwen3-VL-8B-Instruct-4bit is not loaded in oMLX"}), (code, body))
            self.assertEqual([], self.get_json("/api/jobs")[1]["jobs"])
        loaded = {"version": None, "default_model": None, "loaded_models": ["Qwen3-VL-8B-Instruct-4bit"]}
        Image.new("RGB", (120, 90)).save(self.root / "photo.jpg")
        # /api/status reads oMLX through diagnostics; patch it too so the test never contacts a real server.
        with patch("lumen.library.get_server_status", return_value=loaded), patch("lumen.diagnostics.get_server_status", return_value=loaded), patch("lumen.library.OmlxTagger", FakeTagger):
            self.wait_job(self.post_json("/api/import", {"path": str(self.root / "photo.jpg")})[1]["job_id"])
            for payload in ({"scope": "some"}, {"scope": [1, 1]}, {"scope": ["1"]}, {"scope": "keepers", "extra": 1}, {}):
                with self.subTest(payload=payload):
                    self.assertEqual(400, self.post_json("/api/photos/tag", payload)[0])
            code, job = self.post_json("/api/photos/tag", {"scope": "keepers"})
            self.assertEqual(202, code)
            final = self.wait_job(job["job_id"])
            self.assertEqual("succeeded", final["status"])
            self.assertNotIn("raw_response", json.dumps(final))
            code, status = self.get_json("/api/status")
            self.assertEqual({"model": "Qwen3-VL-8B-Instruct-4bit", "loaded": True}, status["omlx_roles"]["fast"])
            self.assertFalse(status["omlx_roles"]["review"]["loaded"])
            code, vocab = self.get_json("/api/vocabulary")
            self.assertIn("wildlife", vocab["categories"])
            photo = self.get_json("/api/photos/1")[1]
            self.assertEqual(["ice"], photo["tags"]["data"]["subjects"])
            self.assertNotIn("raw_response", json.dumps(photo))
            self.assertEqual(403, self.post_json("/api/photos/1/keywords", {"action": "remove", "keyword": "ice"}, headers={"Origin": "https://attacker.example"})[0])
            self.assertEqual(400, self.post_json("/api/photos/1/keywords", {"action": "drop", "keyword": "ice"})[0])
            code, body = self.post_json("/api/photos/1/keywords", {"action": "remove", "keyword": "ice"})
            self.assertEqual((200, [], ["ice"]), (code, body["subjects"], body["suggested"]))
            code, body = self.post_json("/api/photos/1/keywords", {"action": "add", "keyword": "Lion"})
            self.assertEqual((200, ["Lion"], ["ice"]), (code, body["subjects"], body["suggested"]))
            self.assertEqual(400, self.post_json("/api/photos/1/keywords", {"action": "add", "keyword": "#bad"})[0])

    def test_vocabulary_reports_counts_and_forget_strips_keyword(self):
        with Library(Settings(library_dir=self.root / "library")) as library:
            photo_id = library.catalog.list_photos()[0].id
            library.catalog.save_tags(photo_id, {"subjects": ["Ice", "bay"], "suggested": []}, "fast-model")
        code, vocab = self.get_json("/api/vocabulary")
        self.assertEqual((200, 1, []), (code, vocab["counts"]["ice"], vocab["blocked"]))
        for payload in ({}, {"word": "  "}, {"word": 3}, {"word": "x", "extra": 1}):
            with self.subTest(payload=payload):
                self.assertEqual(400, self.post_json("/api/vocabulary/forget", payload)[0])
        code, body = self.post_json("/api/vocabulary/forget", {"word": "ICE"})
        self.assertEqual((200, {"word": "ice", "removed_from": 1}), (code, body))
        code, vocab = self.get_json("/api/vocabulary")
        self.assertEqual((["ice"], None), (vocab["blocked"], vocab["counts"].get("ice")))
        self.assertEqual(["bay"], self.get_json(f"/api/photos/{photo_id}")[1]["tags"]["data"]["subjects"])

    def test_first_run_card_offers_ingest_and_import_under_the_pixel_mark(self):
        response = urllib.request.urlopen(self.base + "/static/mark.svg", timeout=3)
        self.assertEqual("image/svg+xml", response.headers["Content-Type"])
        svg = response.read().decode()
        self.assertIn('shape-rendering="crispEdges"', svg)
        self.assertEqual(svg, (Path(__file__).parent.parent / "src/lumen/static/mark.svg").read_text())
        js = (Path(__file__).parent.parent / "src/lumen/static/app.js").read_text()
        self.assertIn("/static/mark.svg", js)
        for phrase in ("Photos still on a memory card", "Photos already on a drive or folder", "Not sure?"):
            self.assertIn(phrase, js)
        styles = (Path(__file__).parent.parent / "src/lumen/static/styles.css").read_text()
        self.assertIn(".first-run", styles)

    def test_assist_panel_has_keeper_tag_button_and_inspector_lists_keywords_with_add_remove(self):
        html = (Path(__file__).parent.parent / "src/lumen/static/index.html").read_text()
        self.assertIn('id="tag-keepers"', html)
        js = (Path(__file__).parent.parent / "src/lumen/static/app.js").read_text()
        self.assertIn("omlx_roles", js)
        self.assertIn("suggested-keywords", js)

    def test_reject_suggestion_routes_gate_validate_and_decide_by_ids(self):
        class FakeTagger:
            def __init__(self, *args, **kwargs): self.model, self.role = "fast-model", kwargs.get("role", "review")
            def review_rejects(self, preview):
                from lumen.omlx import RejectReview
                return RejectReview([{"kind": "misfire", "confidence": 0.9}], True, 0.9, "{}")

        loaded = {"version": None, "default_model": None, "loaded_models": ["Qwen3-VL-8B-Instruct-4bit"]}
        for index in range(2):
            Image.new("RGB", (120, 90), (index, 0, 0)).save(self.root / f"p{index}.jpg")
        with patch("lumen.library.get_server_status", return_value=loaded), patch("lumen.library.OmlxTagger", FakeTagger):
            self.wait_job(self.post_json("/api/import", {"path": str(self.root)})[1]["job_id"])
            code, job = self.post_json("/api/photos/review-rejects", {"scope": "keepers"})
            self.assertEqual(202, code)
            self.assertEqual("succeeded", self.wait_job(job["job_id"])["status"])
            code, body = self.get_json("/api/photos/reject-suggestions")
            self.assertEqual(3, body["count"])  # setUp's photo.jpg is a keeper too, beside the two new frames
            self.assertNotIn("raw_response", json.dumps(body))
            self.assertTrue(all(s["preview_url"].startswith("/preview/") for s in body["suggestions"]))
            ids = [s["photo_id"] for s in body["suggestions"]]
            self.assertEqual(403, self.post_json("/api/photos/reject-suggestions/decide", {"photo_ids": ids, "decision": "reject"}, headers={"Origin": "https://attacker.example"})[0])
            for payload in ({"photo_ids": ids}, {"photo_ids": [], "decision": "reject"}, {"photo_ids": ids, "decision": "reject", "count": 2}, {"photo_ids": [ids[0], 99], "decision": "reject"}):
                with self.subTest(payload=payload):
                    self.assertEqual(400, self.post_json("/api/photos/reject-suggestions/decide", payload)[0])
            self.assertEqual("none", self.get_json(f"/api/photos/{ids[0]}")[1]["photo"]["decision"])
            code, body = self.post_json("/api/photos/reject-suggestions/decide", {"photo_ids": ids, "decision": "reject"})
            self.assertEqual((200, sorted(ids), 0), (code, sorted(d["photo_id"] for d in body["decided"]), body["remaining"]))
            self.assertTrue(all(d["decision"] == "reject" and "automatic_pick_id" in d and "group_id" in d for d in body["decided"]))
        with patch("lumen.library.get_server_status", return_value={"version": None, "default_model": None, "loaded_models": []}):
            self.assertEqual(400, self.post_json("/api/photos/review-rejects", {"scope": "keepers"})[0])
            self.assertFalse(any(j["kind"] == "review_rejects" and j["status"] in ("queued", "running") for j in self.get_json("/api/jobs")[1]["jobs"]))

    def test_assist_route_runs_one_job_over_keepers_and_needs_both_roles(self):
        from lumen.omlx import Caption, RejectReview, TagResult

        class FakeTagger:
            def __init__(self, *args, **kwargs): self.model, self.role = kwargs.get("role") + "-model", kwargs.get("role")
            def tag(self, preview, *, vocabulary=None, place=None): return TagResult("d", ["ice"], "", [], 0.9, "{}", species=[], conditions=[])
            def review_rejects(self, preview): return RejectReview([], False, 0.8, "{}")
            def caption(self, preview, *, place=None): return Caption("Cap.", "Alt.", "{}")

        both = {"version": None, "default_model": None, "loaded_models": ["Qwen3-VL-8B-Instruct-4bit", "Qwen3.8-27B-4bit"]}
        with patch("lumen.library.get_server_status", return_value=both), patch("lumen.library.OmlxTagger", FakeTagger):
            for payload in ({}, {"scope": "keepers", "retag": True}, {"scope": "nope"}, {"scope": "keepers", "redo": "yes"}):
                with self.subTest(payload=payload):
                    self.assertEqual(400, self.post_json("/api/photos/assist", payload)[0])
            code, job = self.post_json("/api/photos/assist", {"scope": "keepers", "redo": True})
            self.assertEqual((202, "assist"), (code, job["kind"]))
            done = self.wait_job(job["job_id"])
            self.assertEqual("succeeded", done["status"])
            self.assertEqual({"tagged": 1, "reviewed": 1, "captioned": 1, "skipped": 0, "failed": 0, "suggested": 0}, done["result"])
            tags = self.get_json("/api/photos/1")[1]["tags"]["data"]
            self.assertEqual(("d", "Cap.", "Alt."), (tags["description"], tags["caption"], tags["alt_text"]))
        fast_only = {"version": None, "default_model": None, "loaded_models": ["Qwen3-VL-8B-Instruct-4bit"]}
        with patch("lumen.library.get_server_status", return_value=fast_only), patch("lumen.library.OmlxTagger", FakeTagger):
            code, body = self.post_json("/api/photos/assist", {"scope": "keepers"})
            self.assertEqual(400, code)
            self.assertIn("review model Qwen3.8-27B-4bit is not loaded", body["error"])
            self.assertFalse(any(j["kind"] == "assist" and j["status"] in ("queued", "running") for j in self.get_json("/api/jobs")[1]["jobs"]))

    def test_review_panel_has_native_rows_keys_and_counted_confirmation(self):
        html = (Path(__file__).parent.parent / "src/lumen/static/index.html").read_text()
        for needle in ('id="review-tab"', 'id="review-panel"', 'id="scan-rejects"', 'id="reject-list"', 'id="reject-all-shown"', 'id="reject-all-confirm"'):
            self.assertIn(needle, html)
        js = (Path(__file__).parent.parent / "src/lumen/static/app.js").read_text()
        self.assertIn("Reject all ${", js)

    def test_javascript_learned_keywords_panel_lists_and_forgets(self):
        html = (Path(__file__).parent.parent / "src/lumen/static/index.html").read_text()
        for needle in ('<details id="vocabulary-panel">', 'id="vocabulary-count"', '<ul id="vocabulary-list" aria-label="Learned keywords">'):
            self.assertIn(needle, html)
        self.run_javascript_cleanup_regression("vocabulary")

    def test_javascript_review_panel_decides_rows_by_button_and_key_and_confirms_count(self):
        self.run_javascript_cleanup_regression("review-rejects")

    def test_busy_port_reports_the_bind_error(self):
        with self.assertRaises(OSError):  # not an AttributeError from server_close running before __init__ finished
            make_server(Settings(library_dir=self.root / "other"), port=self.server.server_address[1])

    def test_role_model_switch_moves_either_role_between_the_two_models_only(self):
        both = {"version": None, "default_model": None, "loaded_models": ["Qwen3-VL-8B-Instruct-4bit", "Qwen3.8-27B-4bit"]}
        with patch("lumen.diagnostics.get_server_status", return_value=both):
            status = self.get_json("/api/status")[1]
            self.assertEqual(["Qwen3.8-27B-4bit", "Qwen3-VL-8B-Instruct-4bit"], status["review_models"])
            self.assertEqual("Qwen3.8-27B-4bit", status["omlx_roles"]["review"]["model"])
            self.assertEqual("Qwen3-VL-8B-Instruct-4bit", status["omlx_roles"]["fast"]["model"])
            for payload in ({}, {"model": "Qwen3-VL-8B-Instruct-4bit"}, {"role": "review", "model": "Other-Model"},
                            {"role": "other", "model": "Qwen3-VL-8B-Instruct-4bit"},
                            {"role": "review", "model": "Qwen3-VL-8B-Instruct-4bit", "extra": 1}, {"role": "review", "model": 1}):
                with self.subTest(payload=payload):
                    self.assertEqual(400, self.post_json("/api/role-model", payload)[0])
            self.assertEqual(403, self.post_json("/api/role-model", {"role": "review", "model": "Qwen3-VL-8B-Instruct-4bit"},
                                                 headers={"Origin": "https://attacker.example"})[0])
            self.assertEqual((200, {"role": "review", "model": "Qwen3-VL-8B-Instruct-4bit"}),
                             self.post_json("/api/role-model", {"role": "review", "model": "Qwen3-VL-8B-Instruct-4bit"}))
            self.assertEqual((200, {"role": "fast", "model": "Qwen3.8-27B-4bit"}),
                             self.post_json("/api/role-model", {"role": "fast", "model": "Qwen3.8-27B-4bit"}))
            status = self.get_json("/api/status")[1]
            self.assertEqual("Qwen3-VL-8B-Instruct-4bit", status["omlx_roles"]["review"]["model"])
            self.assertEqual("Qwen3.8-27B-4bit", status["omlx_roles"]["fast"]["model"])
            self.assertEqual(["Qwen3.8-27B-4bit", "Qwen3-VL-8B-Instruct-4bit"], status["review_models"])  # the choice survives a switch
        with patch("lumen.library.get_server_status", return_value=both), patch("lumen.library.Library.caption_photo",
                                                                                  lambda self, photo_id, tagger: {"model": tagger.model}):
            self.assertEqual((200, {"model": "Qwen3-VL-8B-Instruct-4bit"}), self.post_json("/api/photos/1/caption", {}))

    def test_critique_routes_gate_on_review_model_and_never_change_the_pick(self):
        class FakeTagger:
            def __init__(self, *args, **kwargs): self.model, self.role = "review-model", kwargs.get("role", "review")
            def critique_burst(self, frames):
                from lumen.omlx import BurstCritique
                return BurstCritique(frames[-1][0], "Best timing.", 0.72, "{}")

        for index in range(3):
            image = Image.new("RGB", (120, 90), (index * 30, 0, 0)); exif = Image.Exif(); exif[36867] = f"2026:10:22 20:00:0{index}"
            image.save(self.root / f"b{index}.jpg", exif=exif)
        fast_only = {"version": None, "default_model": None, "loaded_models": ["Qwen3-VL-8B-Instruct-4bit"]}
        both = {"version": None, "default_model": None, "loaded_models": ["Qwen3-VL-8B-Instruct-4bit", "Qwen3.8-27B-4bit"]}
        with patch("lumen.library.get_server_status", return_value=fast_only), patch("lumen.library.OmlxTagger", FakeTagger):
            self.wait_job(self.post_json("/api/import", {"path": str(self.root)})[1]["job_id"])
            group = self.get_json("/api/groups")[1]["groups"][0]
            self.assertEqual((400, {"error": "review model Qwen3.8-27B-4bit is not loaded in oMLX"}), self.post_json(f"/api/groups/{group['id']}/critique", {}))
            self.assertEqual({"critique": None}, self.get_json(f"/api/groups/{group['id']}/critique")[1])
        with patch("lumen.library.get_server_status", return_value=both), patch("lumen.library.OmlxTagger", FakeTagger):
            self.assertEqual(403, self.post_json(f"/api/groups/{group['id']}/critique", {}, headers={"Origin": "https://attacker.example"})[0])
            self.assertEqual(400, self.post_json(f"/api/groups/{group['id']}/critique", {"pick": True})[0])
            self.assertEqual(404, self.post_json("/api/groups/999/critique", {})[0])
            code, body = self.post_json(f"/api/groups/{group['id']}/critique", {})
            self.assertEqual(200, code)
            self.assertEqual({"group_id", "photo_id", "name", "rationale", "confidence", "candidate_ids"}, set(body))
            self.assertEqual(group["automatic_pick_id"], self.get_json("/api/groups")[1]["groups"][0]["automatic_pick_id"])
            self.assertEqual(body, self.get_json(f"/api/groups/{group['id']}/critique")[1]["critique"])

    def test_triage_routes_and_groups_cue_never_switch_mode_or_change_exclusions(self):
        class FakeTagger:
            def __init__(self, *args, **kwargs): self.model, self.role = "fast-model", kwargs.get("role", "review")
            def triage_astro(self, preview):
                from lumen.omlx import AstroTriage
                return AstroTriage("night_sky", 0.9, [], "{}")

        for index in range(3):
            image = Image.new("RGB", (120, 90), (0, 0, index * 20)); exif = Image.Exif(); exif[36867] = f"2026:10:22 22:00:{index * 10:02d}"; exif[33434] = 15
            image.save(self.root / f"l{index}.jpg", exif=exif)
        loaded = {"version": None, "default_model": None, "loaded_models": ["Qwen3-VL-8B-Instruct-4bit"]}
        with patch("lumen.library.get_server_status", return_value=loaded), patch("lumen.library.OmlxTagger", FakeTagger):
            self.wait_job(self.post_json("/api/import", {"path": str(self.root)})[1]["job_id"])
            group = self.get_json("/api/groups")[1]["groups"][0]
            self.assertFalse(group["astro_cue"])
            self.assertEqual(403, self.post_json(f"/api/groups/{group['id']}/triage", {}, headers={"Origin": "https://attacker.example"})[0])
            self.assertEqual(400, self.post_json(f"/api/groups/{group['id']}/triage", {"all": True})[0])
            code, job = self.post_json(f"/api/groups/{group['id']}/triage", {})
            self.assertEqual(202, code)
            self.assertEqual("succeeded", self.wait_job(job["job_id"])["status"])
            self.assertTrue(self.get_json("/api/groups")[1]["groups"][0]["astro_cue"])
            code, triage = self.get_json(f"/api/groups/{group['id']}/triage")
            self.assertEqual(3, len(triage["lights"]))
            self.assertNotIn("raw_response", json.dumps(triage))
        with patch("lumen.library.get_server_status", return_value={"version": None, "default_model": None, "loaded_models": []}):
            code, body = self.post_json(f"/api/groups/{group['id']}/triage", {})
            self.assertEqual((400, "fast model Qwen3-VL-8B-Instruct-4bit is not loaded in oMLX"), (code, body["error"]))
            self.assertEqual(404, self.get_json("/api/groups/999/triage")[0])

    def test_astro_panel_has_triage_text_and_counted_exclude_confirmation(self):
        html = (Path(__file__).parent.parent / "src/lumen/static/index.html").read_text()
        for needle in ('id="triage-astro"', 'id="exclude-flagged"', 'id="exclude-confirm"'):
            self.assertIn(needle, html)

    def test_javascript_astro_triage_excludes_only_after_confirmation_and_cue_is_text(self):
        self.run_javascript_cleanup_regression("astro-triage")

    def test_javascript_advice_has_status_and_survives_leaving_the_frame(self):
        self.run_javascript_cleanup_regression("advice-status")

    def test_javascript_burst_critique_shows_advice_and_only_use_this_pick_changes_the_pick(self):
        self.run_javascript_cleanup_regression("critique")

    def test_search_expand_route_returns_terms_only_and_refuses_without_the_fast_model(self):
        class FakeTagger:
            def __init__(self, *args, **kwargs): self.model, self.role = "fast-model", kwargs.get("role", "review")
            def expand_query(self, query, vocabulary): return ["tern", "gull"]

        offline = {"version": None, "default_model": None, "loaded_models": []}
        with patch("lumen.library.get_server_status", return_value=offline):
            self.assertEqual((400, {"error": "fast model Qwen3-VL-8B-Instruct-4bit is not loaded in oMLX"}),
                             self.post_json("/api/search/expand", {"query": "seabirds"}))
            self.assertEqual([], self.get_json("/api/jobs")[1]["jobs"])
        loaded = {"version": None, "default_model": None, "loaded_models": ["Qwen3-VL-8B-Instruct-4bit"]}
        with patch("lumen.library.get_server_status", return_value=loaded), patch("lumen.library.OmlxTagger", FakeTagger):
            self.assertEqual((200, {"query": "seabirds", "terms": ["tern", "gull"], "text": "seabirds | tern | gull"}),
                             self.post_json("/api/search/expand", {"query": "seabirds"}))
            for payload in ({"query": ""}, {"query": "x" * 81}, {"query": "a", "extra": 1}, {}, {"query": 3}):
                with self.subTest(payload=payload):
                    self.assertEqual(400, self.post_json("/api/search/expand", payload)[0])
            self.assertEqual([], self.get_json("/api/jobs")[1]["jobs"])

    def test_caption_routes_gate_validate_and_inspector_has_native_inputs(self):
        class FakeTagger:
            def __init__(self, *args, **kwargs): self.model, self.role = "review-model", kwargs.get("role", "review")
            def caption(self, preview, *, place=None):
                from lumen.omlx import Caption
                return Caption("A caption.", "Alt text.", "{}")

        Image.new("RGB", (120, 90)).save(self.root / "p.jpg")
        both = {"version": None, "default_model": None, "loaded_models": ["Qwen3.8-27B-4bit"]}
        with patch("lumen.library.get_server_status", return_value=both), patch("lumen.library.OmlxTagger", FakeTagger):
            self.wait_job(self.post_json("/api/import", {"path": str(self.root / "p.jpg")})[1]["job_id"])
            self.assertEqual(403, self.post_json("/api/photos/1/caption", {}, headers={"Origin": "https://attacker.example"})[0])
            self.assertEqual(400, self.post_json("/api/photos/1/caption", {"style": "fun"})[0])
            self.assertEqual((200, {"photo_id": 1, "caption": "A caption.", "alt_text": "Alt text."}), self.post_json("/api/photos/1/caption", {}))
            for payload in ({"caption": "x" * 201, "alt_text": None}, {"caption": "a"}, {"caption": "a", "alt_text": "b", "tone": "x"}, {"caption": 1, "alt_text": None}):
                with self.subTest(payload=payload):
                    self.assertEqual(400, self.post_json("/api/photos/1/caption/edit", payload)[0])
            self.assertEqual("Edited.", self.post_json("/api/photos/1/caption/edit", {"caption": "Edited.", "alt_text": None})[1]["caption"])
            self.assertEqual("Edited.", self.get_json("/api/photos/1")[1]["tags"]["data"]["caption"])
        with patch("lumen.library.get_server_status", return_value={"version": None, "default_model": None, "loaded_models": []}):
            self.assertEqual((400, {"error": "review model Qwen3.8-27B-4bit is not loaded in oMLX"}), self.post_json("/api/photos/1/caption", {}))
        html = (Path(__file__).parent.parent / "src/lumen/static/index.html").read_text()
        js = (Path(__file__).parent.parent / "src/lumen/static/app.js").read_text()
        self.assertIn("caption-input", js); self.assertIn("alt-input", js); self.assertIn("maxLength = 125", js)

    def test_javascript_caption_inputs_save_and_never_trigger_culling_keys(self):
        self.run_javascript_cleanup_regression("caption")

    def test_javascript_keywords_add_and_remove_without_reload_and_tag_keepers_gates_on_role(self):
        self.run_javascript_cleanup_regression("keywords")

    def test_javascript_typed_keyword_posts_add_and_refuses_duplicates(self):
        self.run_javascript_cleanup_regression("keyword-typed")

    def test_javascript_tag_keepers_disabled_when_fast_model_unloaded(self):
        self.run_javascript_cleanup_regression("keywords-unloaded")

    def test_digest_route_confirms_overwrite_and_degrades_without_review_model(self):
        for index in range(2):
            image = Image.new("RGB", (120, 90), (index * 40, 0, 0)); exif = Image.Exif(); exif[36867] = f"2026:10:22 10:00:0{index}"
            image.save(self.root / f"d{index}.jpg", exif=exif)
        none = {"version": None, "default_model": None, "loaded_models": []}
        with patch("lumen.library.get_server_status", return_value=none):
            self.wait_job(self.post_json("/api/import", {"path": str(self.root)})[1]["job_id"])
            self.assertEqual(403, self.post_json("/api/digests", {"date": "2026-10-22"}, headers={"Origin": "https://attacker.example"})[0])
            for payload in ({"date": "22/10/2026"}, {"date": "2026-10-22", "overwrite": "yes"}, {}, {"date": "2026-10-22", "model": "x"}):
                with self.subTest(payload=payload):
                    self.assertEqual(400, self.post_json("/api/digests", payload)[0])
            code, body = self.post_json("/api/digests", {"date": "2026-10-22"})
            self.assertEqual((200, {"date": "2026-10-22", "file": "2026-10-22.md", "frames": 2, "narrative": False, "overwritten": False}), (code, body))
            self.assertEqual((409, True), (lambda r: (r[0], r[1]["exists"]))(self.post_json("/api/digests", {"date": "2026-10-22"})))
            self.assertTrue(self.post_json("/api/digests", {"date": "2026-10-22", "overwrite": True})[1]["overwritten"])
            self.assertIn("digest_dir", self.get_json("/api/status")[1])
            text = (self.root / "library" / "digests" / "2026-10-22.md").read_text()
            self.assertIn("Narrative unavailable: review model Qwen3.8-27B-4bit is not loaded in oMLX", text)

    def test_digest_route_strips_absolute_paths_from_os_errors(self):
        none = {"version": None, "default_model": None, "loaded_models": []}
        failure = OSError("/Users/someone/Pictures/Lumen Library/digests: permission denied")
        with patch("lumen.library.get_server_status", return_value=none), patch("lumen.library.Library.digest_day", side_effect=failure):
            code, body = self.post_json("/api/digests", {"date": "2026-10-22"})
        self.assertEqual(400, code)
        self.assertNotIn("/Users", body["error"])
        self.assertIn("<path>", body["error"])

    def test_javascript_digest_button_confirms_overwrite_and_announces_result(self):
        html = (Path(__file__).parent.parent / "src/lumen/static/index.html").read_text()
        for needle in ('id="write-digest"', '<p id="digest-result"', '<dialog id="digest-confirm"', 'id="digest-confirm-yes"'):
            self.assertIn(needle, html)
        self.run_javascript_cleanup_regression("digest")

    def run_javascript_cleanup_regression(self, scenario):
        script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const harness = String.raw`
class Element {
  constructor() {
    this.children = []; this.attrs = {}; this.dataset = {}; this.style = { setProperty(name, value) { this[name] = value; } }; this.listeners = {}; this.value = ''; this.className = '';
    this.classList = {
      add: name => { if (!this.className.split(' ').includes(name)) this.className = (this.className + ' ' + name).trim(); },
      contains: name => this.className.split(' ').includes(name),
      remove: name => { this.className = this.className.split(' ').filter(item => item !== name).join(' '); },
    };
  }
  append(...children) { children.forEach(child => { if (child && typeof child === 'object') child.parent = this; }); this.children.push(...children); }
  replaceChildren(...children) { this.children.forEach(child => { if (child && typeof child === 'object') child.parent = null; }); this.children = []; this.append(...children); }
  contains(node) { for (; node; node = node.parent) if (node === this) return true; return false; }
  setAttribute(name, value) { this.attrs[name] = value; }
  getAttribute(name) { return this.attrs[name]; }
  removeAttribute(name) { delete this.attrs[name]; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  querySelector(selector) { return element('#import-form ' + selector); }
  closest(selector) {
    const test = { '.crop-card': node => node.classList.contains('crop-card'), '.reject-row': node => node.classList.contains('reject-row'), 'input, textarea, select': node => ['INPUT', 'TEXTAREA', 'SELECT'].includes(node.tagName), dialog: node => node.tagName === 'DIALOG', 'dialog form': node => node.tagName === 'FORM' && Boolean(node.parent?.closest('dialog')) }[selector];
    for (let node = this; node; node = node.parent) if (test?.(node)) return node;
    return null;
  }
  focus(options) { focused = this; this.focusOptions = options; }
  // A scenario sets box for a displayed size; otherwise scale 1: the width/height attributes, else the natural size (a fitted img smaller than its viewport).
  getBoundingClientRect() { return this.box || { left: 0, top: 0, width: this.width ?? this.naturalWidth, height: this.height ?? this.naturalHeight }; }
  setPointerCapture() {}
  scrollIntoView(options) { scrolled.push([this, options]); }
  scrollTo(options) { scrolled.push([this, options]); }
}
let focused = null;
const scrolled = [];
const stored = {};
function inspectorRating() {
  const tree = node => [node, ...(node.children || []).flatMap(tree)];
  return tree(element('#inspection')).find(node => node.id === 'rating') || null;
}
const elements = new Map();
// Attribute selectors walk the rendered trees in DOM order (rail, sheet, inspector), so hooks shared by cards and controls collide here as they would in a browser.
function byData(selector) {
  const match = /^\[data-([a-z-]+)(?:="([^"]*)")?\]$/.exec(selector);
  if (!match) return null;
  const key = match[1].replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
  const tree = node => [node, ...(node.children || []).flatMap(tree)];
  return ['#batch-list', '#contact-sheet', '#inspection'].flatMap(name => tree(element(name)))
    .filter(node => node.dataset && key in node.dataset && (match[2] === undefined || String(node.dataset[key]) === match[2]));
}
// Radios in one group are exclusive in a browser; the fake elements need it done by hand.
function pickStatus(value) {
  ['all', 'keepers', 'flag', 'none', 'reject'].forEach(name => { element('#status-' + name).checked = name === value; });
  return element('#status-' + value);
}
function element(selector) {
  if (!elements.has(selector)) elements.set(selector, new Element());
  return elements.get(selector);
}
const tabElements = ['add', 'review', 'assist', 'astro', 'export', 'clear'].map(name => {
  const tab = element('#' + name + '-tab');
  tab.id = name + '-tab';
  tab.setAttribute('aria-controls', name + '-panel');
  tab.setAttribute('aria-selected', String(name === 'add'));
  element('#' + name + '-panel').hidden = name !== 'add';  // as the markup ships it
  return tab;
});
const frameTabElements = ['frame', 'crop', 'develop', 'exif'].map((name, index) => {
  const tab = element('#' + name + '-tab');
  tab.id = name + '-tab';
  tab.setAttribute('aria-controls', name + '-panel');
  tab.setAttribute('aria-selected', String(index === 0));
  return tab;
});
const scope = new Element(); scope.value = 'current';
const presetRadios = ['full', 'share', 'web'].map((value, index) => Object.assign(new Element(), { value, checked: index === 0 }));
const webRadio = presetRadios[2];
const recipes = ['tracked', 'untracked'].map((value, index) => Object.assign(new Element(), { value, checked: index === 0 }));
const nrModes = ['auto', 'off', 'manual'].map((value, index) => Object.assign(new Element(), { value, checked: index === 0 }));
const addSources = ['ingest', 'import'].map(value => Object.assign(element('#add-source-' + value), { value, checked: value === 'import' }));
element('#ingest-panel').hidden = true;  // as the markup ships it
const document = {
  get activeElement() { return focused; },
  listeners: {},
  addEventListener(name, callback) { this.listeners[name] = callback; },
  createElement: tag => Object.assign(new Element(), { tagName: tag.toUpperCase() }),
  querySelector: selector => selector === 'input[name="export-scope"]:checked' ? scope : selector === 'input[name="astro-recipe"]:checked' ? recipes.find(input => input.checked) : selector === 'input[name="export-preset"]:checked' ? presetRadios.find(input => input.checked) : selector === '.burst-frames' ? (() => { const tree = node => [node, ...(node.children || []).flatMap(tree)]; return tree(element('#contact-sheet')).find(node => node.className === 'burst-frames') ?? null; })() : byData(selector) ? byData(selector)[0] ?? null
    : selector === '#rating' ? inspectorRating() : element(selector),
  querySelectorAll: selector => {
    if (selector === '.frame') { const tree = node => [node, ...(node.children || []).flatMap(tree)]; return tree(element('#contact-sheet')).filter(item => item.className === 'frame'); }
    if (selector === '.batch-button' || selector === '.batch-select') { const tree = node => [node, ...(node.children || []).flatMap(tree)]; return tree(element('#batch-list')).filter(node => node.className === selector.slice(1)); }
    if (selector === '.tab-row [role="tab"]') return tabElements;
    if (selector === '#frame-tabs [role="tab"]') return frameTabElements;
    if (selector === 'input[name="astro-recipe"]') return recipes;
    if (selector === 'input[name="export-preset"]') return presetRadios;
    if (selector === 'input[name="develop-nr-mode"]') return nrModes;
    if (selector === 'input[name="add-source"]') return addSources;
    if (selector === '#astro-lights input') return element('#astro-lights').children.map(label => label.children[0]);
    if (selector === '[data-crop-action]') return [element('#primary-crops'), element('#alternative-crops')].flatMap(grid => grid.children.map(card => card.children.at(-1)));
    return byData(selector) || [];
  },
};
// Intervals are recorded, never scheduled: scenarios advance them deterministically with tick().
let activeTimers = 0;
let nextTimer = 0;
const timers = new Map();
// The find scenario records timeouts so its debounce is real; elsewhere they run at once.
const timeouts = new Map();
let nextTimeout = 0;
const observers = [];  // [node, callback] for crop-draw, which resizes the frame viewport
const window = { ResizeObserver: scenario === 'crop-draw' ? class { constructor(callback) { this.callback = callback; } observe(node) { observers.push([node, this.callback]); } } : undefined, setTimeout: callback => (scenario === 'find' ? (timeouts.set(++nextTimeout, callback), nextTimeout) : callback()), clearTimeout: id => { timeouts.delete(id); }, setInterval: callback => { activeTimers += 1; timers.set(++nextTimer, callback); return nextTimer; }, clearInterval: id => { if (timers.delete(id)) activeTimers -= 1; }, matchMedia: () => ({ matches: true }), getComputedStyle: node => ({ gridTemplateColumns: scenario === 'rows' && node?.className === 'burst-frames' ? '224px 224px' : 'none' }) };
// pollJob returns its promise; setImmediate then flushes the fire-and-forget load() once the mocked-fetch microtasks drain.
async function tick() { for (const cb of [...timers.values()]) await cb(); await new Promise(setImmediate); }
async function settle() { for (let i = 0; i < 10 && activeTimers; i++) await tick(); }
const location = { search: '', pathname: '/' };
const history = { replaceState(_state, _title, url) { const at = url.indexOf('?'); location.search = at < 0 ? '' : url.slice(at); } };
const localStorage = { getItem: key => stored[key] ?? (scenario === 'astro-resume' && key === 'lumen-astro-job' ? '7' : scenario === 'tabs-remembered' && key === 'lumen-tab' ? 'export-tab' : scenario === 'tabs-remembered' && key === 'lumen-add-source' ? 'ingest' : scenario === 'frame-view' && key === 'lumen-tab' ? 'compose-tab' : scenario === 'tiles-remembered' && key === 'lumen-tile-size' ? 'small' : null), setItem(key, value) { stored[key] = value; }, removeItem(key) { delete stored[key]; } };
const photo = id => ({ id, name: 'photo-' + id + '.jpg', total_score: 0.8, preview_url: '/preview/' + id, decision: 'none', rating: 0 });
const decisionPosts = [];
const decisions = { 1: 'none', 2: 'none' };
const ratings = { 1: 0, 2: 0 };
const group = (id, ids) => ({ id, automatic_pick_id: ids[0], frame_count: ids.length, rejected_count: 0, photos: ids.map(photo) });
const picks = [];
const splitPosts = [];
const critiqueRequests = [];
const captionPosts = [];
const expandPosts = [];
const digestPosts = [];
const digestBusy = [];
let deferCritique = false;
const pendingTags = [];
let deferGroups = false;
const pendingGroups = [];
const pendingCritiques = [];
const storedCritique = { group_id: 1, photo_id: 3, name: 'b2.jpg', rationale: 'Best timing.', confidence: 0.72, candidate_ids: [1, 2, 3] };
let groups = scenario === 'default-tab' ? [] : scenario === 'merge' || scenario === 'merge-batches' || scenario === 'rail-visibility' ? [group(1, [1]), group(2, [2])] : scenario.startsWith('astro') || scenario === 'critique' || scenario === 'advice-status' || scenario === 'loupe-strip' ? [group(1, [1, 2, 3])] : [group(1, [1, 2])];
if (scenario === 'astro-triage') groups[0].astro_cue = true;
if (scenario === 'export-watermark') Object.assign(stored, { 'lumen-watermark': '\u00a9 N. David', 'lumen-export-preset': 'web' });  // remembered from an earlier visit
if (scenario === 'export-bad-preset') stored['lumen-export-preset'] = 'huge';
if (scenario.startsWith('resume')) {  // seeded before the startup load, which is the only one that resumes
  const range = (start, count) => Array.from({ length: count }, (_, index) => start + index);
  groups = [group(1, [...range(1000, 249), 2]), group(2, [1, 3])];  // photo 2 is the 250th frame, past the first page
  stored['lumen-selected'] = scenario === 'resume' ? '2' : '99';
}
const triageRequests = [];
const triagePayload = () => ({ group_id: 1, astro_cue: scenario === 'astro-triage', lights: [1, 2, 3].map(id => ({
  photo_id: id, name: 'photo-' + id + '.jpg', sky: { class: id === 2 ? 'cloud' : 'night_sky', confidence: 0.91 }, confidence: 0.91,
  issues: id === 2 ? ['cloud_band'] : [], star_count: 40, trailing_warning: false, flagged: scenario === 'astro-triage' && id === 2 })) });
const astroPosts = [];
let polls = 0;
let cancels = 0;
let runs = 0;
const statusWrites = [];
Object.defineProperty(element('#astro-status'), 'textContent', { get() { return this._text || ''; }, set(value) { this._text = value; statusWrites.push(value); } });
const jobPayload = (status, progress, extra = {}) => ({ job_id: 7, kind: 'astro', status, progress, error: null, result: null, log_tail: ['log: Running command: register'], warnings: ['No flat frames supplied; stacking without flats.'], lights: 2, excluded_lights: 1, darks: 0, flats: 0, biases: 0, created_at: '', updated_at: '', ...extra });
const importJob = (status, progress, extra = {}) => ({ job_id: 9, kind: 'import', status, progress, label: null, processed: null, total: null, error: null, result: null, log_tail: [], created_at: '', updated_at: '', ...extra });
const browsePaths = scenario === 'ingest' ? ['/Volumes/CARD', '/Volumes/Primary', '/Volumes/Mirror'] : [];
const ingestPosts = [];
let ingestPolls = 0;
let ingestOutcome = { copied: 3, already_present: 1, verified: 4, skipped: 2, card_verified_everywhere: true, snapshot: '20261022T120000Z.sqlite3' };
const ingestCounts = extra => ({ copied: 0, verified: 0, already_present: 0, failed: 0, files: 4, skipped: 0, unreadable: 0, card_verified_everywhere: false, manifest: 'manifest.json', snapshot: null, ...extra });
const ingestJob = (status, progress, extra = {}) => ({ job_id: 11, kind: 'ingest', status, progress, label: null, processed: null, total: null, error: null, result: null, log_tail: [], created_at: '', updated_at: '', ingest: ingestCounts(), ...extra });
let darktableReady = true;
let refineError = null;
const exportPosts = [];
let exportPolls = 0;
let exportCancels = 0;
const exportJob = (status, progress, extra = {}) => ({ job_id: 17, kind: 'export', status, progress, label: null, processed: null, total: null, error: null, result: null, log_tail: [], created_at: '', updated_at: '', ...extra });
const developPosts = [];
const suggestPosts = [];
let suggestCount = 0;
let developPolls = 0;
let developSaveError = null;
const developPreview = () => ({ image: 'data:image/jpeg;base64,RENDER', histogram: { y: [0.1, 0.2, 0.4, 0.1], r: [0.1, 0.2, 0.2, 0.1], g: [0.1, 0.1, 0.2, 0.1], b: [0.8, 0.05, 0.1, 0.05] }, defaults: { contrast: 0, sharpness: 1, luminance_nr: 0.004, color_nr: 0.5 }, neutral_temperature: 5600, neutral_tint: 7 });
const developJob = (status, progress, extra = {}) => ({ job_id: 21, kind: 'develop_group', status, progress, label: null, processed: null, total: null, error: null, result: null, log_tail: [], created_at: '', updated_at: '', ...extra });
const refinePosts = [];
let refinePolls = 0;
const refineJob = (status, progress, extra = {}) => ({ job_id: 13, kind: 'refine_previews', status, progress, label: null, processed: null, total: null, error: null, result: null, log_tail: [], created_at: '', updated_at: '', ...extra });
let groupLoads = 0;
let lastGroupsUrl = null;
const photoDetailRequests = [];
// URLs listed here wait until the scenario runs their entry in heldReplies, so replies can arrive out of order.
const holdUrls = new Set();
let fullStatus = 404;  // what /full/<id> answers when the frame view asks why the image failed
const heldReplies = [];
let keeperOverride = null;
let groupsError = null;
let resumeJobs = [];
let importPolls = 0;
const importPosts = [];
const tagPosts = [];
const assistPosts = [];
let tagPolls = 0;
const tagJob = (status, progress, extra = {}) => ({ job_id: 15, kind: 'tag', status, progress, label: null, processed: null, total: null, error: null, result: null, log_tail: [], created_at: '', updated_at: '', ...extra });
const suggestionRows = () => [1, 2].map(id => ({ photo_id: id, name: 'photo-' + id + '.jpg', group_id: 1, problems: [{ kind: 'motion_blur', confidence: 0.84 }, { kind: 'eyes_closed', confidence: 0.61 }], confidence: 0.9, preview_url: '/preview/' + id + '?v=x' }));
let rejectRows = suggestionRows();
const rejectPosts = [];
const keywordPosts = [];
const placePosts = [];
const locationPosts = [];
const keywordData = { subjects: ['ice'], suggested: ['krill', 'gentoo penguin'] };
const untaggedKeywords = { subjects: [], suggested: [] };
const vocabState = { categories: { wildlife: ['gentoo penguin'], learned: ['spindrift', 'krill'] }, counts: { spindrift: 3, krill: 1 }, blocked: [] };
const vocabularyPosts = [];
const removals = [];
let cleared = { frames: 3, batches: 1 };
let clearedLoads = 0;
const clearedPosts = [];
const mergePosts = [];
const compositionReviews = [];
const cropMutations = [];
const cropId = 'crop-1111111111111111';
let activeCrop = null;
let reviewMode = 'success';
let networkDown = false;  // the browser's fetch itself fails, as when lumen serve restarts mid-request
let routeMissing = false;  // the route is absent, so the stdlib answers with an HTML error page, not JSON
let cropLoads = 0;
const cropRequests = [];
const exifRequests = [];
let deferCropLoads = false;
let deferReviews = false;
const pendingCropLoads = [];
const pendingReviews = [];
function cropPayload(active = activeCrop) {
  return {
    photo_id: 2, preview_url: '/preview/2?v=test', active_crop: active, source_width: 6000, source_height: 4000,
    candidates: [
      { id: 'crop-original', kind: 'original', aspect: 'native', rect: { left: 0, top: 0, width: 1, height: 1 }, pixel_box: [0, 0, 120, 90], remaining_megapixels: 0.01, rationale: '', primary: true, revealable: false, resolution_warning: '0.010800 MP remaining; below 12 MP' },
      { id: cropId, kind: 'centered-fallback', aspect: '1:1', rect: { left: 0.125, top: 0, width: 0.75, height: 1 }, pixel_box: [15, 0, 105, 90], remaining_megapixels: 8.1, rationale: 'centered fallback', primary: true, revealable: false, resolution_warning: '8.100000 MP remaining; below 12 MP' },
    ],
  };
}
async function fetch(url, options = {}) {
  if (holdUrls.delete(url)) return new Promise(resolve => heldReplies.push(() => resolve(fetch(url, options))));
  if (networkDown) throw new TypeError('NetworkError when attempting to fetch resource.');
  if (routeMissing) return { ok: false, status: 404, json: async () => { throw new SyntaxError("Unexpected token '<', \"<!DOCTYPE \"... is not valid JSON"); } };
  if (url.startsWith('/full/')) return { ok: false, status: fullStatus, json: async () => ({}) };
  let payload;
  let ok = true;
  if (url === '/api/status' && scenario === 'status-skew') payload = { omlx_live: { summary: 'Ready' }, tools: {}, jpeg_output_dir: '/managed/outputs/jpeg' };  // an older server without omlx_roles
  else if (url === '/api/status' && scenario === 'stale-server') payload = { omlx_live: { summary: 'Ready' }, stale: ['This server is running code older than the files on disk. Restart it with lumen serve, then reload this page.'], omlx_roles: { fast: { model: 'm', loaded: true }, review: { model: 'm', loaded: true } }, tools: {}, jpeg_output_dir: '/managed/outputs/jpeg' };
  else if (url === '/api/status') payload = { omlx_live: { summary: 'Ready', online: true }, omlx_roles: { fast: { model: 'Qwen3-VL-8B-Instruct-4bit', loaded: scenario !== 'keywords-unloaded' }, review: { model: 'Qwen3.8-27B-4bit', loaded: scenario !== 'keywords-unloaded' } }, tools: { 'darktable-cli': { available: darktableReady }, 'siril-cli': { available: true } }, jpeg_output_dir: '/managed/outputs/jpeg', astro_output_dir: '/managed/outputs/astro', ingest_output_dir: '/managed/outputs/ingest', digest_dir: '/managed/digests' };
  else if (scenario.startsWith('workspace')) return httpFetch(apiBase + url, options);
  else if (/^\/api\/groups\/\d+\/critique$/.test(url)) {
    critiqueRequests.push([url, options.method]);
    if (options.method === 'POST') assert.deepEqual(JSON.parse(options.body), {});
    if (options.method === 'POST' && deferCritique) return new Promise(resolve => pendingCritiques.push(() => resolve({ ok: true, json: async () => ({ ...storedCritique, rationale: 'Fresh look.' }) })));
    payload = options.method === 'POST' ? { ...storedCritique, rationale: 'Fresh look.' } : { critique: scenario === 'critique' ? storedCritique : null };
  }
  else if (/^\/api\/groups\/\d+\/triage$/.test(url)) { assert.notEqual(options.method, 'POST'); triageRequests.push(url); payload = triagePayload(); }
  else if (groupsError && url.startsWith('/api/groups')) { lastGroupsUrl = url; ok = false; payload = { error: groupsError }; groupsError = null; }
  else if (url === '/api/groups' || url.startsWith('/api/groups?')) {
    groupLoads += 1; lastGroupsUrl = url;
    const keepers = groups.reduce((count, item) => count + item.photos.filter(p => p.decision !== 'reject' && (p.decision === 'flag' || p.id === item.automatic_pick_id)).length, 0);
    const frames = groups.reduce((count, item) => count + item.photos.length, 0);
    payload = { groups, photo_count: groups.reduce((count, item) => count + item.frame_count, 0), rejected_count: groups.reduce((count, item) => count + item.rejected_count, 0), counts: { filtered: frames, total: frames }, cameras: ['ILCE-7M4:1', 'ILCE-7RV:2'], keeper_count: keeperOverride ?? keepers };
  }
  else if (/^\/api\/photos\/\d+\/split$/.test(url)) {
    assert.deepEqual(JSON.parse(options.body), {}); splitPosts.push(url);
    const id = Number(url.split('/')[3]);
    groups = [group(1, groups[0].photos.map(item => item.id).filter(item => item !== id)), group(9, [id])];
    payload = { photo_id: id, group_id: 9, from_group_id: 1 };
  }
  else if (/^\/api\/photos\/\d+\/pick$/.test(url)) { assert.deepEqual(JSON.parse(options.body), {}); picks.push(url); payload = { photo_id: Number(url.split('/')[3]), manual_pick: true }; }
  else if (url === '/api/jobs') payload = { jobs: resumeJobs };
  else if (url === '/api/previews/refine') {
    assert.equal(options.method, 'POST'); refinePosts.push(JSON.parse(options.body));
    if (refineError) { ok = false; payload = { error: refineError }; } else payload = refineJob('queued', 0);
  }
  else if (url === '/api/jobs/13') {
    refinePolls += 1;
    if (refinePolls < 3) payload = refineJob('running', 0.5, { label: 'photo-1.jpg', processed: 1, total: 2 });
    else {
      groups.forEach(item => item.photos.forEach(entry => { entry.preview_kind = 'refined'; entry.preview_url = '/preview/' + entry.id + '?v=0123456789abcdef-refined'; }));
      payload = refineJob('succeeded', 1, { processed: 2, total: 2, result: { refined: 2, skipped: 0, failed: 0 } });
    }
  }
  else if (url === '/api/import') { importPosts.push(JSON.parse(options.body)); payload = importJob('queued', 0); }
  else if (url === '/api/photos/tag-all') { assert.equal(options.method, 'POST'); tagPosts.push(JSON.parse(options.body)); payload = tagJob('queued', 0); }
  else if (url === '/api/jobs/15') {
    tagPolls += 1;
    payload = tagPolls < 2 ? tagJob('running', 0.5) : tagJob('succeeded', 1, { result: { tagged: 2, skipped: 0, failed: 0 } });
  }
  else if (url === '/api/jobs/9') {
    importPolls += 1;
    payload = importPolls < 3 ? importJob('running', 0.5, { label: 'DSC0001.ARW', processed: 1, total: 2 })
      : importJob('succeeded', 1, { label: 'DSC0002.ARW', processed: 2, total: 2, result: { discovered: 2, imported: 1, updated: 0, skipped: 1, failed: 0 } });
  }
  else if (/^\/api\/photos\/\d+\/(decision|rating)$/.test(url)) {
    assert.equal(options.method, 'POST');
    const id = Number(url.split('/')[3]);
    const body = JSON.parse(options.body);
    decisionPosts.push({ url, body });
    if ('decision' in body) decisions[id] = body.decision; else ratings[id] = body.rating;
    payload = { photo_id: id, decision: decisions[id], rating: ratings[id], group_id: 1, automatic_pick_id: decisions[1] === 'reject' ? 2 : 1 };
  }
  else if (url === '/api/vocabulary') { assert.equal(options.method, undefined); payload = JSON.parse(JSON.stringify(vocabState)); }
  else if (url === '/api/vocabulary/forget') {
    assert.equal(options.method, 'POST');
    const body = JSON.parse(options.body);
    vocabularyPosts.push(body);
    vocabState.categories.learned = vocabState.categories.learned.filter(word => word !== body.word);
    payload = { word: body.word, removed_from: vocabState.counts[body.word] };
  }
  else if (url === '/api/photos/reject-suggestions') { assert.equal(options.method, undefined); payload = { suggestions: rejectRows.map(row => ({ ...row })), count: rejectRows.length }; }
  else if (url === '/api/photos/reject-suggestions/decide') {
    assert.equal(options.method, 'POST');
    const body = JSON.parse(options.body);
    rejectPosts.push(body);
    if (body.decision === 'reject') body.photo_ids.forEach(id => { decisions[id] = 'reject'; });
    rejectRows = rejectRows.filter(row => !body.photo_ids.includes(row.photo_id));
    payload = { decided: body.photo_ids.map(id => ({ photo_id: id, decision: decisions[id], rating: ratings[id], group_id: 1, automatic_pick_id: decisions[1] === 'reject' ? 2 : 1 })), remaining: rejectRows.length };
  }
  else if (url === '/api/photos/tag') { assert.equal(options.method, 'POST'); tagPosts.push(JSON.parse(options.body)); payload = tagJob('queued', 0); }
  else if (url === '/api/photos/assist') { assert.equal(options.method, 'POST'); assistPosts.push(JSON.parse(options.body)); payload = tagJob('queued', 0, { kind: 'assist' }); }
  else if (url === '/api/photos/1/tag') { assert.equal(options.method, 'POST'); return new Promise(resolve => pendingTags.push(() => resolve({ ok: true, json: async () => ({ photo_id: 1 }) }))); }
  else if (/^\/api\/photos\/[12]\/keywords$/.test(url)) {
    assert.equal(options.method, 'POST');
    const body = JSON.parse(options.body);
    const id = Number(url.split('/')[3]);
    keywordPosts.push(body);
    const words = id === 1 ? keywordData : untaggedKeywords;  // photo 2 was never tagged
    const [from, to] = body.action === 'add' ? [words.suggested, words.subjects] : [words.subjects, words.suggested];
    if (from.includes(body.keyword)) from.splice(from.indexOf(body.keyword), 1);  // a typed word was never suggested
    to.push(body.keyword);
    payload = { photo_id: id, subjects: [...words.subjects], suggested: [...words.suggested] };
  }
  else if (/^\/api\/photos\/[12]\/place$/.test(url)) {
    assert.equal(options.method, 'POST');
    const body = JSON.parse(options.body);
    placePosts.push(body);
    if (!body.name.trim()) { ok = false; payload = { error: 'place name must be 1-80 characters and country at most 60' }; }
    else payload = { photo_ids: body.scope === 'frame' ? [Number(url.split('/')[3])] : [1, 2], name: body.name, country: body.country || null, remembered: body.remember };  // radius_km rides along; the server decides who is near
  }
  else if (/^\/api\/photos\/[12]\/location$/.test(url)) {
    assert.equal(options.method, 'POST');
    const body = JSON.parse(options.body);
    locationPosts.push(body);
    payload = { photo_ids: [Number(url.split('/')[3])], latitude: -33.92, longitude: 18.42, place: 'Hide', country: null, location_typed: true };
  }
  else if (url === '/api/digests') {
    assert.equal(options.method, 'POST');
    const body = JSON.parse(options.body);
    digestPosts.push(body);
    digestBusy.push([state.busy, element('#write-digest').disabled]);
    if (body.overwrite === true) payload = { date: body.date, file: body.date + '.md', frames: 2, narrative: false, overwritten: true };
    else { ok = false; payload = { error: 'digest exists; confirm overwrite', exists: true }; }
  }
  else if (url === '/api/search/expand') {
    assert.equal(options.method, 'POST');
    expandPosts.push(JSON.parse(options.body));
    payload = { query: 'seabirds', terms: ['tern', 'gull'], text: 'seabirds | tern | gull' };
  }
  else if (/^\/api\/photos\/1\/caption(\/edit)?$/.test(url)) {
    const body = JSON.parse(options.body);
    captionPosts.push({ url, body });
    payload = url.endsWith('/edit') ? { photo_id: 1, ...body } : { photo_id: 1, caption: 'Drafted.', alt_text: 'Drafted alt.' };
  }
  else if (/^\/api\/photos\/\d+\/develop\/suggest$/.test(url)) {
    suggestPosts.push(JSON.parse(options.body));
    suggestCount += 1;
    payload = suggestCount === 1
      ? { settings: { exposure_ev: 0.8, clarity: 0.2 }, rationale: 'The top of the histogram is empty, so nothing reaches white; +0.8 EV fills it.' }
      : { settings: { exposure_ev: 0.6 }, rationale: 'Pulled back and the warmth left alone.' };
  }
  else if (/^\/api\/photos\/\d+\/develop(\/preview|\/apply-to-group)?$/.test(url)) {
    assert.equal(options.method, 'POST');
    const body = JSON.parse(options.body);
    developPosts.push({ url, body, signal: options.signal });
    if (url.endsWith('/apply-to-group')) payload = developJob('queued', 0);
    else if (!url.endsWith('/preview')) {
      if (developSaveError) { ok = false; payload = { error: developSaveError }; }
      else payload = { photo_id: Number(url.split('/')[3]), develop: Object.keys(body.settings).length ? body.settings : null, preview_url: '/preview/1?v=saved-developed' };
    }
    else payload = developPreview();
  }
  else if (url === '/api/jobs/21') {
    developPolls += 1;
    payload = developPolls < 2 ? developJob('running', 0.5, { processed: 1, total: 2 })
      : developJob('succeeded', 1, { processed: 2, total: 2, result: { applied: 2, skipped_non_raw: 0, skipped_offline: 1, failed: 0 } });
  }
  else if (/^\/api\/photos\/[123]$/.test(url) && photoDetailRequests.push(url)) payload = { metrics: { sharpness: 0.8, exposure: 0.8, eye_quality: null },
    develop: scenario === 'develop' && url.endsWith('/1') ? { exposure_ev: 0.5 } : null,
    advice: scenario === 'advice-status' && url.endsWith('/1') ? { settings: { exposure_ev: 0.5 }, rationale: 'lift' } : null,
    photo: Object.assign({ format: 'ARW', offline: scenario === 'develop-offline' },
      (scenario === 'place-line' || scenario === 'place-edit') && url.endsWith('/1') ? { latitude: -24.74351, longitude: 26.25732, altitude: null, place: 'Madikwe Game Reserve', place_country: 'South Africa' }
      : scenario === 'location' && url.endsWith('/1') ? { latitude: -24.1234, longitude: 31.5678, altitude: 312.4 } : { latitude: null, longitude: null, altitude: null }),
    tags: scenario === 'caption' && url.endsWith('/1') ? { model: 'review-model', data: { description: 'Penguins on ice', caption: 'Porpoising.', alt_text: 'Penguins leaping.' } }
      : scenario.startsWith('keyword') && url.endsWith('/1') ? { model: 'fast-model', data: { description: 'Penguins on ice', subjects: [...keywordData.subjects], suggested: [...keywordData.suggested], species: [{ name: 'gentoo penguin', confidence: 0.82 }] } } : null, warnings: scenario === 'raw-warning' && url.endsWith('/2') ? ['Sony reduced-size lossless raw; darktable cannot decode it'] : [] };
  else if (/^\/api\/photos\/\d+\/exif$/.test(url)) {
    exifRequests.push(url);
    payload = { photo_id: Number(url.split('/')[3]), fields: [{ label: 'Camera', value: 'ILCE-7CM2' }, { label: 'ISO', value: '100' }] };
  }
  else if (/^\/api\/photos\/\d+\/crops$/.test(url)) {
    cropLoads += 1;
    cropRequests.push(url);
    if (deferCropLoads) return new Promise(resolve => pendingCropLoads.push(value => resolve({ ok: true, json: async () => value })));
    const id = Number(url.split('/')[3]);
    payload = { ...cropPayload(activeCrop?.photo_id && activeCrop.photo_id !== id ? null : activeCrop), photo_id: id };  // a drawn crop belongs to its own frame
    if (scenario === 'advice-status') payload.review = { candidate_id: payload.candidates[0].id, rationale: 'tighter', confidence: 0.8 };
  }
  else if (/\/composition-review$/.test(url)) {
    compositionReviews.push(url);
    if (deferReviews) return new Promise(resolve => pendingReviews.push(value => resolve({ ok: true, json: async () => value })));
    if (reviewMode === 'failure') { ok = false; payload = { error: 'local composition review is unavailable for this photo' }; }
    else payload = { candidate_id: reviewMode === 'mismatch' ? 'crop-2222222222222222' : cropId, rationale: 'Keep the context.', confidence: 0.7 };
  } else if (/\/crop(?:\/clear)?$/.test(url)) {
    assert.equal(options.method, 'POST');
    const body = JSON.parse(options.body);
    cropMutations.push({ url, body });
    if (url.endsWith('/clear')) { assert.deepEqual(body, {}); activeCrop = null; payload = { photo_id: 2, active_crop: null }; }
    else if (body.candidate_id === 'crop-manual') { assert.deepEqual(Object.keys(body), ['candidate_id', 'rect', 'aspect']); activeCrop = { ...body, photo_id: Number(url.split('/')[3]) }; payload = activeCrop; }
    else { assert.deepEqual(body, { candidate_id: cropId }); activeCrop = { candidate_id: cropId }; payload = activeCrop; }
  }
  else if (url === '/api/groups/merge') {
    assert.equal(options.method, 'POST');
    mergePosts.push(JSON.parse(options.body));
    groups = [group(1, [1, 2])];
    payload = { group_id: 1, merged_group_ids: [2], photo_count: 2, automatic_pick_id: 1 };
  }
  else if (/^\/api\/groups\/\d+\/remove$/.test(url)) {
    assert.equal(options.method, 'POST');
    assert.deepEqual(JSON.parse(options.body), {});
    removals.push(url);
    const id = Number(url.split('/')[3]);
    const removed = groups.find(item => item.id === id);
    groups = groups.filter(item => item.id !== id);
    payload = { photos_removed: removed.photos.length, files_removed: 3, trash_path: '/managed/trash/recovery', snapshot_path: '/managed/trash/recovery/catalog.sqlite3' };
  } else if (url === '/api/library/cleared') {
    assert.equal(options.method, undefined);
    clearedLoads += 1;
    payload = { ...cleared, seed: { snapshots: 0, skipped: 0, added: 0 } };
  } else if (url === '/api/library/remove-cleared') {
    assert.equal(options.method, 'POST');
    clearedPosts.push(JSON.parse(options.body));
    payload = { photos_removed: cleared.frames, files_removed: 2, trash_path: '/managed/trash/recovery', snapshot_path: '/managed/trash/recovery/catalog.sqlite3' };
    cleared = { frames: 0, batches: 0 };
  } else if (url === '/api/browse') payload = { path: browsePaths.shift() || '/cal/darks', cancelled: false };
  else if (url === '/api/ingest') { ingestPosts.push(JSON.parse(options.body)); payload = ingestJob('queued', 0); }
  else if (url === '/api/jobs/11') {
    ingestPolls += 1;
    payload = ingestPolls < 3 ? ingestJob('running', 0.25, { label: 'DSC00001.ARW', processed: 1, total: 4, ingest: ingestCounts({ copied: 1, verified: 1 }) })
      : ingestJob('succeeded', 1, { label: 'DSC00004.ARW', processed: 4, total: 4, ingest: ingestCounts(ingestOutcome) });
  }
  else if (url === '/api/export') {
    assert.equal(options.method, 'POST'); exportPosts.push(JSON.parse(options.body));
    payload = exportPosts.at(-1).scope === 'current' ? { results: [{ photo_id: exportPosts.at(-1).photo_id, success: true, output: '/managed/outputs/jpeg/photo-2.jpg' }] } : exportJob('queued', 0);
  }
  else if (url === '/api/jobs/17/cancel') { assert.deepEqual(JSON.parse(options.body), {}); exportCancels += 1; payload = exportJob('cancelled', 0.5, { label: 'photo-1.jpg', processed: 1, total: 2, result: { exported: 1, failed: 0, skipped: 0 } }); }
  else if (url === '/api/jobs/17') {
    exportPolls += 1;
    payload = exportPolls < 3 || scenario === 'export-cancel' ? exportJob('running', 0.5, { label: 'photo-1.jpg', processed: 1, total: 2 })
      : exportJob('succeeded', 1, { label: 'photo-2.jpg', processed: 2, total: 2, result: { exported: 1, failed: 1, skipped: 0 }, log_tail: ['photo-2.jpg: bad render'] });
  }
  else if (url === '/api/astro/jobs') { assert.equal(options.method, 'POST'); astroPosts.push(JSON.parse(options.body)); payload = jobPayload('queued', 0, { recipe: astroPosts.at(-1).recipe }); }
  else if (url === '/api/astro/jobs/7/run') { assert.deepEqual(JSON.parse(options.body), {}); runs += 1; payload = jobPayload('queued', 0); }
  else if (url === '/api/astro/jobs/7/cancel') { assert.deepEqual(JSON.parse(options.body), {}); cancels += 1; payload = jobPayload('cancelled', 0.5); }
  else if (url === '/api/astro/jobs/7/reveal') { assert.deepEqual(JSON.parse(options.body), {}); payload = { path: '/managed/outputs/astro/7' }; }
  else if (url === '/api/astro/jobs/7' || url === '/api/jobs/7') {
    polls += 1;
    if (polls < 3 || scenario === 'astro-cancel') payload = jobPayload('running', polls === 1 ? 0.1 : 0.12);
    else if (scenario === 'astro-fail') payload = jobPayload('failed', 0.5, { error: 'Siril exited with exit code 3' });
    else payload = jobPayload('succeeded', 1, { result: { artifact_id: 1, name: 'result.tif' } });
  } else throw new Error('Unexpected request: ' + url);
  if (deferGroups && url.startsWith('/api/groups')) { const reply = { ok, json: async () => payload }; return new Promise(resolve => pendingGroups.push(() => resolve(reply))); }
  return { ok, json: async () => payload };
}
// Boxes the markup ships checked; the fake elements start unchecked.
element('#export-dated-folder').checked = true;
element('#loupe-dated-folder').checked = true;
`;
const regression = `
(async () => {
  await new Promise(setImmediate);
  if (scenario.startsWith('workspace')) {
    await load();
    const expected = scenario === 'workspace-mixed' ? 2 : 1;
    assert.equal(element('#workspace-cleanup-count').textContent, scenario === 'workspace-mixed' ? '1 batch · 2 frames' : '0 batches · 1 frame');
    assert.ok(element('#clear-help').textContent.includes('all ' + expected + ' frame'));
    assert.equal(element('#photo-count').textContent, scenario === 'workspace-mixed' ? '1 frame' : '0 frames');
    if (scenario === 'workspace-mixed') {
      await inspect(state.groups[0].photos[0]);
      assert.equal(element('#group-cleanup-count').textContent, 'Burst 1 · 1 frame');
    }
    assert.equal(element('#clear-workspace').disabled, true);
    const confirmation = element('#clear-confirmation');
    for (const value of ['clear', 'CLEAR ', 'CLEAR']) {
      confirmation.value = value;
      confirmation.listeners.input();
      assert.equal(element('#clear-workspace').disabled, value !== 'CLEAR');
    }
    await element('#clear-workspace').listeners.click();
    assert.ok(element('#clear-status').textContent.startsWith('Removed ' + expected + ' frame'));
    assert.equal(element('#workspace-cleanup-count').textContent, '0 batches · 0 frames');
    assert.ok(element('#clear-help').textContent.includes('all 0 frames'));
    assert.equal(element('#clear-workspace').disabled, true);
    assert.equal(confirmation.value, '');
    assert.equal(state.selectedId, null);
    assert.equal(state.selectedGroupId, null);
    return;
  }
  if (scenario === 'job-strip') {
    await load();
    const loadsBefore = groupLoads;
    element('#import-path').value = '/Volumes/Card';
    await element('#import-form').listeners.submit({ preventDefault() {}, currentTarget: element('#import-form') });
    assert.equal(state.job.status, 'queued');
    assert.equal(state.busy, true, 'a queued job keeps the workspace busy');
    assert.equal(activeTimers, 1, 'polling starts as soon as the import is accepted');
    assert.equal(element('#job-strip').hidden, false);
    assert.ok(element('#announcer').textContent.includes('Import'));
    for (const name of ['#import-form button[type="submit"]', '#browse-path', '#remove-group', '#browse-xmp']) assert.equal(element(name).disabled, true, name + ' is disabled during a job');
    assert.deepEqual([element('#cancel-job').disabled, element('#cancel-job').textContent, element('#cancel-job').className], [false, 'Cancel job', 'danger']);
    assert.equal(element('#tag-all').disabled, true, 'tag-all is structural: one job at a time');
    assert.equal(state.cullingBlocked, false, 'culling continues while an import runs');
    assert.equal(element('#open-compare').disabled, true, 'compare still needs two marked frames');
    await decide(state.groups[0].photos[1], 'decision', 'reject');
    assert.deepEqual(decisionPosts.at(-1), { url: '/api/photos/2/decision', body: { decision: 'reject' } }, 'a reject posts during an import');
    assert.ok(element('#announcer').textContent.includes('rejected'));
    assert.equal(state.busy, true, 'the import still holds structural actions');
    await tick();
    assert.equal(state.job.status, 'running');
    assert.ok(element('#job-label').textContent.includes('1 of 2'));
    assert.ok(element('#job-label').textContent.includes('DSC0001.ARW'));
    assert.equal(element('#job-progress').value, 50);
    assert.equal(element('#import-form button[type="submit"]').disabled, true);
    assert.equal(groupLoads, loadsBefore, 'groups are not refreshed while the job runs');
    await settle();
    assert.equal(activeTimers, 0, 'polling stops on a terminal state');
    assert.equal(state.job.status, 'succeeded');
    assert.equal(state.busy, false);
    assert.ok(element('#job-status').textContent.includes('finished'));
    assert.ok(element('#job-status').textContent.includes('1 skipped'));
    assert.ok(element('#announcer').textContent.includes('finished'));
    assert.equal(groupLoads, loadsBefore + 1, 'load() runs once when the job ends');
    assert.equal(element('#import-form button[type="submit"]').disabled, false);
    assert.deepEqual([element('#cancel-job').disabled, element('#cancel-job').textContent, element('#cancel-job').className], [false, 'Clear', 'secondary'], 'a finished job offers Clear');
    element('#cancel-job').listeners.click();
    assert.equal(element('#job-strip').hidden, true, 'Clear hides the strip');
    importPolls = 0;
    resumeJobs = [jobPayload('queued', 0), importJob('queued', 0)];
    await load();
    assert.equal(state.job.kind, 'import', 'a queued job from another tab is resumed; a prepared astro stack is not');
    assert.equal(state.job.status, 'queued');
    assert.equal(activeTimers, 1);
    resumeJobs = [];
    await settle();
    assert.equal(activeTimers, 0);
    assert.equal(state.job.status, 'succeeded');
    assert.equal(element('#tag-all').disabled, false);
    element('#retag-all').checked = true;
    await element('#tag-all').listeners.click();
    assert.deepEqual(tagPosts, [{ retag: true }]);
    assert.equal(state.job.kind, 'tag');
    assert.equal(activeTimers, 1, 'the accepted tag job is watched');
    assert.equal(element('#announcer').textContent, 'Tagging queued.');
    assert.equal(element('#tag-all').disabled, true);
    assert.equal(element('#retag-all').disabled, true);
    const posted = decisionPosts.length;
    await decide(state.groups[0].photos[1], 'decision', 'none');
    assert.equal(decisionPosts.length, posted, 'culling is refused while tagging runs');
    assert.equal(element('#announcer').textContent, 'Culling is paused while tagging runs.');
    assert.equal(element('#open-compare').disabled, true);
    await settle();
    assert.equal(state.job.status, 'succeeded');
    assert.equal(element('#job-status').textContent, 'Tagging finished. 2 tagged, 0 skipped, 0 failed.');
    assert.equal(element('#tag-all').disabled, false);
    await decide(state.groups[0].photos[1], 'decision', 'none');
    assert.equal(decisionPosts.length, posted + 1, 'culling resumes when the tag job ends');
    return;
  }
  if (scenario === 'decisions') {
    await load();
    element('#export-tab').listeners.click();
    const loadsBefore = groupLoads;
    const tree = node => [node, ...(node.children || []).flatMap(tree)];
    const card = id => document.querySelectorAll('.frame').find(item => Number(item.dataset.id) === id);
    const texts = id => tree(card(id)).map(node => node.textContent);
    const control = value => tree(element('#inspection')).find(node => node.dataset?.decisionControl === value);
    assert.equal(element('#keeper-count').textContent, '1 keeper');
    await inspect(state.groups[0].photos[1]);
    await control('flag').listeners.click();
    assert.deepEqual(decisionPosts.at(-1), { url: '/api/photos/2/decision', body: { decision: 'flag' } });
    assert.ok(texts(2).includes('Flagged'));
    assert.equal(element('#keeper-count').textContent, '2 keepers', 'a flagged alternate is a keeper');
    assert.equal(element('#announcer').textContent, 'photo-2 flagged');
    await inspect(state.groups[0].photos[0]);
    state.busy = true;
    updateWorkflow();
    assert.equal(control('reject').disabled, true, 'decision controls are disabled while busy');
    assert.ok(document.querySelectorAll('.frame').every(item => item.disabled !== true), 'frame cards stay usable while busy');
    state.busy = false;
    updateWorkflow();
    await control('reject').listeners.click();
    assert.deepEqual(decisionPosts.at(-1), { url: '/api/photos/1/decision', body: { decision: 'reject' } });
    assert.equal(focused, control('reject'), 'focus returns to the re-rendered Reject control, not a frame card');
    assert.ok(document.querySelectorAll('.frame').every(item => item.disabled !== true));
    assert.equal(groupLoads, loadsBefore, 'a decision updates cards in place without load()');
    assert.ok(texts(1).includes('Rejected'));
    assert.ok(card(1).getAttribute('aria-label').includes('rejected'));
    assert.ok(!texts(1).includes('Best'), 'the rejected pick loses its Best mark');
    assert.ok(texts(2).includes('Best'), 'the next frame is promoted in place');
    assert.equal(element('#keeper-count').textContent, '1 keeper');
    assert.equal(element('#announcer').textContent, 'photo-1 rejected');
    assert.ok(tree(element('#inspection')).some(node => node.textContent === 'Decision: Rejected · Rating 0'));
    const rating = tree(element('#inspection')).find(node => node.id === 'rating');
    assert.equal(rating.children.length, 6);
    rating.value = '3';
    await rating.listeners.change();
    assert.deepEqual(decisionPosts.at(-1), { url: '/api/photos/1/rating', body: { rating: 3 } });
    assert.ok(texts(1).includes('Rating 3'));
    assert.equal(focused.id, 'rating');
    assert.equal(element('#announcer').textContent, 'Rating 3 set for photo-1');
    await control('none').listeners.click();
    assert.equal(element('#announcer').textContent, 'photo-1 decision cleared');
    assert.ok(texts(2).includes('Flagged') && !texts(2).includes('Best'));
    assert.ok(texts(1).includes('Best') && !texts(1).includes('Rejected'));
    assert.equal(groupLoads, loadsBefore);
    await pickStatus('reject').listeners.change();
    assert.equal(groupLoads, loadsBefore + 1, 'choosing Rejected reloads with the rejected frames');
    assert.equal(lastGroupsUrl, '/api/groups?decision=reject');
    return;
  }
  if (scenario === 'keywords' || scenario === 'keywords-unloaded') {
    keeperOverride = 2;
    await load();
    const loaded = scenario === 'keywords';
    assert.equal(element('#tag-keepers').textContent, 'Tag keepers (2)');
    const state_ = loaded ? 'loaded' : 'not loaded';
    assert.equal(element('#system-status').textContent, 'Local AI online');
    assert.equal(element('#ai-availability').textContent, loaded ? '' : 'Fast model Qwen3-VL-8B-Instruct-4bit is not loaded in oMLX. Load it in oMLX, then reload this page.');
    assert.equal(element('#frame-ai-availability').textContent, element('#ai-availability').textContent, 'the frame view repeats the one line');
    assert.equal(element('#system-status').getAttribute('data-stale'), undefined, 'a current server carries no warning');
    const aiTree = node => [node, ...(node.children || []).flatMap(aiTree)];
    if (!loaded) {
      assert.equal(element('#assist-keepers').disabled, true, 'the combined run waits for both models');
      for (const id of ['#tag-keepers', '#tag-all']) {
        assert.equal(element(id).disabled, true, id + ' waits for the fast model');
      }
      await inspect(state.groups[0].photos[0]);
      const tagPhoto = aiTree(element('#inspection')).find(node => node.dataset?.tagPhoto === 'true');
      assert.equal(tagPhoto.disabled, true, 'the per-photo tag button waits for the fast model');
      assert.ok(!aiTree(element('#inspection')).some(node => node.id?.endsWith('-availability')), 'the frame-level line replaces per-block reasons');
      state.composition = { photo_id: state.selectedId };
      updateWorkflow();
      assert.equal(element('#ask-composition-ai').disabled, true, 'composition review waits for the review model');
      return;
    }
    assert.equal(element('#tag-all').disabled, false);
    assert.equal(element('#tag-keepers').disabled, false);
    const loadsBefore = groupLoads;
    const tree = node => [node, ...(node.children || []).flatMap(tree)];
    const list = id => tree(element('#inspection')).find(node => node.id === id);
    const texts = id => tree(list(id)).map(node => node.textContent);
    const labelled = label => tree(element('#inspection')).find(node => node.getAttribute?.('aria-label') === label);
    await inspect(state.groups[0].photos[0]);
    assert.ok(texts('keywords').includes('ice'));
    assert.ok(texts('suggested-keywords').includes('krill'));
    assert.ok(texts('suggested-keywords').includes('gentoo penguin (species, 82%)'), 'a species below the vocabulary threshold stays a labelled suggestion');
    assert.ok(!texts('suggested-keywords').some(text => text?.startsWith('Species:')), 'no separate species line');
    assert.ok(labelled('Add keyword gentoo penguin'));
    await labelled('Add keyword krill').listeners.click();
    assert.deepEqual(keywordPosts, [{ action: 'add', keyword: 'krill' }]);
    assert.ok(texts('keywords').includes('krill'));
    assert.ok(!texts('suggested-keywords').includes('krill'));
    assert.equal(focused, labelled('Remove keyword krill'), 'focus moves to the undo control');
    assert.equal(element('#announcer').textContent, 'Keyword krill added to photo-1.jpg');
    await labelled('Remove keyword ice').listeners.click();
    assert.deepEqual(keywordPosts.at(-1), { action: 'remove', keyword: 'ice' });
    assert.ok(texts('suggested-keywords').includes('ice'));
    assert.equal(element('#announcer').textContent, 'Keyword ice removed from photo-1.jpg');
    assert.equal(groupLoads, loadsBefore, 'keyword changes re-render in place without load()');
    const control = value => tree(element('#inspection')).find(node => node.dataset?.decisionControl === value);
    const tagging = tree(element('#inspection')).find(node => node.dataset?.tagPhoto === 'true').listeners.click();
    await new Promise(setImmediate);
    assert.equal(state.busy, false, 'tagging one frame never sets busy');
    assert.equal(control('reject').disabled, false, 'culling continues while the fast model answers');
    assert.equal(tree(element('#inspection')).find(node => node.className === 'ai-task' && node.dataset.key === 'tag:1').textContent, 'Tags running…');
    pendingTags.shift()();
    await tagging;
    assert.equal(control('reject').disabled, false);
    assert.equal(tree(element('#inspection')).find(node => node.className === 'ai-task' && node.dataset.key === 'tag:1').textContent, 'Tags ready');
    await element('#tag-keepers').listeners.click();
    assert.deepEqual(tagPosts, [{ scope: 'keepers' }]);
    assert.equal(element('#tag-keepers').disabled, true, 'one job at a time');
    await settle();
    assert.equal(state.job.status, 'succeeded');
    return;
  }
  if (scenario === 'keyword-typed') {
    await load();
    assert.deepEqual(element('#vocabulary-words').children.map(option => option.value), ['gentoo penguin', 'spindrift', 'krill'], 'every vocabulary word is offered once per load');
    await inspect(state.groups[0].photos[0]);
    const tree = node => [node, ...(node.children || []).flatMap(tree)];
    const byId = id => tree(element('#inspection')).find(node => node.id === id);
    const texts = id => tree(byId(id)).map(node => node.textContent);
    const input = byId('keyword-input');
    assert.equal(input.getAttribute('list'), 'vocabulary-words');
    assert.equal(tree(element('#inspection')).find(node => node.tagName === 'LABEL' && node.htmlFor === 'keyword-input').textContent, 'Add a keyword');
    assert.equal(byId('keyword-add').textContent, 'Add');
    input.value = 'Lion';
    await input.listeners.keydown({ key: 'Enter', preventDefault() {} });
    assert.deepEqual(keywordPosts, [{ action: 'add', keyword: 'Lion' }]);
    assert.ok(texts('keywords').includes('Lion'));
    const fresh = byId('keyword-input');
    assert.equal(fresh.value, '');
    assert.equal(focused, fresh, 'focus returns to the input for the next word');
    assert.equal(element('#announcer').textContent, 'Keyword Lion added to photo-1.jpg');
    fresh.value = 'lion';
    await fresh.listeners.keydown({ key: 'Enter', preventDefault() {} });
    assert.equal(keywordPosts.length, 1, 'a duplicate is refused without a round trip');
    assert.equal(element('#announcer').textContent, 'Keyword lion is already on photo-1.jpg');
    fresh.value = ' seal ';
    await byId('keyword-add').listeners.click();
    assert.deepEqual(keywordPosts.at(-1), { action: 'add', keyword: 'seal' });
    await inspect(state.groups[0].photos[1]);
    assert.equal(state.details.tags, null, 'photo 2 was never tagged');
    byId('keyword-input').value = 'Zebra';
    await byId('keyword-input').listeners.keydown({ key: 'Enter', preventDefault() {} });
    assert.deepEqual(keywordPosts.at(-1), { action: 'add', keyword: 'Zebra' });
    assert.ok(texts('keywords').includes('Zebra'), 'an untagged keeper takes a typed keyword');
    assert.equal(element('#announcer').textContent, 'Keyword Zebra added to photo-2.jpg');
    assert.deepEqual(element('#vocabulary-words').children.map(option => option.value), ['gentoo penguin', 'spindrift', 'krill', 'Lion', 'seal', 'Zebra'], 'learned words join the datalist without a reload');
    return;
  }
  if (scenario === 'digest') {
    await load();
    const announcements = [];
    Object.defineProperty(element('#announcer'), 'textContent', { get() { return this._text || ''; }, set(value) { this._text = value; if (value) announcements.push(value); } });
    const today = new Date().toLocaleDateString('sv-SE');
    await element('#write-digest').listeners.click();
    assert.deepEqual(digestPosts, [{ date: today }]);
    assert.equal(element('#digest-confirm').hidden, false, 'an existing digest asks before overwriting');
    assert.equal(element('#digest-confirm-text').textContent, 'A digest for ' + today + ' already exists. Overwrite it?');
    await element('#digest-confirm-yes').listeners.click();
    assert.deepEqual(digestPosts.at(-1), { date: today, overwrite: true });
    assert.equal(element('#digest-confirm').hidden, true);
    assert.equal(announcements.at(-1), 'Digest written for ' + today + ': ' + today + '.md (facts only)');
    assert.equal(element('#digest-result').textContent, today + '.md');
    assert.deepEqual(digestBusy, [[true, true], [true, true]], 'culling and the button wait while the digest request runs');
    assert.deepEqual([state.busy, element('#write-digest').disabled], [false, false]);
    return;
  }
  if (scenario === 'caption') {
    await load();
    const tree = node => [node, ...(node.children || []).flatMap(tree)];
    const byId = id => tree(element('#inspection')).find(node => node.id === id);
    const announcements = [];
    Object.defineProperty(element('#announcer'), 'textContent', { get() { return this._text || ''; }, set(value) { this._text = value; if (value) announcements.push(value); } });
    await inspect(state.groups[0].photos[0]);
    const caption = byId('caption-input');
    const alt = byId('alt-input');
    assert.deepEqual([caption.tagName, caption.value, caption.maxLength], ['INPUT', 'Porpoising.', 200]);
    assert.deepEqual([alt.tagName, alt.value, alt.maxLength], ['INPUT', 'Penguins leaping.', 125]);
    const labels = tree(byId('caption-controls')).filter(node => node.tagName === 'LABEL');
    assert.deepEqual(labels.map(node => [node.htmlFor, node.textContent]), [['caption-input', 'Caption'], ['alt-input', 'Alt text']]);
    const posts = decisionPosts.length;
    await element('#contact-sheet').listeners.keydown({ key: 'x', metaKey: false, ctrlKey: false, altKey: false, target: caption, preventDefault() {} });
    assert.equal(decisionPosts.length, posts, 'typing in the caption never culls');
    caption.value = 'Edited.';
    alt.value = 'Alt.';
    assert.equal(byId('save-caption').textContent, 'Save caption');
    await byId('save-caption').listeners.click();
    assert.deepEqual(captionPosts.at(-1), { url: '/api/photos/1/caption/edit', body: { caption: 'Edited.', alt_text: 'Alt.' } });
    assert.ok(announcements.includes('Caption saved for photo-1.jpg'));
    assert.equal(byId('ask-caption-ai').textContent, 'Draft a caption');
    assert.equal(byId('ask-caption-ai').disabled, false);
    await byId('ask-caption-ai').listeners.click();
    assert.deepEqual(captionPosts.at(-1), { url: '/api/photos/1/caption', body: {} });
    assert.deepEqual([caption.value, alt.value], ['Drafted.', 'Drafted alt.']);
    state.status.omlx_roles.review.loaded = false;
    updateWorkflow();  // as a status refresh does
    renderInspector(state.groups[0].photos[0]);
    assert.equal(byId('ask-caption-ai').disabled, true);
    assert.equal(byId('ask-caption-ai').getAttribute('aria-describedby'), 'frame-ai-availability');
    assert.ok(byId('ask-caption-ai').className.split(' ').includes('ai'));
    assert.ok(element('#frame-ai-availability').textContent.startsWith('Review model Qwen3.8-27B-4bit is not loaded in oMLX.'), 'the reason is said once, at the top of the frame panel');
    return;
  }
  if (scenario === 'advice-status') {
    await load();
    const tree = node => [node, ...(node.children || []).flatMap(tree)];
    const byId = id => tree(element('#inspection')).find(node => node.id === id);
    const control = value => tree(element('#inspection')).find(node => node.dataset?.decisionControl === value);
    const taskLine = () => tree(byId('burst-critique')).find(node => node.className === 'ai-task');
    const firstCandidateId = cropPayload().candidates[0].id;
    const resolveCritique = () => pendingCritiques.shift()();  // the fake answers { ...storedCritique, rationale: 'Fresh look.' }
    deferCritique = true;
    await inspect(photoById(1));
    const ask = byId('ask-burst-ai');
    const pending = ask.listeners.click();  // not awaited: the model is "thinking"
    assert.equal(state.busy, false, 'advice never sets busy');
    assert.equal(taskLine().textContent, 'Burst pick running…');
    assert.equal(control('reject').disabled, false, 'culling stays enabled');
    await inspect(photoById(2));  // walk away
    assert.equal(byId('ask-burst-ai').disabled, true, 'a re-rendered ask stays disabled while the model is running');
    resolveCritique({ ...storedCritique, rationale: 'Fresh look.' });
    await pending;
    await inspect(photoById(1));  // come back
    assert.ok(byId('burst-advice').textContent.includes('Fresh look.'));
    assert.equal(taskLine().textContent, 'Burst pick ready');
    assert.equal(byId('ask-burst-ai').disabled, false);
    await loadComposition();
    assert.equal(state.recommendedCropId, firstCandidateId, 'stored crop review is applied on load');
    assert.ok(element('#composition-advice').textContent.includes('tighter'));
    fillDevelop(photoById(1));
    assert.equal(element('#develop-advice').textContent, 'lift');
    assert.equal(element('#develop-use-advice').hidden, false);
    await element('#develop-use-advice').listeners.click();
    assert.equal(state.develop.fields.exposure_ev, 0.5);
    assert.equal(element('#develop-use-advice').hidden, true, 'a parked suggestion hides the button');
    return;
  }
  if (scenario === 'critique') {
    await load();
    const tree = node => [node, ...(node.children || []).flatMap(tree)];
    const byId = id => tree(element('#inspection')).find(node => node.id === id);
    await inspect(state.groups[0].photos[0]);
    assert.equal(byId('burst-advice').textContent, 'Local AI suggests frame 3 (b2.jpg): Best timing. (confidence 72%)');
    assert.equal(byId('ask-burst-ai').textContent, 'Pick the best frame');
    assert.equal(byId('ask-burst-ai').disabled, false);
    assert.deepEqual(picks, [], 'rendering stored advice never changes the pick');
    await inspect(state.groups[0].photos[1]);
    assert.equal(critiqueRequests.filter(([, method]) => method !== 'POST').length, 1, 'stored advice is fetched once per burst');
    const control = value => tree(element('#inspection')).find(node => node.dataset?.decisionControl === value);
    deferCritique = true;
    const asking = byId('ask-burst-ai').listeners.click();
    await new Promise(setImmediate);
    assert.equal(state.busy, false, 'a burst critique never sets busy');
    assert.equal(control('reject').disabled, false, 'culling continues while the review model answers');
    assert.deepEqual([byId('ask-burst-ai').getAttribute('aria-busy'), byId('ask-burst-ai').textContent], ['true', 'Asking local AI…'], 'the button shows the wait where the status bar is hidden');
    pendingCritiques.shift()();
    await asking;
    assert.equal(control('reject').disabled, false);
    assert.deepEqual(critiqueRequests.at(-1), ['/api/groups/1/critique', 'POST']);
    assert.equal(byId('burst-advice').textContent, 'Local AI suggests frame 3 (b2.jpg): Fresh look. (confidence 72%)');
    assert.deepEqual(picks, [], 'asking for advice never changes the pick');
    Object.assign(element('#loupe'), { tagName: 'DIALOG', id: 'loupe' });
    Object.assign(element('#loupe-viewport'), { tagName: 'DIV' });
    openLoupe(2);
    await new Promise(setImmediate);
    assert.equal(element('#loupe-heading').textContent, state.groups[0].photos[1].name);
    const flashed = [];
    const viewportClasses = element('#loupe-viewport').classList;
    const addClass = viewportClasses.add;
    viewportClasses.add = name => { flashed.push(name); addClass(name); };  // the fake timer clears it at once
    await byId('use-burst-pick').listeners.click();
    assert.deepEqual(picks, ['/api/photos/3/pick']);
    assert.equal(state.groups[0].automatic_pick_id, 3);
    assert.deepEqual([element('#loupe-heading').textContent, state.selectedId, state.focusedId], ['photo-3.jpg', 3, 3], 'the frame view moves to the new pick');
    assert.equal(element('#announcer').textContent, 'photo-3.jpg is now your best frame.');
    assert.deepEqual(flashed, ['pick-flash'], 'the frame view flashes when it changes photo');
    await new Promise(setImmediate);
    state.groups[0].photos[2].decision = 'reject';
    renderInspector(state.groups[0].photos[1]);
    assert.ok(byId('burst-advice').textContent.endsWith('(frame is rejected)'));
    assert.equal(byId('use-burst-pick'), undefined, 'a rejected suggestion offers no Use this pick');
    state.status.omlx_roles.review.loaded = false;
    renderInspector(state.groups[0].photos[1]);
    assert.equal(byId('ask-burst-ai').disabled, true);
    assert.equal(byId('burst-ai-availability'), undefined, 'the frame-level line replaces the per-block reason');
    assert.equal(byId('ask-burst-ai').getAttribute('aria-describedby'), 'frame-ai-availability');
    assert.ok(byId('ask-burst-ai').className.split(' ').includes('ai'));
    return;
  }
  if (scenario === 'vocabulary') {
    await load();
    element('#assist-tab').listeners.click();
    await new Promise(setImmediate);
    const rows = () => element('#vocabulary-list').children;
    assert.equal(rows().length, 2);
    assert.equal(element('#vocabulary-count').textContent, '(2)');
    assert.equal(rows()[0].children[0].textContent, 'spindrift (3 frames)');
    assert.equal(rows()[1].children[0].textContent, 'krill (1 frame)');
    assert.deepEqual(vocabularyPosts, [], 'listing never forgets');
    await rows()[0].children[1].listeners.click();
    assert.deepEqual(vocabularyPosts, [{ word: 'spindrift' }]);
    assert.equal(rows().length, 1);
    assert.equal(rows()[0].children[1].getAttribute('aria-label'), 'Forget keyword krill');
    assert.equal(focused, rows()[0].children[1], 'focus moves to the next Forget button');
    assert.equal(element('#announcer').textContent, 'spindrift forgotten; removed from 3 frames');
    return;
  }
  if (scenario === 'review-rejects') {
    await load();
    const loadsBefore = groupLoads;
    const announcements = [];
    Object.defineProperty(element('#announcer'), 'textContent', { get() { return this._text || ''; }, set(value) { this._text = value; if (value) announcements.push(value); } });
    element('#review-tab').listeners.click();
    await new Promise(setImmediate);
    await loadRejectSuggestions();
    await tick();
    const rows = () => element('#reject-list').children;
    const button = (label) => rows().flatMap(row => row.children[2].children).find(node => node.getAttribute('aria-label') === label);
    assert.equal(rows().length, 2);
    assert.deepEqual(rejectPosts, [], 'rendering and polling never post a decision');
    assert.equal(element('#review-count').textContent, '2 suggested rejects');
    assert.equal(rows()[0].className, 'reject-row');
    assert.equal(rows()[0].dataset.photoId, '1');
    assert.equal(rows()[0].children[0].alt, '');
    assert.equal(rows()[0].children[0].loading, 'lazy');
    assert.equal(rows()[0].children[1].children[1].textContent, 'motion blur 84%, eyes closed 61%');
    assert.equal(element('#scan-rejects').disabled, false, 'the fast role is loaded');
    const typing = { key: 'x', target: Object.assign(document.createElement('input'), { parent: rows()[0] }), preventDefault() {} };
    await element('#reject-list').listeners.keydown(typing);
    assert.deepEqual(rejectPosts, [], 'X typed in a field is ignored');
    await element('#reject-list').listeners.keydown({ key: 'ArrowDown', target: button('Reject photo-1.jpg'), preventDefault() {} });
    assert.equal(focused, button('Reject photo-2.jpg'), 'ArrowDown moves to the next row');
    await element('#reject-list').listeners.keydown({ key: 'ArrowUp', target: button('Reject photo-2.jpg'), preventDefault() {} });
    assert.equal(focused, button('Reject photo-1.jpg'));
    await element('#reject-list').listeners.keydown({ key: 'X', target: button('Reject photo-1.jpg'), preventDefault() {} });
    await tick();
    assert.deepEqual(rejectPosts, [{ photo_ids: [1], decision: 'reject' }]);
    assert.equal(rows().length, 1);
    assert.equal(element('#review-count').textContent, '1 suggested reject');
    assert.equal(state.groups[0].photos[0].decision, 'reject', 'the frame is marked through the decide bookkeeping');
    assert.equal(state.rejectedCount, 1);
    assert.equal(state.groups[0].automatic_pick_id, 2, 'the next frame becomes best in place');
    assert.ok(document.querySelectorAll('.frame').find(card => Number(card.dataset.id) === 1).getAttribute('aria-label').includes('rejected'));
    assert.equal(announcements.at(-1), 'photo-1.jpg rejected');
    await button('Keep photo-2.jpg').listeners.click();
    await tick();
    assert.deepEqual(rejectPosts.at(-1), { photo_ids: [2], decision: 'keep' });
    assert.equal(rows().length, 0);
    assert.equal(state.groups[0].photos[1].decision, 'none', 'Keep never changes a decision');
    assert.equal(announcements.at(-1), 'photo-2.jpg kept');
    rejectRows = suggestionRows();
    await loadRejectSuggestions();
    assert.equal(rows().length, 2);
    const posted = rejectPosts.length;
    element('#reject-all-shown').listeners.click();
    assert.equal(rejectPosts.length, posted, 'opening the confirmation posts nothing');
    assert.equal(element('#reject-all-confirm').hidden, false);
    assert.ok(element('#reject-all-text').textContent.includes('Reject all 2 shown frames?'));
    assert.equal(element('#reject-all-text').textContent, 'Reject all 2 shown frames? This marks them rejected; you can restore any frame from the frame view.');
    await element('#reject-all-yes').listeners.click();
    await tick();
    assert.deepEqual(rejectPosts.at(-1), { photo_ids: [1, 2], decision: 'reject' });
    assert.equal(rows().length, 0);
    assert.equal(element('#reject-all-confirm').hidden, true);
    assert.equal(announcements.at(-1), '2 frames rejected');
    assert.equal(groupLoads, loadsBefore, 'decisions update in place without load()');
    return;
  }
  if (scenario === 'culling-keys') {
    groups = [group(1, [1, 2, 3])];
    await load();
    const loadsBefore = groupLoads;
    const sheetNode = element('#contact-sheet');
    const tree = node => [node, ...(node.children || []).flatMap(tree)];
    const card = id => document.querySelectorAll('.frame').find(item => Number(item.dataset.id) === id);
    const texts = id => tree(card(id)).map(node => node.textContent);
    const tabindexes = () => document.querySelectorAll('.frame').map(item => item.getAttribute('tabindex'));
    let prevented = 0;
    const key = (name, extra = {}) => sheetNode.listeners.keydown({ key: name, metaKey: false, ctrlKey: false, altKey: false, target: card(state.focusedId), preventDefault() { prevented += 1; }, ...extra });
    assert.equal(state.focusedId, 1);
    assert.deepEqual(tabindexes(), ['0', '-1', '-1'], 'exactly one frame is in the tab order');
    await key('ArrowRight');
    assert.equal(state.focusedId, 2);
    assert.equal(focused, card(2));
    assert.deepEqual(tabindexes(), ['-1', '0', '-1']);
    assert.equal(prevented, 1);
    await key('End');
    assert.equal(focused, card(3));
    await key('Home');
    assert.equal(focused, card(1));
    assert.deepEqual(tabindexes(), ['0', '-1', '-1']);
    await key('PageDown');
    assert.equal(state.focusedId, 2, 'without computed grid columns a row is one frame');
    await key('x');
    assert.deepEqual(decisionPosts.at(-1), { url: '/api/photos/2/decision', body: { decision: 'reject' } });
    assert.ok(texts(2).includes('Rejected'));
    assert.equal(element('#announcer').textContent, 'photo-2 rejected');
    assert.equal(focused, card(2), 'a keyboard decision keeps focus on the frame');
    await key('3');
    assert.deepEqual(decisionPosts.at(-1), { url: '/api/photos/2/rating', body: { rating: 3 } });
    assert.ok(texts(2).includes('Rating 3'));
    assert.ok(texts(1).includes('Best'));
    await key('p');
    assert.deepEqual(picks, ['/api/photos/2/pick']);
    assert.ok(texts(2).includes('Best') && !texts(2).includes('Rejected'), 'a pick clears the rejection and moves Best in place');
    assert.ok(!texts(1).includes('Best'));
    assert.equal(element('#announcer').textContent, 'photo-2.jpg is now your best frame.');
    assert.equal(groupLoads, loadsBefore, 'keyboard culling updates cards without load()');
    await key('Home');
    await key('c');
    assert.deepEqual(state.compare, [1]);
    assert.equal(element('#announcer').textContent, 'Marked for compare (1 of 2)');
    await key('ArrowRight');
    await key('C');
    assert.deepEqual(state.compare, [1, 2]);
    assert.equal(element('#announcer').textContent, 'Marked for compare (2 of 2)');
    await key('End');
    await key('c');
    assert.deepEqual(state.compare, [2, 3], 'a third mark replaces the oldest');
    assert.ok(texts(3).includes('Compare') && !texts(1).includes('Compare'));
    await key('c');
    assert.deepEqual(state.compare, [2]);
    assert.equal(element('#announcer').textContent, 'Unmarked');
    const posts = decisionPosts.length;
    const before = prevented;
    for (const modifier of ['metaKey', 'ctrlKey', 'altKey']) await key('x', { [modifier]: true });
    for (const name of ['#import-path', '#clear-confirmation', '#caption-input', '#alt-input']) {
      element(name).tagName = 'INPUT';
      await key('x', { target: element(name) });
      await key('Home', { target: element(name) });
    }
    await key('Tab');
    assert.equal(decisionPosts.length, posts, 'modified keys and typing never cull');
    assert.equal(state.focusedId, 3);
    assert.equal(prevented, before, 'ignored keys keep their native behaviour');
    state.busy = true;
    await key('f');
    assert.equal(decisionPosts.length, posts, 'no decision while busy');
    state.busy = false;
    element('#shortcuts-button').listeners.click();
    assert.equal(element('#shortcuts').hidden, false);
    element('#shortcuts').listeners.keydown({ key: 'Escape' });
    assert.equal(element('#shortcuts').hidden, true);
    const splitLoads = groupLoads;
    await key('s');
    assert.deepEqual(splitPosts, ['/api/photos/' + state.focusedId + '/split']);
    assert.equal(groupLoads, splitLoads + 1, 'a split reloads the bursts');
    assert.deepEqual([state.selectedGroupId, state.selectedId], [9, state.focusedId], 'selection follows the split frame into its new burst');
    assert.equal(focused, card(state.focusedId));
    assert.ok(element('#announcer').textContent.includes('burst of its own'), element('#announcer').textContent);
    await key('s');
    assert.equal(splitPosts.length, 1, 'a frame already alone is not posted');
    assert.ok(element('#announcer').textContent.includes('already'), element('#announcer').textContent);
    return;
  }
  if (scenario === 'compare' || scenario === 'loupe') {
    await load();
    const sheetNode = element('#contact-sheet');
    const tree = node => [node, ...(node.children || []).flatMap(tree)];
    const card = id => document.querySelectorAll('.frame').find(item => Number(item.dataset.id) === id);
    const texts = node => tree(node).map(item => item.textContent);
    let prevented = 0;
    let stopped = 0;
    const key = (name, target, node = sheetNode) => node.listeners.keydown({ key: name, metaKey: false, ctrlKey: false, altKey: false, target, preventDefault() { prevented += 1; }, stopPropagation() { stopped += 1; } });
    const dialog = element('#' + scenario);
    dialog.tagName = 'DIALOG';
    const form = Object.assign(new Element(), { tagName: 'FORM', parent: dialog });
    if (scenario === 'loupe') {
      await key('ArrowRight', card(1));
      await key(' ', card(2));
      assert.equal(dialog.hidden, false, 'Space opens the loupe for the focused frame');
      assert.equal(prevented, 2, 'Space never scrolls the sheet or clicks the card');
      dialog.hidden = true; openView = null;
      await key('Enter', card(2));
      assert.equal(dialog.hidden, false, 'Enter opens the loupe for the focused frame');
      assert.equal(element('#loupe-heading').textContent, 'photo-2.jpg');
      const viewport = element('#loupe-viewport');
      assert.equal(viewport.getAttribute('aria-label'), 'photo-2.jpg viewport');
      const image = viewport.children[0];
      assert.equal(image.src, '/preview/2');
      Object.assign(image, { naturalWidth: 1600, naturalHeight: 1200 });
      image.listeners.load();
      assert.deepEqual([image.className, image.width, image.height], ['fit', undefined, undefined], 'the frame view fits the stored preview to its viewport; CSS sizes it');
      assert.equal(focused, viewport);
      Object.assign(viewport, { scrollTop: 0, scrollLeft: 0, scrollWidth: 3200, clientWidth: 1200, tagName: 'DIV' });  // full size: wider than the view
      await key('ArrowDown', viewport, viewport);
      await key('ArrowRight', viewport, viewport);
      assert.deepEqual([viewport.scrollTop, viewport.scrollLeft], [40, 40]);
      await key('PageDown', viewport, viewport);
      assert.equal(stopped, 3, 'panning keys never reach contact-sheet navigation');
      assert.equal(state.focusedId, 2);
      const closeButton = Object.assign(element('#close-loupe'), { tagName: 'BUTTON', parent: form });
      const before = prevented;
      await key('Enter', closeButton, dialog);
      assert.equal(prevented, before, 'Enter on a dialog button keeps its native activation');
      await key('Escape', viewport, dialog);
      assert.equal(dialog.hidden, true);
      assert.equal(focused, card(2), 'closing returns focus to the originating frame');
      return;
    }
    const opener = element('#open-compare');
    assert.equal(opener.disabled, true);
    await key('c', card(1));
    assert.equal(opener.disabled, true, 'one marked frame cannot be compared');
    await key('ArrowRight', card(1));
    await key('c', card(2));
    assert.equal(opener.disabled, false);
    await opener.listeners.click();
    assert.equal(dialog.hidden, false);
    const details = side => texts(element('#compare-' + side + '-details'));
    for (const [side, id] of [['left', 1], ['right', 2]]) {
      for (const word of ['Name', 'photo-' + id + '.jpg', 'Overall score', '80%', 'Sharpness', 'Exposure', 'Decision', 'No decision', 'Rating', 'No rating']) assert.ok(details(side).includes(word), side + ' lists ' + word);
      assert.ok(!details(side).includes('Eye quality'), 'eye quality is listed only when measured');
      assert.equal(element('#compare-' + side + '-viewport').getAttribute('aria-label'), 'photo-' + id + '.jpg viewport');
      assert.equal(element('#compare-' + side + '-viewport').children[0].src, '/preview/' + id);
      const pane = Object.assign(element('#compare-' + side + '-viewport').children[0], { naturalWidth: 1600, naturalHeight: 1200 });
      pane.listeners.load();
      assert.deepEqual([pane.className, pane.width, pane.height], ['', 1600, 1200], 'compare panes keep scale 1 so zoom matches');
    }
    const left = element('#compare-left-viewport');
    const right = element('#compare-right-viewport');
    Object.assign(left, { tagName: 'DIV', scrollTop: 0, scrollLeft: 0, scrollHeight: 1200, clientHeight: 400, scrollWidth: 1600, clientWidth: 400 });
    Object.assign(right, { tagName: 'DIV', scrollTop: 0, scrollLeft: 0, scrollHeight: 2400, clientHeight: 400, scrollWidth: 3200, clientWidth: 400 });
    Object.assign(left, { scrollTop: 400, scrollLeft: 600 });
    left.listeners.scroll();
    assert.deepEqual([right.scrollTop, right.scrollLeft], [1000, 1400], 'panning copies the fraction of the scrollable range');
    right.listeners.scroll();
    assert.deepEqual([left.scrollTop, left.scrollLeft], [400, 600], 'the echoed scroll does not feed back');
    await key('ArrowDown', left, left);
    assert.equal(left.scrollTop, 440);
    assert.equal(stopped, 1);
    element('#swap-compare').listeners.click();
    assert.ok(details('left').includes('photo-2.jpg') && details('right').includes('photo-1.jpg'), 'Swap sides exchanges the panes');
    assert.ok(element('#announcer').textContent.startsWith('Swapped'));
    const loadsBefore = groupLoads;
    await element('#compare-right-reject').listeners.click();
    assert.deepEqual(decisionPosts.at(-1), { url: '/api/photos/1/decision', body: { decision: 'reject' } });
    assert.ok(details('right').includes('Rejected'));
    assert.ok(details('left').includes('No decision'));
    assert.ok(texts(card(1)).includes('Rejected'), 'the contact sheet card updates without reload');
    assert.equal(groupLoads, loadsBefore);
    const rating = Object.assign(element('#compare-left-rating'), { tagName: 'SELECT', value: '4' });
    await rating.listeners.change();
    assert.deepEqual(decisionPosts.at(-1), { url: '/api/photos/2/rating', body: { rating: 4 } });
    assert.ok(details('left').includes('4 of 5'));
    const posts = decisionPosts.length;
    const before = prevented;
    await key('x', rating, dialog);
    const swap = Object.assign(element('#swap-compare'), { tagName: 'BUTTON', parent: form });
    await key('x', swap, dialog);
    await key('Enter', swap, dialog);
    assert.equal(decisionPosts.length, posts, 'the pane rating select and dialog buttons never cull');
    assert.equal(prevented, before);
    assert.notEqual(element('#loupe').hidden, false);
    right.listeners.focus();
    await key('f', right, dialog);
    assert.deepEqual(decisionPosts.at(-1), { url: '/api/photos/1/decision', body: { decision: 'flag' } }, 'culling keys act on the focused pane');
    assert.ok(details('right').includes('Flagged'));
    await key('Escape', right, dialog);
    assert.equal(dialog.hidden, true);
    assert.equal(focused, card(2), 'closing returns focus to the originating frame');
    assert.equal(state.focusedId, 2);
    return;
  }
  if (scenario === 'frame-view') {
    assert.equal(element('#add-tab').getAttribute('aria-selected'), 'true', 'a remembered Compose tab falls back to Add photos');
    element('#loupe-export-options').hidden = false;  // as the markup ships it
    await load();
    const card = id => document.querySelectorAll('.frame').find(item => Number(item.dataset.id) === id);
    let prevented = 0;
    const key = (name, target, node) => node.listeners.keydown({ key: name, metaKey: false, ctrlKey: false, altKey: false, target, preventDefault() { prevented += 1; }, stopPropagation() {} });
    const sheetNode = element('#contact-sheet');
    const loupe = Object.assign(element('#loupe'), { tagName: 'DIALOG', id: 'loupe' });
    const viewport = Object.assign(element('#loupe-viewport'), { tagName: 'DIV' });
    card(1).listeners.click();
    await new Promise(setImmediate);
    assert.deepEqual(cropRequests, [], 'a sheet click never requests crops');
    photoDetailRequests.length = 0;
    await key('Home', card(1), sheetNode);
    await key('Enter', card(1), sheetNode);
    await new Promise(setImmediate);
    assert.equal(loupe.hidden, false);
    assert.equal(element('#announcer').textContent, 'photo-1.jpg, 1 of 2', 'opening announces the position');
    assert.equal(state.selectedId, 1, 'opening the frame view selects the frame');
    assert.deepEqual(photoDetailRequests, ['/api/photos/1']);
    assert.deepEqual(cropRequests, ['/api/photos/1/crops']);
    assert.equal(element('#loupe-heading').textContent, 'photo-1.jpg', 'the dialog heading names the frame');
    assert.ok(element('#inspection').children.length, 'the inspector renders inside the frame view');
    assert.equal(element('#compose-workspace').hidden, false, 'crop choices show in the frame view');
    let before = prevented;
    await key('Enter', Object.assign(new Element(), { tagName: 'A' }), loupe);
    assert.equal(prevented, before, 'Enter on a frame-view link keeps its native activation');
    [element('#inspection'), viewport].forEach(node => { node.parent = loupe; });
    const tree = node => [node, ...(node.children || []).flatMap(tree)];
    const flag = tree(element('#inspection')).find(node => node.dataset?.decisionControl === 'flag');
    flag.focus();
    await key('f', flag, loupe);
    await new Promise(setImmediate);
    assert.equal(focused, viewport, 'culling from the right column keeps focus inside the frame view');
    await key(']', viewport, loupe);
    assert.equal(element('#announcer').textContent, 'photo-2.jpg, 2 of 2');
    await new Promise(setImmediate);
    assert.equal(state.selectedId, 2);
    assert.deepEqual(photoDetailRequests, ['/api/photos/1', '/api/photos/2']);
    assert.deepEqual(cropRequests, ['/api/photos/1/crops', '/api/photos/2/crops']);
    assert.equal(element('#loupe-heading').textContent, 'photo-2.jpg');
    assert.equal(element('#loupe-export-options').hidden, false, 'the export options show on the Frame tab');
    const inspected = tree(element('#inspection'));
    const chooseBest = inspected.find(node => node.textContent === 'Choose as best');
    const tagFrame = inspected.find(node => node.textContent === 'Tag this frame');
    assert.ok(!chooseBest.className.split(' ').includes('secondary'), 'Choose as best is the primary action');
    assert.deepEqual(['secondary', 'ai'].map(name => tagFrame.className.split(' ').includes(name)), [true, true], 'tagging is a secondary AI action');
    assert.equal(tagFrame.getAttribute('aria-describedby'), 'frame-ai-availability');
    const exportButton = Object.assign(element('#loupe-export'), { tagName: 'BUTTON' });
    before = prevented;
    await key('Enter', exportButton, loupe);
    assert.equal(prevented, before, 'Enter on a dialog button keeps its native activation');
    await key('x', exportButton, loupe);
    assert.deepEqual(decisionPosts.at(-1), { url: '/api/photos/2/decision', body: { decision: 'reject' } }, 'culling keys work from the dialog buttons');
    const posts = decisionPosts.length;
    await key('x', Object.assign(new Element(), { tagName: 'INPUT' }), loupe);
    assert.equal(decisionPosts.length, posts, 'a text field in the frame view swallows culling keys');
    assert.equal(exportButton.disabled, false);
    await exportButton.listeners.click();
    assert.deepEqual(exportPosts.at(-1), { scope: 'current', profile: null, preset: 'full', photo_id: 2 });
    assert.ok(element('#export-status').textContent.startsWith('Full resolution export complete: 1 saved'));
    assert.equal(element('#compose-status').textContent, element('#export-status').textContent, 'the export result shows in the frame view');
    assert.equal(element('#announcer').textContent, element('#export-status').textContent, 'a Frame-tab export announces its result, since the Crop tab status is not on screen');
    await key('Escape', viewport, loupe);
    assert.equal(loupe.hidden, true);
    assert.equal(focused, card(2), 'closing returns focus to the frame last shown');
    return;
  }
  if (scenario === 'frame-view-race') {
    groups = [group(1, [1, 2, 3])];
    await load();
    const card = id => document.querySelectorAll('.frame').find(item => Number(item.dataset.id) === id);
    const key = (name, target, node) => node.listeners.keydown({ key: name, metaKey: false, ctrlKey: false, altKey: false, target, preventDefault() {}, stopPropagation() {} });
    const loupe = Object.assign(element('#loupe'), { tagName: 'DIALOG', id: 'loupe' });
    const viewport = Object.assign(element('#loupe-viewport'), { tagName: 'DIV' });
    ['/api/photos/1', '/api/photos/1/crops'].forEach(url => holdUrls.add(url));
    await key('Home', card(1), element('#contact-sheet'));
    await key('Enter', card(1), element('#contact-sheet'));
    await key(']', viewport, loupe);
    await key(']', viewport, loupe);
    await new Promise(setImmediate);
    assert.equal(heldReplies.length, 2, "frame 1's replies are still in flight");
    assert.equal(state.selectedId, 3);
    assert.equal(element('#inspection').children[0].textContent, 'Alternate frame; compare its component scores.', "the inspector shows frame 3, not frame 1's pick verdict");
    const inspected = element('#inspection').children;
    assert.equal(state.composition.photo_id, 3);
    const cards = element('#primary-crops').children;
    heldReplies.splice(0).forEach(reply => reply());
    await new Promise(setImmediate);
    assert.equal(element('#loupe-heading').textContent, 'photo-3.jpg', 'late replies leave the frame view on frame 3');
    assert.equal(element('#inspection').children, inspected, "frame 1's late details are ignored");
    assert.equal(state.composition.photo_id, 3, "frame 1's late crops are ignored");
    assert.equal(element('#primary-crops').children, cards, 'the crop cards are not redrawn');
    return;
  }
  if (scenario === 'frame-tabs') {
    element('#loupe-export-options').hidden = false;  // as the markup ships it
    await load();
    const card = id => document.querySelectorAll('.frame').find(item => Number(item.dataset.id) === id);
    const key = (name, target, node) => node.listeners.keydown({ key: name, metaKey: false, ctrlKey: false, altKey: false, target, preventDefault() {}, stopPropagation() {} });
    const loupe = Object.assign(element('#loupe'), { tagName: 'DIALOG', id: 'loupe' });
    const [frameTab, cropTab, developTabButton, exifTab] = frameTabElements;
    await key('Enter', card(1), element('#contact-sheet'));
    await new Promise(setImmediate);
    assert.equal(frameTab.getAttribute('aria-selected'), 'true', 'the frame view opens on Frame by default');
    assert.ok(!element('#frame-panel').hidden);
    const exportOptions = element('#loupe-export-options');
    assert.equal(exportOptions.hidden, false, 'the one export button and its options serve every tab');
    assert.ok(!element('#inspection').children.some(node => node.tagName === 'H3' && node.textContent === 'photo-1.jpg'), 'the dialog heading carries the name, not the inspector');
    cropTab.listeners.click();
    assert.equal(cropTab.getAttribute('aria-selected'), 'true');
    assert.equal(exportOptions.hidden, false, 'Crop keeps the export options');
    assert.equal(frameTab.getAttribute('aria-selected'), 'false');
    assert.equal(cropTab.tabIndex, 0);
    assert.equal(frameTab.tabIndex, -1);
    assert.equal(element('#frame-panel').hidden, true, 'choosing Crop hides the Frame panel');
    assert.equal(element('#crop-panel').hidden, false);
    assert.equal(stored['lumen-frame-tab'], 'crop-tab', 'the chosen frame tab is remembered');
    assert.equal(stored['lumen-tab'], undefined, 'the frame tabs leave the main tab row alone');
    await key('ArrowLeft', cropTab, cropTab);
    assert.equal(frameTab.getAttribute('aria-selected'), 'true', 'ArrowLeft on Crop selects Frame');
    assert.equal(focused, frameTab);
    await key('End', frameTab, frameTab);
    assert.equal(exifTab.getAttribute('aria-selected'), 'true', 'End selects the last tab, EXIF');
    assert.equal(focused, exifTab);
    assert.equal(exportOptions.hidden, false, 'EXIF still offers the export');
    await new Promise(setImmediate);
    assert.deepEqual(exifRequests, ['/api/photos/1/exif'], 'EXIF is read when its tab opens');
    assert.deepEqual(element('#exif-fields').children.map(node => [node.tagName, node.textContent]), [['DT', 'Camera'], ['DD', 'ILCE-7CM2'], ['DT', 'ISO'], ['DD', '100']]);
    assert.equal(element('#exif-status').textContent, '');
    await key('ArrowLeft', exifTab, exifTab);
    assert.equal(developTabButton.getAttribute('aria-selected'), 'true', 'Develop sits just before EXIF');
    assert.equal(exportOptions.hidden, false, 'Develop keeps the export options');
    await key('Home', developTabButton, developTabButton);
    assert.equal(frameTab.getAttribute('aria-selected'), 'true');
    await key('ArrowRight', frameTab, frameTab);
    assert.equal(cropTab.getAttribute('aria-selected'), 'true');
    await key('Escape', element('#loupe-viewport'), loupe);
    assert.equal(loupe.hidden, true);
    frameTab.listeners.click();
    stored['lumen-frame-tab'] = 'crop-tab';
    await key('Enter', card(1), element('#contact-sheet'));
    await new Promise(setImmediate);
    assert.equal(cropTab.getAttribute('aria-selected'), 'true', 'reopening restores the remembered Crop tab');
    assert.equal(element('#crop-panel').hidden, false);
    const cropButton = Object.assign(new Element(), { tagName: 'BUTTON' });
    element('#crop-panel').append(cropButton);
    await key('x', cropButton, loupe);
    assert.deepEqual(decisionPosts.at(-1), { url: '/api/photos/1/decision', body: { decision: 'reject' } }, 'culling keys work from the Crop panel');
    return;
  }
  if (scenario === 'develop' || scenario === 'develop-offline') {
    await load();
    const card = id => document.querySelectorAll('.frame').find(item => Number(item.dataset.id) === id);
    const key = (name, target, node) => node.listeners.keydown({ key: name, metaKey: false, ctrlKey: false, altKey: false, target, preventDefault() {}, stopPropagation() {} });
    Object.assign(element('#loupe'), { tagName: 'DIALOG', id: 'loupe' });
    const viewport = Object.assign(element('#loupe-viewport'), { tagName: 'DIV', clientWidth: 800, clientHeight: 600 });
    const tab = element('#develop-tab');
    const settled = () => new Promise(setImmediate);
    const quiet = where => {  // a DOM bug reported as a render failure, or any failure at all, fails the scenario
      const text = element(where).textContent || '';
      assert.ok(!/could not|retried|unavailable|undefined|NaN|error/i.test(text), where + ' reads: ' + text);
      return text;
    };
    const flush = async () => { await settled(); quiet('#develop-status'); quiet('#announcer'); };
    const field = name => element('#develop-' + name + '-value');
    await key('Home', card(1), element('#contact-sheet'));
    await key('Enter', card(1), element('#contact-sheet'));
    await flush();
    if (scenario === 'develop-offline') {
      assert.equal(tab.getAttribute('aria-disabled'), 'true', 'an offline original leaves the tab in the rail but unavailable');
      assert.ok(element('#develop-unavailable').textContent.includes('offline'), element('#develop-unavailable').textContent);
      assert.equal(element('#develop-unavailable').hidden, false);
      assert.equal(element('#develop-workspace').hidden, true);
      assert.equal(element('#develop-apply-group').disabled, true, 'a burst cannot be developed from a frame that cannot be rendered');
      assert.equal(element('#develop-exposure_ev').disabled, true);
      assert.equal(element('#develop-point-0').getAttribute('tabindex'), '-1');
      const [frameTab, , developTabButton, exifTab] = frameTabElements;
      await key('End', frameTab, frameTab);
      await key('ArrowLeft', exifTab, exifTab);
      assert.equal(developTabButton.getAttribute('aria-selected'), 'false', 'arrows skip the unavailable tab');
      assert.equal(element('#crop-tab').getAttribute('aria-selected'), 'true');
      assert.deepEqual(developPosts, [], 'nothing is rendered without the raw');
      return;
    }
    assert.equal(tab.getAttribute('aria-disabled'), 'false');
    assert.equal(element('#develop-workspace').hidden, false);
    tab.listeners.click();
    await flush();
    assert.deepEqual(developPosts.map(post => [post.url, post.body]), [['/api/photos/1/develop/preview', { settings: { exposure_ev: 0.5 }, long_edge: 800 }]],
      'the saved settings render as they are, at the viewport long edge');
    assert.equal(viewport.children[0].src, 'data:image/jpeg;base64,RENDER', 'the viewport shows the render, whole and uncropped');
    assert.equal(viewport.children[0].className, 'fit');
    assert.equal(element('#develop-histogram-y').getAttribute('d').startsWith('M0.00,60'), true, 'the luma band is drawn behind the curve');
    assert.ok(['y', 'r', 'g', 'b'].every(channel => element('#develop-histogram-' + channel).getAttribute('d')), 'every channel starts shown');
    // B holds 0.8 in its first bucket, above every other bucket in the fixture. Scaled to it,
    // the rest of the distribution would draw flat along the floor; the end buckets are left
    // out of the scale, so Y's 0.4 is the tallest bar and B's clipped bucket clamps to it.
    assert.ok(element('#develop-histogram-y').getAttribute('d').includes(',0.00'), "the tallest bucket that is not clipping reaches the top");
    assert.ok(element('#develop-histogram-b').getAttribute('d').startsWith('M0.00,60L0.00,0.00'), 'a clipped end bucket clamps to full height');
    // Y holds the tallest bucket in this fixture, so hiding it is what would rescale the rest
    // if the scale were taken over the shown channels instead of all four.
    const beforeHiding = element('#develop-histogram-r').getAttribute('d');
    const luma = element('#develop-channel-y');
    luma.checked = false;
    luma.listeners.change();
    assert.equal(element('#develop-histogram-y').getAttribute('d'), '', 'unchecking a channel removes its band');
    assert.equal(element('#develop-histogram-r').getAttribute('d'), beforeHiding, 'the other bands keep their scale');
    assert.equal(element('#announcer').textContent, 'Y hidden', 'the toggle is announced');
    luma.checked = true;
    luma.listeners.change();
    assert.ok(element('#develop-histogram-y').getAttribute('d'), 'checking it back redraws the band');
    assert.equal(field('exposure_ev').value, '0.50');
    assert.equal(field('contrast').value, '', 'an untouched field shows nothing, because the file sets it');
    assert.equal(element('#develop-temperature_k').value, '5600', "white balance parks on this frame's own neutral");
    // The decoder already applies full sharpening to a Sony raw. Parked at the slider's
    // midpoint the handle invited sharpening that cannot happen; it parks on the real value.
    assert.equal(element('#develop-sharpness').value, '1', 'the handle parks on what the decoder actually applies');
    assert.equal(element('#develop-contrast').value, '0', 'not the middle of the slider');
    assert.equal(element('#develop-sharpness').getAttribute('aria-valuetext'), 'not set', 'parking is still not a value this frame sets');

    // Noise reduction mode is derived from the two amounts, never stored beside them.
    const nrMode = () => nrModes.find(radio => radio.checked).value;
    const pick = value => { const radio = nrModes.find(r => r.value === value); nrModes.forEach(r => { r.checked = r === radio; }); radio.listeners.change(); };
    assert.equal(nrMode(), 'auto', 'an untouched frame leaves the decoder its own baseline');
    assert.equal(element('#develop-nr-manual').hidden, true, 'the amounts are hidden until they apply');
    pick('off');
    await settled();
    assert.deepEqual([state.develop.fields.luminance_nr, state.develop.fields.color_nr], [0, 0], 'Off asks for none at all');
    assert.equal(element('#develop-nr-manual').hidden, true);
    pick('manual');
    await settled();
    assert.deepEqual([state.develop.fields.luminance_nr, state.develop.fields.color_nr], [0.004, 0.5],
      "Manual starts from what the decoder reported, so the first render matches the one before it");
    assert.equal(element('#develop-nr-manual').hidden, false, 'the amounts appear once they apply');
    pick('auto');
    await settled();
    assert.ok(!('luminance_nr' in state.develop.fields) && !('color_nr' in state.develop.fields), 'Auto leaves both unset');

    // Lens correction is a flag, and the panel only offers it when the file carries a profile.
    const lens = element('#develop-lens_correction');
    assert.equal(lens.disabled, false, 'this render reported a profile');
    lens.checked = true;
    lens.listeners.change();
    await settled();
    assert.equal(state.develop.fields.lens_correction, true);
    lens.checked = false;
    lens.listeners.change();
    await settled();
    assert.ok(!('lens_correction' in state.develop.fields), 'unchecking removes it rather than sending false');

    // Full size on the Develop tab renders the raw again rather than swapping in the
    // camera's embedded JPEG, which shows none of the photographer's settings.
    const full = element('#loupe-full');
    developPosts.length = 0;
    full.listeners.click();
    await settled();
    assert.deepEqual(developPosts.map(post => post.body.long_edge), [0], "zoomed, the panel asks for the raw's own size");
    assert.equal(full.getAttribute('aria-pressed'), 'true');
    assert.notEqual(viewport.children[0].className, 'fit', 'at 1:1 the image is not scaled to the viewport');
    assert.equal(viewport.children[0].src, 'data:image/jpeg;base64,RENDER', 'it is the develop render, not /full/');
    // A setting changed while zoomed stays zoomed, or sharpening cannot be judged at all.
    developPosts.length = 0;
    const sharp = element('#develop-sharpness');
    sharp.value = '0.2';
    sharp.listeners.change();
    await settled();
    assert.deepEqual(developPosts.map(post => post.body.long_edge), [0], 'still at full size after a change');
    developPosts.length = 0;
    full.listeners.click();
    await settled();
    assert.deepEqual(developPosts.map(post => post.body.long_edge), [800], 'toggling off returns to the viewport size');
    assert.equal(viewport.children[0].className, 'fit');
    element('#develop-sharpness-reset').listeners.click();  // leave the frame as this scenario's later checks expect it
    await settled();
    developPosts.length = 0;
    // Leaving the tab while zoomed hands the viewport back: the other tabs' full-size button
    // means the camera's embedded JPEG, and cannot toggle a develop render back out.
    full.listeners.click();
    await settled();
    assert.equal(full.getAttribute('aria-pressed'), 'true');
    element('#crop-tab').listeners.click();
    await settled();
    assert.equal(full.getAttribute('aria-pressed'), 'false', 'the develop zoom is handed back');
    assert.equal(viewport.children[0].className, 'fit');
    element('#develop-tab').listeners.click();
    await settled();
    developPosts.length = 0;
    assert.equal(field('temperature_k').value, '', 'parking a handle is not an edit');
    assert.equal(element('#develop-contrast-reset').disabled, true, 'nothing to reset until a field is set');
    assert.equal(element('#develop-status').textContent, '1 setting applied to this render. Nothing is saved until you leave the frame or export.');
    assert.equal(element('#develop-contrast').getAttribute('aria-valuetext'), 'not set', 'a parked handle is announced as unset, not as its midpoint');
    assert.equal(element('#develop-contrast').getAttribute('data-unset'), '');
    assert.equal(element('#develop-exposure_ev').getAttribute('aria-valuetext'), '0.50 EV');
    assert.equal(element('#develop-exposure_ev').getAttribute('data-unset'), undefined);

    const contrast = element('#develop-contrast');
    contrast.value = '0.3';
    await contrast.listeners.change();
    await flush();
    assert.deepEqual(developPosts.at(-1).body.settings, { exposure_ev: 0.5, contrast: 0.3 }, 'only the fields the photographer touched are sent');
    assert.equal(field('contrast').value, '0.30');
    assert.equal(element('#develop-contrast').getAttribute('aria-valuetext'), '0.30');
    assert.equal(element('#develop-contrast').getAttribute('data-unset'), undefined, 'a set control is no longer marked unset');
    assert.equal(element('#announcer').textContent, 'Contrast 0.30');
    await element('#develop-contrast-reset').listeners.click();
    await flush();
    assert.deepEqual(developPosts.at(-1).body.settings, { exposure_ev: 0.5 }, 'Reset deletes the field rather than setting it to zero');
    assert.equal(field('contrast').value, '');
    assert.equal(element('#develop-contrast').getAttribute('aria-valuetext'), 'not set', 'Reset puts the control back to unset');
    assert.ok(element('#announcer').textContent.includes("own value"));
    field('tint').value = '-40';
    await field('tint').listeners.change();
    await flush();
    assert.deepEqual(developPosts.at(-1).body.settings, { exposure_ev: 0.5, tint: -40 }, 'the numeric field sets the same key');
    field('tint').value = '';
    await field('tint').listeners.change();
    await flush();
    assert.deepEqual(developPosts.at(-1).body.settings, { exposure_ev: 0.5 }, 'emptying the numeric field clears the key');

    element('#develop-sharpness').value = '0.4';
    element('#develop-sharpness').listeners.change();  // left in flight on purpose
    element('#develop-sharpness').value = '0.6';
    element('#develop-sharpness').listeners.change();
    await flush();
    assert.equal(developPosts.at(-2).signal.aborted, true, 'the older preview request is aborted when a newer one starts');
    assert.equal(developPosts.at(-1).signal.aborted, false);
    assert.equal(developPosts.at(-1).body.settings.sharpness, 0.6);
    await element('#develop-sharpness-reset').listeners.click();
    await flush();

    const svg = Object.assign(element('#develop-curve'), { box: { left: 0, top: 0, width: 200, height: 120 } });
    const pointer = (type, x, y) => svg.listeners[type]({ button: 0, pointerId: 1, clientX: x, clientY: y, preventDefault() {} });
    const renders = developPosts.length;
    pointer('pointerdown', 100, 30);
    pointer('pointermove', 100, 24);
    assert.equal(developPosts.length, renders, 'a drag in progress renders nothing');
    assert.equal(element('#develop-point-2').getAttribute('cy'), '12.00', 'the dragged point follows the pointer');
    pointer('pointerup', 100, 24);
    await flush();
    assert.equal(developPosts.length, renders + 1, 'one gesture, one render');
    const curve = developPosts.at(-1).body.settings.curve;
    assert.equal(curve.length, 5);
    assert.deepEqual(curve[2], [0.5, 0.8], 'the middle point took the release position');
    assert.ok(curve.every((point, index) => index === 0 || point[0] > curve[index - 1][0]), 'x values strictly ascend: ' + JSON.stringify(curve));
    pointer('pointerdown', 100, 24);
    pointer('pointerup', 190, 24);
    await flush();
    const squeezed = developPosts.at(-1).body.settings.curve;
    assert.ok(Math.abs(squeezed[2][0] - 0.74) < 1e-9, 'a point stops short of its neighbour, so x values never tie: ' + squeezed[2][0]);
    const point = element('#develop-point-1');
    assert.equal(point.getAttribute('tabindex'), '0', 'every point is reachable by keyboard');
    point.listeners.keydown({ key: 'ArrowUp', preventDefault() {}, stopPropagation() {} });
    await flush();
    assert.ok(Math.abs(developPosts.at(-1).body.settings.curve[1][1] - 0.26) < 1e-9, 'an arrow key nudges by one percent');
    assert.ok(element('#announcer').textContent.startsWith('Tone curve point 2'), element('#announcer').textContent);

    await element('#develop-curve-reset').listeners.click();
    await flush();
    assert.equal(state.develop.fields.curve, undefined, 'the curve resets on its own, without discarding the sliders');
    assert.equal(developPosts.at(-1).body.settings.exposure_ev, 0.5, 'the sliders keep their values');
    assert.ok(!('curve' in developPosts.at(-1).body.settings));
    assert.equal(element('#develop-point-2').getAttribute('cy'), '30.00', 'the widget is drawn straight again');
    point.listeners.keydown({ key: 'ArrowUp', preventDefault() {}, stopPropagation() {} });
    await flush();

    // 6b. the suggestion: the model parks the handles and says why, and Discard puts them back.
    const ask = element('#develop-ask');
    const discard = element('#develop-discard-advice');
    assert.equal(ask.disabled, false, 'the review model is loaded in this scenario');
    assert.equal(discard.disabled, true, 'nothing to discard until a suggestion arrives');
    const held = { ...state.develop.fields };
    element('#develop-feedback').value = '  too warm  ';
    await ask.listeners.click();
    await flush();
    assert.deepEqual(suggestPosts.at(-1), { settings: held, feedback: 'too warm' },
      'the model reads the handles as they stand, and the typed words are trimmed');
    assert.deepEqual(state.develop.fields, { exposure_ev: 0.8, clarity: 0.2 }, 'the handles park on the answer');
    assert.equal(field('exposure_ev').value, '0.80');
    assert.ok(element('#develop-advice').textContent.includes('nothing reaches white'), 'the rationale is the product');
    assert.equal(element('#develop-feedback').value, '', 'the box empties, ready for the next correction');
    assert.equal(discard.disabled, false);
    assert.deepEqual(developPosts.at(-1).body.settings, { exposure_ev: 0.8, clarity: 0.2 }, 'the preview re-renders on the advice');
    assert.ok(!developPosts.some(post => post.url.endsWith('/develop')), 'nothing is saved by a suggestion');

    await ask.listeners.click();  // a second attempt, with no new words
    await flush();
    assert.deepEqual(suggestPosts.at(-1), { settings: { exposure_ev: 0.8, clarity: 0.2 }, feedback: null },
      'attempt two critiques its own first answer');
    assert.deepEqual(state.develop.fields, { exposure_ev: 0.6 }, 'the latest attempt replaces the one before it');
    assert.ok(element('#develop-advice').textContent.includes('Pulled back'));

    await discard.listeners.click();
    await flush();
    assert.deepEqual(state.develop.fields, held, 'Discard goes back past every attempt, not just the last');
    assert.equal(element('#develop-advice').textContent, '');
    assert.equal(discard.disabled, true);
    assert.deepEqual(developPosts.at(-1).body.settings, held, 'the preview goes back too');

    // 7. the frame dialog's copy of the export checkbox governs what its own export buttons do
    const mirror = element('#loupe-keep-original');
    mirror.checked = true;
    mirror.listeners.change();
    assert.equal(element('#export-keep-original').checked, true, 'the two checkboxes stay in step');
    element('#export-keep-original').checked = false;
    element('#export-keep-original').listeners.change();
    assert.equal(mirror.checked, false);
    const dated = element('#loupe-dated-folder');
    dated.checked = false;
    dated.listeners.change();
    assert.equal(element('#export-dated-folder').checked, false, 'the dated-folder boxes stay in step');
    await element('#loupe-export').listeners.click();
    await flush();
    assert.equal(exportPosts.at(-1).dated_folder, false, 'unchecking it exports loose into the JPEG folder');
    dated.checked = true;
    dated.listeners.change();

    await element('#develop-apply-group').listeners.click();
    assert.equal(developPosts.at(-2).url, '/api/photos/1/develop', 'the settings are stored before the job copies them');
    assert.deepEqual([developPosts.at(-1).url, developPosts.at(-1).body], ['/api/photos/1/develop/apply-to-group', {}]);
    assert.equal(element('#export-dialog').hidden, false, 'the burst job uses the existing progress dialog');
    assert.equal(element('#export-dialog-heading').textContent, 'Apply develop to burst');
    await settle();
    assert.ok(element('#export-dialog-status').textContent.includes('2 developed, 1 skipped, 0 failed.'), element('#export-dialog-status').textContent);
    assert.ok(!(element('#export-status').textContent || '').includes('Apply develop'), 'a develop job never writes into the export panel');
    assert.equal(card(1).children[0].src, '/thumb/1?v=saved-developed', 'the contact-sheet tile follows the re-rendered preview');

    const saves = developPosts.filter(post => post.url === '/api/photos/1/develop').length;
    element('#close-loupe').listeners.click();
    await flush();
    assert.equal(developPosts.filter(post => post.url === '/api/photos/1/develop').length, saves, 'nothing changed since the last save, so closing saves nothing');
    const stored = developPosts.filter(post => post.url === '/api/photos/1/develop').at(-1).body;
    assert.deepEqual(Object.keys(stored), ['settings']);
    assert.equal(stored.settings.exposure_ev, 0.5);
    assert.equal(stored.settings.curve.length, 5);
    assert.ok(!('contrast' in stored.settings) && !('tint' in stored.settings), 'a reset field is never saved');

    await key('Enter', card(1), element('#contact-sheet'));
    await flush();
    element('#develop-exposure_ev').value = '2';
    element('#develop-exposure_ev').listeners.change();
    await flush();
    developSaveError = 'the catalog is busy';
    holdUrls.add('/api/photos/1/develop');
    await key(']', element('#loupe-viewport'), element('#loupe'));  // the filmstrip steps on while the save is held
    for (let i = 0; i < 6; i++) await settled();
    assert.equal(state.develop.photoId, 2);
    const showing = element('#develop-status').textContent || '';
    heldReplies.shift()();
    await settled();
    await settled();
    assert.equal(element('#develop-status').textContent || '', showing, "a failed save never writes on another frame's panel");
    const spoken = element('#announcer').textContent;
    assert.ok(spoken.includes('photo-1.jpg') && spoken.includes('could not be saved'), 'the failure names the frame it belongs to: ' + spoken);
    return;
  }
  if (scenario === 'frame-view-full') {
    groups[0].photos.forEach(item => { item.preview_url = '/preview/' + item.id + '?v=abc-embedded'; });
    await load();
    const card = id => document.querySelectorAll('.frame').find(item => Number(item.dataset.id) === id);
    const key = (name, target, node) => node.listeners.keydown({ key: name, metaKey: false, ctrlKey: false, altKey: false, target, preventDefault() {}, stopPropagation() {} });
    const loupe = Object.assign(element('#loupe'), { tagName: 'DIALOG', id: 'loupe' });
    const viewport = Object.assign(element('#loupe-viewport'), { tagName: 'DIV', scrollLeft: 0, scrollTop: 0 });
    const full = element('#loupe-full');
    await key('Home', card(1), element('#contact-sheet'));
    await key('Enter', card(1), element('#contact-sheet'));
    assert.equal(full.getAttribute('aria-pressed'), 'false');
    full.listeners.click();
    assert.equal(full.getAttribute('aria-pressed'), 'true', 'pressed as soon as the swap starts');
    let image = viewport.children[0];
    assert.equal(image.src, '/full/1?v=abc-embedded', 'the full-size image carries the preview version');
    Object.assign(viewport, { scrollWidth: 3504, clientWidth: 1000, scrollHeight: 2336, clientHeight: 800 });
    Object.assign(image, { naturalWidth: 3504, naturalHeight: 2336 });
    image.listeners.load();
    assert.deepEqual([image.className, image.width, image.height], ['', 3504, 2336], 'full size shows 1:1 pixels');
    assert.equal(full.textContent, 'Full size, 3504 by 2336', 'the button names the real pixel size');
    assert.equal(full.getAttribute('aria-pressed'), 'true');
    assert.deepEqual([viewport.scrollLeft, viewport.scrollTop], [1252, 768], 'zooming in centres the view');
    await key('z', viewport, loupe);
    assert.equal(viewport.children[0].src, '/preview/1?v=abc-embedded', 'z returns to the preview');
    assert.equal(full.getAttribute('aria-pressed'), 'false');
    assert.equal(full.textContent, 'Full size');
    await key('z', viewport, loupe);
    assert.equal(viewport.children[0].src, '/full/1?v=abc-embedded', 'z zooms in again');
    await key('z', viewport, loupe);
    assert.equal(viewport.children[0].src, '/preview/1?v=abc-embedded', 'a second z while it loads returns to the preview');
    await key(']', viewport, loupe);
    assert.equal(viewport.children[0].src, '/preview/2?v=abc-embedded', 'stepping resets to the preview');
    assert.equal(full.getAttribute('aria-pressed'), 'false');
    full.listeners.click();
    await viewport.children[0].listeners.error();
    assert.equal(full.disabled, true);
    assert.equal(full.textContent, 'no full-size image in this file');
    assert.equal(full.getAttribute('aria-pressed'), 'false');
    assert.equal(viewport.children[0].src, '/preview/2?v=abc-embedded', 'a missing full-size image keeps the preview');
    await key('[', viewport, loupe);
    assert.equal(full.disabled, false, 'the next frame gets its own chance');
    assert.equal(full.textContent, 'Full size');
    fullStatus = 503;
    full.listeners.click();
    await viewport.children[0].listeners.error();
    assert.equal(full.textContent, 'original is offline', 'an unplugged volume is named, not blamed on the file');
    return;
  }
  if (scenario === 'crop-draw') {
    await load();
    const card = id => document.querySelectorAll('.frame').find(item => Number(item.dataset.id) === id);
    const key = (name, target, node, stop = () => {}) => node.listeners.keydown({ key: name, metaKey: false, ctrlKey: false, altKey: false, target, preventDefault() {}, stopPropagation: stop });
    const loupe = Object.assign(element('#loupe'), { tagName: 'DIALOG', id: 'loupe' });
    const viewport = Object.assign(element('#loupe-viewport'), { tagName: 'DIV', scrollLeft: 0, scrollTop: 0 });
    const overlay = element('#crop-overlay');
    const outline = element('#crop-outline');
    const loadImage = (width, height) => { Object.assign(viewport.children[0], { naturalWidth: width, naturalHeight: height }); viewport.children[0].listeners.load(); };
    const pointer = (type, x, y, shiftKey = false) => viewport.listeners[type]({ button: 0, pointerId: 1, clientX: x, clientY: y, shiftKey, target: viewport.children[0], preventDefault() {} });
    const drag = (x0, y0, x1, y1, shiftKey = false) => { pointer('pointerdown', x0, y0, shiftKey); pointer('pointermove', x1, y1); pointer('pointerup', x1, y1); };
    const near = (actual, expected, message) => Object.entries(expected).forEach(([side, value]) => assert.ok(Math.abs(actual[side] - value) < 0.001, message + ': ' + side + ' ' + actual[side] + ' vs ' + value));
    await key('Home', card(1), element('#contact-sheet'));
    await key('Enter', card(1), element('#contact-sheet'));
    loadImage(2048, 1365);
    assert.equal(viewport.children[0].draggable, false, 'the image never starts a native drag');
    assert.equal(state.manualCrop, null);
    let stopped = false;
    pointer('pointerdown', 200, 100);
    pointer('pointermove', 600, 500);
    assert.equal(overlay.hidden, false, 'a drag in progress shows the outline');
    await key('Escape', viewport, viewport, () => { stopped = true; });
    pointer('pointerup', 600, 500);
    assert.ok(stopped, 'Escape during a drag stops before the dialog sees it');
    assert.equal(state.manualCrop, null, 'Escape cancels the drag');
    assert.equal(overlay.hidden, true);
    assert.equal(openView?.id, 'loupe', 'Escape during a drag keeps the frame view open');
    assert.equal(viewport.children[0].className, 'fit', 'the preview is fitted to the viewport');
    assert.equal(viewport.children[0].width, undefined, 'a fitted preview takes its size from CSS');
    focused = element('#crop-lock');
    pointer('pointerdown', 200, 100);
    assert.equal(focused, viewport, 'a drag moves focus to the viewport, so Escape reaches the cancel');
    assert.deepEqual(viewport.focusOptions, { preventScroll: true });
    pointer('pointerup', 200, 100);
    assert.equal(element('#crop-tab').getAttribute('aria-selected'), 'false', 'a slip keeps the Frame tab');
    drag(200, 100, 1200, 800);
    const expected = { left: 200 / 2048, top: 100 / 1365, width: 1000 / 2048, height: 700 / 1365 };
    near(state.manualCrop.rect, expected, 'preview rect');
    assert.equal(state.manualCrop.aspect, 'free');
    assert.equal(element('#announcer').textContent, 'Crop drawn: 1000 by 700 pixels');
    assert.equal(element('#crop-tab').getAttribute('aria-selected'), 'true', 'drawing shows the Crop tab with the card and save button');
    assert.equal(element('#crop-panel').hidden, false);
    assert.equal(element('#frame-panel').hidden, true);
    assert.equal(overlay.hidden, false);
    assert.ok(viewport.contains(overlay), 'the overlay sits inside the viewport over the image');
    assert.deepEqual([overlay.style.width, overlay.style.height], ['2048px', '1365px'], "the overlay covers the image's rendered box");
    assert.deepEqual([outline.style.left, outline.style.top, outline.style.width, outline.style.height], ['9.765625%', 100 / 1365 * 100 + '%', '48.828125%', 700 / 1365 * 100 + '%']);
    const before = state.manualCrop;
    drag(500, 500, 510, 505);
    assert.equal(state.manualCrop, before, 'a 10 px drag is ignored');
    element('#loupe-full').listeners.click();
    loadImage(4096, 2730);
    assert.ok(viewport.contains(overlay) && !overlay.hidden, 'the outline is redrawn at full size');
    assert.deepEqual([overlay.style.width, overlay.style.height], ['4096px', '2730px']);
    drag(400, 200, 2400, 1600, true);  // Shift: redraw even though the press lands inside the old outline
    near(state.manualCrop.rect, expected, 'full-size rect matches the preview rect');
    const lock = element('#crop-lock');
    lock.value = '1:1';
    lock.listeners.change();
    assert.equal(stored['lumen-crop-lock'], '1:1');
    assert.equal(cropLockValue(), '1:1');
    element('#loupe-full').listeners.click();
    loadImage(2048, 1365);
    drag(0, 0, 900, 300, true);
    const { rect, aspect } = state.manualCrop;
    assert.equal(aspect, '1:1');
    assert.ok(Math.abs(rect.width * 2048 - rect.height * 1365) < 1, 'a 1:1 lock keeps a square: ' + rect.width * 2048 + ' by ' + rect.height * 1365);
    assert.ok(Math.abs(rect.width * 2048 - 900) < 1, 'the height follows the width');
    assert.equal(element('#announcer').textContent, 'Crop drawn, 1:1: 900 by 900 pixels', 'a lock other than Free is named');
    // Pressing inside the outline moves it without changing its size; the edges stop it.
    drag(450, 150, 650, 250);
    near(state.manualCrop.rect, { left: rect.left + 200 / 2048, top: rect.top + 100 / 1365, width: rect.width, height: rect.height }, 'moved rect');
    assert.equal(state.manualCrop.aspect, '1:1', 'a move keeps the aspect it was drawn with');
    assert.equal(element('#announcer').textContent, 'Crop moved to 200, 100');
    const moved = state.manualCrop.rect;
    drag(700, 300, 5000, 5000);
    near(state.manualCrop.rect, { left: 1 - moved.width, top: 1 - moved.height, width: moved.width, height: moved.height }, 'a move stops at the image edges');
    drag(2000, 1300, 1500, 1000);
    assert.ok(state.manualCrop.rect.width * 2048 > 400, 'a press outside the outline draws a new crop');
    const fitted = viewport.children[0];
    fitted.box = { left: 0, top: 0, width: 1024, height: 682.5 };  // shown at half its natural size
    observers.find(([node]) => node === viewport)[1]();
    assert.deepEqual([overlay.style.width, overlay.style.height], ['1024px', '682.5px'], 'a viewport resize redraws the overlay at the shown size');
    lock.value = 'free';
    drag(100, 50, 600, 400);
    near(state.manualCrop.rect, expected, 'a half-size display gives the same rect as the same drag at scale 1');
    drag(500, 300, 510, 310);
    near(state.manualCrop.rect, expected, 'the slip check counts displayed pixels: 10 shown px is still a slip');
    lock.value = '3:2';
    fitted.box = { left: 0, top: 0, width: 893, height: 595.3 };  // a 2048 px preview fitted to an ordinary frame-view column
    drag(45, 30, 848, 500);
    const candidate = manualCandidate(state.manualCrop.rect, state.manualCrop.aspect, { source_width: 6000, source_height: 4000 });
    assert.ok(candidate.pixel_count > 8_000_000, 'a light 3:2 trim drawn on the fitted preview keeps over 8 MP: ' + candidate.pixel_count);
    lock.value = 'free';
    await key(']', viewport, loupe);
    assert.equal(state.manualCrop, null, 'stepping clears the drawn crop');
    assert.equal(overlay.hidden, true);
    return;
  }
  if (scenario === 'crop-manual-card') {
    await load();
    const card = id => document.querySelectorAll('.frame').find(item => Number(item.dataset.id) === id);
    const key = (name, target, node) => node.listeners.keydown({ key: name, metaKey: false, ctrlKey: false, altKey: false, target, preventDefault() {}, stopPropagation() {} });
    const flush = async () => { for (let i = 0; i < 6; i++) await new Promise(setImmediate); };
    const loupe = Object.assign(element('#loupe'), { tagName: 'DIALOG', id: 'loupe' });
    const viewport = Object.assign(element('#loupe-viewport'), { tagName: 'DIV', scrollLeft: 0, scrollTop: 0 });
    const overlay = element('#crop-overlay');
    const outline = element('#crop-outline');
    const loadImage = (width, height) => { Object.assign(viewport.children[0], { naturalWidth: width, naturalHeight: height }); viewport.children[0].listeners.load(); };
    const pointer = (type, x, y) => viewport.listeners[type]({ button: 0, pointerId: 1, clientX: x, clientY: y, target: viewport.children[0], preventDefault() {} });
    const drag = (x0, y0, x1, y1) => { pointer('pointerdown', x0, y0); pointer('pointermove', x1, y1); pointer('pointerup', x1, y1); };
    const first = () => element('#primary-crops').children[0];
    const heading = () => first().children[0].textContent;
    await key('Home', card(1), element('#contact-sheet'));
    await key('Enter', card(1), element('#contact-sheet'));
    loadImage(2048, 1365);
    await flush();
    assert.equal(heading(), 'Original', 'no card until a crop is drawn');
    drag(200, 100, 1200, 800);
    assert.equal(heading(), 'Your crop', 'the drawn crop is the first card at once');
    const pixels = Math.round(6000 * 1000 / 2048) * Math.round(4000 * 700 / 1365);
    assert.equal(first().children[2].textContent, 'Free · ' + (pixels / 1e6).toFixed(1) + ' MP remaining');
    assert.equal(first().children.at(-1).getAttribute('aria-label'), 'Use your crop, Free');
    assert.equal(first().children[3].textContent, 'drawn by the photographer');
    const warning = first().children[4];
    assert.equal(warning.textContent, (pixels / 1e6).toFixed(6) + ' MP remaining; below 12 MP');
    const small = first().children.at(-1);
    assert.equal(small.textContent, 'Use this crop');
    assert.equal(small.disabled, false, 'under 8 MP is still allowed');
    assert.equal(small.getAttribute('aria-describedby'), warning.id);
    assert.ok(warning.id, 'the warning has an id to describe the button');
    assert.equal(first().children[5].textContent, '', 'a fresh drawing is not the current choice');
    drag(0, 0, 2000, 1300);
    const { rect } = state.manualCrop;
    const use = first().children.at(-1);
    assert.equal(use.disabled, false, 'a large enough crop can be used');
    assert.equal(first().children[4].textContent, '', 'over 12 MP has no warning');
    holdUrls.add('/api/photos/1/crop');
    const saving = use.listeners.click();
    await flush();
    drag(100, 100, 1900, 1200);
    const newer = state.manualCrop;
    heldReplies.shift()();
    await saving;
    await flush();
    assert.equal(state.manualCrop, newer, 'a drawing made while the save was in flight survives it');
    drag(0, 0, 2000, 1300);
    await first().children.at(-1).listeners.click();
    await flush();
    assert.deepEqual(cropMutations.at(-1), { url: '/api/photos/1/crop', body: { candidate_id: 'crop-manual', rect, aspect: 'free' } });
    assert.equal(element('#announcer').textContent, 'Your crop selected.');
    assert.equal(state.manualCrop, null, 'the saved crop now comes from active_crop');
    assert.equal(heading(), 'Your crop');
    assert.equal(first().children[5].textContent, 'Current choice');
    assert.equal(first().children.at(-1).disabled, true);
    assert.equal(overlay.hidden, false, 'the saved crop stays outlined');
    await key(']', viewport, loupe);
    await flush();
    assert.equal(heading(), 'Original', "frame 2 has no drawn crop");
    assert.equal(overlay.hidden, true);
    holdUrls.add('/api/photos/1/crops');
    await key('[', viewport, loupe);
    loadImage(2048, 1365);
    await flush();
    assert.equal(state.manualCrop, null);
    assert.equal(element('#compose-workspace').hidden, true, 'no card until the crops arrive');
    assert.equal(overlay.hidden, true, 'no outline until the crops arrive');
    heldReplies.shift()();
    await flush();
    assert.equal(heading(), 'Your crop', 'a saved drawn crop returns on reopen');
    assert.equal(first().children[5].textContent, 'Current choice');
    assert.equal(overlay.hidden, false, 'the saved crop is outlined');
    assert.deepEqual([outline.style.left, outline.style.width], [rect.left * 100 + '%', rect.width * 100 + '%']);
    await key(']', viewport, loupe);
    await flush();
    await key('[', viewport, loupe);
    await flush();
    assert.equal(heading(), 'Your crop');
    assert.equal(overlay.hidden, true, 'the outline waits for the image');
    loadImage(2048, 1365);
    assert.equal(overlay.hidden, false, 'the image load draws the saved outline');
    assert.deepEqual([overlay.style.width, outline.style.top], ['2048px', rect.top * 100 + '%']);
    return;
  }
  if (scenario === 'loupe-strip') {
    await load();
    const card = id => document.querySelectorAll('.frame').find(item => Number(item.dataset.id) === id);
    let prevented = 0;
    const key = (name, target, node) => node.listeners.keydown({ key: name, metaKey: false, ctrlKey: false, altKey: false, target, preventDefault() { prevented += 1; }, stopPropagation() {} });
    const sheetNode = element('#contact-sheet');
    const loupe = Object.assign(element('#loupe'), { tagName: 'DIALOG', id: 'loupe' });
    const viewport = Object.assign(element('#loupe-viewport'), { tagName: 'DIV' });
    const announced = () => element('#announcer').textContent;
    await key('Home', card(1), sheetNode);
    await key('Enter', card(1), sheetNode);
    assert.equal(loupe.hidden, false);
    assert.equal(element('#loupe-heading').textContent, 'photo-1.jpg');
    await key('[', viewport, loupe);
    assert.equal(announced(), 'First frame');
    assert.equal(element('#loupe-heading').textContent, 'photo-1.jpg', 'the first edge changes nothing');
    assert.equal(state.focusedId, 1);
    await key(']', viewport, loupe);
    await key(']', viewport, loupe);
    assert.equal(announced(), 'photo-3.jpg, 3 of 3');
    await key(']', viewport, loupe);
    assert.equal(announced(), 'Last frame');
    assert.equal(state.focusedId, 3, 'the last edge changes nothing');
    await key('[', viewport, loupe);
    assert.equal(element('#loupe-heading').textContent, 'photo-2.jpg');
    assert.equal(viewport.children[0].src, '/preview/2');
    assert.equal(viewport.getAttribute('aria-label'), 'photo-2.jpg viewport');
    assert.equal(announced(), 'photo-2.jpg, 2 of 3');
    assert.equal(state.focusedId, 2);
    assert.equal(card(2).getAttribute('tabindex'), '0');
    await key('ArrowRight', viewport, loupe);
    assert.equal(element('#loupe-heading').textContent, 'photo-3.jpg', 'arrows step the frame view like brackets');
    await key('ArrowLeft', viewport, loupe);
    assert.equal(element('#loupe-heading').textContent, 'photo-2.jpg');
    await loupe.listeners.keydown({ key: 'ArrowRight', defaultPrevented: true, metaKey: false, ctrlKey: false, altKey: false, target: viewport, preventDefault() {}, stopPropagation() {} });
    assert.equal(element('#loupe-heading').textContent, 'photo-2.jpg', 'an arrow a tab list already used never steps');
    let stopped = 0;
    const viewportKey = () => viewport.listeners.keydown({ key: 'ArrowRight', metaKey: false, ctrlKey: false, altKey: false, target: viewport, preventDefault() {}, stopPropagation() { stopped += 1; } });
    viewportKey();
    assert.equal(stopped, 0, 'an image that fits lets the arrow reach the frame view');
    Object.assign(viewport, { scrollWidth: 2000, clientWidth: 500, scrollLeft: 0 });
    viewportKey();
    assert.deepEqual([stopped, viewport.scrollLeft], [1, 40], 'a full-size image still pans with the arrows');
    Object.assign(viewport, { scrollWidth: 0, clientWidth: 0 });
    await key('x', viewport, loupe);
    assert.deepEqual(decisionPosts.at(-1), { url: '/api/photos/2/decision', body: { decision: 'reject' } }, 'culling keys act on the frame shown');
    await key('Escape', viewport, loupe);
    assert.equal(loupe.hidden, true);
    assert.equal(focused, card(2), 'closing returns focus to the frame last shown');
    const compare = Object.assign(element('#compare'), { tagName: 'DIALOG', id: 'compare' });
    await key('c', card(2), sheetNode);
    await key('ArrowRight', card(2), sheetNode);
    await key('c', card(3), sheetNode);
    await element('#open-compare').listeners.click();
    assert.equal(compare.hidden, false);
    const before = prevented;
    await key(']', viewport, compare);
    await key('[', viewport, compare);
    assert.equal(state.focusedId, 3, 'brackets do nothing in compare');
    assert.equal(prevented, before);
    await key(']', card(3), sheetNode);
    assert.equal(prevented, before, 'brackets do nothing on the sheet');
    return;
  }
  if (scenario === 'expand-search') {
    await load();
    const button = element('#expand-search');
    assert.equal(button.disabled, true, 'blank search fields leave nothing to expand');
    const text = element('#filter-text');
    text.value = 'seabirds';
    text.listeners.input();
    assert.equal(button.disabled, false);
    await button.listeners.click();
    await new Promise(setImmediate);
    assert.deepEqual(expandPosts, [{ query: 'seabirds' }]);
    assert.deepEqual([text.value, element('#find').value], ['seabirds | tern | gull', 'seabirds | tern | gull']);
    assert.equal(new URLSearchParams(location.search).get('text'), 'seabirds | tern | gull');
    assert.equal(lastGroupsUrl, '/api/groups?' + new URLSearchParams({ text: 'seabirds | tern | gull' }));
    assert.equal(element('#announcer').textContent, 'Expanded to 3 terms');
    assert.equal(element('#ai-status').textContent, 'Search expansion finished');
    assert.equal(button.disabled, false);
    state.status.omlx_roles.fast.loaded = false;
    updateWorkflow();
    assert.equal(button.disabled, true);
    assert.ok(element('#ai-availability').textContent.startsWith('Fast model Qwen3-VL-8B-Instruct-4bit is not loaded in oMLX.'));
    return;
  }
  if (scenario === 'find') {
    const tree = node => [node, ...(node.children || []).flatMap(tree)];
    const reasons = () => tree(element('#inspection')).find(node => node.id === 'match-reasons');
    await load();
    await inspect(state.groups[0].photos[0]);
    assert.equal(reasons(), undefined, 'no match line without a find');
    groups[0].photos[0].matched = ['keyword: ice'];
    const find = element('#find');
    find.tagName = 'INPUT';
    find.value = 'ice';
    const flush = () => new Promise(setImmediate);
    const type = async (control, value) => { control.value = value; control.listeners.input(); await flush(); };
    const fire = async () => { const due = [...timeouts.values()]; timeouts.clear(); due.forEach(callback => callback()); await flush(); };  // never await: an apply may wait on a deferred reply
    await type(find, 'ice');
    assert.equal(lastGroupsUrl, '/api/groups', 'find is debounced');
    assert.equal(element('#filter-text').value, 'ice', 'the form mirrors find at once');
    await fire();
    assert.equal(lastGroupsUrl, '/api/groups?text=ice');
    assert.equal(location.search, '?text=ice');
    assert.equal(find.value, 'ice', 'syncFilters keeps the rail find in step with the URL');
    await inspect(state.groups[0].photos[0]);
    assert.equal(reasons().textContent, 'Matches: keyword: ice');
    const posts = decisionPosts.length;
    await element('#contact-sheet').listeners.keydown({ key: 'x', metaKey: false, ctrlKey: false, altKey: false, target: find, preventDefault() {} });
    assert.equal(decisionPosts.length, posts, 'typing in find never culls');
    await type(element('#filter-text'), 'krill');
    assert.equal(find.value, 'krill');
    await fire();
    assert.equal(lastGroupsUrl, '/api/groups?text=krill');

    // Responses land while the photographer keeps typing.
    deferGroups = true;
    find.focus();
    await type(find, 'ab');
    await fire();
    assert.equal(pendingGroups.length, 1);
    await type(find, 'abc');  // its apply is still pending when the "ab" reply lands
    pendingGroups.shift()();
    await flush();
    assert.deepEqual([find.value, element('#filter-text').value], ['abc', 'abc'], 'a reply never rewrites either field while an apply is pending');
    await fire();
    assert.equal(lastGroupsUrl, '/api/groups?text=abc', 'the later keystroke reaches the query');
    pendingGroups.shift()();
    await flush();
    assert.deepEqual([find.value, element('#filter-text').value], ['abc', 'abc']);
    await type(find, 'motion ');
    await fire();
    pendingGroups.shift()();
    await flush();
    assert.deepEqual([find.value, element('#filter-text').value], ['motion ', 'motion '], 'a trailing space survives the reload');

    // Reset drops a pending find apply.
    await type(find, 'zodiac');
    element('#reset-filters').focus();
    const loads = groupLoads;
    element('#reset-filters').listeners.click();
    await flush();
    pendingGroups.shift()();
    await flush();
    await fire();
    assert.equal(groupLoads, loads + 1, 'only the reset fetches');
    assert.equal(lastGroupsUrl, '/api/groups');
    assert.deepEqual([find.value, element('#filter-text').value], ['', '']);
    deferGroups = false;
    return;
  }
  if (scenario === 'filter-live') {
    await load();
    const loads = groupLoads;
    element('#filter-rating').value = '3';
    await element('#filter-rating').listeners.change();
    assert.equal(groupLoads, loads + 1, 'a filter applies as soon as it changes');
    assert.equal(lastGroupsUrl, '/api/groups?min_rating=3');
    assert.equal(element('#photo-count').textContent, '2 of 2 frames', 'a narrowed sheet counts against the library');
    await pickStatus('keepers').listeners.change();
    assert.equal(lastGroupsUrl, '/api/groups?min_rating=3&keepers_only=1');
    for (const control of ['#filter-from', '#filter-to', '#filter-camera', '#filter-tagged', '#status-all', '#status-flag', '#status-none', '#status-reject']) {
      assert.equal(typeof element(control).listeners.change, 'function', control + ' applies on change');
    }
    location.search = '?decision=flag&camera=ILCE-7M4%3A1';
    await load();
    assert.deepEqual(['all', 'keepers', 'flag', 'none', 'reject'].map(value => element('#status-' + value).checked), [false, false, true, false, false], 'a reload restores the status from the URL');
    assert.equal(filterQuery(), 'camera=ILCE-7M4%3A1&decision=flag');
    await element('#reset-filters').listeners.click();
    assert.equal(lastGroupsUrl, '/api/groups');
    assert.deepEqual([element('#filter-rating').value, element('#status-all').checked, element('#status-flag').checked], ['0', true, false]);
    assert.equal(element('#photo-count').textContent, '2 frames');
    return;
  }
  if (scenario === 'filters') {
    const tree = node => [node, ...(node.children || []).flatMap(tree)];
    const range = (start, count) => Array.from({ length: count }, (_, index) => start + index);
    const burst = (id, day, ids) => ({ ...group(id, ids), started_at: day && day + 'T08:00:00+00:00' });
    groups = [burst(1, '2026-10-21', range(1, 150)), burst(2, '2026-10-21', range(151, 100)), burst(3, '2026-10-22', range(251, 140)), burst(4, null, range(391, 10))];
    groups[0].photos[1].decision = 'flag';
    groups[2].photos[0].decision = 'reject';
    groups[2].rejected_count = 1;
    await load();
    assert.equal(lastGroupsUrl, '/api/groups');
    const days = () => element('#batch-list').children;
    assert.deepEqual(days().map(day => [day.tagName, day.open, day.children[0].tagName]), [['DETAILS', true, 'SUMMARY'], ['DETAILS', false, 'SUMMARY'], ['DETAILS', false, 'SUMMARY']], 'without a selection only the first day is expanded');
    assert.deepEqual(days().map(day => day.children[0].textContent), ['2026-10-21 · 250 frames · 3 keepers', '2026-10-22 · 140 frames · 0 keepers', 'Undated · 10 frames · 1 keeper']);
    assert.deepEqual(days().map(day => day.children.slice(1).map(item => item.className)), [['batch-row', 'batch-row'], ['batch-row'], ['batch-row']]);
    const cards = () => document.querySelectorAll('.frame');
    const more = element('#show-more');
    assert.equal(cards().length, 200);
    assert.ok(cards().every(card => card.children[0].loading === 'lazy'));
    assert.equal(more.hidden, false);
    assert.equal(more.textContent, 'Show 200 more · 200 remaining');
    assert.deepEqual(element('#filter-camera').children.map(option => option.value), ['', 'ILCE-7M4:1', 'ILCE-7RV:2']);
    assert.equal(element('#keeper-count').textContent, '4 keepers');
    keeperOverride = 4;

    element('#filter-from').value = '2026-10-21';
    element('#filter-to').value = '2026-10-22';
    element('#filter-camera').value = 'ILCE-7M4:1';
    pickStatus('flag');
    element('#filter-rating').value = '2';
    element('#filter-tagged').checked = true;
    element('#filter-export-failed').checked = true;
    element('#filter-text').value = ' red fox ';
    groups = groups.slice(0, 3);
    await element('#filter-form').listeners.submit({ preventDefault() {} });
    const query = 'date_from=2026-10-21&date_to=2026-10-22&camera=ILCE-7M4%3A1&min_rating=2&tagged_only=1&export_failed_only=1&text=red+fox&decision=flag';
    assert.equal(location.search, '?' + query);
    assert.equal(lastGroupsUrl, '/api/groups?' + query);
    assert.equal(element('#announcer').textContent, 'Showing 200 of 390 frames');
    assert.equal(element('#keeper-count').textContent, '4 keepers', 'export counts library-wide keepers, not the filtered view');
    assert.equal(more.textContent, 'Show 190 more · 190 remaining');
    assert.equal(cards().length, 200);
    assert.deepEqual(cards().map(card => card.getAttribute('tabindex')).filter(value => value === '0'), ['0'], 'one frame in the tab order');
    await more.listeners.click();
    assert.equal(cards().length, 390);
    assert.equal(more.hidden, true);
    assert.equal(element('#announcer').textContent, 'Showing 390 of 390 frames');
    assert.equal(focused, cards()[200], 'focus moves to the first revealed frame');
    assert.equal(state.focusedId, cards()[200].dataset.id);
    assert.deepEqual(cards().map(card => card.getAttribute('tabindex')).filter(value => value === '0'), ['0']);
    const loadsBefore = groupLoads;
    await document.querySelectorAll('.batch-button')[0].listeners.click();
    assert.equal(cards().length, 390, 'choosing a burst keeps the revealed pages');

    for (const name of ['#filter-from', '#filter-to', '#filter-camera', '#filter-rating', '#filter-text']) element(name).value = '';
    pickStatus('all');
    element('#filter-tagged').checked = false;
    await load();
    assert.equal(groupLoads, loadsBefore + 1);
    assert.equal(lastGroupsUrl, '/api/groups?' + query, 'a reload keeps the filters from the URL');
    assert.deepEqual(['#filter-from', '#filter-to', '#filter-camera', '#filter-rating', '#filter-text'].map(name => element(name).value),
      ['2026-10-21', '2026-10-22', 'ILCE-7M4:1', '2', 'red fox']);
    assert.deepEqual([element('#status-flag').checked, element('#status-keepers').checked, element('#filter-tagged').checked], [true, false, true]);

    element('#filter-to').value = '2026-10-01';
    groupsError = 'invalid filter: date_to';
    await element('#filter-form').listeners.submit({ preventDefault() {} });
    assert.equal(element('#announcer').textContent, 'invalid filter: date_to');
    assert.ok(lastGroupsUrl.includes('date_to=2026-10-01'));
    assert.equal(location.search, '?' + query, 'a rejected filter is not left in the URL');
    assert.equal(element('#filter-to').value, '2026-10-01', 'the form keeps what was typed');
    await load();
    assert.equal(lastGroupsUrl, '/api/groups?' + query, 'later loads still work');
    keeperOverride = null;

    groups = [burst(9, '2026-10-21', range(1, 3))];
    await element('#reset-filters').listeners.click();
    assert.equal(location.search, '');
    assert.equal(lastGroupsUrl, '/api/groups');
    assert.deepEqual(['#filter-from', '#filter-camera', '#filter-rating', '#filter-text'].map(name => element(name).value), ['', '', '0', '']);
    assert.deepEqual([element('#status-all').checked, element('#status-flag').checked], [true, false]);
    assert.equal(more.hidden, true);
    assert.equal(element('#announcer').textContent, 'Showing 3 of 3 frames');
    groups = [];
    element('#filter-text').value = 'zebra';
    await element('#filter-form').listeners.submit({ preventDefault() {} });
    assert.ok(tree(element('#contact-sheet')).some(node => node.textContent === 'No frames match these filters.'));
    return;
  }
  if (scenario === 'refine') {
    const tree = node => [node, ...(node.children || []).flatMap(tree)];
    const card = id => document.querySelectorAll('.frame').find(item => Number(item.dataset.id) === id);
    const scopeSelect = element('#refine-scope');
    scopeSelect.value = 'group';
    darktableReady = false;
    await load();
    assert.equal(element('#refine-previews').disabled, true);
    assert.equal(element('#refine-availability').textContent, 'darktable-cli is unavailable. Install darktable or add it to PATH, restart Lumen, then reload this page.');
    darktableReady = true;
    await load();
    assert.equal(element('#refine-availability').textContent, '');
    assert.equal(element('#refine-previews').disabled, true, 'the selected-burst scope needs a selected burst');
    assert.ok(!tree(card(1)).some(node => node.textContent === 'Refined'));
    await inspect(state.groups[0].photos[1]);
    assert.equal(element('#refine-previews').disabled, false);
    assert.deepEqual(refinePayload(), { scope: 'group', group_id: 1 });
    scopeSelect.value = 'all';
    assert.deepEqual(refinePayload(), { scope: 'all' });
    scopeSelect.value = 'filtered';
    history.replaceState(null, '', '?min_rating=2&camera=ILCE-7M4%3A1');
    await element('#refine-previews').listeners.click();
    assert.deepEqual(refinePosts, [{ scope: 'filtered', min_rating: '2', camera: 'ILCE-7M4:1' }]);
    assert.equal(state.job.kind, 'refine_previews');
    assert.equal(element('#refine-previews').disabled, true, 'one heavy job at a time');
    assert.equal(element('#job-strip').hidden, false);
    assert.ok(element('#announcer').textContent.startsWith('Refine previews'));
    await settle();
    assert.equal(state.job.status, 'succeeded');
    assert.equal(element('#job-status').textContent, 'Refine previews finished. 2 refined, 0 skipped, 0 failed.');
    assert.ok(tree(card(1)).some(node => node.textContent === 'Refined' && node.className === 'refined'));
    assert.ok(card(2).getAttribute('aria-label').includes('refined'));
    assert.ok(card(1).children[0].src.endsWith('-refined'));
    darktableReady = false;  // the server's own check still answers when the page's status is stale
    refineError = 'darktable-cli is unavailable';
    scopeSelect.value = 'all';
    await element('#refine-previews').listeners.click();
    assert.equal(element('#refine-status').textContent, 'Refine previews could not start: darktable-cli is unavailable.');
    assert.equal(element('#announcer').textContent, element('#refine-status').textContent);
    return;
  }
  if (scenario === 'all-rejected') {
    const tree = node => [node, ...(node.children || []).flatMap(tree)];
    const rejected = id => ({ ...photo(id), decision: 'reject' });
    const NO_KEEPER = 'No keeper in this burst: every frame is rejected.';
    groups = [{ id: 1, automatic_pick_id: 1, frame_count: 2, rejected_count: 2, photos: [] }, group(2, [3])];
    await load();
    const batches = () => document.querySelectorAll('.batch-button');
    assert.equal(batches().length, 2, 'an all-rejected burst keeps its rail entry while hidden');
    assert.equal(batches()[0].textContent, 'Burst 1 · 2 frames · ' + NO_KEEPER);
    assert.equal(element('#keeper-count').textContent, '1 keeper');
    assert.equal(element('#workspace-cleanup-count').textContent, '2 batches · 3 frames');
    await batches()[0].listeners.click();
    assert.equal(state.selectedGroupId, 1);
    assert.equal(element('#group-cleanup-count').textContent, 'Burst 1 · 2 frames', 'removal counts the hidden rejected frames');
    assert.equal(element('#remove-group').disabled, false);
    assert.equal(element('#inspection').textContent, NO_KEEPER);
    groups = [{ id: 1, automatic_pick_id: 1, frame_count: 2, rejected_count: 2, photos: [rejected(1), rejected(2)] }, group(2, [3])];
    await pickStatus('reject').listeners.change();
    const card = id => document.querySelectorAll('.frame').find(item => Number(item.dataset.id) === id);
    const texts = id => tree(card(id)).map(node => node.textContent);
    assert.ok(texts(1).includes('Rejected'));
    assert.ok(!texts(1).includes('Best'), 'a rejected pick never shows the Best badge');
    assert.ok(!card(1).getAttribute('aria-label').includes('best in group'));
    await inspect(state.groups[0].photos[0]);
    assert.ok(tree(element('#inspection')).some(node => node.textContent === NO_KEEPER));
    const choose = tree(element('#inspection')).find(node => node.textContent === 'Choose as best');
    assert.ok(choose, 'a rejected best frame can still be chosen as best');
    groups = [{ id: 1, automatic_pick_id: 1, frame_count: 2, rejected_count: 1, photos: [photo(1), rejected(2)] }, group(2, [3])];
    await choose.listeners.click();
    assert.deepEqual(picks, ['/api/photos/1/pick']);
    assert.ok(texts(1).includes('Best') && !texts(1).includes('Rejected'));
    assert.equal(batches()[0].textContent, 'Burst 1 · 2 frames');
    assert.equal(element('#keeper-count').textContent, '2 keepers');
    return;
  }
  if (scenario === 'ingest') {
    await load();
    element('#add-tab').listeners.click();
    element('#add-source-ingest').checked = true; element('#add-source-import').checked = false;
    element('#add-source-ingest').listeners.change();
    assert.equal(element('#ingest-panel').hidden, false, 'the ingest radio shows its section');
    assert.equal(element('#import-panel').hidden, true);
    assert.equal(stored['lumen-add-source'], 'ingest', 'the source is remembered');
    assert.equal(element('#start-ingest').disabled, true, 'a card and a primary destination are required');
    await element('#browse-ingest-card').listeners.click();
    assert.equal(element('#ingest-card-path').textContent, '/Volumes/CARD');
    assert.equal(element('#start-ingest').disabled, true);
    await element('#browse-ingest-primary').listeners.click();
    assert.equal(element('#start-ingest').disabled, false);
    await element('#browse-ingest-mirror').listeners.click();
    assert.equal(element('#ingest-mirror-path').textContent, '/Volumes/Mirror');
    await element('#start-ingest').listeners.click();
    assert.deepEqual(ingestPosts, [{ card: '/Volumes/CARD', primary: '/Volumes/Primary', mirror: '/Volumes/Mirror' }]);
    assert.ok(element('#announcer').textContent.includes('Ingest'), 'the start is announced');
    assert.equal(element('#start-ingest').disabled, true, 'one ingest at a time');
    assert.equal(element('#browse-ingest-card').disabled, true);
    assert.equal(activeTimers, 1);
    assert.equal(element('#ingest-verdict').textContent, '', 'no verdict while the copy runs');
    await tick();
    assert.ok(element('#ingest-counts').textContent.includes('1 copied'));
    await settle();
    assert.equal(activeTimers, 0);
    assert.equal(state.job.status, 'succeeded');
    assert.ok(element('#ingest-counts').textContent.includes('3 copied'));
    assert.ok(element('#ingest-counts').textContent.includes('1 already present'));
    assert.ok(element('#ingest-counts').textContent.includes('4 of 4 files verified'));
    assert.ok(element('#ingest-counts').textContent.includes('2 hidden entries skipped'));
    assert.equal(element('#ingest-manifest').textContent, '/managed/outputs/ingest/11/manifest.json');
    assert.ok(element('#ingest-snapshot').textContent.includes('20261022T120000Z.sqlite3'));
    assert.equal(element('#ingest-verdict').textContent, 'Every file verified on every destination.');
    assert.ok(element('#announcer').textContent.includes('Every file verified on every destination.'));
    assert.equal(element('#start-ingest').disabled, false);
    ingestPolls = 0;
    ingestOutcome = { copied: 2, verified: 2, failed: 1, unreadable: 1, card_verified_everywhere: false };
    await element('#reset-ingest-mirror').listeners.click();
    assert.equal(element('#ingest-mirror-path').textContent, 'No mirror');
    await element('#start-ingest').listeners.click();
    assert.deepEqual(ingestPosts[1], { card: '/Volumes/CARD', primary: '/Volumes/Primary', mirror: null });
    await settle();
    assert.equal(element('#ingest-verdict').textContent, 'Do not format the card: 3 files were not verified.');
    assert.ok(element('#ingest-counts').textContent.includes('1 entry on the card could not be read'));
    assert.ok(element('#announcer').textContent.includes('Do not format the card'));
    return;
  }
  const pathsText = () => element('#astro-paths').children.map(child => child.textContent).join(' ');
  if (scenario === 'astro-triage') {
    await load();
    const tree = node => [node, ...(node.children || []).flatMap(tree)];
    const cue = tree(element('#contact-sheet')).find(node => node.className === 'astro-cue');
    assert.equal(cue?.textContent, 'Looks like astro', 'the cue is text in the burst header');
    assert.equal(element('#add-tab').getAttribute('aria-selected'), 'true', 'the cue never switches the tab');
    await inspect(state.groups[0].photos[0]);
    await new Promise(setImmediate);
    assert.equal(element('#add-tab').getAttribute('aria-selected'), 'true', 'selecting an astro-looking burst keeps the tab');
    assert.deepEqual(triageRequests, ['/api/groups/1/triage']);
    const lights = document.querySelectorAll('#astro-lights input');
    const triageTexts = element('#astro-lights').children.map(label => label.children[2].textContent);
    assert.deepEqual(triageTexts, ['night sky 91%', 'cloud 91% · cloud band · flagged', 'night sky 91%']);
    assert.equal(element('#exclude-flagged').textContent, 'Exclude flagged lights (1)');
    assert.equal(element('#exclude-flagged').disabled, false);
    assert.equal(element('#triage-astro').disabled, false, 'fast model ready');
    element('#exclude-flagged').listeners.click();
    assert.equal(element('#exclude-confirm').hidden, false);
    assert.ok(element('#exclude-confirm-text').textContent.startsWith('Uncheck 1 flagged light? Flagged means cloud, not sky, trailing, or a cloud band.'));
    assert.ok(lights.every(input => input.checked), 'opening the confirmation changes nothing');
    element('#exclude-confirm').hidden = true;  // Cancel: the form closes the dialog; no listener runs
    assert.ok(lights.every(input => input.checked), 'cancel leaves every light checked');
    element('#exclude-flagged').listeners.click();
    element('#exclude-confirm-yes').listeners.click();
    assert.deepEqual(lights.map(input => input.checked), [true, false, true]);
    assert.deepEqual(astroLights().excluded, [2]);
    assert.equal(element('#announcer').textContent, '1 light excluded');
    assert.equal(element('#exclude-flagged').textContent, 'Exclude flagged lights (0)');
    assert.equal(element('#add-tab').getAttribute('aria-selected'), 'true');
    return;
  }
  if (scenario === 'resume') {
    assert.equal(state.selectedId, 2, 'a reload re-selects the last inspected frame');
    assert.ok(state.shown >= 250, 'the page grows to include the remembered frame');
    const card = document.querySelectorAll('.frame').find(item => Number(item.dataset.id) === 2);
    assert.equal(card.getAttribute('aria-pressed'), 'true');
    assert.equal(card.getAttribute('tabindex'), '0');
    assert.deepEqual(photoDetailRequests, ['/api/photos/2']);
    state.selectedId = null;  // as selectGroup does; a later load (filter, job, cleanup) must not resume again
    await load();
    assert.equal(state.selectedId, null, 'only the first load of a page resumes');
    assert.deepEqual(photoDetailRequests, ['/api/photos/2']);
    await inspect(state.groups[1].photos[1]);
    assert.equal(stored['lumen-selected'], '3', 'inspecting remembers the frame');
    return;
  }
  if (scenario === 'resume-gone') {
    assert.equal(state.selectedId, null);
    assert.equal(stored['lumen-selected'], undefined, 'a frame that is gone is forgotten');
    assert.deepEqual(photoDetailRequests, []);
    return;
  }
  if (scenario === 'astro-resume') {
    await load();
    assert.equal(state.astro.job.status, 'running', 'a remembered job is refetched on load');
    assert.equal(activeTimers, 1, 'a remembered running job resumes polling');
    await settle();
    assert.equal(activeTimers, 0);
    assert.equal(state.astro.job.status, 'succeeded');
    return;
  }
  if (scenario.startsWith('astro')) {
    await load();
    assert.equal(element('#astro-group-count').textContent, 'No batch selected');
    await inspect(state.groups[0].photos[0]);
    element('#astro-tab').listeners.click();
    assert.equal(element('#astro-group-count').textContent, 'Burst 1 · 3 frames');
    const lights = document.querySelectorAll('#astro-lights input');
    assert.equal(lights.length, 3);
    assert.ok(lights.every(input => input.checked), 'every light starts included');
    assert.equal(element('#prepare-astro').disabled, true, 'prepare requires explicit confirmation');
    const confirm = element('#astro-confirm');
    confirm.checked = true;
    confirm.listeners.change();
    assert.equal(element('#prepare-astro').disabled, false);
    lights[1].checked = false; lights[1].listeners.change();
    lights[2].checked = false; lights[2].listeners.change();
    assert.equal(element('#prepare-astro').disabled, true, 'fewer than two lights cannot be stacked');
    lights[1].checked = true; lights[1].listeners.change();
    await element('#browse-astro-darks').listeners.click();
    assert.equal(element('#astro-darks-path').textContent, '/cal/darks');
    const recipe = scenario === 'astro-recipe' ? 'untracked' : 'tracked';
    recipes.forEach(input => { input.checked = input.value === recipe; });
    await element('#prepare-astro').listeners.click();
    assert.deepEqual(astroPosts, [{ group_id: 1, confirmed: true, excluded_photo_ids: [3], recipe, darks: '/cal/darks', flats: null, biases: null }]);
    assert.ok(element('#astro-summary').textContent.endsWith('Recipe: ' + recipe));
    assert.ok(recipes.every(input => input.disabled), 'a prepared job locks the recipe');
    assert.ok(element('#astro-warnings').children[0].textContent.startsWith('Warning: No flat frames'));
    assert.ok(element('#astro-summary').textContent.includes('2 lights'));
    assert.ok(pathsText().includes('/managed/outputs/astro/7/manifest.json'));
    assert.equal(element('#run-astro').disabled, false);
    assert.equal(element('#prepare-astro').disabled, true, 'one managed job at a time');
    assert.equal(element('#cancel-astro').disabled, false);
    assert.equal(lights[0].disabled, true, 'a prepared job locks the light selection');
    assert.equal(confirm.disabled, true);
    assert.equal(element('#browse-astro-darks').disabled, true);
    assert.ok(element('#astro-status').textContent.includes('Cancel this stack to change'));
    await element('#run-astro').listeners.click();
    assert.equal(runs, 1);
    assert.equal(state.astro.job.status, 'queued', 'the server answers run with the still-queued row');
    assert.equal(activeTimers, 1, 'polling starts as soon as run is accepted');
    assert.equal(element('#run-astro').disabled, true, 'run cannot be submitted twice');
    await tick();
    assert.equal(state.astro.job.status, 'running');
    assert.ok(element('#astro-progress').value >= 10);
    assert.ok(element('#announcer').textContent.includes('running'));
    if (scenario === 'astro-cancel') {
      await tick();
      assert.equal(activeTimers, 1, 'a running job keeps polling');
      await element('#cancel-astro').listeners.click();
      assert.equal(cancels, 1);
      assert.equal(activeTimers, 0, 'cancellation stops polling');
      assert.equal(state.astro.job.status, 'cancelled');
      assert.ok(element('#announcer').textContent.includes('cancelled'));
      assert.ok(pathsText().includes('siril.log'), 'logs stay reachable after cancel');
    } else {
      await settle();
      assert.equal(activeTimers, 0, 'polling stops on a terminal state');
      assert.ok(polls >= 3, 'the job was polled while running');
      assert.equal(state.astro.job.status, scenario === 'astro-fail' ? 'failed' : 'succeeded');
      statusWrites.forEach((text, index) => assert.notEqual(text, statusWrites[index - 1], 'the live region must not be rewritten with identical text'));
      assert.ok(pathsText().includes('/managed/outputs/astro/7/manifest.json'));
      assert.ok(pathsText().includes('/managed/outputs/astro/7/siril.log'));
      assert.ok(element('#astro-log').textContent.includes('Running command: register'));
      if (scenario === 'astro-fail') {
        assert.ok(element('#astro-status').textContent.includes('exit code 3'));
        assert.ok(element('#announcer').textContent.includes('failed'));
        assert.equal(element('#reveal-astro').disabled, false, 'failed jobs keep their folder reachable');
      } else {
        assert.ok(pathsText().includes('/managed/outputs/astro/7/result.tif'));
        assert.ok(element('#announcer').textContent.includes('finished'));
        assert.equal(element('#astro-progress').value, 100);
        await element('#reveal-astro').listeners.click();
        assert.ok(element('#astro-status').textContent.includes('/managed/outputs/astro/7'));
      }
    }
    assert.equal(element('#cancel-astro').disabled, true);
    assert.equal(element('#run-astro').disabled, true);
    assert.equal(element('#prepare-astro').disabled, false, 'a finished job frees the form');
    assert.equal(lights[0].disabled, false);
    assert.equal(confirm.disabled, false);
    return;
  }
  if (scenario === 'export-bad-preset') {
    assert.equal(document.querySelector('input[name="export-preset"]:checked').value, 'full', 'an unknown remembered preset is ignored');
    return;
  }
  if (scenario === 'export-watermark') {
    const watermark = element('#export-watermark');
    assert.equal(watermark.value, '\u00a9 N. David', 'a reload restores the remembered watermark');
    await load();
    scope.value = 'keepers';
    assert.equal(webRadio.checked, true, 'a reload restores the remembered preset');
    assert.equal(document.querySelector('input[name="export-preset"]:checked'), webRadio);
    presetRadios[0].checked = true; webRadio.checked = false;
    presetRadios[0].listeners.change();
    assert.equal(stored['lumen-export-preset'], 'full', 'choosing a preset remembers it');
    presetRadios[0].checked = false; webRadio.checked = true;
    webRadio.listeners.change();
    assert.equal(stored['lumen-export-preset'], 'web');
    state.keeperCount = 2;
    updateWorkflow();
    element('#export-dialog').hidden = true;
    watermark.value = '  \u00a9 2026 N. David ';
    watermark.listeners.input();
    assert.equal(stored['lumen-watermark'], '  \u00a9 2026 N. David ', 'typing remembers the field');
    await element('#export-jpegs').listeners.click();
    await settle();
    watermark.value = '   ';
    watermark.listeners.change();
    assert.equal('lumen-watermark' in stored, false, 'a blank field forgets the watermark');
    await element('#export-jpegs').listeners.click();
    await settle();
    assert.deepEqual(exportPosts, [{ scope: 'keepers', profile: null, preset: 'web', watermark: '\u00a9 2026 N. David' },
                                   { scope: 'keepers', profile: null, preset: 'web' }], 'the watermark is sent only when non-blank');
    return;
  }
  if (scenario === 'export-filtered') {
    location.search = '?min_rating=2&decision=flag';
    await load();
    scope.value = 'filtered';
    updateWorkflow();
    const shown = document.querySelectorAll('.frame').length;
    assert.equal(element('#filtered-export-count').textContent, shown + ' frame' + (shown === 1 ? '' : 's'));
    assert.equal(element('#export-jpegs').disabled, false);
    await element('#export-jpegs').listeners.click();
    assert.deepEqual(exportPosts, [{ scope: 'filtered', profile: null, preset: 'full', min_rating: '2', decision: 'flag' }], 'the export carries the contact sheet filters');
    assert.ok(element('#export-status').textContent.startsWith('Exporting the filtered frames at Full resolution.'));
    location.search = '';
    await load();
    assert.ok(element('#filtered-export-count').textContent.endsWith(', no filter on'));
    return;
  }
  if (scenario === 'export' || scenario === 'export-cancel') {
    await load();
    scope.value = 'keepers';
    assert.equal(document.querySelector('input[name="export-preset"]:checked').value, 'full', 'Full resolution is the default');
    state.keeperCount = 2;
    updateWorkflow();
    const dialog = element('#export-dialog');
    dialog.hidden = true;
    await element('#export-jpegs').listeners.click();
    assert.deepEqual(exportPosts, [{ scope: 'keepers', profile: null, preset: 'full' }]);
    assert.equal(state.job.kind, 'export');
    assert.equal(state.busy, true, 'an export job holds structural actions');
    assert.equal(dialog.hidden, false, 'the progress dialog opens when the job is accepted');
    assert.equal(element('#export-dialog-cancel').hidden, false);
    assert.equal(element('#export-dialog-close').hidden, true);
    assert.equal(element('#ai-status').dataset.phase, 'running');
    assert.ok(element('#export-status').textContent.includes('status bar'));
    await tick();
    assert.equal(element('#export-dialog-label').textContent, '1 of 2 · photo-1.jpg');
    assert.equal(element('#export-dialog-progress').value, 50);
    assert.equal(element('#export-dialog-status').textContent, 'Export running.');
    assert.equal(element('#ai-status').textContent, 'Export running. 1 of 2 · photo-1.jpg', 'the bottom status bar carries the same progress');
    assert.ok(element('#job-label').textContent.includes('Export 17 · 1 of 2'));
    if (scenario === 'export-cancel') {
      await element('#export-dialog-cancel').listeners.click();
      assert.equal(exportCancels, 1);
      assert.equal(state.job.status, 'cancelled');
      assert.equal(activeTimers, 0);
      assert.equal(element('#export-dialog-status').textContent, 'Export cancelled. Finished items are kept.');
      assert.equal(element('#ai-status').dataset.phase, 'failed');
      assert.equal(element('#export-dialog-cancel').hidden, true);
      assert.equal(element('#export-dialog-close').hidden, false);
      return;
    }
    await settle();
    assert.equal(state.job.status, 'succeeded');
    assert.equal(state.busy, false);
    assert.equal(element('#export-dialog-status').textContent, 'Export finished. 1 saved, 1 failed. Tick Export failed in the filters to see them.');
    assert.equal(element('#export-status').textContent, 'Export finished. 1 saved, 1 failed. Tick Export failed in the filters to see them.');
    assert.deepEqual(element('#export-results').children.map(item => item.textContent), ['photo-2.jpg: bad render'], 'failures are listed by basename');
    assert.equal(element('#ai-status').dataset.phase, 'done');
    assert.equal(element('#ai-status').textContent, 'Export finished. 1 saved, 1 failed. Tick Export failed in the filters to see them.');
    assert.equal(element('#export-dialog-cancel').hidden, true);
    assert.equal(element('#export-dialog-close').hidden, false);
    assert.equal(dialog.hidden, false, 'the outcome stays visible until closed');
    element('#export-dialog-close').listeners.click();
    assert.equal(dialog.hidden, true);
    assert.equal(element('#export-jpegs').disabled, false);
    return;
  }
  if (scenario === 'rank-explain') {
    groups[0].photos[0].sharpness = 0.62; groups[0].photos[0].exposure = 0.9; groups[0].photos[0].total_score = 0.69;
    groups[0].photos[1].sharpness = 0.4; groups[0].photos[1].exposure = 0.95; groups[0].photos[1].total_score = 0.54;
    await load();
    const tree = node => [node, ...(node.children || []).flatMap(tree)];
    const ranking = () => tree(element('#inspection')).find(node => node.className === 'ranking')?.textContent;
    await inspect(state.groups[0].photos[0]);
    assert.equal(ranking(), 'Ranked 1 of 2 in this burst: sharpness 62% (highest), exposure 90%, overall 69%. Overall weighs sharpness 50% and exposure 25%, plus faces and eyes 25% when detected.');
    await inspect(state.groups[0].photos[1]);
    assert.equal(ranking(), 'Ranked 2 of 2 in this burst: sharpness 40%, exposure 95% (highest), overall 54%. Overall weighs sharpness 50% and exposure 25%, plus faces and eyes 25% when detected.');
    return;
  }
  if (scenario === 'raw-warning') {
    await load();
    const tree = node => [node, ...(node.children || []).flatMap(tree)];
    await inspect(state.groups[0].photos[0]);
    assert.ok(!tree(element('#inspection')).some(node => node.className === 'warning'), 'no warning for a supported raw');
    await inspect(state.groups[0].photos[1]);
    const warning = tree(element('#inspection')).find(node => node.className === 'warning');
    assert.equal(warning?.textContent, 'Warning: Sony reduced-size lossless raw; darktable cannot decode it');
    return;
  }
  if (scenario === 'rows') {
    groups = [group(1, [1, 2, 3, 4, 5])];
    await load();
    const sheetNode = element('#contact-sheet');
    const card = id => document.querySelectorAll('.frame').find(item => Number(item.dataset.id) === id);
    const key = name => sheetNode.listeners.keydown({ key: name, metaKey: false, ctrlKey: false, altKey: false, target: card(state.focusedId), preventDefault() {} });
    assert.equal(state.focusedId, 1);
    await key('ArrowDown');
    assert.equal(state.focusedId, 3, 'ArrowDown moves one grid row, read from the burst frame grid, not the sheet');
    await key('PageDown');
    assert.equal(state.focusedId, 5);
    await key('ArrowUp');
    assert.equal(state.focusedId, 3);
    return;
  }
  if (scenario === 'tabs') {
    groups = [group(1, [1, 2])];
    await load();
    scrolled.length = 0;
    element('#review-tab').listeners.click();
    const main = element('#main-content');
    assert.ok(scrolled.some(([node, options]) => node === main && options.top === 0), 'choosing a tab scrolls the main column to its panel');
    assert.equal(element('#review-panel').hidden, false);
    assert.equal(stored['lumen-tab'], 'review-tab', 'the chosen tab is remembered');
    return;
  }
  if (scenario === 'tabs-remembered') {
    await new Promise(setImmediate);  // the startup load
    assert.equal(element('#export-tab').getAttribute('aria-selected'), 'true', 'a remembered tab is selected at startup');
    assert.equal(element('#review-panel').hidden, true);
    assert.notEqual(element('#workflow-panels').hidden, true, 'a remembered tab wins over the folded default');
    assert.deepEqual([element('#ingest-panel').hidden, element('#import-panel').hidden, element('#add-source-ingest').checked], [false, true, true], 'the remembered source is restored');
    return;
  }
  if (scenario === 'default-tab' || scenario === 'default-tab-frames') {
    await new Promise(setImmediate);  // the startup load
    assert.equal(element('#add-tab').getAttribute('aria-selected'), 'true');
    assert.equal(stored['lumen-tab'], undefined, 'the default is not remembered as a choice');
    if (scenario === 'default-tab') {
      assert.equal(element('#workflow-panels').hidden, false, 'an empty catalog opens Add photos');
      assert.equal(element('#add-panel').hidden, false);
    } else {
      assert.equal(element('#workflow-panels').hidden, true, 'a catalog with frames opens folded on the sheet');
    }
    await load();
    element('#workflow-panels').hidden = false;
    await load();
    assert.equal(element('#workflow-panels').hidden, false, 'only the first load chooses');
    return;
  }
  if (scenario === 'keep-only') {
    groups = [group(1, [1, 2, 3])];
    await load();
    const tree = node => [node, ...(node.children || []).flatMap(tree)];
    const card = id => document.querySelectorAll('.frame').find(item => Number(item.dataset.id) === id);
    const texts = id => tree(card(id)).map(node => node.textContent);
    const key = name => element('#contact-sheet').listeners.keydown({ key: name, metaKey: false, ctrlKey: false, altKey: false, target: card(state.focusedId), preventDefault() {} });
    await key('ArrowRight');
    await key('r');
    assert.deepEqual(picks, ['/api/photos/2/pick'], 'R picks the focused frame');
    assert.deepEqual(decisionPosts.map(item => item.url), ['/api/photos/1/decision', '/api/photos/3/decision'], 'R rejects every other frame of the burst');
    assert.ok(decisionPosts.every(item => item.body.decision === 'reject'));
    assert.ok(texts(2).includes('Best') && !texts(2).includes('Rejected'));
    assert.ok(texts(1).includes('Rejected') && texts(3).includes('Rejected'));
    assert.equal(element('#announcer').textContent, 'photo-2.jpg kept as best; 2 other frames rejected');
    assert.equal(focused, card(2), 'focus stays on the kept frame');
    await inspect(state.groups[0].photos[0]);
    const button = tree(element('#inspection')).find(node => node.tagName === 'BUTTON' && node.textContent === 'Keep this, reject the rest');
    assert.ok(button, 'the inspector offers the same action');
    await button.listeners.click();
    assert.deepEqual(picks.at(-1), '/api/photos/1/pick');
    assert.deepEqual(decisionPosts.at(-1), { url: '/api/photos/2/decision', body: { decision: 'reject' } }, 'already rejected frames are not posted again');
    assert.equal(decisionPosts.length, 3);
    assert.ok(!tree(element('#inspection')).some(node => node.tagName === 'BUTTON' && node.textContent === 'Keep this, reject the rest'), 'the inspector re-renders once the burst has nothing left to reject');
    return;
  }
  if (scenario === 'tiles' || scenario === 'tiles-remembered') {
    await load();
    const select = element('#tile-size');
    const sheetNode = element('#contact-sheet');
    if (scenario === 'tiles-remembered') {
      assert.equal(select.value, 'small', 'a remembered size is restored');
      assert.equal(sheetNode.style['--tile-min'], '140px');
      return;
    }
    assert.equal(sheetNode.style['--tile-min'], undefined, 'medium is the stylesheet default');
    select.value = 'large';
    select.listeners.change();
    assert.equal(sheetNode.style['--tile-min'], '260px');
    assert.equal(stored['lumen-tile-size'], 'large');
    assert.equal(element('#announcer').textContent, 'Large tiles');
    return;
  }
  if (scenario === 'rail-days') {
    const burst = (id, day, ids) => ({ ...group(id, ids), started_at: day + 'T08:00:00+00:00' });
    groups = [burst(1, '2026-10-21', [1]), burst(2, '2026-10-22', [2]), burst(3, '2026-10-23', [3])];
    await load();
    const days = () => element('#batch-list').children;
    assert.deepEqual(days().map(day => day.open), [true, false, false]);
    await inspect(state.groups[1].photos[0]);
    await load();
    assert.deepEqual(days().map(day => day.open), [false, true, false], 'the day of the selected burst is expanded');
    days()[2].open = true;
    days()[2].children[0].listeners.click();  // the photographer opens a day by hand
    await load();
    assert.deepEqual(days().map(day => day.open), [false, true, true], 'hand-opened days survive a reload');
    const range = (start, count) => Array.from({ length: count }, (_, index) => start + index);
    groups = [burst(1, '2026-10-21', range(1, 200)), burst(2, '2026-10-22', [201])];
    openDays = null;
    state.selectedId = null; state.selectedGroupId = null;
    await load();
    assert.deepEqual(days().map(day => day.open), [true, false]);
    const tree = node => [node, ...(node.children || []).flatMap(tree)];
    tree(days()[1]).find(node => node.className === 'batch-button').listeners.click();
    await new Promise(setImmediate);
    assert.deepEqual(days().map(day => day.open), [false, true], 'a rail click that reveals a deeper page keeps its own day open');
    assert.equal(state.selectedGroupId, 2);
    return;
  }
  if (scenario === 'status-health') {
    darktableReady = false;
    await load();
    const line = element('#system-status').textContent;
    assert.equal(line, 'Local AI online · darktable-cli missing');
    assert.ok(!line.includes('Qwen'), 'the header names no model');
    return;
  }
  if (scenario === 'stale-server') {
    await load();
    const line = element('#system-status');
    // Ahead of the tool summary: nothing else on this line can be acted on until the server
    // is restarted, and the failure it causes looks like anything but a stale server.
    assert.ok(line.textContent.startsWith('This server is running code older than the files on disk.'), line.textContent);
    assert.ok(line.textContent.endsWith('Local AI offline'), 'the health summary is still there');
    assert.equal(line.getAttribute('data-stale'), '', 'and it is marked so it reads as a warning');
    assert.equal(document.querySelectorAll('.frame').length, 2, 'the sheet still renders');
    return;
  }
  if (scenario === 'status-skew') {
    await load();
    assert.equal(document.querySelectorAll('.frame').length, 2, 'the sheet renders even when the status payload lacks the AI role block');
    assert.ok(element('#system-status').textContent.startsWith('Local AI offline · '), element('#system-status').textContent);
    assert.ok(element('#system-status').textContent.toLowerCase().includes('restart'), 'the status line says the server is older than the page');
    assert.equal(element('#tag-all').disabled, true, 'AI controls stay disabled without role status');
    return;
  }
  if (scenario === 'ai-status') {
    await load();
    const bar = element('#ai-status');
    assert.equal(bar.dataset.phase, undefined, 'idle until the first call');
    const tree = node => [node, ...(node.children || []).flatMap(tree)];
    await inspect(state.groups[0].photos[1]);
    await loadComposition();
    assert.equal(state.composition?.photo_id, 2, 'the frame view loaded crop choices for photo 2');
    reviewMode = 'failure';
    await askCompositionAI();
    assert.equal(bar.dataset.phase, 'failed');
    assert.equal(bar.textContent, 'Composition review failed: local composition review is unavailable for this photo');
    const ask = tree(element('#inspection')).find(node => node.id === 'ask-burst-ai');
    const pending = ask.listeners.click();
    assert.equal(bar.dataset.phase, 'running');
    assert.equal(bar.textContent, 'Burst critique running…');
    await pending;
    assert.equal(bar.dataset.phase, 'done');
    assert.equal(bar.textContent, 'Burst critique finished');
    return;
  }
  if (scenario === 'assist') {
    keeperOverride = 2;
    await load();
    const button = element('#assist-keepers');
    assert.equal(button.textContent, 'Tag, scan and caption keepers (2)');
    assert.equal(button.disabled, false);
    await button.listeners.click();
    assert.deepEqual(assistPosts, [{ scope: 'keepers' }]);
    assert.equal(state.job.kind, 'assist');
    assert.ok(element('#job-label').textContent.startsWith('Local AI assist'), element('#job-label').textContent);
    assert.equal(element('#ai-status').dataset.phase, 'running');
    assert.equal(button.disabled, true, 'one job at a time');
    state.job = null;
    updateWorkflow();
    element('#redo-keepers').checked = true;
    await button.listeners.click();
    assert.deepEqual(assistPosts.at(-1), { scope: 'keepers', redo: true }, 'the redo tick repeats finished work');
    state.job = null;
    updateWorkflow();
    await element('#tag-keepers').listeners.click();
    assert.deepEqual(tagPosts.at(-1), { scope: 'keepers', retag: true }, 'tag keepers honours the same tick');
    return;
  }
  if (scenario === 'import-tab') {
    await load();
    element('#add-tab').listeners.click();
    element('#add-source-import').checked = true; element('#add-source-ingest').checked = false;
    element('#add-source-import').listeners.change();
    assert.equal(element('#import-panel').hidden, false, 'the import radio shows its section');
    assert.equal(element('#ingest-panel').hidden, true);
    element('#import-path').value = '/Volumes/Card';
    await element('#import-form').listeners.submit({ preventDefault() {}, currentTarget: element('#import-form') });
    assert.equal(state.job.status, 'queued');
    assert.equal(element('#import-panel').hidden, false, 'the panel stays open with its status');
    assert.ok(element('#announcer').textContent.includes('Import'));
    assert.deepEqual(importPosts, [{ path: '/Volumes/Card' }]);
    return;
  }
  if (scenario === 'import-cleared') {
    await load();
    element('#import-path').value = '/Volumes/Card';
    await element('#import-form').listeners.submit({ preventDefault() {}, currentTarget: element('#import-form') });
    state.busy = false;
    element('#import-include-cleared').checked = true;
    await element('#import-form').listeners.submit({ preventDefault() {}, currentTarget: element('#import-form') });
    assert.deepEqual(importPosts, [{ path: '/Volumes/Card' }, { path: '/Volumes/Card', include_cleared: true }]);
    const counts = { discovered: 5, imported: 2, updated: 0, skipped: 0, failed: 0 };
    assert.ok(jobText(importJob('succeeded', 1, { result: { ...counts, previously_cleared: 3 } })).includes('2 new, 0 updated, 0 skipped, 0 failed, 3 previously cleared.'));
    assert.ok(!jobText(importJob('succeeded', 1, { result: { ...counts, previously_cleared: 0 } })).includes('previously cleared'));
    return;
  }
  if (scenario === 'network-error') {
    await load();
    await inspect(state.groups[0].photos[1]);
    await loadComposition();
    networkDown = true;
    await askCompositionAI();
    assert.equal(element('#ai-status').dataset.phase, 'failed');
    assert.equal(element('#ai-status').textContent, 'Composition review failed: Lumen server unreachable. If you just restarted lumen serve, reload this page.');
    networkDown = false;
    // A route the server does not have answers with the stdlib's HTML error page, not JSON.
    // Reading the body before the status turned a plain 404 into "Unexpected token '<'",
    // which is what a lumen serve older than the page it is serving looked like.
    routeMissing = true;
    await askCompositionAI();
    assert.equal(element('#ai-status').textContent, 'Composition review failed: Request failed (404). Lumen may be older than this page: restart lumen serve and reload.');
    routeMissing = false;
    return;
  }
  if (scenario === 'place-line') {
    await load();
    const tree = node => [node, ...(node.children || []).flatMap(tree)];
    await inspect(state.groups[0].photos[0]);
    const line = tree(element('#inspection')).find(node => node.id === 'location');
    assert.ok(line.textContent.startsWith('Place: Madikwe Game Reserve, South Africa'), line.textContent);
    assert.ok(line.textContent.includes('24.7435° S, 26.2573° E'), line.textContent);
    const link = tree(element('#inspection')).find(node => node.tagName === 'A');
    assert.equal(link.href, 'https://maps.apple.com/?ll=-24.74351,26.25732&q=photo-1.jpg');
    return;
  }
  if (scenario === 'place-edit') {
    await load();
    const tree = node => [node, ...(node.children || []).flatMap(tree)];
    const byId = id => tree(element('#inspection')).find(node => node.id === id);
    const submit = () => byId('place-form').listeners.submit({ preventDefault() {} });
    await inspect(state.groups[0].photos[0]);
    const edit = byId('edit-place');
    assert.equal(edit.textContent, 'Edit place');
    assert.equal(byId('place-form').hidden, true, 'the form waits for the button');
    edit.listeners.click();
    assert.equal(byId('place-form').hidden, false);
    assert.equal(edit.getAttribute('aria-expanded'), 'true');
    assert.equal(focused, byId('place-name'));
    assert.equal(byId('place-name').value, 'Madikwe Game Reserve', 'prefilled with the current place');
    assert.equal(byId('place-country').value, 'South Africa');
    assert.equal(tree(element('#inspection')).find(node => node.tagName === 'LABEL' && node.htmlFor === 'place-name').textContent, 'Place name');
    assert.equal(byId('place-burst').checked, true, 'the whole burst by default');
    assert.equal(byId('place-burst').parent.children[1], ' Apply to whole burst');
    assert.equal(byId('place-nearby').checked, false, 'the wider radius is opt-in');
    assert.equal(byId('place-nearby').parent.children[1], ' Apply to every frame within a radius');
    assert.ok(!byId('place-nearby').disabled, 'a GPS fix allows the radius');
    assert.deepEqual([byId('place-radius').value, byId('place-radius').min, byId('place-radius').max], [5, 1, 20]);
    assert.equal(byId('place-radius').disabled, true, 'the slider waits for the checkbox');
    assert.equal(byId('place-radius-km').textContent, '5 km');
    assert.equal(byId('place-remember').checked, true);
    assert.ok(!byId('place-remember').disabled, 'a GPS fix allows remembering');
    byId('place-cancel').listeners.click();
    assert.equal(byId('place-form').hidden, true);
    assert.equal(focused, edit, 'cancel returns focus to Edit place');
    edit.listeners.click();
    byId('place-name').value = ' ';
    await submit();
    assert.equal(element('#announcer').textContent, 'place name must be 1-80 characters and country at most 60');
    assert.equal(byId('place-form').hidden, false, 'a refused place keeps the form open');
    byId('place-name').value = 'Our camp';
    byId('place-country').value = 'Botswana';
    await submit();
    assert.deepEqual(placePosts.at(-1), { name: 'Our camp', country: 'Botswana', scope: 'burst', remember: true, radius_km: 5 });
    assert.equal(element('#announcer').textContent, 'Place set to Our camp on 2 frames, remembered');
    assert.deepEqual(state.groups[0].photos.map(item => [item.place, item.place_country]), [['Our camp', 'Botswana'], ['Our camp', 'Botswana']]);
    assert.ok(byId('location').textContent.startsWith('Place: Our camp, Botswana'), byId('location').textContent);
    assert.equal(byId('place-form').hidden, true);
    assert.equal(focused, byId('edit-place'), 'focus returns to Edit place after saving');
    byId('edit-place').listeners.click();
    byId('place-nearby').checked = true;
    byId('place-nearby').listeners.change();
    assert.equal(byId('place-radius').disabled, false, 'checking the box frees the slider');
    byId('place-radius').value = 12;
    byId('place-radius').listeners.input();
    assert.equal(byId('place-radius-km').textContent, '12 km');
    await submit();
    assert.deepEqual(placePosts.at(-1), { name: 'Our camp', country: 'Botswana', scope: 'nearby', remember: true, radius_km: 12 }, 'the radius outranks the burst');
    await inspect(state.groups[0].photos[1]);
    const remember = byId('place-remember');
    assert.ok(byId('edit-place'), 'a frame without a GPS fix still takes a place by hand');
    assert.equal(remember.disabled, true);
    assert.equal(remember.checked, false);
    assert.equal(byId(remember.getAttribute('aria-describedby')).textContent, 'no GPS fix');
    assert.equal(byId('place-nearby').disabled, true, 'no fix, no radius');
    assert.equal(byId('place-nearby').checked, false);
    assert.equal(byId(byId('place-nearby').getAttribute('aria-describedby')).textContent, 'no GPS fix');
    assert.equal(byId('place-radius').disabled, true, 'and the slider with it');
    byId('edit-place').listeners.click();
    byId('place-burst').checked = false;
    byId('place-name').value = 'Hide';
    await submit();
    assert.deepEqual(placePosts.at(-1), { name: 'Hide', country: '', scope: 'frame', remember: false, radius_km: 5 });
    assert.equal(element('#announcer').textContent, 'Place set to Hide on 1 frame');
    assert.equal(byId('location').textContent, 'Place: Hide');
    assert.equal(byId('place-day').parent.children[1], " Fill this day's frames that have no GPS and no place");
    assert.deepEqual([byId('place-day').checked, Boolean(byId('place-day').disabled)], [false, false], 'opt-in, and needs no fix');
    byId('place-day').checked = true;
    byId('place-name').value = 'Hide';
    await submit();
    assert.deepEqual(placePosts.at(-1), { name: 'Hide', country: '', scope: 'day', remember: false, radius_km: 5 }, 'the day outranks the burst');
    const add = byId('add-position');
    assert.equal(add.textContent, 'Add GPS position');
    assert.equal(byId('position-form').hidden, true);
    add.listeners.click();
    assert.equal(byId('position-form').hidden, false);
    assert.equal(focused, byId('position-text'));
    assert.equal(tree(element('#inspection')).find(node => node.tagName === 'LABEL' && node.htmlFor === 'position-text').textContent, 'Position');
    assert.deepEqual([byId('position-burst').checked, byId('position-day').checked], [true, false]);
    byId('position-day').checked = true;
    byId('position-text').value = " 33°55.2'S 18°25.2'E ";
    await byId('position-form').listeners.submit({ preventDefault() {} });
    assert.deepEqual(locationPosts.at(-1), { position: " 33°55.2'S 18°25.2'E ", scope: 'day' }, 'the day outranks the burst');
    assert.equal(element('#announcer').textContent, 'GPS position added to 1 frame');
    assert.equal(byId('location').textContent, 'Place: Hide · 33.9200° S, 18.4200° E');
    assert.equal(byId('add-position').textContent, 'Edit GPS position', 'a typed position stays editable');
    assert.equal(focused, byId('edit-place'));
    byId('add-position').listeners.click();
    assert.equal(byId('position-text').value, '-33.92, 18.42', 'prefilled with the typed position');
    await inspect(state.groups[0].photos[0]);
    assert.ok(!byId('add-position'), "the camera's fix is not editable");
    return;
  }
  if (scenario === 'location') {
    await load();
    const tree = node => [node, ...(node.children || []).flatMap(tree)];
    await inspect(state.groups[0].photos[0]);
    const line = tree(element('#inspection')).find(node => node.id === 'location');
    assert.equal(line.textContent, 'Location: 24.1234° S, 31.5678° E · 312 m');
    const link = tree(element('#inspection')).find(node => node.tagName === 'A');
    assert.equal(link.href, 'https://maps.apple.com/?ll=-24.1234,31.5678&q=photo-1.jpg');
    assert.equal(link.textContent, 'Open in Apple Maps');
    await inspect(state.groups[0].photos[1]);
    assert.equal(tree(element('#inspection')).find(node => node.id === 'location').textContent, 'No location in the file');
    assert.ok(!tree(element('#inspection')).some(node => node.tagName === 'A'));
    return;
  }
  if (scenario === 'bursts') {
    const tree = node => [node, ...(node.children || []).flatMap(tree)];
    const texts = node => tree(node).map(item => item.textContent);
    groups = [{ ...group(1, [1, 2]), started_at: '2026-10-21T08:00:00+00:00' }, { ...group(2, [3]), started_at: '2026-10-21T09:30:00+00:00' }];
    await load();
    const sections = () => element('#contact-sheet').children;
    assert.deepEqual(sections().map(section => [section.tagName, section.className, section.dataset.burst]), [['SECTION', 'burst', 1], ['SECTION', 'burst', 2]]);
    for (const section of sections()) {
      const heading = tree(section).find(node => node.tagName === 'H3');
      assert.equal(section.getAttribute('aria-labelledby'), heading.id, 'each burst section is labelled by its heading');
    }
    const headerText = texts(sections()[0]).join(' ');
    for (const text of ['Burst 1', '2 frames', 'best photo-1.jpg']) assert.ok(headerText.includes(text), headerText);
    assert.ok(headerText.includes(new Date(groups[0].started_at).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })), 'the header shows the local start time: ' + headerText);
    assert.equal(document.querySelectorAll('.frame').length, 3, 'frames stay as buttons inside their burst sections');
    assert.ok(document.querySelectorAll('.frame').every(card => card.parent.parent === sections()[Number(card.dataset.group) - 1]));
    const rows = tree(element('#batch-list')).filter(node => node.className === 'batch-row');
    assert.deepEqual(rows.map(row => row.children.map(child => child.tagName)), [['IMG', 'INPUT', 'BUTTON'], ['IMG', 'INPUT', 'BUTTON']]);
    assert.deepEqual(rows.map(row => [row.children[0].src, row.children[0].alt, row.children[0].loading]), [['/thumb/1', '', 'lazy'], ['/thumb/3', '', 'lazy']], 'rail thumbs use the small tier');
    assert.ok(document.querySelectorAll('.frame').every(card => card.children[0].src === '/thumb/' + card.dataset.id), 'cards use the small tier');
    assert.ok(rows[0].children[2].textContent.startsWith('Burst 1 · 2 frames · '), rows[0].children[2].textContent);
    assert.equal(element('#merge-count').textContent, '0 batches selected');

    const sheetBoxes = () => document.querySelectorAll('[data-merge-group]').filter(box => box.className === 'burst-select');
    const railBoxes = () => document.querySelectorAll('.batch-select');
    assert.equal(sheetBoxes().length, 2);
    assert.ok(sheetBoxes().every(box => box.getAttribute('aria-label').startsWith('Select Burst')));
    sheetBoxes()[0].checked = true; sheetBoxes()[0].listeners.change();
    assert.equal(railBoxes()[0].checked, true, 'the rail checkbox follows the burst header');
    assert.equal(element('#merge-count').textContent, '1 batch selected');
    railBoxes()[1].checked = true; railBoxes()[1].listeners.change();
    assert.equal(sheetBoxes()[1].checked, true, 'the burst header follows the rail checkbox');
    assert.equal(element('#merge-count').textContent, '2 batches selected');
    assert.equal(element('#merge-batches').disabled, false);

    const batches = () => document.querySelectorAll('.batch-button');
    focused = batches()[1];
    await batches()[1].listeners.click();
    assert.equal(state.selectedGroupId, 2);
    assert.deepEqual(scrolled.at(-1), [sections()[1], { behavior: 'auto', block: 'start' }], 'a rail click scrolls the burst into view, honouring reduced motion');
    assert.equal(focused, batches()[1], 'keyboard focus stays on the rail');
    assert.deepEqual(sections().map(section => section.dataset.selected), ['false', 'true']);
    assert.ok(texts(sections()[1]).includes('Selected') && !texts(sections()[0]).includes('Selected'), 'the selected burst has a text cue');

    element('#jump-selected').listeners.click();
    assert.deepEqual(scrolled.at(-1), [sections()[1], { behavior: 'auto', block: 'start' }]);
    assert.ok(element('#announcer').textContent.includes('Burst 2'));
    element('#back-to-top').listeners.click();
    assert.deepEqual(scrolled.at(-1), [element('#main-content'), { top: 0, behavior: 'auto' }]);
    assert.ok(element('#announcer').textContent.includes('workflow tabs'));

    const exportTab = element('#add-tab');  // the open tab
    element('#workflow-panels').hidden = false;
    exportTab.listeners.click();
    assert.equal(element('#workflow-panels').hidden, true, 'clicking the open tab folds the panel away');
    assert.equal(stored['lumen-panel-collapsed'], '1', 'the folded panel is remembered');
    assert.equal(element('#announcer').textContent, 'Workflow panel collapsed; the contact sheet has the space.');
    exportTab.listeners.click();
    assert.equal(element('#workflow-panels').hidden, false, 'clicking it again brings the panel back');
    assert.equal(stored['lumen-panel-collapsed'], undefined);
    exportTab.listeners.click();
    element('#review-tab').listeners.click();
    assert.equal(element('#workflow-panels').hidden, false, 'another tab always shows its panel');
    assert.equal(element('#review-tab').getAttribute('aria-selected'), 'true');

    const headerButtons = sections().flatMap(section => tree(section.children[0]).filter(node => node.tagName === 'BUTTON').map(node => node.textContent));
    assert.deepEqual(headerButtons, [], 'burst headers carry no per-burst action buttons; Clear and Astro tabs own them');
    return;
  }
  if (scenario === 'remove-cleared') {
    await load();
    const button = element('#remove-cleared');
    assert.equal(clearedLoads, 0, 'counted only when the Clear tab opens');
    selectTab(element('#clear-tab'));
    await new Promise(setImmediate);
    assert.equal(element('#cleared-count').textContent, '3 frames in 1 batch');
    assert.equal(button.disabled, false);
    state.busy = true; updateWorkflow();
    assert.equal(button.disabled, true, 'busy disables it');
    state.busy = false; updateWorkflow();
    cleared = { frames: 1, batches: 0 };
    watchJob(importJob('running', 0.5));
    watchJob(importJob('succeeded', 1, { result: { discovered: 1, imported: 1, updated: 0, skipped: 0, failed: 0 } }));
    await new Promise(setImmediate);
    assert.equal(clearedLoads, 2, 'a finished job refreshes the count on the Clear tab');
    assert.equal(element('#cleared-count').textContent, '1 frame in 0 batches');
    await button.listeners.click();
    assert.deepEqual(clearedPosts, [{}]);
    assert.ok(element('#clear-status').textContent.startsWith('Removed 1 frame'), element('#clear-status').textContent);
    assert.equal(clearedLoads, 3, 'removal refreshes the count');
    assert.equal(element('#cleared-count').textContent, 'None');
    assert.equal(button.disabled, true, 'nothing left to remove');
    selectTab(element('#review-tab'));
    watchJob(importJob('running', 0.5)); watchJob(importJob('succeeded', 1, { result: { discovered: 1, imported: 1, updated: 0, skipped: 0, failed: 0 } }));
    await new Promise(setImmediate);
    assert.equal(clearedLoads, 3, 'other tabs do not count');
    return;
  }
  if (scenario === 'rail-visibility') {
    await load();
    assert.equal(state.selectedGroupId, null);
    assert.equal(element('#merge-count').hidden, true, 'nothing ticked: no merge count');
    assert.equal(element('#merge-batches').hidden, true, 'nothing ticked: no merge button');
    assert.equal(element('#jump-selected').hidden, true, 'no burst selected: no jump');
    assert.ok(!element('#back-to-top').hidden, 'Back to top always applies');
    assert.equal(element('#open-compare').hidden, true, 'nothing marked: no compare');
    const boxes = document.querySelectorAll('.batch-select');
    boxes[0].checked = true; boxes[0].listeners.change();
    assert.equal(element('#merge-count').hidden, false);
    assert.equal(element('#merge-batches').hidden, false, 'one ticked batch shows the merge button');
    assert.equal(element('#merge-batches').disabled, true, 'but one batch is not a merge');
    await document.querySelectorAll('.batch-button')[1].listeners.click();
    assert.equal(element('#jump-selected').hidden, false, 'a selected burst shows Jump to selected');
    const card = id => document.querySelectorAll('.frame').find(item => Number(item.dataset.id) === id);
    await element('#contact-sheet').listeners.keydown({ key: 'c', metaKey: false, ctrlKey: false, altKey: false, target: card(state.selectedId), preventDefault() {}, stopPropagation() {} });
    assert.equal(element('#open-compare').hidden, false, 'a marked frame shows Compare marked');
    assert.equal(element('#open-compare').disabled, true, 'one marked frame cannot be compared');
    boxes[0].checked = false; boxes[0].listeners.change();
    assert.equal(element('#merge-batches').hidden, true, 'unticking hides it again');
    return;
  }
  if (scenario === 'merge-batches') {
    await load();
    const button = element('#merge-batches');
    assert.equal(button.disabled, true, 'nothing selected');
    let boxes = document.querySelectorAll('.batch-select');
    assert.equal(boxes.length, 2);
    assert.ok(boxes.every(box => box.getAttribute('aria-label').startsWith('Select Burst')));
    boxes[0].checked = true; boxes[0].listeners.change();
    assert.equal(button.disabled, true, 'one batch is not a merge');
    boxes[1].checked = true; boxes[1].listeners.change();
    assert.equal(button.disabled, false);
    const loadsBefore = groupLoads;
    const mKey = (extra = {}) => document.listeners.keydown({ key: 'm', metaKey: false, ctrlKey: false, altKey: false, target: boxes[1], preventDefault() {}, ...extra });
    await mKey({ target: document.createElement('input') });
    await mKey({ metaKey: true });
    assert.deepEqual(mergePosts, [], 'typing M in a field or with a modifier does not merge');
    await mKey();
    assert.deepEqual(mergePosts, [{ group_ids: [1, 2] }]);
    assert.equal(groupLoads, loadsBefore + 1, 'merge reloads the batches');
    assert.equal(state.selectedGroupId, 1);
    assert.equal(state.selectedId, 1, 'the merged batch is selected');
    assert.equal(document.querySelectorAll('.batch-button').length, 1);
    boxes = document.querySelectorAll('.batch-select');
    assert.equal(boxes.length, 1);
    assert.equal(boxes[0].checked, false, 'selection is cleared after the merge');
    assert.equal(element('#merge-batches').disabled, true);
    assert.ok(element('#announcer').textContent.includes('Merged 2 batches'), element('#announcer').textContent);
    return;
  }
  await inspect(state.groups.flatMap(item => item.photos).find(item => item.id === 2));
  assert.equal(state.selectedGroupId, scenario === 'merge' ? 2 : 1);
  element('#export-tab').listeners.click();
  assert.equal(state.selectedId, 2, 'changing tabs must preserve the selected photo');
  element('#composition-advice').textContent = 'Old advice';
  const requestBeforePhotoChange = state.compositionRequest;
  await inspect(state.groups.flatMap(item => item.photos).find(item => item.id === 1));
  assert.ok(state.compositionRequest > requestBeforePhotoChange, 'a photo change must invalidate composition operations');
  assert.equal(element('#composition-advice').textContent, '');
  await inspect(state.groups.flatMap(item => item.photos).find(item => item.id === 2));
  await loadComposition();
  assert.equal(state.selectedId, 2, 'loading crops must preserve the selected photo');
  assert.equal(state.composition.photo_id, 2);
  let cards = element('#primary-crops').children;
  let originalAction = cards[0].children.at(-1);
  let cropAction = cards[1].children.at(-1);
  assert.equal(originalAction.getAttribute('aria-label'), 'Keep Original, Original ratio');
  assert.equal(cropAction.getAttribute('aria-label'), 'Use Centered fallback crop, 1:1');
  await cropAction.listeners.click();
  assert.deepEqual(cropMutations, [{ url: '/api/photos/2/crop', body: { candidate_id: cropId } }]);
  cards = element('#primary-crops').children;
  assert.ok(cards[1].children.find(child => child.className === 'crop-marker').textContent.includes('Current choice'));
  assert.equal(cards[1].children.at(-1).disabled, true);
  originalAction = cards[0].children.at(-1);
  await originalAction.listeners.click();
  assert.deepEqual(cropMutations[1], { url: '/api/photos/2/crop/clear', body: {} });
  assert.ok(element('#primary-crops').children[0].children.find(child => child.className === 'crop-marker').textContent.includes('Current choice'));

  reviewMode = 'failure';
  await element('#ask-composition-ai').listeners.click();
  assert.ok(element('#compose-status').textContent.includes('Manual crop choices still work.'));
  assert.equal(element('#primary-crops').children[1].children.at(-1).disabled, false);
  reviewMode = 'success';

  const oldCropAction = element('#primary-crops').children[1].children.at(-1);
  const reviewsBeforeStale = compositionReviews.length;
  state.selectedId = 1;
  updateWorkflow();
  assert.equal(element('#ask-composition-ai').disabled, true, 'stale AI advice must be disabled');
  await oldCropAction.listeners.click();
  assert.equal(cropMutations.length, 2, 'a stale card must not mutate the newly selected photo');
  await element('#ask-composition-ai').listeners.click();
  assert.equal(compositionReviews.length, reviewsBeforeStale, 'stale candidates must not be sent for AI review of a new photo');
  state.selectedId = 2;
  await element('#ask-composition-ai').listeners.click();
  assert.equal(compositionReviews.at(-1), '/api/photos/2/composition-review');
  assert.equal(cropMutations.length, 2, 'AI advice must never select a crop');
  assert.ok(element('#composition-advice').textContent.includes('Keep the context.'));

  activeCrop = { candidate_id: cropId };
  deferCropLoads = true;
  const olderLoad = loadComposition();
  const newerLoad = loadComposition();
  assert.equal(state.composition, null, 'a new load must invalidate the displayed candidate state');
  assert.equal(element('#compose-workspace').hidden, true);
  await new Promise(setImmediate);
  pendingCropLoads[1](cropPayload(activeCrop));
  await newerLoad;
  pendingCropLoads[0](cropPayload(null));
  await olderLoad;
  assert.equal(state.composition.active_crop.candidate_id, cropId, 'an older same-photo load must not replace newer state');
  deferCropLoads = false;

  deferReviews = true;
  const oldReview = askCompositionAI();
  await new Promise(setImmediate);
  const refresh = loadComposition();
  pendingReviews[0]({ candidate_id: cropId, rationale: 'Stale advice.', confidence: 1 });
  await Promise.all([oldReview, refresh]);
  assert.equal(state.recommendedCropId, null, 'a new load must invalidate an older AI review');
  assert.equal(element('#composition-advice').textContent, '');
  deferReviews = false;

  reviewMode = 'mismatch';
  const beforeMismatchReload = cropLoads;
  await askCompositionAI();
  assert.equal(cropLoads, beforeMismatchReload + 1, 'an unknown recommendation must reload candidates');
  assert.equal(state.recommendedCropId, null);
  reviewMode = 'success';

  state.selectedId = 2;
  groups = scenario === 'split' ? [group(1, [1]), group(2, [2])] : scenario === 'merge' ? [group(3, [1, 2])] : [group(1, [1])];
  await load();
  if (scenario === 'missing') {
    assert.equal(state.selectedId, null);
    assert.equal(state.selectedGroupId, null);
    assert.equal(element('#group-cleanup-count').textContent, 'No batch selected · 0 frames');
    assert.equal(element('#remove-group').disabled, true);
    assert.equal(element('#export-jpegs').disabled, true);
  } else {
    const id = scenario === 'split' ? 2 : 3;
    assert.equal(state.selectedId, 2);
    assert.equal(state.selectedGroupId, id, 'selected batch must follow the selected photo');
    assert.equal(element('#group-cleanup-count').textContent, scenario === 'split' ? 'Burst 2 · 1 frame' : 'Burst 1 · 2 frames');
    assert.equal(element('#remove-group').disabled, false);
    const batches = document.querySelectorAll('.batch-button');
    assert.deepEqual(batches.map(item => item.getAttribute('aria-pressed')), scenario === 'split' ? ['false', 'true'] : ['true']);
    await element('#remove-group').listeners.click();
    assert.deepEqual(removals, ['/api/groups/' + id + '/remove']);
  }
})()
`;
let finished = false;  // Timers are stubbed, so a never-settling await would otherwise exit 0 with assertions skipped.
process.on('exit', () => { if (!finished) { console.error('scenario did not finish'); process.exitCode = 1; } });
vm.runInNewContext(harness + fs.readFileSync(process.argv[1], 'utf8') + regression, {
  assert: require('node:assert/strict'), URLSearchParams, AbortController, setImmediate, scenario: process.argv[2], apiBase: process.argv[3], httpFetch: globalThis.fetch,
}).then(() => { finished = true; }, error => { finished = true; console.error(error); process.exitCode = 1; });
"""
        app = Path(__file__).resolve().parents[1] / "src" / "lumen" / "static" / "app.js"
        result = subprocess.run(["node", "-e", script, str(app), scenario, self.base], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_javascript_decisions_update_cards_in_place_and_keeper_count(self):
        self.run_javascript_cleanup_regression("decisions")

    def test_filter_bar_is_two_rows_without_apply_or_fold(self):
        html = urllib.request.urlopen(self.base + "/", timeout=3).read().decode()
        parser = LandmarkParser()
        parser.feed(html)
        by_id = {node["attrs"]["id"]: node for node in parser.nodes if "id" in node["attrs"]}
        form = by_id["filter-form"]
        rows = [node for node in parser.nodes if node["parents"] and node["parents"][-1] is form and "filter-row" in node["attrs"].get("class", "")]
        self.assertEqual(2, len(rows))
        for name in ("filter-text", "expand-search", "filter-status", "filter-rating", "reset-filters"):
            self.assertIn(rows[0], by_id[name]["parents"], f"{name} sits on the first row")
        for name in ("filter-from", "filter-to", "filter-camera", "filter-tagged"):
            self.assertIn(rows[1], by_id[name]["parents"], f"{name} sits on the second row")
        heading = by_id["contact-heading"]["parents"][-1]
        for name in ("tile-size", "open-compare", "shortcuts-button"):
            self.assertIn(heading, by_id[name]["parents"], f"{name} sits in the heading row, apart from the filters")
        self.assertFalse(any(node["tag"] in {"details", "summary"} and form in node["parents"] for node in parser.nodes), "no folded section")
        self.assertFalse(any(node["tag"] == "button" and node["attrs"].get("type") == "submit" and form in node["parents"] for node in parser.nodes), "no Apply button")
        self.run_javascript_cleanup_regression("filter-live")

    def test_javascript_filters_write_url_group_days_and_page_frames(self):
        self.run_javascript_cleanup_regression("filters")

    def test_javascript_reload_resumes_last_selected_frame(self):
        self.run_javascript_cleanup_regression("resume")

    def test_javascript_reload_forgets_a_frame_that_is_gone(self):
        self.run_javascript_cleanup_regression("resume-gone")

    def test_javascript_expand_search_posts_the_query_and_applies_the_or_text(self):
        self.run_javascript_cleanup_regression("expand-search")

    def test_javascript_find_input_filters_and_inspector_shows_match_reasons(self):
        self.run_javascript_cleanup_regression("find")

    def test_rail_find_input_is_labelled_and_mirrors_the_text_filter(self):
        html = (Path(__file__).parent.parent / "src/lumen/static/index.html").read_text()
        self.assertIn('<label for="find">Find</label>', html)
        self.assertIn('id="find"', html)

    def test_javascript_all_rejected_burst_explains_no_keeper_and_offers_pick(self):
        self.run_javascript_cleanup_regression("all-rejected")

    def test_javascript_culling_keys_move_focus_and_decide_in_place(self):
        self.run_javascript_cleanup_regression("culling-keys")

    def test_javascript_compare_syncs_panes_and_decides_in_place(self):
        self.run_javascript_cleanup_regression("compare")

    def test_javascript_loupe_opens_on_enter_pans_and_returns_focus(self):
        self.run_javascript_cleanup_regression("loupe")

    def test_javascript_loupe_filmstrip_steps_with_brackets(self):
        self.run_javascript_cleanup_regression("loupe-strip")

    def test_javascript_frame_view_gathers_inspector_and_crops(self):
        html = urllib.request.urlopen(self.base + "/", timeout=3).read().decode()
        parser = LandmarkParser()
        parser.feed(html)
        by_id = {node["attrs"]["id"]: node for node in parser.nodes if "id" in node["attrs"]}
        for gone in ("compose-tab", "compose-panel"):
            self.assertNotIn(gone, by_id)
        self.assertFalse(any("inspector" in node["attrs"].get("class", "").split() for node in parser.nodes))
        loupe = by_id["loupe"]
        order = ["loupe-viewport", "loupe-full", "inspection", "compose-workspace", "compose-status", "loupe-export", "close-loupe"]
        for name in order:
            self.assertIn(loupe, by_id[name]["parents"], f"{name} lives in the frame view")
            self.assertFalse(any(node["tag"] == "form" for node in by_id[name]["parents"]), f"{name} sits outside any form")
        self.assertEqual(order, sorted(order, key=lambda name: parser.nodes.index(by_id[name])), "tab order runs viewport, zoom, right column, Close")
        full = by_id["loupe-full"]
        self.assertEqual(("button", "button", "false", "Full size"), (full["tag"], full["attrs"].get("type"), full["attrs"].get("aria-pressed"), full["text"].strip()))
        self.assertEqual(("button", "Export this frame"), (by_id["loupe-export"]["tag"], by_id["loupe-export"]["text"].strip()))
        self.assertEqual("export-availability", by_id["loupe-export"]["attrs"].get("aria-describedby"), "a disabled export says why")
        styles = (Path(__file__).resolve().parents[1] / "src" / "lumen" / "static" / "styles.css").read_text()
        self.assertRegex(styles, r"#loupe\[open\] \{[^}]*grid-template-columns: minmax\(0, 2fr\) minmax\(0, 1fr\)")  # the viewport keeps two thirds
        self.run_javascript_cleanup_regression("frame-view")

    def test_ai_buttons_share_grammar_and_one_availability_line(self):
        html = urllib.request.urlopen(self.base + "/", timeout=3).read().decode()
        parser = LandmarkParser()
        parser.feed(html)
        by_id = {node["attrs"]["id"]: node for node in parser.nodes if "id" in node["attrs"]}
        labels = {
            "assist-keepers": "Tag, scan and caption keepers", "tag-keepers": "Tag keepers",
            "tag-all": "Tag every untagged frame", "scan-rejects": "Scan keepers for problems",
            "triage-astro": "Triage lights", "expand-search": "Expand search",
            "ask-composition-ai": "Suggest a crop", "develop-ask": "Suggest settings",
            "write-digest": "Write today's digest",
        }
        for name, label in labels.items():
            node = by_id[name]
            self.assertIn("ai", node["attrs"].get("class", "").split(), name)
            line = "frame-ai-availability" if by_id["loupe"] in node["parents"] else "ai-availability"
            self.assertEqual(line, node["attrs"].get("aria-describedby"), name)
            self.assertEqual(label, " ".join(node["text"].split()).partition(" (")[0], name)
        self.assertEqual("Tag keepers (0)", by_id["tag-keepers"]["text"].strip(), "the count is the bare number")
        lines = {name for name in by_id if name.endswith("-availability")}
        self.assertEqual({"ai-availability", "frame-ai-availability", "export-availability", "refine-availability", "astro-availability"}, lines)
        panel = next(node for node in parser.nodes if "frame-panel" in node["attrs"].get("class", "").split())
        first = next(node for node in parser.nodes if node["parents"] and node["parents"][-1] is panel)
        self.assertEqual("frame-ai-availability", first["attrs"].get("id"), "the not-loaded line leads the frame panel")
        self.assertNotIn("develop-export", by_id)
        self.assertEqual(1, html.count('id="loupe-export"'))
        export = by_id["loupe-export"]
        self.assertIn(by_id["loupe"], export["parents"])
        for tab_panel in ("frame-panel", "crop-panel", "develop-panel", "exif-panel"):
            self.assertNotIn(by_id[tab_panel], export["parents"], "the export sits in the dialog footer, under every tab")
        self.assertGreater(parser.nodes.index(export), parser.nodes.index(by_id["exif-panel"]))
        self.assertNotIn("hidden", by_id["loupe-export-options"]["attrs"], "the options show on every frame tab")
        styles = (Path(__file__).resolve().parents[1] / "src" / "lumen" / "static" / "styles.css").read_text()
        self.assertIn('button.ai::before { content: "\u2726 "; color: var(--cyan); }', styles)
        self.assertIn("button.ai:not(.secondary)::before { color: var(--black); }", styles)

    def test_javascript_frame_view_ignores_stale_frame_responses(self):
        self.run_javascript_cleanup_regression("frame-view-race")

    def test_javascript_frame_view_full_size_toggle(self):
        self.run_javascript_cleanup_regression("frame-view-full")

    def test_develop_controls_match_the_server_ranges(self):
        """A slider that offers a value the server rejects is a 400 the photographer cannot explain."""
        html = urllib.request.urlopen(self.base + "/", timeout=3).read().decode()
        parser = LandmarkParser()
        parser.feed(html)
        by_id = {node["attrs"]["id"]: node for node in parser.nodes if "id" in node["attrs"]}
        panel = by_id["develop-panel"]
        for key, (low, high) in DEVELOP_RANGES.items():
            for node in (by_id[f"develop-{key}"], by_id[f"develop-{key}-value"]):
                self.assertIn(panel, node["parents"], f"{key} lives in the Develop panel")
                self.assertEqual((float(low), float(high)), (float(node["attrs"]["min"]), float(node["attrs"]["max"])), key)
                self.assertGreater(float(node["attrs"]["step"]), 0, key)
            self.assertEqual("range", by_id[f"develop-{key}"]["attrs"]["type"])
            self.assertEqual(("number", "not set"), (by_id[f"develop-{key}-value"]["attrs"]["type"],
                                                     by_id[f"develop-{key}-value"]["attrs"]["placeholder"]),
                             "an unset field shows that it is unset, never a value the file did not choose")
            self.assertEqual("Reset", by_id[f"develop-{key}-reset"]["text"].strip())
        self.assertEqual(5, len([node for node in parser.nodes if node["attrs"].get("class") == "curve-point"]),
                         "five curve points, each keyboard reachable")
        for node in (node for node in parser.nodes if node["attrs"].get("class") == "curve-point"):
            self.assertEqual(("slider", "0"), (node["attrs"].get("role"), node["attrs"].get("tabindex")))
        summaries = [" ".join(node["text"].split()) for node in parser.nodes if node["tag"] == "summary" and panel in node["parents"]]
        # The suggestion panel is not a develop control, so it sits above them all rather than in
        # their order: it is where a photographer who cannot read the histogram yet starts.
        self.assertEqual(["Where to start", "Brightness", "White Balance", "Contrast", "Sharpness",
                          "Lens Correction", "Noise Reduction", "How this works"], summaries, "IEDT's order, after the suggestion")
        # Every flag in DEVELOP_FLAGS needs a control, or the server accepts a setting the
        # panel cannot reach.
        for flag in DEVELOP_FLAGS:
            node = by_id[f"develop-{flag}"]
            self.assertIn(panel, node["parents"], f"{flag} lives in the Develop panel")
            self.assertEqual("checkbox", node["attrs"]["type"], flag)
        modes = [node["attrs"]["value"] for node in parser.nodes
                 if node["attrs"].get("name") == "develop-nr-mode"]
        self.assertEqual(["auto", "off", "manual"], modes, "auto first: it is what an untouched frame is")
        self.assertIn(by_id["export-controls"], by_id["export-keep-original"]["parents"])
        self.assertIn(by_id["loupe"], by_id["loupe-keep-original"]["parents"])
        self.assertNotIn(by_id["develop-panel"], by_id["loupe-keep-original"]["parents"])  # the frame view's one export honours it, so the dialog footer shows it
        for name in ("export-keep-original", "loupe-keep-original"):
            self.assertEqual("checkbox", by_id[name]["attrs"]["type"])
            label = next(node for node in parser.nodes if node["tag"] == "label" and node["attrs"].get("for") == name)
            self.assertEqual("Also export the untouched version", " ".join(label["text"].split()))

    def test_javascript_develop_tab_sends_only_touched_fields(self):
        self.run_javascript_cleanup_regression("develop")

    def test_javascript_develop_tab_is_disabled_when_the_original_is_offline(self):
        self.run_javascript_cleanup_regression("develop-offline")

    def test_javascript_frame_view_crop_draw(self):
        css = (Path(__file__).parent.parent / "src/lumen/static/styles.css").read_text()
        self.assertIn("#loupe-viewport img.fit { max-width: 100%; max-height: 100%; }", css, "the frame view shows its preview whole")
        self.run_javascript_cleanup_regression("crop-draw")

    def test_javascript_frame_view_crop_manual_card(self):
        self.run_javascript_cleanup_regression("crop-manual-card")

    def test_javascript_frame_tabs_split_the_right_column(self):
        html = urllib.request.urlopen(self.base + "/", timeout=3).read().decode()
        parser = LandmarkParser()
        parser.feed(html)
        by_id = {node["attrs"]["id"]: node for node in parser.nodes if "id" in node["attrs"]}
        tablist = by_id["frame-tabs"]
        self.assertEqual("tablist", tablist["attrs"].get("role"))
        self.assertIn(by_id["loupe"], tablist["parents"])
        for tab, panel, selected, contents in (
            ("frame-tab", "frame-panel", "true", ["inspection"]),
            ("crop-tab", "crop-panel", "false", ["crop-lock", "primary-crops", "ask-composition-ai"]),
            ("develop-tab", "develop-panel", "false", ["develop-curve", "develop-curve-reset", "develop-exposure_ev", "develop-exposure_ev-value",
                                                       "develop-exposure_ev-reset", "develop-reset-all", "develop-apply-group"]),
            ("exif-tab", "exif-panel", "false", ["exif-fields", "exif-status"]),
        ):
            self.assertIn(tablist, by_id[tab]["parents"])
            self.assertEqual(("tab", panel, selected), (by_id[tab]["attrs"].get("role"), by_id[tab]["attrs"].get("aria-controls"), by_id[tab]["attrs"].get("aria-selected")))
            self.assertEqual(("tabpanel", tab), (by_id[panel]["attrs"].get("role"), by_id[panel]["attrs"].get("aria-labelledby")))
            for name in contents:
                self.assertIn(by_id[panel], by_id[name]["parents"], f"{name} lives in {panel}")
        self.assertIn("hidden", by_id["crop-panel"]["attrs"])
        self.assertIn('<label for="crop-lock">Crop lock</label>', html, "the label matches the README")
        self.assertNotIn(by_id["crop-panel"], by_id["close-loupe"]["parents"])
        self.assertNotIn(by_id["frame-panel"], by_id["close-loupe"]["parents"])
        for name in ("loupe-keep-original", "loupe-dated-folder"):
            self.assertIn(by_id["loupe-export-options"], by_id[name]["parents"])
        self.run_javascript_cleanup_regression("frame-tabs")

    def test_compare_and_loupe_dialogs_have_labeled_viewports_and_actions(self):
        html = urllib.request.urlopen(self.base + "/", timeout=3).read().decode()
        parser = LandmarkParser()
        parser.feed(html)
        by_id = {node["attrs"]["id"]: node for node in parser.nodes if "id" in node["attrs"]}
        opener = by_id["open-compare"]
        self.assertEqual(("button", "button"), (opener["tag"], opener["attrs"].get("type")))
        self.assertIn("disabled", opener["attrs"])
        self.assertTrue(any(node["attrs"].get("class") == "section-heading" for node in opener["parents"]))
        for name in ("compare", "loupe"):
            dialog = by_id[name]
            self.assertEqual("dialog", dialog["tag"])
            self.assertTrue(by_id[dialog["attrs"]["aria-labelledby"]]["text"].strip())
        form = next(node for node in parser.nodes if node["tag"] == "form" and by_id["compare"] in node["parents"])
        self.assertEqual("dialog", form["attrs"].get("method"))
        viewports = ["compare-left-viewport", "compare-right-viewport", "loupe-viewport"]
        for name in viewports:
            viewport = by_id[name]
            self.assertEqual("0", viewport["attrs"].get("tabindex"))
            self.assertIn("loupe-viewport", viewport["attrs"].get("class", ""))
            self.assertTrue(viewport["attrs"].get("aria-label"))
            self.assertIn(by_id["compare" if name.startswith("compare") else "loupe"], viewport["parents"])
        for side in ("left", "right"):
            pane = by_id[f"compare-{side}"]
            self.assertTrue(pane["attrs"].get("aria-label"))
            self.assertIn(pane, by_id[f"compare-{side}-viewport"]["parents"])
            self.assertEqual("dl", by_id[f"compare-{side}-details"]["tag"])
            for action, text in (("pick", "Pick as best"), ("flag", "Flag"), ("reject", "Reject")):
                button = by_id[f"compare-{side}-{action}"]
                self.assertEqual(("button", "button", text), (button["tag"], button["attrs"].get("type"), button["text"].strip()))
                self.assertIn(pane, button["parents"])
            rating = by_id[f"compare-{side}-rating"]
            self.assertEqual("select", rating["tag"])
            self.assertTrue(any(node["tag"] == "label" and node["attrs"].get("for") == f"compare-{side}-rating" and node["text"].strip() for node in parser.nodes))
        for name, text, dialog in (("swap-compare", "Swap sides", "compare"), ("close-compare", "Close", "compare"), ("close-loupe", "Close", "loupe")):
            self.assertEqual(("button", text), (by_id[name]["tag"], by_id[name]["text"].strip()))
            self.assertIn(by_id[dialog], by_id[name]["parents"])
        styles = (Path(__file__).resolve().parents[1] / "src" / "lumen" / "static" / "styles.css").read_text()
        self.assertRegex(styles, r"\.loupe-viewport\s*\{[^}]*overflow:\s*auto")
        self.assertRegex(styles, r"@media \(max-width: 680px\)\s*\{[^@]*\.compare-grid\s*\{\s*grid-template-columns:\s*1fr")

    def test_panels_lead_with_one_line_and_fold_the_rest(self):
        html = urllib.request.urlopen(self.base + "/", timeout=3).read().decode()
        parser = LandmarkParser()
        parser.feed(html)
        by_id = {node["attrs"]["id"]: node for node in parser.nodes if "id" in node["attrs"]}
        leads = {
            "add-panel": "Copy a memory card with verification, or add photos already on a drive. Originals are never moved, renamed or changed.",
            "review-panel": "The fast model looks for technical failures in keepers. Nothing is rejected until you choose Reject.",
            "assist-panel": "Tags, a problem scan and a caption for every keeper that lacks them. Already finished work is skipped.",
            "astro-panel": "Stacks the selected batch with one reviewed Siril script. Camera originals are never changed.",
            "export-panel": "sRGB JPEGs with camera EXIF, caption, keywords and rating. Only Full resolution keeps GPS.",
            "clear-panel": "Camera originals and finished exports remain. Working files move to recoverable library trash.",
        }
        def children(parent):
            return [node for node in parser.nodes if node["parents"] and node["parents"][-1] is parent]
        def how(parent):
            return [node for node in children(parent) if node["tag"] == "details" and node["attrs"].get("class") == "how"]
        for panel_id, line in leads.items():
            with self.subTest(panel=panel_id):
                panel = by_id[panel_id]
                kids = children(panel)
                self.assertEqual(["h2", "p"], [node["tag"] for node in kids[:2]], "the heading, then the one line")
                self.assertEqual(("lead", line), (kids[1]["attrs"].get("class"), " ".join(kids[1]["text"].split())))
                # Review's old paragraph became its lead and a Shortcuts row, so nothing is left to fold.
                # Add photos folds inside each of its two sources instead, beside that source's controls.
                expected = 0 if panel_id in ("review-panel", "add-panel") else 1
                folds = how(panel)
                self.assertEqual(expected, len(folds))
                if folds:
                    self.assertIs(kids[2], folds[0], "the fold sits right under the line")
        for section in ("ingest-panel", "import-panel"):
            folds = how(by_id[section])
            self.assertEqual(1, len(folds), section)
            self.assertIs(children(by_id[section])[1], folds[0], "under the section's heading")
        for fold in (node for node in parser.nodes if node["attrs"].get("class") == "how"):
            summary = children(fold)[0]
            self.assertEqual(("summary", "How this works"), (summary["tag"], summary["text"].strip()))
        self.assertFalse([node for node in parser.nodes if "terms" in node["attrs"].get("class", "").split()], "the glossary is gone")
        develop = [node for node in parser.nodes if node["attrs"].get("class") == "how" and by_id["develop-panel"] in node["parents"]]
        self.assertEqual(1, len(develop), "one fold for the Develop panel's explanations")
        text = " ".join(develop[0]["text"].split())
        for sentence in ("A spike at either edge is clipping.", "Nothing is saved until you leave the tab",
                         "frame's own neutral", "Sharpness is the decoder's own", "Auto leaves the file's own baseline alone.",
                         "Apply gives every other frame in this burst"):
            self.assertIn(sentence, text)
        self.assertNotIn("nudge it with the arrow keys", html)
        self.assertNotIn("to move between rows", html)
        legend = [node for node in parser.nodes if by_id["shortcuts"] in node["parents"]]
        rows = [(" ".join(node["text"].split())) for node in legend if node["tag"] in ("dt", "dd")]
        for row in ("X K ↑ ↓", "In Review, with a row's button focused: reject, keep, previous or next row",
                    "↑ ↓ ← → on a curve point", "Nudge the tone curve point"):
            self.assertIn(row, rows)
        styles = (Path(__file__).resolve().parents[1] / "src" / "lumen" / "static" / "styles.css").read_text()
        self.assertRegex(styles, r"\.lead \{[^}]*color: var\(--muted\)")
        self.assertRegex(styles, r"\.how summary[^{]*\{[^}]*min-height: 44px")

    def test_index_has_shortcuts_button_and_readable_legend_without_reserved_modifiers(self):
        html = urllib.request.urlopen(self.base + "/", timeout=3).read().decode()
        parser = LandmarkParser()
        parser.feed(html)
        by_id = {node["attrs"]["id"]: node for node in parser.nodes if "id" in node["attrs"]}
        self.assertEqual(("button", "button"), (by_id["shortcuts-button"]["tag"], by_id["shortcuts-button"]["attrs"].get("type")))
        self.assertEqual("dialog", by_id["shortcuts"]["tag"])
        legend = [node for node in parser.nodes if by_id["shortcuts"] in node["parents"]]
        self.assertTrue(any(node["tag"] == "dl" for node in legend))
        terms = [node for node in legend if node["tag"] == "dt"]
        keys = [node["text"].strip() for node in legend if node["tag"] == "kbd"]
        self.assertGreaterEqual(len(terms), 8)
        for key in ("P", "X", "F", "U", "C", "0", "5", "Home", "End", "[", "]", "←", "→"):
            self.assertIn(key, keys)
        for text in ("Previous or next frame in the frame view; at full size the arrows pan instead", "Open the frame view", "Close compare or the frame view", "Full size on or off in the frame view"):
            self.assertTrue(any(node["tag"] == "dd" and node["text"].strip() == text for node in legend), text)
        self.assertIn("Z", keys)
        for key in keys:
            self.assertTrue(len(key) == 1 or key in {"Home", "End", "Page Up", "Page Down", "Enter", "Space", "Esc"}, key)
        for key in ("Enter", "Esc"):
            self.assertIn(key, keys)
        for term in terms:
            self.assertNotRegex(term["text"], r"Cmd|Ctrl|Option|Command|Control|Alt|Shift|[+⌘⌥⌃]")
        self.assertTrue(any(node["tag"] == "dd" and node["text"].strip() for node in legend))

    def test_decision_and_rating_routes_validate_strictly_and_hide_rejected_by_default(self):
        def photo_state():
            code, payload = self.get_json("/api/photos/1")
            self.assertEqual(200, code)
            return payload["photo"]["decision"], payload["photo"]["rating"]

        invalid = [
            ("decision", {}), ("decision", {"decision": "maybe"}), ("decision", {"decision": None}),
            ("decision", {"decision": "FLAG"}), ("decision", {"decision": "flag", "rating": 1}), ("decision", {"rating": 3}),
            ("rating", {}), ("rating", {"rating": True}), ("rating", {"rating": 6}), ("rating", {"rating": -1}),
            ("rating", {"rating": 2.0}), ("rating", {"rating": "3"}), ("rating", {"rating": 3, "decision": "flag"}),
        ]
        for route, payload in invalid:
            with self.subTest(route=route, payload=payload):
                code, body = self.post_json(f"/api/photos/1/{route}", payload)
                self.assertEqual(400, code)
                self.assertIsInstance(body["error"], str)
                self.assertEqual(("none", 0), photo_state())
        for route, payload in (("decision", {"decision": "flag"}), ("rating", {"rating": 2})):
            code, body = self.post_json(f"/api/photos/99999/{route}", payload)
            self.assertEqual((400, {"error": "unknown photo ID: 99999"}), (code, body))
            code, _ = self.post_json(f"/api/photos/1/{route}", payload, headers={"Origin": "http://evil.example"})
            self.assertEqual(403, code)
        self.assertEqual(("none", 0), photo_state())

        with Library(Settings(library_dir=self.root / "library")) as library:
            group_id = library.list_groups()[0]["id"]
        code, body = self.post_json("/api/photos/1/decision", {"decision": "reject"})
        self.assertEqual((200, {"photo_id": 1, "decision": "reject", "rating": 0, "group_id": group_id, "automatic_pick_id": 1}), (code, body))
        code, body = self.post_json("/api/photos/1/rating", {"rating": 3})
        self.assertEqual((200, 3), (code, body["rating"]))
        self.assertEqual(("reject", 3), photo_state())

        code, hidden = self.get_json("/api/groups")
        self.assertEqual((200, 1, 1), (code, hidden["rejected_count"], hidden["photo_count"]))
        self.assertEqual([(group_id, [], 1, 1)], [(group["id"], group["photos"], group["frame_count"], group["rejected_count"])
                                                 for group in hidden["groups"]], "an all-rejected group stays listed with its counts")
        code, shown = self.get_json("/api/groups?show_rejected=1")
        self.assertEqual(200, code)
        photo = shown["groups"][0]["photos"][0]
        self.assertEqual(("reject", 3, 1), (photo["decision"], photo["rating"], shown["rejected_count"]))
        self.assertFalse({"path", "fingerprint", "preview_path"} & photo.keys())
        self.assertNotIn(str(self.root), json.dumps(shown))
        self.post_json("/api/photos/1/decision", {"decision": "none"})
        code, restored = self.get_json("/api/groups")
        self.assertEqual((1, 0, 1, 0), (len(restored["groups"][0]["photos"]), restored["rejected_count"],
                                        restored["groups"][0]["frame_count"], restored["groups"][0]["rejected_count"]))

    def test_groups_report_match_reasons_only_when_text_filter_is_set(self):
        with Library(Settings(library_dir=self.root / "library")) as library:  # setUp already imported photo 1
            library.catalog.save_tags(1, {"description": "d", "subjects": ["ice"], "caption": "Ice floe"}, "m")
        plain = self.get_json("/api/groups")[1]["groups"][0]["photos"][0]
        self.assertEqual([], plain["matched"])
        found = self.get_json("/api/groups?text=ice")[1]["groups"][0]["photos"][0]
        self.assertEqual(["keyword: ice", "caption"], found["matched"])
        self.assertEqual([], self.get_json("/api/groups?text=zodiac")[1]["groups"])

    def test_groups_filters_round_trip_and_reject_invalid_repeated_or_inverted_params_publicly(self):
        with Library(Settings(library_dir=self.root / "library")) as library:
            ids, _ = seed_filter_photos(library.catalog)
            original = library.catalog.list_photos()[0]
        names = {photo_id: name for name, photo_id in ids.items()} | {original.id: "original"}

        def shown(query):
            code, payload = self.get_json("/api/groups" + query)
            self.assertEqual(200, code, payload)
            self.assertNotIn(str(self.root), json.dumps(payload))
            return payload, sorted(names[photo["id"]] for group in payload["groups"] for photo in group["photos"])

        payload, everything = shown("")
        self.assertEqual(["a1", "a2", "b2", "c1", "original"], everything)
        self.assertEqual({"filtered": 5, "total": 7}, payload["counts"])
        self.assertEqual(4, payload["keeper_count"])
        self.assertEqual(sorted({"A", "B", original.camera_id}), payload["cameras"])
        self.assertEqual((7, 2), (payload["photo_count"], payload["rejected_count"]))
        payload, narrowed = shown("?date_from=2026-10-21&date_to=2026-10-22&camera=B&min_rating=1&tagged_only=1&text=Heron")
        self.assertEqual((["b2"], {"filtered": 1, "total": 7}), (narrowed, payload["counts"]))
        self.assertEqual(sorted({"A", "B", original.camera_id}), payload["cameras"], "cameras ignore the filters")
        self.assertEqual([(2, 1)], [(group["frame_count"], group["rejected_count"]) for group in payload["groups"]],
                         "burst counts are unfiltered: removal acts on the whole burst")
        self.assertEqual(4, payload["keeper_count"], "keeper_count is library-wide, as export uses it")
        payload, top_rated = shown("?min_rating=5")
        self.assertEqual([], top_rated)
        self.assertEqual([], payload["groups"], "a burst whose only match (a3) is rejected but has unrejected frames is omitted, "
                                                "never shown as all-rejected")
        self.assertEqual((4, 1), (payload["keeper_count"], payload["rejected_count"]))
        self.assertEqual(["a1", "a2", "c1", "original"], shown("?keepers_only=1")[1])
        self.assertEqual(["a3", "b1"], shown("?decision=reject")[1])
        payload, rejected = shown("?camera=B&show_rejected=1")
        self.assertEqual((["b1", "b2", "c1"], 1), (rejected, payload["rejected_count"]))
        self.assertEqual(["b2", "c1"], shown("?camera=B&show_rejected=0")[1])
        self.assertEqual(shown("")[1], shown("?text=&camera=")[1], "blank values are ignored")

        invalid = {
            "?camera=A&camera=B": "camera", "?decision=reject&decision=flag": "decision",
            "?date_from=2026-10-22&date_to=2026-10-21": "date_to", "?date_from=2026-13-01": "date_from",
            "?date_to=20261021": "date_to", "?min_rating=two": "min_rating", "?min_rating=6": "min_rating",
            "?min_rating=2.0": "min_rating", "?decision=maybe": "decision", "?decision=FLAG": "decision",
            "?keepers_only=true": "keepers_only", "?tagged_only=2": "tagged_only", "?export_failed_only=yes": "export_failed_only", "?show_rejected=yes": "show_rejected",
            "?bogus=1": "bogus", f"?{self.root}=1": "unknown",
        }
        for query, name in invalid.items():
            with self.subTest(query=query):
                code, payload = self.get_json("/api/groups" + query)
                self.assertEqual((400, {"error": f"invalid filter: {name}"}), (code, payload))

    def test_filter_form_and_day_headings_are_native_and_labeled(self):
        html = urllib.request.urlopen(self.base + "/", timeout=3).read().decode()
        parser = LandmarkParser()
        parser.feed(html)
        by_id = {node["attrs"]["id"]: node for node in parser.nodes if "id" in node["attrs"]}
        labels = {node["attrs"].get("for"): node["text"].strip() for node in parser.nodes if node["tag"] == "label"}
        form = by_id["filter-form"]
        self.assertEqual("form", form["tag"])
        controls = {"filter-from": ("input", "date"), "filter-to": ("input", "date"), "filter-camera": ("select", None),
                    "filter-rating": ("select", None), "filter-tagged": ("input", "checkbox"), "filter-text": ("input", "search"),
                    **{f"status-{value}": ("input", "radio") for value in ("all", "keepers", "flag", "none", "reject")}}
        for name, (tag, kind) in controls.items():
            with self.subTest(control=name):
                node = by_id[name]
                self.assertEqual((tag, kind), (node["tag"], node["attrs"].get("type")))
                self.assertIn(form, node["parents"])
                self.assertTrue(labels.get(name), f"{name} needs a visible label")
        options = lambda name: [node["attrs"]["value"] for node in parser.nodes if node["tag"] == "option" and by_id[name] in node["parents"]]
        self.assertEqual("fieldset", by_id["filter-status"]["tag"])
        self.assertTrue(any(node["tag"] == "legend" and node["text"].strip() == "Show" for node in parser.nodes if by_id["filter-status"] in node["parents"]))
        self.assertEqual(["0", "1", "2", "3", "4", "5"], options("filter-rating"))
        buttons = {node["text"].strip(): node["attrs"].get("type") for node in parser.nodes if node["tag"] == "button" and form in node["parents"]}
        self.assertEqual({"Reset": "button", "Expand search": "button"}, buttons)
        self.assertEqual("ai-availability", by_id["expand-search"]["attrs"].get("aria-describedby"))
        more = by_id["show-more"]
        self.assertEqual(("button", "button"), (more["tag"], more["attrs"].get("type")))
        self.assertIn("hidden", more["attrs"])
        self.assertNotIn(by_id["contact-sheet"], more["parents"])
        javascript = (Path(__file__).resolve().parents[1] / "src" / "lumen" / "static" / "app.js").read_text()
        for text in ("createElement('details')", "createElement('summary')", "details.open = dayIsOpen(", "history.replaceState", "location.search",
                     "loading = 'lazy'", "Showing ${"):
            self.assertIn(text, javascript)
        styles = (Path(__file__).resolve().parents[1] / "src" / "lumen" / "static" / "styles.css").read_text()
        self.assertRegex(styles, r"\.day summary \{[^}]*min-height: 44px")
        self.assertRegex(styles, r"summary:focus-visible")

    def test_export_panel_states_keeper_definition_and_inspector_has_native_decision_controls(self):
        html = urllib.request.urlopen(self.base + "/", timeout=3).read().decode()
        parser = LandmarkParser()
        parser.feed(html)
        by_id = {node["attrs"]["id"]: node for node in parser.nodes if "id" in node["attrs"]}
        copy = ("Keepers are frames that are not rejected and are either flagged or best in their burst. "
                "A single frame is best in its own burst, so it stays a keeper unless you reject it.")
        self.assertIn(copy, " ".join(by_id["export-panel"]["text"].split()))
        toggle = by_id["status-reject"]
        self.assertEqual(("input", "radio"), (toggle["tag"], toggle["attrs"].get("type")))
        self.assertTrue(any(node["tag"] == "label" and "Rejected" in node["text"] for node in toggle["parents"]))
        javascript = (Path(__file__).resolve().parents[1] / "src" / "lumen" / "static" / "app.js").read_text()
        for text in ("'Flag'", "'Reject'", "'Clear decision'", "'Flagged'", "'Rejected'", "`Rating ${", "createElement('select')",
                     "id = 'rating'", "htmlFor = 'rating'", "`Rating ${result.rating} set for ${"):
            self.assertIn(text, javascript)
        self.assertNotRegex(javascript, r"decision[^\n]*\.innerHTML")
        styles = (Path(__file__).resolve().parents[1] / "src" / "lumen" / "static" / "styles.css").read_text()
        self.assertRegex(styles, r"button, input, select \{[^}]*min-height: 44px")
        self.assertRegex(styles, r"select:focus-visible")

    def test_javascript_astro_tab_confirms_includes_polls_and_retains_failure_context(self):
        for scenario in ("astro", "astro-fail", "astro-cancel", "astro-resume", "astro-recipe"):
            with self.subTest(scenario=scenario):
                self.run_javascript_cleanup_regression(scenario)

    def test_javascript_keepers_export_runs_as_a_job_with_progress_dialog_and_status_bar(self):
        for scenario in ("export", "export-cancel", "export-watermark", "export-bad-preset", "export-filtered"):
            with self.subTest(scenario=scenario):
                self.run_javascript_cleanup_regression(scenario)

    def test_javascript_clear_tab_counts_and_removes_previously_cleared_frames(self):
        self.run_javascript_cleanup_regression("remove-cleared")

    def test_javascript_batch_rail_merges_selected_batches(self):
        self.run_javascript_cleanup_regression("merge-batches")

    def test_javascript_rail_and_heading_controls_appear_only_when_they_apply(self):
        self.run_javascript_cleanup_regression("rail-visibility")

    def test_refine_previews_fold_under_the_filter_bar(self):
        html = urllib.request.urlopen(self.base + "/", timeout=3).read().decode()
        parser = LandmarkParser()
        parser.feed(html)
        by_id = {node["attrs"]["id"]: node for node in parser.nodes if "id" in node["attrs"]}
        fold = by_id["refine-fold"]
        self.assertEqual("details", fold["tag"])
        summary = next(node for node in parser.nodes if node["tag"] == "summary" and node["parents"][-1] is fold)
        self.assertEqual("Refine previews", summary["text"].strip())
        top = next(node for node in parser.nodes if "contact-top" in node["attrs"].get("class", "").split())
        self.assertIs(top, fold["parents"][-1])
        self.assertIs(top, by_id["filter-form"]["parents"][-1])
        order = [node["attrs"].get("id") for node in parser.nodes if node["parents"] and node["parents"][-1] is top]
        self.assertEqual(order.index("filter-form") + 1, order.index("refine-fold"), "the fold sits directly after the filter bar")
        for name in ("refine-scope", "refine-previews", "refine-availability", "refine-status"):
            self.assertIn(fold, by_id[name]["parents"], name)
        view = next(node for node in parser.nodes if node["attrs"].get("class") == "view-controls")
        self.assertEqual(["tile-size"], [node["attrs"]["id"] for node in parser.nodes if view in node["parents"] and node["tag"] in ("select", "button")])
        styles = (Path(__file__).resolve().parents[1] / "src" / "lumen" / "static" / "styles.css").read_text()
        self.assertRegex(styles, r"#refine-fold summary[^{]*\{[^}]*min-height: 44px")
        self.assertRegex(styles, r"\.rail-header button \{ width: 100%; \}")

    def test_javascript_cleanup_selection_follows_photo_after_group_refresh(self):
        for scenario in ("split", "merge", "missing"):
            with self.subTest(scenario=scenario):
                self.run_javascript_cleanup_regression(scenario)

    def assert_workspace_count_includes_failed_import(self, retain_success):
        if not retain_success:
            self.assertEqual(200, self.post_json("/api/library/clear", {"confirmation": "CLEAR"})[0])
        failed = self.root / "analysis-failed.jpg"
        Image.new("RGB", (40, 30), "red").save(failed)
        with patch("lumen.library.measure_image", side_effect=ValueError("analysis failed after upsert")):
            code, result = self.post_json("/api/import", {"path": str(failed)})
            self.assertEqual(202, code)
            result = self.wait_job(result["job_id"])
        self.assertEqual(1, result["result"]["failed"])
        with Library(Settings(library_dir=self.root / "library")) as library:
            ungrouped = library.catalog.photo_by_path(str(failed.resolve()))
            self.assertIsNotNone(ungrouped)
            self.assertIsNone(ungrouped.group_id)
            self.assertTrue(Path(ungrouped.preview_path).is_file())
        with urllib.request.urlopen(self.base + "/api/groups", timeout=3) as response:
            payload = json.load(response)
        self.assertEqual(2 if retain_success else 1, payload.get("photo_count"))
        self.assertEqual(1 if retain_success else 0, sum(len(group["photos"]) for group in payload["groups"]))
        self.run_javascript_cleanup_regression("workspace-mixed" if retain_success else "workspace-ungrouped")
        with urllib.request.urlopen(self.base + "/api/groups", timeout=3) as response:
            self.assertEqual({"groups": [], "photo_count": 0, "rejected_count": 0, "counts": {"filtered": 0, "total": 0}, "cameras": [], "keeper_count": 0}, json.load(response))
        with Library(Settings(library_dir=self.root / "library")) as library:
            self.assertEqual([], library.catalog.list_photos())

    def test_workspace_clear_counts_successful_and_ungrouped_imports(self):
        self.assert_workspace_count_includes_failed_import(retain_success=True)

    def test_workspace_clear_is_enabled_for_only_ungrouped_imports(self):
        self.assert_workspace_count_includes_failed_import(retain_success=False)

    def test_xmp_browser_only_returns_existing_xmp_and_handles_cancel(self):
        profile = self.root / "look.XMP"
        profile.write_text("<xmp/>")
        with patch("lumen.web.sys.platform", "darwin"), patch("lumen.web.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, str(profile) + "\n", "")
            code, payload = self.post_json("/api/browse-xmp", {})
            self.assertEqual(200, code, payload)
            self.assertEqual({"path": str(profile.resolve()), "cancelled": False}, payload)
            self.assertEqual("/usr/bin/osascript", run.call_args.args[0][0])
            for selected in (self.root / "photo.jpg", self.root / "missing.xmp", self.root):
                run.return_value = subprocess.CompletedProcess([], 0, str(selected), "")
                code, payload = self.post_json("/api/browse-xmp", {})
                self.assertEqual(400, code, payload)
                self.assertIn("existing .xmp", payload["error"])
            run.return_value = subprocess.CompletedProcess([], 1, "", "User canceled. (-128)")
            self.assertEqual((200, {"path": None, "cancelled": True}), self.post_json("/api/browse-xmp", {}))

    def test_xmp_browser_reports_tool_failure_and_rejects_client_paths(self):
        with patch("lumen.web.sys.platform", "darwin"), patch("lumen.web.subprocess.run") as run:
            code, payload = self.post_json("/api/browse-xmp", {"path": "/tmp/look.xmp"})
            self.assertEqual(400, code)
            run.assert_not_called()
            run.side_effect = subprocess.TimeoutExpired("osascript", 300)
            code, payload = self.post_json("/api/browse-xmp", {})
            self.assertEqual(400, code)
            self.assertIn("timed out", payload["error"])
            run.side_effect = None
            run.return_value = subprocess.CompletedProcess([], 1, "", "picker denied")
            code, payload = self.post_json("/api/browse-xmp", {})
            self.assertEqual(400, code)
            self.assertIn("picker denied", payload["error"])

    def test_reveal_only_opens_managed_jpeg_directory_with_argv(self):
        destination = self.root / "library" / "outputs" / "jpeg"
        with patch("lumen.web.sys.platform", "darwin"), patch("lumen.web.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            code, payload = self.post_json("/api/outputs/reveal", {})
            self.assertEqual(200, code, payload)
            self.assertEqual(str(destination.resolve()), payload["path"])
            self.assertTrue(destination.is_dir())
            self.assertEqual(["/usr/bin/open", str(destination.resolve())], run.call_args.args[0])
            self.assertNotIn("shell", run.call_args.kwargs)
            run.reset_mock()
            code, payload = self.post_json("/api/outputs/reveal", {"path": str(self.root)})
            self.assertEqual(400, code)
            run.assert_not_called()

    def test_reveal_rejects_escaped_directory_before_mac_tool(self):
        destination = self.root / "library" / "outputs" / "jpeg"
        destination.symlink_to(self.root, target_is_directory=True)
        with patch("lumen.web.sys.platform", "darwin"), patch("lumen.web.subprocess.run") as run:
            code, payload = self.post_json("/api/outputs/reveal", {})
            self.assertEqual(400, code)
            self.assertIn("managed outputs", payload["error"])
            run.assert_not_called()

    def test_reveal_reports_tool_failure_as_json(self):
        with patch("lumen.web.sys.platform", "darwin"), patch("lumen.web.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 1, "", "Finder unavailable")
            code, payload = self.post_json("/api/outputs/reveal", {})
            self.assertEqual(400, code)
            self.assertIn("Finder unavailable", payload["error"])

    def test_status_exposes_configured_jpeg_destination(self):
        with patch("lumen.web.doctor_report", return_value={}):
            with urllib.request.urlopen(self.base + "/api/status", timeout=3) as response:
                payload = json.load(response)
        self.assertEqual(str((self.root / "library" / "outputs" / "jpeg").resolve()), payload.get("jpeg_output_dir"))
        self.assertEqual(str((self.root / "library" / "outputs" / "astro").resolve()), payload.get("astro_output_dir"))

    def test_astro_panel_has_explicit_confirmation_and_labeled_controls(self):
        html = urllib.request.urlopen(self.base + "/", timeout=3).read().decode()
        parser = LandmarkParser()
        parser.feed(html)
        by_id = {node["attrs"]["id"]: node for node in parser.nodes if "id" in node["attrs"]}
        panel = by_id["astro-panel"]
        confirm = by_id["astro-confirm"]
        self.assertEqual(("input", "checkbox"), (confirm["tag"], confirm["attrs"].get("type")))
        self.assertIn(panel, confirm["parents"])
        self.assertTrue(any(node["tag"] == "label" and node["attrs"].get("for") == "astro-confirm" and "confirm" in node["text"].lower() for node in parser.nodes))
        self.assertEqual("fieldset", by_id["astro-lights-group"]["tag"])
        radios = [node for node in parser.nodes if node["attrs"].get("name") == "astro-recipe"]
        self.assertEqual([("radio", "tracked"), ("radio", "untracked")], [(node["attrs"].get("type"), node["attrs"].get("value")) for node in radios])
        self.assertTrue(all(by_id["astro-recipe"] in node["parents"] for node in radios))
        self.assertIn("checked", radios[0]["attrs"])
        self.assertIn("legend", [child["tag"] for child in parser.nodes if by_id["astro-lights-group"] in child["parents"]])
        for kind in ("darks", "flats", "biases"):
            browse = by_id[f"browse-astro-{kind}"]
            self.assertEqual(("button", "button"), (browse["tag"], browse["attrs"].get("type")))
            self.assertIn(kind[:-1], browse["text"].lower())
            self.assertEqual("path", by_id[f"astro-{kind}-path"]["attrs"].get("class"))
            self.assertTrue(by_id[f"astro-{kind}-path"]["attrs"].get("aria-labelledby"))
            row = browse["parents"][-1]
            self.assertEqual(("div", "calibration-row"), (row["tag"], row["attrs"].get("class")), kind)
            children = [node["attrs"].get("id") for node in parser.nodes if node["parents"] and node["parents"][-1] is row]
            self.assertEqual([f"astro-{kind}-label", f"astro-{kind}-path", f"browse-astro-{kind}", f"reset-astro-{kind}"], children)
        self.assertEqual(3, sum(node["attrs"].get("class") == "calibration-row" for node in parser.nodes))
        for name in ("prepare-astro", "run-astro", "cancel-astro", "reveal-astro"):
            self.assertEqual("button", by_id[name]["tag"])
            self.assertIn(panel, by_id[name]["parents"])
        self.assertEqual("progress", by_id["astro-progress"]["tag"])
        self.assertTrue(by_id["astro-progress"]["attrs"].get("aria-labelledby"))
        self.assertTrue(by_id["astro-warnings"]["attrs"].get("aria-label"))
        self.assertIn("originals", panel["text"].lower())
        javascript = (Path(__file__).resolve().parents[1] / "src" / "lumen" / "static" / "app.js").read_text()
        self.assertIn("Warning: ", javascript)  # non-colour warning cue
        self.assertIn("clearInterval", javascript)  # polling stops on terminal states
        self.assertNotRegex(javascript, r"astro[^\n]*\.innerHTML")

    def test_javascript_keep_only_picks_the_frame_and_rejects_the_rest_of_its_burst(self):
        self.run_javascript_cleanup_regression("keep-only")

    def test_javascript_tile_size_control_sets_the_grid_and_is_remembered(self):
        self.run_javascript_cleanup_regression("tiles")
        self.run_javascript_cleanup_regression("tiles-remembered")

    def test_javascript_rail_expands_only_the_selected_day_and_hand_opened_days(self):
        self.run_javascript_cleanup_regression("rail-days")

    def test_sheet_has_tile_size_control_and_keep_only_shortcut(self):
        html = urllib.request.urlopen(self.base + "/", timeout=3).read().decode()
        parser = LandmarkParser()
        parser.feed(html)
        by_id = {node["attrs"]["id"]: node for node in parser.nodes if "id" in node["attrs"]}
        self.assertEqual("select", by_id["tile-size"]["tag"])
        self.assertTrue(any(node["tag"] == "label" and node["attrs"].get("for") == "tile-size" for node in parser.nodes))
        self.assertIn("<kbd>R</kbd>", html)
        self.assertIn("reject the rest", html.lower())
        styles = (Path(__file__).resolve().parents[1] / "src" / "lumen" / "static" / "styles.css").read_text()
        self.assertRegex(styles, r"\.burst-frames \{[^}]*minmax\(var\(--tile-min, 180px\), 1fr\)")
        self.assertRegex(styles, r"\.frame img \{[^}]*object-fit: contain")  # portrait frames are shown whole

    def test_javascript_page_still_renders_when_server_status_predates_the_page(self):
        self.run_javascript_cleanup_regression("status-skew")

    def test_javascript_status_bar_leads_with_a_stale_server(self):
        self.run_javascript_cleanup_regression("stale-server")

    def test_javascript_ai_status_bar_reports_running_done_and_failed(self):
        self.run_javascript_cleanup_regression("ai-status")

    def test_ai_status_bar_is_a_footer_live_region(self):
        html = urllib.request.urlopen(self.base + "/", timeout=3).read().decode()
        parser = LandmarkParser()
        parser.feed(html)
        by_id = {node["attrs"]["id"]: node for node in parser.nodes if "id" in node["attrs"]}
        status = by_id["ai-status"]
        self.assertEqual(("status", "polite"), (status["attrs"].get("role"), status["attrs"].get("aria-live")))
        self.assertTrue(any(node["tag"] == "footer" for node in status["parents"]), "the AI status line sits in a footer at the bottom")
        styles = (Path(__file__).resolve().parents[1] / "src" / "lumen" / "static" / "styles.css").read_text()
        self.assertRegex(styles, r'#ai-status\[data-phase="failed"\] \{[^}]*var\(--danger\)')

    def test_javascript_assist_button_runs_one_job_over_keepers(self):
        self.run_javascript_cleanup_regression("assist")

    def test_javascript_import_is_a_source_beside_ingest(self):
        self.run_javascript_cleanup_regression("import-tab")

    def test_javascript_import_sends_include_cleared_only_when_ticked(self):
        self.run_javascript_cleanup_regression("import-cleared")

    def test_add_photos_tab_and_assist_tab_own_the_import_and_tag_controls(self):
        html = urllib.request.urlopen(self.base + "/", timeout=3).read().decode()
        parser = LandmarkParser()
        parser.feed(html)
        by_id = {node["attrs"]["id"]: node for node in parser.nodes if "id" in node["attrs"]}
        panel = by_id["import-panel"]
        self.assertIn(by_id["add-panel"], panel["parents"], "Import is one source in the Add photos tab")
        self.assertIn(panel, by_id["import-form"]["parents"], "the import form lives in the Import section")
        self.assertNotIn("import-dialog", by_id)
        self.assertNotIn("open-import", by_id, "the Import… button gave way to the tab")
        fold = next(node for node in parser.nodes if node["tag"] == "details" and node["parents"][-1] is panel)
        self.assertEqual("how", fold["attrs"].get("class"), "the explanation folds under How this works")
        for text in ("Nothing is copied, moved, or renamed", "Use Ingest first"):
            self.assertIn(text, " ".join(fold["text"].split()))
        for text in ('<label class="toggle" for="import-include-cleared"><input type="checkbox" id="import-include-cleared"> Include frames cleared in earlier sessions</label>',
                     "Frames you removed or cleared before are skipped unless ticked."):
            self.assertIn(text, html)
        for name in ("tag-all", "retag-all"):
            self.assertIn(by_id["assist-panel"], by_id[name]["parents"], f"{name} moved into the Assist tab")

    def test_javascript_network_failure_names_the_server_not_the_browser(self):
        self.run_javascript_cleanup_regression("network-error")

    def test_javascript_inspector_shows_gps_location_with_a_maps_link(self):
        self.run_javascript_cleanup_regression("location")

    def test_javascript_inspector_shows_the_place_before_the_coordinates(self):
        self.run_javascript_cleanup_regression("place-line")

    def test_javascript_place_editor_posts_burst_scope_and_disables_remember_without_a_fix(self):
        self.run_javascript_cleanup_regression("place-edit")

    def test_exif_route_returns_labelled_fields_and_json_errors(self):
        with patch("lumen.web.read_exif_summary", return_value=[("Camera", "ILCE-7CM2"), ("ISO", "100")]) as read:
            code, body = self.get_json("/api/photos/1/exif")
        self.assertEqual((200, {"photo_id": 1, "fields": [{"label": "Camera", "value": "ILCE-7CM2"}, {"label": "ISO", "value": "100"}]}), (code, body))
        self.assertTrue(read.call_args.args[0].is_file(), "it reads the original, not the preview")
        with patch("lumen.web.read_exif_summary", side_effect=PreviewError("exiftool is required to read EXIF")):
            code, body = self.get_json("/api/photos/1/exif")
        self.assertEqual((503, {"error": "exiftool is required to read EXIF"}), (code, body))
        code, body = self.get_json("/api/photos/999/exif")
        self.assertEqual((404, "unknown photo"), (code, body.get("error")))

    def test_photo_detail_carries_place_and_country(self):
        from lumen.catalog import Catalog
        with Catalog(Settings(library_dir=self.root / "library").database_path) as catalog:
            catalog.update_place(1, place="Madikwe Game Reserve", country="South Africa")
        photo = self.get_json("/api/photos/1")[1]["photo"]
        self.assertEqual(("Madikwe Game Reserve", "South Africa"), (photo["place"], photo["place_country"]))

    def test_place_route_validates_and_sets_place(self):
        valid = {"name": "Our camp", "country": "South Africa", "scope": "frame", "remember": False, "radius_km": 5}
        for payload in ({}, {k: v for k, v in valid.items() if k != "remember"},
                        {k: v for k, v in valid.items() if k != "radius_km"}, valid | {"scope": "all"},
                        valid | {"name": ""}, valid | {"name": "x" * 81}, valid | {"remember": "yes"},
                        valid | {"extra": 1}, valid | {"country": None}, valid | {"radius_km": "5"},
                        valid | {"radius_km": True}, valid | {"radius_km": 0.9}, valid | {"radius_km": 21}):
            with self.subTest(payload=payload):
                self.assertEqual(400, self.post_json("/api/photos/1/place", payload)[0])
        self.assertEqual(400, self.post_json("/api/photos/999/place", valid)[0])
        self.assertEqual((200, {"photo_ids": [1], "name": "Our camp", "country": "South Africa", "remembered": False}),
                         self.post_json("/api/photos/1/place", valid))
        photo = self.get_json("/api/photos/1")[1]["photo"]
        self.assertEqual(("Our camp", "South Africa"), (photo["place"], photo["place_country"]))

    def test_location_route_validates_and_fills_a_frame_without_a_fix(self):
        valid = {"position": "33°55.2'S 18°25.2'E", "scope": "frame"}
        for payload in ({}, {"position": "-33.9, 18.4"}, valid | {"scope": "nearby"}, valid | {"position": 5},
                        valid | {"position": "nowhere"}, valid | {"position": "x" * 81}, valid | {"extra": 1}):
            with self.subTest(payload=payload):
                self.assertEqual(400, self.post_json("/api/photos/1/location", payload)[0])
        self.assertEqual(400, self.post_json("/api/photos/999/location", valid)[0])
        code, body = self.post_json("/api/photos/1/location", valid)
        self.assertEqual(200, code, body)
        self.assertEqual({"photo_ids", "latitude", "longitude", "place", "country", "location_typed"}, set(body))
        self.assertEqual([1], body["photo_ids"])
        photo = self.get_json("/api/photos/1")[1]["photo"]
        self.assertAlmostEqual(-33.92, photo["latitude"])
        self.assertTrue(photo["location_typed"])
        code, body = self.post_json("/api/photos/1/location", valid | {"position": "-1, 1"})
        self.assertEqual((200, -1.0), (code, body["latitude"]), "a typed position can be corrected")

    def test_javascript_contact_sheet_groups_frames_into_burst_sections(self):
        self.run_javascript_cleanup_regression("bursts")

    def test_javascript_arrow_up_down_move_one_grid_row(self):
        self.run_javascript_cleanup_regression("rows")

    def test_javascript_choosing_a_tab_scrolls_its_panel_into_view(self):
        self.run_javascript_cleanup_regression("tabs")
        self.run_javascript_cleanup_regression("tabs-remembered")

    def test_javascript_default_tab_follows_the_catalog_on_a_fresh_visit(self):
        self.run_javascript_cleanup_regression("default-tab")
        self.run_javascript_cleanup_regression("default-tab-frames")

    def test_frames_tab_row_and_caption_inputs_fit_the_sticky_layout(self):
        styles = (Path(__file__).resolve().parents[1] / "src" / "lumen" / "static" / "styles.css").read_text()
        self.assertRegex(styles, r"\n\.frame \{[^}]*scroll-margin-top: var\(--sticky-top")  # focus never lands under the sticky header
        self.assertRegex(styles, r"\.tab-row \{[^}]*flex-wrap: wrap")  # the collapse button wraps instead of clipping
        self.assertRegex(styles, r"#caption-controls label, #caption-controls input \{[^}]*display: block")
        self.assertRegex(styles, r"\.crop-grid \{[^}]*repeat\(auto-fill, minmax\(220px, 360px\)\)")  # one card never fills the panel
        html = urllib.request.urlopen(self.base + "/", timeout=3).read().decode()
        parser = LandmarkParser()
        parser.feed(html)
        by_id = {node["attrs"]["id"]: node for node in parser.nodes if "id" in node["attrs"]}
        sticky = next(node for node in parser.nodes if node["attrs"].get("class") == "main-top")
        self.assertIn(sticky, by_id["export-tab"]["parents"], "the tab row stays pinned")
        for name in ("import-form", "tag-all"):
            self.assertNotIn(sticky, by_id[name]["parents"], f"{name} scrolls away with the page")

    def test_layout_is_full_height_with_collapse_toggle_burst_sections_and_rail_merge_controls(self):
        html = urllib.request.urlopen(self.base + "/", timeout=3).read().decode()
        parser = LandmarkParser()
        parser.feed(html)
        by_id = {node["attrs"]["id"]: node for node in parser.nodes if "id" in node["attrs"]}
        self.assertNotIn("collapse-panel", by_id, "clicking the open tab folds the panel; no separate button")
        self.assertEqual("tabpanel", by_id["export-panel"]["attrs"]["role"])
        self.assertIn(by_id["workflow-panels"], by_id["export-panel"]["parents"])
        nav = next(node for node in parser.nodes if node["tag"] == "nav")
        self.assertNotIn("aside", parser.tags, "the inspector rail is gone; the frame view holds the inspector")
        header = by_id["rail-header"]
        self.assertIn(nav, header["parents"])
        for name in ("merge-batches", "merge-count", "jump-selected", "back-to-top"):
            self.assertIn(header, by_id[name]["parents"], f"{name} lives in the sticky rail header")
        for name in ("merge-batches", "jump-selected", "back-to-top"):
            node = by_id[name]
            self.assertEqual(("button", "button"), (node["tag"], node["attrs"].get("type")))
            self.assertTrue(node["text"].strip() or node["attrs"].get("aria-label"), f"{name} needs an accessible name")
        styles = (Path(__file__).resolve().parents[1] / "src" / "lumen" / "static" / "styles.css").read_text()
        self.assertRegex(styles, r"\nbody \{[^}]*overflow: hidden")
        self.assertRegex(styles, r"\.masthead \{[^}]*position: sticky")
        self.assertRegex(styles, r"\.batch-rail, main \{[^}]*overflow-y: auto")
        self.assertRegex(styles, r"\.workspace \{[^}]*grid-template-columns: 250px minmax\(0, 1fr\);")  # the sheet takes the rail's width
        self.assertRegex(styles, r"\.main-top \{[^}]*position: sticky")
        self.assertRegex(styles, r"\.rail-header \{[^}]*position: sticky")
        self.assertRegex(styles, r'\.burst\[data-selected="true"\] \{[^}]*outline: 2px solid var\(--amber\)')
        self.assertRegex(styles, r"\.batch-thumb \{[^}]*width: 44px")
        self.assertIn("prefers-reduced-motion", styles)
        javascript = (Path(__file__).resolve().parents[1] / "src" / "lumen" / "static" / "app.js").read_text()
        for text in ("createElement('section')", "className = 'burst'", "'aria-labelledby'", "block: 'start'", "prefers-reduced-motion",
                     "lumen-panel-collapsed", "'Selected'", "'Select for merge'", "toLocaleTimeString", "toLocaleString"):
            self.assertIn(text, javascript)
        self.assertNotRegex(javascript, r"burst[^\n]*\.innerHTML")

    def test_index_has_semantic_landmarks_and_labeled_import_control(self):
        html = urllib.request.urlopen(self.base + "/", timeout=3).read().decode()
        parser = LandmarkParser()
        parser.feed(html)
        self.assertIn("main", parser.tags)
        self.assertIn("nav", parser.tags)
        self.assertGreaterEqual(parser.labels, 1)

    def test_groups_api_returns_ranked_photo_with_preview_url(self):
        payload = json.load(urllib.request.urlopen(self.base + "/api/groups", timeout=3))
        self.assertEqual(1, len(payload["groups"]))
        photo = payload["groups"][0]["photos"][0]
        self.assertTrue(photo["preview_url"].startswith("/preview/"))
        self.assertIsInstance(photo["total_score"], float)

    def test_thumb_route_serves_a_small_cached_jpeg_inside_the_library(self):
        source = self.root / "big.jpg"
        Image.new("RGB", (1600, 1200), (30, 60, 90)).save(source)
        with Library(Settings(library_dir=self.root / "library")) as library:
            library.import_path(source)
        photo_id = max(photo["id"] for group in json.load(urllib.request.urlopen(self.base + "/api/groups", timeout=3))["groups"] for photo in group["photos"])
        with urllib.request.urlopen(f"{self.base}/thumb/{photo_id}?v=abc", timeout=3) as response:
            self.assertEqual("image/jpeg", response.headers["Content-Type"])
            self.assertIn("max-age", response.headers["Cache-Control"])
            thumb = Image.open(io.BytesIO(response.read()))
        self.assertEqual((480, 360), thumb.size)
        cached = list((self.root / "library" / "cache" / "thumbs").glob("*.jpg"))
        self.assertEqual(1, len(cached), "the thumbnail is written once below the library cache")
        with urllib.request.urlopen(f"{self.base}/thumb/{photo_id}", timeout=3) as response:
            response.read()
        self.assertEqual(cached, list((self.root / "library" / "cache" / "thumbs").glob("*.jpg")))
        for bad in ("/thumb/999", "/thumb/../catalog.sqlite3"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(self.base + bad, timeout=3)
            self.assertEqual(404, caught.exception.code)
            caught.exception.close()

    def test_full_route_serves_cached_embedded_jpeg_or_404(self):
        buffer = BytesIO()
        Image.new("RGB", (640, 427), "blue").save(buffer, "JPEG")

        def run(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, buffer.getvalue() if "-JpgFromRaw" in command else b"", b"")

        def status(path):
            try:
                with urllib.request.urlopen(self.base + path, timeout=3) as response:
                    return response.code, response.headers["Content-Type"], response.read()
            except urllib.error.HTTPError as error:
                with error:
                    return error.code, error.headers["Content-Type"], error.read()

        gone = self.root / "gone.jpg"
        Image.new("RGB", (60, 40), "red").save(gone)
        with Library(Settings(library_dir=self.root / "library")) as library:
            library.import_path(gone)
            gone_photo = max(library.catalog.list_photos(), key=lambda photo: photo.id)
            gone_id = gone_photo.id
        gone.unlink()
        with patch("lumen.previews.shutil.which", return_value=None):
            self.assertEqual(404, status("/full/1?v=x")[0])
        with patch("lumen.previews.shutil.which", return_value="exiftool"), \
                patch("lumen.previews.subprocess.run", side_effect=run):
            code, kind, body = status("/full/1?v=x")
            self.assertEqual((200, "image/jpeg"), (code, kind))
            self.assertEqual((640, 427), Image.open(BytesIO(body)).size)
            code, _, body = status(f"/full/{gone_id}")
            self.assertEqual(503, code, "an unplugged original with nothing cached is offline, not missing")
            self.assertNotIn(str(self.root).encode(), body)
            for bad in ("/full/999", "/full/../catalog.sqlite3"):
                code, _, body = status(bad)
                self.assertEqual(404, code, bad)
                self.assertNotIn(str(self.root).encode(), body)
                self.assertNotIn(b"Traceback", body)
            (self.root / "library" / "cache" / "full" / f"{gone_photo.fingerprint}.jpg").write_bytes(buffer.getvalue())
            self.assertEqual(200, status(f"/full/{gone_id}")[0], "a cached copy serves while the original is unplugged")

    def test_preview_route_rejects_traversal(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.base + "/preview/../catalog.sqlite3", timeout=3)
        self.assertEqual(404, caught.exception.code)
        caught.exception.close()

    def test_post_rejects_cross_origin_browser_request(self):
        request = urllib.request.Request(
            self.base + "/api/import",
            data=json.dumps({"path": str(self.root / "photo.jpg")}).encode(),
            headers={"Content-Type": "application/json", "Origin": "https://attacker.example"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(403, caught.exception.code)
        caught.exception.close()

    def test_browse_api_returns_selected_folder(self):
        chosen = str(self.root / "camera imports")
        request = urllib.request.Request(
            self.base + "/api/browse",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with patch("lumen.web.choose_macos_folder", return_value=chosen):
            payload = json.load(urllib.request.urlopen(request, timeout=3))

        self.assertEqual({"path": chosen, "cancelled": False}, payload)

    def test_browse_api_reports_cancel_without_error(self):
        request = urllib.request.Request(
            self.base + "/api/browse",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with patch("lumen.web.choose_macos_folder", return_value=None):
            payload = json.load(urllib.request.urlopen(request, timeout=3))

        self.assertEqual({"path": None, "cancelled": True}, payload)

    def test_get_rejects_non_loopback_host(self):
        for path in ("/api/groups", "/api/photos/1/crops"):
            with self.subTest(path=path):
                request = urllib.request.Request(
                    self.base + path,
                    headers={"Host": f"attacker.example:{self.server.server_port}"},
                )
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=3)
                self.assertEqual(403, caught.exception.code)
                caught.exception.close()

    def test_preview_url_changes_when_source_content_changes(self):
        first = json.load(urllib.request.urlopen(self.base + "/api/groups", timeout=3))["groups"][0]["photos"][0]
        source = self.root / "photo.jpg"
        Image.new("RGB", (120, 90), (5, 10, 15)).save(source)
        request = urllib.request.Request(
            self.base + "/api/import",
            data=json.dumps({"path": str(source)}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            self.wait_job(json.load(response)["job_id"])
        second = json.load(urllib.request.urlopen(self.base + "/api/groups", timeout=3))["groups"][0]["photos"][0]

        self.assertEqual(first["id"], second["id"])
        self.assertNotEqual(first["preview_url"], second["preview_url"])


if __name__ == "__main__":
    unittest.main()


class AstroWebTests(unittest.TestCase):
    def setUp(self):
        import sys
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.night = self.root / "Night ✨"
        self.night.mkdir()
        for index in range(3):
            exif = Image.Exif()
            exif[34665] = {36867: f"2026:10:21 22:00:0{index}"}
            Image.new("RGB", (64, 48), (index, 0, 0)).save(self.night / f"light {index} é.tif", exif=exif.tobytes())
        self.settings = Settings(library_dir=self.root / "Lumen Library ✨")
        with Library(self.settings) as library:
            library.import_path(self.night)
            self.group = library.list_groups()[0]
        self.photo_ids = [photo["id"] for photo in self.group["photos"]]
        self.scenario = "success"
        real_popen = subprocess.Popen
        for target, kwargs in (
            ("lumen.library.find_tool", {"return_value": SIRIL.executable}),
            ("lumen.library.probe_siril", {"return_value": SIRIL}),
            ("lumen.astro.subprocess.Popen", {"side_effect": lambda command, **kw: real_popen(
                [sys.executable, "-c", FAKE_SIRIL, self.scenario], **kw)}),
        ):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = make_server(self.settings, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    post_json = WebTests.post_json

    def get_json(self, path):
        try:
            response = urllib.request.urlopen(self.base + path, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            self.assertIn("application/json", response.headers.get("Content-Type", ""))
            return response.code, json.load(response)

    def prepare(self, **extra):
        return self.post_json("/api/astro/jobs", {"group_id": self.group["id"], "confirmed": True, **extra})

    def poll(self, job_id, until, timeout=10):
        import time
        deadline = time.monotonic() + timeout
        while True:
            code, payload = self.get_json(f"/api/astro/jobs/{job_id}")
            self.assertEqual(200, code)
            if payload["status"] in until:
                return payload
            self.assertLess(time.monotonic(), deadline, f"job stayed {payload['status']}")
            time.sleep(0.05)

    def assert_safe(self, payload):
        text = json.dumps(payload)
        for secret in (str(self.root), str(self.night), "fingerprint", "manifest_path", "result_path", SIRIL.executable, "lumen: command"):
            self.assertNotIn(secret, text)

    def test_prepare_requires_explicit_confirmation_and_strict_fields(self):
        bad = (
            {"group_id": self.group["id"]},
            {"group_id": self.group["id"], "confirmed": "yes"},
            {"group_id": self.group["id"], "confirmed": 1},
            {"group_id": str(self.group["id"]), "confirmed": True},
            {"group_id": self.group["id"], "confirmed": True, "excluded_photo_ids": "1"},
            {"group_id": self.group["id"], "confirmed": True, "excluded_photo_ids": [True]},
            {"group_id": self.group["id"], "confirmed": True, "darks": 5},
            {"group_id": self.group["id"], "confirmed": True, "extra": 1},
            {"group_id": 999, "confirmed": True},
            {"group_id": self.group["id"], "confirmed": True, "recipe": "fast"},
            {"group_id": self.group["id"], "confirmed": True, "recipe": None},
        )
        for payload in bad:
            with self.subTest(payload=payload):
                code, body = self.post_json("/api/astro/jobs", payload)
                self.assertEqual(400, code)
                self.assertIn("error", body)
                self.assert_safe(body)
        with Library(self.settings) as library:
            self.assertEqual([], library.catalog.list_jobs())
            self.assertFalse((self.settings.output_dir / "astro").exists())

    def test_prepare_reports_warnings_and_selection_without_paths(self):
        empty = self.root / "Darks Folder"
        empty.mkdir()
        code, payload = self.prepare(excluded_photo_ids=[self.photo_ids[2]], darks=str(empty), flats=None)
        self.assertEqual(201, code)
        self.assertEqual(("astro", "queued", 0.0, None, None), (
            payload["kind"], payload["status"], payload["progress"], payload["result"], payload["error"]))
        self.assertEqual(2, payload["lights"])
        self.assertEqual(1, payload["excluded_lights"])
        self.assertTrue(any("Darks Folder" in warning for warning in payload["warnings"]))
        self.assertTrue(any("flat" in warning.lower() for warning in payload["warnings"]))
        self.assertEqual([], payload["log_tail"])
        self.assert_safe(payload)
        code, status = self.get_json(f"/api/astro/jobs/{payload['job_id']}")
        self.assertEqual((200, payload), (code, status))

    def test_prepare_passes_the_recipe_through_and_reports_it(self):
        code, payload = self.prepare(recipe="untracked")
        self.assertEqual((201, "untracked"), (code, payload["recipe"]))
        script = self.settings.output_dir / "astro" / str(payload["job_id"]) / "stack.ssf"
        self.assertIn("register light -2pass\nseqapplyreg light -framing=min\n", script.read_text())
        self.assert_safe(payload)
        code, payload = self.prepare()
        self.assertEqual((201, "tracked"), (code, payload["recipe"]))

    def test_run_then_poll_reports_progress_log_tail_and_result_artifact(self):
        code, prepared = self.prepare()
        job_id = prepared["job_id"]
        code, started = self.post_json(f"/api/astro/jobs/{job_id}/run", {})
        self.assertEqual(202, code)
        self.assertIn(started["status"], ("queued", "running"))
        finished = self.poll(job_id, {"succeeded", "failed", "cancelled"})
        self.assertEqual("succeeded", finished["status"])
        self.assertEqual(1.0, finished["progress"])
        self.assertTrue(any("savetif" in line for line in finished["log_tail"]))
        self.assertFalse(any(line.startswith("lumen: command") for line in finished["log_tail"]))
        self.assert_safe(finished)
        with Library(self.settings) as library:
            [artifact] = library.catalog.list_artifacts()
        self.assertEqual({"artifact_id": artifact.id, "name": "result.tif"}, finished["result"])
        code, body = self.post_json(f"/api/astro/jobs/{job_id}/run", {})
        self.assertEqual(400, code)
        code, body = self.post_json(f"/api/astro/jobs/{job_id}/run", {"force": True})
        self.assertEqual(400, code)

    def test_second_heavy_job_conflicts_until_first_is_cancelled(self):
        self.scenario = "hang"
        _, first = self.prepare()
        _, second = self.prepare()
        self.assertEqual(202, self.post_json(f"/api/astro/jobs/{first['job_id']}/run", {})[0])
        self.poll(first["job_id"], {"running"})
        code, body = self.post_json(f"/api/astro/jobs/{second['job_id']}/run", {})
        self.assertEqual(409, code)
        self.assertIn("another heavy job", body["error"])
        self.assertEqual("queued", self.get_json(f"/api/astro/jobs/{second['job_id']}")[1]["status"])
        code, cancelled = self.post_json(f"/api/astro/jobs/{first['job_id']}/cancel", {})
        self.assertEqual(200, code)
        self.assertEqual("cancelled", cancelled["status"])
        self.assertIsNone(cancelled["result"])
        self.assert_safe(cancelled)
        self.poll(first["job_id"], {"cancelled"})
        self.scenario = "success"
        self.assertEqual(202, self.post_json(f"/api/astro/jobs/{second['job_id']}/run", {})[0])
        self.assertEqual("succeeded", self.poll(second["job_id"], {"succeeded", "failed"})["status"])
        code, body = self.post_json(f"/api/astro/jobs/{second['job_id']}/cancel", {"why": "x"})
        self.assertEqual(400, code)
        code, body = self.post_json(f"/api/astro/jobs/{second['job_id']}/cancel", {})
        self.assertEqual(400, code)
        self.assertEqual("succeeded", self.get_json(f"/api/astro/jobs/{second['job_id']}")[1]["status"])

    def test_missing_siril_and_unknown_jobs_are_safe_json_errors(self):
        with patch("lumen.library.find_tool", return_value=None):
            code, body = self.prepare()
        self.assertEqual(400, code)
        self.assertIn("siril-cli", body["error"])
        for method, route in (("GET", "/api/astro/jobs/42"), ("POST", "/api/astro/jobs/42/run"), ("POST", "/api/astro/jobs/42/cancel")):
            with self.subTest(route=route):
                code, body = self.get_json(route) if method == "GET" else self.post_json(route, {})
                self.assertEqual(404 if method == "GET" else 400, code)
                self.assertIn("error", body)
                self.assert_safe(body)

    def test_deleted_job_directory_still_reports_status_and_safe_errors(self):
        import shutil
        _, prepared = self.prepare()
        job_id = prepared["job_id"]
        shutil.rmtree(self.settings.output_dir / "astro" / str(job_id))
        code, status = self.get_json(f"/api/astro/jobs/{job_id}")
        self.assertEqual((200, "queued", []), (code, status["status"], status["log_tail"]))
        self.assertTrue(any("unavailable" in warning for warning in status["warnings"]))
        self.assert_safe(status)
        code, body = self.post_json(f"/api/astro/jobs/{job_id}/run", {})
        self.assertEqual(400, code)
        self.assert_safe(body)
        code, cancelled = self.post_json(f"/api/astro/jobs/{job_id}/cancel", {})
        self.assertEqual((200, "cancelled"), (code, cancelled["status"]))
        self.assert_safe(cancelled)
        (self.settings.output_dir / "astro" / str(job_id + 1)).mkdir(parents=True)
        code, body = self.prepare()
        self.assertEqual(400, code)
        self.assert_safe(body)

    def test_worker_errors_never_expose_executable_or_library_paths(self):
        _, prepared = self.prepare()
        with patch("lumen.astro.subprocess.Popen", side_effect=FileNotFoundError(2, "No such file or directory", SIRIL.executable)):
            self.post_json(f"/api/astro/jobs/{prepared['job_id']}/run", {})
            finished = self.poll(prepared["job_id"], {"failed", "succeeded", "cancelled"})
        self.assertEqual("failed", finished["status"])
        self.assertIn("No such file", finished["error"])
        self.assert_safe(finished)

    def test_stale_running_job_from_a_crashed_server_is_recovered_on_start(self):
        _, prepared = self.prepare()
        self.server.shutdown()
        self.server.server_close()  # Simulate the serving process going away mid-job.
        with Library(self.settings) as library:
            library.catalog.update_job(prepared["job_id"], status="running")
        self.server = make_server(self.settings, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        with Library(self.settings) as library:
            self.assertEqual("interrupted", library.catalog.get_job(prepared["job_id"]).status)

    def test_reveal_opens_only_an_existing_managed_job_folder(self):
        _, prepared = self.prepare()
        job_dir = (self.settings.output_dir / "astro" / str(prepared["job_id"])).resolve()
        with patch("lumen.web.sys.platform", "darwin"), patch("lumen.web.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            code, payload = self.post_json(f"/api/astro/jobs/{prepared['job_id']}/reveal", {})
            self.assertEqual((200, str(job_dir)), (code, payload["path"]))
            self.assertEqual(["/usr/bin/open", str(job_dir)], run.call_args.args[0])
            run.reset_mock()
            for route, body in ((f"/api/astro/jobs/{prepared['job_id']}/reveal", {"path": "x"}), ("/api/astro/jobs/42/reveal", {})):
                code, payload = self.post_json(route, body)
                self.assertEqual(400, code)
                self.assert_safe(payload)
            run.assert_not_called()
            self.assertFalse((self.settings.output_dir / "astro" / "42").exists())

    def test_failed_job_status_keeps_error_and_log_without_paths(self):
        self.scenario = "nonzero"
        _, prepared = self.prepare()
        self.post_json(f"/api/astro/jobs/{prepared['job_id']}/run", {})
        finished = self.poll(prepared["job_id"], {"failed", "succeeded", "cancelled"})
        self.assertEqual("failed", finished["status"])
        self.assertIn("exit code 3", finished["error"])
        self.assertTrue(any("invalid arguments" in line for line in finished["log_tail"]))
        self.assertIsNone(finished["result"])
        self.assert_safe(finished)


class DevelopWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.settings = Settings(library_dir=self.root / "library")
        source = self.root / "secret-folder" / "photo.jpg"
        source.parent.mkdir()
        Image.new("RGB", (120, 90), (80, 100, 120)).save(source)
        with Library(self.settings) as library:
            library.import_path(source)
            self.photo = library.catalog.get_photo(1)
        self.calls = []
        self.render_fails = False
        patchers = [patch("lumen.web.render_raw", side_effect=self.render),
                    patch("lumen.library.render_raw", side_effect=self.render)]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = make_server(self.settings, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def render(self, source, destination, executable, *, settings=None, long_edge=0,
               image_format="tiff16", histogram=False, runner=None):
        self.calls.append({"settings": settings, "long_edge": long_edge, "histogram": histogram})
        if self.render_fails:
            from lumen.rawdevelop import DevelopError
            raise DevelopError(f"The raw developer could not develop {Path(source).name}.")
        Image.new("RGB", (64, 48), (200, 100, 50)).save(destination, format="JPEG")
        return DevelopRender(path=Path(destination), width=64, height=48, histogram={"y": (0.1, 0.2), "r": (0.3, 0.4), "g": (0.5, 0.6), "b": (0.7, 0.8)},
                              defaults={"contrast": 0.0, "sharpness": 1.0, "luminance_nr": 0.004, "color_nr": 0.5},
                              lens_correction_supported=True,
                             neutral_temperature=5500.0, neutral_tint=3.0)

    post_json = WebTests.post_json
    get_json = WebTests.get_json

    def request(self, method, path, headers=None):
        request = urllib.request.Request(self.base + path, headers=headers or {}, method=method,
                                         data=b"{}" if method == "DELETE" else None)
        if method == "DELETE":
            request.add_header("Content-Type", "application/json")
        try:
            response = urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.code, response.read()

    def preview(self, **body):
        return self.post_json("/api/photos/1/develop/preview", {"settings": {"contrast": 0.5}, "long_edge": 512} | body)

    def test_photo_detail_carries_saved_develop_settings_and_the_offline_flag(self):
        """The Develop tab fills its controls from this payload and disables itself when the original is away."""
        payload = self.get_json("/api/photos/1")[1]
        self.assertIsNone(payload["develop"], "an undeveloped frame reports no settings, not an empty object")
        self.assertFalse(payload["photo"]["offline"])
        self.post_json("/api/photos/1/develop", {"settings": {"exposure_ev": 0.5, "curve": [[0, 0], [0.2, 0.3], [0.5, 0.5], [0.7, 0.8], [1, 1]]}})
        payload = self.get_json("/api/photos/1")[1]
        self.assertEqual({"exposure_ev": 0.5, "curve": [[0, 0], [0.2, 0.3], [0.5, 0.5], [0.7, 0.8], [1, 1]]}, payload["develop"],
                         "only the fields the photographer set come back")
        self.assertFalse(payload["photo"]["offline"])
        Path(self.photo.path).rename(self.root / "moved.jpg")
        payload = self.get_json("/api/photos/1")[1]
        self.assertTrue(payload["photo"]["offline"], "an unreachable original is reported, so no control offers a render")
        self.assertNotIn(str(self.root), json.dumps(payload))

    def test_develop_preview_returns_an_image_and_a_histogram(self):
        code, body = self.preview()
        self.assertEqual(200, code)
        self.assertTrue(body["image"].startswith("data:image/jpeg;base64,"))
        self.assertEqual({"y": [0.1, 0.2], "r": [0.3, 0.4], "g": [0.5, 0.6], "b": [0.7, 0.8]}, body["histogram"])
        self.assertEqual((5500.0, 3.0), (body["neutral_temperature"], body["neutral_tint"]))
        self.assertEqual({"contrast": 0.5}, self.calls[0]["settings"])
        self.assertTrue(self.calls[0]["histogram"])
        with Library(self.settings) as library:
            self.assertIsNone(library.develop_for(1), "the preview route never persists")

    def test_develop_preview_clamps_an_oversized_long_edge(self):
        self.assertEqual(200, self.preview(long_edge=10**9)[0])
        self.assertEqual(2048, self.calls[0]["long_edge"])

    def test_develop_preview_renders_native_for_a_zero_long_edge(self):
        """Zoomed to 1:1, the panel asks for the raw's own size.

        Sharpening and noise reduction act on single pixels, so a preview scaled down to the
        viewport cannot show them however it was decoded. The viewport clamp does not apply:
        the raw's own dimensions already bound the request.
        """
        self.assertEqual(200, self.preview(long_edge=0)[0])
        self.assertEqual(0, self.calls[0]["long_edge"])

    def test_develop_preview_rejects_a_bad_long_edge(self):
        for value in (-1, 1.5, "512", True, None):
            self.assertEqual(400, self.preview(long_edge=value)[0], value)
        self.assertEqual([], self.calls)

    def test_develop_rejects_bad_settings_and_extra_keys(self):
        curve = [[0.5, 0], [0.4, 0.2], [0.6, 0.4], [0.8, 0.6], [1, 1]]
        for settings in ({"nope": 1}, {"contrast": 99}, {"curve": curve}, "x", {"contrast": float("nan")}):
            self.assertEqual(400, self.post_json("/api/photos/1/develop", {"settings": settings})[0], settings)
        self.assertEqual(400, self.post_json("/api/photos/1/develop", {"settings": {}, "extra": 1})[0])
        self.assertEqual(400, self.preview(extra=1)[0])
        self.assertEqual(400, self.post_json("/api/photos/1/develop", {})[0])
        self.assertEqual([], self.calls)

    def test_develop_empty_settings_clear_the_frame(self):
        self.post_json("/api/photos/1/develop", {"settings": {"contrast": 0.5}})
        code, body = self.post_json("/api/photos/1/develop", {"settings": {}})
        self.assertEqual((200, 1, None), (code, body["photo_id"], body["develop"]))
        with Library(self.settings) as library:
            self.assertIsNone(library.develop_for(1))
            self.assertEqual("refined", library.catalog.get_photo(1).preview_kind)

    def test_develop_full_size_render_failure_is_a_500(self):
        self.post_json("/api/photos/1/develop", {"settings": {"contrast": 0.5}})
        self.render_fails = True
        self.assertEqual(500, self.request("GET", "/full/1")[0])

    def test_develop_save_and_clear(self):
        code, body = self.post_json("/api/photos/1/develop", {"settings": {"contrast": 0.5}})
        self.assertEqual((200, {"photo_id": 1, "develop": {"contrast": 0.5}}), (code, {k: body[k] for k in ("photo_id", "develop")}))
        self.assertRegex(body["preview_url"], r"^/preview/1\?v=.+-developed-\d+$")
        with Library(self.settings) as library:
            self.assertEqual({"contrast": 0.5}, library.develop_for(1).to_dict())
        code, raw = self.request("DELETE", "/api/photos/1/develop")
        raw = json.loads(raw)
        self.assertEqual((200, None), (code, raw["develop"]))
        self.assertRegex(raw["preview_url"], r"^/preview/1\?v=.+-refined-\d+$")
        with Library(self.settings) as library:
            self.assertIsNone(library.develop_for(1))

    def test_apply_to_group_starts_a_job_and_validates_the_request(self):
        url = "/api/photos/1/develop/apply-to-group"
        second = self.root / "secret-folder" / "photo2.jpg"
        Image.new("RGB", (120, 90), (90, 110, 130)).save(second)
        with Library(self.settings) as library:
            library.import_path(second)
            library.merge_groups([group["id"] for group in library.list_groups()])
            with library.catalog.connection:
                library.catalog.connection.execute("UPDATE photos SET format = 'ARW'")
        self.assertEqual(400, self.post_json(url, {})[0], "no settings saved yet")
        self.post_json("/api/photos/1/develop", {"settings": {"contrast": 0.5}})
        self.assertEqual(400, self.post_json(url, {"extra": 1})[0])
        self.assertEqual(400, self.post_json("/api/photos/999/develop/apply-to-group", {})[0])
        self.assertEqual(403, self.post_json(url, {}, headers={"Origin": "https://attacker.example"})[0])
        code, job = self.post_json(url, {})
        finished = WebTests.wait_job(self, job["job_id"])
        self.assertEqual((202, "develop_group", "succeeded"), (code, job["kind"], finished["status"]))
        with Library(self.settings) as library:
            self.assertEqual({"contrast": 0.5}, library.develop_for(2).to_dict())
        self.assertNotIn(str(self.root), json.dumps(finished))

    def test_develop_rejects_cross_origin_and_foreign_host(self):
        for headers in ({"Origin": "https://attacker.example"}, {"Host": f"attacker.example:{self.server.server_port}"}):
            self.assertEqual(403, self.post_json("/api/photos/1/develop", {"settings": {}}, headers=headers)[0])
            self.assertEqual(403, self.post_json("/api/photos/1/develop/preview", {"settings": {}, "long_edge": 9}, headers=headers)[0])
            self.assertEqual(403, self.request("DELETE", "/api/photos/1/develop", headers)[0])
        self.assertEqual([], self.calls)

    def test_develop_unknown_photo_and_helper_failure_stay_public(self):
        self.assertEqual(400, self.post_json("/api/photos/999/develop", {"settings": {}})[0])
        self.render_fails = True
        for code, body in (self.preview(), self.post_json("/api/photos/1/develop", {"settings": {"contrast": 0.5}})):
            self.assertEqual(400, code)
            self.assertIn("could not develop", body["error"])
            text = json.dumps(body)
            self.assertNotIn(str(self.root), text)
            self.assertNotIn(self.photo.fingerprint, text)

    def test_full_size_serves_a_developed_render_only_for_developed_frames(self):
        full_dir = self.settings.cache_dir / "full"
        plain = full_dir / f"{self.photo.fingerprint}.jpg"
        full_dir.mkdir(parents=True)
        Image.new("RGB", (30, 20), "blue").save(plain)
        self.assertEqual(200, self.request("GET", "/full/1")[0])
        self.assertEqual([], self.calls, "an undeveloped frame keeps the embedded JPEG")
        self.post_json("/api/photos/1/develop", {"settings": {"contrast": 0.5}})
        self.calls.clear()
        code, body = self.request("GET", "/full/1")
        self.assertEqual(200, code)
        self.assertEqual((64, 48), Image.open(BytesIO(body)).size)
        self.assertEqual(1, len(self.calls))
        self.assertEqual(0, self.calls[0]["long_edge"])
        self.assertEqual(1, len([p for p in full_dir.glob(f"{self.photo.fingerprint}-*.jpg")]))
        self.request("GET", "/full/1")
        self.assertEqual(1, len(self.calls), "the second request is served from the cache")
        self.post_json("/api/photos/1/develop", {"settings": {"contrast": 0.6}})
        self.request("GET", "/full/1")
        self.assertEqual(2, len(list(full_dir.glob(f"{self.photo.fingerprint}-*.jpg"))))
