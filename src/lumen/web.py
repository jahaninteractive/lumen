from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, urlsplit

from .cli import _cleanup_error_payload, _json_default, _serializable_groups
from .config import Settings
from .diagnostics import doctor_report
from .library import CleanupError, ExportRequest, Library
from .omlx import OmlxTagger


STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
}


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


def reveal_outputs(settings: Settings) -> str:
    root = settings.output_dir.resolve()
    destination = settings.jpeg_output_dir.resolve()
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
                self._send_json(doctor_report(settings) | {"jpeg_output_dir": str(settings.jpeg_output_dir.resolve())})
                return
            if path == "/api/groups":
                with Library(settings) as library:
                    self._send_json({"groups": _serializable_groups(library.list_groups()), "photo_count": library.photo_count()})
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
                        "preview_url": f"/preview/{photo_id}?v={photo.fingerprint[:16]}",
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
                preview_version = photo["fingerprint"][:16]
                photo.pop("path", None)
                photo.pop("fingerprint", None)
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
                    source = payload.get("path")
                    if not isinstance(source, str) or not source.strip():
                        raise ValueError("path is required")
                    with Library(settings) as library:
                        result = library.import_path(Path(source))
                    self._send_json(asdict(result), status=HTTPStatus.CREATED)
                    return
                if path == "/api/export":
                    scope = payload.get("scope")
                    if scope not in ("current", "keepers"):
                        raise ValueError("scope must be current or keepers")
                    allowed = {"scope", "profile", "photo_id"} if scope == "current" else {"scope", "profile"}
                    if payload.keys() - allowed:
                        raise ValueError("unexpected export selectors or fields")
                    if scope == "current" and type(payload.get("photo_id")) is not int:
                        raise ValueError("photo_id must be an integer")
                    profile = payload.get("profile")
                    if profile is not None and not isinstance(profile, str):
                        raise ValueError("profile must be a path string or null")
                    with Library(settings) as library:
                        photo_ids = (payload["photo_id"],) if scope == "current" else tuple(library.keeper_ids())
                        results = library.export_jpegs(ExportRequest(photo_ids, Path(profile) if profile is not None else None))
                    status = HTTPStatus.OK if all(result.success for result in results) else HTTPStatus.BAD_REQUEST
                    self._send_json({"results": [asdict(result) for result in results]}, status=status)
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
                match = re.fullmatch(r"/api/photos/(\d+)/pick", path)
                if match:
                    photo_id = int(match.group(1))
                    with Library(settings) as library:
                        library.set_pick(photo_id)
                    self._send_json({"photo_id": photo_id, "manual_pick": True})
                    return
                self.send_error(HTTPStatus.NOT_FOUND)
            except CleanupError as error:
                self._send_json(_cleanup_error_payload(error), status=HTTPStatus.BAD_REQUEST)
            except (ValueError, OverflowError, OSError, RuntimeError) as error:
                self._send_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)

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

    return ThreadingHTTPServer((host, port), Handler)

