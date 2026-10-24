import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageCms

from lumen.cli import main
from lumen.config import Settings
from lumen.diagnostics import doctor_report
from lumen.library import Library
from lumen.omlx import AstroTriage, BurstCritique, Caption
from lumen.pipelines import DarktablePipeline, PipelineResult
from lumen.tools import ToolStatus

try:
    from tests.test_astro import FAKE_SIRIL, SIRIL
    from tests.test_catalog import seed_filter_photos
    from tests.test_previews import write_fake_darktable
except ModuleNotFoundError:  # discovered with -s tests
    from test_astro import FAKE_SIRIL, SIRIL
    from test_catalog import seed_filter_photos
    from test_previews import write_fake_darktable


class CliTests(unittest.TestCase):
    def test_import_command_returns_machine_readable_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "photo.jpg"
            Image.new("RGB", (100, 80), (100, 120, 140)).save(source)
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["--library", str(root / "library"), "import", str(source), "--json"])

        self.assertEqual(0, code)
        payload = json.loads(output.getvalue())
        self.assertEqual(1, payload["result"]["imported"])
        self.assertEqual(0, payload["result"]["failed"])

    def run_json(self, library_dir, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--library", str(library_dir), *args, "--json"])
        return code, json.loads(output.getvalue())

    def test_ingest_command_exit_code_follows_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            card = root / "CARD" / "DCIM"
            card.mkdir(parents=True)
            for index in range(2):
                Image.new("RGB", (60, 40), (index * 90, 20, 20)).save(card / f"DSC000{index}.JPG")
            primary, mirror = root / "primary", root / "mirror"
            primary.mkdir()
            mirror.mkdir()
            separate_volumes = patch("lumen.ingest._device", side_effect=str)  # tests share one temp volume
            separate_volumes.start()
            self.addCleanup(separate_volumes.stop)
            code, verified = self.run_json(root / "library", "ingest", str(root / "CARD"), "--to", str(primary), "--mirror", str(mirror))
            from lumen import ingest

            def mirror_fails(source, destination, **kwargs):
                if destination.is_relative_to(mirror.resolve()):
                    raise ingest.IngestVerifyError("mirror copy did not verify")
                return real(source, destination, **kwargs)

            real = ingest.copy_verified
            for path in list(mirror.rglob("*.JPG")):
                path.unlink()
            with patch("lumen.ingest.copy_verified", side_effect=mirror_fails):
                failed_code, failed = self.run_json(root / "library", "ingest", str(root / "CARD"), "--to", str(primary), "--mirror", str(mirror))
            missing_code, missing = self.run_json(root / "library", "ingest", str(root / "missing"), "--to", str(primary))

        self.assertEqual((0, "succeeded", "ingest"), (code, verified["status"], verified["kind"]))
        self.assertEqual((True, 2), (verified["ingest"]["card_verified_everywhere"], verified["ingest"]["verified"]))
        self.assertEqual(2, failed_code)
        self.assertEqual((False, 2, 0), (failed["ingest"]["card_verified_everywhere"], failed["ingest"]["failed"], failed["ingest"]["verified"]))
        self.assertEqual(2, missing_code)
        self.assertIn("error", missing)
        for payload in (verified, failed, missing):
            self.assertNotIn(str(root), json.dumps(payload))

    def test_job_commands_explain_a_catalog_held_by_lumen_serve(self):
        from lumen.jobs import LocalJobRunner

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "library"
            (root / "CARD").mkdir()
            (root / "primary").mkdir()
            runner = LocalJobRunner(Settings(library_dir=library).database_path)  # what `lumen serve` holds
            self.addCleanup(runner.shutdown)
            separate_volumes = patch("lumen.ingest._device", side_effect=str)  # tests share one temp volume
            separate_volumes.start()
            self.addCleanup(separate_volumes.stop)
            live = {"version": None, "default_model": None, "loaded_models": ["Qwen3-VL-8B-Instruct-4bit"]}
            loaded_model = patch("lumen.library.get_server_status", return_value=live)  # tag reaches tagger_for before the lock
            loaded_model.start()
            self.addCleanup(loaded_model.stop)
            for args in (["import", str(root / "CARD")], ["tag", "--all"], ["refine-previews", "--all"],
                         ["ingest", str(root / "CARD"), "--to", str(root / "primary")]):
                with self.subTest(command=args[0]):
                    code, payload = self.run_json(library, *args)
                    self.assertEqual(2, code)
                    self.assertEqual({"error": "the catalog is in use by lumen serve: stop the server or use the web page"}, payload)

    def test_import_waits_for_job_and_reports_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "Card"
            folder.mkdir()
            for index in range(2):
                Image.new("RGB", (60, 40), (index * 90, 20, 20)).save(folder / f"DSC000{index}.jpg")
            code, first = self.run_json(root / "library", "import", str(folder))
            again_code, again = self.run_json(root / "library", "import", str(folder))
            missing_code, missing = self.run_json(root / "library", "import", str(root / "missing"))

        self.assertEqual((0, "succeeded", "import"), (code, first["status"], first["kind"]))
        self.assertEqual({"discovered": 2, "imported": 2, "updated": 0, "skipped": 0, "failed": 0}, first["result"])
        self.assertEqual((2, 2), (first["processed"], first["total"]))
        self.assertEqual((0, 2), (again_code, again["result"]["skipped"]))
        self.assertEqual(2, missing_code)
        self.assertIn("does not exist", missing["error"])
        for payload in (first, again, missing):
            self.assertNotIn(str(root), json.dumps(payload))

    def test_tag_all_retag_jobs_and_cancel_commands(self):
        from lumen.omlx import TagResult

        class FakeTagger:
            calls = 0

            def __init__(self, *args, **kwargs):
                self.model, self.role = "test-vlm", kwargs.get("role")

            def tag(self, preview, *, vocabulary=None):
                FakeTagger.calls += 1
                return TagResult("A frame", ["frame"], "room", [], 0.9, "{}")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library_dir = root / "library"
            source = root / "photo.jpg"
            Image.new("RGB", (60, 40), (1, 2, 3)).save(source)
            self.run_json(library_dir, "import", str(source))
            live = {"version": None, "default_model": None, "loaded_models": ["Qwen3-VL-8B-Instruct-4bit"]}
            with patch("lumen.library.OmlxTagger", FakeTagger), patch("lumen.library.get_server_status", return_value=live):
                tag_code, tagged = self.run_json(library_dir, "tag", "--all")
                skip_code, skipped = self.run_json(library_dir, "tag", "--all")
                retag_code, retagged = self.run_json(library_dir, "tag", "--all", "--retag")
            jobs_code, jobs = self.run_json(library_dir, "jobs")
            with Library(Settings(library_dir=library_dir)) as library:
                queued = library.catalog.create_job(kind="tag", manifest_path="/q/manifest.json", log_path="/q/job.log")
            cancel_code, cancelled = self.run_json(library_dir, "cancel", str(queued.id))
            done_code, done = self.run_json(library_dir, "cancel", str(tagged["job_id"]))
            missing_code, missing = self.run_json(library_dir, "cancel", "999")

        self.assertEqual((0, "tag", {"tagged": 1, "skipped": 0, "failed": 0}), (tag_code, tagged["kind"], tagged["result"]))
        self.assertEqual((0, {"tagged": 0, "skipped": 1, "failed": 0}), (skip_code, skipped["result"]))
        self.assertEqual((0, {"tagged": 1, "skipped": 0, "failed": 0}), (retag_code, retagged["result"]))
        self.assertEqual(2, FakeTagger.calls)
        self.assertEqual(0, jobs_code)
        self.assertEqual([retagged["job_id"], skipped["job_id"], tagged["job_id"]], [job["job_id"] for job in jobs["jobs"][:3]])
        self.assertEqual((0, "cancelled"), (cancel_code, cancelled["status"]))
        self.assertEqual((2, "succeeded"), (done_code, done["status"]))
        self.assertEqual(2, missing_code)
        self.assertIn("error", missing)
        self.assertNotIn(str(root), json.dumps(jobs))

    def test_doctor_reports_required_and_optional_capabilities(self):
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["--library", directory, "doctor", "--json"])

        self.assertIn(code, (0, 2))
        payload = json.loads(output.getvalue())
        self.assertEqual("Qwen3.8-27B-4bit", payload["omlx_model"]["name"])
        self.assertIn("exiftool", payload["tools"])
        self.assertIn("darktable-cli", payload["tools"])

    def test_siril_command_runs_managed_script_hook(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "library" / "outputs" / "stack.ssf"
            script.parent.mkdir(parents=True)
            script.write_text("requires 1.4.0\n")

            class FakeSiril:
                def run(self, path):
                    return PipelineResult(True, 0, path, ("siril-cli", "-s", str(path)), "ok")

            output = io.StringIO()
            with patch("lumen.cli.PipelineRegistry.siril", return_value=FakeSiril()):
                with redirect_stdout(output):
                    code = main(["--library", str(root / "library"), "siril", str(script), "--json"])

        self.assertEqual(0, code)
        self.assertTrue(json.loads(output.getvalue())["success"])

    def test_doctor_recommendation_does_not_advertise_missing_flag(self):
        missing = ToolStatus(name="tool", available=False, path=None, version=None)
        with tempfile.TemporaryDirectory() as directory, patch(
            "lumen.diagnostics._find_model", return_value=None
        ), patch("lumen.diagnostics.tool_status", return_value=missing):
            report = doctor_report(Settings(library_dir=Path(directory)))

        self.assertNotIn("--vision-test", report["omlx_model"]["recommendation"])

    def test_doctor_distinguishes_loaded_model_from_lumen_target(self):
        missing = ToolStatus(name="tool", available=False, path=None, version=None)
        live = {
            "version": "0.7.2",
            "default_model": "Qwen3.6-35B-A3B-4bit",
            "loaded_models": ["Qwen3.5-9B-4bit"],
        }
        with tempfile.TemporaryDirectory() as directory, patch(
            "lumen.diagnostics._find_model", return_value=None
        ), patch("lumen.diagnostics.tool_status", return_value=missing), patch(
            "lumen.diagnostics.get_server_status", return_value=live, create=True
        ):
            report = doctor_report(Settings(library_dir=Path(directory)))

        self.assertIn("omlx_live", report)
        self.assertTrue(report["omlx_live"]["online"])
        self.assertEqual(["Qwen3.5-9B-4bit"], report["omlx_live"]["loaded_models"])
        self.assertFalse(report["omlx_live"]["target_loaded"])
        self.assertEqual(
            "Loaded: Qwen3.5-9B-4bit · Lumen target: Qwen3.8-27B-4bit",
            report["omlx_live"]["summary"],
        )

    def test_process_refuses_to_overwrite_any_catalog_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.jpg"
            second = root / "second.jpg"
            Image.new("RGB", (40, 40), (1, 2, 3)).save(first)
            Image.new("RGB", (40, 40), (4, 5, 6)).save(second)
            library_dir = root / "library"
            main(["--library", str(library_dir), "import", str(first), "--json"])
            main(["--library", str(library_dir), "import", str(second), "--json"])

            original_bytes = second.read_bytes()
            with self.assertRaisesRegex(ValueError, "catalog original"):
                main(
                    [
                        "--library",
                        str(library_dir),
                        "process",
                        "1",
                        "--output",
                        str(second),
                    ]
                )
            self.assertEqual(original_bytes, second.read_bytes())


    def test_groups_filter_flags_narrow_output(self):
        with tempfile.TemporaryDirectory() as directory:
            library_dir = Path(directory) / "library"
            with Library(Settings(library_dir=library_dir)) as library:
                ids, _ = seed_filter_photos(library.catalog)
            names = {photo_id: name for name, photo_id in ids.items()}

            def shown(*flags):
                code, payload = self.run_json(library_dir, "groups", *flags)
                self.assertEqual(0, code, payload)
                return sorted(names[photo["id"]] for group in payload["groups"] for photo in group["photos"])

            self.assertEqual(["a1", "a2", "b2", "c1"], shown())
            self.assertEqual(["a1", "a2", "a3", "b1", "b2", "c1"], shown("--show-rejected"))
            self.assertEqual(["b2"], shown("--from", "2026-10-22", "--to", "2026-10-22"))
            self.assertEqual(["b2", "c1"], shown("--camera", "B"))
            self.assertEqual(["a3", "b1"], shown("--decision", "reject"))
            self.assertEqual(["a1", "b2"], shown("--min-rating", "3"))
            self.assertEqual(["a1", "a2", "c1"], shown("--keepers"))
            self.assertEqual(["a1", "b2"], shown("--tagged"))
            self.assertEqual(["b2"], shown("--text", "heron"))
            self.assertEqual(["a1", "a2"], shown("--camera", "A", "--keepers", "--from", "2026-10-21"))
            code, payload = self.run_json(library_dir, "groups", "--from", "2026-10-22", "--to", "2026-10-21")
            self.assertEqual((2, {"error": "invalid filter: date_to"}), (code, payload))
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(["--library", str(library_dir), "groups", "--decision", "maybe"])


    def test_refine_previews_command_waits_and_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "Card"
            folder.mkdir()
            for index in range(2):
                Image.new("RGB", (60, 40), (index * 90, 20, 20)).save(folder / f"DSC000{index}.jpg")
            library_dir = root / "library"
            self.run_json(library_dir, "import", str(folder))
            with Library(Settings(library_dir=library_dir)) as library:
                group_id = library.list_groups()[0]["id"]
                library.set_rating(library.list_groups()[0]["photos"][0]["id"], 4)
            darktable = write_fake_darktable(root)
            with patch("lumen.library.find_tool", return_value=darktable):
                code, grouped = self.run_json(library_dir, "refine-previews", "--group", str(group_id))
                filtered_code, filtered = self.run_json(library_dir, "refine-previews", "--min-rating", "4")
                all_code, everything = self.run_json(library_dir, "refine-previews", "--all")
                for flags in ((), ("--all", "--group", "1"), ("--all", "--min-rating", "4"), ("--group", "1", "--camera", "A")):
                    with self.subTest(flags=flags):
                        if flags[:2] == ("--all", "--group"):
                            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                                main(["--library", str(library_dir), "refine-previews", *flags])
                        else:
                            bad_code, bad = self.run_json(library_dir, "refine-previews", *flags)
                            self.assertEqual(2, bad_code)
                            self.assertIn("exactly one", bad["error"])
            with patch("lumen.library.find_tool", return_value=None):
                missing_code, missing = self.run_json(library_dir, "refine-previews", "--all")
            with Library(Settings(library_dir=library_dir)) as library:
                kinds = {photo.preview_kind for photo in library.catalog.list_photos()}
                fingerprints = [photo.fingerprint for photo in library.catalog.list_photos()]

        self.assertEqual((0, "succeeded", "refine_previews"), (code, grouped["status"], grouped["kind"]))
        self.assertEqual({"refined": grouped["total"], "skipped": 0, "failed": 0}, grouped["result"])
        self.assertEqual((0, 1), (filtered_code, filtered["total"]))
        self.assertEqual((0, {"refined": 2, "skipped": 0, "failed": 0}), (all_code, everything["result"]))
        self.assertEqual({"refined"}, kinds)
        self.assertEqual((2, {"error": "darktable-cli is not installed"}), (missing_code, missing))
        for payload in (grouped, filtered, everything):
            self.assertNotIn(str(root), json.dumps(payload))
            for fingerprint in fingerprints:
                self.assertNotIn(fingerprint, json.dumps(payload))
    def test_review_rejects_command_runs_job_and_reports_summary(self):
        from lumen.omlx import RejectReview

        class FakeTagger:
            def __init__(self, *args, **kwargs): self.model, self.role = "fast-model", kwargs.get("role", "review")
            def review_rejects(self, preview):
                return RejectReview([], False, 0.7, "{}")

        live = {"version": None, "default_model": None, "loaded_models": ["Qwen3-VL-8B-Instruct-4bit"]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new("RGB", (120, 90)).save(root / "p.jpg")
            with redirect_stdout(io.StringIO()):
                main(["--library", str(root / "library"), "import", str(root)])
            output = io.StringIO()
            with patch("lumen.library.OmlxTagger", FakeTagger), patch("lumen.library.get_server_status", return_value=live), redirect_stdout(output):
                code = main(["--library", str(root / "library"), "review-rejects", "--json"])
        self.assertEqual(0, code)
        # job_status exposes the manifest summary as result (see the tag --keepers test).
        self.assertEqual({"reviewed": 1, "skipped": 0, "failed": 0, "suggested": 0}, json.loads(output.getvalue())["result"])

    def test_assist_keepers_runs_the_combined_job(self):
        from lumen.omlx import Caption, RejectReview, TagResult

        class FakeTagger:
            calls = []
            def __init__(self, *args, **kwargs): self.model, self.role = "m", kwargs.get("role")
            def tag(self, preview, *, vocabulary=None): FakeTagger.calls.append("tag"); return TagResult("d", [], "", [], 0.9, "{}", species=[], conditions=[])
            def review_rejects(self, preview): FakeTagger.calls.append("review"); return RejectReview([], False, 0.8, "{}")
            def caption(self, preview): FakeTagger.calls.append("caption"); return Caption("c", "a", "{}")

        live = {"version": "0.7.2", "default_model": None, "loaded_models": ["Qwen3-VL-8B-Instruct-4bit", "Qwen3.8-27B-4bit"]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new("RGB", (120, 90)).save(root / "p.jpg")
            with redirect_stdout(io.StringIO()):
                main(["--library", str(root / "library"), "import", str(root)])
            output = io.StringIO()
            with patch("lumen.library.OmlxTagger", FakeTagger), patch("lumen.library.get_server_status", return_value=live), redirect_stdout(output):
                code = main(["--library", str(root / "library"), "assist", "--keepers", "--json"])
            self.assertEqual(0, code)
            self.assertEqual(["tag", "review", "caption"], FakeTagger.calls)
            self.assertEqual("succeeded", json.loads(output.getvalue())["status"])

    def test_tag_keepers_runs_job_and_doctor_reports_both_roles(self):
        from lumen.omlx import TagResult

        class FakeTagger:
            calls = 0
            def __init__(self, *args, **kwargs):
                self.model, self.role = "fast-model", kwargs.get("role", "review")
            def tag(self, preview, *, vocabulary=None):
                FakeTagger.calls += 1
                return TagResult("d", ["ice"], "", [], 0.9, "{}", species=[], conditions=[])

        live = {"version": "0.7.2", "default_model": None, "loaded_models": ["Qwen3-VL-8B-Instruct-4bit"]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(2):
                Image.new("RGB", (120, 90), (index, 0, 0)).save(root / f"p{index}.jpg")
            with redirect_stdout(io.StringIO()):
                main(["--library", str(root / "library"), "import", str(root)])
            output = io.StringIO()
            with patch("lumen.library.OmlxTagger", FakeTagger), patch("lumen.library.get_server_status", return_value=live), redirect_stdout(output):
                code = main(["--library", str(root / "library"), "tag", "--keepers", "--json"])
            self.assertEqual(0, code)
            self.assertEqual(2, FakeTagger.calls)
            self.assertEqual({"tagged": 2, "skipped": 0, "failed": 0}, json.loads(output.getvalue())["result"])  # job_status exposes the manifest summary as result
            output = io.StringIO()
            with patch("lumen.diagnostics.get_server_status", return_value=live), patch("lumen.diagnostics.tool_status", return_value=ToolStatus("t", False, None, None)), patch("lumen.diagnostics._find_model", return_value=None), redirect_stdout(output):
                main(["--library", str(root / "library"), "doctor", "--json"])
            roles = json.loads(output.getvalue())["omlx_roles"]
            self.assertEqual({"fast": {"model": "Qwen3-VL-8B-Instruct-4bit", "loaded": True}, "review": {"model": "Qwen3.8-27B-4bit", "loaded": False}}, roles)
            output = io.StringIO()
            with patch("lumen.diagnostics.get_server_status", return_value=live), patch("lumen.diagnostics.tool_status", return_value=ToolStatus("t", False, None, None)), patch("lumen.diagnostics._find_model", return_value=None), redirect_stdout(output):
                main(["--library", str(root / "library"), "doctor"])
            self.assertIn("fast: Qwen3-VL-8B-Instruct-4bit (loaded)", output.getvalue())
            self.assertIn("review: Qwen3.8-27B-4bit (not loaded)", output.getvalue())

    def test_triage_command_runs_job_and_prints_lights(self):
        class FakeTagger:
            def __init__(self, *args, **kwargs): self.model, self.role = "fast-model", kwargs.get("role", "review")
            def triage_astro(self, preview):
                return AstroTriage("aurora", 0.8, [], "{}")

        live = {"version": None, "default_model": None, "loaded_models": ["Qwen3-VL-8B-Instruct-4bit"]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(2):
                image = Image.new("RGB", (120, 90)); exif = Image.Exif(); exif[36867] = f"2026:10:22 22:00:{index * 10:02d}"; exif[33434] = 15
                image.save(root / f"l{index}.jpg", exif=exif)
            with redirect_stdout(io.StringIO()):
                main(["--library", str(root / "library"), "import", str(root)])
            output = io.StringIO()
            with patch("lumen.library.OmlxTagger", FakeTagger), patch("lumen.library.get_server_status", return_value=live), redirect_stdout(output):
                code = main(["--library", str(root / "library"), "triage", "1", "--json"])
        self.assertEqual(0, code)
        payload = json.loads(output.getvalue())
        self.assertEqual((2, True), (len(payload["lights"]), payload["astro_cue"]))

    def test_caption_command_prints_caption_and_alt_text(self):
        class FakeTagger:
            def __init__(self, *args, **kwargs): self.model, self.role = "review-model", kwargs.get("role", "review")
            def caption(self, preview):
                return Caption("A caption.", "Alt text.", "{}")

        live = {"version": None, "default_model": None, "loaded_models": ["Qwen3.8-27B-4bit"]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); Image.new("RGB", (120, 90)).save(root / "p.jpg")
            with redirect_stdout(io.StringIO()):
                main(["--library", str(root / "library"), "import", str(root / "p.jpg")])
            output = io.StringIO()
            with patch("lumen.library.OmlxTagger", FakeTagger), patch("lumen.library.get_server_status", return_value=live), redirect_stdout(output):
                code = main(["--library", str(root / "library"), "caption", "1", "--json"])
        self.assertEqual((0, "Alt text."), (code, json.loads(output.getvalue())["alt_text"]))

    def test_digest_command_writes_file_and_requires_overwrite_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = Image.new("RGB", (120, 90)); exif = Image.Exif(); exif[36867] = "2026:10:22 10:00:00"; image.save(root / "d.jpg", exif=exif)
            with redirect_stdout(io.StringIO()):
                main(["--library", str(root / "library"), "import", str(root / "d.jpg")])
            offline = {"version": None, "default_model": None, "loaded_models": []}
            output = io.StringIO()
            with patch("lumen.library.get_server_status", return_value=offline), redirect_stdout(output):
                code = main(["--library", str(root / "library"), "digest", "2026-10-22", "--json"])
            self.assertEqual((0, False), (code, json.loads(output.getvalue())["narrative"]))
            self.assertTrue((root / "library" / "digests" / "2026-10-22.md").exists())
            output = io.StringIO()
            with patch("lumen.library.get_server_status", return_value=offline), redirect_stdout(output):
                code = main(["--library", str(root / "library"), "digest", "2026-10-22", "--json"])
            self.assertEqual((2, "digest exists; confirm overwrite"), (code, json.loads(output.getvalue())["error"]))
            with redirect_stdout(io.StringIO()):
                self.assertEqual(2, main(["--library", str(root / "library"), "digest", "22-10-2026", "--json"]))

    def test_critique_command_prints_suggestion_and_exits_two_on_failure(self):
        class FakeTagger:
            def __init__(self, *args, **kwargs): self.model, self.role = "review-model", kwargs.get("role", "review")
            def critique_burst(self, frames):
                return BurstCritique(frames[0][0], "Sharpest.", 0.8, "{}")

        live = {"version": None, "default_model": None, "loaded_models": ["Qwen3.8-27B-4bit"]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(2):
                image = Image.new("RGB", (120, 90), (index * 30, 0, 0)); exif = Image.Exif(); exif[36867] = f"2026:10:22 20:00:0{index}"
                image.save(root / f"b{index}.jpg", exif=exif)
            with redirect_stdout(io.StringIO()):
                main(["--library", str(root / "library"), "import", str(root)])
            output = io.StringIO()
            with patch("lumen.library.OmlxTagger", FakeTagger), patch("lumen.library.get_server_status", return_value=live), redirect_stdout(output):
                code = main(["--library", str(root / "library"), "critique", "1", "--json"])
            self.assertEqual(0, code)
            self.assertEqual("Sharpest.", json.loads(output.getvalue())["rationale"])
            output = io.StringIO()
            with patch("lumen.library.get_server_status", return_value={"version": None, "default_model": None, "loaded_models": []}), redirect_stdout(output):
                code = main(["--library", str(root / "library"), "critique", "1", "--json"])
            self.assertEqual((2, "review model Qwen3.8-27B-4bit is not loaded in oMLX"), (code, json.loads(output.getvalue())["error"]))


class ExportCleanupCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = Settings(library_dir=self.root / "library")
        self.source = self.root / "photo.jpg"
        Image.new("RGB", (40, 30), "red").save(self.source)
        with Library(self.settings) as library:
            library.import_path(self.source)
            self.photo_id = library.catalog.list_photos()[0].id
            self.group_id = library.list_groups()[0]["id"]

    def run_command(self, *arguments):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--library", str(self.settings.library_dir), *arguments, "--json"])
        return code, json.loads(output.getvalue())

    def render(self, command, **kwargs):
        if command[1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "darktable test", "")
        Image.new("RGB", (40, 30), "red").save(
            command[-8], format="TIFF",
            icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes(),
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    def test_export_current_and_keepers_return_artifacts_and_distinct_paths(self):
        profile = self.root / "profile.xmp"
        profile.write_text("<xmp/>")
        pipeline = DarktablePipeline("darktable-cli", self.settings.output_dir)
        with patch("lumen.library.PipelineRegistry.darktable", return_value=pipeline), patch(
            "lumen.pipelines.subprocess.run", side_effect=self.render
        ):
            first_code, first = self.run_command("export", "--photo", str(self.photo_id), "--profile", str(profile))
            first_bytes = Path(first["results"][0]["output"]).read_bytes()
            second_code, second = self.run_command("export", "--keepers")
        self.assertEqual((0, 0), (first_code, second_code))
        one, two = first["results"][0], second["results"][0]
        self.assertEqual(self.photo_id, one["photo_id"])
        self.assertTrue(one["success"])
        self.assertEqual("photo-lumen.jpg", Path(one["output"]).name)
        self.assertEqual("photo-lumen-1.jpg", Path(two["output"]).name)
        self.assertEqual(first_bytes, Path(one["output"]).read_bytes())
        self.assertEqual(one["output"], one["artifact"]["path"])
        self.assertEqual(str(profile.resolve()), one["artifact"]["settings"]["profile"])
        datetime.fromisoformat(one["artifact"]["created_at"])

    def test_export_preset_flag(self):
        pipeline = DarktablePipeline("darktable-cli", self.settings.output_dir)
        with patch("lumen.library.PipelineRegistry.darktable", return_value=pipeline), patch(
            "lumen.pipelines.subprocess.run", side_effect=self.render
        ):
            share_code, share = self.run_command("export", "--photo", str(self.photo_id), "--preset", "share")
            full_code, full = self.run_command("export", "--photo", str(self.photo_id))
        self.assertEqual((0, 0), (share_code, full_code))
        self.assertEqual("photo-lumen-share.jpg", Path(share["results"][0]["output"]).name)
        self.assertEqual("share", share["results"][0]["artifact"]["settings"]["preset"])
        self.assertEqual("photo-lumen.jpg", Path(full["results"][0]["output"]).name)
        self.assertEqual("full", full["results"][0]["artifact"]["settings"]["preset"])
        with self.assertRaises(SystemExit) as caught:
            self.run_command("export", "--photo", str(self.photo_id), "--preset", "x")
        self.assertEqual(2, caught.exception.code)

    def test_export_web_preset_with_watermark(self):
        pipeline = DarktablePipeline("darktable-cli", self.settings.output_dir)
        with patch("lumen.library.PipelineRegistry.darktable", return_value=pipeline), patch(
            "lumen.pipelines.subprocess.run", side_effect=self.render
        ):
            code, payload = self.run_command("export", "--keepers", "--preset", "web", "--watermark", "x")
            bad_code, bad = self.run_command("export", "--keepers", "--preset", "web", "--watermark", "  ")
        self.assertEqual(0, code, payload)
        settings = payload["results"][0]["artifact"]["settings"]
        self.assertEqual(("web", True), (settings["preset"], settings["watermark"]))
        self.assertEqual("photo-lumen-web.jpg", Path(payload["results"][0]["output"]).name)
        self.assertEqual((2, {"error": "watermark must be 1 to 80 printable characters"}), (bad_code, bad))

    def test_export_requires_exactly_one_selection(self):
        for arguments in (("export",), ("export", "--photo", "1", "--keepers")):
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit) as caught:
                self.run_command(*arguments)
            self.assertEqual(2, caught.exception.code)

    def test_export_unknown_photo_returns_json_error_and_exit_two(self):
        code, payload = self.run_command("export", "--photo", "99999")
        self.assertEqual(2, code)
        self.assertIn("unknown photo ID", payload["error"])
        self.assertFalse(self.settings.jpeg_output_dir.exists())

    def test_export_out_of_sqlite_range_photo_ids_return_json_without_export_effects(self):
        before = self.source.read_bytes()
        with patch("lumen.library.PipelineRegistry.darktable", side_effect=AssertionError("render attempted")):
            for photo_id in (9223372036854775808, -9223372036854775809):
                with self.subTest(photo_id=photo_id):
                    code, payload = self.run_command("export", "--photo", str(photo_id))
                    self.assertEqual(2, code)
                    self.assertEqual({"error"}, set(payload))
                    self.assertIsInstance(payload["error"], str)
                    self.assertTrue(payload["error"])
        self.assertFalse(self.settings.jpeg_output_dir.exists())
        self.assertEqual(before, self.source.read_bytes())
        with Library(self.settings) as library:
            self.assertEqual([], library.catalog.list_artifacts())
            self.assertFalse(any(event.kind == "exported" for event in library.catalog.events_for(self.photo_id)))

    def test_export_unavailable_darktable_reports_failed_item(self):
        with patch("lumen.library.PipelineRegistry.darktable", side_effect=RuntimeError("darktable-cli is unavailable")):
            code, payload = self.run_command("export", "--keepers")
        self.assertEqual(2, code)
        self.assertIn("darktable-cli", payload["error"])

    def test_keepers_reports_success_when_another_item_fails(self):
        second = self.root / "second.jpg"
        Image.new("RGB", (40, 30), "blue").save(second)
        with Library(self.settings) as library:
            library.import_path(second)
        pipeline = DarktablePipeline("darktable-cli", self.settings.output_dir)
        def render(command, **kwargs):
            if command[1:] != ["--version"] and Path(command[1]).name == "photo.jpg":
                return subprocess.CompletedProcess(command, 3, "", "bad render")
            return self.render(command, **kwargs)
        with patch("lumen.library.PipelineRegistry.darktable", return_value=pipeline), patch(
            "lumen.pipelines.subprocess.run", side_effect=render
        ):
            code, payload = self.run_command("export", "--keepers")
        self.assertEqual(2, code)
        self.assertEqual([False, True], [item["success"] for item in payload["results"]])
        self.assertIsNone(payload["results"][0]["output"])
        self.assertIsNone(payload["results"][0]["artifact"])
        self.assertTrue(Path(payload["results"][1]["output"]).is_file())

    def test_decide_and_rate_commands_persist_and_report_json(self):
        photo, group = self.photo_id, self.group_id
        self.assertEqual((0, {"photo_id": photo, "decision": "reject", "rating": 0, "group_id": group, "automatic_pick_id": photo}),
                         self.run_command("decide", str(photo), "--reject"))
        self.assertEqual((0, {"photo_id": photo, "decision": "reject", "rating": 4, "group_id": group, "automatic_pick_id": photo}),
                         self.run_command("rate", str(photo), "4"))
        self.assertEqual("flag", self.run_command("decide", str(photo), "--flag")[1]["decision"])
        self.assertEqual("none", self.run_command("decide", str(photo), "--clear")[1]["decision"])
        with Library(self.settings) as library:
            stored = library.catalog.get_photo(photo)
            kinds = [event.kind for event in library.catalog.events_for(photo) if event.kind.endswith("_changed")]
        self.assertEqual(("none", 4), (stored.decision, stored.rating))
        self.assertEqual(["decision_changed", "rating_changed", "decision_changed", "decision_changed"], kinds)
        code, payload = self.run_command("decide", "99999", "--flag")
        self.assertEqual((2, {"error": "unknown photo ID: 99999"}), (code, payload))
        for arguments in (("decide", str(photo)), ("decide", str(photo), "--flag", "--reject"), ("rate", str(photo), "6"), ("rate", str(photo), "-1")):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                self.run_command(*arguments)
            self.assertEqual(2, caught.exception.code)

    def test_remove_reports_recovery_paths_and_preserves_original(self):
        before = self.source.read_bytes()
        code, payload = self.run_command("remove", str(self.group_id))
        self.assertEqual(0, code)
        self.assertEqual(1, payload["photos_removed"])
        self.assertGreater(payload["files_removed"], 0)
        self.assertTrue(Path(payload["trash_path"]).is_dir())
        self.assertTrue(Path(payload["snapshot_path"]).is_file())
        self.assertEqual(before, self.source.read_bytes())
        with Library(self.settings) as library:
            self.assertEqual([], library.list_groups())

    def test_clear_requires_exact_confirmation_before_removing_photos(self):
        for confirmation in ("clear", "CLEAR "):
            with self.subTest(confirmation=confirmation):
                code, payload = self.run_command("clear", confirmation)
                self.assertEqual(2, code)
                self.assertIn("CLEAR", payload["error"])
                with Library(self.settings) as library:
                    self.assertEqual(1, len(library.catalog.list_photos()))
        code, payload = self.run_command("clear", "CLEAR")
        self.assertEqual(0, code)
        self.assertEqual(1, payload["photos_removed"])
        self.assertTrue(Path(payload["snapshot_path"]).is_file())

    def test_merge_joins_groups_into_the_earliest_and_rejects_single_ids(self):
        second = self.root / "second.jpg"
        Image.new("RGB", (40, 30), "blue").save(second)
        with Library(self.settings) as library:
            library.import_path(second)
            self.assertEqual(2, len(library.list_groups()))
        code, payload = self.run_command("merge", "2", "1")
        self.assertEqual(0, code)
        self.assertEqual((1, [2], 2), (payload["group_id"], payload["merged_group_ids"], payload["photo_count"]))
        with Library(self.settings) as library:
            self.assertEqual(1, len(library.list_groups()))
        code, payload = self.run_command("merge", "1")
        self.assertEqual(2, code)
        self.assertIn("error", payload)

    def test_remove_unknown_group_returns_json_error(self):
        code, payload = self.run_command("remove", "99999")
        self.assertEqual(2, code)
        self.assertIn("does not exist", payload["error"])

    def test_cleanup_sqlite_delete_failure_returns_json_and_restores_workspace(self):
        output = self.settings.output_dir / "completed.jpg"
        output.write_bytes(b"completed export")
        protected = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (self.source, output)}
        with Library(self.settings) as library:
            photo = library.catalog.get_photo(self.photo_id)
            library.catalog.save_artifact(photo.id, self.source.name, photo.fingerprint, "jpeg", str(output), {}, "output")
            managed = {path: path.read_bytes() for path in (Path(photo.preview_path), *library.sidecars.paths(photo))}
            library.catalog.connection.execute("CREATE TRIGGER fail_cleanup AFTER DELETE ON photos BEGIN SELECT RAISE(ABORT, 'cleanup deletion failed'); END")
            library.catalog.connection.commit()
        for arguments in (("remove", str(self.group_id)), ("clear", "CLEAR")):
            with self.subTest(arguments=arguments):
                code, payload = self.run_command(*arguments)
                self.assertEqual(2, code)
                self.assertIn("cleanup deletion failed", payload["error"])
                self.assertTrue(payload["recovery_complete"])
                self.assertEqual([], payload["restoration_errors"])
                recovery = Path(payload["recovery_path"])
                self.assertTrue(recovery.is_dir())
                self.assertEqual(recovery / "catalog.sqlite3", Path(payload["snapshot_path"]))
                self.assertTrue(Path(payload["snapshot_path"]).is_file())
                self.assertEqual(managed, {path: path.read_bytes() for path in managed})
                with Library(self.settings) as library:
                    self.assertIsNotNone(library.get_photo(photo.id)["metrics"])
                    self.assertEqual([photo.id], library.keeper_ids())
                    self.assertEqual(photo.id, library.catalog.list_artifacts()[0].photo_id)
        self.assertEqual(protected, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in protected})

    def test_cleanup_compound_move_restore_failure_reports_surviving_recovery_files(self):
        with Library(self.settings) as library:
            photo = library.catalog.get_photo(self.photo_id)
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
            code, payload = self.run_command("remove", str(self.group_id))
        self.assertEqual(2, code)
        self.assertIn("staging move failed", payload["error"])
        self.assertFalse(payload["recovery_complete"])
        self.assertTrue(any("preview restore failed" in error for error in payload["restoration_errors"]))
        recovery = Path(payload["recovery_path"])
        self.assertEqual(before, (recovery / "files" / "previews" / preview.name).read_bytes())
        self.assertTrue(Path(payload["snapshot_path"]).is_file())
        self.assertTrue(sidecar.is_file())
        with Library(self.settings) as library:
            self.assertIsNotNone(library.get_photo(photo.id)["metrics"])


if __name__ == "__main__":
    unittest.main()


class AstroCliTests(unittest.TestCase):
    def setUp(self):
        import sys
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)
        night = self.root / "Night"
        night.mkdir()
        for index in range(3):
            exif = Image.Exif()
            exif[34665] = {36867: f"2026:10:21 22:00:0{index}"}
            Image.new("RGB", (64, 48), (index, 0, 0)).save(night / f"light{index}.tif", exif=exif.tobytes())
        self.library_dir = self.root / "library"
        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(night)
            self.group = library.list_groups()[0]
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

    def run_cli(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--library", str(self.library_dir), "astro", *args, "--json"])
        return code, json.loads(output.getvalue())

    def test_prepare_requires_confirm_flag_and_reports_safe_status(self):
        code, payload = self.run_cli("prepare", str(self.group["id"]))
        self.assertEqual(2, code)
        self.assertIn("confirmation", payload["error"])
        empty = self.root / "darks"
        empty.mkdir()
        photo_id = self.group["photos"][0]["id"]
        code, payload = self.run_cli("prepare", str(self.group["id"]), "--confirm", "--exclude", str(photo_id), "--darks", str(empty))
        self.assertEqual(0, code)
        self.assertEqual(("queued", 2, 1), (payload["status"], payload["lights"], payload["excluded_lights"]))
        self.assertTrue(any("darks" in warning for warning in payload["warnings"]))
        self.assertNotIn(str(self.root), json.dumps(payload))
        code, status = self.run_cli("status", str(payload["job_id"]))
        self.assertEqual((0, payload), (code, status))

    def test_prepare_passes_the_recipe_through(self):
        code, payload = self.run_cli("prepare", str(self.group["id"]), "--recipe", "untracked", "--confirm")
        self.assertEqual((0, "untracked"), (code, payload["recipe"]))
        script = Settings(library_dir=self.library_dir).output_dir / "astro" / str(payload["job_id"]) / "stack.ssf"
        self.assertIn("register light -2pass\nseqapplyreg light -framing=min\n", script.read_text())
        _, payload = self.run_cli("prepare", str(self.group["id"]), "--confirm")
        self.assertEqual("tracked", payload["recipe"])

    def test_run_waits_for_completion_and_status_reports_result(self):
        _, prepared = self.run_cli("prepare", str(self.group["id"]), "--confirm")
        code, finished = self.run_cli("run", str(prepared["job_id"]))
        self.assertEqual(0, code)
        self.assertEqual(("succeeded", 1.0, "result.tif"), (finished["status"], finished["progress"], finished["result"]["name"]))
        self.assertNotIn(str(self.root), json.dumps(finished))
        code, again = self.run_cli("run", str(prepared["job_id"]))
        self.assertEqual(2, code)
        self.assertIn("error", again)

    def test_failed_run_exits_two_and_cancel_handles_queued_jobs(self):
        self.scenario = "nonzero"
        _, prepared = self.run_cli("prepare", str(self.group["id"]), "--confirm")
        code, finished = self.run_cli("run", str(prepared["job_id"]))
        self.assertEqual(2, code)
        self.assertEqual("failed", finished["status"])
        self.assertIn("exit code 3", finished["error"])
        _, queued = self.run_cli("prepare", str(self.group["id"]), "--confirm")
        code, cancelled = self.run_cli("cancel", str(queued["job_id"]))
        self.assertEqual((0, "cancelled"), (code, cancelled["status"]))
        code, missing = self.run_cli("cancel", "999")
        self.assertEqual(2, code)
        self.assertIn("error", missing)
        with patch("lumen.library.find_tool", return_value=None):
            code, payload = self.run_cli("prepare", str(self.group["id"]), "--confirm")
        self.assertEqual(2, code)
        self.assertIn("siril-cli", payload["error"])
