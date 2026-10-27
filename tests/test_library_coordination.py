import json
import subprocess
import sys
import tempfile
import threading
import unittest
from xml.etree import ElementTree
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageCms

from lumen.config import Settings
from lumen.library import ExportRequest, Library
from lumen.pipelines import DarktablePipeline


class LibraryCoordinationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "photo.jpg"
        Image.new("RGB", (40, 30), "red").save(self.source)
        self.settings = Settings(self.root / "library")
        with Library(self.settings) as library:
            library.import_path(self.source)
            self.photo_id = library.catalog.list_photos()[0].id
            self.group_id = library.list_groups()[0]["id"]

    def runner(self, command, **kwargs):
        if command[1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "darktable 5.2", "")
        Image.new("RGB", (40, 30), "red").save(command[-8], format="TIFF", icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
        return subprocess.CompletedProcess(command, 0, "", "")

    def pipeline(self, output_root, raw_developer=None):
        return DarktablePipeline("darktable-cli", output_root, raw_developer)

    def export(self, library):
        return library.export_jpegs(ExportRequest((self.photo_id,)))[0]

    def test_export_settings_events_and_sidecars_have_no_private_operation_token(self):
        with Library(self.settings) as library, patch("lumen.library.PipelineRegistry.darktable", side_effect=self.pipeline), patch("lumen.pipelines.subprocess.run", side_effect=self.runner):
            result = self.export(library)
            self.assertTrue(result.success, result.message)
            self.assertNotIn("operation_id", result.artifact.settings)
            event = next(event for event in library.catalog.events_for(self.photo_id) if event.kind == "exported")
            self.assertNotIn("operation_id", event.details)
            sidecar = library.sidecars.paths(library.catalog.get_photo(self.photo_id))[0].read_text()
            self.assertNotIn('"operation_id"', sidecar)
            with Image.open(result.output) as image:
                xmp = ElementTree.fromstring(image.info["xmp"])
                jpeg_settings = json.loads(xmp.find(".//{https://lumen.local/ns/1.0/}export").text)
            public = {key: value for key, value in result.artifact.settings.items() if key not in ("command", "source_fingerprint")}
            self.assertEqual(public, jpeg_settings)

    def test_insertion_failure_does_not_attempt_catalog_history_compensation(self):
        with Library(self.settings) as library, patch("lumen.library.PipelineRegistry.darktable", side_effect=self.pipeline), patch("lumen.pipelines.subprocess.run", side_effect=self.runner):
            first = self.export(library)
            with patch.object(library.catalog, "save_artifact", side_effect=OSError("insertion failed")), patch.object(library.catalog, "discard_export", wraps=library.catalog.discard_export) as discard:
                failed = self.export(library)
            self.assertFalse(failed.success)
            discard.assert_not_called()
            self.assertEqual([first.artifact], library.catalog.list_artifacts())
            self.assertEqual(1, sum(event.kind == "exported" for event in library.catalog.events_for(self.photo_id)))

    def test_later_event_or_sidecar_failure_removes_only_the_new_artifact_id(self):
        for failure in ("event", "sidecar"):
            with self.subTest(failure=failure), Library(self.settings) as library, patch("lumen.library.PipelineRegistry.darktable", side_effect=self.pipeline), patch("lumen.pipelines.subprocess.run", side_effect=self.runner):
                first = self.export(library)
                before_artifacts = library.catalog.list_artifacts()
                before_events = library.catalog.events_for(self.photo_id)
                target, method = (library.catalog, "append_event") if failure == "event" else (library, "_write_sidecar")
                original = getattr(target, method)
                raised = False
                def fail_after_write(*args, **kwargs):
                    nonlocal raised
                    result = original(*args, **kwargs)
                    if not raised:
                        raised = True
                        raise OSError(f"injected later {failure} failure")
                    return result
                with patch.object(target, method, side_effect=fail_after_write):
                    failed = self.export(library)
                self.assertFalse(failed.success)
                self.assertTrue(first.output.exists())
                self.assertEqual(before_artifacts, library.catalog.list_artifacts())
                self.assertEqual(before_events, library.catalog.events_for(self.photo_id))
                sidecar = json.loads(library.sidecars.paths(library.catalog.get_photo(self.photo_id))[0].read_text())
                self.assertEqual([artifact.id for artifact in before_artifacts], [item["id"] for item in sidecar["artifacts"]])

    def test_moved_export_and_archived_artifact_paths_remain_reserved(self):
        with Library(self.settings) as library, patch("lumen.library.PipelineRegistry.darktable", side_effect=self.pipeline), patch("lumen.pipelines.subprocess.run", side_effect=self.runner):
            first = self.export(library)
            self.assertTrue(first.success, first.message)
            moved = self.root / "moved.jpg"
            first.output.rename(moved)
            before = (moved.read_bytes(), moved.stat().st_mtime_ns)
            archived_path = first.output.with_name("photo-lumen-1.jpg")  # beside the first export, wherever that landed
            archived = library.catalog.save_artifact(None, "old photo.jpg", "old-source", "jpeg", str(archived_path), {}, "old-output")
            archived_event = library.catalog.append_event(None, "exported", {"artifact_id": archived.id})
            second = self.export(library)
            self.assertTrue(second.success, second.message)
            self.assertEqual("photo-lumen-2.jpg", second.output.name)
            self.assertEqual(before, (moved.read_bytes(), moved.stat().st_mtime_ns))
            self.assertEqual([first.artifact.id, archived.id, second.artifact.id], [artifact.id for artifact in library.catalog.list_artifacts()])
            self.assertEqual(archived_event, library.catalog.events_for(None)[0].id)
            events = [event for event in library.catalog.events_for(self.photo_id) if event.kind == "exported"]
            self.assertEqual(2, len(events))
            sidecar = json.loads(library.sidecars.paths(library.catalog.get_photo(self.photo_id))[0].read_text())
            self.assertEqual([first.artifact.id, second.artifact.id], [item["id"] for item in sidecar["artifacts"]])

    def test_failed_collision_insert_cannot_compensate_an_earlier_operation(self):
        with Library(self.settings) as library, patch("lumen.library.PipelineRegistry.darktable", side_effect=self.pipeline), patch("lumen.pipelines.subprocess.run", side_effect=self.runner):
            first = self.export(library)
            first.output.rename(self.root / "moved.jpg")
            original = DarktablePipeline.export_jpeg
            def ignore_reservations(pipeline, *args, **kwargs):
                kwargs.pop("reserved_paths", None)
                return original(pipeline, *args, **kwargs)
            with patch.object(DarktablePipeline, "export_jpeg", ignore_reservations):
                second = self.export(library)
            self.assertFalse(second.success)
            self.assertFalse(library.catalog.connection.in_transaction)
            self.assertEqual([first.artifact], library.catalog.list_artifacts())
            self.assertEqual(1, sum(event.kind == "exported" for event in library.catalog.events_for(self.photo_id)))

    def test_compensation_rebuilds_current_sidecars_after_nested_newer_success(self):
        with Library(self.settings) as first, Library(self.settings) as second:
            original_write = first._write_sidecar
            failed = False
            def write_then_fail(photo_id):
                nonlocal failed
                original_write(photo_id)
                if not failed:
                    failed = True
                    raise OSError("older export sidecar failure")
            newer = []
            nested = False
            def runner(command, **kwargs):
                nonlocal nested
                if command[1:] != ["--version"] and not nested:
                    nested = True
                    newer.append(self.export(second))
                return self.runner(command, **kwargs)
            with patch("lumen.library.PipelineRegistry.darktable", side_effect=self.pipeline), patch("lumen.pipelines.subprocess.run", side_effect=runner), patch.object(first, "_write_sidecar", side_effect=write_then_fail):
                older = self.export(first)
            self.assertFalse(older.success)
            self.assertTrue(newer[0].success, newer[0].message)
            self.assertTrue(newer[0].output.exists())
            sidecar = json.loads(first.sidecars.paths(first.catalog.get_photo(self.photo_id))[0].read_text())
            self.assertEqual([newer[0].artifact.id], [item["id"] for item in sidecar["artifacts"]])
            self.assertEqual(1, sum(event["kind"] == "exported" for event in sidecar["provenance"]))

    def test_compensation_does_not_recreate_sidecars_after_nested_clear(self):
        with Library(self.settings) as exporting, Library(self.settings) as cleaning:
            cleared = False
            def runner(command, **kwargs):
                nonlocal cleared
                if command[1:] != ["--version"] and not cleared:
                    cleared = True
                    cleaning.clear_workspace("CLEAR")
                return self.runner(command, **kwargs)
            with patch("lumen.library.PipelineRegistry.darktable", side_effect=self.pipeline), patch("lumen.pipelines.subprocess.run", side_effect=runner):
                result = self.export(exporting)
            self.assertFalse(result.success)
            self.assertEqual([], exporting.catalog.list_photos())
            self.assertEqual([], list(self.settings.sidecar_dir.iterdir()))
            self.assertEqual([], list(self.settings.jpeg_output_dir.rglob("*.jpg")))

    def test_threads_serialize_export_failure_with_export_remove_and_clear(self):
        for action in ("export", "remove", "clear"):
            with self.subTest(action=action):
                # Each scenario uses a fresh library because cleanup removes rows.
                settings = Settings(self.root / action)
                with Library(settings) as library:
                    library.import_path(self.source)
                    photo_id = library.catalog.list_photos()[0].id
                    group_id = library.list_groups()[0]["id"]
                entered, release, attempted, second_entered = (threading.Event() for _ in range(4))
                results, errors = {}, []
                def runner(command, **kwargs):
                    if command[1:] != ["--version"] and threading.current_thread().name == "older-export":
                        entered.set()
                        if not release.wait(5):
                            raise TimeoutError("test release timeout")
                    return self.runner(command, **kwargs)
                def older():
                    try:
                        with Library(settings) as library:
                            original = library._write_sidecar
                            def fail_once(photo_id):
                                original(photo_id)
                                if not getattr(fail_once, "failed", False):
                                    fail_once.failed = True
                                    raise OSError("older export failed")
                            with patch.object(library, "_write_sidecar", side_effect=fail_once):
                                results["older"] = library.export_jpegs(ExportRequest((photo_id,)))[0]
                    except BaseException as error:
                        errors.append(error)
                def newer():
                    try:
                        with Library(settings) as library:
                            original = library.catalog.get_photo if action == "export" else library._cleanup
                            def track(*args, **kwargs):
                                second_entered.set()
                                return original(*args, **kwargs)
                            target, method = (library.catalog, "get_photo") if action == "export" else (library, "_cleanup")
                            with patch.object(target, method, side_effect=track):
                                attempted.set()
                                results["newer"] = library.export_jpegs(ExportRequest((photo_id,)))[0] if action == "export" else library.remove_group(group_id) if action == "remove" else library.clear_workspace("CLEAR")
                    except BaseException as error:
                        errors.append(error)
                with patch("lumen.library.PipelineRegistry.darktable", side_effect=self.pipeline), patch("lumen.pipelines.subprocess.run", side_effect=runner):
                    first_thread = threading.Thread(target=older, name="older-export")
                    second_thread = threading.Thread(target=newer, name="newer-mutation")
                    first_thread.start()
                    try:
                        self.assertTrue(entered.wait(5))
                        second_thread.start()
                        self.assertTrue(attempted.wait(5))
                        self.assertFalse(second_entered.wait(.15), "second mutation entered before export compensation finished")
                    finally:
                        release.set()
                        first_thread.join(5)
                        if second_thread.ident is not None:
                            second_thread.join(5)
                self.assertFalse(first_thread.is_alive())
                self.assertFalse(second_thread.is_alive())
                self.assertEqual([], errors)
                self.assertFalse(results["older"].success)
                with Library(settings) as library:
                    if action == "export":
                        self.assertTrue(results["newer"].success)
                        sidecar = json.loads(next(settings.sidecar_dir.glob("*.lumen.json")).read_text())
                        self.assertEqual([results["newer"].artifact.id], [item["id"] for item in sidecar["artifacts"]])
                    else:
                        self.assertEqual([], library.catalog.list_photos())
                        self.assertEqual([], list(settings.sidecar_dir.iterdir()))

    def test_separate_process_cleanup_waits_for_export(self):
        entered, release = threading.Event(), threading.Event()
        results = []
        def runner(command, **kwargs):
            if command[1:] != ["--version"]:
                entered.set()
                if not release.wait(10):
                    raise TimeoutError("test release timeout")
            return self.runner(command, **kwargs)
        def exporting():
            with Library(self.settings) as library:
                results.append(self.export(library))
        script = '''import fcntl, sys
from pathlib import Path
from lumen.config import Settings
from lumen.library import Library
settings = Settings(Path(sys.argv[1]))
with (settings.library_dir / ".mutations.lock").open("a+b") as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("LOCKED", flush=True)
    else:
        print("UNLOCKED", flush=True)
        fcntl.flock(lock, fcntl.LOCK_UN)
with Library(settings) as library:
    library.clear_workspace("CLEAR")
print("CLEARED", flush=True)
'''
        with patch("lumen.library.PipelineRegistry.darktable", side_effect=self.pipeline), patch("lumen.pipelines.subprocess.run", side_effect=runner):
            thread = threading.Thread(target=exporting)
            thread.start()
            process = None
            try:
                self.assertTrue(entered.wait(5))
                process = subprocess.Popen([sys.executable, "-c", script, str(self.settings.library_dir)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                self.assertEqual("LOCKED", process.stdout.readline().strip())
            finally:
                release.set()
                thread.join(10)
                if process is not None:
                    stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(0, process.returncode, stderr)
            self.assertIn("CLEARED", stdout)
        self.assertTrue(results[0].success, results[0].message)
        self.assertTrue(results[0].output.exists())
        with Library(self.settings) as library:
            self.assertEqual([], library.catalog.list_photos())
            self.assertEqual([], list(self.settings.sidecar_dir.iterdir()))
