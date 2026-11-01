from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import threading
from dataclasses import asdict, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlparse, urlsplit

from .astro import RECIPES
from .catalog import GroupFilters
from .cli import _cleanup_error_payload, _json_default, _serializable_groups, preview_version
from .config import Settings
from .diagnostics import doctor_report, stale_sources
from .digest import parse_day
from .domain import MANUAL_CROP_ID, DevelopSettings
from .jobs import LocalJobRunner
from .library import CleanupError, ExportRequest, Library, public_message
from .omlx import OmlxError
from .pipelines import EXPORT_PRESETS, validate_watermark
from .previews import PreviewError, full_size_for, read_exif_summary, thumbnail_for
from .rawdevelop import DevelopError, render_raw


STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/mark.svg": ("mark.svg", "image/svg+xml"),
}

DEVELOP_FEEDBACK_MAX = 200  # one short correction ("too warm"), matching the field's maxlength

_FLAG_FILTERS = {"keepers_only", "tagged_only", "export_failed_only", "show_rejected"}
OVERLAY_TEXT_MAX = 20_000_000  # characters; the request body limit leaves room for JSON escaping
_MAP_BUILD_LOCK = threading.Lock()  # one index build at a time; other viewport requests answer {"building": true}
_GEOTAG_FIELDS = {"query", "offset", "max_gap_minutes", "dry_run", "replace"}


def _parse_bbox(query: str) -> tuple[tuple[float, float, float, float], float]:
    """`bbox=S,W,N,E&zoom=Z` as ((S, N, W, E), zoom); W > E is a viewport across 180 degrees."""
    values = parse_qs(query)
    try:
        south, west, north, east = (float(part) for part in values["bbox"][0].split(","))
    except (KeyError, ValueError):
        raise ValueError("bbox must be S,W,N,E in degrees") from None
    if not (-90 <= south < north <= 90 and -180 <= west <= 180 and -180 <= east <= 180):  # also refuses nan
        raise ValueError("bbox must be S,W,N,E in degrees")
    try:
        zoom = float(values["zoom"][0])
    except (KeyError, ValueError):
        zoom = -1.0
    if not 0 <= zoom <= 20:
        raise ValueError("zoom must be between 0 and 20")
    return (south, north, west, east), zoom


def _preview_url(photo, crop) -> str:
    """The frame's versioned preview URL; the thumb tier is cropped, so the selected crop versions it too."""
    stamp = crop.selected_at.isoformat() if crop else None
    return f"/preview/{photo.id}?v={preview_version(photo.fingerprint, photo.preview_kind, photo.preview_path, stamp)}"


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


def _tag_scope(payload: dict) -> tuple[str | list[int], bool]:
    """Strict `/api/photos/tag` body: scope is keepers, all, or distinct positive photo IDs; retag is optional."""
    if payload.keys() - {"scope", "retag"} or "scope" not in payload:
        raise ValueError("tag requires scope and optional retag only")
    scope, retag = payload["scope"], payload.get("retag", False)
    if type(retag) is not bool:
        raise ValueError("retag must be true or false")
    if scope in ("keepers", "all"):
        return scope, retag
    if (isinstance(scope, list) and scope and len(set(scope)) == len(scope)
            and all(type(item) is int and item > 0 for item in scope)):
        return scope, retag
    raise ValueError("scope must be keepers, all, or a list of distinct photo IDs")


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
        self.settings = settings
        self._runner_lock = threading.Lock()  # before bind: a failed bind calls server_close, which takes this lock
        self._runner: LocalJobRunner | None = None
        super().__init__(address, handler)
        # Eager: takes the catalog's runner lock for the server's lifetime and recovers stale running jobs now.
        try:
            self._runner = LocalJobRunner(settings.database_path)
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


def developed_full_size(settings: Settings, source: Path, full_dir: Path, fingerprint: str, develop: DevelopSettings) -> Path:
    """The full-size render of a developed frame, cached beside the embedded JPEGs under a name that carries the
    settings, so changing them never serves a stale file. Housekeeping prunes it with the rest of that cache; the next loupe open re-renders."""
    digest = hashlib.sha256(json.dumps(develop.to_dict(), sort_keys=True).encode()).hexdigest()[:12]
    target = full_dir / f"{fingerprint}-{digest}.jpg"
    if target.is_file():
        return target
    source.stat()  # an unreadable original with no cached render raises OSError, as for the embedded JPEG
    full_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".develop-full-", dir=full_dir) as directory:
        rendered = Path(directory) / "full.jpg"
        render_raw(source, rendered, settings.raw_developer_path, settings=develop.to_dict(), image_format="jpeg")
        rendered.replace(target)
    return target


