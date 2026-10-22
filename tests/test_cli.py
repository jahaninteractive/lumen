import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageCms

from lumen.cli import main
from lumen.config import Settings
from lumen.diagnostics import doctor_report
from lumen.library import Library
from lumen.pipelines import DarktablePipeline, PipelineResult
from lumen.tools import ToolStatus


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
        self.assertEqual(1, payload["imported"])
        self.assertEqual(0, payload["failed"])

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
