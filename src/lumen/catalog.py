from __future__ import annotations

import json
import math
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Any

from .composition import CropRect
from .domain import DECISIONS, Artifact, Job, Photo, PhotoEdit, PhotoMetrics, ProvenanceEvent


_UNCHANGED = object()
_JOB_TRANSITIONS = {
    "queued": frozenset({"running", "cancelled"}),
    "running": frozenset({"succeeded", "failed", "cancelled", "interrupted"}),
}
# Columns added after a table first shipped: {table: {column: DDL}}; applied once to older catalogs.
_MIGRATIONS = {
    "jobs": {"label": "TEXT", "processed": "INTEGER", "total": "INTEGER"},
    "photos": {"decision": "TEXT NOT NULL DEFAULT 'none'", "rating": "INTEGER NOT NULL DEFAULT 0"},
}


class Catalog:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS photos (
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL UNIQUE,
                fingerprint TEXT NOT NULL,
                captured_at TEXT,
                camera_id TEXT NOT NULL,
                preview_path TEXT,
                format TEXT NOT NULL DEFAULT 'ARW',
                group_id INTEGER,
                manual_pick INTEGER NOT NULL DEFAULT 0,
                imported_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS provenance (
                id INTEGER PRIMARY KEY,
                photo_id INTEGER REFERENCES photos(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                created_at TEXT NOT NULL,
                details_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS provenance_photo_idx
                ON provenance(photo_id, id);
            CREATE TABLE IF NOT EXISTS metrics (
                photo_id INTEGER PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE,
                sharpness REAL NOT NULL,
                exposure REAL NOT NULL,
                clipping REAL NOT NULL,
                face_quality REAL,
                eye_quality REAL,
                astro_json TEXT NOT NULL,
                total_score REAL
            );
            CREATE TABLE IF NOT EXISTS photo_groups (
                id INTEGER PRIMARY KEY,
                stable_key TEXT NOT NULL UNIQUE,
                camera_id TEXT NOT NULL,
                started_at TEXT,
                automatic_pick_id INTEGER REFERENCES photos(id)
            );
            CREATE TABLE IF NOT EXISTS tags (
                photo_id INTEGER PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE,
                model TEXT NOT NULL,
                data_json TEXT NOT NULL,
                tagged_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS artifacts (
                id INTEGER PRIMARY KEY,
                photo_id INTEGER REFERENCES photos(id) ON DELETE SET NULL,
                source_name TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL,
                kind TEXT NOT NULL,
                path TEXT NOT NULL UNIQUE,
                settings_json TEXT NOT NULL,
                output_fingerprint TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS photo_edits (
                photo_id INTEGER PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE,
                candidate_id TEXT NOT NULL,
                left REAL NOT NULL,
                top REAL NOT NULL,
                width REAL NOT NULL,
                height REAL NOT NULL,
                aspect TEXT NOT NULL,
                source_width INTEGER NOT NULL,
                source_height INTEGER NOT NULL,
                author TEXT NOT NULL,
                rationale TEXT NOT NULL,
                selected_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                progress REAL NOT NULL,
                manifest_path TEXT NOT NULL,
                log_path TEXT NOT NULL,
                result_path TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs(status, id);
            """
        )
        self.connection.commit()
        self._migrate()

    def _migrate(self) -> None:
        with self.connection:
            for table, columns in _MIGRATIONS.items():
                existing = {row["name"] for row in self.connection.execute(f"PRAGMA table_info({table})")}
                for column, ddl in columns.items():
                    if column not in existing:
                        self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def close(self) -> None:
        self.connection.close()

    def backup(self, destination: Path) -> None:
        with closing(sqlite3.connect(destination)) as snapshot:
            self.connection.backup(snapshot)

    def __enter__(self) -> "Catalog":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def upsert_photo(self, photo: Photo) -> int:
        now = datetime.now(timezone.utc).isoformat()
        captured = photo.captured_at.isoformat() if photo.captured_at else None
        row = self.connection.execute(
            """
            INSERT INTO photos
                (path, fingerprint, captured_at, camera_id, preview_path, format,
                 group_id, manual_pick, imported_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                fingerprint=excluded.fingerprint,
                captured_at=excluded.captured_at,
                camera_id=excluded.camera_id,
                preview_path=excluded.preview_path,
                format=excluded.format,
                group_id=COALESCE(excluded.group_id, photos.group_id),
                manual_pick=MAX(photos.manual_pick, excluded.manual_pick)
            RETURNING id
            """,
            (
                photo.path,
                photo.fingerprint,
                captured,
                photo.camera_id,
                photo.preview_path,
                photo.format,
                photo.group_id,
                int(photo.manual_pick),
                now,
            ),
        ).fetchone()
        self.connection.commit()
        return int(row["id"])

    def list_photos(self) -> list[Photo]:
        rows = self.connection.execute("SELECT * FROM photos ORDER BY id").fetchall()
        return [self._photo_from_row(row) for row in rows]

    def photo_count(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM photos").fetchone()[0])

    def get_photo(self, photo_id: int) -> Photo | None:
        row = self.connection.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()
        return self._photo_from_row(row) if row else None

    def create_job(self, *, kind: str, manifest_path: str, log_path: str) -> Job:
        now = datetime.now(timezone.utc)
        candidate = Job(
            id=1, kind=kind, status="queued", progress=0.0,
            manifest_path=manifest_path, log_path=log_path,
            result_path=None, error=None, created_at=now, updated_at=now,
        )
        with self.connection:
            row = self.connection.execute(
                """INSERT INTO jobs
                (kind, status, progress, manifest_path, log_path, result_path, error,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *""",
                (candidate.kind, candidate.status, candidate.progress, candidate.manifest_path,
                 candidate.log_path, candidate.result_path, candidate.error,
                 candidate.created_at.isoformat(), candidate.updated_at.isoformat()),
            ).fetchone()
        return self._job_from_row(row)

    def create_prepared_job(self, kind: str, prepare: Callable[[int], tuple[str, str]]) -> Job:
        """Insert a queued job, then prepare its files under the new ID outside any transaction.

        ``prepare`` receives the job ID and returns (manifest_path, log_path). It may take
        seconds (it hashes every frame), so no SQLite lock is held while it runs; if it
        raises, the placeholder row is deleted so no job exists without its manifest.
        """
        now = datetime.now(timezone.utc)
        with self.connection:
            row = self.connection.execute(
                """INSERT INTO jobs
                (kind, status, progress, manifest_path, log_path, result_path, error, created_at, updated_at)
                VALUES (?, 'queued', 0, 'pending', 'pending', NULL, NULL, ?, ?) RETURNING id""",
                (kind, now.isoformat(), now.isoformat()),
            ).fetchone()
        job_id = int(row["id"])
        try:
            manifest_path, log_path = prepare(job_id)
            Job(id=job_id, kind=kind, status="queued", progress=0.0, manifest_path=manifest_path,
                log_path=log_path, result_path=None, error=None, created_at=now, updated_at=now)  # validates the paths
            with self.connection:
                row = self.connection.execute(
                    "UPDATE jobs SET manifest_path = ?, log_path = ? WHERE id = ? AND status = 'queued' RETURNING *",
                    (manifest_path, log_path, job_id),
                ).fetchone()
            if row is None:
                raise ValueError(f"job {job_id} was cancelled during preparation")
        except BaseException:
            with self.connection:
                self.connection.execute(
                    "DELETE FROM jobs WHERE id = ? AND status = 'queued' AND manifest_path = 'pending'", (job_id,)
                )
            raise
        return self._job_from_row(row)

    def get_job(self, job_id: int) -> Job:
        if type(job_id) is not int or job_id <= 0:
            raise ValueError("job ID must be a positive integer")
        row = self.connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown job ID: {job_id}")
        return self._job_from_row(row)

    def list_jobs(self) -> list[Job]:
        return [self._job_from_row(row) for row in self.connection.execute(
            "SELECT * FROM jobs ORDER BY id"
        )]

    def update_job(
        self,
        job_id: int,
        *,
        status: str | None = None,
        progress: float | None = None,
        result_path: str | None | object = _UNCHANGED,
        error: str | None | object = _UNCHANGED,
        label: str | None | object = _UNCHANGED,
        processed: int | None = None,
        total: int | None = None,
    ) -> Job:
        if type(job_id) is not int or job_id <= 0:
            raise ValueError("job ID must be a positive integer")
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise ValueError(f"unknown job ID: {job_id}")
            current = self._job_from_row(row)
            if current.status not in _JOB_TRANSITIONS:
                raise ValueError(f"job {job_id} is terminal")
            next_status = current.status if status is None else status
            if status is not None and (
                    not isinstance(status, str) or status not in _JOB_TRANSITIONS[current.status]):
                raise ValueError(f"invalid job transition: {current.status} to {status}")
            next_progress = current.progress if progress is None else progress
            if (not isinstance(next_progress, (int, float)) or isinstance(next_progress, bool)
                    or not math.isfinite(next_progress) or next_progress < current.progress):
                raise ValueError("job progress cannot decrease and must be finite")
            next_result = current.result_path if result_path is _UNCHANGED else result_path
            next_error = current.error if error is _UNCHANGED else error
            updated = Job(
                id=current.id, kind=current.kind, status=next_status, progress=next_progress,
                manifest_path=current.manifest_path, log_path=current.log_path,
                result_path=next_result, error=next_error,
                created_at=current.created_at, updated_at=datetime.now(timezone.utc),
                label=current.label if label is _UNCHANGED else label,
                processed=current.processed if processed is None else processed,
                total=current.total if total is None else total,
            )
            self.connection.execute(
                """UPDATE jobs SET status = ?, progress = ?, result_path = ?, error = ?,
                updated_at = ?, label = ?, processed = ?, total = ? WHERE id = ?""",
                (updated.status, updated.progress, updated.result_path, updated.error,
                 updated.updated_at.isoformat(), updated.label, updated.processed, updated.total, updated.id),
            )
        return updated

    def recover_running_jobs(self) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self.connection:
            # A placeholder left by a crash mid-preparation has no manifest and can never run.
            self.connection.execute("DELETE FROM jobs WHERE status = 'queued' AND manifest_path = 'pending'")
            cursor = self.connection.execute(
                """UPDATE jobs SET status = 'interrupted',
                error = 'Interrupted during application restart', updated_at = ?
                WHERE status = 'running'""",
                (now,),
            )
        return cursor.rowcount

    def get_crop(self, photo_id: int) -> PhotoEdit | None:
        row = self.connection.execute("SELECT * FROM photo_edits WHERE photo_id = ?", (photo_id,)).fetchone()
        return self._crop_from_row(row) if row else None

    def crop_event_ids(self, photo_id: int) -> list[int]:
        return [int(row[0]) for row in self.connection.execute(
            "SELECT id FROM provenance WHERE photo_id = ? AND kind IN ('crop_selected', 'crop_cleared') ORDER BY id",
            (photo_id,),
        )]

    def save_crop(self, edit: PhotoEdit) -> PhotoEdit:
        if not isinstance(edit, PhotoEdit):
            raise ValueError("crop edit is invalid")
        if self.get_photo(edit.photo_id) is None:
            raise ValueError(f"unknown photo ID: {edit.photo_id}")
        rect = edit.rect
        with self.connection:
            self.connection.execute(
                """INSERT INTO photo_edits
                (photo_id, candidate_id, left, top, width, height, aspect, source_width,
                 source_height, author, rationale, selected_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(photo_id) DO UPDATE SET candidate_id=excluded.candidate_id,
                left=excluded.left, top=excluded.top, width=excluded.width, height=excluded.height,
                aspect=excluded.aspect, source_width=excluded.source_width,
                source_height=excluded.source_height, author=excluded.author,
                rationale=excluded.rationale, selected_at=excluded.selected_at""",
                (edit.photo_id, edit.candidate_id, rect.left, rect.top, rect.width, rect.height,
                 edit.aspect, edit.source_width, edit.source_height, edit.author,
                 edit.rationale, edit.selected_at.isoformat()),
            )
            self._insert_event(edit.photo_id, "crop_selected", edit.to_dict())
        return edit

    def clear_crop(self, photo_id: int) -> PhotoEdit | None:
        previous = self.get_crop(photo_id)
        if previous is None:
            return None
        with self.connection:
            self.connection.execute("DELETE FROM photo_edits WHERE photo_id = ?", (photo_id,))
            self._insert_event(photo_id, "crop_cleared", previous.to_dict())
        return previous

    def invalidate_crop(self, photo_id: int, previous_fingerprint: str, source_fingerprint: str) -> PhotoEdit | None:
        previous = self.get_crop(photo_id)
        if previous is None:
            return None
        with self.connection:
            self.connection.execute("DELETE FROM photo_edits WHERE photo_id = ?", (photo_id,))
            self._insert_event(photo_id, "crop_invalidated_source_replaced", {
                "candidate_id": previous.candidate_id,
                "previous_fingerprint": previous_fingerprint,
                "source_fingerprint": source_fingerprint,
                "reason": "source content changed; saved crop dimensions and pixels are no longer trusted",
            })
        return previous

    def restore_crop_state(self, photo_id: int, edit: PhotoEdit | None, retained_event_ids: list[int]) -> None:
        """Restore a failed crop mutation while retaining all earlier provenance rows."""
        with self.connection:
            self.connection.execute("DELETE FROM photo_edits WHERE photo_id = ?", (photo_id,))
            if edit is not None:
                rect = edit.rect
                self.connection.execute(
                    """INSERT INTO photo_edits
                    (photo_id, candidate_id, left, top, width, height, aspect, source_width,
                     source_height, author, rationale, selected_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (photo_id, edit.candidate_id, rect.left, rect.top,
                     rect.width, rect.height, edit.aspect, edit.source_width, edit.source_height,
                     edit.author, edit.rationale, edit.selected_at.isoformat()),
                )
            rows = self.connection.execute(
                "SELECT id FROM provenance WHERE photo_id = ? AND kind IN ('crop_selected', 'crop_cleared')",
                (photo_id,),
            ).fetchall()
            created_event_ids = [int(row[0]) for row in rows if int(row[0]) not in retained_event_ids]
            self.connection.executemany("DELETE FROM provenance WHERE id = ?", ((event_id,) for event_id in created_event_ids))

    def _insert_event(self, photo_id: int, kind: str, details: dict[str, Any]) -> int:
        cursor = self.connection.execute(
            "INSERT INTO provenance(photo_id, kind, created_at, details_json) VALUES (?, ?, ?, ?)",
            (photo_id, kind, datetime.now(timezone.utc).isoformat(),
             json.dumps(details, sort_keys=True, separators=(",", ":"))),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _crop_from_row(row: sqlite3.Row) -> PhotoEdit:
        return PhotoEdit(
            photo_id=int(row["photo_id"]), candidate_id=row["candidate_id"],
            rect=CropRect(row["left"], row["top"], row["width"], row["height"]),
            aspect=row["aspect"], source_width=int(row["source_width"]),
            source_height=int(row["source_height"]), author=row["author"],
            rationale=row["rationale"], selected_at=datetime.fromisoformat(row["selected_at"]),
        )

    def photo_by_path(self, path: str) -> Photo | None:
        row = self.connection.execute("SELECT * FROM photos WHERE path = ?", (path,)).fetchone()
        return self._photo_from_row(row) if row else None

    def save_artifact(
        self,
        photo_id: int | None,
        source_name: str,
        source_fingerprint: str,
        kind: str,
        path: str,
        settings: dict[str, Any],
        output_fingerprint: str,
    ) -> Artifact:
        created_at = datetime.now(timezone.utc)
        row = self.connection.execute(
            """
            INSERT INTO artifacts
                (photo_id, source_name, source_fingerprint, kind, path, settings_json,
                 output_fingerprint, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING *
            """,
            (
                photo_id,
                source_name,
                source_fingerprint,
                kind,
                path,
                json.dumps(settings, sort_keys=True),
                output_fingerprint,
                created_at.isoformat(),
            ),
        ).fetchone()
        self.connection.commit()
        return self._artifact_from_row(row)

    def list_artifacts(self) -> list[Artifact]:
        rows = self.connection.execute("SELECT * FROM artifacts ORDER BY id").fetchall()
        return [self._artifact_from_row(row) for row in rows]

    def discard_export(self, artifact_id: int, kind: str = "exported") -> None:
        """Remove only the newly created artifact and its success events."""
        with self.connection:
            self.connection.execute(
                """
                DELETE FROM provenance WHERE kind = ?
                AND json_extract(details_json, '$.artifact_id') = ?
                """, (kind, artifact_id),
            )
            self.connection.execute("DELETE FROM artifacts WHERE id = ?", (artifact_id,))

    def group_photo_ids(self, group_id: int) -> list[int]:
        rows = self.connection.execute(
            "SELECT id FROM photos WHERE group_id = ? ORDER BY id", (group_id,)
        ).fetchall()
        return [int(row["id"]) for row in rows]

    def list_group_ids(self) -> list[int]:
        return [int(row["id"]) for row in self.connection.execute("SELECT id FROM photo_groups ORDER BY id")]

    def delete_group(self, group_id: int) -> list[int]:
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT id FROM photo_groups WHERE id = ?", (group_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"group {group_id} does not exist")
            photo_ids = self.group_photo_ids(group_id)
            self.connection.execute("DELETE FROM photo_groups WHERE id = ?", (group_id,))
            self.connection.executemany(
                "DELETE FROM photos WHERE id = ?", ((photo_id,) for photo_id in photo_ids)
            )
        return photo_ids

    def delete_photos(self, photo_ids: list[int], *, group_ids: list[int] | None = None) -> int:
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            if len(set(photo_ids)) != len(photo_ids):
                raise ValueError("photo IDs must be unique")
            selected_groups = group_ids if group_ids is not None else []
            if len(set(selected_groups)) != len(selected_groups):
                raise ValueError("group IDs must be unique")
            for group_id in selected_groups:
                if self.connection.execute("SELECT id FROM photo_groups WHERE id = ?", (group_id,)).fetchone() is None:
                    raise ValueError(f"group {group_id} does not exist")
                if not set(self.group_photo_ids(group_id)).issubset(photo_ids):
                    raise ValueError(f"group {group_id} contains unselected photos")
            self.connection.executemany(
                "DELETE FROM photo_groups WHERE id = ?", ((group_id,) for group_id in selected_groups)
            )
            if not photo_ids:
                return 0

            placeholders = ",".join("?" for _ in photo_ids)
            rows = self.connection.execute(
                f"SELECT id FROM photos WHERE id IN ({placeholders})", photo_ids
            ).fetchall()
            found_ids = {int(row["id"]) for row in rows}
            if found_ids != set(photo_ids):
                missing_ids = sorted(set(photo_ids) - found_ids)
                raise ValueError(f"unknown photo IDs: {missing_ids}")

            groups = self.connection.execute(
                f"""
                SELECT DISTINCT g.id, g.automatic_pick_id
                FROM photo_groups g
                LEFT JOIN photos p ON p.group_id = g.id
                WHERE g.automatic_pick_id IN ({placeholders})
                   OR p.id IN ({placeholders})
                """,
                (*photo_ids, *photo_ids),
            ).fetchall()
            for group in groups:
                group_id = int(group["id"])
                remaining = self.connection.execute(
                    f"""
                    SELECT id FROM photos
                    WHERE group_id = ? AND id NOT IN ({placeholders})
                    ORDER BY id
                    """,
                    (group_id, *photo_ids),
                ).fetchall()
                if not remaining:
                    self.connection.execute("DELETE FROM photo_groups WHERE id = ?", (group_id,))
                elif group["automatic_pick_id"] in photo_ids:
                    self.connection.execute(
                        "UPDATE photo_groups SET automatic_pick_id = ? WHERE id = ?",
                        (int(remaining[0]["id"]), group_id),
                    )

            self.connection.executemany(
                "DELETE FROM photos WHERE id = ?", ((photo_id,) for photo_id in photo_ids)
            )
        return len(photo_ids)

    def save_metrics(self, photo_id: int, metrics: PhotoMetrics, total_score: float | None = None) -> None:
        self.connection.execute(
            """
            INSERT INTO metrics
                (photo_id, sharpness, exposure, clipping, face_quality, eye_quality, astro_json, total_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(photo_id) DO UPDATE SET
                sharpness=excluded.sharpness, exposure=excluded.exposure,
                clipping=excluded.clipping, face_quality=excluded.face_quality,
                eye_quality=excluded.eye_quality, astro_json=excluded.astro_json,
                total_score=COALESCE(excluded.total_score, metrics.total_score)
            """,
            (
                photo_id,
                metrics.sharpness,
                metrics.exposure,
                metrics.clipping,
                metrics.face_quality,
                metrics.eye_quality,
                json.dumps(metrics.astro, sort_keys=True),
                total_score,
            ),
        )
        self.connection.commit()

    def get_metrics(self, photo_id: int) -> PhotoMetrics | None:
        row = self.connection.execute("SELECT * FROM metrics WHERE photo_id = ?", (photo_id,)).fetchone()
        if not row:
            return None
        return PhotoMetrics(
            sharpness=float(row["sharpness"]),
            exposure=float(row["exposure"]),
            clipping=float(row["clipping"]),
            face_quality=row["face_quality"],
            eye_quality=row["eye_quality"],
            astro=json.loads(row["astro_json"]),
        )

    def save_tags(self, photo_id: int, data: dict[str, Any], model: str) -> None:
        self.connection.execute(
            """
            INSERT INTO tags(photo_id, model, data_json, tagged_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(photo_id) DO UPDATE SET
                model=excluded.model, data_json=excluded.data_json, tagged_at=excluded.tagged_at
            """,
            (
                photo_id,
                model,
                json.dumps(data, sort_keys=True),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.connection.commit()

    def get_tags(self, photo_id: int) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM tags WHERE photo_id = ?", (photo_id,)).fetchone()
        if not row:
            return None
        return {"model": row["model"], "data": json.loads(row["data_json"]), "tagged_at": row["tagged_at"]}

    def clear_tags(self, photo_id: int) -> None:
        self.connection.execute("DELETE FROM tags WHERE photo_id = ?", (photo_id,))
        self.connection.commit()

    def assign_group(
        self,
        stable_key: str,
        camera_id: str,
        started_at: str | None,
        photo_ids: list[int],
        automatic_pick_id: int,
        scores: dict[int, float],
    ) -> int:
        row = self.connection.execute(
            """
            INSERT INTO photo_groups(stable_key, camera_id, started_at, automatic_pick_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(stable_key) DO UPDATE SET
                camera_id=excluded.camera_id, started_at=excluded.started_at,
                automatic_pick_id=excluded.automatic_pick_id
            RETURNING id
            """,
            (stable_key, camera_id, started_at, automatic_pick_id),
        ).fetchone()
        group_id = int(row["id"])
        placeholders = ",".join("?" for _ in photo_ids)
        if photo_ids:
            self.connection.execute(
                f"UPDATE photos SET group_id = ? WHERE id IN ({placeholders})",
                (group_id, *photo_ids),
            )
        for photo_id, score in scores.items():
            self.connection.execute("UPDATE metrics SET total_score = ? WHERE photo_id = ?", (score, photo_id))
        self.connection.commit()
        return group_id

    def clear_group_assignments(self) -> None:
        self.connection.execute("UPDATE photos SET group_id = NULL")
        self.connection.commit()

    def set_manual_pick(self, photo_id: int) -> int:
        row = self.connection.execute("SELECT group_id FROM photos WHERE id = ?", (photo_id,)).fetchone()
        if not row or row["group_id"] is None:
            raise ValueError(f"photo {photo_id} is not in a group")
        group_id = int(row["group_id"])
        self.connection.execute("UPDATE photos SET manual_pick = 0 WHERE group_id = ?", (group_id,))
        self.connection.execute("UPDATE photos SET manual_pick = 1 WHERE id = ?", (photo_id,))
        self.connection.execute("UPDATE photo_groups SET automatic_pick_id = ? WHERE id = ?", (photo_id, group_id))
        self.connection.commit()
        return group_id

    def set_decision(self, photo_id: int, decision: str, *, reason: str | None = None) -> Photo:
        if not isinstance(decision, str) or decision not in DECISIONS:
            raise ValueError("decision must be none, flag, or reject")
        return self._change_photo(photo_id, "decision", decision, {"reason": reason} if reason else {})

    def set_rating(self, photo_id: int, rating: int) -> Photo:
        if type(rating) is not int or not 0 <= rating <= 5:
            raise ValueError("rating must be an integer from 0 through 5")
        return self._change_photo(photo_id, "rating", rating, {})

    def _change_photo(self, photo_id: int, column: str, value: str | int, details: dict[str, Any]) -> Photo:
        """Set decision or rating and record a `<column>_changed` event in one transaction; no-ops record nothing."""
        photo = self.get_photo(photo_id) if type(photo_id) is int else None
        if photo is None:
            raise ValueError(f"unknown photo ID: {photo_id}")
        previous = getattr(photo, column)
        if previous != value:
            with self.connection:
                self.connection.execute(f"UPDATE photos SET {column} = ? WHERE id = ?", (value, photo_id))
                self._insert_event(photo_id, f"{column}_changed", {"previous": previous, "next": value, **details})
            setattr(photo, column, value)
        return photo

    def rerank_group(self, group_id: int, automatic_pick_id: int, scores: dict[int, float]) -> None:
        with self.connection:
            self.connection.execute("UPDATE photo_groups SET automatic_pick_id = ? WHERE id = ?", (automatic_pick_id, group_id))
            self.connection.executemany(
                "UPDATE metrics SET total_score = ? WHERE photo_id = ?", ((score, photo_id) for photo_id, score in scores.items())
            )

    def list_groups(self) -> list[dict[str, Any]]:
        groups = self.connection.execute(
            """
            SELECT g.* FROM photo_groups g
            WHERE EXISTS (SELECT 1 FROM photos p WHERE p.group_id = g.id)
            ORDER BY COALESCE(g.started_at, '9999'), g.id
            """
        ).fetchall()
        result: list[dict[str, Any]] = []
        for group in groups:
            rows = self.connection.execute(
                """
                SELECT p.*, m.sharpness, m.exposure, m.clipping, m.face_quality,
                       m.eye_quality, m.astro_json, m.total_score
                FROM photos p LEFT JOIN metrics m ON m.photo_id = p.id
                WHERE p.group_id = ?
                ORDER BY COALESCE(m.total_score, -1) DESC, p.path
                """,
                (group["id"],),
            ).fetchall()
            result.append(
                {
                    "id": int(group["id"]),
                    "stable_key": group["stable_key"],
                    "camera_id": group["camera_id"],
                    "started_at": group["started_at"],
                    "automatic_pick_id": group["automatic_pick_id"],
                    "photos": [dict(row) for row in rows],
                }
            )
        return result

    def append_event(self, photo_id: int | None, kind: str, details: dict[str, Any]) -> int:
        cursor = self.connection.execute(
            "INSERT INTO provenance(photo_id, kind, created_at, details_json) VALUES (?, ?, ?, ?)",
            (
                photo_id,
                kind,
                datetime.now(timezone.utc).isoformat(),
                json.dumps(details, sort_keys=True, separators=(",", ":")),
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def events_for(self, photo_id: int | None) -> list[ProvenanceEvent]:
        if photo_id is None:
            rows = self.connection.execute(
                "SELECT * FROM provenance WHERE photo_id IS NULL ORDER BY id"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM provenance WHERE photo_id = ? ORDER BY id", (photo_id,)
            ).fetchall()
        return [
            ProvenanceEvent(
                id=int(row["id"]),
                photo_id=row["photo_id"],
                kind=row["kind"],
                created_at=datetime.fromisoformat(row["created_at"]),
                details=json.loads(row["details_json"]),
            )
            for row in rows
        ]

    @staticmethod
    def _photo_from_row(row: sqlite3.Row) -> Photo:
        return Photo(
            id=int(row["id"]),
            path=row["path"],
            fingerprint=row["fingerprint"],
            captured_at=datetime.fromisoformat(row["captured_at"]) if row["captured_at"] else None,
            camera_id=row["camera_id"],
            preview_path=row["preview_path"],
            format=row["format"],
            group_id=row["group_id"],
            manual_pick=bool(row["manual_pick"]),
            decision=row["decision"],
            rating=int(row["rating"]),
        )

    @staticmethod
    def _artifact_from_row(row: sqlite3.Row) -> Artifact:
        return Artifact(
            id=int(row["id"]),
            photo_id=int(row["photo_id"]) if row["photo_id"] is not None else None,
            source_name=row["source_name"],
            source_fingerprint=row["source_fingerprint"],
            kind=row["kind"],
            path=row["path"],
            settings=json.loads(row["settings_json"]),
            output_fingerprint=row["output_fingerprint"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> Job:
        return Job(
            id=int(row["id"]), kind=row["kind"], status=row["status"],
            progress=float(row["progress"]), manifest_path=row["manifest_path"],
            log_path=row["log_path"], result_path=row["result_path"], error=row["error"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            label=row["label"], processed=row["processed"], total=row["total"],
        )
