from __future__ import annotations

import hashlib
import re
import fcntl
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from functools import wraps
from pathlib import Path

from PIL import Image

from .analysis import analyze_astro, analyze_faces, analyze_subject_regions, apply_vision_result, measure_image
from .catalog import Catalog, GroupFilters
from .config import Settings
from .composition import CropCandidate, generate_candidates
from .domain import SUPPORTED_EXTENSIONS, Artifact, Job, Photo, PhotoEdit
from . import ingest
from .astro import execute_job, load_manifest, prepare_siril_job, probe_siril
from .jobs import LocalJobRunner
from .omlx import OmlxError
from .pipelines import PipelineRegistry
from .tools import find_tool
from .grouping import TimeGapGroupingPolicy
from .previews import ExifToolPreviewProvider, PreviewError, fingerprint_file, read_image_dimensions, read_metadata
from .scoring import RankedPhoto, rank_group
from .sidecars import SidecarWriter


_MUTATION_LOCKS = {}
_MUTATION_GUARD = threading.Lock()
_MUTATION_HELD = threading.local()


def _coordinated(method):
    @wraps(method)
    def coordinated(self, *args, **kwargs):
        with self._mutation():
            return method(self, *args, **kwargs)
    return coordinated


_ABSOLUTE_PATH = re.compile(r"'?/[^\s'\"]+'?")


def _job_detail(job_id: int | None) -> dict:
    """Provenance link to the background job that did the work; synchronous calls record none."""
    return {} if job_id is None else {"job_id": job_id}


def _select(ranking: list[RankedPhoto]) -> RankedPhoto:
    """Manual unrejected pick, else best unrejected frame, else best frame (keeper filtering drops it)."""
    kept = [item for item in ranking if item.photo.decision != "reject"]
    return next((item for item in kept if item.photo.manual_pick), kept[0] if kept else ranking[0])


def public_message(text: str | None) -> str | None:
    """Strip absolute paths (sources, library, executables) from a message bound for the browser or CLI."""
    return None if text is None else _ABSOLUTE_PATH.sub("<path>", text)


@dataclass(slots=True)
class ImportSummary:
    discovered: int = 0
    imported: int = 0
    updated: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    skipped: int = 0


@dataclass(frozen=True, slots=True)
class ExportRequest:
    photo_ids: tuple[int, ...]
    profile: Path | None = None


@dataclass(frozen=True, slots=True)
class ExportResult:
    photo_id: int
    success: bool
    output: Path | None
    message: str
    artifact: Artifact | None = None


@dataclass(frozen=True, slots=True)
class CleanupResult:
    photos_removed: int
    files_removed: int
    trash_path: Path
    snapshot_path: Path


class CleanupError(RuntimeError):
    def __init__(self, cause: Exception, recovery_path: Path | None = None,
                 snapshot_path: Path | None = None, restoration_errors: tuple[Exception, ...] = ()):
        self.recovery_path = recovery_path
        self.snapshot_path = snapshot_path
        self.restoration_errors = restoration_errors
        self.recovery_complete = not restoration_errors
        message = f"cleanup failed: {cause}"
        if restoration_errors:
            message += "; restoration failed: " + "; ".join(str(error) for error in restoration_errors)
        if recovery_path is not None:
            message += f"; recovery files: {recovery_path}"
        super().__init__(message)


