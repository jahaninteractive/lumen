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
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from functools import wraps
from pathlib import Path

from PIL import Image

from .analysis import analyze_astro, analyze_faces, analyze_subject_regions, apply_vision_result, difference_hash, measure_image
from .catalog import Catalog, GroupFilters
from .config import Settings
from .composition import CropCandidate, generate_candidates
from .digest import day_facts, render_facts_table
from .domain import (MANUAL_ASPECTS, MANUAL_CROP_ID, SUPPORTED_EXTENSIONS, Artifact, CropRect, DevelopSettings, Job,
                     Photo, PhotoEdit)
from .geonames import Place, PlaceIndex, distance_km, online_lookup, parse_position
from . import ingest
from .astro import execute_job, load_manifest, prepare_siril_job, probe_siril
from .jobs import LocalJobRunner
from .omlx import (ALT_TEXT_MAX, CAPTION_MAX, ROLES, BurstResponseError, DevelopAdvice, OmlxError,
                   OmlxTagger, get_server_status)
from .pipelines import EXPORT_PRESETS, RAW_EXTENSIONS, PipelineRegistry, validate_watermark
from .tools import find_tool
from .grouping import TimeGapGroupingPolicy, split_near_duplicates
from .rawdevelop import DevelopError, describe_histogram, render_raw
from .previews import (ExifToolPreviewProvider, PreviewError, fingerprint_file, prune_cache, prune_thumbnails, read_image_dimensions,
                       read_metadata)
from .scoring import RankedPhoto, rank_group
from .sidecars import SidecarWriter
from .vocabulary import forget_keyword as forget_vocabulary_keyword, learn_keywords, load_blocked, load_vocabulary, match_keywords


DEVELOP_ADVICE_LONG_EDGE = 1024  # all the vision model needs; it is not judging single pixels
NEARBY_PLACE_KM = 5.0  # fixes this close share one name: a drifting boat's bursts are all the same spot
NEARBY_PLACE_RANGE = (1.0, 20.0)  # the photographer's radius; the slider's own bounds

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


def _develop_fields(develop: DevelopSettings | None) -> dict | None:
    """The settings to render with, or None for an undeveloped frame. Empty settings mean undeveloped."""
    return None if develop is None or develop.is_empty else develop.to_dict()


def _job_detail(job_id: int | None) -> dict:
    """Provenance link to the background job that did the work; synchronous calls record none."""
    return {} if job_id is None else {"job_id": job_id}


def _check_caption(caption: str | None, alt_text: str | None) -> None:
    """Photographer edits share the model limits; None leaves a field unchanged and "" clears it."""
    if caption and (len(caption) > CAPTION_MAX or "#" in caption):
        raise ValueError(f"caption must contain at most {CAPTION_MAX} characters and no hashtags")
    if alt_text and len(alt_text) > ALT_TEXT_MAX:
        raise ValueError(f"alt text must contain at most {ALT_TEXT_MAX} characters")


def _stable_key(first: Photo) -> str:
    return hashlib.sha256(f"{first.camera_id}|{first.path}".encode()).hexdigest()[:24]


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
    previously_cleared: int = 0


@dataclass(frozen=True, slots=True)
class ExportRequest:
    photo_ids: tuple[int, ...]
    profile: Path | None = None
    preset: str = "full"
    watermark: str | None = None
    keep_original: bool = False  # Also export the undeveloped version of every developed frame.
    dated_folder: bool = True  # File each frame under <library>/outputs/jpeg/Oct-18-2026/, its capture date.


