from __future__ import annotations

import json
import math
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Any, Iterable

from .composition import CropRect
from .domain import DECISIONS, PREVIEW_KINDS, Artifact, DevelopSettings, Job, Photo, PhotoEdit, PhotoMetrics, ProvenanceEvent


_UNCHANGED = object()
_JOB_TRANSITIONS = {
    "queued": frozenset({"running", "cancelled"}),
    "running": frozenset({"succeeded", "failed", "cancelled", "interrupted"}),
}
# Columns added after a table first shipped: {table: {column: DDL}}; applied once to older catalogs.
_MIGRATIONS = {
    "jobs": {"label": "TEXT", "processed": "INTEGER", "total": "INTEGER"},
    "metrics": {"dhash": "TEXT"},
    "photos": {"decision": "TEXT NOT NULL DEFAULT 'none'", "rating": "INTEGER NOT NULL DEFAULT 0", "mirror_path": "TEXT",
               "preview_kind": "TEXT NOT NULL DEFAULT 'embedded'", "exposure_seconds": "REAL", "merged_into": "TEXT",
               "latitude": "REAL", "longitude": "REAL", "altitude": "REAL", "place": "TEXT", "place_country": "TEXT"},
}


MAX_ID_FILTER = 200


@dataclass(frozen=True)
class GroupFilters:
    """Contact-sheet filters; every condition is ANDed in SQL. Invalid values raise ValueError("invalid filter: <field>")."""
    date_from: str | None = None
    date_to: str | None = None
    camera: str | None = None
    decision: str = "any"
    min_rating: int = 0
    keepers_only: bool = False
    tagged_only: bool = False
    export_failed_only: bool = False
    text: str | None = None
    show_rejected: bool = False
    ids: tuple[int, ...] | None = None  # exactly these frames (a map cluster), still ANDed with the rest

    def __post_init__(self) -> None:
        for name in ("date_from", "date_to"):
            value = getattr(self, name)
            if value is not None and not (isinstance(value, str) and _canonical_date(value)):
                raise ValueError(f"invalid filter: {name}")
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("invalid filter: date_to")
        for name in ("camera", "text"):
            if getattr(self, name) is not None and not isinstance(getattr(self, name), str):
                raise ValueError(f"invalid filter: {name}")
        if self.text is not None:  # "a | b" ORs its terms; blank-only text is no filter.
            object.__setattr__(self, "text", " | ".join(_text_terms(self.text)) or None)
        if not isinstance(self.decision, str) or self.decision not in DECISIONS | {"any"}:
            raise ValueError("invalid filter: decision")
        if type(self.min_rating) is not int or not 0 <= self.min_rating <= 5:
            raise ValueError("invalid filter: min_rating")
        for name in ("keepers_only", "tagged_only", "export_failed_only", "show_rejected"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"invalid filter: {name}")
        if self.ids is not None and not (type(self.ids) is tuple and 0 < len(self.ids) <= MAX_ID_FILTER
                                         and len(set(self.ids)) == len(self.ids)
                                         and all(type(i) is int and i > 0 for i in self.ids)):
            raise ValueError("invalid filter: ids")

    def where(self) -> tuple[str, list[Any]]:
        """SQL conditions over photos `p` joined to photo_groups `g`; values are always bound parameters."""
        conditions, params = ["1"], []
        if self.date_from:
            conditions.append("substr(p.captured_at, 1, 10) >= ?")
            params.append(self.date_from)
        if self.date_to:
            conditions.append("substr(p.captured_at, 1, 10) <= ?")
            params.append(self.date_to)
        if self.camera:
            conditions.append("p.camera_id = ?")
            params.append(self.camera)
        if self.decision != "any":
            conditions.append("p.decision = ?")
            params.append(self.decision)
        elif not self.show_rejected:
            conditions.append("p.decision != 'reject'")
        if self.ids:
            conditions.append(f"p.id IN ({','.join('?' * len(self.ids))})")
            params.extend(self.ids)
        if self.min_rating:
            conditions.append("p.rating >= ?")
            params.append(self.min_rating)
        if self.keepers_only:  # Same rule as Library.keeper_ids.
            conditions.append("p.decision != 'reject' AND (p.decision = 'flag' OR p.id = g.automatic_pick_id)")
        if self.export_failed_only:  # The frame's latest export attempt failed.
            conditions.append("(SELECT v.kind FROM provenance v WHERE v.photo_id = p.id AND v.kind IN ('exported', 'export_failed')"
                              " ORDER BY v.id DESC LIMIT 1) = 'export_failed'")
        if self.tagged_only or self.text:
            # String arrays are searched as their JSON text; species by name only (not its "confidence" key);
            # problem kinds also match their human form, so "motion blur" finds motion_blur.
            fields = [f"json_extract(t.data_json, '$.{key}')" for key in ("description", "subjects", "suggested", "caption", "problems.kinds")]
            fields += ["(SELECT group_concat(CASE WHEN s.type = 'object' THEN json_extract(s.value, '$.name') END, ' | ') FROM json_each(t.data_json, '$.species') s)",
                       "replace(json_extract(t.data_json, '$.problems.kinds'), '_', ' ')"]
            terms = _text_terms(self.text or "")
            if self.tagged_only:
                conditions.append("EXISTS (SELECT 1 FROM tags t WHERE t.photo_id = p.id)")
            if terms:  # Each term matches the tags or the frame's place, so untagged frames are found by place.
                likes = " OR ".join(f"{field} LIKE ? ESCAPE '\\'" for field in fields)
                conditions.append("(" + " OR ".join(f"EXISTS (SELECT 1 FROM tags t WHERE t.photo_id = p.id AND ({likes}))"
                                                    " OR p.place LIKE ? ESCAPE '\\' OR p.place_country LIKE ? ESCAPE '\\'"
                                                    for _ in terms) + ")")
            for term in terms:
                pattern = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                params += [pattern] * (len(fields) + 2)
        return " AND ".join(conditions), params