class Library:
    def __init__(self, settings: Settings, job_runner: LocalJobRunner | None = None):
        self.settings = settings
        settings.ensure_directories()
        self.catalog = Catalog(settings.database_path)
        self.previews = ExifToolPreviewProvider()
        self.sidecars = SidecarWriter(settings.sidecar_dir)
        # An injected runner outlives this Library (the web server shares one across requests).
        self._runner: LocalJobRunner | None = job_runner
        self._owns_runner = job_runner is None

    @property
    def job_runner(self) -> LocalJobRunner:
        # Created on first use: the runner takes the catalog's exclusive owner lock and starts a worker thread.
        if self._runner is None:
            self._runner = LocalJobRunner(self.settings.database_path)
        return self._runner

    def close(self) -> None:
        try:
            if self._runner is not None and self._owns_runner:
                self._runner.shutdown()
            self._runner = None
        finally:
            self.catalog.close()

    def __enter__(self) -> "Library":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def _mutation(self):
        path = self.settings.library_dir.resolve() / self.settings.mutation_lock_path.name
        with _MUTATION_GUARD:
            lock = _MUTATION_LOCKS.setdefault(path, threading.RLock())
        with lock:
            held = getattr(_MUTATION_HELD, "paths", None)
            if held is None:
                held = _MUTATION_HELD.paths = set()
            if path in held:
                yield
                return
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "a+b") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                held.add(path)
                try:
                    yield
                finally:
                    held.remove(path)
                    fcntl.flock(handle, fcntl.LOCK_UN)

    @_coordinated
    def import_path(self, requested: Path) -> ImportSummary:
        requested = requested.expanduser().resolve()
        summary = ImportSummary()
        if not requested.exists():
            summary.failed = 1
            summary.errors.append(f"Path does not exist: {requested}")
            return summary
        candidates = self._candidates(requested)
        if requested.is_file() and requested.suffix.lower() not in SUPPORTED_EXTENSIONS:
            summary.failed = 1
            summary.errors.append(f"Unsupported image type: {requested.name}")
            return summary
        summary.discovered = len(candidates)
        for source in candidates:
            self._import_candidate(source, summary)
        self._regroup_and_write_sidecars()
        return summary

    def _import_candidate(self, source: Path, summary: ImportSummary, *, skip_unchanged: bool = False,
                          job_id: int | None = None) -> None:
        previous = self.catalog.photo_by_path(str(source))
        try:
            fingerprint = fingerprint_file(source)
            if (skip_unchanged and previous and previous.fingerprint == fingerprint
                    and previous.preview_path and Path(previous.preview_path).is_file()):
                summary.skipped += 1
                return
            self._import_one(source, fingerprint, job_id=job_id)
            if previous:
                summary.updated += 1
            else:
                summary.imported += 1
        except (OSError, PreviewError, ValueError, subprocess.SubprocessError) as error:
            summary.failed += 1
            summary.errors.append(f"{source.name}: {error}")
            failed_photo = self.catalog.photo_by_path(str(source))
            details = {"source": str(source), "error": str(error)} | _job_detail(job_id)
            try:
                details["source_fingerprint"] = fingerprint_file(source)
            except OSError:
                pass
            self.catalog.append_event(failed_photo.id if failed_photo else None, "import_failed", details)

    def start_import_job(self, requested: Path) -> Job:
        requested = requested.expanduser().resolve()
        if not requested.exists():
            raise ValueError(f"Path does not exist: {requested}")
        if requested.is_file() and requested.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise ValueError(f"Unsupported image type: {requested.name}")
        runner = self.job_runner
        job = self.catalog.create_prepared_job(
            "import", lambda job_id: self._write_job_files(job_id, {"kind": "import", "path": str(requested)}))

        def work(context) -> None:
            with Library(self.settings, job_runner=runner) as worker:
                candidates = worker._candidates(requested)
                summary = ImportSummary(discovered=len(candidates))
                for index, source in enumerate(candidates, 1):
                    if context.cancelled:
                        break
                    with worker._mutation():
                        worker._import_candidate(source, summary, skip_unchanged=True, job_id=job.id)
                    for error in summary.errors:
                        worker._log_job(job, error)
                    summary.errors.clear()
                    context.update_progress(index / len(candidates), label=source.name[:200], processed=index, total=len(candidates))
                with worker._mutation():
                    worker._regroup_and_write_sidecars()
                counts = {key: getattr(summary, key) for key in ("discovered", "imported", "updated", "skipped", "failed")}
                worker._write_job_files(job.id, {"kind": "import", "path": str(requested), "summary": counts})

        self._submit_job(runner, job, work)
        return job

    def start_tag_job(self, tagger, *, retag: bool = False) -> Job:
        if type(retag) is not bool:
            raise ValueError("retag must be true or false")
        runner = self.job_runner
        job = self.catalog.create_prepared_job(
            "tag", lambda job_id: self._write_job_files(job_id, {"kind": "tag", "retag": retag}))

        def work(context) -> None:
            with Library(self.settings, job_runner=runner) as worker:
                photos = worker.catalog.list_photos()
                counts = {"tagged": 0, "skipped": 0, "failed": 0}
                failed_photo_ids = []
                for index, photo in enumerate(photos, 1):
                    if context.cancelled:
                        break
                    with worker._mutation():
                        if not retag and worker.catalog.get_tags(photo.id) is not None:
                            counts["skipped"] += 1
                        else:
                            try:
                                worker.tag_photo(photo.id, tagger, job_id=job.id)  # A model failure appends tag_failed itself.
                                counts["tagged"] += 1
                            except (OmlxError, ValueError, OSError) as error:
                                counts["failed"] += 1
                                failed_photo_ids.append(photo.id)
                                worker._log_job(job, f"{Path(photo.path).name}: {type(error).__name__}: {error}")
                    context.update_progress(index / len(photos), label=Path(photo.path).name[:200],
                                            processed=index, total=len(photos))
                with worker._mutation():
                    worker._regroup_and_write_sidecars()
                worker._write_job_files(job.id, {"kind": "tag", "retag": retag,
                                                 "failed_photo_ids": failed_photo_ids, "summary": counts})

        self._submit_job(runner, job, work)
        return job

    def start_ingest_job(self, card: Path, primary: Path, mirror: Path | None = None) -> Job:
        """Copy a card to primary (and mirror), verify every copy, then import only the primary copies."""
        card, roots = ingest.validate_ingest(card, [primary] if mirror is None else [primary, mirror],
                                             library_dir=self.settings.library_dir)
        runner = self.job_runner
        manifest = {"schema_version": 1, "card": str(card), "destinations": [str(root) for root in roots],
                    "started_at": None, "finished_at": None, "snapshot": None, "files": [], "summary": None}

        def prepare(job_id: int) -> tuple[str, str]:
            job_dir = self.settings.output_dir / "ingest" / str(job_id)
            job_dir.mkdir(parents=True, exist_ok=True)
            (job_dir / "ingest.log").touch()
            ingest.write_manifest(job_dir / "manifest.json", manifest)
            return str(job_dir / "manifest.json"), str(job_dir / "ingest.log")

        job = self.catalog.create_prepared_job("ingest", prepare)
        manifest_path = Path(job.manifest_path)
        hidden_paths = (*zip(roots, ("<primary>", "<mirror>")), (card, "<card>"))

        def log(line: str) -> None:
            Library._log_job(job, line, hide=hidden_paths)

        def work(context) -> None:
            with Library(self.settings, job_runner=runner) as worker:
                manifest["started_at"] = datetime.now(timezone.utc).isoformat()
                scan = ingest.scan_card(card)
                # ponytail: one exiftool call per photo while planning; batch it if large cards plan too slowly.
                items = ingest.plan_ingest(card, roots, library_dir=self.settings.library_dir, today=date.today(), scan=scan)
                if scan.hidden:
                    log(f"{len(scan.hidden)} hidden entries (dot-prefixed) were skipped")
                for relative in scan.unreadable:
                    log(f"{relative}: could not be read from the card; not verified")
                files = manifest["files"]

                def save() -> None:  # Rewritten after every file so an interrupted ingest can be resumed by a rerun.
                    manifest["summary"] = ingest.summarize(files, roots, planned=len(items), skipped=len(scan.hidden),
                                                           unreadable=len(scan.unreadable))
                    ingest.write_manifest(manifest_path, manifest)

                save()
                for index, item in enumerate(items, 1):
                    if context.cancelled:
                        break
                    files.append(ingest.ingest_item(item, roots))
                    if "error" in files[-1]:
                        log(f"{item.relative}: {files[-1]['error']}")
                    save()
                    context.update_progress(index / len(items) / 2, label=item.source.name[:200],
                                            processed=index, total=len(items))
                primary_verified = len(files) == len(items) and all(
                    entry["outcomes"][str(roots[0])] in ingest.VERIFIED for entry in files)
                if not context.cancelled and primary_verified and items:
                    manifest["import"] = worker._import_ingested(context, job, items, files, roots, log)
                    if not context.cancelled:
                        with worker._mutation():
                            manifest["snapshot"] = worker._snapshot_catalog(roots[0])
                elif not context.cancelled and not primary_verified:
                    log("Primary copies did not all verify; nothing was imported.")
                manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
                save()
                if not context.cancelled:
                    worker.catalog.update_job(job.id, result_path=str(manifest_path))

        self._submit_job(runner, job, work)
        return job

    def _import_ingested(self, context, job: Job, items: list, files: list[dict], roots: tuple[Path, ...], log) -> dict:
        """Import each verified primary photo copy (never the card; videos and other files are only copied)."""
        photos = [(item, entry) for item, entry in zip(items, files) if item.source.suffix.lower() in SUPPORTED_EXTENSIONS]
        summary = ImportSummary(discovered=len(photos))
        for index, (item, entry) in enumerate(photos, 1):
            if context.cancelled:
                break
            mirrored = len(roots) > 1 and entry["outcomes"][str(roots[1])] in ingest.VERIFIED
            with self._mutation():
                self._import_candidate(item.destinations[0], summary, skip_unchanged=True, job_id=job.id)
                if photo := self.catalog.photo_by_path(str(item.destinations[0])):
                    self.catalog.set_mirror_path(photo.id, str(item.destinations[1]) if mirrored else None)
                    self.catalog.append_event(photo.id, "ingested", {
                        "job_id": job.id, "destination": "primary", "mirror": mirrored,
                        "card_relative": item.relative, "sha256": entry["sha256"],
                    })
            for error in summary.errors:
                log(error)
            summary.errors.clear()
            context.update_progress(0.5 + index / len(photos) / 2, label=item.source.name[:200],
                                    processed=index, total=len(photos))
        with self._mutation():
            self._regroup_and_write_sidecars()
        return {key: getattr(summary, key) for key in ("discovered", "imported", "updated", "skipped", "failed")}

    def _snapshot_catalog(self, primary: Path) -> str:
        """Catalog copy beside the primary originals, written to a temporary name and renamed into place."""
        backups = primary / "lumen-catalog-backups"
        backups.mkdir(exist_ok=True)
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".sqlite3"
        temporary = backups / f".{name}.partial"
        try:
            temporary.unlink(missing_ok=True)
            self.catalog.backup(temporary)
            with temporary.open("rb") as handle:
                ingest.full_fsync(handle.fileno())
            temporary.replace(backups / name)
            ingest.fsync_directory(backups)
        finally:
            temporary.unlink(missing_ok=True)
        return name

    def _write_job_files(self, job_id: int, manifest: dict) -> tuple[str, str]:
        job_dir = self.settings.output_dir / "jobs" / str(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        log = job_dir / "job.log"
        log.touch()
        SidecarWriter._atomic_write(job_dir / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        return str(job_dir / "manifest.json"), str(log)

    @staticmethod
    def _log_job(job: Job, line: str, *, hide: Sequence[tuple[Path, str]] = ()) -> None:
        for path, label in hide:  # Named roots first: public_message alone cannot see paths that contain spaces.
            line = line.replace(str(path), label)
        with open(job.log_path, "a", encoding="utf-8") as handle:
            handle.write(public_message(line).replace("\n", " ") + "\n")

    def _submit_job(self, runner: LocalJobRunner, job: Job, work) -> None:
        try:
            runner.submit(job.id, work)
        except RuntimeError:
            self.catalog.update_job(job.id, status="cancelled")  # Never leave a queued row nothing will run.
            raise

    def list_groups(self, filters: GroupFilters | None = None) -> list[dict]:
        return self.catalog.list_groups(filters)

    def cameras(self) -> list[str]:
        """Every camera in the catalog, unfiltered, for the filter menu."""
        return [row[0] for row in self.catalog.connection.execute("SELECT DISTINCT camera_id FROM photos ORDER BY camera_id")]

    def photo_count(self) -> int:
        return self.catalog.photo_count()

    def keeper_ids(self) -> list[int]:
        return [int(photo["id"]) for group in self.list_groups() for photo in group["photos"]
                if photo["decision"] != "reject" and (photo["decision"] == "flag" or photo["id"] == group["automatic_pick_id"])]

    @_coordinated
    def set_decision(self, photo_id: int, decision: str) -> dict:
        return self._decided(self.catalog.set_decision(photo_id, decision))

    @_coordinated
    def set_rating(self, photo_id: int, rating: int) -> dict:
        return self._decided(self.catalog.set_rating(photo_id, rating))

    def _decided(self, photo: Photo) -> dict:
        pick = self._rerank_group(photo.group_id) if photo.group_id is not None else None
        if pick is None:
            self._write_sidecar(photo.id)
        return {"photo_id": photo.id, "decision": photo.decision, "rating": photo.rating,
                "group_id": photo.group_id, "automatic_pick_id": pick}

    def _rerank_group(self, group_id: int) -> int | None:
        """Reapply the selection rule to one group and rewrite its sidecars; other groups are untouched."""
        photos = [photo for photo_id in self.catalog.group_photo_ids(group_id) if (photo := self.catalog.get_photo(photo_id))]
        scored = [(photo, metrics) for photo in photos if (metrics := self.catalog.get_metrics(photo.id))]
        if not scored:
            return None
        ranking = rank_group(scored)
        pick = int(_select(ranking).photo.id)
        self.catalog.rerank_group(group_id, pick, {int(item.photo.id): item.total for item in ranking})
        for photo in photos:
            self._write_sidecar(photo.id, selected=photo.id == pick)
        return pick

    @_coordinated
    def crop_candidates(self, photo_id: int) -> list[CropCandidate]:
        if type(photo_id) is not int or not (photo := self.catalog.get_photo(photo_id)):
            raise ValueError(f"unknown photo ID: {photo_id}")
        source = Path(photo.path)
        if not source.is_file():
            raise ValueError(f"source image is unavailable: {source}")
        width, height = read_image_dimensions(source)
        regions = analyze_subject_regions(source, self.settings.vision_analyzer_path)
        if regions and (regions.image_width, regions.image_height) != (width, height):
            regions = None
        return generate_candidates(width, height, regions)

    @_coordinated
    def choose_crop(self, photo_id: int, candidate_id: str) -> PhotoEdit:
        candidates = self.crop_candidates(photo_id)
        candidate = next((item for item in candidates if item.id == candidate_id and item.kind != "original"), None)
        if candidate is None:
            raise ValueError("crop candidate is unavailable or cannot be selected")
        photo = self.catalog.get_photo(photo_id)
        if photo is None:
            raise ValueError(f"unknown photo ID: {photo_id}")
        width, height = read_image_dimensions(Path(photo.path))
        edit = PhotoEdit(
            photo_id=photo_id, candidate_id=candidate.id, rect=candidate.rect,
            aspect=candidate.aspect, source_width=width, source_height=height,
            author="manual", rationale=candidate.rationale,
            selected_at=datetime.now(timezone.utc),
        )
        self._change_crop(photo_id, edit)
        return edit

    @_coordinated
    def clear_crop(self, photo_id: int) -> None:
        if type(photo_id) is not int or not self.catalog.get_photo(photo_id):
            raise ValueError(f"unknown photo ID: {photo_id}")
        if self.catalog.get_crop(photo_id) is None:
            return
        self._change_crop(photo_id, None)

    @_coordinated
    def review_composition(self, photo_id: int, tagger):
        photo = self.catalog.get_photo(photo_id)
        if not photo or not photo.preview_path or not (preview := Path(photo.preview_path)).is_file():
            raise ValueError(f"photo {photo_id} has no preview")
        candidates = self.crop_candidates(photo_id)
        candidate_ids = [candidate.id for candidate in candidates]
        model = getattr(tagger, "model", "unknown")
        try:
            with tempfile.TemporaryDirectory(prefix=".composition-review-", dir=self.settings.cache_dir) as directory:
                review_previews = []
                with Image.open(preview) as image:
                    for candidate in candidates:
                        path = preview
                        if candidate.kind != "original":
                            path = Path(directory) / f"{candidate.id}.jpg"
                            image.crop(candidate.rect.pixel_box(*image.size)).convert("RGB").save(
                                path, format="JPEG", quality=90
                            )
                        review_previews.append((candidate.id, path))
                result = tagger.review_composition(preview, review_previews)
                if getattr(result, "candidate_id", None) not in candidate_ids:
                    raise ValueError("composition review returned an unavailable candidate")
        except Exception as error:
            self.catalog.append_event(photo_id, "composition_review_failed", {
                "model": model,
                "error": str(error),
                "raw_response": getattr(error, "raw_response", None),
                "candidate_ids": candidate_ids,
            })
            try:
                self._write_sidecar(photo_id)
            except Exception as sidecar_error:
                error.add_note(f"composition failure sidecar write failed: {sidecar_error}")
            raise
        self.catalog.append_event(photo_id, "composition_reviewed", {
            "model": model,
            "candidate_id": result.candidate_id,
            "rationale": result.rationale,
            "confidence": result.confidence,
            "candidate_ids": candidate_ids,
        })
        self._write_sidecar(photo_id)
        return result

    def _change_crop(self, photo_id: int, edit: PhotoEdit | None) -> None:
        previous = self.catalog.get_crop(photo_id)
        previous_events = self.catalog.crop_event_ids(photo_id)
        if edit is None:
            self.catalog.clear_crop(photo_id)
        else:
            self.catalog.save_crop(edit)
        try:
            self._write_sidecar(photo_id)
        except BaseException as error:
            compensation_errors = []
            try:
                self.catalog.restore_crop_state(photo_id, previous, previous_events)
            except BaseException as compensation_error:
                compensation_errors.append(compensation_error)
            try:
                self._write_sidecar(photo_id)
            except BaseException as compensation_error:
                compensation_errors.append(compensation_error)
            if compensation_errors:
                error.add_note("crop compensation failed: " + "; ".join(map(str, compensation_errors)))
            raise

    @_coordinated
    def remove_group(self, group_id: int) -> CleanupResult:
        try:
            if type(group_id) is not int or group_id not in self.catalog.list_group_ids():
                raise ValueError(f"group {group_id} does not exist")
            photo_ids = self.catalog.group_photo_ids(group_id)
            return self._cleanup(photo_ids, [group_id])
        except sqlite3.Error as error:
            if isinstance(error, sqlite3.ProgrammingError):
                raise
            raise CleanupError(error) from error

    @_coordinated
    def clear_workspace(self, confirmation: str) -> CleanupResult:
        if confirmation != "CLEAR":
            raise ValueError('clear workspace requires the exact confirmation "CLEAR"')
        try:
            photo_ids = [photo.id for photo in self.catalog.list_photos()]
            return self._cleanup(photo_ids, self.catalog.list_group_ids())
        except sqlite3.Error as error:
            if isinstance(error, sqlite3.ProgrammingError):
                raise
            raise CleanupError(error) from error

    def _cleanup(self, photo_ids: list[int], group_ids: list[int]) -> CleanupResult:
        photos = self.catalog.list_photos()
        selected = set(photo_ids)
        root = self.settings.library_dir.resolve()

        def managed_path(path: Path) -> Path:
            relative = path.absolute().relative_to(self.settings.library_dir.absolute())
            if not relative.parts or ".." in relative.parts:
                raise ValueError(f"unsafe managed cleanup path: {path}")
            canonical = root
            for part in relative.parts:
                canonical /= part
                if canonical.is_symlink():
                    raise ValueError(f"symlink in managed cleanup path: {path}")
            return canonical

        directories = {directory: managed_path(directory) for directory in (
            self.settings.preview_dir, self.settings.sidecar_dir, self.settings.trash_dir,
        )}
        recovery_root = directories[self.settings.trash_dir]
        protected = {Path(photo.path).resolve() for photo in photos}
        protected.update(Path(artifact.path).resolve() for artifact in self.catalog.list_artifacts())
        database = self.catalog.path.resolve()
        protected.update(database.with_name(database.name + suffix) for suffix in ("", "-wal", "-shm", "-journal"))
        retained_previews = {
            Path(photo.preview_path).resolve()
            for photo in photos if photo.id not in selected and photo.preview_path
        }
        manifest: dict[Path, list[int]] = {}
        for photo in photos:
            if photo.id not in selected:
                continue
            candidates = [(path, self.settings.sidecar_dir) for path in self.sidecars.paths(photo)]
            if photo.preview_path and Path(photo.preview_path).resolve() not in retained_previews:
                candidates.insert(0, (Path(photo.preview_path), self.settings.preview_dir))
            for path, directory in candidates:
                resolved = managed_path(path)
                if (not resolved.is_relative_to(directories[directory])
                        or resolved.is_relative_to(recovery_root) or resolved in protected):
                    raise ValueError(f"unsafe managed cleanup path: {path}")
                if path.is_file():
                    manifest.setdefault(resolved, []).append(photo.id)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ-")
        trash = Path(tempfile.mkdtemp(prefix=timestamp, dir=self.settings.trash_dir))
        snapshot = None
        moves = [(source, trash / "files" / source.relative_to(root)) for source in manifest]
        staged = []
        try:
            destination = trash / "catalog.sqlite3"
            destination.touch(exist_ok=False)
            self.catalog.backup(destination)
            snapshot = destination
            with (trash / "manifest.json").open("x", encoding="utf-8") as handle:
                json.dump({
                    "photo_ids": photo_ids,
                    "group_ids": group_ids,
                    "files": [{"photo_ids": manifest[source], "source": str(source), "staged": str(target)}
                              for source, target in moves],
                }, handle, indent=2)
                handle.write("\n")
            for _, target in moves:
                target.parent.mkdir(parents=True, exist_ok=True)
            for source, target in moves:
                if target.exists() or target.is_symlink():
                    raise FileExistsError(f"staging destination already exists: {target}")
                staged.append((source, target))
                source.rename(target)
            self.catalog.delete_photos(photo_ids, group_ids=group_ids)
        except BaseException as error:
            failures = []
            for source, target in reversed(staged):
                try:
                    if target.exists():
                        if source.exists():
                            raise FileExistsError(f"cannot restore over existing path: {source}")
                        target.rename(source)
                except BaseException as restore_error:
                    failures.append(restore_error)
            if all(isinstance(failure, (OSError, sqlite3.Error))
                   and not isinstance(failure, sqlite3.ProgrammingError) for failure in (error, *failures)):
                raise CleanupError(error, trash, snapshot, tuple(failures)) from error
            if failures:
                raise BaseExceptionGroup(f"cleanup failed; recovery files remain in {trash}", [error, *failures]) from error
            raise
        return CleanupResult(len(photo_ids), len(moves), trash, snapshot)

    @_coordinated
    def export_jpegs(self, request: ExportRequest) -> list[ExportResult]:
        if not request.photo_ids or len(set(request.photo_ids)) != len(request.photo_ids):
            raise ValueError("export requires unique photo IDs")
        photos = []
        for photo_id in request.photo_ids:
            if type(photo_id) is not int or not (photo := self.catalog.get_photo(photo_id)):
                raise ValueError(f"unknown photo ID: {photo_id}")
            photos.append(photo)
        if request.profile is not None and not request.profile.is_file():
            raise ValueError(f"profile does not exist: {request.profile}")
        if not self.settings.jpeg_output_dir.resolve().is_relative_to(self.settings.library_dir.resolve()):
            raise ValueError("JPEG output must stay inside the managed library")
        pipeline = PipelineRegistry().darktable(self.settings.output_dir)
        results = []
        for photo in photos:
            rendered = None
            artifact = None
            try:
                tags = self.catalog.get_tags(photo.id)
                data = tags["data"] if tags else {}
                if fingerprint_file(Path(photo.path)) != photo.fingerprint:
                    raise ValueError("source content changed since import; reimport before exporting")
                edit = self.catalog.get_crop(photo.id)
                crop = None
                if edit is not None:
                    source_width, source_height = read_image_dimensions(Path(photo.path))
                    if (source_width, source_height) != (edit.source_width, edit.source_height):
                        raise ValueError("source dimensions changed since the crop was selected; choose a new crop")
                    crop = {
                        "left": edit.rect.left, "top": edit.rect.top,
                        "width": edit.rect.width, "height": edit.rect.height,
                        "photo_id": photo.id, "candidate_id": edit.candidate_id,
                        "aspect": edit.aspect, "source_width": edit.source_width,
                        "source_height": edit.source_height, "source_fingerprint": photo.fingerprint,
                        "author": edit.author, "rationale": edit.rationale,
                        "selected_at": edit.selected_at.isoformat(),
                    }
                rendered = pipeline.export_jpeg(Path(photo.path), request.profile,
                    crop=crop,
                    description=data.get("description"), keywords=data.get("subjects"),
                    rating=-1 if photo.decision == "reject" else photo.rating,
                    reserved_paths={Path(artifact.path) for artifact in self.catalog.list_artifacts()})
                if not rendered.success:
                    results.append(ExportResult(photo.id, False, None, rendered.message))
                    continue
                artifact = self.catalog.save_artifact(photo.id, Path(photo.path).name,
                    rendered.settings["source_fingerprint"], "jpeg", str(rendered.output),
                    rendered.settings, rendered.output_fingerprint)
                self.catalog.append_event(photo.id, "exported", {
                    **rendered.settings, "artifact_id": artifact.id, "output": artifact.path,
                    "output_fingerprint": artifact.output_fingerprint,
                    "created_at": artifact.created_at.isoformat(),
                })
                self._write_sidecar(photo.id)
                results.append(ExportResult(photo.id, True, rendered.output, rendered.message, artifact))
            except Exception as error:
                errors = [str(error)]
                if rendered is not None and rendered.success:
                    try:
                        if artifact is not None:
                            self.catalog.discard_export(artifact.id)
                        else:
                            self.catalog.connection.rollback()
                    except Exception as compensation_error:
                        errors.append(f"catalog compensation failed: {compensation_error}")
                    try:
                        rendered.output.unlink(missing_ok=True)
                    except Exception as compensation_error:
                        errors.append(f"JPEG compensation failed ({rendered.output}): {compensation_error}")
                    try:
                        if self.catalog.get_photo(photo.id) is None:
                            for path in self.sidecars.paths(photo):
                                path.unlink(missing_ok=True)
                        else:
                            self._write_sidecar(photo.id)
                    except Exception as compensation_error:
                        errors.append(f"sidecar compensation failed: {compensation_error}")
                results.append(ExportResult(photo.id, False, None, "; ".join(errors)))
        return results

    @_coordinated
    def prepare_astro_job(self, group_id: int, *, confirmed: bool, excluded_photo_ids: Sequence[int] = (),
                          darks: Path | None = None, flats: Path | None = None, biases: Path | None = None) -> Job:
        if confirmed is not True:
            raise ValueError("astro stacking requires explicit photographer confirmation")
        if type(group_id) is not int or not (photo_ids := self.catalog.group_photo_ids(group_id)):
            raise ValueError(f"unknown group ID: {group_id}")
        excluded = list(excluded_photo_ids)
        if len(set(excluded)) != len(excluded) or any(type(item) is not int or item not in photo_ids for item in excluded):
            raise ValueError("excluded photos must be unique members of the group")
        photos = [self.catalog.get_photo(photo_id) for photo_id in photo_ids]
        metrics = {}
        for photo in photos:
            if fingerprint_file(Path(photo.path)) != photo.fingerprint:
                raise ValueError(f"source content changed since import; reimport before stacking: {Path(photo.path).name}")
            if stored := self.catalog.get_metrics(photo.id):
                metrics[Path(photo.path)] = stored.astro
        executable = find_tool("siril-cli")
        if not executable:
            raise RuntimeError("siril-cli is not installed")
        siril = probe_siril(executable)

        def prepare(job_id: int) -> tuple[str, str]:
            manifest = prepare_siril_job(
                output_root=self.settings.output_dir, job_id=job_id, siril=siril,
                lights=[Path(photo.path) for photo in photos],
                excluded=[Path(photo.path) for photo in photos if photo.id in excluded],
                darks=darks, flats=flats, biases=biases, astro_metrics=metrics,
            )
            job_dir = Path(manifest.job_dir)
            return str(job_dir / "manifest.json"), str(job_dir / manifest.log)

        return self.catalog.create_prepared_job("astro", prepare)

    def run_astro_job(self, job_id: int, *, timeout_seconds: float = 7200.0) -> Job:
        with self._mutation():
            job = self.catalog.get_job(job_id)
            if job.kind != "astro":
                raise ValueError(f"job {job_id} is not an astro job")
            if job.status != "queued":
                raise ValueError(f"job {job_id} is not queued")
            load_manifest(Path(job.manifest_path), self.settings.output_dir)
            runner = self.job_runner

        def work(context) -> None:
            # Re-read and re-verify on the worker so nothing changed between submission and launch.
            manifest = load_manifest(Path(job.manifest_path), self.settings.output_dir)
            execution = execute_job(context, manifest, output_root=self.settings.output_dir, timeout_seconds=timeout_seconds)
            if execution is not None:
                self._record_astro_result(context, job_id, manifest, execution)

        runner.submit(job_id, work)
        return job

    def cancel_job(self, job_id: int) -> bool:
        if self._runner is not None:
            return self._runner.cancel(job_id)
        with self._mutation():
            job = self.catalog.get_job(job_id)
            if job.status == "queued":
                self.catalog.update_job(job_id, status="cancelled")
                return True
            if job.status == "running":
                raise RuntimeError(f"job {job_id} is running under another job runner; cancel it there")
            return job.status == "cancelled"

    def wait_for_job(self, job_id: int, timeout: float | None = None) -> bool:
        return self._runner is not None and self._runner.wait(job_id, timeout)

    def list_job_status(self, limit: int = 50) -> list[dict]:
        return [self.job_status(job.id) for job in reversed(self.catalog.list_jobs()[-limit:])]

    def job_status(self, job_id: int, *, log_lines: int = 40) -> dict:
        """Browser/CLI-safe view of a job: no manifest, paths, fingerprints, or launch command."""
        job = self.catalog.get_job(job_id)
        counts = {}
        result = None
        if job.kind == "astro":
            counts = {"warnings": [], "lights": 0, "excluded_lights": 0, "darks": 0, "flats": 0, "biases": 0}
            if job.manifest_path != "pending":
                try:
                    manifest = load_manifest(Path(job.manifest_path), self.settings.output_dir)
                except (ValueError, OSError):  # Deleted or tampered job files: the catalog row still has a status.
                    counts["warnings"] = ["Job manifest is unavailable; this job cannot run."]
                else:
                    counts = {"warnings": list(manifest.warnings), "lights": len(manifest.lights.frames),
                              "excluded_lights": len(manifest.excluded_lights), "darks": len(manifest.darks.frames),
                              "flats": len(manifest.flats.frames), "biases": len(manifest.biases.frames)}
        elif job.manifest_path != "pending":
            try:  # Import, tag, and ingest jobs record their counts in the manifest.
                manifest = json.loads(Path(job.manifest_path).read_text(encoding="utf-8"))
            except (ValueError, OSError):
                manifest = None
            manifest = manifest if isinstance(manifest, dict) else {}
            summary = manifest.get("summary")
            if job.kind != "ingest":
                result = summary
            else:
                summary = summary if isinstance(summary, dict) else {}
                counts = {"ingest": {key: summary.get(key, 0) for key in (
                    "copied", "verified", "already_present", "failed", "files", "skipped", "unreadable")} | {
                    "card_verified_everywhere": summary.get("card_verified_everywhere") is True,
                    "manifest": "manifest.json", "snapshot": manifest.get("snapshot")}}
        tail: list[str] = []
        log = Path(job.log_path)
        if job.log_path != "pending" and log.is_file():
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = [line.replace(str(self.settings.library_dir.resolve()), "<library>") for line in lines
                    if not line.startswith("lumen: command ")][-log_lines:]
        if job.kind == "astro" and job.result_path is not None:
            for artifact in self.catalog.list_artifacts():
                if artifact.kind == "astro_tiff" and artifact.settings.get("job_id") == job_id:
                    result = {"artifact_id": artifact.id, "name": Path(artifact.path).name}
        return {"job_id": job.id, "kind": job.kind, "status": job.status, "progress": job.progress,
                "label": job.label, "processed": job.processed, "total": job.total,
                "error": public_message(job.error), "result": result, "log_tail": tail,
                "created_at": job.created_at.isoformat(), "updated_at": job.updated_at.isoformat(), **counts}

    def _record_astro_result(self, context, job_id: int, manifest, execution) -> None:
        """Worker-thread bookkeeping after a verified stack: artifact, provenance, sidecars, job result."""
        output_fingerprint = fingerprint_file(execution.result)
        with self._mutation(), Catalog(self.settings.database_path) as catalog:
            if context.cancelled:
                return
            by_path = {Path(photo.path).resolve(): photo for photo in catalog.list_photos()}
            light_ids = [photo.id for frame in manifest.lights.frames if (photo := by_path.get(Path(frame.source)))]
            settings = {
                "job_id": manifest.job_id, "manifest": "manifest.json", "script_sha256": manifest.script_sha256,
                "siril_version": manifest.siril.version_text, "command": list(manifest.command),
                "warnings": list(manifest.warnings), "lights": len(manifest.lights.frames),
                "excluded_lights": len(manifest.excluded_lights), "darks": len(manifest.darks.frames),
                "flats": len(manifest.flats.frames), "biases": len(manifest.biases.frames),
                "light_fingerprints": [frame.fingerprint for frame in manifest.lights.frames],
                "light_photo_ids": light_ids, "exit_code": execution.returncode,
                "bits_per_sample": list(execution.bits_per_sample),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            stack_identity = hashlib.sha256("\n".join(settings["light_fingerprints"]).encode()).hexdigest()
            artifact = catalog.save_artifact(None, f"astro-job-{manifest.job_id}", stack_identity, "astro_tiff",
                                             str(execution.result), settings, output_fingerprint)
            try:
                details = {"job_id": manifest.job_id, "artifact_id": artifact.id, "output": artifact.path,
                           "output_fingerprint": output_fingerprint, "created_at": settings["created_at"]}
                catalog.append_event(None, "astro_stacked", details | {"light_photo_ids": light_ids})
                for photo_id in light_ids:
                    catalog.append_event(photo_id, "astro_stacked", details)
                for photo_id in light_ids:
                    self._write_sidecar(photo_id, catalog=catalog)
                catalog.update_job(job_id, result_path=str(execution.result))
            except Exception:
                catalog.discard_export(artifact.id, kind="astro_stacked")
                for photo_id in light_ids:  # Sidecars must not keep events the catalog no longer has.
                    try:
                        self._write_sidecar(photo_id, catalog=catalog)
                    except Exception:
                        pass
                raise

    def get_photo(self, photo_id: int) -> dict | None:
        photo = self.catalog.get_photo(photo_id)
        if not photo:
            return None
        metrics = self.catalog.get_metrics(photo_id)
        return {
            "photo": photo,
            "metrics": metrics,
            "events": self.catalog.events_for(photo_id),
            "tags": self.catalog.get_tags(photo_id),
        }

    @_coordinated
    def tag_photo(self, photo_id: int, tagger, *, job_id: int | None = None):
        photo = self.catalog.get_photo(photo_id)
        if not photo or not photo.preview_path:
            raise ValueError(f"photo {photo_id} has no preview")
        try:
            result = tagger.tag(Path(photo.preview_path))
        except Exception as error:
            raw_response = getattr(error, "raw_response", None)
            self.catalog.append_event(
                photo_id,
                "tag_failed",
                {
                    "model": getattr(tagger, "model", "unknown"),
                    "error": str(error),
                    "raw_response": raw_response,
                } | _job_detail(job_id),
            )
            self._write_sidecar(photo_id)
            raise
        payload = {
            "description": result.description,
            "subjects": result.subjects,
            "scene": result.scene,
            "technical": result.technical,
            "confidence": result.confidence,
        }
        self.catalog.save_tags(photo_id, payload, tagger.model)
        self.catalog.append_event(
            photo_id,
            "tagged",
            {"model": tagger.model, "confidence": result.confidence, "raw_response": result.raw_response} | _job_detail(job_id),
        )
        self._write_sidecar(photo_id)
        return result

    @_coordinated
    def set_pick(self, photo_id: int) -> None:
        group_id = self.catalog.set_manual_pick(photo_id)
        if self.catalog.get_photo(photo_id).decision == "reject":  # Choosing a frame as best overrides its rejection.
            self.catalog.set_decision(photo_id, "none", reason="manual_pick")
        self.catalog.append_event(photo_id, "manual_pick", {"group_id": group_id})
        group = next(group for group in self.catalog.list_groups() if group["id"] == group_id)
        for item in group["photos"]:
            self._write_sidecar(int(item["id"]), selected=int(item["id"]) == photo_id)

    def _candidates(self, requested: Path) -> list[Path]:
        if requested.is_file():
            return [requested] if requested.suffix.lower() in SUPPORTED_EXTENSIONS else []
        if not requested.is_dir():
            return []
        library_dir = self.settings.library_dir.resolve()
        return sorted(
            resolved
            for path in requested.rglob("*")
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
            if not (resolved := path.resolve()).is_relative_to(library_dir)
        )

    def _import_one(self, source: Path, fingerprint: str | None = None, *, job_id: int | None = None) -> int:
        fingerprint = fingerprint or fingerprint_file(source)
        previous = self.catalog.photo_by_path(str(source))
        metadata = read_metadata(source)
        captured = datetime.fromisoformat(metadata["captured_at"]) if metadata["captured_at"] else None
        preview_path = self.settings.preview_dir / f"{fingerprint}.jpg"
        preview_existed = preview_path.exists()  # Previews are content-addressed; an existing one is never rewritten.
        self.previews.create_preview(source, preview_path)
        if fingerprint_file(source) != fingerprint:  # Swapped after hashing: metadata and preview may describe other bytes.
            if not preview_existed:
                preview_path.unlink(missing_ok=True)
            raise ValueError("source changed during import")
        photo = Photo(
            path=str(source),
            fingerprint=fingerprint,
            captured_at=captured,
            camera_id=str(metadata["camera_id"]),
            preview_path=str(preview_path),
            format=source.suffix.lstrip(".").upper(),
        )
        if previous and previous.fingerprint != fingerprint:
            self.catalog.clear_tags(previous.id)
            self.catalog.invalidate_crop(previous.id, previous.fingerprint, fingerprint)
        photo_id = self.catalog.upsert_photo(photo)
        if previous and previous.fingerprint != fingerprint:
            self.catalog.append_event(
                photo_id,
                "source_replaced",
                {"previous_fingerprint": previous.fingerprint, "source_fingerprint": fingerprint},
            )
            for old_sidecar in self.sidecars.paths(previous):
                if old_sidecar.parent == self.settings.sidecar_dir and not old_sidecar.parent.is_symlink():
                    old_sidecar.unlink(missing_ok=True)
        metrics = measure_image(preview_path)
        metrics.astro = analyze_astro(preview_path)
        if vision := analyze_faces(preview_path, self.settings.vision_analyzer_path):
            apply_vision_result(metrics, vision)
        self.catalog.save_metrics(photo_id, metrics)
        self.catalog.append_event(
            photo_id,
            "imported",
            {
                "source_fingerprint": fingerprint,
                "preview": str(preview_path),
                "analyzer": "Pillow",
                "vision": "available" if vision else "unavailable",
            } | _job_detail(job_id),
        )
        return photo_id

    def _regroup_and_write_sidecars(self) -> None:
        photos = self.catalog.list_photos()
        groups = TimeGapGroupingPolicy(self.settings.burst_gap_seconds).group(photos)
        self.catalog.clear_group_assignments()
        selected_ids: set[int] = set()
        for group in groups:
            scored_input = []
            for photo in group:
                if photo.id is None:
                    continue
                metrics = self.catalog.get_metrics(photo.id)
                if metrics:
                    scored_input.append((photo, metrics))
            if not scored_input:
                continue
            ranking = rank_group(scored_input)
            selected = _select(ranking)
            selected_ids.add(int(selected.photo.id))
            first = group[0]
            stable_key = hashlib.sha256(f"{first.camera_id}|{first.path}".encode()).hexdigest()[:24]
            self.catalog.assign_group(
                stable_key,
                first.camera_id,
                first.captured_at.isoformat() if first.captured_at else None,
                [int(item.photo.id) for item in ranking],
                int(selected.photo.id),
                {int(item.photo.id): item.total for item in ranking},
            )

        for photo in self.catalog.list_photos():
            if photo.id is not None:
                self._write_sidecar(photo.id, selected=photo.id in selected_ids)

    def _write_sidecar(self, photo_id: int, selected: bool | None = None, *, catalog: Catalog | None = None) -> None:
        catalog = catalog or self.catalog  # Worker threads pass their own connection.
        photo = catalog.get_photo(photo_id)
        metrics = catalog.get_metrics(photo_id)
        if not photo or not metrics:
            return
        if selected is None:
            selected = any(group["automatic_pick_id"] == photo_id for group in catalog.list_groups())
        tags = catalog.get_tags(photo_id)
        data = tags["data"] if tags else {}
        self.sidecars.write(
            photo,
            metrics,
            catalog.events_for(photo_id),
            selected=selected,
            description=data.get("description", ""),
            keywords=data.get("subjects", []),
            artifacts=[artifact for artifact in catalog.list_artifacts() if artifact.photo_id == photo_id],
            active_crop=catalog.get_crop(photo_id),
            decision=photo.decision,
            rating=photo.rating,
        )
