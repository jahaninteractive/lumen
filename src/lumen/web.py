from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
from dataclasses import asdict, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlsplit

from .catalog import GroupFilters
from .cli import _cleanup_error_payload, _json_default, _serializable_groups
from .config import Settings
from .diagnostics import doctor_report
from .jobs import LocalJobRunner
from .library import CleanupError, ExportRequest, Library, public_message
from .pipelines import EXPORT_PRESETS
from .omlx import OmlxTagger


STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
}

_FLAG_FILTERS = {"keepers_only", "tagged_only", "show_rejected"}


def parse_group_filters(query: str) -> GroupFilters:
    """Strict `/api/groups` query parser: unknown, repeated, or malformed keys raise ValueError("invalid filter: <name>")."""
    values = {}
    for key, items in parse_qs(query, keep_blank_values=False).items():
        if key not in GroupFilters.__dataclass_fields__:
            raise ValueError(f"invalid filter: {key if re.fullmatch(r'[a-z_]{1,32}', key) else 'unknown'}")
        if len(items) != 1:
            raise ValueError(f"invalid filter: {key}")
        value = items[0]
        if key in _FLAG_FILTERS:
            if value not in {"0", "1"}:
                raise ValueError(f"invalid filter: {key}")
            values[key] = value == "1"
        elif key == "min_rating":
            if not re.fullmatch(r"[0-5]", value):
                raise ValueError("invalid filter: min_rating")
            values[key] = int(value)
        else:
            values[key] = (value.strip() or None) if key == "text" else value
    return GroupFilters(**values)


def _refine_selector(payload: dict) -> dict:
    """Strict `/api/previews/refine` body: group needs only an integer group_id; filtered takes `/api/groups` fields as strings."""
    scope = payload.get("scope")
    fields = {key: value for key, value in payload.items() if key != "scope"}
    if scope == "group":
        if fields.keys() != {"group_id"} or type(fields["group_id"]) is not int:
            raise ValueError("group scope requires exactly one integer group_id")
        return {"group_id": fields["group_id"]}
    if scope == "filtered":
        if not all(isinstance(value, str) for value in fields.values()):
            raise ValueError("filter values must be strings")
        return {"filters": parse_group_filters(urlencode(fields))}  # Same validation as the /api/groups query.
    if scope == "all":
        if fields:
            raise ValueError("all scope takes no other fields")
        return {"all_photos": True}
    raise ValueError("scope must be group, filtered, or all")


def choose_macos_folder() -> str | None:
    if sys.platform != "darwin":
        raise RuntimeError("Folder browsing is only available on macOS")
    try:
        result = subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                'POSIX path of (choose folder with prompt "Choose a photo folder")',
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("The folder picker timed out") from error
    if result.returncode == 0:
        selected = result.stdout.strip()
        if selected:
            return selected
        raise RuntimeError("The folder picker returned no path")
    if "(-128)" in result.stderr:
        return None
    detail = result.stderr.strip() or "unknown macOS error"
    raise RuntimeError(f"Could not open the folder picker: {detail}")


def choose_macos_xmp() -> str | None:
    if sys.platform != "darwin":
        raise RuntimeError("XMP browsing is only available on macOS")
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", 'POSIX path of (choose file with prompt "Choose a darktable XMP profile" of type {"xmp"})'],
            capture_output=True, text=True, timeout=300, check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("The XMP picker timed out") from error
    if result.returncode == 0:
        selected = Path(result.stdout.strip()).expanduser().resolve()
        if selected.is_file() and selected.suffix.lower() == ".xmp":
            return str(selected)
        raise ValueError("Choose an existing .xmp file")
    if "(-128)" in result.stderr:
        return None
    raise RuntimeError(f"Could not open the XMP picker: {result.stderr.strip() or 'unknown macOS error'}")