def _text_terms(text: str) -> list[str]:
    return [term for term in (piece.strip() for piece in text.split("|")) if term]


def _canonical_date(value: str) -> bool:
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


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
            CREATE TABLE IF NOT EXISTS photo_develop (
                photo_id INTEGER PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE,
                settings_json TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL,
                updated_at TEXT NOT NULL
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
            CREATE TABLE IF NOT EXISTS cleared_fingerprints (
                fingerprint TEXT PRIMARY KEY,
                cleared_at TEXT NOT NULL
            );
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
                 group_id, manual_pick, imported_at, preview_kind, exposure_seconds, latitude, longitude, altitude,
                 place, place_country)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                fingerprint=excluded.fingerprint,
                captured_at=excluded.captured_at,
                exposure_seconds=excluded.exposure_seconds,
                latitude=excluded.latitude,
                longitude=excluded.longitude,
                altitude=excluded.altitude,
                camera_id=excluded.camera_id,
                preview_path=excluded.preview_path,
                preview_kind=excluded.preview_kind,
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
                photo.preview_kind,
                photo.exposure_seconds,
                photo.latitude,
                photo.longitude,
                photo.altitude,
                photo.place,
                photo.place_country,
            ),
        ).fetchone()
        self.connection.commit()
        return int(row["id"])

    def update_location(self, photo_id: int, *, latitude: float | None, longitude: float | None, altitude: float | None) -> None:
        """Backfill a GPS fix read later for an otherwise unchanged file."""
        self.connection.execute("UPDATE photos SET latitude = ?, longitude = ?, altitude = ? WHERE id = ?",
                                (latitude, longitude, altitude, photo_id))
        self.connection.commit()

    def update_place(self, photo_id: int, *, place: str | None, country: str | None) -> None:
        self.connection.execute("UPDATE photos SET place = ?, place_country = ? WHERE id = ?", (place, country, photo_id))
        self.connection.commit()

    def list_photos(self, filters: GroupFilters | None = None) -> list[Photo]:
        """Every photo, or (with filters) those matching them, ungrouped frames included."""
        where, params = filters.where() if filters else ("1", [])
        rows = self.connection.execute(
            f"SELECT p.* FROM photos p LEFT JOIN photo_groups g ON g.id = p.group_id WHERE {where} ORDER BY p.id", params
        ).fetchall()
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

    def get_develop(self, photo_id: int) -> DevelopSettings | None:
        row = self.connection.execute(
            "SELECT settings_json FROM photo_develop WHERE photo_id = ?", (photo_id,)).fetchone()
        return DevelopSettings.from_dict(json.loads(row["settings_json"])) if row else None

    def develop_event_ids(self, photo_id: int) -> list[int]:
        return [int(row[0]) for row in self.connection.execute(
            "SELECT id FROM provenance WHERE photo_id = ? AND kind LIKE 'develop\\_%' ESCAPE '\\' ORDER BY id",
            (photo_id,),
        )]

    def save_develop(self, photo_id: int, settings: DevelopSettings, source_fingerprint: str,
                     details: dict | None = None) -> DevelopSettings:
        """`details` joins the develop_changed event, so one event carries the settings and what they produced."""
        if not isinstance(settings, DevelopSettings):
            raise ValueError("develop settings are invalid")
        if self.get_photo(photo_id) is None:
            raise ValueError(f"unknown photo ID: {photo_id}")
        fields = settings.to_dict()
        with self.connection:
            self.connection.execute(
                """INSERT INTO photo_develop (photo_id, settings_json, source_fingerprint, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(photo_id) DO UPDATE SET settings_json=excluded.settings_json,
                source_fingerprint=excluded.source_fingerprint, updated_at=excluded.updated_at""",
                (photo_id, json.dumps(fields, sort_keys=True, separators=(",", ":")),
                 source_fingerprint, datetime.now(timezone.utc).isoformat()),
            )
            self._insert_event(photo_id, "develop_changed", {"settings": fields, **(details or {})})
        return settings

    def clear_develop(self, photo_id: int, details: dict | None = None) -> DevelopSettings | None:
        previous = self.get_develop(photo_id)
        if previous is None and not details:  # Nothing saved and nothing to record: no event.
            return None
        with self.connection:
            self.connection.execute("DELETE FROM photo_develop WHERE photo_id = ?", (photo_id,))
            self._insert_event(photo_id, "develop_cleared",
                               {"settings": previous.to_dict() if previous else {}, **(details or {})})
        return previous

    def invalidate_develop(self, photo_id: int, previous_fingerprint: str,
                           source_fingerprint: str) -> DevelopSettings | None:
        previous = self.get_develop(photo_id)
        if previous is None:
            return None
        with self.connection:
            self.connection.execute("DELETE FROM photo_develop WHERE photo_id = ?", (photo_id,))
            self._insert_event(photo_id, "develop_invalidated_source_replaced", {
                "settings": previous.to_dict(),
                "previous_fingerprint": previous_fingerprint,
                "source_fingerprint": source_fingerprint,
                "reason": "source content changed; saved develop settings are no longer trusted",
            })
        return previous

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

    def merge_groups(self, target_id: int, source_ids: list[int]) -> list[int]:
        """Move every photo of `source_ids` into `target_id`, keeping at most one manual pick (target's first), and drop the emptied groups."""
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            target = self.connection.execute("SELECT stable_key FROM photo_groups WHERE id = ?", (target_id,)).fetchone()
            if target is None:
                raise ValueError(f"group {target_id} does not exist")
            keep = None
            for group_id in (target_id, *source_ids):
                row = self.connection.execute(
                    "SELECT id FROM photos WHERE group_id = ? AND manual_pick = 1 ORDER BY id LIMIT 1", (group_id,)).fetchone()
                if row and keep is None:
                    keep = int(row["id"])
                elif row:
                    self.connection.execute("UPDATE photos SET manual_pick = 0 WHERE group_id = ?", (group_id,))
            for group_id in source_ids:
                self.connection.execute("UPDATE photos SET group_id = ?, merged_into = ? WHERE group_id = ?",
                                        (target_id, target["stable_key"], group_id))
                self.connection.execute("DELETE FROM photo_groups WHERE id = ?", (group_id,))
        return self.group_photo_ids(target_id)

    SPLIT_KEY = "split"  # merged_into for a frame kept in a burst of its own; regrouping never derives a key equal to it

    def split_photo(self, photo_id: int) -> int:
        """Mark `photo_id` to stay in a burst of its own and return the burst it leaves; regrouping does the move."""
        row = self.connection.execute("SELECT group_id FROM photos WHERE id = ?", (photo_id,)).fetchone()
        if not row or row["group_id"] is None:
            raise ValueError(f"photo {photo_id} is not in a group")
        if len(self.group_photo_ids(int(row["group_id"]))) < 2:
            raise ValueError(f"photo {photo_id} is already in a burst of its own")
        self.connection.execute("UPDATE photos SET merged_into = ? WHERE id = ?", (self.SPLIT_KEY, photo_id))
        self.connection.commit()
        return int(row["group_id"])

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

    def record_cleared(self, fingerprints: Iterable[str], cleared_at: str) -> int:
        """Remember cleared frames by fingerprint; returns how many were new. The ledger outlives photo rows."""
        with self.connection:
            before = self.connection.total_changes
            self.connection.executemany("INSERT OR IGNORE INTO cleared_fingerprints (fingerprint, cleared_at) VALUES (?, ?)",
                                        ((fingerprint, cleared_at) for fingerprint in fingerprints))
            return self.connection.total_changes - before

    def restore_work(self, photo_id: int, fingerprint: str, snapshot: Path) -> bool:
        """Copy the photographer's work for `fingerprint` from a cleanup snapshot onto a freshly imported row: rating,
        decision, manual pick, tags (caption and alt text live there), crop, develop settings, a typed place name and
        a typed position (only when the new file carries no camera fix). Returns False when the snapshot lacks it."""
        with closing(sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)) as source:
            source.row_factory = sqlite3.Row
            old = source.execute("SELECT * FROM photos WHERE fingerprint = ? ORDER BY id DESC LIMIT 1", (fingerprint,)).fetchone()
            if old is None:
                return False
            events = [(row["kind"], json.loads(row["details_json"])) for row in source.execute(
                "SELECT kind, details_json FROM provenance WHERE photo_id = ? AND kind IN "
                "('location_set', 'place_resolved', 'place_edited') ORDER BY id", (old["id"],))]
            rows = {table: source.execute(f"SELECT * FROM {table} WHERE photo_id = ?", (old["id"],)).fetchone()
                    for table in ("tags", "photo_edits", "photo_develop")}
        last = {kind: details for kind, details in events}  # newest of each kind wins
        last_place = next((kind for kind, _ in reversed(events) if kind != "location_set"), None)
        typed = last.get("location_set")
        position_typed = typed is not None and (typed["latitude"], typed["longitude"]) == (old["latitude"], old["longitude"])
        with self.connection:
            self.connection.execute("UPDATE photos SET rating = ?, decision = ?, manual_pick = ? WHERE id = ?",
                                    (old["rating"], old["decision"], old["manual_pick"], photo_id))
            if last_place == "place_edited":
                self.connection.execute("UPDATE photos SET place = ?, place_country = ? WHERE id = ?",
                                        (old["place"], old["place_country"], photo_id))
                self._insert_event(photo_id, "place_edited", last["place_edited"])
            if position_typed:
                fix = self.connection.execute("SELECT latitude FROM photos WHERE id = ?", (photo_id,)).fetchone()
                if fix["latitude"] is None:
                    self.connection.execute("UPDATE photos SET latitude = ?, longitude = ?, altitude = NULL WHERE id = ?",
                                            (old["latitude"], old["longitude"], photo_id))
                    self._insert_event(photo_id, "location_set", typed)
            for table, row in rows.items():
                if row is None:
                    continue
                values = dict(row) | {"photo_id": photo_id}
                self.connection.execute(f"INSERT OR REPLACE INTO {table} ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
                                        tuple(values.values()))
            self.connection.execute("DELETE FROM cleared_fingerprints WHERE fingerprint = ?", (fingerprint,))
            self._insert_event(photo_id, "work_restored", {"snapshot": snapshot.parent.name, "source_fingerprint": fingerprint})
        return True

    def cleared_fingerprints(self) -> set[str]:
        return {row["fingerprint"] for row in self.connection.execute("SELECT fingerprint FROM cleared_fingerprints")}

    def live_cleared_photo_ids(self) -> list[int]:
        return [row["id"] for row in self.connection.execute(
            "SELECT id FROM photos WHERE fingerprint IN (SELECT fingerprint FROM cleared_fingerprints) ORDER BY id")]

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
                (photo_id, sharpness, exposure, clipping, face_quality, eye_quality, astro_json, total_score, dhash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(photo_id) DO UPDATE SET
                sharpness=excluded.sharpness, exposure=excluded.exposure,
                clipping=excluded.clipping, face_quality=excluded.face_quality,
                eye_quality=excluded.eye_quality, astro_json=excluded.astro_json,
                total_score=COALESCE(excluded.total_score, metrics.total_score), dhash=excluded.dhash
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
                metrics.dhash,
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
            dhash=row["dhash"],
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

    def match_terms(self, photo_id: int, text: str) -> list[str]:
        """Labels for the stored local-AI fields that contain any `|`-separated term of `text` (case-insensitive), in a fixed order."""
        needles = [term.casefold() for term in _text_terms(text)]
        if not needles:
            return []
        row = self.connection.execute("SELECT place, place_country FROM photos WHERE id = ?", (photo_id,)).fetchone()
        place = [f"place: {row['place']}"] if row and row["place"] and any(
            needle in f"{row['place']} {row['place_country'] or ''}".casefold() for needle in needles) else []
        tags = self.get_tags(photo_id)
        if not tags:
            return place
        data = tags["data"]

        def strings(value: Any) -> list[str]:
            return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []
        problems = data.get("problems")
        species = [item.get("name") for item in data.get("species") or [] if isinstance(item, dict)]
        candidates = [("description", data.get("description"))]
        candidates += [(f"keyword: {k}", k) for k in strings(data.get("subjects"))]
        candidates += [(f"suggested: {k}", k) for k in strings(data.get("suggested"))]
        candidates += [(f"species: {name}", name) for name in strings(species)]
        candidates.append(("caption", data.get("caption")))
        kinds = strings(problems.get("kinds")) if isinstance(problems, dict) else []
        candidates += [(f"problem: {kind.replace('_', ' ')}", f"{kind} {kind.replace('_', ' ')}") for kind in kinds]
        return place + [label for label, value in candidates if isinstance(value, str) and any(needle in value.casefold() for needle in needles)]

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

    def update_preview(self, photo_id: int, preview_path: str, preview_kind: str) -> None:
        if not isinstance(preview_kind, str) or preview_kind not in PREVIEW_KINDS:
            raise ValueError("preview kind must be embedded or refined")
        with self.connection:
            if not self.connection.execute("UPDATE photos SET preview_path = ?, preview_kind = ? WHERE id = ?",
                                            (preview_path, preview_kind, photo_id)).rowcount:
                raise ValueError(f"unknown photo ID: {photo_id}")

    def preview_references(self, preview_path: str) -> int:
        return self.connection.execute("SELECT COUNT(*) FROM photos WHERE preview_path = ?", (preview_path,)).fetchone()[0]

    def photos_for_filters(self, filters: GroupFilters) -> list[int]:
        """IDs of grouped photos matching the contact-sheet filters, in catalog order."""
        where, params = filters.where()
        return [row[0] for row in self.connection.execute(
            f"SELECT p.id FROM photos p JOIN photo_groups g ON g.id = p.group_id WHERE {where} ORDER BY p.id", params)]

    def set_mirror_path(self, photo_id: int, mirror_path: str | None) -> None:
        """Verified mirror copy of an ingested original; upserts never touch it."""
        with self.connection:
            self.connection.execute("UPDATE photos SET mirror_path = ? WHERE id = ?", (mirror_path, photo_id))

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

    def list_groups(self, filters: GroupFilters | None = None) -> list[dict[str, Any]]:
        """Groups with their ranked photos; with filters, only matching photos and non-empty groups."""
        where, params = filters.where() if filters else ("1", [])
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
                f"""
                SELECT p.*, m.sharpness, m.exposure, m.clipping, m.face_quality,
                       m.eye_quality, m.astro_json, m.total_score, e.selected_at AS crop_selected_at
                FROM photos p JOIN photo_groups g ON g.id = p.group_id LEFT JOIN metrics m ON m.photo_id = p.id
                LEFT JOIN photo_edits e ON e.photo_id = p.id
                WHERE p.group_id = ? AND {where}
                ORDER BY COALESCE(m.total_score, -1) DESC, p.path
                """,
                (group["id"], *params),
            ).fetchall()
            if rows:
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

    def group_frame_counts(self) -> dict[int, tuple[int, int]]:
        """Unfiltered {group_id: (frames, rejected)}; a burst's counts never depend on the view."""
        rows = self.connection.execute(
            "SELECT group_id, COUNT(*), SUM(decision = 'reject') FROM photos WHERE group_id IS NOT NULL GROUP BY group_id"
        )
        return {int(row[0]): (int(row[1]), int(row[2])) for row in rows}

    def group_counts(self, filters: GroupFilters | None = None) -> dict[str, int]:
        """Grouped photos matching the filters, and all grouped photos."""
        where, params = filters.where() if filters else ("1", [])
        query = "SELECT COUNT(*) FROM photos p JOIN photo_groups g ON g.id = p.group_id WHERE "
        return {
            "filtered": self.connection.execute(query + where, params).fetchone()[0],
            "total": self.connection.execute(query + "1").fetchone()[0],
        }

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
            mirror_path=row["mirror_path"],
            preview_kind=row["preview_kind"],
            exposure_seconds=row["exposure_seconds"],
            merged_into=row["merged_into"],
            latitude=row["latitude"],
            longitude=row["longitude"],
            altitude=row["altitude"],
            place=row["place"],
            place_country=row["place_country"],
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