def make_server(settings: Settings, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("Lumen only binds to a loopback address")
    static_dir = Path(__file__).parent / "static"

    # The switches offer the full and fast models only, never a typed name; fixed at startup so a switch cannot shrink the choice.
    role_models = list(dict.fromkeys((settings.omlx_model, settings.omlx_fast_model)))
    role_fields = {"fast": "omlx_fast_model", "review": "omlx_review_model"}  # role -> Settings field a switch rebinds

    class Handler(BaseHTTPRequestHandler):
        def handle_one_request(self) -> None:
            # The browser abandons a render whenever the photographer moves a slider again,
            # which leaves a dead socket part-way through the reply. Routine, not a fault,
            # but it reached socketserver as an unhandled error and printed two stack traces
            # per cancelled render — enough noise to bury a real one. There is nobody left to
            # answer, so close quietly. Caught here rather than in each route because every
            # route's own OSError handler would otherwise try to send a 400 down the same
            # dead socket and fail again.
            try:
                super().handle_one_request()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

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
                    "review_models": role_models,
                    "stale": stale_sources(settings),
                    "jpeg_output_dir": str(settings.jpeg_output_dir.resolve()),
                    "astro_output_dir": str((settings.output_dir / "astro").resolve()),
                    "ingest_output_dir": str((settings.output_dir / "ingest").resolve()),
                    "digest_dir": str(settings.digest_dir.resolve()),
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
                    for group in groups:  # Advisory text cue from stored triage; never a model call or mode switch.
                        group["astro_cue"] = library.astro_cue([photo["id"] for photo in group["photos"]])
                        for photo in group["photos"]:  # Why each frame matched the find text; always present.
                            photo["matched"] = library.catalog.match_terms(photo["id"], filters.text) if filters.text else []
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
            if path == "/api/library/cleared":
                try:
                    with Library(settings) as library:
                        self._send_json(library.cleared_in_catalog())
                except (ValueError, OSError) as error:
                    self._send_json({"error": public_message(str(error))}, status=HTTPStatus.BAD_REQUEST)
                return
            if path == "/api/map/features":
                try:
                    box, zoom = _parse_bbox(urlparse(self.path).query)
                except ValueError as error:
                    self._send_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
                    return
                if not _MAP_BUILD_LOCK.acquire(blocking=False):
                    self._send_json({"building": True})
                    return
                try:
                    with Library(settings) as library:
                        self._send_json(library.map_features(*box, zoom=zoom))
                except (ValueError, OSError) as error:
                    self._send_json({"error": public_message(str(error))}, status=HTTPStatus.BAD_REQUEST)
                finally:
                    _MAP_BUILD_LOCK.release()
                return
            if path in {"/api/map/photos", "/api/map/overlays"}:
                try:
                    with Library(settings) as library:
                        if path == "/api/map/overlays":
                            self._send_json({"overlays": library.list_overlays()})
                        else:
                            photos = library.map_photos(parse_group_filters(urlparse(self.path).query))
                            self._send_json({"photos": photos, "count": len(photos)})
                except (ValueError, OSError) as error:
                    self._send_json({"error": public_message(str(error))}, status=HTTPStatus.BAD_REQUEST)
                return
            if path == "/api/vocabulary":
                try:
                    with Library(settings) as library:
                        self._send_json(library.vocabulary_report())
                except (ValueError, OSError) as error:
                    self._send_json({"error": public_message(str(error))}, status=HTTPStatus.BAD_REQUEST)
                return
            if path == "/api/photos/reject-suggestions":
                with Library(settings) as library:
                    suggestions = library.reject_suggestions()
                for item in suggestions:  # The fingerprint only versions the preview URL; it never reaches the browser.
                    version = preview_version(item.pop("fingerprint16"), item.pop("preview_kind"),
                                              item.pop("preview_path"), item.pop("crop_selected_at"))
                    item["preview_url"] = f"/preview/{item['photo_id']}?v={version}"
                self._send_json({"suggestions": suggestions, "count": len(suggestions)})
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
            match = re.fullmatch(r"/api/groups/(\d+)/triage", path)
            if match:
                try:
                    with Library(settings) as library:
                        self._send_json(library.astro_triage(int(match.group(1))))
                except (ValueError, OverflowError):
                    self._send_json({"error": "unknown burst"}, status=HTTPStatus.NOT_FOUND)
                return
            match = re.fullmatch(r"/api/groups/(\d+)/critique", path)
            if match:
                with Library(settings) as library:
                    self._send_json({"critique": library.latest_critique(int(match.group(1)))})
                return
            match = re.fullmatch(r"/api/photos/(\d+)/exif", path)
            if match:
                with Library(settings) as library:
                    photo = library.catalog.get_photo(int(match.group(1)))
                if not photo:
                    self._send_json({"error": "unknown photo"}, status=HTTPStatus.NOT_FOUND)
                    return
                if not Path(photo.path).is_file():
                    self._send_json({"error": "the original is offline"}, status=HTTPStatus.SERVICE_UNAVAILABLE)
                    return
                try:
                    fields = read_exif_summary(Path(photo.path))
                except PreviewError as error:
                    self._send_json({"error": str(error)}, status=HTTPStatus.SERVICE_UNAVAILABLE)
                    return
                self._send_json({"photo_id": photo.id, "fields": [{"label": label, "value": value} for label, value in fields]})
                return
            match = re.fullmatch(r"/api/photos/(\d+)/crops", path)
            if match:
                try:
                    photo_id = int(match.group(1))
                    with Library(settings) as library:
                        candidates = library.crop_candidates(photo_id)
                        photo = library.catalog.get_photo(photo_id)
                        active = library.catalog.get_crop(photo_id)
                        review = library.latest_composition_review(photo_id, [candidate.id for candidate in candidates])
                    if photo is None:
                        raise ValueError(f"unknown photo ID: {photo_id}")
                    self._send_json({
                        "photo_id": photo_id,
                        "preview_url": _preview_url(photo, active),
                        "candidates": [asdict(candidate) for candidate in candidates],
                        "active_crop": active.to_dict() if active else None,
                        "review": review,
                        "source_width": candidates[0].pixel_box[2],
                        "source_height": candidates[0].pixel_box[3],
                    })
                except (ValueError, OverflowError, OSError, RuntimeError):
                    self._send_json({"error": "crop choices are unavailable for this photo"}, status=HTTPStatus.BAD_REQUEST)
                return
            match = re.fullmatch(r"/api/photos/(\d+)", path)
            if match:
                with Library(settings) as library:
                    item = library.get_photo(int(match.group(1)))
                    develop = library.develop_for(item["photo"].id) if item else None
                    crop = library.catalog.get_crop(item["photo"].id) if item else None
                    location_typed = library.location_typed(item["photo"]) if item else False
                    advice = library.latest_develop_advice(item["photo"].id) if item else None
                if not item:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                preview_url = _preview_url(item["photo"], crop)
                photo = asdict(item["photo"])
                # The Develop tab needs both before it offers a control: every render reads the original.
                photo["offline"] = not Path(photo["path"]).is_file()
                for key in ("path", "fingerprint", "preview_path", "mirror_path"):
                    photo.pop(key, None)
                photo["preview_url"] = preview_url
                photo["location_typed"] = location_typed  # only a position typed in Lumen can be edited
                self._send_json({"photo": photo, "develop": develop and develop.to_dict(),
                                 "metrics": asdict(item["metrics"]), "tags": item["tags"], "warnings": item["warnings"],
                                 "advice": advice})
                return
            match = re.fullmatch(r"/full/(\d+)", path)
            if match:
                with Library(settings) as library:
                    photo = library.catalog.get_photo(int(match.group(1)))
                    develop = library.develop_for(photo.id) if photo else None
                full_dir = (settings.cache_dir / "full").resolve()
                offline = failed = False
                try:
                    if develop and not develop.is_empty:
                        full = developed_full_size(settings, Path(photo.path), full_dir, photo.fingerprint, develop)
                    else:
                        full = full_size_for(Path(photo.path), full_dir, photo.fingerprint) if photo else None
                    if full:
                        full.resolve().relative_to(full_dir)
                except OSError:  # the original's volume is unplugged and nothing is cached
                    full, offline = None, True
                except ValueError:
                    full = None
                except DevelopError:  # the frame exists but the render broke
                    full, failed = None, True
                if not full:
                    self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR if failed else
                                    HTTPStatus.SERVICE_UNAVAILABLE if offline else HTTPStatus.NOT_FOUND)
                    return
                self._send_bytes(full.read_bytes(), "image/jpeg", cache=True)
                return
            match = re.fullmatch(r"/(preview|thumb)/(\d+)", path)
            if match:
                with Library(settings) as library:
                    photo = library.catalog.get_photo(int(match.group(2)))
                    edit = library.catalog.get_crop(photo.id) if photo else None
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
                if match.group(1) == "thumb":
                    try:
                        preview = thumbnail_for(preview, settings.cache_dir / "thumbs", edit and (
                            edit.rect.left, edit.rect.top, edit.rect.width, edit.rect.height))
                    except (OSError, ValueError):
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                self._send_bytes(preview.read_bytes(), "image/jpeg", cache=True)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def _develop_reply(self, photo_id: int, develop: DevelopSettings | None) -> dict:
            """Save (or, for none or empty settings, clear) and report the frame's new preview URL."""
            with Library(settings) as library:
                if develop is None or develop.is_empty:
                    library.clear_develop(photo_id)
                    develop = None
                else:
                    develop = library.save_develop(photo_id, develop)
                photo = library.catalog.get_photo(photo_id)
                crop = library.catalog.get_crop(photo_id)
            return {"photo_id": photo_id, "develop": develop and develop.to_dict(), "preview_url": _preview_url(photo, crop)}

        def do_DELETE(self) -> None:
            path = urlparse(self.path).path
            if not self._trusted_mutation():
                self._send_json({"error": "cross-origin request rejected"}, status=HTTPStatus.FORBIDDEN)
                return
            if overlay := re.fullmatch(r"/api/map/overlays/([^/]+)", path):
                name = unquote(overlay.group(1))
                try:
                    with Library(settings) as library:
                        library.remove_overlay(name)
                except ValueError as error:
                    self._send_json({"error": str(error)}, status=HTTPStatus.NOT_FOUND)
                    return
                except OSError as error:
                    self._send_json({"error": public_message(str(error))}, status=HTTPStatus.BAD_REQUEST)
                    return
                self._send_json({"removed": name})
                return
            match = re.fullmatch(r"/api/photos/(\d+)/develop", path)
            if not match:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                if self.headers.get("Content-Length", "0") != "0" and self._read_json():
                    raise ValueError("develop clear requires an empty JSON object")
                self._send_json(self._develop_reply(int(match.group(1)), None))
            except (ValueError, OverflowError, OSError, RuntimeError) as error:
                self._send_json({"error": public_message(str(error))}, status=HTTPStatus.BAD_REQUEST)

        def do_POST(self) -> None:
            nonlocal settings  # the role-model switch rebinds it; every later request builds its Library from it
            path = urlparse(self.path).path
            if not self._trusted_mutation():
                self._send_json({"error": "cross-origin request rejected"}, status=HTTPStatus.FORBIDDEN)
                return
            try:
                payload = self._read_json(limit=25_000_000 if path == "/api/map/overlays" else 1_000_000)
                if path == "/api/map/overlays":
                    if payload.keys() != {"name", "text"} or not isinstance(payload["name"], str) or not isinstance(payload["text"], str):
                        raise ValueError("overlay requires a name and text")
                    if len(payload["text"]) > OVERLAY_TEXT_MAX:
                        raise ValueError("overlay larger than 20 MB")
                    with Library(settings) as library:
                        self._send_json(library.add_overlay(payload["name"], payload["text"]), status=HTTPStatus.CREATED)
                    return
                if overlay := re.fullmatch(r"/api/map/overlays/([^/]+)/geotag", path):
                    if payload.keys() != _GEOTAG_FIELDS:
                        raise ValueError(f"geotag requires {', '.join(sorted(_GEOTAG_FIELDS))}")
                    if not isinstance(payload["query"], str):
                        raise ValueError("query must be a string")
                    if payload["offset"] is not None and not isinstance(payload["offset"], str):
                        raise ValueError("offset must be a string or null")
                    gap = payload["max_gap_minutes"]
                    if isinstance(gap, bool) or not isinstance(gap, int) or not 1 <= gap <= 120:
                        raise ValueError("max_gap_minutes must be a whole number from 1 to 120")
                    if not isinstance(payload["dry_run"], bool) or not isinstance(payload["replace"], bool):
                        raise ValueError("dry_run and replace must be true or false")
                    filters = parse_group_filters(payload["query"].removeprefix("?"))
                    with Library(settings) as library:
                        self._send_json(library.geotag_from_track(
                            unquote(overlay.group(1)), filters=filters, offset=payload["offset"], max_gap_minutes=gap,
                            dry_run=payload["dry_run"], replace=payload["replace"]))
                    return
                if path == "/api/browse":
                    selected = choose_macos_folder()
                    self._send_json({"path": selected, "cancelled": selected is None})
                    return
                if path == "/api/role-model":
                    if payload.keys() != {"role", "model"} or payload["role"] not in role_fields:
                        raise ValueError("role must be fast or review, with a model")
                    if payload["model"] not in role_models:
                        raise ValueError(f"model must be one of {', '.join(role_models)}")
                    settings = replace(settings, **{role_fields[payload["role"]]: payload["model"]})
                    self._send_json({"role": payload["role"], "model": payload["model"]})
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
                    if payload.keys() - {"path", "include_cleared"}:
                        raise ValueError("unexpected import fields")
                    source = payload.get("path")
                    if not isinstance(source, str) or not source.strip():
                        raise ValueError("path is required")
                    include_cleared = payload.get("include_cleared", False)
                    if type(include_cleared) is not bool:
                        raise ValueError("include_cleared must be true or false")
                    with Library(settings, job_runner=self.server.job_runner()) as library:
                        job = library.start_import_job(Path(source), include_cleared=include_cleared)
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
                if path in ("/api/photos/tag", "/api/photos/tag-all"):
                    if path.endswith("-all"):  # Older alias: the whole catalog.
                        if "scope" in payload:
                            raise ValueError("unexpected tag fields")
                        payload = payload | {"scope": "all"}
                    scope, retag = _tag_scope(payload)
                    with Library(settings, job_runner=self.server.job_runner()) as library:
                        job = library.start_tag_job(library.tagger_for("fast"), scope=scope, retag=retag)
                        self._send_json(library.job_status(job.id), status=HTTPStatus.ACCEPTED)
                    return
                if path == "/api/photos/assist":
                    if payload.keys() - {"scope", "redo"} or "scope" not in payload:
                        raise ValueError("assist requires scope and optional redo only")
                    redo = payload.get("redo", False)
                    if type(redo) is not bool:
                        raise ValueError("redo must be true or false")
                    scope, _ = _tag_scope({"scope": payload["scope"]})
                    with Library(settings, job_runner=self.server.job_runner()) as library:
                        fast, review = library.tagger_for("fast"), library.tagger_for("review")  # refusals reach the browser verbatim
                        job = library.start_assist_job(fast, review, scope=scope, redo=redo)
                        self._send_json(library.job_status(job.id), status=HTTPStatus.ACCEPTED)
                    return
                if path == "/api/photos/review-rejects":
                    if payload.keys() != {"scope"}:
                        raise ValueError("review-rejects requires only scope")
                    scope, _ = _tag_scope(payload)
                    with Library(settings, job_runner=self.server.job_runner()) as library:
                        job = library.start_reject_review_job(library.tagger_for("fast"), scope=scope)
                        self._send_json(library.job_status(job.id), status=HTTPStatus.ACCEPTED)
                    return
                if path == "/api/photos/reject-suggestions/decide":
                    if payload.keys() != {"photo_ids", "decision"}:
                        raise ValueError("decide requires exactly photo_ids and decision")
                    ids = payload["photo_ids"]
                    if not isinstance(ids, list) or not ids or not all(type(item) is int and item > 0 for item in ids):
                        raise ValueError("photo_ids must be a non-empty list of photo IDs")
                    with Library(settings) as library:
                        self._send_json(library.apply_reject_suggestions(ids, payload["decision"]))
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
                    if scope not in ("current", "keepers", "filtered"):
                        raise ValueError("scope must be current, keepers, or filtered")
                    common = {"scope", "profile", "preset", "watermark", "keep_original", "dated_folder"}
                    filters = None
                    if scope == "filtered":  # The rest of the body is the contact sheet's filter fields, validated like /api/groups.
                        fields = {key: value for key, value in payload.items() if key not in common}
                        if not all(isinstance(value, str) for value in fields.values()):
                            raise ValueError("filter values must be strings")
                        filters = parse_group_filters(urlencode(fields))
                    elif payload.keys() - (common | {"photo_id"} if scope == "current" else common):
                        raise ValueError("unexpected export selectors or fields")
                    if scope == "current" and type(payload.get("photo_id")) is not int:
                        raise ValueError("photo_id must be an integer")
                    profile = payload.get("profile")
                    if profile is not None and not isinstance(profile, str):
                        raise ValueError("profile must be a path string or null")
                    preset = payload.get("preset", "full")
                    if not isinstance(preset, str) or preset not in EXPORT_PRESETS:
                        raise ValueError("preset must be full, share, or web")
                    watermark = validate_watermark(payload.get("watermark"))
                    keep_original = payload.get("keep_original", False)
                    if type(keep_original) is not bool:
                        raise ValueError("keep_original must be true or false")
                    dated_folder = payload.get("dated_folder", True)
                    if type(dated_folder) is not bool:
                        raise ValueError("dated_folder must be true or false")
                    profile_path = Path(profile) if profile is not None else None
                    if scope != "current":  # Many renders: a background job with progress and cancel.
                        with Library(settings, job_runner=self.server.job_runner()) as library:
                            ids = library.keeper_ids() if scope == "keepers" else library.catalog.photos_for_filters(filters)
                            if not ids and scope == "filtered":
                                raise ValueError("no frames match these filters")
                            job = library.start_export_job(ExportRequest(tuple(ids), profile_path, preset, watermark, keep_original, dated_folder))
                            self._send_json(library.job_status(job.id), status=HTTPStatus.ACCEPTED)
                        return
                    with Library(settings) as library:
                        results = library.export_jpegs(ExportRequest((payload["photo_id"],), profile_path, preset, watermark, keep_original, dated_folder))
                    status = HTTPStatus.OK if all(result.success for result in results) else HTTPStatus.BAD_REQUEST
                    # Only what the browser reads. The Artifact record would carry the source
                    # fingerprint, the profile path and the render command, which stay on the server.
                    self._send_json({"results": [
                        {"photo_id": result.photo_id, "success": result.success,
                         "output": str(result.output) if result.output else None, "message": result.message}
                        for result in results]}, status=status)
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
                if path == "/api/library/remove-cleared":
                    if payload:
                        raise ValueError("remove-cleared requires an empty JSON object")
                    with Library(settings) as library:
                        result = library.remove_cleared()
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
                    with Library(settings) as library:
                        result = library.tag_photo(int(match.group(1)), library.tagger_for("fast"))
                    self._send_json(asdict(result) | {"raw_response": None})
                    return
                if path == "/api/vocabulary/forget":
                    if payload.keys() != {"word"} or not isinstance(payload["word"], str) or not payload["word"].strip():
                        raise ValueError("forget requires exactly one non-blank string word")
                    with Library(settings) as library:
                        self._send_json(library.forget_keyword(payload["word"]))
                    return
                match = re.fullmatch(r"/api/photos/(\d+)/keywords", path)
                if match:
                    if (payload.keys() != {"action", "keyword"} or payload["action"] not in ("add", "remove")
                            or not isinstance(payload["keyword"], str)):
                        raise ValueError("keywords requires action add or remove and a string keyword")
                    with Library(settings) as library:
                        change = library.add_keyword if payload["action"] == "add" else library.remove_keyword
                        self._send_json(change(int(match.group(1)), payload["keyword"]))
                    return
                match = re.fullmatch(r"/api/photos/(\d+)/crop", path)
                if match:
                    manual = payload.get("candidate_id") == MANUAL_CROP_ID
                    if manual:
                        rect = payload.get("rect")
                        if (set(payload) != {"candidate_id", "rect", "aspect"} or not isinstance(rect, dict)
                                or set(rect) != {"left", "top", "width", "height"}
                                or any(type(value) not in (int, float) for value in rect.values())
                                or not isinstance(payload["aspect"], str)):
                            raise ValueError("a drawn crop requires candidate_id, rect {left, top, width, height}, and aspect")
                    elif set(payload) != {"candidate_id"} or not isinstance(payload["candidate_id"], str):
                        raise ValueError("crop requires exactly one string candidate_id")
                    extra = {"rect": payload["rect"], "aspect": payload["aspect"]} if manual else {}
                    try:
                        with Library(settings) as library:
                            edit = library.choose_crop(int(match.group(1)), payload["candidate_id"], **extra)
                            photo = library.catalog.get_photo(edit.photo_id)
                    except (ValueError, OverflowError, OSError, RuntimeError) as error:
                        raise ValueError("crop could not be selected for this photo") from error
                    self._send_json({**edit.to_dict(), "preview_url": _preview_url(photo, edit)})
                    return
                match = re.fullmatch(r"/api/photos/(\d+)/crop/clear", path)
                if match:
                    if payload:
                        raise ValueError("crop clear requires an empty JSON object")
                    photo_id = int(match.group(1))
                    try:
                        with Library(settings) as library:
                            library.clear_crop(photo_id)
                            photo = library.catalog.get_photo(photo_id)
                    except (ValueError, OverflowError, OSError, RuntimeError) as error:
                        raise ValueError("crop could not be cleared for this photo") from error
                    self._send_json({"photo_id": photo_id, "active_crop": None, "preview_url": _preview_url(photo, None)})
                    return
                if match := re.fullmatch(r"/api/photos/(\d+)/develop/apply-to-group", path):
                    if payload:
                        raise ValueError("apply to burst requires an empty JSON object")
                    with Library(settings, job_runner=self.server.job_runner()) as library:
                        job = library.start_develop_group_job(int(match.group(1)))
                        self._send_json(library.job_status(job.id), status=HTTPStatus.ACCEPTED)
                    return
                match = re.fullmatch(r"/api/photos/(\d+)/develop/suggest", path)
                if match:
                    if payload.keys() != {"settings", "feedback"}:
                        raise ValueError("develop suggest requires exactly settings and feedback")
                    feedback = payload["feedback"]
                    if feedback is not None and (not isinstance(feedback, str) or len(feedback) > DEVELOP_FEEDBACK_MAX):
                        raise ValueError(f"feedback must be null or at most {DEVELOP_FEEDBACK_MAX} characters")
                    try:
                        develop = DevelopSettings.from_dict(payload["settings"])
                    except ValueError as error:
                        raise ValueError(f"develop settings are invalid: {error}") from error
                    with Library(settings) as library:
                        tagger = library.tagger_for("review")  # A not-loaded refusal reaches the browser verbatim.
                        # Only the settings and the rationale go back: the raw model output stays in provenance.
                        advice = library.suggest_develop(int(match.group(1)), tagger, settings=develop,
                                                         feedback=(feedback or "").strip() or None)
                    self._send_json({"settings": advice.settings, "rationale": advice.rationale})
                    return
                match = re.fullmatch(r"/api/photos/(\d+)/develop(/preview)?", path)
                if match:
                    preview = match.group(2) is not None
                    if payload.keys() != ({"settings", "long_edge"} if preview else {"settings"}):
                        raise ValueError("develop requires exactly " + ("settings and long_edge" if preview else "settings"))
                    try:
                        develop = DevelopSettings.from_dict(payload["settings"])
                    except ValueError as error:
                        raise ValueError(f"develop settings are invalid: {error}") from error
                    photo_id = int(match.group(1))
                    if not preview:
                        self._send_json(self._develop_reply(photo_id, develop))
                        return
                    edge = payload["long_edge"]
                    # Zero means the raw's own size: zoomed to 1:1 the panel needs the pixels
                    # sharpening and noise reduction act on, which no scaled render can show.
                    if type(edge) is not int or edge < 0:
                        raise ValueError("long_edge must be zero or a positive integer")
                    with Library(settings) as library:
                        photo = library.catalog.get_photo(photo_id)
                        if not photo:
                            raise ValueError("unknown photo")
                        # The viewport clamp bounds an arbitrary number; a native request is
                        # already bounded by the file's own dimensions.
                        edge = edge if edge == 0 else min(edge, library.previews.max_size)
                    with tempfile.TemporaryDirectory(prefix=".develop-preview-", dir=settings.cache_dir) as directory:
                        rendered = render_raw(Path(photo.path), Path(directory) / "preview.jpg", settings.raw_developer_path,
                                              settings=develop.to_dict(), long_edge=edge, image_format="jpeg", histogram=True)
                        image = base64.b64encode(rendered.path.read_bytes()).decode()
                    self._send_json({"image": "data:image/jpeg;base64," + image, "histogram": {channel: list(buckets) for channel, buckets in rendered.histogram.items()},
                                     "defaults": dict(rendered.defaults), "lens_correction": rendered.lens_correction_supported,
                                     "neutral_temperature": rendered.neutral_temperature, "neutral_tint": rendered.neutral_tint})
                    return
                match = re.fullmatch(r"/api/photos/(\d+)/composition-review", path)
                if match:
                    if payload:
                        raise ValueError("composition review requires an empty JSON object")
                    with Library(settings) as library:
                        tagger = library.tagger_for("review")  # A not-loaded refusal reaches the browser verbatim.
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
                if path == "/api/digests":
                    if payload.keys() - {"date", "overwrite"} or type(payload.get("overwrite", False)) is not bool:
                        raise ValueError("digest requires date (YYYY-MM-DD) and an optional boolean overwrite")
                    day = parse_day(payload.get("date"))
                    with Library(settings) as library:
                        try:  # Soft gate: without the review model the digest is facts only.
                            tagger, reason = library.tagger_for("review"), None
                        except RuntimeError as error:
                            tagger, reason = None, str(error)
                        try:
                            result = library.digest_day(day, tagger, overwrite=payload.get("overwrite", False), unavailable_reason=reason)
                        except FileExistsError as error:
                            self._send_json({"error": str(error), "exists": True}, status=HTTPStatus.CONFLICT)
                            return
                    self._send_json(result)
                    return
                if path == "/api/search/expand":
                    if payload.keys() != {"query"}:
                        raise ValueError("search expansion requires only a query")
                    with Library(settings) as library:
                        tagger = library.tagger_for("fast")  # A not-loaded refusal reaches the browser verbatim.
                        try:
                            result = library.expand_search(payload["query"], tagger)
                        except OmlxError as error:
                            raise ValueError(public_message(str(error))) from error
                    self._send_json(result)
                    return
                match = re.fullmatch(r"/api/photos/(\d+)/caption", path)
                if match:
                    if payload:
                        raise ValueError("caption requires an empty JSON object")
                    with Library(settings) as library:
                        tagger = library.tagger_for("review")  # A not-loaded refusal reaches the browser verbatim.
                        try:
                            caption = library.caption_photo(int(match.group(1)), tagger)
                        except (ValueError, OverflowError, OSError, RuntimeError) as error:
                            raise ValueError(public_message(str(error))) from error
                    self._send_json(caption)
                    return
                match = re.fullmatch(r"/api/photos/(\d+)/caption/edit", path)
                if match:
                    if payload.keys() != {"caption", "alt_text"} or not all(
                            value is None or isinstance(value, str) for value in payload.values()):
                        raise ValueError("caption edit requires caption and alt_text, each a string or null")
                    with Library(settings) as library:
                        self._send_json(library.set_caption(
                            int(match.group(1)), caption=payload["caption"], alt_text=payload["alt_text"]))
                    return
                match = re.fullmatch(r"/api/photos/(\d+)/place", path)
                if match:
                    if payload.keys() != {"name", "country", "scope", "remember", "radius_km"} or not (
                            isinstance(payload["name"], str) and isinstance(payload["country"], str)
                            and isinstance(payload["scope"], str) and isinstance(payload["remember"], bool)
                            and isinstance(payload["radius_km"], (int, float)) and not isinstance(payload["radius_km"], bool)):
                        raise ValueError("place requires name, country and scope strings, a remember boolean and a radius_km number")
                    with Library(settings) as library:
                        self._send_json(library.set_place(int(match.group(1)), payload["name"], payload["country"],
                                                          scope=payload["scope"], remember=payload["remember"],
                                                          radius_km=float(payload["radius_km"])))
                    return
                match = re.fullmatch(r"/api/photos/(\d+)/location", path)
                if match:
                    if payload.keys() != {"position", "scope"} or not (
                            isinstance(payload["position"], str) and len(payload["position"]) <= 80
                            and isinstance(payload["scope"], str)):
                        raise ValueError("location requires a position string of at most 80 characters and a scope string")
                    with Library(settings) as library:
                        self._send_json(library.set_location(int(match.group(1)), payload["position"], scope=payload["scope"]))
                    return
                match = re.fullmatch(r"/api/groups/(\d+)/triage", path)
                if match:
                    if payload:
                        raise ValueError("astro triage requires an empty JSON object")
                    group_id = int(match.group(1))
                    with Library(settings, job_runner=self.server.job_runner()) as library:
                        if not any(group["id"] == group_id for group in library.list_groups()):
                            self._send_json({"error": "unknown burst"}, status=HTTPStatus.NOT_FOUND)
                            return
                        job = library.start_triage_job(library.tagger_for("fast"), group_id=group_id)
                        self._send_json(library.job_status(job.id), status=HTTPStatus.ACCEPTED)
                    return
                match = re.fullmatch(r"/api/groups/(\d+)/critique", path)
                if match:
                    if payload:
                        raise ValueError("burst critique requires an empty JSON object")
                    group_id = int(match.group(1))
                    with Library(settings) as library:
                        if not any(group["id"] == group_id for group in library.list_groups()):
                            self._send_json({"error": "unknown burst"}, status=HTTPStatus.NOT_FOUND)
                            return
                        tagger = library.tagger_for("review")  # A not-loaded refusal reaches the browser verbatim.
                        try:
                            critique = library.critique_burst(group_id, tagger)
                        except (ValueError, OverflowError, OSError, RuntimeError) as error:
                            raise ValueError(public_message(str(error))) from error
                    self._send_json(critique)
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
                match = re.fullmatch(r"/api/photos/(\d+)/split", path)
                if match:
                    if payload:
                        raise ValueError("split requires an empty JSON object")
                    with Library(settings) as library:
                        self._send_json(library.split_photo(int(match.group(1))))
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
                job_route = path.startswith(("/api/map/", "/api/astro/", "/api/jobs/", "/api/import", "/api/ingest", "/api/photos/tag", "/api/photos/assist",
                                             "/api/photos/review-rejects", "/api/previews/", "/api/digests", "/api/groups/")) or re.search(r"/develop(/|$)", path)
                message = public_message(str(error)) if job_route else str(error)
                self._send_json({"error": message}, status=HTTPStatus.BAD_REQUEST)

        def _prepare_astro(self, payload: dict) -> dict:
            allowed = {"group_id", "confirmed", "excluded_photo_ids", "darks", "flats", "biases", "recipe"}
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
            recipe = payload.get("recipe", "tracked")
            if type(recipe) is not str or recipe not in RECIPES:
                raise ValueError("unknown recipe")
            with Library(settings) as library:
                job = library.prepare_astro_job(payload["group_id"], confirmed=True, excluded_photo_ids=excluded,
                                                recipe=recipe, **folders)
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

        def _read_json(self, limit: int = 1_000_000) -> dict:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > limit:
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