def reveal_outputs(settings: Settings, destination: Path | None = None) -> str:
    root = settings.output_dir.resolve()
    destination = (destination or settings.jpeg_output_dir).resolve()
    if not root.is_relative_to(settings.library_dir.resolve()) or destination == root or not destination.is_relative_to(root):
        raise ValueError("JPEG directory must stay inside managed outputs")
    if sys.platform != "darwin":
        raise RuntimeError("Finder reveal is only available on macOS")
    destination.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            ["/usr/bin/open", str(destination)], capture_output=True, text=True, timeout=30, check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Finder reveal timed out") from error
    if result.returncode:
        raise RuntimeError(f"Could not show outputs in Finder: {result.stderr.strip() or 'unknown macOS error'}")
    return str(destination)


def reveal_astro_job(settings: Settings, job_id: int) -> str:
    """Finder reveal of one existing managed job folder; never creates folders for unknown jobs."""
    with Library(settings) as library:
        library.catalog.get_job(job_id)
    job_dir = settings.output_dir / "astro" / str(job_id)
    if not job_dir.is_dir():
        raise ValueError("astro job folder is unavailable")
    return reveal_outputs(settings, job_dir)


class LumenServer(ThreadingHTTPServer):
    """Loopback server that owns the single heavy-job runner shared by every per-request Library."""

    def __init__(self, address, handler, settings: Settings):
        super().__init__(address, handler)
        self.settings = settings
        self._runner_lock = threading.Lock()
        # Eager: takes the catalog's runner lock for the server's lifetime and recovers stale running jobs now.
        try:
            self._runner: LocalJobRunner | None = LocalJobRunner(settings.database_path)
        except BaseException:
            super().server_close()
            raise

    def job_runner(self) -> LocalJobRunner:
        with self._runner_lock:
            if self._runner is None:
                raise RuntimeError("the job runner is shut down")
            return self._runner

    def server_close(self) -> None:
        try:
            with self._runner_lock:
                runner, self._runner = self._runner, None
            if runner is not None:
                runner.shutdown()
        finally:
            super().server_close()


