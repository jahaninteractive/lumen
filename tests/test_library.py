import tempfile
import fcntl
import json
import os
from dataclasses import replace
from datetime import date, datetime
import sqlite3
import subprocess
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from lumen.config import Settings
from lumen.vocabulary import load_blocked as load_vocabulary_blocked, load_vocabulary
from lumen.library import Library
from lumen.omlx import AstroTriage, BurstCritique, BurstResponseError, Caption, CaptionResponseError, DigestNarrative
from lumen import library as library_module
from lumen import ingest as ingest_module
from lumen.pipelines import DarktablePipeline
from PIL import ImageCms
from lumen.analysis import RegionObservation, SubjectRegions
from lumen.composition import CropRect
from lumen.domain import PhotoEdit
from lumen.omlx import CompositionResponseError, CompositionReview, OmlxError, RejectReview, TagResult
from lumen.omlx import AstroResponseError, TagResponseError, parse_composition_response, parse_tag_response
from lumen.catalog import GroupFilters

try:
    from tests.test_previews import write_fake_darktable
except ModuleNotFoundError:  # discovered with -s tests
    from test_previews import write_fake_darktable


class LibraryTests(unittest.TestCase):
    def test_composition_review_records_original_recommendation_without_selecting_crop(self):
        class FakeTagger:
            model = "test-vlm"
            role = "review"

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
        self.assertEqual("review", event.details["role"])
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
        self.assertIsNone(event.details["role"])
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

    def test_import_stores_gps_and_a_later_import_backfills_unchanged_files(self):
        base = {"captured_at": None, "camera_id": "ILCE-7CM2:1", "exposure_seconds": None, "raw_warning": None,
                "latitude": None, "longitude": None, "altitude": None}
        with Library(Settings(library_dir=self.library_dir)) as library:
            with patch("lumen.library.read_metadata", return_value=base):
                library.import_path(self.image_path)
            photo = library.catalog.list_photos()[0]
            self.assertIsNone(photo.latitude)
            fix = base | {"latitude": -24.1234, "longitude": 31.5678, "altitude": 312.0}
            with patch("lumen.library.read_metadata", return_value=fix):  # e.g. location was linked after the first import
                library.wait_for_job(library.start_import_job(self.image_path).id)
            photo = library.catalog.get_photo(photo.id)
            self.assertEqual((-24.1234, 31.5678, 312.0), (photo.latitude, photo.longitude, photo.altitude), "an unchanged file still gains its fix")
            self.assertEqual((-24.1234, 31.5678), (library.get_photo(photo.id)["photo"].latitude, library.get_photo(photo.id)["photo"].longitude))

    def test_unsupported_raw_is_flagged_at_import_and_exports_through_the_macos_decoder(self):
        warning = "Sony reduced-size lossless raw; darktable cannot decode it"
        metadata = {"captured_at": None, "camera_id": "ILCE-7CM2:1", "exposure_seconds": None, "raw_warning": warning}

        def macos_runner(command, **kwargs):
            self.assertNotEqual("darktable-cli", command[0], "darktable must not run for a flagged raw")
            if command[0].endswith("sw_vers"):
                return subprocess.CompletedProcess(command, 0, "26.5.1", "")
            Image.new("RGB", (320, 240), "red").save(command[command.index("--out") + 1], format="TIFF",
                                                   icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
            return subprocess.CompletedProcess(command, 0, "", "")

        with Library(Settings(library_dir=self.library_dir)) as library:
            with patch("lumen.library.read_metadata", return_value=metadata):
                library.import_path(self.image_path)
            photo = library.catalog.list_photos()[0]
            imported = next(event for event in library.catalog.events_for(photo.id) if event.kind == "imported")
            self.assertEqual(warning, imported.details["raw_warning"])
            self.assertEqual([warning], library.get_photo(photo.id)["warnings"])
            pipeline = DarktablePipeline("darktable-cli", library.settings.output_dir)
            with patch("lumen.library.PipelineRegistry.darktable", return_value=pipeline), patch(
                "lumen.pipelines.subprocess.run", side_effect=macos_runner
            ):
                result = library.export_jpegs(library_module.ExportRequest((photo.id,), None))[0]
            self.assertTrue(result.success, result.message)
            self.assertEqual("macos", result.artifact.settings["decoder"])
            exported = next(event for event in library.catalog.events_for(photo.id) if event.kind == "exported")
            self.assertEqual("macos", exported.details["decoder"])
            with patch("lumen.library.read_metadata", return_value=metadata | {"raw_warning": None}):
                library.import_path(self.image_path)  # a later decoder fix clears the flag on reimport
            self.assertEqual([], library.get_photo(photo.id)["warnings"])

    def test_merge_groups_moves_photos_into_earliest_group_and_survives_reimport(self):
        for name in ("DSC0002.jpg", "DSC0003.jpg"):
            Image.new("RGB", (320, 240), (90, 90, 90)).save(self.source_dir / name, quality=95)
        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(self.source_dir)
            groups = library.list_groups()
            self.assertEqual(3, len(groups))
            first, second, third = (group["id"] for group in groups)
            third_photo = groups[2]["photos"][0]["id"]
            library.set_pick(third_photo)
            for bad in ([first], [first, first], [first, 99999], [first, "2"], [first, True]):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    library.merge_groups(bad)
            self.assertEqual(3, len(library.list_groups()))

            result = library.merge_groups([third, first, second])

            merged = library.list_groups()
            self.assertEqual([first], [group["id"] for group in merged])
            self.assertEqual({"group_id": first, "merged_group_ids": [second, third], "photo_count": 3,
                              "automatic_pick_id": third_photo}, result)
            self.assertEqual(third_photo, merged[0]["automatic_pick_id"], "the manual pick is kept")
            for item in merged[0]["photos"]:
                events = [event for event in library.catalog.events_for(item["id"]) if event.kind == "groups_merged"]
                self.assertEqual([{"group_id": first, "merged_group_ids": [second, third]}], [event.details for event in events])
                sidecar = json.loads(library.sidecars.paths(library.catalog.get_photo(item["id"]))[0].read_text())
                self.assertEqual(item["id"] == third_photo, sidecar["selection"]["best_in_group"])

            library.import_path(self.source_dir)  # automatic regrouping must honour the manual merge
            after = library.list_groups()
            self.assertEqual([first], [group["id"] for group in after])
            self.assertEqual(3, len(after[0]["photos"]))
            self.assertEqual(third_photo, after[0]["automatic_pick_id"])

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
                self.assertEqual(0, image.getexif()[18246], "the stored rating, not a keeper default")

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

    def test_export_records_preset_in_artifact_settings_sidecar_and_event(self):
        self.assertEqual("full", library_module.ExportRequest((1,)).preset)
        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(self.image_path)
            photo = library.catalog.list_photos()[0]
            with patch("lumen.library.PipelineRegistry.darktable", side_effect=AssertionError("render attempted")):
                with self.assertRaises(ValueError):
                    library.export_jpegs(library_module.ExportRequest((photo.id,), None, "x"))
            pipeline = DarktablePipeline("darktable-cli", library.settings.output_dir)
            with patch("lumen.library.PipelineRegistry.darktable", return_value=pipeline), patch("lumen.pipelines.subprocess.run", side_effect=self.export_runner):
                result = library.export_jpegs(library_module.ExportRequest((photo.id,), None, "share"))[0]
            self.assertTrue(result.success, result.message)
            self.assertEqual(f"{self.image_path.stem}-lumen-share.jpg", Path(result.artifact.path).name)
            expected = {"preset": "share", "quality": 85, "long_edge": 2048, "include_gps": False}
            self.assertEqual(expected, {key: result.artifact.settings[key] for key in expected})
            sidecar = json.loads(next(library.settings.sidecar_dir.glob("*.lumen.json")).read_text())
            self.assertEqual(expected, {key: sidecar["artifacts"][0]["settings"][key] for key in expected})
            event = next(event for event in sidecar["provenance"] if event["kind"] == "exported")
            self.assertEqual(expected, {key: event["details"][key] for key in expected})

    def test_export_validates_watermark_before_any_job_and_passes_it_to_the_pipeline(self):
        self.assertIsNone(library_module.ExportRequest((1,)).watermark)
        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(self.image_path)
            photo = library.catalog.list_photos()[0]
            with patch("lumen.library.PipelineRegistry.darktable", side_effect=AssertionError("render attempted")):
                for bad in ("   ", "x" * 81, "a\tb"):
                    with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "watermark must be 1 to 80 printable characters"):
                        library.start_export_job(library_module.ExportRequest((photo.id,), None, "web", bad))
                self.assertEqual([], library.catalog.list_jobs())
            pipeline = DarktablePipeline("darktable-cli", library.settings.output_dir)
            with patch("lumen.library.PipelineRegistry.darktable", return_value=pipeline), patch("lumen.pipelines.subprocess.run", side_effect=self.export_runner):
                result = library.export_jpegs(library_module.ExportRequest((photo.id,), None, "web", "\u00a9 N. David"))[0]
            self.assertTrue(result.success, result.message)
            self.assertEqual(f"{self.image_path.stem}-lumen-web.jpg", Path(result.artifact.path).name)
            self.assertEqual(("web", True), (result.artifact.settings["preset"], result.artifact.settings["watermark"]))
            self.assertNotIn("N. David", json.dumps(result.artifact.settings))

    def test_expand_search_puts_the_query_first_and_dedupes_terms(self):
        class FakeTagger:
            def expand_query(self, query, vocabulary):
                self.seen = (query, vocabulary)
                return ["Seabirds", "tern", "TERN", "gull"]

        tagger = FakeTagger()
        with Library(Settings(library_dir=self.library_dir)) as library:
            result = library.expand_search("  seabirds ", tagger)
            self.assertEqual({"query": "seabirds", "terms": ["tern", "gull"], "text": "seabirds | tern | gull"}, result)
            self.assertEqual(("seabirds", load_vocabulary(library.settings)), tagger.seen)
            for bad in ("", "   ", "x" * 81, None):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    library.expand_search(bad, tagger)

    def test_caption_is_advisory_editable_and_only_share_export_uses_it(self):
        class FakeTagger:
            model, role = "review-model", "review"
            def caption(self, preview):
                return Caption("Penguins porpoise beside the zodiac.", "Three penguins leaping from grey water.", '{"c":1}')

        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(self.image_path)
            photo = library.catalog.list_photos()[0]
            library.catalog.save_tags(photo.id, {"description": "Original description", "subjects": ["ice"]}, "fast-model")
            result = library.caption_photo(photo.id, FakeTagger())
            self.assertEqual("Three penguins leaping from grey water.", result["alt_text"])
            event = [e for e in library.catalog.events_for(photo.id) if e.kind == "caption_written"][-1]
            self.assertEqual(("model", '{"c":1}'), (event.details["author"], event.details["raw_response"]))
            edited = library.set_caption(photo.id, caption="Porpoising gentoos.", alt_text=None)
            self.assertEqual(("Porpoising gentoos.", "Three penguins leaping from grey water."), (edited["caption"], edited["alt_text"]))
            self.assertEqual("photographer", [e for e in library.catalog.events_for(photo.id) if e.kind == "caption_written"][-1].details["author"])
            with self.assertRaises(ValueError):
                library.set_caption(photo.id, caption="#nope", alt_text=None)
            with self.assertRaises(ValueError):
                library.set_caption(photo.id, caption=None, alt_text="x" * 126)
            self.assertIn("AltTextAccessibility", library.sidecars.paths(photo)[1].read_text())
            pipeline = DarktablePipeline("darktable-cli", library.settings.output_dir)
            with patch("lumen.library.PipelineRegistry.darktable", return_value=pipeline), patch("lumen.pipelines.subprocess.run", side_effect=self.export_runner):
                share = library.export_jpegs(library_module.ExportRequest((photo.id,), None, "share"))[0]
                full = library.export_jpegs(library_module.ExportRequest((photo.id,), None, "full"))[0]
            with Image.open(share.output) as image:
                self.assertEqual("Porpoising gentoos.", image.getexif()[270])
                self.assertIn("Three penguins leaping from grey water.", image.info["xmp"].decode())
            with Image.open(full.output) as image:
                self.assertEqual("Original description", image.getexif()[270])
                self.assertNotIn("AltTextAccessibility", image.info["xmp"].decode())
            self.assertTrue(share.artifact.settings["caption_used"]); self.assertFalse(full.artifact.settings["caption_used"])
            cleared = library.set_caption(photo.id, caption="", alt_text="")
            self.assertEqual((None, None), (cleared["caption"], cleared["alt_text"]))
            self.assertNotIn("AltTextAccessibility", library.sidecars.paths(photo)[1].read_text())

    def test_caption_failure_records_raw_response_and_reraises(self):
        class BadTagger:
            model, role = "review-model", "review"
            def caption(self, preview):
                raise CaptionResponseError("bad caption", "#raw")

        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(self.image_path)
            photo = library.catalog.list_photos()[0]
            with self.assertRaises(CaptionResponseError):
                library.caption_photo(photo.id, BadTagger())
            event = [e for e in library.catalog.events_for(photo.id) if e.kind == "caption_failed"][-1]
            self.assertEqual(("review", "#raw"), (event.details["role"], event.details["raw_response"]))
            self.assertIsNone(library.catalog.get_tags(photo.id))

    def test_digest_writes_facts_first_and_narrative_only_when_the_model_answers(self):  # Review Focus
        RAW_DIGEST = json.dumps({"narrative": "A calm day with gentoos."})

        class FakeTagger:
            model, role = "review-model", "review"
            def __init__(self, fail=False): self.fail = fail; self.prompt = None
            def write_digest(self, facts_markdown):
                self.prompt = facts_markdown
                if self.fail: raise OmlxError("Local oMLX request failed: boom")
                return DigestNarrative("A calm day with gentoos.", RAW_DIGEST)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(2):
                image = Image.new("RGB", (120, 90), (index * 40, 0, 0)); exif = Image.Exif(); exif[36867] = f"2026:10:22 10:00:0{index}"
                image.save(root / f"d{index}.jpg", exif=exif)
            with Library(Settings(library_dir=root / "library")) as library:
                library.import_path(root)
                tagger = FakeTagger()
                result = library.digest_day(date(2026, 10, 22), tagger)
                path = library.settings.digest_dir / "2026-10-22.md"
                text = path.read_text()
                self.assertTrue(result["narrative"]); self.assertIn("| Frames | 2 |", text); self.assertLess(text.index("| Frames"), text.index("## Narrative"))
                self.assertIn("A calm day with gentoos.", text); self.assertIn("| Frames | 2 |", tagger.prompt)
                with self.assertRaises(FileExistsError):
                    library.digest_day(date(2026, 10, 22), tagger)
                result = library.digest_day(date(2026, 10, 22), FakeTagger(fail=True), overwrite=True)
                text = path.read_text()
                self.assertFalse(result["narrative"]); self.assertIn("| Frames | 2 |", text); self.assertIn("Narrative unavailable: Local oMLX request failed: boom", text)
                result = library.digest_day(date(2026, 10, 22), None, overwrite=True, unavailable_reason="review model Qwen3.8-27B-4bit is not loaded in oMLX")
                self.assertIn("Narrative unavailable: review model Qwen3.8-27B-4bit is not loaded in oMLX", path.read_text())
                empty = library.digest_day(date(2026, 10, 23), FakeTagger(fail=True))
                self.assertEqual((0, False), (empty["frames"], empty["narrative"]))
                self.assertIn("No frames were imported for this day.", (library.settings.digest_dir / "2026-10-23.md").read_text())
                events = [e for e in library.catalog.events_for(None) if e.kind == "digest_written"]
                self.assertEqual(4, len(events)); self.assertEqual("2026-10-22.md", events[0].details["path_name"]); self.assertNotIn("/", events[0].details["path_name"])
                self.assertEqual([RAW_DIGEST, None, None, None], [event.details["raw_response"] for event in events])

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

    def test_export_job_counts_outcomes_logs_failures_by_name_and_reports_progress(self):
        second = self.source_dir / "second.jpg"
        Image.new("RGB", (30, 20), "blue").save(second)
        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(self.source_dir)
            ids = tuple(photo.id for photo in library.catalog.list_photos())
            pipeline = DarktablePipeline("darktable-cli", library.settings.output_dir)
            def runner(command, **kwargs):
                if command[1:] != ["--version"] and Path(command[1]).name == "DSC0001.jpg":
                    return subprocess.CompletedProcess(command, 3, "", "bad render")
                return self.export_runner(command, **kwargs)
            with patch("lumen.library.PipelineRegistry.darktable", return_value=pipeline), patch("lumen.pipelines.subprocess.run", side_effect=runner):
                with self.assertRaises(ValueError):  # validated before any job row exists
                    library.start_export_job(library_module.ExportRequest(ids + (9999,), None))
                self.assertEqual([], library.catalog.list_jobs())
                job = library.start_export_job(library_module.ExportRequest(ids, None, "share"))
                self.assertTrue(library.wait_for_job(job.id, 10))
            status = library.job_status(job.id)
            self.assertEqual(("export", "succeeded", 2, 2), (status["kind"], status["status"], status["processed"], status["total"]))
            self.assertEqual({"exported": 1, "failed": 1, "skipped": 0}, status["result"])
            self.assertEqual(1, len(status["log_tail"]))
            self.assertTrue(status["log_tail"][0].startswith("DSC0001.jpg: "))
            self.assertNotIn(str(self.root), json.dumps(status))
            artifacts = library.catalog.list_artifacts()
            self.assertEqual([(ids[1], "share")], [(artifact.photo_id, artifact.settings["preset"]) for artifact in artifacts])

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

            def tag(self, preview, *, vocabulary=None):
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

    def write_scene_change_burst(self):
        """Three frames 1 s apart; the third is an inverted gradient, a different scene."""
        gradient = Image.new("L", (320, 240))
        gradient.putdata([255 - x * 255 // 319 for _ in range(240) for x in range(320)])
        for index, frame in enumerate((gradient, gradient.point(lambda value: min(255, value + 4)), gradient.transpose(Image.Transpose.FLIP_LEFT_RIGHT))):
            exif = Image.Exif(); exif[36867] = f"2026:10:21 12:00:0{index}"
            frame.convert("RGB").save(self.source_dir / f"S{index}.jpg", exif=exif, quality=95)
        return [(self.source_dir / f"S{index}.jpg").resolve() for index in range(3)]

    def test_scene_change_inside_a_burst_splits_into_clusters_each_with_its_pick(self):
        paths = self.write_scene_change_burst()
        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(self.source_dir)
            ids = [library.catalog.photo_by_path(str(path)).id for path in paths]
            groups = [group for group in library.list_groups() if {photo["id"] for photo in group["photos"]} & set(ids)]
            self.assertEqual([{ids[0], ids[1]}, {ids[2]}], [{photo["id"] for photo in group["photos"]} for group in groups])
            self.assertIn(groups[0]["automatic_pick_id"], {ids[0], ids[1]})
            self.assertEqual(ids[2], groups[1]["automatic_pick_id"])

            other = ({ids[0], ids[1]} - {groups[0]["automatic_pick_id"]}).pop()
            library.set_pick(other)
            library.set_decision(ids[2], "flag")
            library.import_path(self.source_dir)
            after = [group for group in library.list_groups() if {photo["id"] for photo in group["photos"]} & set(ids)]
            self.assertEqual(2, len(after))
            self.assertEqual(other, after[0]["automatic_pick_id"], "a manual pick survives the split")
            self.assertTrue(library.catalog.get_photo(other).manual_pick)
            self.assertEqual("flag", library.catalog.get_photo(ids[2]).decision)

    def test_unchanged_reimport_backfills_missing_hashes_and_splits_the_burst(self):
        paths = self.write_scene_change_burst()
        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(self.source_dir)
            ids = {library.catalog.photo_by_path(str(path)).id for path in paths}
            members = lambda: [group for group in library.list_groups() if {photo["id"] for photo in group["photos"]} & ids]
            library.catalog.connection.execute("UPDATE metrics SET dhash = NULL")
            library.catalog.connection.commit()
            library._regroup_and_write_sidecars()
            self.assertEqual(1, len(members()), "a burst without hashes stays whole")

            library.wait_for_job(library.start_import_job(self.source_dir).id)  # every file is unchanged and skipped
            hashes = [row[0] for row in library.catalog.connection.execute("SELECT dhash FROM metrics")]
            self.assertTrue(hashes and all(isinstance(value, str) and len(value) == 16 for value in hashes), hashes)
            self.assertEqual(2, len(members()))

    def test_near_duplicate_distance_zero_keeps_the_time_burst_whole(self):
        paths = self.write_scene_change_burst()
        with Library(Settings(library_dir=self.library_dir, near_duplicate_distance=0)) as library:
            library.import_path(self.source_dir)
            ids = {library.catalog.photo_by_path(str(path)).id for path in paths}
            groups = [group for group in library.list_groups() if {photo["id"] for photo in group["photos"]} & ids]
        self.assertEqual([ids], [{photo["id"] for photo in group["photos"]} for group in groups])

    def test_near_duplicate_distance_reads_environment_and_rejects_bad_values(self):
        with patch.dict(os.environ, {"LUMEN_NEAR_DUP_DISTANCE": "5"}):
            self.assertEqual(5, Settings.from_environment().near_duplicate_distance)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LUMEN_NEAR_DUP_DISTANCE", None)
            self.assertEqual(12, Settings.from_environment().near_duplicate_distance)
        for bad in ("-1", "twelve", "1.5", ""):
            with self.subTest(bad=bad), patch.dict(os.environ, {"LUMEN_NEAR_DUP_DISTANCE": bad}), self.assertRaises(ValueError):
                Settings.from_environment()

    def import_burst(self, library, levels):
        """Import a dated burst beside the undated DSC0001 singleton; return burst IDs best-first."""
        exif = Image.Exif()
        exif[36867] = "2026:10:21 12:00:00"
        for index, level in enumerate(levels, start=2):
            Image.new("RGB", (320, 240), (level, level, level)).save(self.source_dir / f"DSC000{index}.jpg", exif=exif)
        library.import_path(self.source_dir)
        burst = next(group for group in library.list_groups() if len(group["photos"]) > 1)
        single = next(group for group in library.list_groups() if len(group["photos"]) == 1)
        return [int(photo["id"]) for photo in burst["photos"]], int(single["photos"][0]["id"])

    def group_of(self, library, photo_id):
        return next(group for group in library.list_groups() if any(photo["id"] == photo_id for photo in group["photos"]))

    def test_keepers_are_unrejected_flagged_or_group_best_including_single_frames(self):
        with Library(Settings(library_dir=self.library_dir)) as library:
            (best, alternate, worst), single = self.import_burst(library, (118, 70, 30))
            self.assertEqual({best, single}, set(library.keeper_ids()), "a single frame is best in its own burst")
            library.set_decision(alternate, "flag")
            self.assertEqual({best, alternate, single}, set(library.keeper_ids()), "a flagged alternate keeps beside the pick")
            library.set_decision(single, "reject")
            self.assertEqual({best, alternate}, set(library.keeper_ids()), "a rejected single frame is not a keeper")
            library.set_decision(single, "none")
            library.set_decision(alternate, "none")
            self.assertEqual({best, single}, set(library.keeper_ids()))
            self.assertNotIn(worst, library.keeper_ids())

    def test_rejecting_the_pick_promotes_next_unrejected_frame_and_manual_pick_clears_rejection(self):
        with Library(Settings(library_dir=self.library_dir)) as library:
            (best, alternate, worst), _ = self.import_burst(library, (118, 70, 30))
            result = library.set_decision(best, "reject")
            group = self.group_of(library, best)
            self.assertEqual({"photo_id": best, "decision": "reject", "rating": 0, "group_id": group["id"],
                              "automatic_pick_id": alternate}, result)
            self.assertEqual(alternate, group["automatic_pick_id"])
            self.assertEqual(alternate, library.set_decision(worst, "reject")["automatic_pick_id"], "rejecting an alternate keeps the pick")
            rejected = library.catalog.get_photo(best)
            json_path, xmp_path = library.sidecars.paths(rejected)
            sidecar = json.loads(json_path.read_text())
            self.assertEqual(("reject", False), (sidecar["decision"], sidecar["selection"]["best_in_group"]))
            self.assertEqual({"previous": "none", "next": "reject"},
                             next(event for event in sidecar["provenance"] if event["kind"] == "decision_changed")["details"])
            self.assertIn('xmp:Rating="-1"', xmp_path.read_text())
            promoted = json.loads(library.sidecars.paths(library.catalog.get_photo(alternate))[0].read_text())
            self.assertTrue(promoted["selection"]["best_in_group"])

            library.set_pick(best)
            restored = library.catalog.get_photo(best)
            self.assertEqual(("none", True), (restored.decision, restored.manual_pick))
            self.assertEqual(best, self.group_of(library, best)["automatic_pick_id"])
            cleared = [event.details for event in library.catalog.events_for(best) if event.kind == "decision_changed"][-1]
            self.assertEqual({"previous": "reject", "next": "none", "reason": "manual_pick"}, cleared)
            self.assertIn('xmp:Rating="0"', library.sidecars.paths(restored)[1].read_text())
            self.assertEqual("reject", library.catalog.get_photo(worst).decision, "picking one frame leaves other rejections")

    def test_all_rejected_burst_has_no_keeper_and_pick_restores_one(self):
        with Library(Settings(library_dir=self.library_dir)) as library:
            (best, alternate), single = self.import_burst(library, (118, 30))
            library.set_decision(best, "reject")
            library.set_decision(alternate, "reject")
            group = self.group_of(library, best)
            self.assertEqual(best, group["automatic_pick_id"], "an all-rejected burst still records its best frame")
            self.assertEqual([single], library.keeper_ids())
            library.import_path(self.source_dir)  # regrouping keeps decisions and the same rule
            self.assertEqual(best, self.group_of(library, best)["automatic_pick_id"])
            self.assertEqual([single], library.keeper_ids())
            self.assertEqual(("reject", "reject"), (library.catalog.get_photo(best).decision, library.catalog.get_photo(alternate).decision))
            library.set_pick(alternate)
            self.assertEqual({alternate, single}, set(library.keeper_ids()))
            self.assertEqual(alternate, self.group_of(library, best)["automatic_pick_id"])
            self.assertEqual("reject", library.catalog.get_photo(best).decision)

    def test_keepers_export_uses_new_definition_and_stored_rating(self):
        with Library(Settings(library_dir=self.library_dir)) as library:
            (best, alternate, worst), single = self.import_burst(library, (118, 70, 30))
            library.set_rating(best, 3)
            library.set_decision(alternate, "flag")
            library.set_rating(alternate, 5)
            library.set_decision(single, "reject")
            library.set_rating(single, 2)
            self.assertEqual({best, alternate}, set(library.keeper_ids()))
            self.assertEqual(["rating_changed"], [event.kind for event in library.catalog.events_for(best) if event.kind.endswith("_changed")])
            pipeline = DarktablePipeline("darktable-cli", library.settings.output_dir)
            with patch("lumen.library.PipelineRegistry.darktable", return_value=pipeline), patch("lumen.pipelines.subprocess.run", side_effect=self.export_runner):
                results = library.export_jpegs(library_module.ExportRequest(tuple(library.keeper_ids()) + (single,), None))
            self.assertTrue(all(result.success for result in results), [result.message for result in results])
            ratings = {}
            for result in results:
                with Image.open(result.output) as image:
                    rating = image.info["xmp"].decode("utf-8").split('xmp:Rating="')[1].split('"')[0]
                    ratings[result.photo_id] = (image.getexif()[18246], rating)
            self.assertEqual({best: (3, "3"), alternate: (5, "5"), single: (0, "-1")}, ratings)

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

            def tag(self, preview, *, vocabulary=None):
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

        def timeout_second(library, source, *args, **kwargs):
            if source.name == "DSC0002.jpg":
                raise subprocess.TimeoutExpired("exiftool", 30)
            return original(library, source, *args, **kwargs)

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

            def tag(self, preview, *, vocabulary=None):
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

            def tag(self, preview, *, vocabulary=None):
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

    def run_job(self, library, job):
        self.assertTrue(library.wait_for_job(job.id, 10))
        return library.job_status(job.id)

    def refine(self, library, sharp=(), **selector):
        darktable = write_fake_darktable(self.root, sharp)
        with patch("lumen.library.find_tool", return_value=darktable):
            return self.run_job(library, library.start_refine_job(**selector))

    def test_refine_job_changes_sharpness_reranks_group_and_keeps_pick_decision_and_rating(self):
        with Library(Settings(library_dir=self.library_dir)) as library:
            (best, alternate, worst), single = self.import_burst(library, (118, 70, 30))
            library.set_rating(best, 3)
            library.set_rating(alternate, 2)
            library.set_decision(worst, "flag")
            group_id = self.group_of(library, best)["id"]
            state = lambda: {photo_id: (photo.manual_pick, photo.decision, photo.rating)
                             for photo_id in (best, alternate, worst) if (photo := library.catalog.get_photo(photo_id))}
            choices = lambda: [(event.photo_id, event.kind) for photo_id in (best, alternate, worst)
                               for event in library.catalog.events_for(photo_id)
                               if event.kind in {"manual_pick", "decision_changed", "rating_changed"}]
            before, choices_before = state(), choices()
            embedded = {photo_id: Path(library.catalog.get_photo(photo_id).preview_path) for photo_id in (best, alternate, worst)}

            status = self.refine(library, ("DSC0003",), group_id=group_id)

            self.assertEqual(("succeeded", "refine_previews"), (status["status"], status["kind"]))
            self.assertEqual({"refined": 3, "skipped": 0, "failed": 0}, status["result"])
            self.assertEqual((3, 3), (status["processed"], status["total"]))
            self.assertRegex(status["label"], r"^DSC000[234]\.jpg$")
            self.assertEqual(alternate, self.group_of(library, best)["automatic_pick_id"], "the sharper refined frame now ranks first")
            self.assertEqual(before, state(), "manual pick, decision, and rating are never written")
            self.assertEqual(choices_before, choices())
            for photo_id in (best, alternate, worst):
                photo = library.catalog.get_photo(photo_id)
                self.assertEqual(("refined", f"{photo.fingerprint}-2048.jpg"), (photo.preview_kind, Path(photo.preview_path).name))
                with Image.open(photo.preview_path) as preview:
                    self.assertEqual(2048, max(preview.size))
                self.assertFalse(embedded[photo_id].exists(), "an unshared embedded preview is removed")
                event = [item for item in library.catalog.events_for(photo_id) if item.kind == "preview_refined"][-1]
                self.assertEqual((status["job_id"], "refined"), (event.details["job_id"], event.details["preview_kind"]))
                self.assertEqual(library.catalog.get_metrics(photo_id).sharpness, event.details["metrics"]["sharpness"])
                sidecar = json.loads(library.sidecars.paths(photo)[0].read_text())
                self.assertEqual(("refined", photo_id == alternate), (sidecar["preview_kind"], sidecar["selection"]["best_in_group"]))
                self.assertEqual("preview_refined", sidecar["provenance"][-1]["kind"])
            self.assertGreater(library.catalog.get_metrics(alternate).sharpness, 0.9)
            self.assertEqual("embedded", library.catalog.get_photo(single).preview_kind, "other groups are untouched")

            library.set_pick(worst)
            before = state()
            again = self.refine(library, ("DSC0003",), all_photos=True)
            self.assertEqual({"refined": 4, "skipped": 0, "failed": 0}, again["result"])
            self.assertEqual(worst, self.group_of(library, best)["automatic_pick_id"], "a manual pick is honoured after re-rank")
            self.assertEqual(before, state())
            self.assertTrue(library.catalog.get_photo(worst).manual_pick)

    def test_refine_job_skips_changed_sources_and_retains_shared_embedded_previews(self):
        (self.source_dir / "copy.jpg").write_bytes(self.image_path.read_bytes())
        changed = self.source_dir / "DSC0005.jpg"
        Image.new("RGB", (320, 240), (10, 200, 10)).save(changed)
        Image.new("RGB", (320, 240), (200, 10, 10)).save(self.source_dir / "DSC_FAIL.jpg")
        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(self.source_dir)
            by_name = lambda: {Path(photo.path).name: photo for photo in library.catalog.list_photos()}
            first, copy = by_name()["DSC0001.jpg"], by_name()["copy.jpg"]
            embedded = Path(first.preview_path)
            self.assertEqual(first.preview_path, copy.preview_path)
            changed_preview = by_name()["DSC0005.jpg"].preview_path
            Image.new("RGB", (320, 240), (11, 201, 11)).save(changed)

            status = self.refine(library, group_id=first.group_id)
            self.assertEqual({"refined": 1, "skipped": 0, "failed": 0}, status["result"])
            self.assertTrue(embedded.is_file(), "the copy still shows the shared embedded preview")
            self.assertEqual(("refined", "embedded"), (by_name()["DSC0001.jpg"].preview_kind, by_name()["copy.jpg"].preview_kind))
            self.assertEqual(str(embedded), by_name()["copy.jpg"].preview_path)

            status = self.refine(library, all_photos=True)
            self.assertEqual("succeeded", status["status"])
            self.assertEqual({"refined": 2, "skipped": 1, "failed": 1}, status["result"])
            self.assertFalse(embedded.exists(), "removed once no photo references it")
            skipped = by_name()["DSC0005.jpg"]
            self.assertEqual(("embedded", changed_preview), (skipped.preview_kind, skipped.preview_path))
            self.assertTrue(Path(changed_preview).is_file())
            self.assertEqual("embedded", by_name()["DSC_FAIL.jpg"].preview_kind)
            log = Path(library.catalog.get_job(status["job_id"]).log_path).read_text()
            self.assertIn("DSC0005.jpg", log)
            self.assertIn("DSC_FAIL.jpg", log)
            self.assertNotIn(str(self.root), log)
            self.assertNotIn(str(self.root), json.dumps(status))
            leftovers = [path.name for path in (self.library_dir / "previews").iterdir()
                         if path.name.startswith(".") or path.suffix != ".jpg"]
            self.assertEqual([], leftovers, "failed renders leave no temporary files")

    def test_refine_job_scopes_group_filtered_and_all(self):
        with Library(Settings(library_dir=self.library_dir)) as library:
            (best, alternate, worst), single = self.import_burst(library, (118, 70, 30))
            library.set_rating(alternate, 4)
            kinds = lambda: {photo.id: photo.preview_kind for photo in library.catalog.list_photos()}
            with patch("lumen.library.find_tool", return_value=None):
                with self.assertRaisesRegex(RuntimeError, "^darktable-cli is not installed$"):
                    library.start_refine_job(all_photos=True)
            darktable = write_fake_darktable(self.root)
            with patch("lumen.library.find_tool", return_value=darktable):
                for selector in ({}, {"group_id": 1, "all_photos": True}, {"filters": GroupFilters(), "group_id": 1},
                                 {"group_id": 999}, {"group_id": "1"}, {"all_photos": 1}):
                    with self.subTest(selector=selector), self.assertRaises(ValueError):
                        library.start_refine_job(**selector)
            self.assertEqual([], library.catalog.list_jobs(), "rejected selectors never create a job")

            status = self.refine(library, filters=GroupFilters(min_rating=4))
            self.assertEqual((1, 1), (status["processed"], status["total"]))
            self.assertEqual({alternate}, {photo_id for photo_id, kind in kinds().items() if kind == "refined"})
            status = self.refine(library, group_id=self.group_of(library, single)["id"])
            self.assertEqual({alternate, single}, {photo_id for photo_id, kind in kinds().items() if kind == "refined"})
            status = self.refine(library, all_photos=True)
            self.assertEqual((4, 4), (status["processed"], status["total"]))
            self.assertEqual({"refined"}, set(kinds().values()))
            manifest = json.loads(Path(library.catalog.get_job(status["job_id"]).manifest_path).read_text())
            self.assertEqual(("refine_previews", "all"), (manifest["kind"], manifest["scope"]))

    def test_import_job_skips_cataloged_matching_photos_without_preview_work(self):
        second = self.source_dir / "DSC0002.jpg"
        Image.new("RGB", (320, 240), (20, 20, 20)).save(second)
        settings = Settings(library_dir=self.library_dir)
        with Library(settings) as library:
            library.import_path(self.source_dir)
            events_before = len(library.catalog.events_for(library.catalog.list_photos()[0].id))
            added = self.source_dir / "DSC0003.jpg"
            Image.new("RGB", (320, 240), (200, 20, 20)).save(added)
            real = library_module.ExifToolPreviewProvider.create_preview
            with patch.object(library_module.ExifToolPreviewProvider, "create_preview", autospec=True, side_effect=real) as preview, \
                    patch("lumen.library.measure_image", wraps=library_module.measure_image) as measure:
                status = self.run_job(library, library.start_import_job(self.source_dir))
            photos = library.catalog.list_photos()
            events_after = len(library.catalog.events_for(photos[0].id))

        self.assertEqual("succeeded", status["status"])
        self.assertEqual("import", status["kind"])
        self.assertEqual([added.resolve()], [call.args[1] for call in preview.call_args_list])
        self.assertEqual(1, measure.call_count)
        self.assertEqual({"discovered": 3, "imported": 1, "updated": 0, "skipped": 2, "failed": 0}, status["result"])
        self.assertEqual((3, 3), (status["processed"], status["total"]))
        self.assertEqual(3, len(photos))
        self.assertEqual(events_before, events_after)
        manifest = json.loads((self.library_dir / "outputs" / "jobs" / str(status["job_id"]) / "manifest.json").read_text())
        self.assertEqual(("import", str(self.source_dir.resolve())), (manifest["kind"], manifest["path"]))
        self.assertTrue((self.library_dir / "outputs" / "jobs" / str(status["job_id"]) / "job.log").is_file())

    def test_import_job_cancel_between_files_regroups_and_keeps_imported_rows(self):
        for index in (2, 3):
            Image.new("RGB", (320, 240), (index * 40, 20, 20)).save(self.source_dir / f"DSC000{index}.jpg")
        original = Library._import_one
        holder = {}

        def import_then_cancel(library, source, *args, **kwargs):
            photo_id = original(library, source, *args, **kwargs)
            holder["library"].cancel_job(holder["job"].id)
            return photo_id

        with Library(Settings(library_dir=self.library_dir)) as library:
            holder["library"] = library
            with patch.object(Library, "_import_one", import_then_cancel):
                holder["job"] = library.start_import_job(self.source_dir)
                status = self.run_job(library, holder["job"])
            photos = library.catalog.list_photos()
            groups = library.list_groups()

        self.assertEqual("cancelled", status["status"])
        self.assertEqual(["DSC0001.jpg"], [Path(photo.path).name for photo in photos])
        self.assertIsNotNone(photos[0].group_id)
        self.assertEqual(1, len(groups))
        self.assertEqual(1, len(list((self.library_dir / "sidecars").glob("*.lumen.json"))))

    def test_import_job_records_source_replaced_mid_job_and_continues(self):
        second = self.source_dir / "DSC0002.jpg"
        Image.new("RGB", (320, 240), (20, 20, 20)).save(second)
        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(self.source_dir)
            prior = library.catalog.photo_by_path(str(second.resolve())).fingerprint
            Image.new("RGB", (320, 240), (90, 20, 20)).save(second)
            hashed = library_module.fingerprint_file(second)
            added = self.source_dir / "DSC0003.jpg"
            Image.new("RGB", (320, 240), (20, 90, 20)).save(added)
            real = library_module.ExifToolPreviewProvider.create_preview
            swapped = []

            def swap_between_hash_and_preview(provider, source, destination):
                if source.name == "DSC0002.jpg" and not swapped:
                    Image.new("RGB", (320, 240), (5, 5, 200)).save(second)
                    swapped.append(source)
                return real(provider, source, destination)

            with patch.object(library_module.ExifToolPreviewProvider, "create_preview", autospec=True,
                              side_effect=swap_between_hash_and_preview):
                status = self.run_job(library, library.start_import_job(self.source_dir))
            changed = library.catalog.photo_by_path(str(second.resolve()))
            failure = library.catalog.events_for(changed.id)[-1]
            new_photo = library.catalog.photo_by_path(str(added.resolve()))
            imported = library.catalog.events_for(new_photo.id)[-1]
            photo_count = library.photo_count()

        self.assertEqual("succeeded", status["status"])
        self.assertEqual({"discovered": 3, "imported": 1, "updated": 0, "skipped": 1, "failed": 1}, status["result"])
        self.assertEqual(prior, changed.fingerprint, "the row keeps its prior identity, never mismatched content")
        self.assertEqual("import_failed", failure.kind)
        self.assertIn("source changed during import", failure.details["error"])
        self.assertEqual(library_module.fingerprint_file(second), failure.details["source_fingerprint"])
        self.assertEqual(status["job_id"], failure.details["job_id"])
        self.assertFalse((self.library_dir / "previews" / f"{hashed}.jpg").exists())
        self.assertEqual(("imported", status["job_id"]), (imported.kind, imported.details["job_id"]))
        self.assertEqual(3, photo_count)

    def test_import_job_label_is_basename_only(self):
        nested = self.source_dir / "private trip folder"
        nested.mkdir()
        long_name = "a" * 226 + ".jpg"
        Image.new("RGB", (320, 240), (20, 20, 20)).save(nested / long_name)
        self.image_path.unlink()
        Image.new("RGB", (320, 240), (20, 90, 20)).save(nested / "zz.jpg")
        with Library(Settings(library_dir=self.library_dir)) as library:
            labels = []
            original = Library._import_one

            def record_label(worker, source, *args, **kwargs):
                labels.append(worker.catalog.get_job(job_ids[0]).label)
                return original(worker, source, *args, **kwargs)

            job_ids = []
            with patch.object(Library, "_import_one", record_label):
                job = library.start_import_job(self.source_dir)
                job_ids.append(job.id)
                status = self.run_job(library, job)
            job = library.catalog.get_job(job.id)

        self.assertEqual([None, long_name[:200]], labels)
        self.assertEqual("zz.jpg", job.label)
        self.assertEqual("zz.jpg", status["label"])
        text = json.dumps(status)
        self.assertNotIn("private trip folder", text)
        self.assertNotIn(str(self.root), text)

    def test_tag_job_skips_tagged_unless_retag_and_records_failures_without_stopping(self):
        for index in (2, 3):
            Image.new("RGB", (320, 240), (index * 40, 20, 20)).save(self.source_dir / f"DSC000{index}.jpg")

        class FlakyTagger:
            model = "test-vlm"

            def __init__(self, failing):
                self.failing = failing
                self.calls = []

            def tag(self, preview, *, vocabulary=None):
                self.calls.append(preview)
                if preview == self.failing:
                    raise TagResponseError(f"bad response for {preview}")
                return TagResult("A frame", ["frame"], "room", [], 0.9, "{}")

        with Library(Settings(library_dir=self.library_dir)) as library:
            library.import_path(self.source_dir)
            first, second, third = library.catalog.list_photos()
            library.tag_photo(first.id, FlakyTagger(None))
            tagger = FlakyTagger(Path(second.preview_path))
            status = self.run_job(library, library.start_tag_job(tagger, scope="all"))
            manifest = json.loads((self.library_dir / "outputs" / "jobs" / str(status["job_id"]) / "manifest.json").read_text())
            second_kinds = library.catalog.events_for(second.id)
            third_event = library.catalog.events_for(third.id)[-1]
            third_tags = library.catalog.get_tags(third.id)
            retagger = FlakyTagger(None)
            retag_status = self.run_job(library, library.start_tag_job(retagger, scope="all", retag=True))
            with self.assertRaises(ValueError):
                library.start_tag_job(retagger, retag="yes")

        self.assertEqual("succeeded", status["status"])
        self.assertEqual("tag", status["kind"])
        self.assertEqual([Path(second.preview_path), Path(third.preview_path)], tagger.calls)
        self.assertEqual({"kind": "tag", "retag": False, "failed_photo_ids": [second.id]}, {key: manifest[key] for key in ("kind", "retag", "failed_photo_ids")})
        self.assertEqual({"tagged": 1, "skipped": 1, "failed": 1}, status["result"])
        self.assertEqual("tag_failed", second_kinds[-1].kind)
        self.assertEqual(status["job_id"], second_kinds[-1].details["job_id"])
        self.assertEqual(status["job_id"], third_event.details["job_id"])
        self.assertEqual("A frame", third_tags["data"]["description"])
        self.assertTrue(status["log_tail"])
        self.assertNotIn(str(self.root), json.dumps(status))
        self.assertEqual(3, len(retagger.calls))
        self.assertEqual({"tagged": 3, "skipped": 0, "failed": 0}, retag_status["result"])

    def test_interrupted_import_job_completes_on_rerun_of_same_folder(self):
        for index in (2, 3):
            Image.new("RGB", (320, 240), (index * 40, 20, 20)).save(self.source_dir / f"DSC000{index}.jpg")
        settings = Settings(library_dir=self.library_dir)
        with Library(settings) as library:
            library.import_path(self.image_path)
            stale = library.catalog.create_job(kind="import", manifest_path="/stale/manifest.json", log_path="/stale/job.log")
            library.catalog.update_job(stale.id, status="running", label="DSC0001.jpg", processed=1, total=3)
        with Library(settings) as library:
            library.job_runner
            self.assertEqual("interrupted", library.job_status(stale.id)["status"])
            status = self.run_job(library, library.start_import_job(self.source_dir))
            paths = [photo.path for photo in library.catalog.list_photos()]

        self.assertEqual("succeeded", status["status"])
        self.assertEqual({"discovered": 3, "imported": 2, "updated": 0, "skipped": 1, "failed": 0}, status["result"])
        self.assertEqual(3, len(paths))
        self.assertEqual(len(paths), len(set(paths)))

    def test_import_job_rejects_missing_and_unsupported_paths_before_creating_a_job(self):
        with Library(Settings(library_dir=self.library_dir)) as library:
            for path in (self.source_dir / "missing.ARW", self.source_dir / "notes.txt"):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    library.start_import_job(path)
            self.assertEqual([], library.catalog.list_jobs())

    def make_card(self, count=2, video=False):
        separate_volumes = patch("lumen.ingest._device", side_effect=str)  # tests share one temp volume
        separate_volumes.start()
        self.addCleanup(separate_volumes.stop)
        card = self.root / "CARD"
        (card / "DCIM" / "100MSDCF").mkdir(parents=True)
        if video:
            clip = card / "PRIVATE" / "M4ROOT" / "CLIP" / "C0001.MP4"
            clip.parent.mkdir(parents=True)
            clip.write_bytes(b"\x00\x00\x00\x18ftypXAVC video bytes")
            os.utime(clip, (datetime(2026, 10, 20, 12).timestamp(),) * 2)
        for index in range(1, count + 1):
            path = card / "DCIM" / "100MSDCF" / f"DSC0000{index}.JPG"
            Image.new("RGB", (320, 240), (index * 70, 20, 20)).save(path)
            os.utime(path, (datetime(2026, 10, 20, 12, 0, index).timestamp(),) * 2)
        self.primary = self.root / "primary"
        self.mirror = self.root / "mirror"
        self.primary.mkdir()
        self.mirror.mkdir()
        return card

    def failing_copy(self, root, name):
        real = ingest_module.copy_verified

        def copy(source, destination, **kwargs):
            if destination.is_relative_to(root.resolve()) and source.name == name:
                raise ingest_module.IngestVerifyError(f"copy of {source.name} did not verify")
            return real(source, destination, **kwargs)
        return patch("lumen.ingest.copy_verified", side_effect=copy)

    def test_ingest_job_imports_primary_copies_not_card_and_records_mirror_path(self):
        card = self.make_card(video=True)
        with Library(Settings(library_dir=self.library_dir)) as library:
            status = self.run_job(library, library.start_ingest_job(card, self.primary, self.mirror))
            photos = library.catalog.list_photos()
            job = library.catalog.get_job(status["job_id"])
            events = {photo.id: library.catalog.events_for(photo.id) for photo in photos}
            sidecars = {photo.id: [path.read_text() for path in library.sidecars.paths(photo)] for photo in photos}

        self.assertEqual("succeeded", status["status"])
        self.assertEqual("ingest", status["kind"])
        snapshot = status["ingest"].pop("snapshot")
        self.assertEqual({"copied": 3, "verified": 3, "already_present": 0, "failed": 0, "files": 3, "skipped": 0,
                          "unreadable": 0, "card_verified_everywhere": True, "manifest": "manifest.json"}, status["ingest"])
        for root in (self.primary, self.mirror):
            clip = root / "2026-10-20" / "PRIVATE" / "M4ROOT" / "CLIP" / "C0001.MP4"
            self.assertEqual((card / "PRIVATE" / "M4ROOT" / "CLIP" / "C0001.MP4").read_bytes(), clip.read_bytes(),
                             "videos are copied and verified, though never imported")
        self.assertRegex(snapshot, r"^\d{8}T\d{6}Z\.sqlite3$")
        job_dir = self.library_dir / "outputs" / "ingest" / str(job.id)
        self.assertEqual(str(job_dir / "manifest.json"), job.result_path)
        self.assertTrue((job_dir / "ingest.log").is_file())
        self.assertEqual("DSC00002.JPG", status["label"])
        relative = Path("2026-10-20") / "DCIM" / "100MSDCF"
        self.assertEqual([str(self.primary.resolve() / relative / f"DSC0000{index}.JPG") for index in (1, 2)],
                         sorted(photo.path for photo in photos))
        for photo in photos:
            name = Path(photo.path).name
            self.assertEqual(str(self.mirror.resolve() / relative / name), photo.mirror_path)
            kinds = [event.kind for event in events[photo.id]]
            self.assertIn("imported", kinds)
            ingested = next(event for event in events[photo.id] if event.kind == "ingested")
            self.assertEqual({"job_id": job.id, "destination": "primary", "mirror": True}, {
                key: ingested.details[key] for key in ("job_id", "destination", "mirror")})
            sidecar, xmp = sidecars[photo.id]
            self.assertEqual(photo.mirror_path, json.loads(sidecar)["source"]["mirror_path"])
            self.assertIn("ingested", xmp)
        manifest = json.loads((job_dir / "manifest.json").read_text())
        self.assertEqual(1, manifest["schema_version"])
        self.assertEqual(str(card.resolve()), manifest["card"])
        self.assertEqual([str(self.primary.resolve()), str(self.mirror.resolve())], manifest["destinations"])
        self.assertTrue(manifest["finished_at"])
        self.assertEqual(snapshot, manifest["snapshot"])
        self.assertEqual(3, len(manifest["files"]))
        self.assertEqual({"discovered": 2, "imported": 2, "updated": 0, "skipped": 0, "failed": 0}, manifest["import"])
        self.assertNotIn(str(self.root), json.dumps(status))

    def test_ingest_job_cancel_between_files_leaves_no_partial_or_final_half_file(self):
        card = self.make_card(3)
        real = ingest_module.copy_verified
        holder = {}

        def copy_then_cancel(source, destination, **kwargs):
            digest = real(source, destination, **kwargs)
            holder["library"].cancel_job(holder["job"].id)
            return digest

        with Library(Settings(library_dir=self.library_dir)) as library:
            holder["library"] = library
            with patch("lumen.ingest.copy_verified", side_effect=copy_then_cancel):
                holder["job"] = library.start_ingest_job(card, self.primary, self.mirror)
                status = self.run_job(library, holder["job"])
            photo_count = library.photo_count()

        self.assertEqual("cancelled", status["status"])
        self.assertFalse(status["ingest"]["card_verified_everywhere"])
        self.assertEqual(0, photo_count, "a cancelled ingest imports nothing")
        copies = [path for root in (self.primary, self.mirror) for path in root.rglob("*") if path.is_file()]
        self.assertEqual([], [path for path in copies if "lumen-partial" in path.name])
        self.assertEqual(2, len(copies), "the file in progress finishes on every destination, then the job stops")
        for path in copies:
            self.assertEqual((card / path.relative_to(path.parents[3]).relative_to("2026-10-20")).read_bytes(), path.read_bytes())
        self.assertFalse((self.primary / "lumen-catalog-backups").exists())
        manifest = json.loads((self.library_dir / "outputs" / "ingest" / str(status["job_id"]) / "manifest.json").read_text())
        self.assertEqual(1, len(manifest["files"]))
        self.assertEqual(3, manifest["summary"]["files"])

    def test_ingest_job_mirror_failure_blocks_card_verified_everywhere_but_still_imports_primary(self):
        card = self.make_card()
        with Library(Settings(library_dir=self.library_dir)) as library, self.failing_copy(self.mirror, "DSC00002.JPG"):
            status = self.run_job(library, library.start_ingest_job(card, self.primary, self.mirror))
            photos = {Path(photo.path).name: photo for photo in library.catalog.list_photos()}
            second_ingested = next(event for event in library.catalog.events_for(photos["DSC00002.JPG"].id) if event.kind == "ingested")

        self.assertEqual("succeeded", status["status"])
        self.assertEqual((False, 1, 1), (status["ingest"]["card_verified_everywhere"], status["ingest"]["failed"], status["ingest"]["verified"]))
        self.assertEqual(["DSC00001.JPG", "DSC00002.JPG"], sorted(photos))
        self.assertIsNotNone(photos["DSC00001.JPG"].mirror_path)
        self.assertIsNone(photos["DSC00002.JPG"].mirror_path)
        self.assertFalse(second_ingested.details["mirror"])
        self.assertTrue(any("did not verify" in line for line in status["log_tail"]))
        self.assertIsNotNone(status["ingest"]["snapshot"])

    def test_ingest_job_writes_catalog_snapshot_beside_primary_copies(self):
        card = self.make_card()
        real_fcntl = fcntl.fcntl
        synced = []

        def record(descriptor, command, *args):
            if command == fcntl.F_FULLFSYNC:
                synced.append(Path(real_fcntl(descriptor, fcntl.F_GETPATH, bytes(1024)).rstrip(b"\0").decode()).name)
            return real_fcntl(descriptor, command, *args)

        with Library(Settings(library_dir=self.library_dir)) as library, patch("lumen.ingest.fcntl.fcntl", side_effect=record):
            status = self.run_job(library, library.start_ingest_job(card, self.primary))
        backups = self.primary / "lumen-catalog-backups"
        self.assertIn(f".{status['ingest']['snapshot']}.partial", synced, "the snapshot is flushed to the media before its rename")
        self.assertEqual("lumen-catalog-backups", synced[-1])
        self.assertEqual([status["ingest"]["snapshot"]], [path.name for path in backups.iterdir()])
        with closing(sqlite3.connect(backups / status["ingest"]["snapshot"])) as snapshot:
            self.assertEqual(2, snapshot.execute("SELECT COUNT(*) FROM photos").fetchone()[0])
        self.assertTrue(status["ingest"]["card_verified_everywhere"], "one destination is every requested destination")

    def test_ingest_job_with_primary_failure_skips_import_and_snapshot(self):
        card = self.make_card()
        with Library(Settings(library_dir=self.library_dir)) as library, self.failing_copy(self.primary, "DSC00001.JPG"):
            status = self.run_job(library, library.start_ingest_job(card, self.primary, self.mirror))
            photo_count = library.photo_count()
            job = library.catalog.get_job(status["job_id"])

        self.assertEqual("succeeded", status["status"])
        self.assertFalse(status["ingest"]["card_verified_everywhere"])
        self.assertIsNone(status["ingest"]["snapshot"])
        self.assertEqual(0, photo_count)
        self.assertFalse((self.primary / "lumen-catalog-backups").exists())
        self.assertEqual(str(self.library_dir / "outputs" / "ingest" / str(job.id) / "manifest.json"), job.result_path)

    def test_ingest_job_rejects_unsafe_destinations_before_creating_a_job(self):
        card = self.make_card()
        with Library(Settings(library_dir=self.library_dir)) as library:
            for primary, mirror in ((self.library_dir, None), (self.primary, self.primary), (card / "DCIM", None)):
                with self.subTest(primary=primary, mirror=mirror), self.assertRaises(ValueError):
                    library.start_ingest_job(card, primary, mirror)
            self.assertEqual([], library.catalog.list_jobs())


    def test_tagger_for_refuses_roles_whose_model_is_not_loaded(self):
        live = {"version": "0.7.2", "default_model": None, "loaded_models": ["Qwen3-VL-8B-Instruct-4bit"]}
        with tempfile.TemporaryDirectory() as directory, patch("lumen.library.get_server_status", return_value=live):
            with Library(Settings(library_dir=Path(directory))) as library:
                self.assertEqual(("fast", "Qwen3-VL-8B-Instruct-4bit"), (library.tagger_for("fast").role, library.tagger_for("fast").model))
                with self.assertRaisesRegex(RuntimeError, "review model Qwen3.8-27B-4bit is not loaded in oMLX"):
                    library.tagger_for("review")
                with self.assertRaises(ValueError):
                    library.tagger_for("judge")
        with tempfile.TemporaryDirectory() as directory, patch("lumen.library.get_server_status", side_effect=OmlxError("down")):
            with Library(Settings(library_dir=Path(directory))) as library:
                with self.assertRaisesRegex(RuntimeError, "fast model .* is not loaded in oMLX; oMLX is offline"):
                    library.tagger_for("fast")

    def test_tag_job_keepers_scope_applies_vocabulary_and_high_confidence_species_only(self):
        class FakeTagger:
            model, role = "fast-model", "fast"
            calls = []

            def tag(self, preview, *, vocabulary=None):
                FakeTagger.calls.append(sorted(vocabulary))
                return TagResult("Penguins", ["Gentoo Penguin", "snow", "krill"], "ice", [], 0.9, '{"raw": true}',
                                 species=[{"name": "gentoo penguin", "confidence": 0.82}, {"name": "chinstrap penguin", "confidence": 0.4}],
                                 conditions=["overcast", "spindrift"])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(3):
                Image.new("RGB", (120, 90), (index, 0, 0)).save(root / f"p{index}.jpg")
            with Library(Settings(library_dir=root / "library")) as library:
                library.import_path(root)
                photos = library.catalog.list_photos()
                library.set_decision(photos[2].id, "reject")  # three single-frame bursts: two keepers, one rejected
                job = library.start_tag_job(FakeTagger(), scope="keepers")
                library.wait_for_job(job.id)
                status = library.job_status(job.id)
                data = library.catalog.get_tags(photos[0].id)["data"]
                event = [e for e in library.catalog.events_for(photos[0].id) if e.kind == "tagged"][-1]
                sidecar = json.loads(library.sidecars.paths(photos[0])[0].read_text())
                xmp = library.sidecars.paths(photos[0])[1].read_text()
                self.assertIsNone(library.catalog.get_tags(photos[2].id))
                learned = load_vocabulary(library.settings)
                learn_event = [e for e in library.catalog.events_for(photos[0].id) if e.kind == "vocabulary_learned"][-1]
        self.assertEqual("succeeded", status["status"])
        self.assertEqual(2, status["processed"])
        # The vocabulary learns from the model: free-text words become keywords and join <library>/vocabulary.json;
        # only a weak species (< 0.6) stays a suggestion.
        self.assertEqual(["gentoo penguin", "ice", "overcast", "snow", "krill", "spindrift"], data["subjects"])
        self.assertEqual(["chinstrap penguin"], data["suggested"])
        self.assertEqual(["snow", "krill", "spindrift"], learned["learned"])
        self.assertNotIn("chinstrap penguin", learn_event.details["words"], "a weak species teaches nothing")
        self.assertEqual(["snow", "krill", "spindrift"], learn_event.details["words"])
        self.assertEqual("fast", event.details["role"])
        self.assertEqual('{"raw": true}', event.details["raw_response"])
        self.assertEqual(["gentoo penguin", "ice", "overcast", "snow", "krill", "spindrift"], sidecar["keywords"])
        self.assertIn("<rdf:li>gentoo penguin</rdf:li>", xmp)
        self.assertIn("<rdf:li>krill</rdf:li>", xmp)
        self.assertEqual(2, len(FakeTagger.calls))
        self.assertIn("wildlife", FakeTagger.calls[0])

    def test_tag_job_explicit_ids_validate_and_keyword_add_remove_round_trip(self):
        class FakeTagger:
            model, role = "fast-model", "fast"
            def tag(self, preview, *, vocabulary=None):
                return TagResult("d", ["ice"], "", [], 0.9, "{}", species=[], conditions=[])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new("RGB", (120, 90)).save(root / "p.jpg")
            with Library(Settings(library_dir=root / "library")) as library:
                library.import_path(root / "p.jpg")
                photo_id = library.catalog.list_photos()[0].id
                with self.assertRaisesRegex(ValueError, "unknown photo ID: 99"):
                    library.start_tag_job(FakeTagger(), scope=[photo_id, 99])
                with self.assertRaises(ValueError):
                    library.start_tag_job(FakeTagger(), scope=[])
                job = library.start_tag_job(FakeTagger(), scope=[photo_id])
                library.wait_for_job(job.id)
                library.catalog.save_tags(photo_id, library.catalog.get_tags(photo_id)["data"] | {"suggested": ["krill"]}, "fast-model")
                added = library.add_suggested_keyword(photo_id, "krill")
                self.assertEqual((["ice", "krill"], []), (added["subjects"], added["suggested"]))
                self.assertIn("krill", load_vocabulary(library.settings)["learned"], "a hand-accepted keyword teaches the vocabulary")
                removed = library.remove_keyword(photo_id, "ice")
                self.assertEqual((["krill"], ["ice"]), (removed["subjects"], removed["suggested"]))
                with self.assertRaises(ValueError):
                    library.add_suggested_keyword(photo_id, "orca")
                kinds = [e.kind for e in library.catalog.events_for(photo_id)]
                self.assertEqual(["keyword_added", "vocabulary_learned", "keyword_removed"], kinds[-3:])

    def _two_tagged_photos(self, root: Path, library: Library) -> tuple[int, int]:
        for index in range(2):
            Image.new("RGB", (120 + index, 90), (index * 40, 0, 0)).save(root / f"k{index}.jpg")
        library.import_path(root)
        a, b = sorted(photo.id for photo in library.catalog.list_photos())
        library.catalog.save_tags(a, {"subjects": ["ice", "bay"], "suggested": []}, "fast-model")
        library.catalog.save_tags(b, {"subjects": ["Ice"], "suggested": []}, "fast-model")
        return a, b

    def test_forget_keyword_strips_every_photo_and_rewrites_sidecars(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with Library(Settings(library_dir=root / "library")) as library:
                a, b = self._two_tagged_photos(root, library)
                result = library.forget_keyword("ice")
                self.assertEqual({"word": "ice", "removed_from": 2}, result)
                self.assertEqual(["bay"], library.catalog.get_tags(a)["data"]["subjects"])
                self.assertEqual([], library.catalog.get_tags(b)["data"]["subjects"])
                sidecar = json.loads(library.sidecars.paths(library.catalog.get_photo(a))[0].read_text())
                self.assertEqual(["bay"], sidecar["keywords"])
                event = [e for e in library.catalog.events_for(None) if e.kind == "keyword_forgotten"][-1]
                self.assertEqual({"word": "ice", "removed_from": 2}, event.details)
                self.assertIn("ice", load_vocabulary_blocked(library.settings))
                self.assertEqual({"word": "ice", "removed_from": 0}, library.forget_keyword("ice"))

    def test_vocabulary_report_counts_photos_per_word(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with Library(Settings(library_dir=root / "library")) as library:
                self._two_tagged_photos(root, library)
                report = library.vocabulary_report()
                self.assertEqual(2, report["counts"]["ice"])
                self.assertEqual(1, report["counts"]["bay"])
                self.assertEqual([], report["blocked"])
                self.assertIn("gentoo penguin", report["categories"]["wildlife"])

    def test_add_suggested_species_learns_as_wildlife(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with Library(Settings(library_dir=root / "library")) as library:
                a, _ = self._two_tagged_photos(root, library)
                library.catalog.save_tags(a, {"subjects": [], "suggested": ["skua chick"],
                                              "species": [{"name": "Skua Chick", "confidence": 0.4}]}, "fast-model")
                library.add_suggested_keyword(a, "skua chick")
                vocab = load_vocabulary(library.settings)
                self.assertIn("skua chick", vocab["wildlife"])
                self.assertNotIn("skua chick", vocab.get("learned", []))

    def test_reject_review_job_stores_problems_and_never_changes_decisions(self):
        class FakeTagger:
            model, role = "fast-model", "fast"
            def review_rejects(self, preview):
                blurry = preview.stat().st_size % 2 == 0  # deterministic per generated preview
                return RejectReview([{"kind": "motion_blur", "confidence": 0.84}] if blurry else [], blurry, 0.8, '{"r":1}')

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(4):
                Image.new("RGB", (120 + index, 90), (index * 20, 0, 0)).save(root / f"p{index}.jpg")
            with Library(Settings(library_dir=root / "library")) as library:
                library.import_path(root)
                job = library.start_reject_review_job(FakeTagger(), scope="all")
                library.wait_for_job(job.id)
                photos = library.catalog.list_photos()
                self.assertTrue(all(photo.decision == "none" for photo in photos))
                suggestions = library.reject_suggestions()
                self.assertEqual(sorted(s["photo_id"] for s in suggestions),
                                 sorted(p.id for p in photos if library.catalog.get_tags(p.id)["data"]["problems"]["suggest_reject"]))
                first = photos[0].id
                event = [e for e in library.catalog.events_for(first) if e.kind == "reject_suggested"][-1]
                self.assertEqual(("fast", '{"r":1}'), (event.details["role"], event.details["raw_response"]))
                self.assertEqual("succeeded", library.job_status(job.id)["status"])

    def test_apply_reject_suggestions_uses_ids_not_counts_and_keep_dismisses(self):  # Review Focus
        class FakeTagger:
            model, role = "fast-model", "fast"
            def review_rejects(self, preview):
                return RejectReview([{"kind": "misfire", "confidence": 0.9}], True, 0.9, "{}")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(3):
                Image.new("RGB", (120, 90), (index, 0, 0)).save(root / f"p{index}.jpg")
            with Library(Settings(library_dir=root / "library")) as library:
                library.import_path(root)
                library.wait_for_job(library.start_reject_review_job(FakeTagger(), scope="all").id)
                a, b, c = [s["photo_id"] for s in library.reject_suggestions()]
                library.set_decision(c, "reject")  # decided elsewhere while the list was on screen
                with self.assertRaisesRegex(ValueError, f"photo {c} is not a current reject suggestion"):
                    library.apply_reject_suggestions([a, c], "reject")
                self.assertEqual("none", library.catalog.get_photo(a).decision)
                result = library.apply_reject_suggestions([a], "reject")
                self.assertEqual(([a], ["reject"], 1), ([d["photo_id"] for d in result["decided"]], [d["decision"] for d in result["decided"]], result["remaining"]))
                self.assertEqual({"photo_id", "decision", "rating", "group_id", "automatic_pick_id"}, set(result["decided"][0]))
                self.assertEqual("reject", library.catalog.get_photo(a).decision)
                result = library.apply_reject_suggestions([b], "keep")
                self.assertEqual(([b], ["none"], 0), ([d["photo_id"] for d in result["decided"]], [d["decision"] for d in result["decided"]], result["remaining"]))
                self.assertEqual("none", library.catalog.get_photo(b).decision)
                self.assertIn("dismissed_at", library.catalog.get_tags(b)["data"]["problems"])
                with self.assertRaises(ValueError):
                    library.apply_reject_suggestions([b], "maybe")

    def test_reject_review_merges_into_existing_tags_and_records_failures(self):
        class FakeTagger:
            model, role = "fast-model", "fast"
            def review_rejects(self, preview):
                raise OmlxError("offline")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new("RGB", (120, 90)).save(root / "p.jpg")
            with Library(Settings(library_dir=root / "library")) as library:
                library.import_path(root)
                photo_id = library.catalog.list_photos()[0].id
                library.catalog.save_tags(photo_id, {"subjects": ["ice"]}, "other")
                job = library.start_reject_review_job(FakeTagger(), scope=[photo_id])
                library.wait_for_job(job.id)
                self.assertEqual({"reviewed": 0, "skipped": 0, "failed": 1, "suggested": 0}, library.job_status(job.id)["result"])
                self.assertEqual("reject_review_failed", library.catalog.events_for(photo_id)[-1].kind)
                FakeTagger.review_rejects = lambda self, preview: RejectReview([], False, 0.7, "{}")
                library.wait_for_job(library.start_reject_review_job(FakeTagger(), scope=[photo_id]).id)
                data = library.catalog.get_tags(photo_id)["data"]
                self.assertEqual((["ice"], False, []), (data["subjects"], data["problems"]["suggest_reject"], data["problems"]["kinds"]))

    def test_tag_job_still_tags_a_photo_that_only_has_reject_problems(self):
        class FakeTagger:
            model, role = "fast-model", "fast"
            def review_rejects(self, preview):
                return RejectReview([], False, 0.7, "{}")
            def tag(self, preview, vocabulary=None):
                return TagResult("ice", ["ice"], "", [], 0.9, "{}")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new("RGB", (120, 90)).save(root / "p.jpg")
            with Library(Settings(library_dir=root / "library")) as library:
                library.import_path(root)
                library.wait_for_job(library.start_reject_review_job(FakeTagger(), scope="all").id)
                job = library.start_tag_job(FakeTagger(), scope="all")
                library.wait_for_job(job.id)
                self.assertEqual(1, library.job_status(job.id)["result"]["tagged"])
                self.assertIn("problems", library.catalog.get_tags(library.catalog.list_photos()[0].id)["data"])

    def test_assist_job_tags_reviews_and_captions_keepers_and_skips_finished_work(self):
        from lumen.omlx import RejectReview, TagResult

        class FakeFast:
            model, role, tags, reviews = "fast-model", "fast", 0, 0
            def tag(self, preview, *, vocabulary=None):
                FakeFast.tags += 1
                return TagResult("A lion", ["lion"], "savanna", [], 0.9, "{}", species=[{"name": "lion", "confidence": 0.95}], conditions=[])
            def review_rejects(self, preview):
                FakeFast.reviews += 1
                return RejectReview([], False, 0.8, "{}")

        class FakeReview:
            model, role, captions = "review-model", "review", 0
            def caption(self, preview):
                FakeReview.captions += 1
                return Caption("Lion at rest.", "A lion lying in dry grass.", "{}")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(3):
                Image.new("RGB", (120, 90), (index * 40, 0, 0)).save(root / f"p{index}.jpg")
            with Library(Settings(library_dir=root / "library")) as library:
                library.import_path(root)
                photos = library.catalog.list_photos()
                library.set_caption(photos[0].id, caption="Mine.", alt_text="My alt.")  # photographer's words are never replaced
                job = library.start_assist_job(FakeFast(), FakeReview(), scope="keepers")
                library.wait_for_job(job.id)
                status = library.job_status(job.id)
                data = [library.catalog.get_tags(photo.id)["data"] for photo in photos]
                again = library.start_assist_job(FakeFast(), FakeReview(), scope="keepers")
                library.wait_for_job(again.id)
                rerun = library.job_status(again.id)
        self.assertEqual(("assist", "succeeded", 3), (status["kind"], status["status"], status["processed"]))
        self.assertEqual({"tagged": 3, "reviewed": 3, "captioned": 2, "skipped": 0, "failed": 0, "suggested": 0}, status["result"])
        self.assertTrue(all(item["description"] == "A lion" and "problems" in item for item in data))
        self.assertEqual(["Mine.", "Lion at rest.", "Lion at rest."], [item["caption"] for item in data])
        self.assertEqual((3, 3, 2), (FakeFast.tags, FakeFast.reviews, FakeReview.captions))
        self.assertEqual({"tagged": 0, "reviewed": 0, "captioned": 0, "skipped": 3, "failed": 0, "suggested": 0}, rerun["result"], "a rerun does no model work")

    def test_low_confidence_species_named_in_subjects_stays_suggested(self):
        class FakeTagger:
            model, role = "fast-model", "fast"
            def tag(self, preview, *, vocabulary=None):
                return TagResult("d", ["Chinstrap Penguin", "ice"], "", [], 0.9, "{}",
                                 species=[{"name": "chinstrap penguin", "confidence": 0.4}], conditions=[])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new("RGB", (120, 90)).save(root / "p.jpg")
            with Library(Settings(library_dir=root / "library")) as library:
                library.import_path(root / "p.jpg")
                photo_id = library.catalog.list_photos()[0].id
                library.tag_photo(photo_id, FakeTagger())
                data = library.catalog.get_tags(photo_id)["data"]
        self.assertEqual(["ice"], data["subjects"])
        self.assertEqual(1, [word.casefold() for word in data["suggested"]].count("chinstrap penguin"))

    def _lights(self, root, count=4, exposure="15"):
        for index in range(count):
            image = Image.new("RGB", (120, 90), (0, 0, index * 20)); exif = Image.Exif()
            exif[36867] = f"2026:10:22 22:00:{index * 10:02d}"; exif[33434] = int(exposure)  # one rational; a (15, 1) tuple is two values
            image.save(root / f"l{index}.jpg", exif=exif)

    def test_tag_job_triages_long_exposures_and_triage_job_covers_a_group(self):
        class FakeTagger:
            model, role = "fast-model", "fast"
            triaged = []
            def tag(self, preview, *, vocabulary=None):
                return TagResult("stars", [], "", [], 0.9, "{}", species=[], conditions=[])
            def triage_astro(self, preview):
                FakeTagger.triaged.append(preview.name)
                cloudy = len(FakeTagger.triaged) == 2
                return AstroTriage("cloud" if cloudy else "night_sky", 0.9, ["cloud_band"] if cloudy else [], '{"t":1}')

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self._lights(root)
            Image.new("RGB", (120, 90)).save(root / "day.jpg")  # no exposure → not triaged
            with Library(Settings(library_dir=root / "library")) as library:
                library.import_path(root)
                self.assertTrue(all(p.exposure_seconds == 15.0 for p in library.catalog.list_photos() if p.path.endswith(".jpg") and "day" not in p.path))
                library.wait_for_job(library.start_tag_job(FakeTagger(), scope="all").id)
                self.assertEqual(4, len(FakeTagger.triaged))
                group = next(g for g in library.list_groups() if len(g["photos"]) == 4)
                triage = library.astro_triage(group["id"])
                self.assertTrue(triage["astro_cue"])
                self.assertEqual(1, sum(light["flagged"] for light in triage["lights"]))
                flagged = next(light for light in triage["lights"] if light["flagged"])
                self.assertEqual(("cloud", ["cloud_band"]), (flagged["sky"]["class"], flagged["issues"]))
                event = [e for e in library.catalog.events_for(flagged["photo_id"]) if e.kind == "astro_triaged"][-1]
                self.assertEqual('{"t":1}', event.details["raw_response"])
                self.assertNotIn("raw_response", json.dumps(triage))
                FakeTagger.triaged.clear()
                library.wait_for_job(library.start_triage_job(FakeTagger(), group_id=group["id"]).id)
                self.assertEqual(4, len(FakeTagger.triaged))
                self.assertEqual(sorted(l["photo_id"] for l in triage["lights"]), sorted(p["id"] for p in group["photos"]))

    def test_tag_job_counts_triage_failure_separately_and_keeps_the_tag(self):
        class FakeTagger:
            model, role = "fast-model", "fast"
            def tag(self, preview, *, vocabulary=None):
                return TagResult("stars", [], "", [], 0.9, "{}", species=[], conditions=[])
            def triage_astro(self, preview):
                raise AstroResponseError("bad sky", '{"sky":"day"}')

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self._lights(root, 2)
            with Library(Settings(library_dir=root / "library")) as library:
                library.import_path(root)
                job = library.start_tag_job(FakeTagger(), scope="all")
                library.wait_for_job(job.id)
                status = library.job_status(job.id)
                self.assertEqual("succeeded", status["status"])
                self.assertEqual({"tagged": 2, "skipped": 0, "failed": 0, "triage_failed": 2}, status["result"])
                photo = library.catalog.list_photos()[0]
                self.assertEqual("stars", library.catalog.get_tags(photo.id)["data"]["description"])
                self.assertIn("astro_triage_failed", [e.kind for e in library.catalog.events_for(photo.id)])
                with self.assertRaises(ValueError):
                    library.start_triage_job(FakeTagger(), group_id=999)

    def test_astro_triage_flags_from_metrics_without_a_model_and_cue_needs_a_majority(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self._lights(root, 3)
            with Library(Settings(library_dir=root / "library")) as library:
                library.import_path(root)
                group = library.list_groups()[0]
                photo = group["photos"][0]
                metrics = library.catalog.get_metrics(photo["id"])
                library.catalog.save_metrics(photo["id"], replace(metrics, astro=metrics.astro | {"trailing_warning": True}), photo["total_score"])
                triage = library.astro_triage(group["id"])
                self.assertFalse(triage["astro_cue"])
                self.assertEqual([photo["id"]], [l["photo_id"] for l in triage["lights"] if l["flagged"]])
                self.assertIsNone(triage["lights"][0]["sky"])

    def _burst(self, root, count=3):
        for index in range(count):
            image = Image.new("RGB", (120, 90), (index * 30, 0, 0))
            exif = Image.Exif(); exif[36867] = f"2026:10:22 20:00:0{index}"
            image.save(root / f"b{index}.jpg", exif=exif)

    def test_burst_critique_records_suggestion_without_changing_the_pick(self):
        class FakeTagger:
            model, role = "review-model", "review"
            def critique_burst(self, frames):
                self.frames = list(frames)
                return BurstCritique(frames[-1][0], "Best timing.", 0.72, '{"r":1}')

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self._burst(root)
            tagger = FakeTagger()
            with Library(Settings(library_dir=root / "library")) as library:
                library.import_path(root)
                group = library.list_groups()[0]
                self.assertEqual(3, len(group["photos"]))
                pick_before = group["automatic_pick_id"]
                result = library.critique_burst(group["id"], tagger)
                group_after = library.list_groups()[0]
                self.assertEqual(pick_before, group_after["automatic_pick_id"])
                self.assertEqual(int(tagger.frames[-1][0]), result["photo_id"])
                self.assertEqual([str(p["id"]) for p in group["photos"]], [fid for fid, _ in tagger.frames])
                event = [e for e in library.catalog.events_for(result["photo_id"]) if e.kind == "burst_reviewed"][-1]
                self.assertEqual(("review", '{"r":1}', group["id"]), (event.details["role"], event.details["raw_response"], event.details["group_id"]))
                self.assertEqual(result | {"raw_response": None}, (library.latest_critique(group["id"]) or {}) | {"raw_response": None})
                self.assertNotIn("raw_response", result)
                library.set_decision(result["photo_id"], "reject")
                self.assertIsNone(library.latest_critique(group["id"]), "a critique of a since-rejected frame is not offered")
                library.set_decision(result["photo_id"], "none")
                library.set_decision(group["photos"][1]["id"], "reject")
                library.set_decision(group["photos"][2]["id"], "reject")
                with self.assertRaisesRegex(ValueError, "at least two frames"):
                    library.critique_burst(group["id"], tagger)

    def test_burst_critique_caps_at_eight_frames_ranked_and_fails_cleanly_when_burst_changes(self):  # Review Focus
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self._burst(root, 10)
            with Library(Settings(library_dir=root / "library")) as library:
                library.import_path(root)
                group = library.list_groups()[0]
                last = group["photos"][-1]["id"]  # ranked last, so outside the 8-frame cap

                class ChangingTagger:
                    model, role = "review-model", "review"
                    def critique_burst(self, frames):
                        self.frames = list(frames)
                        return BurstCritique(str(last), "ok", 0.5, '{"r":2}')  # a member of the burst but not of the supplied set

                tagger = ChangingTagger()
                with self.assertRaises((ValueError, BurstResponseError)):
                    library.critique_burst(group["id"], tagger)
                self.assertEqual([str(p["id"]) for p in group["photos"][:8]], [fid for fid, _ in tagger.frames])
                failed = [e for e in library.catalog.events_for(None) if e.kind == "burst_review_failed"][-1]
                self.assertEqual(([p["id"] for p in group["photos"][:8]], '{"r":2}'), (failed.details["candidate_ids"], failed.details["raw_response"]))

                class RemovingTagger:
                    model, role = "review-model", "review"
                    def critique_burst(self, frames):
                        chosen = int(frames[0][0])
                        with Library(Settings(library_dir=root / "library")) as other:  # the photographer rejects that frame mid-call
                            other.set_decision(chosen, "reject")
                            self.pick_after_reject = other.list_groups()[0]["automatic_pick_id"]
                        return BurstCritique(frames[0][0], "ok", 0.5, "{}")

                removing = RemovingTagger()
                with self.assertRaisesRegex(ValueError, "burst changed during review"):
                    library.critique_burst(group["id"], removing)
                self.assertEqual("burst_review_failed", [e for e in library.catalog.events_for(None)][-1].kind)
                # Rejecting the top frame legitimately reranks; the failed critique leaves that pick alone.
                self.assertEqual(removing.pick_after_reject, library.list_groups()[0]["automatic_pick_id"])

                for photo in group["photos"][1:4]:  # with the mid-call reject, 4 of 10 rejected
                    library.set_decision(photo["id"], "reject")

                class RecordingTagger:
                    model, role = "review-model", "review"
                    def critique_burst(self, frames):
                        self.frames = list(frames)
                        return BurstCritique(frames[0][0], "ok", 0.5, "{}")

                recording = RecordingTagger()
                library.critique_burst(group["id"], recording)
                kept = [p["id"] for p in library.list_groups()[0]["photos"] if p["decision"] != "reject"]
                self.assertEqual(kept, [int(fid) for fid, _ in recording.frames])  # never padded with rejected frames
                self.assertEqual(6, len(kept))


if __name__ == "__main__":
    unittest.main()
