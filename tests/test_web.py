import json
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
from lumen.library import Library
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
        self.assertEqual({"discovered": 2, "imported": 2, "updated": 0, "skipped": 0, "failed": 0}, finished["result"])
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
        self.assertEqual(("tabpanel", "ingest-tab"), (panel["attrs"].get("role"), panel["attrs"].get("aria-labelledby")))
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

            def __init__(self, *args):
                pass

            def tag(self, preview):
                from lumen.omlx import TagResult
                return TagResult("A frame", ["frame"], "room", [], 0.9, "{}")

        changed = self.root / "photo.jpg"
        Image.new("RGB", (120, 90), (1, 2, 3)).save(changed)
        with patch.object(Library, "_import_one", held), patch("lumen.web.OmlxTagger", FakeTagger):
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
        self.assertEqual(("button", "button", "Tag all untagged"), (by_id["tag-all"]["tag"], by_id["tag-all"]["attrs"].get("type"), by_id["tag-all"]["text"].strip()))
        self.assertEqual(("input", "checkbox"), (by_id["retag-all"]["tag"], by_id["retag-all"]["attrs"].get("type")))
        retag_label = next(node for node in parser.nodes if node["tag"] == "label" and node["attrs"].get("for") == "retag-all")
        self.assertEqual("Re-tag already tagged", retag_label["text"].strip())
        styles = (Path(__file__).resolve().parents[1] / "src" / "lumen" / "static" / "styles.css").read_text()
        self.assertRegex(styles, r"#cancel-job\s*\{[^}]*min-width:\s*44px")
        javascript = (Path(__file__).resolve().parents[1] / "src" / "lumen" / "static" / "app.js").read_text()
        for word in ("finished", "failed", "cancelled", "interrupted"):
            self.assertIn(word, javascript)
        self.assertNotRegex(javascript, r"job[^\n]*\.innerHTML")

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

    def test_export_accepts_known_presets_only_and_ui_has_labeled_preset_radios(self):
        with patch("lumen.library.PipelineRegistry.darktable", side_effect=AssertionError("render attempted")):
            for payload in ({"scope": "current", "photo_id": 1, "preset": "web"}, {"scope": "keepers", "preset": 2},
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
            self.assertEqual(200, code, full)
        self.assertEqual("photo-lumen-share.jpg", Path(share["results"][0]["output"]).name)
        self.assertEqual("share", share["results"][0]["artifact"]["settings"]["preset"])
        self.assertEqual("photo-lumen.jpg", Path(full["results"][0]["output"]).name)
        self.assertEqual("full", full["results"][0]["artifact"]["settings"]["preset"])
        html = urllib.request.urlopen(self.base + "/", timeout=3).read().decode()
        parser = LandmarkParser()
        parser.feed(html)
        radios = [node for node in parser.nodes if node["tag"] == "input" and node["attrs"].get("name") == "export-preset"]
        self.assertEqual([("radio", "full"), ("radio", "share")], [(node["attrs"].get("type"), node["attrs"].get("value")) for node in radios])
        self.assertIn("checked", radios[0]["attrs"])
        for radio, label in zip(radios, ("Full resolution (quality 92, keeps GPS)", "Share (2048 px long edge, quality 85, no GPS)")):
            self.assertTrue(any(node["tag"] == "label" and label in " ".join(node["text"].split()) for node in radio["parents"]))
            self.assertTrue(any(node["tag"] == "fieldset" and "Size" in " ".join(node["text"].split()) for node in radio["parents"]))
        self.assertNotIn("Quality 92 · sRGB · Full resolution", html)
        javascript = (Path(__file__).resolve().parents[1] / "src" / "lumen" / "static" / "app.js").read_text()
        self.assertIn('input[name="export-preset"]:checked', javascript)
        self.assertRegex(javascript, r"payload = \{ scope, profile: state.profile, preset \}")

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
            "/api/import", "/api/photos/tag-all", "/api/jobs/1/cancel", "/api/groups/merge",
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
            "/api/import", "/api/photos/tag-all", "/api/jobs/1/cancel", "/api/groups/merge",
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
        self.assertEqual(["Compose", "Export", "Astro", "Ingest", "Clear"], [node["text"].strip() for node in tabs])
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

    def test_preview_url_version_changes_after_refinement_and_cards_show_refined_mark(self):
        with Library(Settings(library_dir=self.root / "library")) as library:
            version = library.catalog.list_photos()[0].fingerprint[:16]
            group_id = library.list_groups()[0]["id"]
        before = self.get_json("/api/groups")[1]["groups"][0]["photos"][0]
        self.assertEqual((f"/preview/1?v={version}-embedded", "embedded"), (before["preview_url"], before["preview_kind"]))
        with patch("lumen.library.find_tool", return_value=write_fake_darktable(self.root)):
            code, started = self.post_json("/api/previews/refine", {"scope": "group", "group_id": group_id})
            self.assertEqual(202, code, started)
            self.assertEqual("succeeded", self.wait_job(started["job_id"])["status"])
        refined_url = f"/preview/1?v={version}-refined"
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

    def test_crop_api_returns_safe_candidates_and_active_edit(self):
        with patch("lumen.library.read_image_dimensions", return_value=(4000, 3000)), patch(
            "lumen.library.analyze_subject_regions", return_value=None
        ):
            with urllib.request.urlopen(self.base + "/api/photos/1/crops", timeout=3) as response:
                payload = json.load(response)
            self.assertEqual(1, payload["photo_id"])
            self.assertRegex(payload["preview_url"], r"^/preview/1\?v=[0-9a-f]{16}-embedded$")
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
  closest(selector) {
    const test = { '.crop-card': node => node.classList.contains('crop-card'), dialog: node => node.tagName === 'DIALOG', 'dialog form': node => node.tagName === 'FORM' && Boolean(node.parent?.closest('dialog')) }[selector];
    for (let node = this; node; node = node.parent) if (test?.(node)) return node;
    return null;
  }
  focus() { focused = this; }
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
function element(selector) {
  if (!elements.has(selector)) elements.set(selector, new Element());
  return elements.get(selector);
}
const tabElements = ['compose', 'export', 'astro', 'ingest', 'clear'].map((name, index) => {
  const tab = element('#' + name + '-tab');
  tab.id = name + '-tab';
  tab.setAttribute('aria-controls', name + '-panel');
  tab.setAttribute('aria-selected', String(index === 0));
  return tab;
});
const scope = new Element(); scope.value = 'current';
const document = {
  createElement: tag => Object.assign(new Element(), { tagName: tag.toUpperCase() }),
  querySelector: selector => selector === 'input[name="export-scope"]:checked' ? scope : byData(selector) ? byData(selector)[0] ?? null
    : selector === '#rating' ? inspectorRating() : element(selector),
  querySelectorAll: selector => {
    if (selector === '.frame') { const tree = node => [node, ...(node.children || []).flatMap(tree)]; return tree(element('#contact-sheet')).filter(item => item.className === 'frame'); }
    if (selector === '.batch-button' || selector === '.batch-select') { const tree = node => [node, ...(node.children || []).flatMap(tree)]; return tree(element('#batch-list')).filter(node => node.className === selector.slice(1)); }
    if (selector === '[role="tab"]') return tabElements;
    if (selector === '#astro-lights input') return element('#astro-lights').children.map(label => label.children[0]);
    if (selector === '[data-crop-action]') return [element('#primary-crops'), element('#alternative-crops')].flatMap(grid => grid.children.map(card => card.children.at(-1)));
    return byData(selector) || [];
  },
};
// Intervals are recorded, never scheduled: scenarios advance them deterministically with tick().
let activeTimers = 0;
let nextTimer = 0;
const timers = new Map();
const window = { setTimeout: callback => callback(), setInterval: callback => { activeTimers += 1; timers.set(++nextTimer, callback); return nextTimer; }, clearInterval: id => { if (timers.delete(id)) activeTimers -= 1; }, matchMedia: () => ({ matches: true }) };
// pollJob returns its promise; setImmediate then flushes the fire-and-forget load() once the mocked-fetch microtasks drain.
async function tick() { for (const cb of [...timers.values()]) await cb(); await new Promise(setImmediate); }
async function settle() { for (let i = 0; i < 10 && activeTimers; i++) await tick(); }
const location = { search: '', pathname: '/' };
const history = { replaceState(_state, _title, url) { const at = url.indexOf('?'); location.search = at < 0 ? '' : url.slice(at); } };
const localStorage = { getItem: key => stored[key] ?? (scenario === 'astro-resume' && key === 'lumen-astro-job' ? '7' : null), setItem(key, value) { stored[key] = value; }, removeItem(key) { delete stored[key]; } };
const photo = id => ({ id, name: 'photo-' + id + '.jpg', total_score: 0.8, preview_url: '/preview/' + id, decision: 'none', rating: 0 });
const decisionPosts = [];
const decisions = { 1: 'none', 2: 'none' };
const ratings = { 1: 0, 2: 0 };
const group = (id, ids) => ({ id, automatic_pick_id: ids[0], frame_count: ids.length, rejected_count: 0, photos: ids.map(photo) });
const picks = [];
let groups = scenario === 'merge' || scenario === 'merge-batches' ? [group(1, [1]), group(2, [2])] : scenario.startsWith('astro') ? [group(1, [1, 2, 3])] : [group(1, [1, 2])];
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
const refinePosts = [];
let refinePolls = 0;
const refineJob = (status, progress, extra = {}) => ({ job_id: 13, kind: 'refine_previews', status, progress, label: null, processed: null, total: null, error: null, result: null, log_tail: [], created_at: '', updated_at: '', ...extra });
let groupLoads = 0;
let lastGroupsUrl = null;
let keeperOverride = null;
let groupsError = null;
let resumeJobs = [];
let importPolls = 0;
const tagPosts = [];
let tagPolls = 0;
const tagJob = (status, progress, extra = {}) => ({ job_id: 15, kind: 'tag', status, progress, label: null, processed: null, total: null, error: null, result: null, log_tail: [], created_at: '', updated_at: '', ...extra });
const removals = [];
const mergePosts = [];
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
  if (url === '/api/status') payload = { omlx_live: { summary: 'Ready' }, tools: { 'darktable-cli': { available: darktableReady }, 'siril-cli': { available: true } }, jpeg_output_dir: '/managed/outputs/jpeg', astro_output_dir: '/managed/outputs/astro', ingest_output_dir: '/managed/outputs/ingest' };
  else if (scenario.startsWith('workspace')) return httpFetch(apiBase + url, options);
  else if (groupsError && url.startsWith('/api/groups')) { lastGroupsUrl = url; ok = false; payload = { error: groupsError }; groupsError = null; }
  else if (url === '/api/groups' || url.startsWith('/api/groups?')) {
    groupLoads += 1; lastGroupsUrl = url;
    const keepers = groups.reduce((count, item) => count + item.photos.filter(p => p.decision !== 'reject' && (p.decision === 'flag' || p.id === item.automatic_pick_id)).length, 0);
    const frames = groups.reduce((count, item) => count + item.photos.length, 0);
    payload = { groups, photo_count: groups.reduce((count, item) => count + item.frame_count, 0), rejected_count: groups.reduce((count, item) => count + item.rejected_count, 0), counts: { filtered: frames, total: frames }, cameras: ['ILCE-7M4:1', 'ILCE-7RV:2'], keeper_count: keeperOverride ?? keepers };
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
  else if (url === '/api/import') { assert.deepEqual(JSON.parse(options.body), { path: '/Volumes/Card' }); payload = importJob('queued', 0); }
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
  else if (/^\/api\/photos\/[12]\/(decision|rating)$/.test(url)) {
    assert.equal(options.method, 'POST');
    const id = Number(url.split('/')[3]);
    const body = JSON.parse(options.body);
    decisionPosts.push({ url, body });
    if ('decision' in body) decisions[id] = body.decision; else ratings[id] = body.rating;
    payload = { photo_id: id, decision: decisions[id], rating: ratings[id], group_id: 1, automatic_pick_id: decisions[1] === 'reject' ? 2 : 1 };
  }
  else if (/^\/api\/photos\/[123]$/.test(url)) payload = { metrics: { sharpness: 0.8, exposure: 0.8, eye_quality: null }, tags: null, warnings: scenario === 'raw-warning' && url.endsWith('/2') ? ['Sony reduced-size lossless raw; darktable cannot decode it'] : [] };
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
  } else if (url === '/api/browse') payload = { path: browsePaths.shift() || '/cal/darks', cancelled: false };
  else if (url === '/api/ingest') { ingestPosts.push(JSON.parse(options.body)); payload = ingestJob('queued', 0); }
  else if (url === '/api/jobs/11') {
    ingestPolls += 1;
    payload = ingestPolls < 3 ? ingestJob('running', 0.25, { label: 'DSC00001.ARW', processed: 1, total: 4, ingest: ingestCounts({ copied: 1, verified: 1 }) })
      : ingestJob('succeeded', 1, { label: 'DSC00004.ARW', processed: 4, total: 4, ingest: ingestCounts(ingestOutcome) });
  }
  else if (url === '/api/astro/jobs') { assert.equal(options.method, 'POST'); astroPosts.push(JSON.parse(options.body)); payload = jobPayload('queued', 0); }
  else if (url === '/api/astro/jobs/7/run') { assert.deepEqual(JSON.parse(options.body), {}); runs += 1; payload = jobPayload('queued', 0); }
  else if (url === '/api/astro/jobs/7/cancel') { assert.deepEqual(JSON.parse(options.body), {}); cancels += 1; payload = jobPayload('cancelled', 0.5); }
  else if (url === '/api/astro/jobs/7/reveal') { assert.deepEqual(JSON.parse(options.body), {}); payload = { path: '/managed/outputs/astro/7' }; }
  else if (url === '/api/astro/jobs/7' || url === '/api/jobs/7') {
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
    assert.equal(element('#cancel-job').disabled, false);
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
    assert.equal(element('#cancel-job').disabled, true);
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
    element('#show-rejected').checked = true;
    await element('#show-rejected').listeners.change();
    assert.equal(groupLoads, loadsBefore + 1, 'the hide-rejected toggle reloads with the rejected frames');
    assert.equal(lastGroupsUrl, '/api/groups?show_rejected=1');
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
    for (const name of ['#import-path', '#clear-confirmation']) {
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
      await key('Enter', card(2));
      assert.equal(dialog.hidden, false, 'Enter opens the loupe for the focused frame');
      assert.equal(element('#loupe-heading').textContent, 'photo-2.jpg');
      const viewport = element('#loupe-viewport');
      assert.equal(viewport.getAttribute('aria-label'), 'photo-2.jpg viewport');
      const image = viewport.children[0];
      assert.equal(image.src, '/preview/2');
      Object.assign(image, { naturalWidth: 1600, naturalHeight: 1200 });
      image.listeners.load();
      assert.deepEqual([image.width, image.height], [1600, 1200], 'the loupe shows the stored preview at 100 percent');
      assert.equal(focused, viewport);
      Object.assign(viewport, { scrollTop: 0, scrollLeft: 0, tagName: 'DIV' });
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
    assert.deepEqual(days().map(day => [day.tagName, day.open, day.children[0].tagName]), [['DETAILS', true, 'SUMMARY'], ['DETAILS', true, 'SUMMARY'], ['DETAILS', true, 'SUMMARY']]);
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
    element('#filter-decision').value = 'flag';
    element('#filter-rating').value = '2';
    element('#filter-keepers').checked = true;
    element('#filter-tagged').checked = true;
    element('#filter-text').value = ' red fox ';
    groups = groups.slice(0, 3);
    await element('#filter-form').listeners.submit({ preventDefault() {} });
    const query = 'date_from=2026-10-21&date_to=2026-10-22&camera=ILCE-7M4%3A1&decision=flag&min_rating=2&keepers_only=1&tagged_only=1&text=red+fox';
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

    for (const name of ['#filter-from', '#filter-to', '#filter-camera', '#filter-decision', '#filter-rating', '#filter-text']) element(name).value = '';
    element('#filter-keepers').checked = false;
    element('#filter-tagged').checked = false;
    await load();
    assert.equal(groupLoads, loadsBefore + 1);
    assert.equal(lastGroupsUrl, '/api/groups?' + query, 'a reload keeps the filters from the URL');
    assert.deepEqual(['#filter-from', '#filter-to', '#filter-camera', '#filter-decision', '#filter-rating', '#filter-text'].map(name => element(name).value),
      ['2026-10-21', '2026-10-22', 'ILCE-7M4:1', 'flag', '2', 'red fox']);
    assert.deepEqual([element('#filter-keepers').checked, element('#filter-tagged').checked, element('#show-rejected').checked], [true, true, false]);

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
    assert.deepEqual(['#filter-from', '#filter-camera', '#filter-decision', '#filter-rating', '#filter-text'].map(name => element(name).value), ['', '', 'any', '0', '']);
    assert.equal(element('#filter-keepers').checked, false);
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
    element('#show-rejected').checked = true;
    await element('#show-rejected').listeners.change();
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
    element('#ingest-tab').listeners.click();
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
    assert.deepEqual(rows.map(row => [row.children[0].src, row.children[0].alt, row.children[0].loading]), [['/preview/1', '', 'lazy'], ['/preview/3', '', 'lazy']]);
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

    const collapse = element('#collapse-panel');
    collapse.listeners.click();
    assert.equal(element('#workflow-panels').hidden, true);
    assert.equal(collapse.getAttribute('aria-pressed'), 'true');
    assert.equal(stored['lumen-panel-collapsed'], '1');
    collapse.listeners.click();
    assert.equal(element('#workflow-panels').hidden, false);
    assert.equal(stored['lumen-panel-collapsed'], undefined);
    collapse.listeners.click();

    const astro = tree(sections()[0]).find(node => node.tagName === 'BUTTON' && node.textContent === 'Astro');
    await astro.listeners.click();
    assert.equal(state.selectedGroupId, 1);
    assert.equal(element('#astro-tab').getAttribute('aria-selected'), 'true');
    assert.equal(element('#workflow-panels').hidden, false, 'choosing a tab expands a collapsed panel');
    const remove = tree(sections()[1]).find(node => node.tagName === 'BUTTON' && node.textContent === 'Remove batch');
    await remove.listeners.click();
    assert.deepEqual(removals, ['/api/groups/2/remove']);
    assert.equal(sections().length, 1);
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
    await button.listeners.click();
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
    const batches = document.querySelectorAll('.batch-button');
    assert.deepEqual(batches.map(item => item.getAttribute('aria-pressed')), scenario === 'split' ? ['false', 'true'] : ['true']);
    await element('#remove-group').listeners.click();
    assert.deepEqual(removals, ['/api/groups/' + id + '/remove']);
  }
})()
`;
vm.runInNewContext(harness + fs.readFileSync(process.argv[1], 'utf8') + regression, {
  assert: require('node:assert/strict'), URLSearchParams, setImmediate, scenario: process.argv[2], apiBase: process.argv[3], httpFetch: globalThis.fetch,
}).catch(error => { console.error(error); process.exitCode = 1; });
"""
        app = Path(__file__).resolve().parents[1] / "src" / "lumen" / "static" / "app.js"
        result = subprocess.run(["node", "-e", script, str(app), scenario, self.base], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_javascript_decisions_update_cards_in_place_and_keeper_count(self):
        self.run_javascript_cleanup_regression("decisions")

    def test_javascript_filters_write_url_group_days_and_page_frames(self):
        self.run_javascript_cleanup_regression("filters")

    def test_javascript_all_rejected_burst_explains_no_keeper_and_offers_pick(self):
        self.run_javascript_cleanup_regression("all-rejected")

    def test_javascript_culling_keys_move_focus_and_decide_in_place(self):
        self.run_javascript_cleanup_regression("culling-keys")

    def test_javascript_compare_syncs_panes_and_decides_in_place(self):
        self.run_javascript_cleanup_regression("compare")

    def test_javascript_loupe_opens_on_enter_pans_and_returns_focus(self):
        self.run_javascript_cleanup_regression("loupe")

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
            form = next(node for node in parser.nodes if node["tag"] == "form" and dialog in node["parents"])
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
        for key in ("P", "X", "F", "U", "C", "0", "5", "Home", "End"):
            self.assertIn(key, keys)
        for key in keys:
            self.assertTrue(len(key) == 1 or key in {"Home", "End", "Page Up", "Page Down", "Enter", "Esc"}, key)
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
            "?keepers_only=true": "keepers_only", "?tagged_only=2": "tagged_only", "?show_rejected=yes": "show_rejected",
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
                    "filter-decision": ("select", None), "filter-rating": ("select", None), "filter-keepers": ("input", "checkbox"),
                    "filter-tagged": ("input", "checkbox"), "show-rejected": ("input", "checkbox"), "filter-text": ("input", "search")}
        for name, (tag, kind) in controls.items():
            with self.subTest(control=name):
                node = by_id[name]
                self.assertEqual((tag, kind), (node["tag"], node["attrs"].get("type")))
                self.assertIn(form, node["parents"])
                self.assertTrue(labels.get(name), f"{name} needs a visible label")
        options = lambda name: [node["attrs"]["value"] for node in parser.nodes if node["tag"] == "option" and by_id[name] in node["parents"]]
        self.assertEqual(["any", "flag", "reject", "none"], options("filter-decision"))
        self.assertEqual(["0", "1", "2", "3", "4", "5"], options("filter-rating"))
        buttons = {node["text"].strip(): node["attrs"].get("type") for node in parser.nodes if node["tag"] == "button" and form in node["parents"]}
        self.assertEqual({"Apply": "submit", "Reset": "button"}, buttons)
        more = by_id["show-more"]
        self.assertEqual(("button", "button"), (more["tag"], more["attrs"].get("type")))
        self.assertIn("hidden", more["attrs"])
        self.assertNotIn(by_id["contact-sheet"], more["parents"])
        javascript = (Path(__file__).resolve().parents[1] / "src" / "lumen" / "static" / "app.js").read_text()
        for text in ("createElement('details')", "createElement('summary')", "open = true", "history.replaceState", "location.search",
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
        toggle = by_id["show-rejected"]
        self.assertEqual(("input", "checkbox"), (toggle["tag"], toggle["attrs"].get("type")))
        self.assertTrue(any(node["tag"] == "label" and "Show rejected" in node["text"] for node in toggle["parents"]))
        javascript = (Path(__file__).resolve().parents[1] / "src" / "lumen" / "static" / "app.js").read_text()
        for text in ("'Flag'", "'Reject'", "'Clear decision'", "'Flagged'", "'Rejected'", "`Rating ${", "createElement('select')",
                     "id = 'rating'", "htmlFor = 'rating'", "`Rating ${result.rating} set for ${", "'show_rejected'"):
            self.assertIn(text, javascript)
        self.assertNotRegex(javascript, r"decision[^\n]*\.innerHTML")
        styles = (Path(__file__).resolve().parents[1] / "src" / "lumen" / "static" / "styles.css").read_text()
        self.assertRegex(styles, r"button, input, select \{[^}]*min-height: 44px")
        self.assertRegex(styles, r"select:focus-visible")

    def test_javascript_astro_tab_confirms_includes_polls_and_retains_failure_context(self):
        for scenario in ("astro", "astro-fail", "astro-cancel", "astro-resume"):
            with self.subTest(scenario=scenario):
                self.run_javascript_cleanup_regression(scenario)

    def test_javascript_batch_rail_merges_selected_batches(self):
        self.run_javascript_cleanup_regression("merge-batches")

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

    def test_javascript_contact_sheet_groups_frames_into_burst_sections(self):
        self.run_javascript_cleanup_regression("bursts")

    def test_layout_is_full_height_with_collapse_toggle_burst_sections_and_rail_merge_controls(self):
        html = urllib.request.urlopen(self.base + "/", timeout=3).read().decode()
        parser = LandmarkParser()
        parser.feed(html)
        by_id = {node["attrs"]["id"]: node for node in parser.nodes if "id" in node["attrs"]}
        tablist = next(node for node in parser.nodes if node["attrs"].get("role") == "tablist")
        collapse = by_id["collapse-panel"]
        self.assertEqual(("button", "button", "false"), (collapse["tag"], collapse["attrs"].get("type"), collapse["attrs"].get("aria-pressed")))
        self.assertEqual("Collapse panel", collapse["text"].strip())
        self.assertEqual(tablist["parents"][-1], collapse["parents"][-1], "the toggle sits next to the tab rail")
        self.assertEqual("tabpanel", by_id["compose-panel"]["attrs"]["role"])
        self.assertIn(by_id[collapse["attrs"]["aria-controls"]], by_id["compose-panel"]["parents"])
        nav = next(node for node in parser.nodes if node["tag"] == "nav")
        aside = next(node for node in parser.nodes if node["tag"] == "aside")
        header = by_id["rail-header"]
        self.assertIn(nav, header["parents"])
        for name in ("merge-batches", "merge-count", "jump-selected"):
            self.assertIn(header, by_id[name]["parents"], f"{name} lives in the sticky rail header")
        self.assertIn(aside, by_id["back-to-top"]["parents"])
        for name in ("collapse-panel", "merge-batches", "jump-selected", "back-to-top"):
            node = by_id[name]
            self.assertEqual(("button", "button"), (node["tag"], node["attrs"].get("type")))
            self.assertTrue(node["text"].strip() or node["attrs"].get("aria-label"), f"{name} needs an accessible name")
        styles = (Path(__file__).resolve().parents[1] / "src" / "lumen" / "static" / "styles.css").read_text()
        self.assertRegex(styles, r"\nbody \{[^}]*overflow: hidden")
        self.assertRegex(styles, r"\.masthead \{[^}]*position: sticky")
        self.assertRegex(styles, r"\.batch-rail, main, \.inspector \{[^}]*overflow-y: auto")
        self.assertRegex(styles, r"\.main-top \{[^}]*position: sticky")
        self.assertRegex(styles, r"\.rail-header \{[^}]*position: sticky")
        self.assertRegex(styles, r'\.burst\[data-selected="true"\] \{[^}]*outline: 2px solid var\(--amber\)')
        self.assertRegex(styles, r"\.batch-thumb \{[^}]*width: 44px")
        self.assertIn("prefers-reduced-motion", styles)
        javascript = (Path(__file__).resolve().parents[1] / "src" / "lumen" / "static" / "app.js").read_text()
        for text in ("createElement('section')", "className = 'burst'", "'aria-labelledby'", "block: 'start'", "prefers-reduced-motion",
                     "lumen-panel-collapsed", "'Selected'", "'Select for merge'", "'Remove batch'", "'Astro'", "toLocaleTimeString", "toLocaleString"):
            self.assertIn(text, javascript)
        self.assertNotRegex(javascript, r"burst[^\n]*\.innerHTML")

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
