import json
import subprocess
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
from lumen.library import Library
from lumen.omlx import CompositionReview, OmlxError
from lumen.pipelines import DarktablePipeline
from lumen.web import make_server

try:
    from tests.test_astro import FAKE_SIRIL, SIRIL
except ModuleNotFoundError:  # discovered with -s tests
    from test_astro import FAKE_SIRIL, SIRIL


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
        Image.new("RGB", (40, 30), "red").save(
            command[-8], format="TIFF",
            icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes(),
        )
        return subprocess.CompletedProcess(command, 0, "", "")

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
        self.assertEqual(200, code, second)
        one, two = first["results"][0], second["results"][0]
        self.assertEqual(1, one["photo_id"])
        self.assertTrue(one["success"])
        self.assertEqual("photo-lumen.jpg", Path(one["output"]).name)
        self.assertEqual("photo-lumen-1.jpg", Path(two["output"]).name)
        self.assertEqual(first_bytes, Path(one["output"]).read_bytes())
        self.assertEqual(one["output"], one["artifact"]["path"])
        self.assertEqual(str(profile.resolve()), one["artifact"]["settings"]["profile"])
        datetime.fromisoformat(one["artifact"]["created_at"])

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
        self.assertEqual(400, code)
        self.assertEqual([False, True], [item["success"] for item in payload["results"]])
        self.assertIsNone(payload["results"][0]["artifact"])
        self.assertTrue(Path(payload["results"][1]["output"]).is_file())

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
        tabs = [node for node in parser.nodes if node["attrs"].get("role") == "tab"]
        self.assertEqual(["Compose", "Export", "Astro", "Clear"], [node["text"].strip() for node in tabs])
        self.assertTrue(any(node["attrs"].get("role") == "tablist" and node["attrs"].get("aria-label") for node in parser.nodes))
        for index, tab in enumerate(tabs):
            self.assertEqual("button", tab["tag"])
            self.assertEqual(str(index == 0).lower(), tab["attrs"]["aria-selected"])
            self.assertEqual("0" if index == 0 else "-1", tab["attrs"].get("tabindex"))
            panel = by_id[tab["attrs"]["aria-controls"]]
            self.assertEqual("tabpanel", panel["attrs"].get("role"))
            self.assertEqual(tab["attrs"]["id"], panel["attrs"].get("aria-labelledby"))
            self.assertEqual(index != 0, "hidden" in panel["attrs"])
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
        for name in ("remove-group", "clear-workspace"):
            self.assertNotIn(export, by_id[name]["parents"])
            self.assertIn(by_id["clear-panel"], by_id[name]["parents"])

    def test_crop_api_returns_safe_candidates_and_active_edit(self):
        with patch("lumen.library.read_image_dimensions", return_value=(4000, 3000)), patch(
            "lumen.library.analyze_subject_regions", return_value=None
        ):
            with urllib.request.urlopen(self.base + "/api/photos/1/crops", timeout=3) as response:
                payload = json.load(response)
            self.assertEqual(1, payload["photo_id"])
            self.assertRegex(payload["preview_url"], r"^/preview/1\?v=[0-9a-f]{16}$")
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
            self.assertEqual((200, {"photo_id": 1, "active_crop": None}), (code, cleared))

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

        with patch("lumen.web.OmlxTagger", return_value=Tagger()):
            code, payload = self.post_json("/api/photos/1/composition-review", {})
        self.assertEqual(200, code, payload)
        self.assertEqual({"candidate_id", "rationale", "confidence"}, set(payload))
        self.assertEqual("crop-original", payload["candidate_id"])
        with Library(Settings(library_dir=self.root / "library")) as library:
            self.assertIsNone(library.catalog.get_crop(1))

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
            with patch("lumen.web.OmlxTagger", return_value=BrokenTagger()):
                code, payload = self.post_json("/api/photos/1/composition-review", {})
        self.assertEqual(400, code, payload)
        self.assertEqual("local composition review is unavailable for this photo", payload["error"])
        self.assertNotIn("raw_response", payload)
        with Library(Settings(library_dir=self.root / "library")) as library:
            self.assertEqual(candidate["id"], library.catalog.get_crop(1).candidate_id)

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
                with self.subTest(path=path):
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
        compose = by_id["compose-panel"]
        self.assertIn("Select a photo", compose["text"])
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
    };
  }
  append(...children) { children.forEach(child => { if (child && typeof child === 'object') child.parent = this; }); this.children.push(...children); }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  setAttribute(name, value) { this.attrs[name] = value; }
  getAttribute(name) { return this.attrs[name]; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  querySelector(selector) { return element('#import-form ' + selector); }
  closest(selector) { for (let node = this; node; node = node.parent) if (selector === '.crop-card' && node.classList.contains('crop-card')) return node; return null; }
  focus() {}
}
const elements = new Map();
function element(selector) {
  if (!elements.has(selector)) elements.set(selector, new Element());
  return elements.get(selector);
}
const tabElements = ['compose', 'export', 'astro', 'clear'].map((name, index) => {
  const tab = element('#' + name + '-tab');
  tab.id = name + '-tab';
  tab.setAttribute('aria-controls', name + '-panel');
  tab.setAttribute('aria-selected', String(index === 0));
  return tab;
});
const scope = new Element(); scope.value = 'current';
const document = {
  createElement: () => new Element(),
  querySelector: selector => selector === 'input[name="export-scope"]:checked' ? scope : element(selector),
  querySelectorAll: selector => {
    if (selector === '.frame') return element('#contact-sheet').children.filter(item => item.className === 'frame');
    if (selector === '.batch-button') return element('#batch-list').children;
    if (selector === '[role="tab"]') return tabElements;
    if (selector === '#astro-lights input') return element('#astro-lights').children.map(label => label.children[0]);
    if (selector === '[data-crop-action]') return [element('#primary-crops'), element('#alternative-crops')].flatMap(grid => grid.children.map(card => card.children.at(-1)));
    return [];
  },
};
let activeTimers = 0;
const window = { setTimeout: callback => callback(), setInterval: callback => { activeTimers += 1; return setInterval(callback, 5); }, clearInterval: id => { activeTimers -= 1; clearInterval(id); } };
const localStorage = { getItem: key => (scenario === 'astro-resume' && key === 'lumen-astro-job' ? '7' : null), setItem() {}, removeItem() {} };
const photo = id => ({ id, name: 'photo-' + id + '.jpg', total_score: 0.8, preview_url: '/preview/' + id });
const group = (id, ids) => ({ id, automatic_pick_id: ids[0], photos: ids.map(photo) });
let groups = scenario === 'merge' ? [group(1, [1]), group(2, [2])] : scenario.startsWith('astro') ? [group(1, [1, 2, 3])] : [group(1, [1, 2])];
const astroPosts = [];
let polls = 0;
let cancels = 0;
let runs = 0;
const statusWrites = [];
Object.defineProperty(element('#astro-status'), 'textContent', { get() { return this._text || ''; }, set(value) { this._text = value; statusWrites.push(value); } });
const jobPayload = (status, progress, extra = {}) => ({ job_id: 7, kind: 'astro', status, progress, error: null, result: null, log_tail: ['log: Running command: register'], warnings: ['No flat frames supplied; stacking without flats.'], lights: 2, excluded_lights: 1, darks: 0, flats: 0, biases: 0, created_at: '', updated_at: '', ...extra });
const removals = [];
const compositionReviews = [];
const cropMutations = [];
const cropId = 'crop-1111111111111111';
let activeCrop = null;
let reviewMode = 'success';
let cropLoads = 0;
let deferCropLoads = false;
let deferReviews = false;
const pendingCropLoads = [];
const pendingReviews = [];
function cropPayload(active = activeCrop) {
  return {
    photo_id: 2, preview_url: '/preview/2?v=test', active_crop: active,
    candidates: [
      { id: 'crop-original', kind: 'original', aspect: 'native', rect: { left: 0, top: 0, width: 1, height: 1 }, pixel_box: [0, 0, 120, 90], remaining_megapixels: 0.01, rationale: '', primary: true, revealable: false, resolution_warning: '0.010800 MP remaining; below 12 MP' },
      { id: cropId, kind: 'centered-fallback', aspect: '1:1', rect: { left: 0.125, top: 0, width: 0.75, height: 1 }, pixel_box: [15, 0, 105, 90], remaining_megapixels: 8.1, rationale: 'centered fallback', primary: true, revealable: false, resolution_warning: '8.100000 MP remaining; below 12 MP' },
    ],
  };
}
async function fetch(url, options = {}) {
  let payload;
  let ok = true;
  if (url === '/api/status') payload = { omlx_live: { summary: 'Ready' }, tools: { 'darktable-cli': { available: true }, 'siril-cli': { available: true } }, jpeg_output_dir: '/managed/outputs/jpeg', astro_output_dir: '/managed/outputs/astro' };
  else if (scenario.startsWith('workspace')) return httpFetch(apiBase + url, options);
  else if (url === '/api/groups') payload = { groups, photo_count: groups.reduce((count, item) => count + item.photos.length, 0) };
  else if (/^\/api\/photos\/[123]$/.test(url)) payload = { metrics: { sharpness: 0.8, exposure: 0.8, eye_quality: null }, tags: null };
  else if (url === '/api/photos/2/crops') {
    cropLoads += 1;
    if (deferCropLoads) return new Promise(resolve => pendingCropLoads.push(value => resolve({ ok: true, json: async () => value })));
    payload = cropPayload();
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
    else { assert.deepEqual(body, { candidate_id: cropId }); activeCrop = { candidate_id: cropId }; payload = activeCrop; }
  }
  else if (/^\/api\/groups\/\d+\/remove$/.test(url)) {
    assert.equal(options.method, 'POST');
    assert.deepEqual(JSON.parse(options.body), {});
    removals.push(url);
    const id = Number(url.split('/')[3]);
    const removed = groups.find(item => item.id === id);
    groups = groups.filter(item => item.id !== id);
    payload = { photos_removed: removed.photos.length, files_removed: 3, trash_path: '/managed/trash/recovery', snapshot_path: '/managed/trash/recovery/catalog.sqlite3' };
  } else if (url === '/api/browse') payload = { path: '/cal/darks', cancelled: false };
  else if (url === '/api/astro/jobs') { assert.equal(options.method, 'POST'); astroPosts.push(JSON.parse(options.body)); payload = jobPayload('queued', 0); }
  else if (url === '/api/astro/jobs/7/run') { assert.deepEqual(JSON.parse(options.body), {}); runs += 1; payload = jobPayload('queued', 0); }
  else if (url === '/api/astro/jobs/7/cancel') { assert.deepEqual(JSON.parse(options.body), {}); cancels += 1; payload = jobPayload('cancelled', 0.5); }
  else if (url === '/api/astro/jobs/7/reveal') { assert.deepEqual(JSON.parse(options.body), {}); payload = { path: '/managed/outputs/astro/7' }; }
  else if (url === '/api/astro/jobs/7') {
    polls += 1;
    if (polls < 3 || scenario === 'astro-cancel') payload = jobPayload('running', polls === 1 ? 0.1 : 0.12);
    else if (scenario === 'astro-fail') payload = jobPayload('failed', 0.5, { error: 'Siril exited with exit code 3' });
    else payload = jobPayload('succeeded', 1, { result: { artifact_id: 1, name: 'result.tif' } });
  } else throw new Error('Unexpected request: ' + url);
  return { ok, json: async () => payload };
}
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
  const pathsText = () => element('#astro-paths').children.map(child => child.textContent).join(' ');
  if (scenario === 'astro-resume') {
    await load();
    assert.equal(state.astro.job.status, 'running', 'a remembered job is refetched on load');
    assert.equal(activeTimers, 1, 'a remembered running job resumes polling');
    await new Promise(resolve => setTimeout(resolve, 80));
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
    await element('#prepare-astro').listeners.click();
    assert.deepEqual(astroPosts, [{ group_id: 1, confirmed: true, excluded_photo_ids: [3], darks: '/cal/darks', flats: null, biases: null }]);
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
    await new Promise(resolve => setTimeout(resolve, 15));
    assert.equal(state.astro.job.status, 'running');
    assert.ok(element('#astro-progress').value >= 10);
    assert.ok(element('#announcer').textContent.includes('running'));
    if (scenario === 'astro-cancel') {
      await new Promise(resolve => setTimeout(resolve, 30));
      assert.equal(activeTimers, 1, 'a running job keeps polling');
      await element('#cancel-astro').listeners.click();
      assert.equal(cancels, 1);
      assert.equal(activeTimers, 0, 'cancellation stops polling');
      assert.equal(state.astro.job.status, 'cancelled');
      assert.ok(element('#announcer').textContent.includes('cancelled'));
      assert.ok(pathsText().includes('siril.log'), 'logs stay reachable after cancel');
    } else {
      await new Promise(resolve => setTimeout(resolve, 80));
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
  element('#compose-tab').listeners.click();
  await new Promise(setImmediate);
  assert.equal(state.selectedId, 2, 'opening Compose must preserve the selected photo');
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

  element('#compose-empty').textContent = 'Crop choices are unavailable for this photo.';
  state.selectedId = null;
  await loadComposition();
  assert.ok(element('#compose-empty').textContent.startsWith('Select a photo'));
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
    const batches = element('#batch-list').children;
    assert.deepEqual(batches.map(item => item.getAttribute('aria-pressed')), scenario === 'split' ? ['false', 'true'] : ['true']);
    await element('#remove-group').listeners.click();
    assert.deepEqual(removals, ['/api/groups/' + id + '/remove']);
  }
})()
`;
vm.runInNewContext(harness + fs.readFileSync(process.argv[1], 'utf8') + regression, {
  assert: require('node:assert/strict'), setImmediate, setInterval, clearInterval, setTimeout, scenario: process.argv[2], apiBase: process.argv[3], httpFetch: globalThis.fetch,
}).catch(error => { console.error(error); process.exitCode = 1; });
"""
        app = Path(__file__).resolve().parents[1] / "src" / "lumen" / "static" / "app.js"
        result = subprocess.run(["node", "-e", script, str(app), scenario, self.base], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_javascript_astro_tab_confirms_includes_polls_and_retains_failure_context(self):
        for scenario in ("astro", "astro-fail", "astro-cancel", "astro-resume"):
            with self.subTest(scenario=scenario):
                self.run_javascript_cleanup_regression(scenario)

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
        self.assertEqual(201, code)
        self.assertEqual(1, result["failed"])
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
            self.assertEqual({"groups": [], "photo_count": 0}, json.load(response))
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
        self.assertIn("legend", [child["tag"] for child in parser.nodes if by_id["astro-lights-group"] in child["parents"]])
        for kind in ("darks", "flats", "biases"):
            browse = by_id[f"browse-astro-{kind}"]
            self.assertEqual(("button", "button"), (browse["tag"], browse["attrs"].get("type")))
            self.assertIn(kind[:-1], browse["text"].lower())
            self.assertEqual("path", by_id[f"astro-{kind}-path"]["attrs"].get("class"))
            self.assertTrue(by_id[f"astro-{kind}-path"]["attrs"].get("aria-labelledby"))
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

    def test_index_has_semantic_landmarks_and_labeled_import_control(self):
        html = urllib.request.urlopen(self.base + "/", timeout=3).read().decode()
        parser = LandmarkParser()
        parser.feed(html)
        self.assertIn("main", parser.tags)
        self.assertIn("nav", parser.tags)
        self.assertIn("aside", parser.tags)
        self.assertGreaterEqual(parser.labels, 1)

    def test_groups_api_returns_ranked_photo_with_preview_url(self):
        payload = json.load(urllib.request.urlopen(self.base + "/api/groups", timeout=3))
        self.assertEqual(1, len(payload["groups"]))
        photo = payload["groups"][0]["photos"][0]
        self.assertTrue(photo["preview_url"].startswith("/preview/"))
        self.assertIsInstance(photo["total_score"], float)

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
        urllib.request.urlopen(request, timeout=3).close()
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
