import tempfile
import json
import sqlite3
import subprocess
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from lumen.config import Settings
from lumen.library import Library
from lumen import library as library_module
from lumen.pipelines import DarktablePipeline
from PIL import ImageCms
from lumen.analysis import RegionObservation, SubjectRegions
from lumen.composition import CropRect
from lumen.domain import PhotoEdit
from lumen.omlx import CompositionResponseError, CompositionReview, TagResult
from lumen.omlx import TagResponseError, parse_composition_response, parse_tag_response


class LibraryTests(unittest.TestCase):
    def test_composition_review_records_original_recommendation_without_selecting_crop(self):
        class FakeTagger:
            model = "test-vlm"

            def review_composition(self, original, candidates):
                self.original = original
                self.candidates = tuple(candidates)
                self.original_was_file = original.is_file()
                return CompositionReview("crop-original", "Keep the environmental context.", 0.88)

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "large.jpg"
            Image.new("RGB", (4000, 3000), "gray").save(source)
            regions = SubjectRegions(4000, 3000, (RegionObservation(.4, .25, .1, .2),), ())
            tagger = FakeTagger()
            with Library(Settings(library_dir=Path(directory) / "library")) as library:
                library.import_path(source)
                photo = library.catalog.list_photos()[0]
                with patch("lumen.library.analyze_subject_regions", return_value=regions):
                    before = library.crop_candidates(photo.id)
                    result = library.review_composition(photo.id, tagger)
                    after = library.crop_candidates(photo.id)
                active = library.catalog.get_crop(photo.id)
                event = library.catalog.events_for(photo.id)[-1]
                sidecar = json.loads(library.sidecars.paths(photo)[0].read_text())

        self.assertEqual("crop-original", result.candidate_id)
        self.assertEqual(before, after)
        self.assertIsNone(active)
        self.assertEqual("composition_reviewed", event.kind)
        self.assertEqual([candidate.id for candidate in before], event.details["candidate_ids"])
        self.assertEqual("crop-original", event.details["candidate_id"])
        self.assertEqual("test-vlm", event.details["model"])
        self.assertEqual("composition_reviewed", sidecar["provenance"][-1]["kind"])
        self.assertTrue(tagger.original_was_file)
        self.assertTrue(all(not path.exists() for _, path in tagger.candidates if path != tagger.original))

    def test_composition_review_does_not_replace_an_active_crop(self):
        class FakeTagger:
            model = "test-vlm"

            def review_composition(self, original, candidates):
                return CompositionReview("crop-original", "The existing frame is strongest.", 0.7)

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "large.jpg"
            Image.new("RGB", (4000, 3000), "gray").save(source)
            regions = SubjectRegions(4000, 3000, (RegionObservation(.4, .25, .1, .2),), ())
            with Library(Settings(library_dir=Path(directory) / "library")) as library:
                library.import_path(source)
                photo = library.catalog.list_photos()[0]
                with patch("lumen.library.analyze_subject_regions", return_value=regions):
                    candidate = next(item for item in library.crop_candidates(photo.id) if item.kind != "original")
                    library.choose_crop(photo.id, candidate.id)
                    before = library.catalog.get_crop(photo.id)
                    library.review_composition(photo.id, FakeTagger())
                    after = library.catalog.get_crop(photo.id)

        self.assertEqual(before, after)

    def test_failed_composition_review_preserves_raw_response_candidates_and_temp_cleanup(self):
        class BrokenTagger:
            model = "broken-vlm"

            def review_composition(self, original, candidates):
                self.original = original
                self.candidates = tuple(candidates)
                return parse_composition_response("not model json", {item[0] for item in candidates})

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "large.jpg"
            Image.new("RGB", (4000, 3000), "gray").save(source)
            regions = SubjectRegions(4000, 3000, (RegionObservation(.4, .25, .1, .2),), ())
            tagger = BrokenTagger()
            with Library(Settings(library_dir=Path(directory) / "library")) as library:
                library.import_path(source)
                photo = library.catalog.list_photos()[0]
                with patch("lumen.library.analyze_subject_regions", return_value=regions):
                    before = library.crop_candidates(photo.id)
                    with self.assertRaisesRegex(Exception, "valid JSON"):
                        library.review_composition(photo.id, tagger)
                    after = library.crop_candidates(photo.id)
                event = library.catalog.events_for(photo.id)[-1]

        self.assertEqual(before, after)
        self.assertEqual("composition_review_failed", event.kind)
        self.assertEqual("not model json", event.details["raw_response"])
        self.assertEqual([candidate.id for candidate in before], event.details["candidate_ids"])
        self.assertTrue(all(not path.exists() for _, path in tagger.candidates if path != tagger.original))

    def test_composition_review_rejects_unknown_returned_id_at_library_boundary(self):
        class UntrustedTagger:
            model = "untrusted-vlm"

            def review_composition(self, original, candidates):
                return CompositionReview("crop-invented", "Invented selection.", 0.5)

        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(self.image_path)
            photo_id = library.catalog.list_photos()[0].id
            before = library.crop_candidates(photo_id)
            with self.assertRaisesRegex(ValueError, "candidate"):
                library.review_composition(photo_id, UntrustedTagger())
            after = library.crop_candidates(photo_id)
            event = library.catalog.events_for(photo_id)[-1]

        self.assertEqual(before, after)
        self.assertEqual("composition_review_failed", event.kind)
        self.assertIsNone(event.details["raw_response"])

    def test_composition_review_keeps_model_error_primary_when_sidecar_write_also_fails(self):
        class BrokenTagger:
            model = "broken-vlm"

            def review_composition(self, original, candidates):
                raise CompositionResponseError("model response failed", "bad model response")

        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(self.image_path)
            photo_id = library.catalog.list_photos()[0].id
            with patch.object(library, "_write_sidecar", side_effect=OSError("sidecar disk full")):
                with self.assertRaises(CompositionResponseError) as raised:
                    library.review_composition(photo_id, BrokenTagger())
            event = library.catalog.events_for(photo_id)[-1]

        self.assertEqual("model response failed", str(raised.exception))
        self.assertEqual("bad model response", event.details["raw_response"])
        self.assertTrue(any("sidecar disk full" in note for note in raised.exception.__notes__))

    def test_crop_selection_uses_candidate_id_and_writes_edit_to_sidecars(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "large.jpg"
            Image.new("RGB", (4000, 3000), "gray").save(source)
            regions = SubjectRegions(4000, 3000, (RegionObservation(.4, .25, .1, .2),), ())
            with Library(Settings(library_dir=Path(directory) / "library")) as library:
                library.import_path(source)
                photo = library.catalog.list_photos()[0]
                with patch("lumen.library.analyze_subject_regions", return_value=regions):
                    candidates = library.crop_candidates(photo.id)
                    candidate = next(item for item in candidates if item.kind != "original")
                    edit = library.choose_crop(photo.id, candidate.id)
                self.assertEqual(candidate.id, edit.candidate_id)
                self.assertEqual("manual", edit.author)
                self.assertEqual(candidate.rect, edit.rect)
                current_photo = library.catalog.get_photo(photo.id)
                data = json.loads(library.sidecars.paths(current_photo)[0].read_text())
                self.assertEqual(candidate.id, data["active_crop"]["candidate_id"])
                self.assertEqual(candidate.rationale, data["active_crop"]["rationale"])
                self.assertEqual("manual", data["active_crop"]["author"])
                xmp = library.sidecars.paths(photo)[1].read_text()
                self.assertIn("crop", xmp)
                self.assertIn(candidate.id, xmp)
                self.assertIn("cropProvenance", xmp)

    def test_crop_selection_rejects_stale_or_original_candidate_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "large.jpg"
            Image.new("RGB", (4000, 3000), "gray").save(source)
            regions = SubjectRegions(4000, 3000, (RegionObservation(.4, .25, .1, .2),), ())
            with Library(Settings(library_dir=Path(directory) / "library")) as library:
                library.import_path(source)
                photo = library.catalog.list_photos()[0]
                with patch("lumen.library.analyze_subject_regions", return_value=regions):
                    for invalid in ("missing", "crop-original"):
                        with self.subTest(candidate=invalid), self.assertRaises(ValueError):
                            library.choose_crop(photo.id, invalid)
                self.assertIsNone(library.catalog.get_crop(photo.id))

    def test_clear_crop_sidecar_failure_compensates_edit_and_event(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "large.jpg"
            Image.new("RGB", (4000, 3000), "gray").save(source)
            regions = SubjectRegions(4000, 3000, (RegionObservation(.4, .25, .1, .2),), ())
            with Library(Settings(library_dir=Path(directory) / "library")) as library:
                library.import_path(source)
                photo = library.catalog.list_photos()[0]
                with patch("lumen.library.analyze_subject_regions", return_value=regions):
                    candidate = next(item for item in library.crop_candidates(photo.id) if item.kind != "original")
                    library.choose_crop(photo.id, candidate.id)
                before = library.catalog.get_crop(photo.id)
                original_write = library.sidecars.write
                calls = 0
                def fail_once(*args, **kwargs):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        raise OSError("injected sidecar failure")
                    return original_write(*args, **kwargs)
                with patch.object(library.sidecars, "write", side_effect=fail_once), self.assertRaisesRegex(OSError, "injected"):
                    library.clear_crop(photo.id)
                self.assertEqual(before, library.catalog.get_crop(photo.id))
                self.assertEqual(["imported", "crop_selected"], [event.kind for event in library.catalog.events_for(photo.id)])
                self.assertGreaterEqual(calls, 2)

    def test_source_replacement_invalidates_crop_before_sidecars_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "large.jpg"
            Image.new("RGB", (4000, 3000), "gray").save(source)
            regions = SubjectRegions(4000, 3000, (RegionObservation(.4, .25, .1, .2),), ())
            with Library(Settings(library_dir=Path(directory) / "library")) as library:
                library.import_path(source)
                photo = library.catalog.list_photos()[0]
                with patch("lumen.library.analyze_subject_regions", return_value=regions):
                    candidate = next(item for item in library.crop_candidates(photo.id) if item.kind != "original")
                    library.choose_crop(photo.id, candidate.id)
                old_sidecars = library.sidecars.paths(photo)
                self.assertTrue(all(path.exists() for path in old_sidecars))
                Image.new("RGB", (4000, 3000), "blue").save(source)
                library.import_path(source)
                self.assertIsNone(library.catalog.get_crop(photo.id))
                self.assertIn("crop_invalidated_source_replaced", [event.kind for event in library.catalog.events_for(photo.id)])
                self.assertTrue(all(not path.exists() for path in old_sidecars))
                current_photo = library.catalog.get_photo(photo.id)
                data = json.loads(library.sidecars.paths(current_photo)[0].read_text())
                self.assertIsNone(data["active_crop"])

    def test_source_replacement_invalidates_crop_before_upsert_can_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "large.jpg"
            Image.new("RGB", (4000, 3000), "gray").save(source)
            regions = SubjectRegions(4000, 3000, (RegionObservation(.4, .25, .1, .2),), ())
            with Library(Settings(library_dir=Path(directory) / "library")) as library:
                library.import_path(source)
                photo = library.catalog.list_photos()[0]
                with patch("lumen.library.analyze_subject_regions", return_value=regions):
                    candidate = next(item for item in library.crop_candidates(photo.id) if item.kind != "original")
                    library.choose_crop(photo.id, candidate.id)
                Image.new("RGB", (4000, 3000), "blue").save(source)
                with patch.object(library.catalog, "upsert_photo", side_effect=OSError("injected upsert failure")):
                    failed = library.import_path(source)

                self.assertEqual(1, failed.failed)
                self.assertIsNone(library.catalog.get_crop(photo.id))
                self.assertEqual(photo.fingerprint, library.catalog.get_photo(photo.id).fingerprint)
                self.assertIn(
                    "crop_invalidated_source_replaced",
                    [event.kind for event in library.catalog.events_for(photo.id)],
                )

                pipeline = DarktablePipeline("darktable-cli", library.settings.output_dir)
                with patch("lumen.library.PipelineRegistry.darktable", return_value=pipeline), patch(
                    "lumen.pipelines.subprocess.run", side_effect=self.export_runner
                ) as run:
                    blocked = library.export_jpegs(
                        library_module.ExportRequest((photo.id,), None)
                    )[0]

                self.assertFalse(blocked.success)
                self.assertIsNone(blocked.output)
                self.assertIsNone(blocked.artifact)
                self.assertEqual([], library.catalog.list_artifacts())
                run.assert_not_called()

                completed = library.import_path(source)
                self.assertEqual(1, completed.updated)
                self.assertIsNone(library.catalog.get_crop(photo.id))
                with patch("lumen.library.PipelineRegistry.darktable", return_value=pipeline), patch(
                    "lumen.pipelines.subprocess.run", side_effect=self.export_runner
                ):
                    result = library.export_jpegs(library_module.ExportRequest((photo.id,), None))[0]

                self.assertTrue(result.success, result.message)
                self.assertIsNone(result.artifact.settings["crop"])

    def test_export_applies_and_records_the_active_crop_everywhere(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "large.jpg"
            Image.new("RGB", (4000, 3000), "gray").save(source)
            before = source.read_bytes()
            regions = SubjectRegions(4000, 3000, (RegionObservation(.4, .25, .1, .2),), ())
            with Library(Settings(library_dir=Path(directory) / "library")) as library:
                library.import_path(source)
                photo = library.catalog.list_photos()[0]
                with patch("lumen.library.analyze_subject_regions", return_value=regions):
                    candidate = next(item for item in library.crop_candidates(photo.id) if item.kind != "original")
                    library.choose_crop(photo.id, candidate.id)
                pipeline = DarktablePipeline("darktable-cli", library.settings.output_dir)
                with patch("lumen.library.PipelineRegistry.darktable", return_value=pipeline), patch(
                    "lumen.pipelines.subprocess.run", side_effect=self.export_runner
                ):
                    result = library.export_jpegs(library_module.ExportRequest((photo.id,), None))[0]
                self.assertTrue(result.success, result.message)
                artifact = result.artifact
                self.assertEqual(candidate.id, artifact.settings["crop"]["candidate_id"])
                self.assertEqual(photo.fingerprint, artifact.settings["crop"]["source_fingerprint"])
                event = next(event for event in library.catalog.events_for(photo.id) if event.kind == "exported")
                self.assertEqual(artifact.settings["crop"], event.details["crop"])
                with Image.open(result.output) as image:
                    self.assertEqual(
                        round(320 * artifact.settings["crop"]["width"]), image.width
                    )
                    self.assertEqual(
                        round(240 * artifact.settings["crop"]["height"]), image.height
                    )
                    self.assertEqual(candidate.id, json.loads(
                        image.info["xmp"].decode("utf-8").split("<lumen:crop>")[1].split("</lumen:crop>")[0]
                    )["candidate_id"])
                data = json.loads(library.sidecars.paths(photo)[0].read_text())
                self.assertEqual(candidate.id, data["active_crop"]["candidate_id"])
            self.assertEqual(before, source.read_bytes())

    def test_cleanup_rejects_canonical_original_and_artifact_aliases_before_mutation(self):
        for operation in ("group", "workspace"):
            for protected_kind in ("original", "artifact"):
                for alias_kind in ("symlink", "parent"):
                    with self.subTest(operation=operation, protected=protected_kind, alias=alias_kind), tempfile.TemporaryDirectory() as directory:
                        with Library(Settings(library_dir=Path(directory) / "library")) as library:
                            library.import_path(self.image_path)
                            photo = library.catalog.list_photos()[0]
                            preview = Path(photo.preview_path)
                            if alias_kind == "symlink":
                                alias = Path(directory) / "protected-alias"
                                alias.symlink_to(preview.parent, target_is_directory=True)
                                protected_path = alias / preview.name
                            else:
                                child = preview.parent / "child"
                                child.mkdir()
                                protected_path = child / ".." / preview.name
                            if protected_kind == "original":
                                library.catalog.connection.execute("UPDATE photos SET path = ? WHERE id = ?", (str(protected_path), photo.id))
                                library.catalog.connection.commit()
                            else:
                                library.catalog.save_artifact(photo.id, "completed.jpg", photo.fingerprint, "jpeg", str(protected_path), {}, "output")
                            before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (self.image_path, protected_path, library.settings.database_path)}
                            with self.assertRaises(ValueError):
                                library.remove_group(photo.group_id) if operation == "group" else library.clear_workspace("CLEAR")
                            self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before})
                            self.assertIsNotNone(library.get_photo(photo.id)["metrics"])
                            self.assertEqual([], list(library.settings.trash_dir.iterdir()))

    def test_cleanup_rejects_managed_root_and_nested_directory_symlinks(self):
        for operation in ("group", "workspace"):
            for managed_name in ("previews", "sidecars", "trash", "nested"):
                with self.subTest(operation=operation, managed=managed_name), tempfile.TemporaryDirectory() as directory:
                    with Library(Settings(library_dir=Path(directory) / "library")) as library:
                        library.import_path(self.image_path)
                        photo = library.catalog.list_photos()[0]
                        if managed_name == "nested":
                            alias = library.settings.preview_dir / "nested"
                        else:
                            alias = library.settings.library_dir / managed_name
                            alias.rename(alias.with_name(f"saved-{managed_name}"))
                        alias.symlink_to(library.settings.preview_dir if managed_name == "nested" else library.settings.library_dir, target_is_directory=True)
                        if managed_name in ("previews", "nested"):
                            candidate = alias / (Path(photo.preview_path).name if managed_name == "nested" else "catalog.sqlite3")
                            library.catalog.connection.execute("UPDATE photos SET preview_path = ? WHERE id = ?", (str(candidate), photo.id))
                            library.catalog.connection.commit()
                        database = library.settings.database_path
                        before = (database.read_bytes(), database.stat().st_mtime_ns)
                        trash_before = set(library.settings.trash_dir.iterdir())
                        with self.assertRaises(ValueError):
                            library.remove_group(photo.group_id) if operation == "group" else library.clear_workspace("CLEAR")
                        self.assertEqual(before, (database.read_bytes(), database.stat().st_mtime_ns))
                        self.assertIsNotNone(library.get_photo(photo.id)["metrics"])
                        self.assertEqual(trash_before, set(library.settings.trash_dir.iterdir()))

    def test_cleanup_explicitly_protects_database_and_existing_recovery_paths(self):
        class NestedDatabaseSettings(Settings):
            @property
            def database_path(self):
                return self.preview_dir / "catalog.sqlite3"
        class NestedTrashSettings(Settings):
            @property
            def trash_dir(self):
                return self.preview_dir / "recovery"
        for settings_type in (NestedDatabaseSettings, NestedTrashSettings):
            for operation in ("group", "workspace"):
                with self.subTest(settings=settings_type.__name__, operation=operation), tempfile.TemporaryDirectory() as directory:
                    with Library(settings_type(library_dir=Path(directory))) as library:
                        library.import_path(self.image_path)
                        photo = library.catalog.list_photos()[0]
                        protected_path = library.settings.database_path
                        if settings_type is NestedTrashSettings:
                            protected_path = library.settings.trash_dir / "previous-snapshot.sqlite3"
                            protected_path.write_bytes(b"existing recovery data")
                        library.catalog.connection.execute("UPDATE photos SET preview_path = ? WHERE id = ?", (str(protected_path), photo.id))
                        library.catalog.connection.commit()
                        before = (protected_path.read_bytes(), protected_path.stat().st_mtime_ns)
                        trash_before = set(library.settings.trash_dir.iterdir())
                        with self.assertRaises(ValueError):
                            library.remove_group(photo.group_id) if operation == "group" else library.clear_workspace("CLEAR")
                        self.assertEqual(before, (protected_path.read_bytes(), protected_path.stat().st_mtime_ns))
                        self.assertEqual(trash_before, set(library.settings.trash_dir.iterdir()))
                        self.assertIsNotNone(library.get_photo(photo.id)["metrics"])

    def test_staging_collision_restores_prior_moves_and_preserves_recovery_metadata(self):
        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(self.image_path)
            photo = library.catalog.list_photos()[0]
            paths = [Path(photo.preview_path), *library.sidecars.paths(photo)]
            before = {path: path.read_bytes() for path in paths}
            rename = Path.rename
            metadata = {}
            collisions = []
            def collide_after_first_move(path, target):
                value = rename(path, target)
                if path.resolve() == paths[0].resolve():
                    recovery = next(library.settings.trash_dir.iterdir())
                    for name in ("catalog.sqlite3", "manifest.json"):
                        metadata[recovery / name] = (recovery / name).read_bytes()
                    manifest = json.loads((recovery / "manifest.json").read_text())
                    second_target = Path(next(item["staged"] for item in manifest["files"] if item["source"] == str(paths[1].resolve())))
                    second_target.write_bytes(b"existing staging destination")
                    collisions.append(second_target)
                return value
            with patch.object(Path, "rename", collide_after_first_move), self.assertRaises(library_module.CleanupError):
                library.remove_group(photo.group_id)
            self.assertEqual(before, {path: path.read_bytes() for path in paths})
            self.assertEqual(metadata, {path: path.read_bytes() for path in metadata})
            self.assertEqual(b"existing staging destination", collisions[0].read_bytes())
            self.assertIsNotNone(library.get_photo(photo.id)["metrics"])
            with closing(sqlite3.connect(next(path for path in metadata if path.name == "catalog.sqlite3"))) as snapshot:
                self.assertEqual(1, snapshot.execute("SELECT COUNT(*) FROM photos").fetchone()[0])

    def test_remove_group_stages_only_its_managed_files_and_keeps_originals_and_exports(self):
        second = self.source_dir / "second.jpg"
        Image.new("RGB", (40, 30), "blue").save(second)
        originals = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (self.image_path, second)}
        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(self.source_dir)
            selected, retained = library.catalog.list_photos()
            managed = [Path(selected.preview_path), *library.sidecars.paths(selected)]
            before = {path: path.read_bytes() for path in managed}
            unrelated = library.settings.sidecar_dir / f"{selected.id}-unrelated.xmp"
            unrelated.write_bytes(b"unrelated")
            orphan = library.settings.preview_dir / "orphan.jpg"
            orphan.write_bytes(b"orphan")
            output = library.settings.output_dir / "completed.tiff"
            output.write_bytes(b"successful TIFF")
            library.catalog.save_artifact(selected.id, "DSC0001.jpg", selected.fingerprint, "tiff", str(output), {}, "output")

            result = library.remove_group(selected.group_id)

            self.assertIsInstance(result, library_module.CleanupResult)
            self.assertEqual((1, 3), (result.photos_removed, result.files_removed))
            self.assertEqual(library.settings.trash_dir, result.trash_path.parent)
            self.assertRegex(result.trash_path.name, r"^\d{8}T\d{6}\.\d{6}Z")
            self.assertEqual(result.trash_path / "catalog.sqlite3", result.snapshot_path)
            self.assertTrue(result.snapshot_path.is_file())
            with closing(sqlite3.connect(result.snapshot_path)) as snapshot:
                self.assertEqual(2, snapshot.execute("SELECT COUNT(*) FROM photos").fetchone()[0])
            for path, content in before.items():
                self.assertFalse(path.exists())
                self.assertEqual(content, (result.trash_path / "files" / path.relative_to(library.settings.library_dir)).read_bytes())
            self.assertEqual([retained.id], [photo.id for photo in library.catalog.list_photos()])
            self.assertEqual([retained.group_id], [group["id"] for group in library.list_groups()])
            self.assertTrue(Path(retained.preview_path).is_file())
            self.assertTrue(all(path.is_file() for path in library.sidecars.paths(retained)))
            self.assertIsNone(library.catalog.list_artifacts()[0].photo_id)
            self.assertEqual(b"successful TIFF", output.read_bytes())
            self.assertEqual(b"unrelated", unrelated.read_bytes())
            self.assertEqual(b"orphan", orphan.read_bytes())
        self.assertEqual(originals, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in originals})

    def test_shared_preview_is_retained_until_last_reference_is_removed(self):
        second = self.source_dir / "copy.jpg"
        second.write_bytes(self.image_path.read_bytes())
        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(self.source_dir)
            first, last = library.catalog.list_photos()
            preview = Path(first.preview_path)
            self.assertEqual(first.preview_path, last.preview_path)
            before = preview.read_bytes()

            first_result = library.remove_group(first.group_id)

            self.assertEqual(2, first_result.files_removed)
            self.assertEqual(before, preview.read_bytes())
            self.assertIsNotNone(library.get_photo(last.id))
            last_result = library.remove_group(last.group_id)
            self.assertEqual(3, last_result.files_removed)
            self.assertEqual(before, (last_result.trash_path / "files" / "previews" / preview.name).read_bytes())
            self.assertFalse(preview.exists())

    def test_clear_workspace_removes_all_selected_rows_including_empty_groups_and_ungrouped_photos(self):
        second = self.source_dir / "second.jpg"
        Image.new("RGB", (40, 30), "blue").save(second)
        originals = {path: path.read_bytes() for path in (self.image_path, second)}
        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(self.source_dir)
            photos = library.catalog.list_photos()
            library.catalog.connection.execute("UPDATE photos SET group_id = NULL WHERE id = ?", (photos[1].id,))
            library.catalog.connection.commit()
            output = library.settings.output_dir / "completed.jpg"
            output.write_bytes(b"successful JPEG")
            library.catalog.save_artifact(photos[0].id, "DSC0001.jpg", photos[0].fingerprint, "jpeg", str(output), {}, "output")

            result = library.clear_workspace("CLEAR")

            self.assertEqual((2, 6), (result.photos_removed, result.files_removed))
            for table in ("photos", "photo_groups", "metrics", "tags", "provenance"):
                self.assertEqual(0, library.catalog.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            self.assertIsNone(library.catalog.list_artifacts()[0].photo_id)
            self.assertEqual(b"successful JPEG", output.read_bytes())
            empty = library.clear_workspace("CLEAR")
            self.assertEqual((0, 0), (empty.photos_removed, empty.files_removed))
            self.assertNotEqual(result.trash_path, empty.trash_path)
            self.assertTrue(empty.snapshot_path.is_file())
        self.assertEqual(originals, {path: path.read_bytes() for path in originals})

    def test_clear_requires_exact_confirmation_without_any_mutation(self):
        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(self.image_path)
            before = {path.relative_to(self.library_dir): path.read_bytes() for path in self.library_dir.rglob("*") if path.is_file()}
            for confirmation in ("", "clear", " CLEAR", "CLEAR\n", None, True):
                with self.subTest(confirmation=confirmation), self.assertRaises(ValueError):
                    library.clear_workspace(confirmation)
            self.assertEqual(before, {path.relative_to(self.library_dir): path.read_bytes() for path in self.library_dir.rglob("*") if path.is_file()})
            self.assertEqual([], list(library.settings.trash_dir.iterdir()))
            self.assertEqual(1, len(library.catalog.list_photos()))

    def test_missing_group_is_rejected_before_snapshot_or_file_changes(self):
        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(self.image_path)
            with self.assertRaises(ValueError):
                library.remove_group(99999)
            self.assertEqual([], list(library.settings.trash_dir.iterdir()))
            self.assertEqual(1, len(library.catalog.list_photos()))

    def test_cleanup_move_failure_restores_already_staged_files_and_usable_rows(self):
        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(self.image_path)
            photo = library.catalog.list_photos()[0]
            paths = [Path(photo.preview_path), *library.sidecars.paths(photo)]
            before = {path: path.read_bytes() for path in paths}
            rename = Path.rename
            def fail_second(path, target):
                if path.resolve() == paths[1].resolve():
                    raise OSError("injected move failure")
                return rename(path, target)
            with patch.object(Path, "rename", fail_second), self.assertRaisesRegex(library_module.CleanupError, "injected move failure"):
                library.remove_group(photo.group_id)
            self.assertEqual(before, {path: path.read_bytes() for path in paths})
            self.assertIsNotNone(library.get_photo(photo.id)["metrics"])
            self.assertEqual([photo.id], library.keeper_ids())
            self.assertTrue(next(library.settings.trash_dir.iterdir()).joinpath("catalog.sqlite3").is_file())

    def test_cleanup_database_failure_restores_all_staged_files_and_dependents(self):
        for operation in ("group", "workspace"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                with Library(Settings(library_dir=Path(directory))) as library:
                    library.import_path(self.image_path)
                    photo = library.catalog.list_photos()[0]
                    library.catalog.save_tags(photo.id, {"subjects": ["bird"]}, "test")
                    library.catalog.connection.execute("CREATE TRIGGER fail_delete AFTER DELETE ON photos BEGIN SELECT RAISE(ABORT, 'injected database failure'); END")
                    library.catalog.connection.commit()
                    paths = [Path(photo.preview_path), *library.sidecars.paths(photo)]
                    before = {path: path.read_bytes() for path in paths}
                    with self.assertRaisesRegex(library_module.CleanupError, "injected database failure") as caught:
                        library.remove_group(photo.group_id) if operation == "group" else library.clear_workspace("CLEAR")
                    self.assertIsInstance(caught.exception.__cause__, sqlite3.IntegrityError)
                    self.assertTrue(caught.exception.recovery_complete)
                    self.assertEqual(before, {path: path.read_bytes() for path in paths})
                    self.assertIsNotNone(library.get_photo(photo.id)["metrics"])
                    self.assertEqual(["bird"], library.catalog.get_tags(photo.id)["data"]["subjects"])
                    self.assertEqual([photo.id], library.keeper_ids())

    def test_snapshot_failure_never_stages_files_or_deletes_rows(self):
        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(self.image_path)
            photo = library.catalog.list_photos()[0]
            paths = [Path(photo.preview_path), *library.sidecars.paths(photo)]
            before = {path: path.read_bytes() for path in paths}
            with patch.object(library.catalog, "backup", side_effect=OSError("snapshot unavailable"), create=True), self.assertRaisesRegex(library_module.CleanupError, "snapshot unavailable"):
                library.remove_group(photo.group_id)
            self.assertEqual(before, {path: path.read_bytes() for path in paths})
            self.assertIsNotNone(library.get_photo(photo.id))

    def test_cleanup_snapshot_database_failure_preserves_recovery_context(self):
        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(self.image_path)
            photo = library.catalog.list_photos()[0]
            managed = {path: path.read_bytes() for path in (Path(photo.preview_path), *library.sidecars.paths(photo))}
            failure = sqlite3.OperationalError("snapshot database unavailable")
            with patch.object(library.catalog, "backup", side_effect=failure), self.assertRaisesRegex(RuntimeError, "snapshot database unavailable") as caught:
                library.remove_group(photo.group_id)
            error = caught.exception
            self.assertIs(failure, error.__cause__)
            self.assertTrue(error.recovery_path.is_dir())
            self.assertIsNone(error.snapshot_path)
            self.assertTrue(error.recovery_complete)
            self.assertEqual(managed, {path: path.read_bytes() for path in managed})
            self.assertIsNotNone(library.get_photo(photo.id)["metrics"])

    def test_cleanup_reraises_interrupts_and_programming_errors_after_restoration(self):
        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(self.image_path)
            photo = library.catalog.list_photos()[0]
            managed = {path: path.read_bytes() for path in (Path(photo.preview_path), *library.sidecars.paths(photo))}
            for failure in (KeyboardInterrupt(), SystemExit(3), TypeError("programming defect"), sqlite3.ProgrammingError("invalid database operation")):
                with self.subTest(failure=type(failure).__name__), patch.object(library.catalog, "delete_photos", side_effect=failure), self.assertRaises(type(failure)) as caught:
                    library.remove_group(photo.group_id)
                self.assertIs(failure, caught.exception)
                self.assertEqual(managed, {path: path.read_bytes() for path in managed})
                self.assertIsNotNone(library.get_photo(photo.id)["metrics"])

    def test_cleanup_rejects_preview_outside_managed_directory(self):
        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(self.image_path)
            photo = library.catalog.list_photos()[0]
            library.catalog.connection.execute("UPDATE photos SET preview_path = ? WHERE id = ?", (str(self.image_path), photo.id))
            library.catalog.connection.commit()
            before = self.image_path.read_bytes()
            with self.assertRaises(ValueError):
                library.remove_group(photo.group_id)
            self.assertEqual(before, self.image_path.read_bytes())
            self.assertIsNotNone(library.get_photo(photo.id))
            self.assertEqual([], list(library.settings.trash_dir.iterdir()))

    def test_export_without_catalog_tags_keeps_rendered_xmp_description_and_keywords(self):
        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(self.image_path)
            photo_id = library.catalog.list_photos()[0].id
            pipeline = DarktablePipeline("darktable-cli", library.settings.output_dir)
            def render(command, **kwargs):
                if command[1:] == ["--version"]:
                    return self.export_runner(command, **kwargs)
                packet = b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:description><rdf:Alt><rdf:li xml:lang="x-default">Rendered caption</rdf:li></rdf:Alt></dc:description><dc:subject><rdf:Bag><rdf:li>bird</rdf:li></rdf:Bag></dc:subject></rdf:Description></rdf:RDF>'
                Image.new("RGB", (30, 20), "blue").save(command[-8], format="TIFF", tiffinfo={700: packet}, icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
                return subprocess.CompletedProcess(command, 0, "", "")
            with patch("lumen.library.PipelineRegistry.darktable", return_value=pipeline), patch("lumen.pipelines.subprocess.run", side_effect=render):
                result = library.export_jpegs(library_module.ExportRequest((photo_id,), None))[0]
            self.assertTrue(result.success, result.message)
            with Image.open(result.output) as image:
                self.assertEqual("Rendered caption", image.getexif()[270])
                self.assertEqual("bird\0", image.getexif()[40094].decode("utf-16le"))
                self.assertEqual(5, image.getexif()[18246])

    def test_export_cleanup_failure_removes_only_owned_collision_output(self):
        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(self.image_path)
            photo_id = library.catalog.list_photos()[0].id
            library.settings.jpeg_output_dir.mkdir()
            existing = library.settings.jpeg_output_dir / "DSC0001-lumen.jpg"
            existing.write_bytes(b"existing export")
            temporary_directory = tempfile.TemporaryDirectory
            workspaces = []
            register_cleanup = self.addCleanup
            class CleanupFailure(temporary_directory):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    workspaces.append(self)
                    register_cleanup(temporary_directory.cleanup, self)
                def cleanup(self):
                    raise OSError("injected workspace cleanup failure")
            pipeline = DarktablePipeline("darktable-cli", library.settings.output_dir)
            with patch("lumen.library.PipelineRegistry.darktable", return_value=pipeline), patch("lumen.pipelines.subprocess.run", side_effect=self.export_runner), patch("lumen.pipelines.tempfile.TemporaryDirectory", CleanupFailure):
                result = library.export_jpegs(library_module.ExportRequest((photo_id,), None))[0]
            self.assertFalse(result.success)
            self.assertIn("injected workspace cleanup failure", result.message)
            self.assertEqual(b"existing export", existing.read_bytes())
            self.assertEqual([existing], list(library.settings.jpeg_output_dir.iterdir()))
            self.assertTrue(workspaces)
            self.assertTrue(all(not Path(work.name).exists() for work in workspaces))
            self.assertEqual([], library.catalog.list_artifacts())
            self.assertFalse(any(event.kind == "exported" for event in library.catalog.events_for(photo_id)))

    def export_runner(self, command, **kwargs):
        if command[1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "darktable 5.2", "")
        Image.new("RGB", (320, 240), "red").save(command[-8], format="TIFF", icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
        return subprocess.CompletedProcess(command, 0, "", "")

    def test_export_records_artifact_and_sidecar_provenance(self):
        self.assertTrue(hasattr(library_module, "ExportRequest"), "ExportRequest is missing")
        before = (self.image_path.read_bytes(), self.image_path.stat().st_mtime_ns)
        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(self.image_path)
            photo = library.catalog.list_photos()[0]
            self.assertEqual([photo.id], library.keeper_ids())
            pipeline = DarktablePipeline("darktable-cli", library.settings.output_dir)
            with patch("lumen.library.PipelineRegistry.darktable", return_value=pipeline), patch("lumen.pipelines.subprocess.run", side_effect=self.export_runner):
                results = library.export_jpegs(library_module.ExportRequest((photo.id,), None))
            self.assertTrue(results[0].success, results[0].message)
            artifacts = library.catalog.list_artifacts()
            self.assertEqual(1, len(artifacts))
            artifact = artifacts[0]
            self.assertTrue(Path(artifact.path).is_file())
            self.assertEqual("jpeg", artifact.kind)
            self.assertEqual(photo.fingerprint, artifact.source_fingerprint)
            self.assertEqual(92, artifact.settings["quality"])
            self.assertEqual("sRGB", artifact.settings["color_space"])
            self.assertTrue(artifact.settings["include_gps"])
            self.assertEqual("darktable 5.2", artifact.settings["tool_version"])
            self.assertEqual(64, len(artifact.output_fingerprint))
            sidecar = json.loads(next(library.settings.sidecar_dir.glob("*.lumen.json")).read_text())
            self.assertEqual(artifact.path, sidecar["artifacts"][0]["path"])
            event = next(event for event in sidecar["provenance"] if event["kind"] == "exported")
            self.assertEqual(artifact.output_fingerprint, event["details"]["output_fingerprint"])
            self.assertEqual(artifact.settings["command"], event["details"]["command"])
        self.assertEqual(before, (self.image_path.read_bytes(), self.image_path.stat().st_mtime_ns))

    def test_export_validates_all_ids_before_starting(self):
        self.assertTrue(hasattr(library_module, "ExportRequest"), "ExportRequest is missing")
        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(self.image_path)
            photo = library.catalog.list_photos()[0]
            with patch("lumen.library.PipelineRegistry.darktable") as factory:
                with self.assertRaises(ValueError):
                    library.export_jpegs(library_module.ExportRequest((photo.id, 9999), None))
                factory.assert_not_called()
            self.assertEqual([], library.catalog.list_artifacts())

    def test_export_failure_does_not_record_success_and_next_item_runs(self):
        self.assertTrue(hasattr(library_module, "ExportRequest"), "ExportRequest is missing")
        second = self.source_dir / "second.jpg"
        Image.new("RGB", (30, 20), "blue").save(second)
        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(self.source_dir)
            ids = tuple(photo.id for photo in library.catalog.list_photos())
            pipeline = DarktablePipeline("darktable-cli", library.settings.output_dir)
            def runner(command, **kwargs):
                if command[1:] != ["--version"] and Path(command[1]).name == "DSC0001.jpg":
                    Path(command[-8]).write_bytes(b"partial")
                    return subprocess.CompletedProcess(command, 3, "", "bad render")
                return self.export_runner(command, **kwargs)
            with patch("lumen.library.PipelineRegistry.darktable", return_value=pipeline), patch("lumen.pipelines.subprocess.run", side_effect=runner):
                results = library.export_jpegs(library_module.ExportRequest(ids, None))
            self.assertEqual([False, True], [result.success for result in results])
            self.assertEqual(1, len(library.catalog.list_artifacts()))
            self.assertFalse(any(event.kind == "exported" for event in library.catalog.events_for(ids[0])))
            self.assertEqual(1, len(list(library.settings.jpeg_output_dir.iterdir())))

    def test_export_bookkeeping_failure_compensates_and_continues(self):
        for failure in ("artifact", "event", "sidecar"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                second = self.source_dir / "second.jpg"
                Image.new("RGB", (30, 20), "blue").save(second)
                with Library(Settings(library_dir=Path(directory))) as library:
                    library.import_path(self.source_dir)
                    ids = tuple(photo.id for photo in library.catalog.list_photos())
                    before = {path: path.read_bytes() for path in library.settings.sidecar_dir.iterdir() if path.name.startswith(f"{ids[0]}-")}
                    pipeline = DarktablePipeline("darktable-cli", library.settings.output_dir)
                    target, method = (library.catalog, "save_artifact") if failure == "artifact" else (library.catalog, "append_event") if failure == "event" else (library, "_write_sidecar")
                    original = getattr(target, method)
                    def write_then_fail(*args, **kwargs):
                        if failure == "artifact" and args[0] == ids[0]:
                            raise OSError("injected artifact failure before insertion")
                        value = original(*args, **kwargs)
                        if args[0] == ids[0]:
                            raise OSError(f"injected {failure} failure after write")
                        return value
                    with patch("lumen.library.PipelineRegistry.darktable", return_value=pipeline), patch("lumen.pipelines.subprocess.run", side_effect=self.export_runner), patch.object(target, method, side_effect=write_then_fail):
                        results = library.export_jpegs(library_module.ExportRequest(ids, None))
                    self.assertEqual([False, True], [result.success for result in results])
                    self.assertIn(f"injected {failure} failure", results[0].message)
                    self.assertEqual([ids[1]], [artifact.photo_id for artifact in library.catalog.list_artifacts()])
                    self.assertFalse(any(event.kind == "exported" for event in library.catalog.events_for(ids[0])))
                    self.assertEqual(1, len(list(library.settings.jpeg_output_dir.iterdir())))
                    self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_export_reports_compensation_failure_without_reporting_success(self):
        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(self.image_path)
            photo_id = library.catalog.list_photos()[0].id
            pipeline = DarktablePipeline("darktable-cli", library.settings.output_dir)
            with patch("lumen.library.PipelineRegistry.darktable", return_value=pipeline), patch("lumen.pipelines.subprocess.run", side_effect=self.export_runner), patch.object(library, "_write_sidecar", side_effect=OSError("sidecar unavailable")), patch.object(library.catalog, "discard_export", side_effect=OSError("catalog unavailable"), create=True):
                result = library.export_jpegs(library_module.ExportRequest((photo_id,), None))[0]
            self.assertFalse(result.success)
            self.assertIn("sidecar unavailable", result.message)
            self.assertIn("catalog unavailable", result.message)
            self.assertEqual([], list(library.settings.jpeg_output_dir.iterdir()))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source_dir = self.root / "source"
        self.source_dir.mkdir()
        self.library_dir = self.root / "library"
        self.image_path = self.source_dir / "DSC0001.jpg"
        Image.new("RGB", (320, 240), (118, 118, 118)).save(self.image_path, quality=95)
        (self.source_dir / "notes.txt").write_text("not a photo")

    def tearDown(self):
        self.tmp.cleanup()

    def test_import_is_idempotent_and_preserves_source(self):
        source_bytes = self.image_path.read_bytes()
        source_mtime = self.image_path.stat().st_mtime_ns
        with Library(Settings(library_dir=self.library_dir)) as library:
            first = library.import_path(self.source_dir)
            second = library.import_path(self.source_dir)
            photos = library.catalog.list_photos()
            groups = library.list_groups()

        self.assertEqual(1, first.imported)
        self.assertEqual(0, first.failed)
        self.assertEqual(0, second.imported)
        self.assertEqual(1, second.updated)
        self.assertEqual(1, len(photos))
        self.assertEqual(1, len(groups))
        self.assertEqual(source_bytes, self.image_path.read_bytes())
        self.assertEqual(source_mtime, self.image_path.stat().st_mtime_ns)
        self.assertEqual(1, len(list((self.library_dir / "previews").glob("*.jpg"))))
        self.assertEqual(1, len(list((self.library_dir / "sidecars").glob("*.lumen.json"))))

    def test_unsupported_path_reports_failure_without_catalog_entry(self):
        text_path = self.source_dir / "only.txt"
        text_path.write_text("not a photograph")
        with Library(Settings(library_dir=self.library_dir)) as library:
            result = library.import_path(text_path)
            self.assertEqual([], library.catalog.list_photos())
        self.assertEqual(1, result.failed)
        self.assertIn("Unsupported", result.errors[0])

    def test_missing_path_reports_failure(self):
        with Library(Settings(library_dir=self.library_dir)) as library:
            result = library.import_path(self.source_dir / "missing.ARW")
        self.assertEqual(1, result.failed)
        self.assertIn("does not exist", result.errors[0])

    def test_local_tags_are_persisted_and_written_to_sidecar(self):
        class FakeTagger:
            model = "test-vlm"

            def tag(self, preview):
                return TagResult("A red bird", ["bird", "red"], "garden", ["sharp"], 0.9, "{}")

        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(self.image_path)
            photo_id = library.catalog.list_photos()[0].id
            result = library.tag_photo(photo_id, FakeTagger())
            stored = library.catalog.get_tags(photo_id)

        self.assertEqual("A red bird", result.description)
        self.assertEqual("test-vlm", stored["model"])
        sidecar = next((self.library_dir / "sidecars").glob("*.lumen.json")).read_text()
        self.assertIn("A red bird", sidecar)
        self.assertIn('"bird"', sidecar)

    def test_manual_pick_becomes_group_winner_and_survives_reimport(self):
        exif = Image.Exif()
        exif[36867] = "2026:10:21 12:00:00"
        second = self.source_dir / "DSC0002.jpg"
        Image.new("RGB", (320, 240), (20, 20, 20)).save(second, exif=exif)
        Image.new("RGB", (320, 240), (118, 118, 118)).save(self.image_path, exif=exif)

        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(self.source_dir)
            photos = library.catalog.list_photos()
            chosen_id = next(photo.id for photo in photos if photo.path.endswith("DSC0002.jpg"))
            library.set_pick(chosen_id)
            library.import_path(self.source_dir)
            group = library.list_groups()[0]
            chosen = library.catalog.get_photo(chosen_id)

        self.assertEqual(chosen_id, group["automatic_pick_id"])
        self.assertTrue(chosen.manual_pick)

    def test_recursive_import_excludes_the_managed_library(self):
        library_dir = self.source_dir / "Lumen Library"
        with Library(Settings(library_dir=library_dir)) as library:
            first = library.import_path(self.source_dir)
            second = library.import_path(self.source_dir)
            photos = library.catalog.list_photos()

        self.assertEqual(1, first.imported)
        self.assertEqual(1, second.discovered)
        self.assertEqual(1, len(photos))

    def test_changed_source_invalidates_old_tags(self):
        class FakeTagger:
            model = "test-vlm"

            def tag(self, preview):
                return TagResult("Old image", ["old"], "room", [], 0.8, "{}")

        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(self.image_path)
            photo_id = library.catalog.list_photos()[0].id
            library.tag_photo(photo_id, FakeTagger())
            Image.new("RGB", (320, 240), (10, 20, 30)).save(self.image_path, quality=95)
            library.import_path(self.image_path)
            tags = library.catalog.get_tags(photo_id)

        self.assertIsNone(tags)

    def test_external_tool_timeout_does_not_abort_remaining_batch_finalization(self):
        second = self.source_dir / "DSC0002.jpg"
        Image.new("RGB", (320, 240), (20, 20, 20)).save(second)
        original = Library._import_one

        def timeout_second(library, source):
            if source.name == "DSC0002.jpg":
                raise subprocess.TimeoutExpired("exiftool", 30)
            return original(library, source)

        with patch.object(Library, "_import_one", timeout_second):
            with Library(Settings(library_dir=self.library_dir)) as library:
                result = library.import_path(self.source_dir)
                groups = library.list_groups()

        self.assertEqual(1, result.imported)
        self.assertEqual(1, result.failed)
        self.assertEqual(1, len(groups))
        self.assertEqual(1, len(list((self.library_dir / "sidecars").glob("*.lumen.json"))))

    def test_failed_tagging_is_recorded_without_replacing_tags(self):
        class BrokenTagger:
            model = "broken-vlm"

            def tag(self, preview):
                raise TagResponseError("invalid response")

        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(self.image_path)
            photo_id = library.catalog.list_photos()[0].id
            with self.assertRaises(TagResponseError):
                library.tag_photo(photo_id, BrokenTagger())
            events = library.catalog.events_for(photo_id)
            tags = library.catalog.get_tags(photo_id)

        self.assertIsNone(tags)
        self.assertEqual("tag_failed", events[-1].kind)
        self.assertIn("invalid response", events[-1].details["error"])

    def test_malformed_tag_response_is_preserved_in_failure_provenance(self):
        class BrokenTagger:
            model = "broken-vlm"

            def tag(self, preview):
                return parse_tag_response("not model json")

        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(self.image_path)
            photo_id = library.catalog.list_photos()[0].id
            with self.assertRaises(TagResponseError):
                library.tag_photo(photo_id, BrokenTagger())
            event = library.catalog.events_for(photo_id)[-1]

        self.assertEqual("not model json", event.details["raw_response"])

    def test_failed_import_is_retained_in_global_provenance(self):
        corrupt = self.source_dir / "corrupt.jpg"
        corrupt.write_bytes(b"not an image")
        with Library(Settings(library_dir=self.library_dir)) as library:
            result = library.import_path(corrupt)
            events = library.catalog.events_for(None)

        self.assertEqual(1, result.failed)
        self.assertEqual("import_failed", events[-1].kind)
        self.assertEqual(str(corrupt.resolve()), events[-1].details["source"])

    def test_workspace_photo_count_includes_failed_analysis_until_clear(self):
        failed = self.source_dir / "analysis-failed.jpg"
        Image.new("RGB", (40, 30), "red").save(failed)
        with Library(Settings(library_dir=self.library_dir)) as library:
            self.assertEqual(1, library.import_path(self.image_path).imported)
            with patch("lumen.library.measure_image", side_effect=ValueError("analysis failed after upsert")):
                self.assertEqual(1, library.import_path(failed).failed)
            photos = library.catalog.list_photos()
            self.assertEqual(2, len(photos))
            ungrouped = next(photo for photo in photos if photo.path == str(failed.resolve()))
            self.assertIsNone(ungrouped.group_id)
            self.assertTrue(Path(ungrouped.preview_path).is_file())
            self.assertEqual(1, sum(len(group["photos"]) for group in library.list_groups()))
            self.assertTrue(callable(getattr(library, "photo_count", None)), "Library must expose the authoritative catalog photo count")
            self.assertEqual(2, library.photo_count())
            self.assertEqual(2, library.clear_workspace("CLEAR").photos_removed)
            self.assertEqual(0, library.photo_count())


if __name__ == "__main__":
    unittest.main()