def make_server(settings: Settings, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("Lumen only binds to a loopback address")
    static_dir = Path(__file__).parent / "static"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if not self._trusted_host():
                self._send_json({"error": "non-loopback host rejected"}, status=HTTPStatus.FORBIDDEN)
                return
            path = urlparse(self.path).path
            if path in STATIC_FILES:
                name, content_type = STATIC_FILES[path]
                self._send_bytes((static_dir / name).read_bytes(), content_type)
                return
            if path == "/api/status":
                self._send_json(doctor_report(settings) | {
                    "jpeg_output_dir": str(settings.jpeg_output_dir.resolve()),
                    "astro_output_dir": str((settings.output_dir / "astro").resolve()),
                    "ingest_output_dir": str((settings.output_dir / "ingest").resolve()),
                })
                return
            if path == "/api/groups":
                try:
                    filters = parse_group_filters(urlparse(self.path).query)
                except ValueError as error:
                    self._send_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
                    return
                with Library(settings) as library:
                    # Rejected frames are fetched so an all-rejected burst stays listed and its counts cover them.
                    groups = _serializable_groups(library.list_groups(replace(filters, show_rejected=True)))
                    frame_counts = library.catalog.group_frame_counts()
                    payload = {"photo_count": library.photo_count(), "counts": library.catalog.group_counts(filters),
                               "cameras": library.cameras(), "keeper_count": len(library.keeper_ids())}
                hide_rejected = not filters.show_rejected and filters.decision == "any"
                rejected_count = 0  # rejected frames the current filters would reveal
                for group in groups:
                    group["frame_count"], group["rejected_count"] = frame_counts.get(group["id"], (len(group["photos"]), 0))
                    rejected_count += sum(photo["decision"] == "reject" for photo in group["photos"])
                    if hide_rejected:
                        group["photos"] = [photo for photo in group["photos"] if photo["decision"] != "reject"]
                # An emptied burst stays listed only when every frame is rejected; otherwise the filter just missed it.
                groups = [group for group in groups if group["photos"] or group["rejected_count"] == group["frame_count"]]
                self._send_json({"groups": groups, "rejected_count": rejected_count, **payload})
                return
            if path == "/api/jobs":
                with Library(settings) as library:
                    self._send_json({"jobs": library.list_job_status()})
                return
            match = re.fullmatch(r"/api/(astro/)?jobs/(\d+)", path)
            if match:
                try:
                    with Library(settings) as library:
                        self._send_json(library.job_status(int(match.group(2))))
                except (ValueError, OverflowError, OSError, RuntimeError):
                    self._send_json({"error": "astro job is unavailable" if match.group(1) else "job is unavailable"}, status=HTTPStatus.NOT_FOUND)
                return
            match = re.fullmatch(r"/api/photos/(\d+)/crops", path)
            if match:
                try:
                    photo_id = int(match.group(1))
                    with Library(settings) as library:
                        candidates = library.crop_candidates(photo_id)
                        photo = library.catalog.get_photo(photo_id)
                        active = library.catalog.get_crop(photo_id)
                    if photo is None:
                        raise ValueError(f"unknown photo ID: {photo_id}")
                    self._send_json({
                        "photo_id": photo_id,
                        "preview_url": f"/preview/{photo_id}?v={photo.fingerprint[:16]}-{photo.preview_kind}",
                        "candidates": [asdict(candidate) for candidate in candidates],
                        "active_crop": active.to_dict() if active else None,
                    })
                except (ValueError, OverflowError, OSError, RuntimeError):
                    self._send_json({"error": "crop choices are unavailable for this photo"}, status=HTTPStatus.BAD_REQUEST)
                return
            match = re.fullmatch(r"/api/photos/(\d+)", path)
            if match:
                with Library(settings) as library:
                    item = library.get_photo(int(match.group(1)))
                if not item:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                photo = asdict(item["photo"])
                preview_version = f"{photo['fingerprint'][:16]}-{photo['preview_kind']}"
                for key in ("path", "fingerprint", "preview_path", "mirror_path"):
                    photo.pop(key, None)
                photo["preview_url"] = f"/preview/{photo['id']}?v={preview_version}"
                self._send_json({"photo": photo, "metrics": asdict(item["metrics"]), "tags": item["tags"]})
                return
            match = re.fullmatch(r"/preview/(\d+)", path)
            if match:
                with Library(settings) as library:
                    photo = library.catalog.get_photo(int(match.group(1)))
                if not photo or not photo.preview_path:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                preview = Path(photo.preview_path).resolve()
                try:
                    preview.relative_to(settings.preview_dir.resolve())
                except ValueError:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                if not preview.is_file():
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self._send_bytes(preview.read_bytes(), "image/jpeg", cache=True)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if not self._trusted_mutation():
                self._send_json({"error": "cross-origin request rejected"}, status=HTTPStatus.FORBIDDEN)
                return
            try:
                payload = self._read_json()
                if path == "/api/browse":
                    selected = choose_macos_folder()
                    self._send_json({"path": selected, "cancelled": selected is None})
                    return
                if path in {"/api/browse-xmp", "/api/outputs/reveal"}:
                    if payload:
                        raise ValueError("this action requires an empty JSON object")
                    if path == "/api/browse-xmp":
                        selected = choose_macos_xmp()
                        self._send_json({"path": selected, "cancelled": selected is None})
                    else:
                        self._send_json({"path": reveal_outputs(settings)})
                    return
                if path == "/api/import":
                    if payload.keys() - {"path"}:
                        raise ValueError("unexpected import fields")
                    source = payload.get("path")
                    if not isinstance(source, str) or not source.strip():
                        raise ValueError("path is required")
                    with Library(settings, job_runner=self.server.job_runner()) as library:
                        job = library.start_import_job(Path(source))
                        self._send_json(library.job_status(job.id), status=HTTPStatus.ACCEPTED)
                    return
                if path == "/api/ingest":
                    if payload.keys() - {"card", "primary", "mirror"}:
                        raise ValueError("unexpected ingest fields")
                    for field in ("card", "primary", "mirror"):
                        value = payload.get(field)
                        if (value is not None or field != "mirror") and (not isinstance(value, str) or not value.strip()):
                            raise ValueError(f"{field} must be a folder path string")
                    mirror = payload.get("mirror")
                    with Library(settings, job_runner=self.server.job_runner()) as library:
                        job = library.start_ingest_job(Path(payload["card"]), Path(payload["primary"]),
                                                       Path(mirror) if mirror is not None else None)
                        self._send_json(library.job_status(job.id), status=HTTPStatus.ACCEPTED)
                    return
                if path == "/api/photos/tag-all":
                    if payload.keys() - {"retag"}:
                        raise ValueError("unexpected tag fields")
                    retag = payload.get("retag", False)
                    if type(retag) is not bool:
                        raise ValueError("retag must be true or false")
                    tagger = OmlxTagger(settings.omlx_url, settings.omlx_model)
                    with Library(settings, job_runner=self.server.job_runner()) as library:
                        job = library.start_tag_job(tagger, retag=retag)
                        self._send_json(library.job_status(job.id), status=HTTPStatus.ACCEPTED)
                    return
                if path == "/api/previews/refine":
                    with Library(settings, job_runner=self.server.job_runner()) as library:
                        try:
                            job = library.start_refine_job(**_refine_selector(payload))
                        except RuntimeError as error:
                            if str(error) != "darktable-cli is not installed":
                                raise
                            raise ValueError("darktable-cli is unavailable") from None
                        self._send_json(library.job_status(job.id), status=HTTPStatus.ACCEPTED)
                    return
                match = re.fullmatch(r"/api/jobs/(\d+)/cancel", path)
                if match:
                    if payload:
                        raise ValueError("cancel requires an empty JSON object")
                    job_id = int(match.group(1))
                    with Library(settings, job_runner=self.server.job_runner()) as library:
                        if library.catalog.get_job(job_id).status not in {"queued", "running"} or not library.cancel_job(job_id):
                            raise ValueError("job is no longer queued or running")
                        self._send_json(library.job_status(job_id))
                    return
                if path == "/api/export":
                    scope = payload.get("scope")
                    if scope not in ("current", "keepers"):
                        raise ValueError("scope must be current or keepers")
                    allowed = {"scope", "profile", "preset", "photo_id"} if scope == "current" else {"scope", "profile", "preset"}
                    if payload.keys() - allowed:
                        raise ValueError("unexpected export selectors or fields")
                    if scope == "current" and type(payload.get("photo_id")) is not int:
                        raise ValueError("photo_id must be an integer")
                    profile = payload.get("profile")
                    if profile is not None and not isinstance(profile, str):
                        raise ValueError("profile must be a path string or null")
                    preset = payload.get("preset", "full")
                    if not isinstance(preset, str) or preset not in EXPORT_PRESETS:
                        raise ValueError("preset must be full or share")
                    with Library(settings) as library:
                        photo_ids = (payload["photo_id"],) if scope == "current" else tuple(library.keeper_ids())
                        results = library.export_jpegs(ExportRequest(photo_ids, Path(profile) if profile is not None else None, preset))
                    status = HTTPStatus.OK if all(result.success for result in results) else HTTPStatus.BAD_REQUEST
                    self._send_json({"results": [asdict(result) for result in results]}, status=status)
                    return
                if path == "/api/groups/merge":
                    if payload.keys() != {"group_ids"}:
                        raise ValueError("merge requires exactly group_ids")
                    group_ids = payload["group_ids"]
                    if (not isinstance(group_ids, list) or len(group_ids) < 2 or len(set(group_ids)) != len(group_ids)
                            or any(type(group_id) is not int or group_id <= 0 for group_id in group_ids)):
                        raise ValueError("group_ids must list at least two distinct positive integers")
                    with Library(settings) as library:
                        self._send_json(library.merge_groups(group_ids))
                    return
                match = re.fullmatch(r"/api/groups/(\d+)/remove", path)
                if match:
                    if payload:
                        raise ValueError("remove requires an empty JSON object")
                    with Library(settings) as library:
                        result = library.remove_group(int(match.group(1)))
                    self._send_json(asdict(result))
                    return
                if path == "/api/library/clear":
                    confirmation = payload.get("confirmation")
                    if not isinstance(confirmation, str):
                        raise ValueError("confirmation must be a string")
                    with Library(settings) as library:
                        result = library.clear_workspace(confirmation)
                    self._send_json(asdict(result))
                    return
                match = re.fullmatch(r"/api/photos/(\d+)/tag", path)
                if match:
                    tagger = OmlxTagger(settings.omlx_url, settings.omlx_model)
                    with Library(settings) as library:
                        result = library.tag_photo(int(match.group(1)), tagger)
                    self._send_json(asdict(result) | {"raw_response": None})
                    return
                match = re.fullmatch(r"/api/photos/(\d+)/crop", path)
                if match:
                    if set(payload) != {"candidate_id"} or not isinstance(payload["candidate_id"], str):
                        raise ValueError("crop requires exactly one string candidate_id")
                    try:
                        with Library(settings) as library:
                            edit = library.choose_crop(int(match.group(1)), payload["candidate_id"])
                    except (ValueError, OverflowError, OSError, RuntimeError) as error:
                        raise ValueError("crop could not be selected for this photo") from error
                    self._send_json(edit.to_dict())
                    return
                match = re.fullmatch(r"/api/photos/(\d+)/crop/clear", path)
                if match:
                    if payload:
                        raise ValueError("crop clear requires an empty JSON object")
                    photo_id = int(match.group(1))
                    try:
                        with Library(settings) as library:
                            library.clear_crop(photo_id)
                    except (ValueError, OverflowError, OSError, RuntimeError) as error:
                        raise ValueError("crop could not be cleared for this photo") from error
                    self._send_json({"photo_id": photo_id, "active_crop": None})
                    return
                match = re.fullmatch(r"/api/photos/(\d+)/composition-review", path)
                if match:
                    if payload:
                        raise ValueError("composition review requires an empty JSON object")
                    tagger = OmlxTagger(settings.omlx_url, settings.omlx_model)
                    try:
                        with Library(settings) as library:
                            review = library.review_composition(int(match.group(1)), tagger)
                    except (ValueError, OverflowError, OSError, RuntimeError) as error:
                        raise ValueError("local composition review is unavailable for this photo") from error
                    self._send_json({
                        "candidate_id": review.candidate_id,
                        "rationale": review.rationale,
                        "confidence": review.confidence,
                    })
                    return
                match = re.fullmatch(r"/api/photos/(\d+)/(decision|rating)", path)
                if match:
                    field = match.group(2)
                    if set(payload) != {field}:
                        raise ValueError(f"{field} requires exactly one {field} field")
                    if field == "rating" and type(payload["rating"]) is not int:
                        raise ValueError("rating must be an integer from 0 through 5")
                    with Library(settings) as library:
                        change = library.set_decision if field == "decision" else library.set_rating
                        self._send_json(change(int(match.group(1)), payload[field]))
                    return
                match = re.fullmatch(r"/api/photos/(\d+)/pick", path)
                if match:
                    photo_id = int(match.group(1))
                    with Library(settings) as library:
                        library.set_pick(photo_id)
                    self._send_json({"photo_id": photo_id, "manual_pick": True})
                    return
                if path == "/api/astro/jobs":
                    self._send_json(self._prepare_astro(payload), status=HTTPStatus.CREATED)
                    return
                match = re.fullmatch(r"/api/astro/jobs/(\d+)/(run|cancel|reveal)", path)
                if match:
                    if payload:
                        raise ValueError("this action requires an empty JSON object")
                    job_id, action = int(match.group(1)), match.group(2)
                    if action == "reveal":
                        self._send_json({"path": reveal_astro_job(settings, job_id)})
                    elif action == "run":
                        with Library(settings, job_runner=self.server.job_runner()) as library:
                            library.run_astro_job(job_id)
                            self._send_json(library.job_status(job_id), status=HTTPStatus.ACCEPTED)
                    else:
                        with Library(settings, job_runner=self.server.job_runner()) as library:
                            if not library.cancel_job(job_id):
                                raise ValueError("job is no longer queued or running")
                            self._send_json(library.job_status(job_id))
                    return
                self.send_error(HTTPStatus.NOT_FOUND)
            except CleanupError as error:
                self._send_json(_cleanup_error_payload(error), status=HTTPStatus.BAD_REQUEST)
            except (ValueError, OverflowError, OSError, RuntimeError) as error:
                if isinstance(error, RuntimeError) and "another heavy job" in str(error):
                    self._send_json({"error": "another heavy job is already active"}, status=HTTPStatus.CONFLICT)
                    return
                job_route = path.startswith(("/api/astro/", "/api/jobs/", "/api/import", "/api/ingest", "/api/photos/tag-all",
                                             "/api/previews/"))
                message = public_message(str(error)) if job_route else str(error)
                self._send_json({"error": message}, status=HTTPStatus.BAD_REQUEST)

        def _prepare_astro(self, payload: dict) -> dict:
            allowed = {"group_id", "confirmed", "excluded_photo_ids", "darks", "flats", "biases"}
            if payload.keys() - allowed:
                raise ValueError("unexpected astro fields")
            if payload.get("confirmed") is not True:
                raise ValueError("astro stacking requires explicit photographer confirmation")
            if type(payload.get("group_id")) is not int:
                raise ValueError("group_id must be an integer")
            excluded = payload.get("excluded_photo_ids", [])
            if not isinstance(excluded, list) or any(type(item) is not int for item in excluded):
                raise ValueError("excluded_photo_ids must be a list of integers")
            folders = {}
            for kind in ("darks", "flats", "biases"):
                folder = payload.get(kind)
                if folder is not None and (not isinstance(folder, str) or not folder.strip()):
                    raise ValueError(f"{kind} must be a folder path string or null")
                folders[kind] = Path(folder) if folder is not None else None
            with Library(settings) as library:
                job = library.prepare_astro_job(payload["group_id"], confirmed=True, excluded_photo_ids=excluded, **folders)
                return library.job_status(job.id)

        def _trusted_mutation(self) -> bool:
            if not self._trusted_host():
                return False
            origin_value = self.headers.get("Origin")
            if not origin_value:
                return True
            origin = urlparse(origin_value)
            try:
                origin_port = origin.port or 80
            except ValueError:
                return False
            return origin.scheme == "http" and origin.hostname in {
                "127.0.0.1",
                "::1",
                "localhost",
            } and origin_port == self.server.server_port

        def _trusted_host(self) -> bool:
            host = urlsplit(f"//{self.headers.get('Host', '')}")
            if host.hostname not in {"127.0.0.1", "::1", "localhost"}:
                return False
            try:
                host_port = host.port
            except ValueError:
                return False
            if host_port is not None and host_port != self.server.server_port:
                return False
            return True

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 1_000_000:
                raise ValueError("invalid request size")
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise ValueError("request body must be an object")
            return value

        def _send_json(self, payload, status: HTTPStatus = HTTPStatus.OK) -> None:
            self._send_bytes(json.dumps(payload, default=_json_default).encode(), "application/json; charset=utf-8", status)

        def _send_bytes(
            self,
            content: bytes,
            content_type: str,
            status: HTTPStatus = HTTPStatus.OK,
            cache: bool = False,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "public, max-age=86400" if cache else "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'")
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, format: str, *args: object) -> None:
            return

    return LumenServer((host, port), Handler, settings)

