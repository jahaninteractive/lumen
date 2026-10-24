from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageOps

from .tools import find_tool


class PreviewError(RuntimeError):
    pass


def read_image_dimensions(path: Path) -> tuple[int, int]:
    """Return full-resolution dimensions after EXIF orientation, never preview size."""
    try:
        with Image.open(path) as image:
            width, height = image.size
            orientation = image.getexif().get(274, 1)
    except (OSError, ValueError):
        width = height = orientation = None
    if width is None:
        exiftool = shutil.which("exiftool")
        if not exiftool:
            raise ValueError(f"could not determine source dimensions for {path.name}")
        try:
            result = subprocess.run(
                [exiftool, "-json", "-n", "-ImageWidth", "-ImageHeight", "-Orientation", str(path)],
                capture_output=True, text=True, timeout=30, check=False,
            )
            payload = json.loads(result.stdout) if result.returncode == 0 else None
            item = payload[0] if isinstance(payload, list) and payload and isinstance(payload[0], dict) else None
            width, height, orientation = (
                item.get("ImageWidth"), item.get("ImageHeight"), item.get("Orientation")
            ) if item else (None, None, None)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError, TypeError, IndexError):
            width = height = orientation = None
    if (type(width) is not int or type(height) is not int or width <= 0 or height <= 0
            or type(orientation) is not int or orientation not in range(1, 9)):
        raise ValueError(f"could not determine source dimensions for {path.name}")
    if orientation in {5, 6, 7, 8}:
        return height, width
    return width, height


