from __future__ import annotations

import hashlib
import fcntl
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from PIL import Image

from .analysis import analyze_astro, analyze_faces, analyze_subject_regions, apply_vision_result, measure_image
from .catalog import Catalog
from .config import Settings
from .composition import CropCandidate, generate_candidates
from .domain import Artifact, Photo, PhotoEdit
from .pipelines import PipelineRegistry
from .grouping import TimeGapGroupingPolicy
from .previews import ExifToolPreviewProvider, PreviewError, fingerprint_file, read_image_dimensions, read_metadata
from .scoring import rank_group
from .sidecars import SidecarWriter


SUPPORTED_EXTENSIONS = {".arw", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic"}

_MUTATION_LOCKS = {}
_MUTATION_GUARD = threading.Lock()
_MUTATION_HELD = threading.local()


def _coordinated(method):
    @wraps(method)
    def coordinated(self, *args, **kwargs):
        with self._mutation():
            return method(self, *args, **kwargs)
    return coordinated


@dataclass(slots=True)
class ImportSummary:
    discovered: int = 0
    imported: int = 0
    updated: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


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
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.ensure_directories()
        self.catalog = Catalog(settings.database_path)
        self.previews = ExifToolPreviewProvider()
        self.sidecars = SidecarWriter(settings.sidecar_dir)

    def close(self) -> None:
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
            existed = self.catalog.photo_by_path(str(source)) is not None
            try:
                self._import_one(source)
                if existed:
                    summary.updated += 1
                else:
                    summary.imported += 1
            except (OSError, PreviewError, ValueError, subprocess.SubprocessError) as error:
                summary.failed += 1
                summary.errors.append(f"{source.name}: {error}")
                failed_photo = self.catalog.photo_by_path(str(source))
                details = {"source": str(source), "error": str(error)}
                try:
                    details["source_fingerprint"] = fingerprint_file(source)
                except OSError:
                    pass
                self.catalog.append_event(failed_photo.id if failed_photo else None, "import_failed", details)
        self._regroup_and_write_sidecars()
        return summary

    def list_groups(self) -> list[dict]:
        return self.catalog.list_groups()

    def photo_count(self) -> int:
        return self.catalog.photo_count()

    def keeper_ids(self) -> list[int]:
        return [int(group["automatic_pick_id"]) for group in self.list_groups() if group["automatic_pick_id"] is not None]

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
        keepers = set(self.keeper_ids())
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
                    rating=5 if photo.id in keepers else None,
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
    def tag_photo(self, photo_id: int, tagger):
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
                },
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
            {"model": tagger.model, "confidence": result.confidence, "raw_response": result.raw_response},
        )
        self._write_sidecar(photo_id)
        return result

    @_coordinated
    def set_pick(self, photo_id: int) -> None:
        group_id = self.catalog.set_manual_pick(photo_id)
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

    def _import_one(self, source: Path) -> int:
        fingerprint = fingerprint_file(source)
        previous = self.catalog.photo_by_path(str(source))
        metadata = read_metadata(source)
        captured = datetime.fromisoformat(metadata["captured_at"]) if metadata["captured_at"] else None
        preview_path = self.settings.preview_dir / f"{fingerprint}.jpg"
        self.previews.create_preview(source, preview_path)
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
            },
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
            manual = next((item for item in ranking if item.photo.manual_pick), None)
            selected = manual or ranking[0]
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

    def _write_sidecar(self, photo_id: int, selected: bool | None = None) -> None:
        photo = self.catalog.get_photo(photo_id)
        metrics = self.catalog.get_metrics(photo_id)
        if not photo or not metrics:
            return
        if selected is None:
            selected = any(group["automatic_pick_id"] == photo_id for group in self.catalog.list_groups())
        tags = self.catalog.get_tags(photo_id)
        data = tags["data"] if tags else {}
        self.sidecars.write(
            photo,
            metrics,
            self.catalog.events_for(photo_id),
            selected=selected,
            description=data.get("description", ""),
            keywords=data.get("subjects", []),
            artifacts=[artifact for artifact in self.catalog.list_artifacts() if artifact.photo_id == photo_id],
            active_crop=self.catalog.get_crop(photo_id),
        )
