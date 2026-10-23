import tempfile
import sqlite3
import unittest
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path

from lumen.catalog import Catalog, GroupFilters
from lumen.config import Settings
from lumen.library import Library
from lumen.domain import Artifact, Job, Photo, PhotoEdit, PhotoMetrics
from lumen.composition import CropRect


def seed_filter_photos(catalog):
    """Two dated days on cameras A and B plus an undated burst; returns {name: photo_id} and group ids."""
    ids = {}
    specs = [("a1", "A", 21, "none", 3), ("a2", "A", 21, "flag", 1), ("a3", "A", 21, "reject", 5),
             ("b1", "B", 22, "reject", 0), ("b2", "B", 22, "none", 4), ("c1", "B", None, "none", 0)]
    for name, camera, day, decision, rating in specs:
        captured = datetime(2026, 10, day, 8, 0, tzinfo=timezone.utc) if day else None
        ids[name] = catalog.upsert_photo(Photo(path=f"/photos/{name}.ARW", fingerprint=name * 8, captured_at=captured, camera_id=camera))
        catalog.set_decision(ids[name], decision)
        catalog.set_rating(ids[name], rating)
    groups = {
        "g1": catalog.assign_group("g1", "A", "2026-10-21T08:00:00+00:00", [ids["a1"], ids["a2"], ids["a3"]], ids["a1"], {}),
        "g2": catalog.assign_group("g2", "B", "2026-10-22T08:00:00+00:00", [ids["b1"], ids["b2"]], ids["b1"], {}),
        "g3": catalog.assign_group("g3", "B", None, [ids["c1"]], ids["c1"], {}),
    }
    catalog.save_tags(ids["a1"], {"description": "Red fox in snow", "subjects": ["animal"]}, "model")
    catalog.save_tags(ids["b2"], {"description": "Bird by the lake", "subjects": ["Heron", "water"]}, "model")
    return ids, groups


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "catalog.sqlite3"
        self.catalog = Catalog(self.db_path)

    def tearDown(self):
        self.catalog.close()
        self.tmp.cleanup()

    def test_upserting_same_path_updates_instead_of_duplicating(self):
        first = Photo(
            path="/photos/a.ARW",
            fingerprint="abc123",
            captured_at=datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc),
            camera_id="ILCE-7M4:123",
        )
        photo_id = self.catalog.upsert_photo(first)
        changed = Photo(
            path="/photos/a.ARW",
            fingerprint="def456",
            captured_at=first.captured_at,
            camera_id=first.camera_id,
        )

        self.assertEqual(photo_id, self.catalog.upsert_photo(changed))
        photos = self.catalog.list_photos()
        self.assertEqual(1, len(photos))
        self.assertEqual("def456", photos[0].fingerprint)

    def test_crop_round_trip_clear_keeps_append_only_history(self):
        photo_id = self.catalog.upsert_photo(
            Photo(path="/photos/crop.jpg", fingerprint="abc", captured_at=None, camera_id="unknown")
        )
        edit = PhotoEdit(
            photo_id=photo_id, candidate_id="crop-0123456789abcdef",
            rect=CropRect(0.25, 0.1, 0.5, 0.8), aspect="4:5",
            source_width=4000, source_height=3000, author="manual",
            rationale="Padded face", selected_at=datetime.now(timezone.utc),
        )

        self.catalog.save_crop(edit)

        self.assertEqual(edit, self.catalog.get_crop(photo_id))
        self.assertEqual(["crop_selected"], [event.kind for event in self.catalog.events_for(photo_id)])
        self.catalog.clear_crop(photo_id)
        self.catalog.clear_crop(photo_id)

        self.assertIsNone(self.catalog.get_crop(photo_id))
        self.assertEqual(["crop_selected", "crop_cleared"], [event.kind for event in self.catalog.events_for(photo_id)])

    def test_crop_persistence_rejects_invalid_edit_fields(self):
        photo_id = self.catalog.upsert_photo(
            Photo(path="/photos/invalid.jpg", fingerprint="abc", captured_at=None, camera_id="unknown")
        )
        for changes in (
            {"author": "robot"}, {"candidate_id": ""}, {"source_width": 0},
            {"rationale": "x" * 1001},
        ):
            with self.subTest(changes=changes):
                values = dict(photo_id=photo_id, candidate_id="candidate", rect=CropRect(.1, .1, .5, .5),
                              aspect="1:1", source_width=4000, source_height=3000, author="manual",
                              rationale="Centered fallback", selected_at=datetime.now(timezone.utc))
                values.update(changes)
                with self.assertRaises(ValueError):
                    self.catalog.save_crop(PhotoEdit(**values))

    def test_equal_content_at_different_paths_has_independent_identity(self):
        base = dict(
            fingerprint="abc123",
            captured_at=None,
            camera_id="unknown",
        )
        left = self.catalog.upsert_photo(Photo(path="/a/copy.ARW", **base))
        right = self.catalog.upsert_photo(Photo(path="/b/copy.ARW", **base))

        self.assertNotEqual(left, right)
        self.assertEqual(2, len(self.catalog.list_photos()))

    def test_provenance_is_returned_in_append_order(self):
        photo_id = self.catalog.upsert_photo(
            Photo(path="/photos/a.ARW", fingerprint="abc", captured_at=None, camera_id="unknown")
        )
        self.catalog.append_event(photo_id, "imported", {"tool": "test"})
        self.catalog.append_event(photo_id, "analyzed", {"sharpness": 0.7})

        events = self.catalog.events_for(photo_id)
        self.assertEqual(["imported", "analyzed"], [event.kind for event in events])
        self.assertEqual(0.7, events[1].details["sharpness"])

    def test_settings_creates_managed_directories(self):
        root = Path(self.tmp.name) / "library"
        settings = Settings(library_dir=root)

        settings.ensure_directories()

        self.assertTrue(settings.database_path.parent.is_dir())
        self.assertTrue(settings.preview_dir.is_dir())
        self.assertTrue(settings.sidecar_dir.is_dir())
        self.assertTrue(settings.output_dir.is_dir())
        self.assertTrue(settings.trash_dir.is_dir())

    def test_backup_preserves_committed_catalog_before_deletion(self):
        photo_id = self.catalog.upsert_photo(
            Photo(path="/photos/a.ARW", fingerprint="a", captured_at=None, camera_id="A")
        )
        snapshot = Path(self.tmp.name) / "snapshot.sqlite3"

        self.catalog.backup(snapshot)
        self.catalog.delete_photos([photo_id])

        with closing(sqlite3.connect(snapshot)) as saved:
            self.assertEqual([(photo_id, "/photos/a.ARW")], saved.execute("SELECT id, path FROM photos").fetchall())

    def test_explicit_group_ids_remove_empty_groups_without_removing_other_groups(self):
        photo_id = self.catalog.upsert_photo(
            Photo(path="/photos/a.ARW", fingerprint="a", captured_at=None, camera_id="A")
        )
        retained = self.catalog.assign_group("retained", "A", None, [photo_id], photo_id, {})
        empty = self.catalog.assign_group("empty", "A", None, [], photo_id, {})

        self.assertEqual(0, self.catalog.delete_photos([], group_ids=[empty]))

        self.assertEqual([(retained,)], [tuple(row) for row in self.catalog.connection.execute("SELECT id FROM photo_groups")])
        self.assertIsNotNone(self.catalog.get_photo(photo_id))

    def test_explicit_group_deletion_rejects_missing_or_unselected_member_ids(self):
        photo_id = self.catalog.upsert_photo(
            Photo(path="/photos/a.ARW", fingerprint="a", captured_at=None, camera_id="A")
        )
        group_id = self.catalog.assign_group("retained", "A", None, [photo_id], photo_id, {})

        for photo_ids, group_ids in (([photo_id], [99999]), ([], [group_id]), ([photo_id], [group_id, group_id])):
            with self.subTest(photo_ids=photo_ids, group_ids=group_ids), self.assertRaises(ValueError):
                self.catalog.delete_photos(photo_ids, group_ids=group_ids)

        self.assertEqual([photo_id], self.catalog.group_photo_ids(group_id))

    def test_tags_round_trip_as_structured_data(self):
        photo_id = self.catalog.upsert_photo(
            Photo(path="/photos/tag.ARW", fingerprint="tag", captured_at=None, camera_id="A")
        )
        payload = {"description": "A bird", "subjects": ["bird"], "confidence": 0.9}

        self.catalog.save_tags(photo_id, payload, model="local-vlm")

        stored = self.catalog.get_tags(photo_id)
        self.assertEqual("local-vlm", stored["model"])
        self.assertEqual(["bird"], stored["data"]["subjects"])

    def test_artifact_survives_source_row_deletion_with_denormalized_identity(self):
        photo_id = self.catalog.upsert_photo(
            Photo(path="/photos/original.ARW", fingerprint="raw-fingerprint", captured_at=None, camera_id="A")
        )

        artifact = self.catalog.save_artifact(
            photo_id=photo_id,
            source_name="original.ARW",
            source_fingerprint="raw-fingerprint",
            kind="jpeg",
            path="/library/outputs/jpeg/original-lumen.jpg",
            settings={"quality": 92, "color_space": "sRGB"},
            output_fingerprint="jpeg-fingerprint",
        )
        self.catalog.connection.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
        self.catalog.connection.commit()

        saved = self.catalog.list_artifacts()[0]
        self.assertIsInstance(artifact, Artifact)
        self.assertIsNone(saved.photo_id)
        self.assertEqual("original.ARW", saved.source_name)
        self.assertEqual("raw-fingerprint", saved.source_fingerprint)
        self.assertEqual("/library/outputs/jpeg/original-lumen.jpg", saved.path)
        self.assertEqual({"quality": 92, "color_space": "sRGB"}, saved.settings)
        self.assertEqual("jpeg-fingerprint", saved.output_fingerprint)

    def test_existing_catalog_gains_artifacts_table(self):
        legacy_path = Path(self.tmp.name) / "legacy.sqlite3"
        legacy = Catalog(legacy_path)
        legacy.connection.execute("DROP TABLE IF EXISTS artifacts")
        legacy.connection.execute("DROP TABLE IF EXISTS jobs")
        legacy.connection.commit()
        legacy.close()

        migrated = Catalog(legacy_path)
        try:
            tables = {
                row["name"]
                for row in migrated.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertIn("artifacts", tables)
            self.assertIn("photo_edits", tables)
            self.assertIn("jobs", tables)
        finally:
            migrated.close()

    def test_migration_adds_job_label_and_counts_to_existing_database(self):
        legacy_path = Path(self.tmp.name) / "pre-migration.sqlite3"
        with closing(sqlite3.connect(legacy_path)) as legacy:
            legacy.executescript(
                """
                CREATE TABLE jobs (
                    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
                    progress REAL NOT NULL, manifest_path TEXT NOT NULL, log_path TEXT NOT NULL,
                    result_path TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                INSERT INTO jobs VALUES (1, 'astro', 'queued', 0, '/m.json', '/j.log', NULL, NULL,
                    '2026-10-21T10:00:00+00:00', '2026-10-21T10:00:00+00:00');
                """
            )
        for _ in range(2):
            with Catalog(legacy_path) as migrated:
                columns = [row["name"] for row in migrated.connection.execute("PRAGMA table_info(jobs)")]
                job = migrated.get_job(1)
        for column in ("label", "processed", "total"):
            self.assertEqual(1, columns.count(column))
        self.assertEqual(("astro", "/m.json", None, None, None), (job.kind, job.manifest_path, job.label, job.processed, job.total))
        with Catalog(legacy_path) as migrated:
            updated = migrated.update_job(1, status="running", label="DSC0001.ARW", processed=1, total=3)
            self.assertEqual(updated, migrated.get_job(1))
            self.assertEqual("DSC0001.ARW", migrated.update_job(1, progress=0.5).label)
            for bad in ({"processed": -1}, {"processed": 4}, {"total": 0}, {"label": ""}, {"label": "x" * 201}, {"processed": True}):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    migrated.update_job(1, **bad)

    def test_mirror_path_is_set_and_preserved_by_upsert(self):
        photo = Photo(path="/primary/a.ARW", fingerprint="abc", captured_at=None, camera_id="cam")
        photo_id = self.catalog.upsert_photo(photo)
        self.assertIsNone(self.catalog.get_photo(photo_id).mirror_path)
        self.catalog.set_mirror_path(photo_id, "/mirror/a.ARW")
        self.assertEqual(photo_id, self.catalog.upsert_photo(photo))
        self.assertEqual("/mirror/a.ARW", self.catalog.get_photo(photo_id).mirror_path)
        self.catalog.set_mirror_path(photo_id, None)
        self.assertIsNone(self.catalog.get_photo(photo_id).mirror_path)
        columns = [row["name"] for row in self.catalog.connection.execute("PRAGMA table_info(photos)")]
        self.assertEqual(1, columns.count("mirror_path"))

    def test_migration_adds_decision_and_rating_and_upsert_preserves_them(self):
        legacy_path = Path(self.tmp.name) / "pre-decision.sqlite3"
        with closing(sqlite3.connect(legacy_path)) as legacy:
            legacy.executescript(
                """
                CREATE TABLE photos (
                    id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL,
                    captured_at TEXT, camera_id TEXT NOT NULL, preview_path TEXT,
                    format TEXT NOT NULL DEFAULT 'ARW', group_id INTEGER,
                    manual_pick INTEGER NOT NULL DEFAULT 0, imported_at TEXT NOT NULL
                );
                INSERT INTO photos (id, path, fingerprint, camera_id, imported_at)
                    VALUES (1, '/photos/a.ARW', 'abc', 'A1', '2026-10-21T10:00:00+00:00');
                """
            )
        for _ in range(2):
            with Catalog(legacy_path) as migrated:
                columns = [row["name"] for row in migrated.connection.execute("PRAGMA table_info(photos)")]
                photo = migrated.get_photo(1)
        self.assertEqual((1, 1), (columns.count("decision"), columns.count("rating")))
        self.assertEqual(("none", 0), (photo.decision, photo.rating))
        with Catalog(legacy_path) as migrated:
            migrated.set_decision(1, "flag")
            migrated.set_rating(1, 4)
            migrated.upsert_photo(Photo(path="/photos/a.ARW", fingerprint="def", captured_at=None, camera_id="A1"))
            photo = migrated.get_photo(1)
            self.assertEqual(("def", "flag", 4), (photo.fingerprint, photo.decision, photo.rating))

    def test_migration_adds_preview_kind_and_update_preview_round_trips(self):
        legacy_path = Path(self.tmp.name) / "pre-refine.sqlite3"
        with closing(sqlite3.connect(legacy_path)) as legacy:
            legacy.executescript(
                """
                CREATE TABLE photos (
                    id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL,
                    captured_at TEXT, camera_id TEXT NOT NULL, preview_path TEXT,
                    format TEXT NOT NULL DEFAULT 'ARW', group_id INTEGER,
                    manual_pick INTEGER NOT NULL DEFAULT 0, imported_at TEXT NOT NULL
                );
                INSERT INTO photos (id, path, fingerprint, camera_id, preview_path, imported_at)
                    VALUES (1, '/photos/a.ARW', 'abc', 'A1', '/lib/previews/abc.jpg', '2026-10-21T10:00:00+00:00');
                """
            )
        for _ in range(2):
            with Catalog(legacy_path) as migrated:
                columns = [row["name"] for row in migrated.connection.execute("PRAGMA table_info(photos)")]
                photo = migrated.get_photo(1)
        self.assertEqual(1, columns.count("preview_kind"))
        self.assertEqual("embedded", photo.preview_kind)
        with Catalog(legacy_path) as migrated:
            migrated.update_preview(1, "/lib/previews/abc-2048.jpg", "refined")
            photo = migrated.get_photo(1)
            self.assertEqual(("/lib/previews/abc-2048.jpg", "refined"), (photo.preview_path, photo.preview_kind))
            for bad in ("raw", "", None):
                with self.subTest(kind=bad), self.assertRaises(ValueError):
                    migrated.update_preview(1, "/lib/previews/x.jpg", bad)
            with self.assertRaises(ValueError):
                migrated.update_preview(99, "/lib/previews/x.jpg", "refined")
            self.assertEqual("refined", migrated.get_photo(1).preview_kind)
            migrated.upsert_photo(Photo(path="/photos/a.ARW", fingerprint="def", captured_at=None, camera_id="A1",
                                        preview_path="/lib/previews/def.jpg"))
            self.assertEqual(("/lib/previews/def.jpg", "embedded"), (migrated.get_photo(1).preview_path, migrated.get_photo(1).preview_kind),
                             "a re-import writes a fresh embedded preview")

    def test_migration_adds_exposure_and_merge_columns_and_upsert_round_trips_exposure(self):
        legacy_path = Path(self.tmp.name) / "pre-exposure.sqlite3"
        with closing(sqlite3.connect(legacy_path)) as legacy:
            legacy.executescript(
                """
                CREATE TABLE photos (
                    id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL,
                    captured_at TEXT, camera_id TEXT NOT NULL, preview_path TEXT,
                    format TEXT NOT NULL DEFAULT 'ARW', group_id INTEGER,
                    manual_pick INTEGER NOT NULL DEFAULT 0, imported_at TEXT NOT NULL
                );
                INSERT INTO photos (id, path, fingerprint, camera_id, imported_at)
                    VALUES (1, '/photos/a.ARW', 'abc', 'A1', '2026-10-21T10:00:00+00:00');
                """
            )
        for _ in range(2):
            with Catalog(legacy_path) as migrated:
                columns = [row["name"] for row in migrated.connection.execute("PRAGMA table_info(photos)")]
                photo = migrated.get_photo(1)
        self.assertEqual((1, 1), (columns.count("exposure_seconds"), columns.count("merged_into")))
        self.assertEqual((None, None), (photo.exposure_seconds, photo.merged_into))
        with Catalog(legacy_path) as migrated:
            migrated.upsert_photo(Photo(path="/photos/a.ARW", fingerprint="abc", captured_at=None, camera_id="A1", exposure_seconds=30.0))
            self.assertEqual(30.0, migrated.get_photo(1).exposure_seconds)
            self.assertEqual(30.0, migrated.list_photos()[0].exposure_seconds)

    def test_photos_for_filters_returns_matching_grouped_photo_ids(self):
        ids, _ = seed_filter_photos(self.catalog)
        self.assertEqual(sorted([ids["a1"], ids["a2"], ids["b2"], ids["c1"]]), self.catalog.photos_for_filters(GroupFilters()))
        self.assertEqual([ids["a1"], ids["a2"]], self.catalog.photos_for_filters(GroupFilters(camera="A")))
        self.assertEqual([ids["b1"]], self.catalog.photos_for_filters(GroupFilters(decision="reject", camera="B")))

    def test_set_decision_and_rating_validate_and_record_events(self):
        photo_id = self.catalog.upsert_photo(Photo(path="/photos/a.ARW", fingerprint="abc", captured_at=None, camera_id="A1"))
        rejected = self.catalog.set_decision(photo_id, "reject")
        self.assertEqual(("reject", 0), (rejected.decision, rejected.rating))
        rated = self.catalog.set_rating(photo_id, 3)
        self.assertEqual(("reject", 3), (rated.decision, rated.rating))
        self.assertEqual(rated, self.catalog.get_photo(photo_id))
        events = [(event.kind, event.details) for event in self.catalog.events_for(photo_id)]
        self.assertEqual([
            ("decision_changed", {"previous": "none", "next": "reject"}),
            ("rating_changed", {"previous": 0, "next": 3}),
        ], events)
        for bad in ("maybe", "", None, 1, "FLAG"):
            with self.subTest(decision=bad), self.assertRaises(ValueError):
                self.catalog.set_decision(photo_id, bad)
        for bad in (-1, 6, True, 2.0, "3", None):
            with self.subTest(rating=bad), self.assertRaises(ValueError):
                self.catalog.set_rating(photo_id, bad)
        for unknown in (999, "1", True):
            with self.subTest(photo_id=unknown), self.assertRaisesRegex(ValueError, "unknown photo ID"):
                self.catalog.set_decision(unknown, "flag")
            with self.subTest(photo_id=unknown), self.assertRaisesRegex(ValueError, "unknown photo ID"):
                self.catalog.set_rating(unknown, 1)
        self.assertEqual(2, len(self.catalog.events_for(photo_id)))
        self.assertEqual(("reject", 3), (self.catalog.get_photo(photo_id).decision, self.catalog.get_photo(photo_id).rating))

    def test_job_round_trip_preserves_paths_and_creation_order(self):
        first = self.catalog.create_job(
            kind="siril-stack",
            manifest_path="/library/outputs/astro/job 1/manifest ☄.json",
            log_path="/library/outputs/astro/job 1/Siril log.txt",
        )
        second = self.catalog.create_job(
            kind="jpeg-export",
            manifest_path="/library/outputs/jobs/2/manifest.json",
            log_path="/library/outputs/jobs/2/run.log",
        )

        self.assertIsInstance(first, Job)
        self.assertEqual("queued", first.status)
        self.assertEqual(0.0, first.progress)
        self.assertEqual("/library/outputs/astro/job 1/manifest ☄.json", first.manifest_path)
        self.assertEqual(first, self.catalog.get_job(first.id))
        self.assertEqual([first, second], self.catalog.list_jobs())

    def test_job_creation_rejects_invalid_fields_without_inserting(self):
        invalid = (
            {"kind": "", "manifest_path": "/m", "log_path": "/l"},
            {"kind": "astro", "manifest_path": Path("/m"), "log_path": "/l"},
            {"kind": "astro", "manifest_path": "/m", "log_path": ""},
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.catalog.create_job(**values)

        self.assertEqual([], self.catalog.list_jobs())

    def test_job_allows_only_declared_state_transitions(self):
        allowed = {
            "queued": ("running", "cancelled"),
            "running": ("succeeded", "failed", "cancelled", "interrupted"),
        }
        terminal = {"succeeded", "failed", "cancelled", "interrupted"}
        for source, targets in allowed.items():
            for target in targets:
                with self.subTest(source=source, target=target):
                    job = self.catalog.create_job(
                        kind="astro", manifest_path=f"/{source}-{target}.json",
                        log_path=f"/{source}-{target}.log",
                    )
                    if source == "running":
                        job = self.catalog.update_job(job.id, status="running", progress=0.25)
                    values = {"status": target}
                    if target == "succeeded":
                        values["progress"] = 1.0
                    elif target == "failed":
                        values["error"] = "processing failed"
                    updated = self.catalog.update_job(job.id, **values)
                    self.assertEqual(target, updated.status)
                    self.assertEqual(target in terminal, updated.status in terminal)

        rejected = (
            ("queued", "succeeded"), ("queued", "failed"), ("queued", "interrupted"),
            ("running", "queued"), ("running", "running"),
        )
        for source, target in rejected:
            with self.subTest(source=source, target=target):
                job = self.catalog.create_job(
                    kind="astro", manifest_path=f"/bad-{source}-{target}.json",
                    log_path=f"/bad-{source}-{target}.log",
                )
                if source == "running":
                    job = self.catalog.update_job(job.id, status="running")
                with self.assertRaises(ValueError):
                    self.catalog.update_job(job.id, status=target)
                self.assertEqual(source, self.catalog.get_job(job.id).status)

    def test_job_progress_and_terminal_validation_are_transactional(self):
        job = self.catalog.create_job(kind="astro", manifest_path="/m.json", log_path="/run.log")
        running = self.catalog.update_job(job.id, status="running", progress=0.5)
        self.assertEqual(0.5, running.progress)

        invalid_updates = (
            {"progress": 0.49}, {"progress": True}, {"progress": float("nan")},
            {"progress": float("inf")}, {"progress": 1.1},
            {"status": "succeeded", "progress": 0.9},
            {"status": "succeeded", "progress": 1.0, "error": "not clean"},
            {"status": "failed", "error": ""},
            {"status": "failed", "error": "x" * 1001},
            {"status": []},
            {"result_path": Path("/result.tif")},
        )
        for values in invalid_updates:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.catalog.update_job(job.id, **values)
            self.assertEqual(running, self.catalog.get_job(job.id))

        failed = self.catalog.update_job(job.id, status="failed", error="bad input")
        for values in ({"progress": 0.7}, {"status": "running"}, {"error": "changed"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.catalog.update_job(job.id, **values)
        self.assertEqual(failed, self.catalog.get_job(job.id))

    def test_job_rejects_unknown_identity_status_and_non_utc_time(self):
        with self.assertRaises(ValueError):
            self.catalog.get_job(0)
        with self.assertRaises(ValueError):
            self.catalog.get_job(999)
        with self.assertRaises(ValueError):
            self.catalog.update_job(999, status="running")

        now = datetime.now(timezone.utc)
        base = dict(
            id=1, kind="astro", status="queued", progress=0.0,
            manifest_path="/m", log_path="/l", result_path=None, error=None,
            created_at=now, updated_at=now,
        )
        for changes in (
            {"id": True}, {"status": "waiting"}, {"progress": False},
            {"created_at": now.replace(tzinfo=None)}, {"updated_at": "now"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                Job(**(base | changes))

    def test_recovery_interrupts_only_running_jobs_and_retains_paths(self):
        queued = self.catalog.create_job(kind="astro", manifest_path="/queued.json", log_path="/queued.log")
        running = self.catalog.create_job(kind="astro", manifest_path="/running.json", log_path="/running.log")
        running = self.catalog.update_job(
            running.id, status="running", progress=0.4, result_path="/partial.tif"
        )
        cancelled = self.catalog.create_job(kind="astro", manifest_path="/cancel.json", log_path="/cancel.log")
        cancelled = self.catalog.update_job(cancelled.id, status="cancelled", error="before launch")

        self.assertEqual(1, self.catalog.recover_running_jobs())

        recovered = self.catalog.get_job(running.id)
        self.assertEqual("interrupted", recovered.status)
        self.assertEqual(0.4, recovered.progress)
        self.assertEqual("/partial.tif", recovered.result_path)
        self.assertIn("restart", recovered.error.lower())
        self.assertEqual(queued, self.catalog.get_job(queued.id))
        self.assertEqual(cancelled, self.catalog.get_job(cancelled.id))
        self.assertEqual(0, self.catalog.recover_running_jobs())

    def test_group_deletion_returns_member_ids_and_is_transactional(self):
        first_id = self.catalog.upsert_photo(
            Photo(path="/photos/a.ARW", fingerprint="a", captured_at=None, camera_id="A")
        )
        second_id = self.catalog.upsert_photo(
            Photo(path="/photos/b.ARW", fingerprint="b", captured_at=None, camera_id="A")
        )
        group_id = self.catalog.assign_group("group-a", "A", None, [first_id, second_id], first_id, {})

        self.assertEqual([first_id, second_id], self.catalog.group_photo_ids(group_id))
        self.assertEqual([first_id, second_id], self.catalog.delete_group(group_id))
        self.assertEqual([], self.catalog.list_photos())

    def test_delete_photos_rejects_duplicate_and_missing_ids(self):
        photo_id = self.catalog.upsert_photo(
            Photo(path="/photos/a.ARW", fingerprint="a", captured_at=None, camera_id="A")
        )

        with self.assertRaises(ValueError):
            self.catalog.delete_photos([photo_id, photo_id])
        with self.assertRaises(ValueError):
            self.catalog.delete_photos([photo_id, 999999])

        self.assertEqual([photo_id], [photo.id for photo in self.catalog.list_photos()])

    def test_delete_photos_removes_only_requested_ids(self):
        photo_id = self.catalog.upsert_photo(
            Photo(path="/photos/a.ARW", fingerprint="a", captured_at=None, camera_id="A")
        )
        remaining_id = self.catalog.upsert_photo(
            Photo(path="/photos/b.ARW", fingerprint="b", captured_at=None, camera_id="A")
        )
        group_id = self.catalog.assign_group("group-a", "A", None, [photo_id, remaining_id], photo_id, {})

        self.assertEqual(1, self.catalog.delete_photos([photo_id]))

        self.assertEqual([remaining_id], [photo.id for photo in self.catalog.list_photos()])
        self.assertEqual([remaining_id], self.catalog.group_photo_ids(group_id))
        group = self.catalog.connection.execute(
            "SELECT automatic_pick_id FROM photo_groups WHERE id = ?", (group_id,)
        ).fetchone()
        self.assertEqual(remaining_id, group["automatic_pick_id"])

    def test_delete_photos_rolls_back_dependents_on_sqlite_failure(self):
        photo_id = self.catalog.upsert_photo(
            Photo(path="/photos/a.ARW", fingerprint="a", captured_at=None, camera_id="A")
        )
        other_id = self.catalog.upsert_photo(
            Photo(path="/photos/b.ARW", fingerprint="b", captured_at=None, camera_id="A")
        )
        group_id = self.catalog.assign_group("group-a", "A", None, [photo_id, other_id], photo_id, {})
        self.catalog.save_metrics(photo_id, PhotoMetrics(sharpness=0.8, exposure=0.7, clipping=0.1))
        self.catalog.save_tags(photo_id, {"subjects": ["bird"]}, "test-model")
        self.catalog.append_event(photo_id, "imported", {"source": "test"})
        self.catalog.save_artifact(
            photo_id=photo_id,
            source_name="a.ARW",
            source_fingerprint="a",
            kind="jpeg",
            path="/outputs/a.jpg",
            settings={"quality": 92},
            output_fingerprint="jpeg-a",
        )
        self.catalog.connection.execute(
            f"""
            CREATE TRIGGER fail_photo_delete AFTER DELETE ON photos
            WHEN OLD.id = {photo_id}
            BEGIN SELECT RAISE(ABORT, 'injected delete failure'); END
            """
        )
        self.catalog.connection.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            self.catalog.delete_photos([photo_id])

        self.assertIsNotNone(self.catalog.get_photo(photo_id))
        self.assertEqual([photo_id, other_id], self.catalog.group_photo_ids(group_id))
        group = self.catalog.connection.execute(
            "SELECT automatic_pick_id FROM photo_groups WHERE id = ?", (group_id,)
        ).fetchone()
        self.assertEqual(photo_id, group["automatic_pick_id"])
        self.assertIsNotNone(self.catalog.get_metrics(photo_id))
        self.assertIsNotNone(self.catalog.get_tags(photo_id))
        self.assertEqual(1, len(self.catalog.events_for(photo_id)))
        self.assertEqual(photo_id, self.catalog.list_artifacts()[0].photo_id)


    def test_group_filters_each_condition_and_combination_with_counts(self):
        ids, groups = seed_filter_photos(self.catalog)
        names = {photo_id: name for name, photo_id in ids.items()}

        def shown(**fields):
            filters = GroupFilters(**fields)
            return sorted(names[photo["id"]] for group in self.catalog.list_groups(filters) for photo in group["photos"])

        cases = [
            ({}, ["a1", "a2", "b2", "c1"]),
            ({"show_rejected": True}, ["a1", "a2", "a3", "b1", "b2", "c1"]),
            ({"date_from": "2026-10-22"}, ["b2"]),
            ({"date_to": "2026-10-21"}, ["a1", "a2"]),
            ({"camera": "B"}, ["b2", "c1"]),
            ({"decision": "flag"}, ["a2"]),
            ({"decision": "reject"}, ["a3", "b1"]),
            ({"decision": "none"}, ["a1", "b2", "c1"]),
            ({"min_rating": 3}, ["a1", "b2"]),
            ({"keepers_only": True}, ["a1", "a2", "c1"]),
            ({"tagged_only": True}, ["a1", "b2"]),
            ({"text": "FOX"}, ["a1"]),
            ({"text": "heron"}, ["b2"]),
            ({"text": "%"}, []),
            ({"text": "_"}, []),
            ({"date_from": "2026-10-21", "date_to": "2026-10-21", "camera": "A", "keepers_only": True, "min_rating": 1}, ["a1", "a2"]),
            ({"camera": "B", "show_rejected": True, "decision": "reject"}, ["b1"]),
        ]
        for fields, expected in cases:
            with self.subTest(fields=fields):
                self.assertEqual(expected, shown(**fields))
                self.assertEqual({"filtered": len(expected), "total": 6}, self.catalog.group_counts(GroupFilters(**fields)))
        self.assertEqual([groups["g2"], groups["g3"]], [group["id"] for group in self.catalog.list_groups(GroupFilters(camera="B"))],
                         "groups keep only matching photos and empty groups are omitted")
        self.assertEqual(6, sum(len(group["photos"]) for group in self.catalog.list_groups()), "no filters lists every grouped frame")
        invalid = [{"date_from": "2026-10-32"}, {"date_from": "20261021"}, {"date_to": "2026-W43-4"}, {"date_from": 20261021},
                   {"date_from": "2026-10-22", "date_to": "2026-10-21"}, {"decision": "maybe"}, {"decision": "FLAG"},
                   {"min_rating": 6}, {"min_rating": -1}, {"min_rating": True}, {"min_rating": "3"}, {"keepers_only": 1},
                   {"tagged_only": "yes"}, {"show_rejected": None}, {"camera": 5}, {"text": ["fox"]}]
        for fields in invalid:
            with self.subTest(invalid=fields), self.assertRaisesRegex(ValueError, "^invalid filter: "):
                GroupFilters(**fields)

    def test_keepers_only_filter_matches_library_keeper_definition(self):
        with Library(Settings(library_dir=Path(self.tmp.name) / "library")) as library:
            ids, groups = seed_filter_photos(library.catalog)
            library.catalog.set_manual_pick(ids["a3"])  # a rejected manual pick is still no keeper
            for show_rejected in (False, True):
                filtered = sorted(photo["id"] for group in library.catalog.list_groups(GroupFilters(keepers_only=True, show_rejected=show_rejected))
                                  for photo in group["photos"])
                self.assertEqual(sorted(library.keeper_ids()), filtered)
            self.assertEqual(sorted([ids["a2"], ids["c1"]]), sorted(library.keeper_ids()))

    def test_text_filter_searches_every_local_ai_field_and_match_terms_explains(self):
        named, _ = seed_filter_photos(self.catalog)  # returns ({name: id}, groups), not a list
        ids = [named["a1"], named["b2"], named["c1"]]
        self.catalog.save_tags(ids[0], {"description": "Penguins on ice", "subjects": ["gentoo penguin"], "suggested": ["krill"],
                                        "species": [{"name": "gentoo penguin", "confidence": 0.8}], "caption": "Porpoising gentoos",
                                        "problems": {"kinds": ["motion_blur"], "suggest_reject": True}}, "m")
        self.catalog.save_tags(ids[1], {"description": "Ship at anchor", "subjects": ["ship"]}, "m")

        def hits(text):
            return sorted(p["id"] for g in self.catalog.list_groups(GroupFilters(text=text, show_rejected=True)) for p in g["photos"])
        self.assertEqual([ids[0]], hits("krill"))
        self.assertEqual([ids[0]], hits("gentoo"))
        self.assertEqual([ids[0]], hits("porpoising"))
        self.assertEqual([ids[0]], hits("motion blur"))
        self.assertEqual([ids[0]], hits("motion_blur"))
        self.assertEqual([ids[1]], hits("ship"))
        self.assertEqual([], hits("100%"))  # LIKE wildcards are escaped
        self.assertEqual(["keyword: gentoo penguin", "species: gentoo penguin", "caption"], self.catalog.match_terms(ids[0], "gentoo"))
        self.assertEqual(["problem: motion blur"], self.catalog.match_terms(ids[0], "blur"))
        self.assertEqual([], self.catalog.match_terms(ids[2], "blur"))


if __name__ == "__main__":
    unittest.main()