def fingerprint_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ExifToolPreviewProvider:
    def __init__(self, max_size: int = 2048):
        self.max_size = max_size

    def create_preview(self, source: Path, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            return destination

        suffix = source.suffix.lower()
        if suffix in {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic"}:
            try:
                return self._normalize(source, destination)
            except OSError:
                pass

        exiftool = shutil.which("exiftool")
        if exiftool:
            for tag in ("PreviewImage", "JpgFromRaw", "ThumbnailImage"):
                try:
                    result = subprocess.run(
                        [exiftool, "-b", f"-{tag}", str(source)],
                        capture_output=True,
                        timeout=60,
                        check=False,
                    )
                except subprocess.SubprocessError:
                    continue
                if result.returncode == 0 and result.stdout.startswith(b"\xff\xd8"):
                    return self._normalize_bytes(result.stdout, destination)

        sips = shutil.which("sips")
        if sips:
            with tempfile.TemporaryDirectory() as directory:
                candidate = Path(directory) / "preview.jpg"
                try:
                    result = subprocess.run(
                        [sips, "-s", "format", "jpeg", str(source), "--out", str(candidate)],
                        capture_output=True,
                        timeout=120,
                        check=False,
                    )
                except subprocess.SubprocessError:
                    result = None
                if result and result.returncode == 0 and candidate.exists():
                    return self._normalize(candidate, destination)

        darktable = find_tool("darktable-cli")
        if darktable:
            with tempfile.TemporaryDirectory() as directory:
                candidate = Path(directory) / "preview.jpg"
                try:
                    result = subprocess.run(
                        [darktable, str(source), str(candidate), "--width", str(self.max_size), "--height", str(self.max_size)],
                        capture_output=True,
                        timeout=300,
                        check=False,
                    )
                except subprocess.SubprocessError:
                    result = None
                if result and result.returncode == 0 and candidate.exists():
                    return self._normalize(candidate, destination)
        raise PreviewError(f"No local preview provider could decode {source.name}")

    def create_refined_preview(self, source: Path, destination: Path, *, darktable: str, runner=subprocess.run) -> Path:
        """Render the RAW with darktable into a temporary beside `destination`, then normalise it atomically into place."""
        with tempfile.TemporaryDirectory(prefix=".refine-", dir=destination.parent) as directory:
            candidate = Path(directory) / "render.jpg"
            try:
                result = runner(
                    [darktable, str(source), str(candidate), "--width", str(self.max_size), "--height", str(self.max_size),
                     "--core", "--conf", "plugins/lighttable/export/metadata_flags=5"],
                    capture_output=True, timeout=300, check=False,
                )
            except subprocess.TimeoutExpired as error:
                raise PreviewError(f"darktable timed out refining {source.name}") from error
            except (OSError, subprocess.SubprocessError) as error:
                raise PreviewError(f"darktable could not start for {source.name}") from error
            if result.returncode != 0:
                raise PreviewError(f"darktable exited with code {result.returncode} refining {source.name}")
            if not candidate.is_file():
                raise PreviewError(f"darktable wrote no preview for {source.name}")
            try:
                return self._normalize(candidate, destination)
            except OSError as error:
                destination.with_suffix(destination.suffix + ".tmp").unlink(missing_ok=True)
                raise PreviewError(f"darktable's preview for {source.name} could not be read") from error

    def _normalize_bytes(self, data: bytes, destination: Path) -> Path:
        with tempfile.NamedTemporaryFile(suffix=".jpg") as temporary:
            temporary.write(data)
            temporary.flush()
            return self._normalize(Path(temporary.name), destination)

    def _normalize(self, source: Path, destination: Path) -> Path:
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
            image.thumbnail((self.max_size, self.max_size), Image.Resampling.LANCZOS)
            temporary = destination.with_suffix(destination.suffix + ".tmp")
            image.save(temporary, format="JPEG", quality=90, optimize=True)
        temporary.replace(destination)
        return destination


def read_metadata(path: Path) -> dict[str, str | float | None]:
    unknown = {"captured_at": None, "camera_id": "unknown", "exposure_seconds": None, "raw_warning": None,
               "latitude": None, "longitude": None, "altitude": None}
    exiftool = shutil.which("exiftool")
    if not exiftool:
        return unknown
    try:
        result = subprocess.run(
            [exiftool, "-json", "-n", "-DateTimeOriginal", "-CreateDate", "-Model", "-SerialNumber", "-ExposureTime",
             "-SubIFD:PhotometricInterpretation", "-GPSLatitude", "-GPSLongitude", "-GPSAltitude", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.SubprocessError:
        return unknown
    if result.returncode != 0:
        return unknown
    try:
        item = json.loads(result.stdout)[0]
    except (json.JSONDecodeError, IndexError, TypeError):
        return unknown
    captured = item.get("DateTimeOriginal") or item.get("CreateDate")
    model = item.get("Model") or "unknown"
    serial = item.get("SerialNumber") or "unknown"
    exposure = item.get("ExposureTime")  # -n gives seconds as a number
    exposure = float(exposure) if isinstance(exposure, (int, float)) and not isinstance(exposure, bool) and exposure > 0 else None
    # Sony's reduced-size (M/S) lossless ARW stores the raw as YCbCr lossless JPEG (photometric 6); rawspeed refuses it.
    unsupported = path.suffix.lower() == ".arw" and item.get("PhotometricInterpretation") == 6
    warning = ("Sony reduced-size lossless raw; darktable cannot decode it. JPEG export uses the macOS decoder with default "
               "settings, refined previews are unavailable, and culling uses the embedded preview.") if unsupported else None
    number = lambda key: value if isinstance(value := item.get(key), (int, float)) and not isinstance(value, bool) else None
    latitude, longitude, altitude = number("GPSLatitude"), number("GPSLongitude"), number("GPSAltitude")  # -n: signed decimals
    if latitude is None or longitude is None:  # half a fix is no fix
        latitude = longitude = altitude = None
    return {"captured_at": _normalize_exif_date(captured), "camera_id": f"{model}:{serial}", "exposure_seconds": exposure,
            "raw_warning": warning, "latitude": latitude, "longitude": longitude, "altitude": altitude}


def _normalize_exif_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.strptime(value[:19], "%Y:%m:%d %H:%M:%S")
        return parsed.isoformat()
    except ValueError:
        return None



THUMB_EDGE = 480  # contact-sheet tiles are ~224 CSS px; 480 covers 2× displays


def thumbnail_for(preview: Path, cache_dir: Path) -> Path:
    """Small JPEG for a managed preview, written once below the library cache; the name carries the preview's mtime."""
    target = cache_dir / f"{preview.stem}-{preview.stat().st_mtime_ns}.jpg"
    if target.is_file():
        return target
    cache_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(preview) as image:
        image.draft("RGB", (THUMB_EDGE * 2, THUMB_EDGE * 2))  # cheap JPEG decode at reduced scale
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((THUMB_EDGE, THUMB_EDGE), Image.Resampling.LANCZOS)
        with tempfile.NamedTemporaryFile(suffix=".jpg", dir=cache_dir, delete=False) as temporary:
            image.save(temporary, "JPEG", quality=82)
    Path(temporary.name).replace(target)
    return target


def prune_thumbnails(cache_dir: Path, previews: Iterable[Path]) -> int:
    """Remove every cached thumbnail that no existing preview would name; returns the count removed."""
    if not cache_dir.is_dir():
        return 0
    live = {f"{preview.stem}-{preview.stat().st_mtime_ns}.jpg" for preview in previews if preview.is_file()}
    removed = 0
    for thumb in cache_dir.glob("*.jpg"):
        if thumb.name not in live:
            thumb.unlink(missing_ok=True)
            removed += 1
    return removed