def _export_folder(photo: Photo, request: ExportRequest) -> str | None:
    """The subfolder for one frame, named for the day it was taken (the stored camera-local date, as
    the digests read it). A frame the camera dated nowhere still needs a folder."""
    if not request.dated_folder:
        return None
    return photo.captured_at.strftime("%b-%d-%Y") if photo.captured_at else "Unknown-Date"


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
        self._places: PlaceIndex | None = None

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
            if self._places is not None:
                self._places.close()
            self.catalog.close()

    @property
    def places(self) -> PlaceIndex | None:
        """The local GeoNames index, opened on first use; None when no GeoNames folder is configured."""
        directory = self.settings.geonames_dir
        if self._places is None and directory is not None and directory.is_dir():
            self._places = PlaceIndex(directory, self.settings.geonames_index_path)
        return self._places

    def _resolve_place(self, photo_id: int, latitude: float | None, longitude: float | None) -> Place | None:
        """Store the nearest named place for a GPS fix (or clear it for a frame without one); no index is a no-op."""
        if self.places is None:
            return None
        place = self.places.lookup(latitude, longitude) if latitude is not None and longitude is not None else None
        if self.settings.geocode_online and latitude is not None and longitude is not None and (place is None or place.near):
            online = online_lookup(latitude, longitude)
            if online is not None:
                self.places.add(online.name, online.country, latitude, longitude)  # cached: nearby frames resolve locally
                place = online
        self.catalog.update_place(photo_id, place=place.name if place else None, country=place.country if place else None)
        if place:
            self.catalog.append_event(photo_id, "place_resolved", {"name": place.name, "country": place.country,
                                                                   "distance_km": round(place.distance_km, 1), "near": place.near})
        return place

    @_coordinated
    def set_place(self, photo_id: int, name: str, country: str, *, scope: str, remember: bool,
                  radius_km: float = NEARBY_PLACE_KM) -> dict:
        """Photographer's place for a frame, its burst, every frame within radius_km, or (`day`) every frame shot the
        same day with neither a GPS fix nor a place; remember adds it to the index."""
        name, country = name.strip(), country.strip() or None
        if scope not in ("frame", "burst", "nearby", "day"):
            raise ValueError("scope must be frame, burst, nearby or day")
        if not 1 <= len(name) <= 80 or len(country or "") > 60:
            raise ValueError("place name must be 1-80 characters and country at most 60")
        low, high = NEARBY_PLACE_RANGE
        if not low <= radius_km <= high:
            raise ValueError(f"radius must be {low:g}-{high:g} km")
        photo = self.catalog.get_photo(photo_id)
        if not photo:
            raise ValueError(f"unknown photo ID: {photo_id}")
        targets = [photo_id]
        if scope == "burst" and photo.group_id is not None:
            targets = next(([int(p["id"]) for p in group["photos"]] for group in self.catalog.list_groups()
                            if group["id"] == photo.group_id), targets)
        elif scope == "nearby":
            if photo.latitude is None or photo.longitude is None:
                raise ValueError("scope nearby needs a GPS fix on this frame")
            # ponytail: linear scan of the catalog; add an rtree over photos if a six-figure library drags
            targets = [other.id for other in self.catalog.list_photos()
                       if other.latitude is not None and other.longitude is not None
                       and distance_km(photo.latitude, photo.longitude, other.latitude, other.longitude) <= radius_km]
        elif scope == "day":
            if photo.captured_at is None:
                raise ValueError("scope day needs a capture date on this frame")
            day = photo.captured_at.date()  # the stored camera-local date, as digests and the rail count days
            targets = [photo_id] + [other.id for other in self.catalog.list_photos()
                                    if other.id != photo_id and other.captured_at and other.captured_at.date() == day
                                    and other.latitude is None and not other.place]
        for target in targets:
            self._drop_place_keywords(target, {name.casefold(), (country or "").casefold()})
            self.catalog.update_place(target, place=name, country=country)
            self.catalog.append_event(target, "place_edited", {"name": name, "country": country, "scope": scope, "remember": remember}
                                      | ({"radius_km": radius_km} if scope == "nearby" else {}))
            self._write_sidecar(target)
        remembered = remember and self.places is not None and photo.latitude is not None and photo.longitude is not None
        if remembered:
            self.places.add(name, country or "", photo.latitude, photo.longitude, source="manual", feature_code="MANUAL")
        return {"photo_ids": targets, "name": name, "country": country, "remembered": remembered}

    @_coordinated
    def set_location(self, photo_id: int, position: str, *, scope: str) -> dict:
        """A position the photographer typed (a ship's display, a map) for a frame without a camera fix, its burst, or
        (`day`) every frame shot the same day. In scope it fills frames with no fix and replaces this frame's own typed
        position wherever that was typed; a camera fix, another typed spot and a typed place name are kept."""
        if scope not in ("frame", "burst", "day"):
            raise ValueError("scope must be frame, burst or day")
        latitude, longitude = parse_position(position)
        photo = self.catalog.get_photo(photo_id)
        if not photo:
            raise ValueError(f"unknown photo ID: {photo_id}")
        typed = self.location_typed(photo)
        if photo.latitude is not None and not typed:
            raise ValueError("this frame already has a GPS fix from the camera")
        old = (photo.latitude, photo.longitude) if typed else None
        candidates = [photo]
        if scope == "burst" and photo.group_id is not None:
            ids = set(self.catalog.group_photo_ids(photo.group_id))
            candidates = [other for other in self.catalog.list_photos() if other.id in ids]
        elif scope == "day" and photo.captured_at is not None:
            day = photo.captured_at.date()
            candidates = [other for other in self.catalog.list_photos() if other.captured_at and other.captured_at.date() == day]
        targets = [other for other in candidates if other.latitude is None
                   or (old is not None and (other.latitude, other.longitude) == old and self.location_typed(other))]
        for target in targets:
            self.catalog.update_location(target.id, latitude=latitude, longitude=longitude, altitude=None)
            self.catalog.append_event(target.id, "location_set", {"latitude": latitude, "longitude": longitude, "scope": scope})
            if not target.place or self._last_event(target.id, ("place_resolved", "place_edited")) == "place_resolved":
                self._resolve_place(target.id, latitude, longitude)
            self._write_sidecar(target.id)
        named = self.catalog.get_photo(photo_id)
        return {"photo_ids": [target.id for target in targets], "latitude": latitude, "longitude": longitude,
                "place": named.place, "country": named.place_country, "location_typed": True}

    def location_typed(self, photo: Photo) -> bool:
        """The frame's fix is the last position the photographer typed, not the camera's (a re-import that changed
        the file's fix leaves the old event behind, which no longer matches)."""
        if photo.latitude is None:
            return False
        last = next((event for event in reversed(self.catalog.events_for(photo.id)) if event.kind == "location_set"), None)
        return last is not None and (last.details["latitude"], last.details["longitude"]) == (photo.latitude, photo.longitude)

    def _last_event(self, photo_id: int, kinds: tuple[str, ...]) -> str | None:
        return next((event.kind for event in reversed(self.catalog.events_for(photo_id)) if event.kind in kinds), None)

    def _drop_place_keywords(self, photo_id: int, keep: set[str]) -> None:
        """Tagging applied the old place as keywords; drop them unless the photographer added them or they still hold."""
        old, tags = self.catalog.get_photo(photo_id), self.catalog.get_tags(photo_id)
        if not tags:
            return
        data = tags["data"]
        stale = {word.casefold() for word in (old.place, old.place_country) if word} - keep
        stale -= {word.casefold() for word in data.get("photographer", [])}
        subjects = [word for word in data.get("subjects", []) if word.casefold() not in stale]
        if subjects != data.get("subjects", []):
            self.catalog.save_tags(photo_id, data | {"subjects": subjects}, tags["model"])

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
    def import_path(self, requested: Path, *, include_cleared: bool = False) -> ImportSummary:
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
        cleared = set() if include_cleared else self.catalog.cleared_fingerprints()
        for source in candidates:
            self._import_candidate(source, summary, cleared=cleared)
        self._regroup_and_write_sidecars()
        return summary

    def _import_candidate(self, source: Path, summary: ImportSummary, *, skip_unchanged: bool = False,
                          job_id: int | None = None, cleared: set[str] | frozenset[str] = frozenset()) -> None:
        """Import one candidate; `cleared` holds ledger fingerprints to skip for frames not already at this path."""
        previous = self.catalog.photo_by_path(str(source))
        try:
            fingerprint = fingerprint_file(source)
            if (skip_unchanged and previous and previous.fingerprint == fingerprint
                    and previous.preview_path and Path(previous.preview_path).is_file()):
                latitude, longitude = previous.latitude, previous.longitude
                if previous.latitude is None:  # ponytail: one exiftool call per unchanged file without a fix, every import
                    fix = read_metadata(source)
                    if fix["latitude"] is not None:
                        self.catalog.update_location(previous.id, latitude=fix["latitude"], longitude=fix["longitude"], altitude=fix["altitude"])
                        latitude, longitude = fix["latitude"], fix["longitude"]
                if previous.place is None and latitude is not None:  # ponytail: placeless fixes (open sea) re-look-up every import
                    self._resolve_place(previous.id, latitude, longitude)
                metrics = self.catalog.get_metrics(previous.id)
                if metrics and metrics.dhash is None:  # ponytail: one preview decode per unhashed photo, every import, until hashed
                    metrics.dhash = difference_hash(Path(previous.preview_path))
                    self.catalog.save_metrics(previous.id, metrics)  # total_score is kept by save_metrics.
                summary.skipped += 1
                return
            if previous is None and fingerprint in cleared:
                summary.previously_cleared += 1
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

    def start_import_job(self, requested: Path, *, include_cleared: bool = False) -> Job:
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
                cleared = set() if include_cleared else worker.catalog.cleared_fingerprints()
                for index, source in enumerate(candidates, 1):
                    if context.cancelled:
                        break
                    with worker._mutation():
                        worker._import_candidate(source, summary, skip_unchanged=True, job_id=job.id, cleared=cleared)
                    for error in summary.errors:
                        worker._log_job(job, error)
                    summary.errors.clear()
                    context.update_progress(index / len(candidates), label=source.name[:200], processed=index, total=len(candidates))
                with worker._mutation():
                    worker._regroup_and_write_sidecars()
                counts = {key: getattr(summary, key) for key in (
                    "discovered", "imported", "updated", "skipped", "failed", "previously_cleared")}
                worker._write_job_files(job.id, {"kind": "import", "path": str(requested), "summary": counts})

        self._submit_job(runner, job, work)
        return job

    def tagger_for(self, role: str) -> OmlxTagger:
        """A tagger for a model role, only when oMLX reports that role's model loaded right now."""
        if role not in ROLES:
            raise ValueError(f"unknown model role: {role}")
        model = getattr(self.settings, ROLES[role])
        try:
            loaded = get_server_status(self.settings.omlx_url)["loaded_models"]
        except (OmlxError, ValueError):
            raise RuntimeError(f"{role} model {model} is not loaded in oMLX; oMLX is offline") from None
        if model not in loaded:
            raise RuntimeError(f"{role} model {model} is not loaded in oMLX")
        return OmlxTagger(self.settings.omlx_url, model, role=role)

    def start_tag_job(self, tagger, *, scope: str | list[int] = "keepers", retag: bool = False) -> Job:
        if type(retag) is not bool:
            raise ValueError("retag must be true or false")

        def per_photo(worker, photo, job) -> tuple[str, ...]:
            existing = worker.catalog.get_tags(photo.id)
            if not retag and existing is not None and "description" in existing["data"]:  # A reject-only row is untagged.
                return ("skipped",)
            worker.tag_photo(photo.id, tagger, job_id=job.id)  # A model failure appends tag_failed itself.
            if photo.exposure_seconds is not None and photo.exposure_seconds >= 1.0:
                try:  # A long exposure is also triaged; a triage failure never fails the tag.
                    worker._triage_one(photo, tagger, job_id=job.id)
                except Exception as error:
                    worker._log_job(job, f"{Path(photo.path).name}: triage {type(error).__name__}: {error}")
                    return ("tagged", "triage_failed")
            return ("tagged",)

        return self._run_photo_job("tag", tagger, scope, per_photo, counters=("tagged", "skipped", "failed"),
                                   manifest={"retag": retag})

    def start_reject_review_job(self, tagger, *, scope: str | list[int] = "keepers") -> Job:
        """Advisory: stores problems and a reject suggestion per photo; never changes a decision."""
        def per_photo(worker, photo, job) -> tuple[str, ...]:
            existing = worker.catalog.get_tags(photo.id)
            if existing is not None and "problems" in existing["data"]:
                return ("skipped",)
            review = worker._review_rejects_one(photo.id, tagger, job_id=job.id)
            return ("reviewed", "suggested") if review.suggest_reject else ("reviewed",)

        return self._run_photo_job("review_rejects", tagger, scope, per_photo,
                                   counters=("reviewed", "skipped", "failed", "suggested"))

    def start_assist_job(self, fast, review, *, scope: str | list[int] = "keepers", redo: bool = False) -> Job:
        """One job per photo: tag (fast), look for problems (fast), caption (review); work already done is skipped
        unless `redo`, which repeats every model step. A failing step is counted and logged, and the next step still
        runs; the photographer's own caption is never replaced, redo or not."""
        def per_photo(worker, photo, job) -> tuple[str, ...]:
            names: list[str] = []
            data = (worker.catalog.get_tags(photo.id) or {"data": {}})["data"]
            own_caption = bool(data.get("caption")) and data.get("caption_author") == "photographer"
            steps = (
                ("tagged", "description" in data and not redo, lambda: worker.tag_photo(photo.id, fast, job_id=job.id)),
                ("reviewed", "problems" in data and not redo, lambda: worker._review_rejects_one(photo.id, fast, job_id=job.id)),
                ("captioned", own_caption or (bool(data.get("caption")) and not redo), lambda: worker.caption_photo(photo.id, review)),
            )
            for name, done, step in steps:
                if done:
                    continue
                try:
                    result = step()
                except (OmlxError, ValueError, OSError) as error:  # the step recorded its own *_failed event
                    names.append("failed")
                    worker._log_job(job, f"{Path(photo.path).name}: {name} {type(error).__name__}: {error}")
                    continue
                names.append(name)
                if name == "reviewed" and result.suggest_reject:
                    names.append("suggested")
            return tuple(names) or ("skipped",)

        return self._run_photo_job("assist", fast, scope, per_photo,
                                   counters=("tagged", "reviewed", "captioned", "skipped", "failed", "suggested"),
                                   manifest={"review_model": review.model, "redo": redo})

    def start_triage_job(self, tagger, *, group_id: int) -> Job:
        """Advisory sky triage of every frame in one group, whatever its exposure; exclusions never change."""
        group = self._group(group_id)
        if group is None:
            raise ValueError(f"unknown group ID: {group_id}")

        def per_photo(worker, photo, job) -> tuple[str, ...]:
            worker._triage_one(photo, tagger, job_id=job.id)
            return ("triaged",)

        return self._run_photo_job("triage", tagger, [photo["id"] for photo in group["photos"]], per_photo,
                                   counters=("triaged", "failed"), manifest={"group_id": group_id})

    def _run_photo_job(self, kind: str, tagger, scope, per_photo, *, counters: tuple[str, ...],
                       manifest: dict | None = None) -> Job:
        """One worker loop for per-photo model jobs; per_photo returns names from counters to increment ("failed" required)."""
        if scope == "keepers":
            ids = self.keeper_ids()
        elif scope == "all":
            ids = None
        elif (isinstance(scope, list) and scope and all(type(item) is int for item in scope)
              and len(set(scope)) == len(scope)):
            for photo_id in scope:
                if self.catalog.get_photo(photo_id) is None:
                    raise ValueError(f"unknown photo ID: {photo_id}")
            ids = list(scope)
        else:
            raise ValueError("scope must be keepers, all, or a non-empty list of distinct photo IDs")
        manifest = {"kind": kind, "scope": scope} | (manifest or {}) | {
            "role": getattr(tagger, "role", None), "model": tagger.model}
        runner = self.job_runner
        job = self.catalog.create_prepared_job(kind, lambda job_id: self._write_job_files(job_id, manifest))

        def work(context) -> None:
            with Library(self.settings, job_runner=runner) as worker:
                photos = worker.catalog.list_photos() if ids is None else [
                    photo for photo_id in ids if (photo := worker.catalog.get_photo(photo_id))]
                counts = dict.fromkeys(counters, 0)
                failed_photo_ids = []
                for index, photo in enumerate(photos, 1):
                    if context.cancelled:
                        break
                    with worker._mutation():
                        try:
                            for name in per_photo(worker, photo, job):  # names outside counters appear once used
                                counts[name] = counts.get(name, 0) + 1
                        except (OmlxError, ValueError, OSError) as error:
                            counts["failed"] += 1
                            failed_photo_ids.append(photo.id)
                            worker._log_job(job, f"{Path(photo.path).name}: {type(error).__name__}: {error}")
                    context.update_progress(index / len(photos), label=Path(photo.path).name[:200],
                                            processed=index, total=len(photos))
                with worker._mutation():
                    worker._regroup_and_write_sidecars()
                worker._write_job_files(job.id, manifest | {"failed_photo_ids": failed_photo_ids, "summary": counts})

        self._submit_job(runner, job, work)
        return job

    def _review_rejects_one(self, photo_id: int, tagger, *, job_id: int | None = None):
        photo = self.catalog.get_photo(photo_id)
        if not photo or not photo.preview_path:
            raise ValueError(f"photo {photo_id} has no preview")
        role = getattr(tagger, "role", None)
        try:
            review = tagger.review_rejects(Path(photo.preview_path))
        except Exception as error:
            self.catalog.append_event(photo_id, "reject_review_failed", {
                "model": getattr(tagger, "model", "unknown"), "role": role, "error": str(error),
                "raw_response": getattr(error, "raw_response", None)} | _job_detail(job_id))
            self._write_sidecar(photo_id)
            raise
        existing = self.catalog.get_tags(photo_id)
        problems = {"items": review.problems, "kinds": [item["kind"] for item in review.problems],
                    "suggest_reject": review.suggest_reject, "confidence": review.confidence,
                    "reviewed_at": datetime.now(timezone.utc).isoformat()}
        self.catalog.save_tags(photo_id, (existing["data"] if existing else {}) | {"problems": problems},
                               existing["model"] if existing else tagger.model)
        self.catalog.append_event(photo_id, "reject_suggested", {
            "model": tagger.model, "role": role, "problems": review.problems, "suggest_reject": review.suggest_reject,
            "confidence": review.confidence, "raw_response": review.raw_response} | _job_detail(job_id))
        self._write_sidecar(photo_id)
        return review

    def _triage_one(self, photo: Photo, tagger, *, job_id: int | None = None):
        if not photo.preview_path:
            raise ValueError(f"photo {photo.id} has no preview")
        role = getattr(tagger, "role", None)
        try:
            triage = tagger.triage_astro(Path(photo.preview_path))
        except Exception as error:
            self.catalog.append_event(photo.id, "astro_triage_failed", {
                "model": getattr(tagger, "model", "unknown"), "role": role, "error": str(error),
                "raw_response": getattr(error, "raw_response", None)} | _job_detail(job_id))
            self._write_sidecar(photo.id)
            raise
        existing = self.catalog.get_tags(photo.id)
        self.catalog.save_tags(photo.id, (existing["data"] if existing else {}) | {
            "sky": {"class": triage.sky, "confidence": triage.confidence}, "issues": list(triage.issues)},
            existing["model"] if existing else tagger.model)
        self.catalog.append_event(photo.id, "astro_triaged", {
            "model": tagger.model, "role": role, "sky": triage.sky, "confidence": triage.confidence,
            "issues": list(triage.issues), "raw_response": triage.raw_response} | _job_detail(job_id))
        self._write_sidecar(photo.id)
        return triage

    def astro_cue(self, photo_ids: Sequence[int]) -> bool:
        """True when stored triage says more than half these frames are night sky, aurora, or Milky Way."""
        skies = [((self.catalog.get_tags(photo_id) or {"data": {}})["data"].get("sky") or {}).get("class")
                 for photo_id in photo_ids]  # ponytail: per-photo tag reads; batch with json_extract if the sheet slows
        return sum(sky in {"night_sky", "aurora", "milky_way"} for sky in skies) * 2 > len(skies)

    def astro_triage(self, group_id: int) -> dict:
        """Per-light sky triage plus metric warnings for the Astro tab; advisory, without raw model output."""
        group = self._group(group_id)
        if group is None:
            raise ValueError(f"unknown group ID: {group_id}")
        lights = []
        for photo in group["photos"]:
            data = (self.catalog.get_tags(photo["id"]) or {"data": {}})["data"]
            astro = json.loads(photo["astro_json"]) if photo["astro_json"] else {}
            sky, issues = data.get("sky"), data.get("issues", [])
            trailing = bool(astro.get("trailing_warning"))
            lights.append({"photo_id": photo["id"], "name": Path(photo["path"]).name, "sky": sky, "issues": issues,
                           "confidence": sky["confidence"] if sky else None, "star_count": astro.get("star_count"),
                           "trailing_warning": trailing,
                           "flagged": (sky or {}).get("class") in {"cloud", "not_sky"}
                           or bool({"trailing", "cloud_band"} & set(issues)) or trailing})
        return {"group_id": group_id, "lights": lights, "astro_cue": self.astro_cue([p["id"] for p in group["photos"]])}

    def reject_suggestions(self) -> list[dict]:
        """Advisory reject suggestions still awaiting the photographer; web turns fingerprint16 into preview_url."""
        rows = self.catalog.connection.execute("""
            SELECT photos.id, photos.path, photos.group_id, photos.fingerprint, photos.preview_kind,
                   photos.preview_path, tags.data_json, photo_edits.selected_at
            FROM tags JOIN photos ON photos.id = tags.photo_id LEFT JOIN photo_edits ON photo_edits.photo_id = photos.id
            WHERE json_extract(tags.data_json, '$.problems.suggest_reject') = 1
              AND json_extract(tags.data_json, '$.problems.dismissed_at') IS NULL
              AND photos.decision != 'reject'
            ORDER BY json_extract(tags.data_json, '$.problems.confidence') DESC, photos.id
        """).fetchall()
        result = []
        for row in rows:
            problems = json.loads(row["data_json"])["problems"]
            result.append({"photo_id": row["id"], "name": Path(row["path"]).name, "group_id": row["group_id"],
                           "problems": problems["items"], "confidence": problems["confidence"],
                           "fingerprint16": row["fingerprint"][:16], "preview_kind": row["preview_kind"],
                           "preview_path": row["preview_path"], "crop_selected_at": row["selected_at"]})
        return result

    @_coordinated
    def apply_reject_suggestions(self, photo_ids: list[int], decision: str) -> dict:
        """Apply the photographer's choice to exactly these ids; any id no longer suggested changes nothing."""
        if decision not in {"reject", "keep"}:
            raise ValueError("decision must be reject or keep")
        if not isinstance(photo_ids, list) or not photo_ids or len(set(photo_ids)) != len(photo_ids):
            raise ValueError("photo_ids must be a non-empty list of distinct photo IDs")
        current = {item["photo_id"] for item in self.reject_suggestions()}
        for photo_id in photo_ids:
            if photo_id not in current:
                raise ValueError(f"photo {photo_id} is not a current reject suggestion")
        decided = []
        for photo_id in photo_ids:
            if decision == "reject":
                decided.append(self.set_decision(photo_id, "reject"))
            else:
                tags = self.catalog.get_tags(photo_id)
                problems = tags["data"]["problems"] | {"dismissed_at": datetime.now(timezone.utc).isoformat()}
                self.catalog.save_tags(photo_id, tags["data"] | {"problems": problems}, tags["model"])
                decided.append(self._decided(self.catalog.get_photo(photo_id)))  # unchanged decision, same shape
            self.catalog.append_event(photo_id, "reject_suggestion_decided", {"decision": decision})
        return {"decided": decided, "remaining": len(self.reject_suggestions())}

    def start_refine_job(self, *, group_id: int | None = None, filters: GroupFilters | None = None,
                         all_photos: bool = False) -> Job:
        """Replace embedded previews with darktable renders, re-measure, and re-rank; never touches picks, decisions, or ratings."""
        if type(all_photos) is not bool or (group_id is not None) + (filters is not None) + all_photos != 1:
            raise ValueError("refine requires exactly one of a group, filters, or all photos")
        darktable = find_tool("darktable-cli")
        if not darktable:
            raise RuntimeError("darktable-cli is not installed")
        if group_id is not None:
            if type(group_id) is not int or not (photo_ids := self.catalog.group_photo_ids(group_id)):
                raise ValueError(f"unknown group ID: {group_id}")
            scope = {"scope": "group", "group_id": group_id}
        elif filters is not None:
            if not isinstance(filters, GroupFilters):
                raise ValueError("filters must be GroupFilters")
            photo_ids, scope = self.catalog.photos_for_filters(filters), {"scope": "filtered", "filters": asdict(filters)}
        else:
            photo_ids, scope = [photo.id for photo in self.catalog.list_photos()], {"scope": "all"}
        manifest = {"kind": "refine_previews", **scope}
        runner = self.job_runner
        job = self.catalog.create_prepared_job("refine_previews", lambda job_id: self._write_job_files(job_id, manifest))

        def work(context) -> None:
            with Library(self.settings, job_runner=runner) as worker:
                counts = {"refined": 0, "skipped": 0, "failed": 0}
                refined = []
                for index, photo_id in enumerate(photo_ids, 1):
                    if context.cancelled:
                        break
                    with worker._mutation():
                        if photo := worker.catalog.get_photo(photo_id):  # Removed since the job started: nothing to do.
                            outcome = worker._refine_one(photo, darktable, job)
                            counts[outcome] += 1
                            if outcome == "refined":
                                refined.append(photo)
                    context.update_progress(index / len(photo_ids), label=Path(photo.path).name[:200] if photo else None,
                                            processed=index, total=len(photo_ids))
                with worker._mutation():
                    for affected in sorted({photo.group_id for photo in refined if photo.group_id is not None}):
                        worker._rerank_group(affected)  # Rewrites the group's sidecars too.
                    for photo in refined:
                        if photo.group_id is None:
                            worker._write_sidecar(photo.id)
                    worker._log_job(job, f"thumbnails pruned: {worker._prune_thumbnails()}")
                worker._write_job_files(job.id, manifest | {"summary": counts})

        self._submit_job(runner, job, work)
        return job

    def _refine_one(self, photo: Photo, darktable: str, job: Job) -> str:
        """Render, measure, and record one refined preview; returns refined, skipped, or failed. Logs basenames only."""
        source = Path(photo.path)
        destination = self.settings.preview_dir / f"{photo.fingerprint}-2048.jpg"

        def unchanged() -> bool:
            try:
                return fingerprint_file(source) == photo.fingerprint
            except OSError:
                return False

        if photo.preview_kind == "developed":
            self._log_job(job, f"{source.name}: developed by hand; refining would discard the develop settings")
            return "skipped"
        if not unchanged():
            self._log_job(job, f"{source.name}: source changed or is unavailable since import; reimport before refining")
            return "skipped"
        try:
            self.previews.create_refined_preview(source, destination, darktable=darktable)
            if not unchanged():  # Swapped during the render: the preview may show other bytes.
                raise PreviewError("source changed during refinement; reimport before refining")
            metrics = measure_image(destination)
            metrics.astro = analyze_astro(destination)
            if vision := analyze_faces(destination, self.settings.vision_analyzer_path):
                apply_vision_result(metrics, vision)
        except (OSError, PreviewError, ValueError, subprocess.SubprocessError) as error:
            if not self.catalog.preview_references(str(destination)):  # Never a render another photo shows.
                destination.unlink(missing_ok=True)
            self._log_job(job, f"{source.name}: {error}")
            return "failed"
        previous = Path(photo.preview_path) if photo.preview_path else None
        self.catalog.update_preview(photo.id, str(destination), "refined")
        self.catalog.save_metrics(photo.id, metrics)  # total_score is kept until the group is re-ranked.
        self.catalog.append_event(photo.id, "preview_refined", {
            "job_id": job.id, "preview_kind": "refined",
            "metrics": {key: getattr(metrics, key) for key in ("sharpness", "exposure", "clipping", "face_quality", "eye_quality")},
        })
        # The old embedded preview goes only when no other photo still shows it (the cleanup retention rule).
        if (previous and previous != destination and previous.resolve().parent == self.settings.preview_dir.resolve()
                and not self.catalog.preview_references(str(previous))):
            previous.unlink(missing_ok=True)
        return "refined"

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
        cleared = self.catalog.cleared_fingerprints()  # Copied, never imported; Import with the tick brings them back.
        for index, (item, entry) in enumerate(photos, 1):
            if context.cancelled:
                break
            mirrored = len(roots) > 1 and entry["outcomes"][str(roots[1])] in ingest.VERIFIED
            with self._mutation():
                self._import_candidate(item.destinations[0], summary, skip_unchanged=True, job_id=job.id, cleared=cleared)
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
        return {key: getattr(summary, key) for key in ("discovered", "imported", "updated", "skipped", "failed", "previously_cleared")}

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
    def choose_crop(self, photo_id: int, candidate_id: str, *, rect: dict | None = None, aspect: str = "free") -> PhotoEdit:
        if candidate_id == MANUAL_CROP_ID:
            return self._choose_manual_crop(photo_id, rect, aspect)
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

    def _choose_manual_crop(self, photo_id: int, rect: dict | None, aspect: str) -> PhotoEdit:
        if aspect not in MANUAL_ASPECTS:
            raise ValueError("crop aspect must be one of " + ", ".join(MANUAL_ASPECTS))
        if not isinstance(rect, dict) or rect.keys() != {"left", "top", "width", "height"}:
            raise ValueError("a drawn crop needs left, top, width, and height")
        def snap(start, size):  # a drag flush to the edge can land one float step past 1
            numbers = all(type(value) in (int, float) for value in (start, size))
            return 1 - start if numbers and 1 < start + size <= 1 + 1e-9 else size

        crop = CropRect(rect["left"], rect["top"], snap(rect["left"], rect["width"]), snap(rect["top"], rect["height"]))
        if type(photo_id) is not int or not (photo := self.catalog.get_photo(photo_id)):
            raise ValueError(f"unknown photo ID: {photo_id}")
        width, height = read_image_dimensions(Path(photo.path))
        # No resolution floor: the photographer's own crop is theirs; only generated suggestions carry the 8 MP gate.
        edit = PhotoEdit(
            photo_id=photo_id, candidate_id=MANUAL_CROP_ID, rect=crop, aspect=aspect,
            source_width=width, source_height=height, author="photographer",
            rationale="drawn by the photographer", selected_at=datetime.now(timezone.utc),
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
    def save_develop(self, photo_id: int, settings: DevelopSettings) -> DevelopSettings:
        """Store these develop settings and re-render the frame's preview from the raw with them applied."""
        if not isinstance(settings, DevelopSettings) or settings.is_empty:  # empty means undeveloped: use clear_develop
            raise ValueError("develop settings are invalid")
        self._develop(photo_id, settings)
        return settings

    def suggest_develop(self, photo_id: int, tagger, *, settings: DevelopSettings,
                        feedback: str | None = None) -> DevelopAdvice:
        """Advisory: the review model proposes develop settings for one frame and says why.

        Nothing is written to `photo_develop` and the frame's own preview is untouched; the panel
        parks its sliders on the answer and the photographer keeps or discards it. The model reads a
        render of what the sliders say right now, so a second attempt critiques its own first one.
        """
        photo = self.catalog.get_photo(photo_id)
        if not photo:
            raise ValueError(f"unknown photo ID: {photo_id}")
        source = Path(photo.path)
        if not source.is_file():
            raise DevelopError(f"{source.name} is unavailable; reimport it before developing.")
        model, role = getattr(tagger, "model", "unknown"), getattr(tagger, "role", None)
        fields = settings.to_dict()
        with tempfile.TemporaryDirectory(prefix=".advise-", dir=self.settings.cache_dir) as directory:
            render = render_raw(source, Path(directory) / "advice.jpg", self.settings.raw_developer_path,
                                settings=fields, long_edge=DEVELOP_ADVICE_LONG_EDGE, image_format="jpeg",
                                histogram=True)
            try:
                advice = tagger.suggest_develop(render.path, settings=fields, feedback=feedback,
                                                histogram_note=describe_histogram(render.histogram))
            except Exception as error:
                with self._mutation():
                    self.catalog.append_event(photo_id, "develop_advice_failed", {
                        "model": model, "role": role, "error": str(error),
                        "raw_response": getattr(error, "raw_response", None)})
                raise
        with self._mutation():
            self.catalog.append_event(photo_id, "develop_advised", {
                "model": model, "role": role, "settings": advice.settings, "rationale": advice.rationale,
                "feedback": feedback})
        return advice

    def clear_develop(self, photo_id: int) -> None:
        """Drop the saved settings and re-render from the raw with none. The embedded preview is long gone, so
        the frame becomes `refined` rather than `embedded`: rendered from the raw, with nothing added."""
        if type(photo_id) is not int or not (photo := self.catalog.get_photo(photo_id)):
            raise ValueError(f"unknown photo ID: {photo_id}")
        # A frame whose settings were invalidated keeps preview_kind `developed` with nothing saved, and refine
        # skips it: clearing is the only way back, so it must still re-render.
        if self.catalog.get_develop(photo_id) is None and photo.preview_kind != "developed":
            return
        self._develop(photo_id, None)

    def develop_for(self, photo_id: int) -> DevelopSettings | None:
        return self.catalog.get_develop(photo_id)

    def _develop(self, photo_id: int, settings: DevelopSettings | None) -> None:
        """Re-render one contact-sheet preview from the raw and re-measure it. It deliberately does not re-rank
        the burst: a develop choice is the photographer's, and must never move the Best badge. Nothing inside the
        library changes until the render and the measurement have both succeeded."""
        if type(photo_id) is not int or not (photo := self.catalog.get_photo(photo_id)):
            raise ValueError(f"unknown photo ID: {photo_id}")
        source = Path(photo.path)

        def refuse_if_changed() -> None:
            """The file must still hold the bytes the catalog knows, both before and after the render."""
            try:
                current = fingerprint_file(source)
            except OSError:
                raise DevelopError(f"{source.name} is unavailable; reimport it before developing.") from None
            if current != photo.fingerprint:  # Other bytes: saved settings no longer describe this file.
                self.catalog.invalidate_develop(photo_id, photo.fingerprint, current)
                self._write_sidecar(photo_id)
                raise DevelopError(f"{source.name} changed since import; reimport it before developing.")

        refuse_if_changed()
        # ponytail: previews are content-addressed, so two copies of one file share this path and the last
        # develop wins; give the path a per-photo suffix if duplicate frames are ever developed apart.
        destination = self.settings.preview_dir / f"{photo.fingerprint}-2048.jpg"
        with tempfile.TemporaryDirectory(prefix=".develop-", dir=self.settings.cache_dir) as directory:
            rendered, normalized = Path(directory) / "render.jpg", Path(directory) / "preview.jpg"
            render_raw(source, rendered, self.settings.raw_developer_path,
                       settings={} if settings is None else settings.to_dict(),
                       long_edge=self.previews.max_size, image_format="jpeg")
            try:
                self.previews.normalize(rendered, normalized)
                metrics = measure_image(normalized)
                metrics.astro = analyze_astro(normalized)
                if vision := analyze_faces(normalized, self.settings.vision_analyzer_path):
                    apply_vision_result(metrics, vision)
            except (OSError, PreviewError, ValueError) as error:
                raise DevelopError(f"The rendered preview for {source.name} could not be read.") from error
            refuse_if_changed()  # Swapped during the render: the preview would show other bytes.
            try:
                normalized.replace(destination)  # The render held up: only now does the library change.
            except OSError as error:
                raise DevelopError(f"The developed preview for {source.name} could not be saved.") from error
        previous = Path(photo.preview_path) if photo.preview_path else None
        kind = "refined" if settings is None else "developed"
        self.catalog.update_preview(photo_id, str(destination), kind)
        self.catalog.save_metrics(photo_id, metrics)  # total_score is left as the last ranking wrote it.
        details = {"preview_kind": kind, "metrics": {key: getattr(metrics, key) for key in
                   ("sharpness", "exposure", "clipping", "face_quality", "eye_quality")}}
        if settings is None:
            self.catalog.clear_develop(photo_id, details)
        else:
            self.catalog.save_develop(photo_id, settings, photo.fingerprint, details)
        self._write_sidecar(photo_id)
        # The old preview goes only when no other photo still shows it (the cleanup retention rule).
        if (previous and previous != destination and previous.resolve().parent == self.settings.preview_dir.resolve()
                and not self.catalog.preview_references(str(previous))):
            previous.unlink(missing_ok=True)

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
            with self._mutation():
                self.catalog.append_event(photo_id, "composition_review_failed", {
                    "model": model,
                    "role": getattr(tagger, "role", None),
                    "error": str(error),
                    "raw_response": getattr(error, "raw_response", None),
                    "candidate_ids": candidate_ids,
                })
                try:
                    self._write_sidecar(photo_id)
                except Exception as sidecar_error:
                    error.add_note(f"composition failure sidecar write failed: {sidecar_error}")
            raise
        with self._mutation():
            self.catalog.append_event(photo_id, "composition_reviewed", {
                "model": model,
                "role": getattr(tagger, "role", None),
                "candidate_id": result.candidate_id,
                "rationale": result.rationale,
                "confidence": result.confidence,
                "candidate_ids": candidate_ids,
            })
            self._write_sidecar(photo_id)
        return result

    def _group(self, group_id: int) -> dict | None:
        return next((group for group in self.catalog.list_groups() if group["id"] == group_id), None)

    @staticmethod
    def _critique(group_id: int, frame: dict, details: dict) -> dict:
        """Public burst-critique shape; raw model output stays in provenance."""
        return {"group_id": group_id, "photo_id": int(frame["id"]), "name": Path(frame["path"]).name,
                "rationale": details["rationale"], "confidence": details["confidence"],
                "candidate_ids": details["candidate_ids"]}

    def critique_burst(self, group_id: int, tagger) -> dict:
        """Advisory: ask the review model for the best frame of a burst; the pick is never changed here."""
        group = self._group(group_id)
        if group is None:
            raise ValueError(f"unknown group ID: {group_id}")
        kept = [photo for photo in group["photos"] if photo["decision"] != "reject"]  # rank order; rejected never sent
        if len(kept) < 2:
            raise ValueError("burst critique needs at least two frames that are not rejected")
        frames = kept[:8]
        if any(not photo["preview_path"] or not Path(photo["preview_path"]).is_file() for photo in frames):
            raise ValueError(f"burst {group_id} has a frame without a preview")
        candidate_ids = [int(photo["id"]) for photo in frames]
        model, role, raw = getattr(tagger, "model", "unknown"), getattr(tagger, "role", None), None

        def failed(error: Exception) -> None:
            with self._mutation():
                self.catalog.append_event(None, "burst_review_failed", {
                    "model": model, "role": role, "group_id": group_id, "error": str(error),
                    "raw_response": getattr(error, "raw_response", None) or raw, "candidate_ids": candidate_ids})

        try:  # The model answers with no lock held, so culling carries on meanwhile.
            result = tagger.critique_burst([(str(photo["id"]), Path(photo["preview_path"])) for photo in frames])
            raw = result.raw_response
            if result.frame_id not in {str(photo_id) for photo_id in candidate_ids}:
                raise BurstResponseError("oMLX returned a frame that was not supplied", raw)
        except Exception as error:
            failed(error)
            raise
        with self._mutation():
            current = self._group(group_id)
            chosen = next((photo for photo in (current or {}).get("photos", [])
                           if photo["id"] == int(result.frame_id) and photo["decision"] != "reject"), None)
            if chosen is None:
                error = ValueError("burst changed during review")
                failed(error)
                raise error
            details = {"model": model, "role": role, "group_id": group_id, "frame_id": int(chosen["id"]),
                       "candidate_ids": candidate_ids, "rationale": result.rationale, "confidence": result.confidence,
                       "raw_response": raw}
            self.catalog.append_event(int(chosen["id"]), "burst_reviewed", details)
            for photo in current["photos"]:
                self._write_sidecar(int(photo["id"]))
        return self._critique(group_id, chosen, details)

    def latest_critique(self, group_id: int) -> dict | None:
        """The newest stored burst critique whose chosen frame is still a non-rejected member, without raw output."""
        row = self.catalog.connection.execute("""
            SELECT photos.id, photos.path, provenance.details_json FROM provenance
            JOIN photos ON photos.id = json_extract(provenance.details_json, '$.frame_id')
            WHERE provenance.kind = 'burst_reviewed' AND json_extract(provenance.details_json, '$.group_id') = ?
              AND photos.group_id = ? AND photos.decision != 'reject'
            ORDER BY provenance.id DESC LIMIT 1
        """, (group_id, group_id)).fetchone()
        return self._critique(group_id, dict(row), json.loads(row["details_json"])) if row else None

    def latest_composition_review(self, photo_id: int, candidate_ids: list[str]) -> dict | None:
        """The newest stored crop review whose pick is still among `candidate_ids`, without raw output."""
        if not candidate_ids:
            return None
        row = self.catalog.connection.execute(f"""
            SELECT details_json FROM provenance
            WHERE kind = 'composition_reviewed' AND photo_id = ?
              AND json_extract(details_json, '$.candidate_id') IN ({", ".join("?" * len(candidate_ids))})
            ORDER BY id DESC LIMIT 1
        """, (photo_id, *candidate_ids)).fetchone()
        if not row:
            return None
        details = json.loads(row["details_json"])
        return {key: details[key] for key in ("candidate_id", "rationale", "confidence")}

    def latest_develop_advice(self, photo_id: int) -> dict | None:
        """The newest stored develop advice, without raw output."""
        row = self.catalog.connection.execute("""
            SELECT details_json FROM provenance WHERE kind = 'develop_advised' AND photo_id = ?
            ORDER BY id DESC LIMIT 1
        """, (photo_id,)).fetchone()
        if not row:
            return None
        details = json.loads(row["details_json"])
        return {"settings": details["settings"], "rationale": details.get("rationale")}

    def expand_search(self, query: str, tagger) -> dict:
        """Advisory: vocabulary keywords the fast model relates to `query`, as an OR text filter with the query first."""
        if not isinstance(query, str) or not 1 <= len(query := query.strip()) <= 80:
            raise ValueError("search expansion requires a query of 1 to 80 characters")
        terms, seen = [], {query.casefold()}
        for term in tagger.expand_query(query, load_vocabulary(self.settings)):
            if term.casefold() not in seen:
                seen.add(term.casefold())
                terms.append(term)
        return {"query": query, "terms": terms, "text": " | ".join([query, *terms])}

    def caption_photo(self, photo_id: int, tagger) -> dict:
        """Advisory: store the review model's caption and alt text for the photographer to edit."""
        photo = self.catalog.get_photo(photo_id)
        if not photo or not photo.preview_path or not Path(photo.preview_path).is_file():
            raise ValueError(f"photo {photo_id} has no preview")
        model, role = getattr(tagger, "model", "unknown"), getattr(tagger, "role", None)
        try:
            result = tagger.caption(Path(photo.preview_path), place=photo.place_label)
        except Exception as error:
            with self._mutation():
                self.catalog.append_event(photo_id, "caption_failed", {
                    "model": model, "role": role, "error": str(error), "raw_response": getattr(error, "raw_response", None)})
                self._write_sidecar(photo_id)
            raise
        with self._mutation():
            return self._save_caption(photo_id, {"caption": result.caption, "alt_text": result.alt_text}, model, {
                "model": model, "role": role, "raw_response": result.raw_response, "author": "model"})

    @_coordinated
    def set_caption(self, photo_id: int, *, caption: str | None, alt_text: str | None) -> dict:
        """Photographer edit: None leaves a field unchanged, an empty string clears it."""
        if not self.catalog.get_photo(photo_id):
            raise ValueError(f"unknown photo ID: {photo_id}")
        caption, alt_text = (None if value is None else value.strip() for value in (caption, alt_text))
        _check_caption(caption, alt_text)
        changes = {key: value for key, value in (("caption", caption), ("alt_text", alt_text)) if value is not None}
        return self._save_caption(photo_id, changes, "photographer", {"author": "photographer"})

    def _save_caption(self, photo_id: int, changes: dict, model: str, details: dict) -> dict:
        existing = self.catalog.get_tags(photo_id)
        data = (existing["data"] if existing else {}) | changes | {"caption_author": details["author"]}
        data = {key: value for key, value in data.items() if not (key in changes and value == "")}
        self.catalog.save_tags(photo_id, data, existing["model"] if existing else model)
        shape = {"photo_id": photo_id, "caption": data.get("caption"), "alt_text": data.get("alt_text")}
        self.catalog.append_event(photo_id, "caption_written", details | {
            "caption": shape["caption"], "alt_text": shape["alt_text"]})
        self._write_sidecar(photo_id)
        return shape

    @_coordinated
    def digest_day(self, day: date, tagger=None, *, overwrite: bool = False, unavailable_reason: str | None = None) -> dict:
        """Facts first: the table is always written; the review model's narrative is appended only when it answers."""
        path = self.settings.digest_dir / f"{day.isoformat()}.md"
        existed = path.exists()
        if existed and not overwrite:
            raise FileExistsError("digest exists; confirm overwrite")
        facts = day_facts(self, day)
        text = render_facts_table(facts)
        narrative, raw = False, None
        if facts["frames"]:
            if tagger is None:
                body = f"Narrative unavailable: {unavailable_reason or 'review model is not loaded in oMLX'}"
            else:
                try:
                    result = tagger.write_digest(text)
                    body, raw, narrative = result.narrative, result.raw_response, True
                except Exception as error:  # A model failure never costs the facts.
                    body, raw = f"Narrative unavailable: {public_message(str(error))}", getattr(error, "raw_response", None)
            text += f"\n## Narrative\n\n{body}\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        SidecarWriter._atomic_write(path, text)
        self.catalog.append_event(None, "digest_written", {
            "date": facts["date"], "path_name": path.name, "frames": facts["frames"], "narrative": narrative,
            "model": getattr(tagger, "model", None), "role": getattr(tagger, "role", None), "raw_response": raw})
        return {"date": facts["date"], "file": path.name, "frames": facts["frames"], "narrative": narrative, "overwritten": existed}

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
    def merge_groups(self, group_ids: list[int]) -> dict:
        """Merge bursts into the earliest one; the merge survives automatic regrouping through `Photo.merged_into`."""
        if (not isinstance(group_ids, list) or len(group_ids) < 2
                or any(type(group_id) is not int or group_id <= 0 for group_id in group_ids) or len(set(group_ids)) != len(group_ids)):
            raise ValueError("merge needs at least two distinct positive group IDs")
        ordered = [group["id"] for group in self.catalog.list_groups() if group["id"] in set(group_ids)]
        if missing := set(group_ids) - set(ordered):
            raise ValueError(f"group {min(missing)} does not exist")
        target, sources = ordered[0], ordered[1:]
        photo_ids = self.catalog.merge_groups(target, sources)
        for photo_id in photo_ids:
            self.catalog.append_event(photo_id, "groups_merged", {"group_id": target, "merged_group_ids": sources})
        pick = self._rerank_group(target)
        return {"group_id": target, "merged_group_ids": sources, "photo_count": len(photo_ids), "automatic_pick_id": pick}

    @_coordinated
    def split_photo(self, photo_id: int) -> dict:
        """Give one frame a burst of its own; `Photo.merged_into` keeps it apart through automatic regrouping."""
        if type(photo_id) is not int or photo_id <= 0:
            raise ValueError("split needs a positive photo ID")
        from_group = self.catalog.split_photo(photo_id)
        self._regroup_and_write_sidecars()  # the pass an import runs, so the result is what the next re-import keeps
        group_id = self.catalog.get_photo(photo_id).group_id
        self.catalog.append_event(photo_id, "burst_split", {"group_id": group_id, "from_group_id": from_group})
        return {"photo_id": photo_id, "group_id": group_id, "from_group_id": from_group}

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

    def _cleared_selection(self) -> tuple[list[int], list[int], bool]:
        """Live ledger frames, the bursts they empty entirely, and whether any burst is only partly covered."""
        photo_ids = self.catalog.live_cleared_photo_ids()
        selected = set(photo_ids)
        members: dict[int, set[int]] = {}
        for photo in self.catalog.list_photos():
            if photo.group_id is not None:
                members.setdefault(photo.group_id, set()).add(photo.id)
        touched = [group_id for group_id, ids in members.items() if ids & selected]
        full = sorted(group_id for group_id in touched if members[group_id] <= selected)
        return photo_ids, full, len(full) < len(touched)

    @_coordinated
    def cleared_in_catalog(self) -> dict:
        seed = self.seed_cleared_from_trash()
        photo_ids, group_ids, _ = self._cleared_selection()
        return {"frames": len(photo_ids), "batches": len(group_ids), "seed": seed}

    @_coordinated
    def remove_cleared(self) -> CleanupResult:
        try:
            self.seed_cleared_from_trash()
            photo_ids, group_ids, partial = self._cleared_selection()
            if not photo_ids:
                raise ValueError("no previously cleared frames in the catalog")
            result = self._cleanup(photo_ids, group_ids)
            if partial:
                self._regroup_and_write_sidecars()  # Survivors of a partly removed burst get a fresh ranking.
            return result
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
            self.catalog.record_cleared({photo.fingerprint for photo in photos if photo.id in selected},
                                        datetime.now(timezone.utc).isoformat())
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
        self._prune_thumbnails()
        return CleanupResult(len(photo_ids), len(moves), trash, snapshot)

    @_coordinated
    def seed_cleared_from_trash(self) -> dict:
        """Fill the ledger from earlier cleanup snapshots; idempotent, and an unreadable snapshot is only counted."""
        snapshots = skipped = added = 0
        for path in sorted(self.settings.trash_dir.glob("*/catalog.sqlite3")):
            try:
                # The snapshot holds the whole catalog; the manifest names the photos that cleanup removed.
                photo_ids = json.loads((path.parent / "manifest.json").read_text(encoding="utf-8"))["photo_ids"]
                placeholders = ",".join("?" for _ in photo_ids)
                with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as snapshot:
                    fingerprints = [row[0] for row in snapshot.execute(
                        f"SELECT fingerprint FROM photos WHERE id IN ({placeholders})", photo_ids)]
            except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
                skipped += 1
                continue
            snapshots += 1
            added += self.catalog.record_cleared(fingerprints, path.parent.name.split("-", 1)[0])
        return {"snapshots": snapshots, "skipped": skipped, "added": added}

    def _prune_thumbnails(self) -> int:
        """Best effort: thumbnails are a cache, so a failed unlink never fails the caller."""
        try:
            photos = self.catalog.list_photos()
            return (prune_thumbnails(self.settings.cache_dir / "thumbs",
                                     {Path(photo.preview_path) for photo in photos if photo.preview_path})
                    + prune_cache(self.settings.cache_dir / "full", {f"{photo.fingerprint}.jpg" for photo in photos}))
        except OSError:
            return 0

    def _prepare_export(self, request: ExportRequest) -> tuple[list[Photo], object]:
        """Validate every selector and the renderer before any photo is touched."""
        if not request.photo_ids or len(set(request.photo_ids)) != len(request.photo_ids):
            raise ValueError("export requires unique photo IDs")
        photos = []
        for photo_id in request.photo_ids:
            if type(photo_id) is not int or not (photo := self.catalog.get_photo(photo_id)):
                raise ValueError(f"unknown photo ID: {photo_id}")
            photos.append(photo)
        if request.profile is not None and not request.profile.is_file():
            raise ValueError(f"profile does not exist: {request.profile}")
        if request.preset not in EXPORT_PRESETS:
            raise ValueError(f"unknown export preset: {request.preset!r}")
        validate_watermark(request.watermark)
        if type(request.keep_original) is not bool:
            raise ValueError("keep_original must be true or false")
        if type(request.dated_folder) is not bool:
            raise ValueError("dated_folder must be true or false")
        if not self.settings.jpeg_output_dir.resolve().is_relative_to(self.settings.library_dir.resolve()):
            raise ValueError("JPEG output must stay inside the managed library")
        # Raws render through the native raw developer, so a library bootstrapped before it existed refuses the
        # whole request rather than falling back to a decoder the photographer did not choose.
        if (request.profile is None and not self.settings.raw_developer_path.is_file()
                and any(Path(photo.path).suffix.lower() in RAW_EXTENSIONS for photo in photos)):
            raise ValueError("the raw developer is missing; run scripts/bootstrap.sh to build it")
        return photos, PipelineRegistry().darktable(self.settings.output_dir, self.settings.raw_developer_path)

    @_coordinated
    def export_jpegs(self, request: ExportRequest) -> list[ExportResult]:
        photos, pipeline = self._prepare_export(request)
        return [self._export_one(photo, request, pipeline) for photo in photos]

    def start_export_job(self, request: ExportRequest) -> Job:
        """Export in the background with progress and cancel; failures are counted and logged by basename."""
        self._prepare_export(request)
        manifest = {"kind": "export", "preset": request.preset, "profile": str(request.profile) if request.profile else None,
                    "keep_original": request.keep_original, "dated_folder": request.dated_folder,
                    "photo_ids": list(request.photo_ids)}
        runner = self.job_runner
        job = self.catalog.create_prepared_job("export", lambda job_id: self._write_job_files(job_id, manifest))

        def work(context) -> None:
            with Library(self.settings, job_runner=runner) as worker:
                pipeline = PipelineRegistry().darktable(worker.settings.output_dir, worker.settings.raw_developer_path)
                counts = {"exported": 0, "failed": 0, "skipped": 0}
                for index, photo_id in enumerate(request.photo_ids, 1):
                    if context.cancelled:
                        break
                    with worker._mutation():
                        photo = worker.catalog.get_photo(photo_id)
                        if photo is None:  # Removed since the job started: nothing to export.
                            counts["skipped"] += 1
                        else:
                            result = worker._export_one(photo, request, pipeline)
                            counts["exported" if result.success else "failed"] += 1
                            if not result.success:
                                worker._log_job(job, f"{Path(photo.path).name}: {result.message}")
                    context.update_progress(index / len(request.photo_ids), label=Path(photo.path).name[:200] if photo else None,
                                            processed=index, total=len(request.photo_ids))
                worker._write_job_files(job.id, manifest | {"summary": counts})

        self._submit_job(runner, job, work)
        return job

    def start_develop_group_job(self, photo_id: int) -> Job:
        """Copy one frame's develop settings onto every other frame of its burst, in the background. Rejected
        frames are included; non-raw and offline frames are skipped and counted, never failed."""
        photo = self.catalog.get_photo(photo_id) if type(photo_id) is int else None
        if photo is None:
            raise ValueError(f"unknown photo ID: {photo_id}")
        if photo.group_id is None or self.catalog.get_develop(photo_id) is None:
            raise ValueError("develop this frame first; it has no settings to apply")
        ids = [other for other in self.catalog.group_photo_ids(photo.group_id) if other != photo_id]
        manifest = {"kind": "develop_group", "source_photo_id": photo_id, "photo_ids": ids}
        runner = self.job_runner
        job = self.catalog.create_prepared_job("develop_group", lambda job_id: self._write_job_files(job_id, manifest))

        def work(context) -> None:
            with Library(self.settings, job_runner=runner) as worker:
                counts = {"applied": 0, "skipped_non_raw": 0, "skipped_offline": 0, "failed": 0}
                written: list[int] = []
                try:  # The frames written are recorded however the loop ends, crash included.
                    for index, other_id in enumerate(ids, 1):
                        if context.cancelled:
                            break
                        name = None
                        with worker._mutation():
                            other = worker.catalog.get_photo(other_id)
                            source = worker.catalog.get_develop(photo_id)
                            if other is not None:
                                name = Path(other.path).name
                            if other is None or source is None:  # Removed or cleared since the job started.
                                counts["failed"] += 1
                                worker._log_job(job, f"{name or f'photo {other_id}'}: "
                                                     + ("removed since the job started" if other is None
                                                        else "the source frame's settings were cleared"))
                            elif other.format.upper() != "ARW":
                                counts["skipped_non_raw"] += 1
                            elif not Path(other.path).is_file():  # The volume is routinely unplugged.
                                counts["skipped_offline"] += 1
                            else:
                                try:  # DevelopSettings(source.fields) raises: fields is read-only.
                                    worker.save_develop(other_id, DevelopSettings.from_dict(source.to_dict()))
                                except (DevelopError, ValueError, OSError) as error:
                                    counts["failed"] += 1
                                    worker._log_job(job, f"{name}: {error}")
                                else:
                                    counts["applied"] += 1
                                    written.append(other_id)
                        context.update_progress(index / len(ids), label=name[:200] if name else None,
                                                processed=index, total=len(ids))
                finally:
                    if written:
                        with worker._mutation():
                            worker.catalog.append_event(photo_id, "develop_applied_to_group",
                                                        {"source_photo_id": photo_id, "photo_ids": written})
                worker._write_job_files(job.id, manifest | {"summary": counts})

        self._submit_job(runner, job, work)
        return job

    def _export_one(self, photo: Photo, request: ExportRequest, pipeline) -> ExportResult:
        done: list[list] = []  # [JpegExportResult, Artifact | None] per published JPEG, for compensation.
        try:
            tags = self.catalog.get_tags(photo.id)
            data = tags["data"] if tags else {}
            if fingerprint_file(Path(photo.path)) != photo.fingerprint:
                raise ValueError("source content changed since import; reimport before exporting")
            develop = _develop_fields(self.catalog.get_develop(photo.id))
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
            share = request.preset != "full"  # Share and Web use the caption; Full keeps the photographer's description and no alt text.
            caption = data.get("caption")
            caption_used = share and bool(caption)
            # The untouched companion is a second render of the same source with no develop settings, under a name
            # that cannot collide with another preset's companion. It is pointless for an undeveloped frame.
            plan = [(develop, None)] + ([(None, "original")] if request.keep_original and develop else [])
            for settings, stem_suffix in plan:
                rendered = pipeline.export_jpeg(Path(photo.path), request.profile,
                    crop=crop,
                    description=caption if caption_used else data.get("description"),
                    keywords=data.get("subjects"), alt_text=data.get("alt_text") if share else None, caption_used=caption_used,
                    rating=-1 if photo.decision == "reject" else photo.rating,
                    reserved_paths={Path(artifact.path) for artifact in self.catalog.list_artifacts()},
                    preset=request.preset, develop=settings, stem_suffix=stem_suffix, watermark=request.watermark,
                    place=photo.place, country=photo.place_country, subfolder=_export_folder(photo, request),
                    gps=(photo.latitude, photo.longitude) if photo.latitude is not None and photo.longitude is not None else None)
                if not rendered.success:
                    if done:  # The developed JPEG published; the companion did not, so neither stands.
                        raise RuntimeError(f"the untouched version failed, so the developed JPEG was withdrawn "
                                           f"as well and nothing was exported: {rendered.message}")
                    return self._export_failed(photo.id, rendered.message)
                published = [rendered, None]
                done.append(published)
                published[1] = self.catalog.save_artifact(photo.id, Path(photo.path).name,
                    rendered.settings["source_fingerprint"], "jpeg", str(rendered.output),
                    rendered.settings, rendered.output_fingerprint)
                self.catalog.append_event(photo.id, "exported", {
                    **rendered.settings, "artifact_id": published[1].id, "output": published[1].path,
                    "output_fingerprint": published[1].output_fingerprint,
                    "created_at": published[1].created_at.isoformat(),
                })
            self._write_sidecar(photo.id)
            first, artifact = done[0]
            return ExportResult(photo.id, True, first.output, first.message, artifact)
        except Exception as error:
            errors = [str(error)]
            if done:
                try:
                    for _, artifact in done:
                        if artifact is not None:
                            self.catalog.discard_export(artifact.id)
                    if any(artifact is None for _, artifact in done):
                        self.catalog.connection.rollback()
                except Exception as compensation_error:
                    errors.append(f"catalog compensation failed: {compensation_error}")
                for rendered, _ in done:
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
            return self._export_failed(photo.id, "; ".join(errors))

    def _export_failed(self, photo_id: int, message: str) -> ExportResult:
        """Record the failure so the contact sheet's Export failed filter can find the frame until it exports."""
        try:
            if self.catalog.get_photo(photo_id) is not None:
                self.catalog.append_event(photo_id, "export_failed", {})
        except Exception as error:  # The failure itself is what the photographer needs to hear about.
            message += f"; recording the failure failed: {error}"
        return ExportResult(photo_id, False, None, message)

    @_coordinated
    def prepare_astro_job(self, group_id: int, *, confirmed: bool, excluded_photo_ids: Sequence[int] = (),
                          darks: Path | None = None, flats: Path | None = None, biases: Path | None = None,
                          recipe: str = "tracked") -> Job:
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
                darks=darks, flats=flats, biases=biases, astro_metrics=metrics, recipe=recipe,
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
                              "flats": len(manifest.flats.frames), "biases": len(manifest.biases.frames),
                              "recipe": manifest.recipe}
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
        events = self.catalog.events_for(photo_id)
        return {
            "photo": photo,
            "metrics": metrics,
            "events": events,
            "tags": self.catalog.get_tags(photo_id),
            "warnings": self._import_warnings(events),
        }

    @staticmethod
    def _import_warnings(events) -> list[str]:
        """Warnings from the latest import of this photo; a reimport after a decoder fix clears them."""
        latest = next((event for event in reversed(events) if event.kind == "imported"), None)
        warning = latest.details.get("raw_warning") if latest else None
        return [warning] if warning else []

    def tag_photo(self, photo_id: int, tagger, *, job_id: int | None = None):
        photo = self.catalog.get_photo(photo_id)
        if not photo or not photo.preview_path:
            raise ValueError(f"photo {photo_id} has no preview")
        role = getattr(tagger, "role", None)
        try:
            vocab = load_vocabulary(self.settings)
            result = tagger.tag(Path(photo.preview_path), vocabulary=vocab, place=photo.place_label)
        except Exception as error:
            with self._mutation():
                raw_response = getattr(error, "raw_response", None)
                self.catalog.append_event(
                    photo_id,
                    "tag_failed",
                    {
                        "model": getattr(tagger, "model", "unknown"),
                        "role": role,
                        "error": str(error),
                        "raw_response": raw_response,
                    } | _job_detail(job_id),
                )
                self._write_sidecar(photo_id)
            raise
        with self._mutation():
            # A species below 0.6 is never applied, even when the model also lists it as a subject; it stays a suggestion.
            weak = {item["name"].casefold() for item in result.species if item["confidence"] < 0.6}
            candidates = list(result.subjects) + ([result.scene] if result.scene else []) + list(result.conditions)
            applied, suggested = match_keywords([word for word in candidates if word.strip().casefold() not in weak], vocab)
            # The vocabulary learns from the model: free-text words and confident species become keywords and join the file.
            # Only a weak species (< 0.6) stays a suggestion for the photographer.
            confident_species = [item["name"] for item in result.species if item["confidence"] >= 0.6
                                 and item["name"].casefold() not in {word.casefold() for word in applied + suggested}]
            # The place is stated, not inferred: it is applied even when the model's reply never mentions it.
            place_words = [word for word in (photo.place, photo.place_country) if word]
            learned = learn_keywords(self.settings, {"places": place_words, "learned": suggested, "wildlife": confident_species})
            if learned:
                self.catalog.append_event(photo_id, "vocabulary_learned", {"words": learned} | _job_detail(job_id))
            vocab = load_vocabulary(self.settings)
            applied = match_keywords(applied + suggested + confident_species + place_words, vocab)[0]
            existing = self.catalog.get_tags(photo_id)
            # The photographer's words lead and keep their casing; the model's words follow. A model word removed earlier may return.
            kept = (existing["data"] if existing else {}).get("photographer", [])
            applied = kept + [word for word in applied if word.casefold() not in {k.casefold() for k in kept}]
            suggested = [item["name"] for item in result.species if item["confidence"] < 0.6
                         and item["name"].casefold() not in {word.casefold() for word in applied}]
            payload = (existing["data"] if existing else {}) | {
                "description": result.description,
                "subjects": applied,
                "scene": result.scene,
                "technical": result.technical,
                "confidence": result.confidence,
                "species": list(result.species),
                "conditions": list(result.conditions),
                "suggested": suggested,
            }
            self.catalog.save_tags(photo_id, payload, tagger.model)
            self.catalog.append_event(
                photo_id,
                "tagged",
                {"model": tagger.model, "role": role, "confidence": result.confidence, "applied": applied,
                 "suggested": suggested, "raw_response": result.raw_response} | _job_detail(job_id),
            )
            self._write_sidecar(photo_id)
        return result

    @_coordinated
    def add_keyword(self, photo_id: int, keyword: str) -> dict:
        """Accept a suggestion or apply a word the photographer typed; both are the photographer's and teach the vocabulary."""
        tags = self.catalog.get_tags(photo_id)
        data = tags["data"] if tags else {}
        typed = keyword not in data.get("suggested", [])
        if typed:
            keyword = keyword.strip()
            if not 1 <= len(keyword) <= 40 or "#" in keyword:
                raise ValueError("keyword must be 1 to 40 characters without #")
            if not self.catalog.get_photo(photo_id):
                raise ValueError(f"unknown photo ID: {photo_id}")
            if keyword.casefold() in {word.casefold() for word in data.get("subjects", [])}:
                raise ValueError(f"keyword is already applied for photo {photo_id}")
            data = {"description": "", "subjects": [], "suggested": []} | data
            data["suggested"] = [word for word in data["suggested"] if word.casefold() != keyword.casefold()]
            data["subjects"] = data["subjects"] + [keyword]
            data["typed"] = data.get("typed", []) + [keyword]
            category = "learned"
        else:
            data["suggested"] = [word for word in data["suggested"] if word != keyword]
            data["subjects"] = data.get("subjects", []) + ([] if keyword in data.get("subjects", []) else [keyword])
            species = {item.get("name", "").casefold() for item in data.get("species", [])
                       if isinstance(item, dict) and isinstance(item.get("name"), str)}
            category = "wildlife" if keyword.strip().casefold() in species else "learned"  # a confirmed species is wildlife
        data["photographer"] = data.get("photographer", []) + [keyword]
        self.catalog.save_tags(photo_id, data, tags["model"] if tags else "photographer")
        self.catalog.append_event(photo_id, "keyword_added", {"keyword": keyword, "typed": typed})
        self._write_sidecar(photo_id)
        if learned := learn_keywords(self.settings, {category: [keyword]}):  # a hand-applied word teaches the vocabulary
            self.catalog.append_event(photo_id, "vocabulary_learned", {"words": learned})
        return {"photo_id": photo_id, "subjects": data["subjects"], "suggested": data["suggested"]}

    def vocabulary_report(self) -> dict:
        """Vocabulary plus how many photos carry each applied word (case-insensitive)."""
        counts: dict[str, int] = {}
        for photo in self.catalog.list_photos():  # ponytail: full scan per request, fine to ~10k photos
            tags = self.catalog.get_tags(photo.id)
            for word in {word.casefold() for word in (tags["data"].get("subjects", []) if tags else [])}:
                counts[word] = counts.get(word, 0) + 1
        return {"categories": load_vocabulary(self.settings), "counts": counts, "blocked": load_blocked(self.settings)}

    @_coordinated
    def forget_keyword(self, word: str) -> dict:
        """Remove a word from every photo's applied keywords and block it from the vocabulary."""
        key = word.strip().casefold()
        if not key:
            raise ValueError("keyword must not be blank")
        forget_vocabulary_keyword(self.settings, key)
        changed = []
        for photo in self.catalog.list_photos():
            tags = self.catalog.get_tags(photo.id)
            data = tags["data"] if tags else {}
            # photographer/typed too, or the next retag would bring the word back as the photographer's
            kept = {field: [word for word in data[field] if word.casefold() != key]
                    for field in ("subjects", "photographer", "typed") if field in data}
            if any(kept[field] != data[field] for field in kept):
                self.catalog.save_tags(photo.id, data | kept, tags["model"])
                changed.append(photo.id)
        result = {"word": key, "removed_from": len(changed)}
        self.catalog.append_event(None, "keyword_forgotten", result)
        for photo_id in changed:
            self._write_sidecar(photo_id)
        return result

    @_coordinated
    def remove_keyword(self, photo_id: int, keyword: str) -> dict:
        """Undo an applied word: a typed word is dropped, any other goes back to suggested."""
        tags = self.catalog.get_tags(photo_id)
        data = tags["data"] if tags else {}
        if keyword not in data.get("subjects", []):
            raise ValueError(f"keyword is not applied for photo {photo_id}")
        typed = keyword in data.get("typed", [])
        data["subjects"] = [word for word in data["subjects"] if word != keyword]
        for key in ("photographer", "typed"):
            if key in data:
                data[key] = [word for word in data[key] if word != keyword]
        suggested = data.get("suggested", [])
        data["suggested"] = suggested + ([] if typed or keyword in suggested else [keyword])
        self.catalog.save_tags(photo_id, data, tags["model"])
        self.catalog.append_event(photo_id, "keyword_removed", {"keyword": keyword})
        self._write_sidecar(photo_id)
        return {"photo_id": photo_id, "subjects": data["subjects"], "suggested": data["suggested"]}

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
            exposure_seconds=metadata.get("exposure_seconds"),
            latitude=metadata.get("latitude"),
            longitude=metadata.get("longitude"),
            altitude=metadata.get("altitude"),
        )
        if previous and previous.fingerprint != fingerprint:
            self.catalog.clear_tags(previous.id)
            self.catalog.invalidate_crop(previous.id, previous.fingerprint, fingerprint)
        photo_id = self.catalog.upsert_photo(photo)
        self._resolve_place(photo_id, photo.latitude, photo.longitude)
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
                "raw_warning": metadata.get("raw_warning"),
            } | _job_detail(job_id),
        )
        return photo_id

    def _regroup_and_write_sidecars(self) -> None:
        photos = self.catalog.list_photos()
        groups = TimeGapGroupingPolicy(self.settings.burst_gap_seconds).group([photo for photo in photos if not photo.merged_into])
        metrics_by_id = {photo.id: self.catalog.get_metrics(photo.id) for photo in photos if photo.id is not None}
        hashes = {photo_id: metrics.dhash if metrics else None for photo_id, metrics in metrics_by_id.items()}
        groups = [cluster for group in groups
                  for cluster in split_near_duplicates(group, hashes, self.settings.near_duplicate_distance)]
        keyed = {_stable_key(group[0]): group for group in groups}  # Merges attach after splitting, so they are never split.
        for photo in sorted((photo for photo in photos if photo.merged_into), key=lambda item: item.path):
            if photo.merged_into in keyed:
                keyed[photo.merged_into].append(photo)
            else:  # ponytail: a merge whose target burst no longer exists falls back to a burst of its own.
                groups.append([photo])
        self.catalog.clear_group_assignments()
        selected_ids: set[int] = set()
        for group in groups:
            scored_input = []
            for photo in group:
                if photo.id is None:
                    continue
                metrics = metrics_by_id.get(photo.id)
                if metrics:
                    scored_input.append((photo, metrics))
            if not scored_input:
                continue
            ranking = rank_group(scored_input)
            selected = _select(ranking)
            selected_ids.add(int(selected.photo.id))
            first = group[0]
            self.catalog.assign_group(
                _stable_key(first),
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
            alt_text=data.get("alt_text"),
            artifacts=[artifact for artifact in catalog.list_artifacts() if artifact.photo_id == photo_id],
            active_crop=catalog.get_crop(photo_id),
            develop=_develop_fields(catalog.get_develop(photo_id)),
            decision=photo.decision,
            rating=photo.rating,
            place=photo.place,
            country=photo.place_country,
        )
