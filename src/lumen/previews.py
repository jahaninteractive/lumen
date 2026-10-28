from __future__ import annotations

import hashlib
import io
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
                return self.normalize(source, destination)
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
                    return self.normalize(candidate, destination)

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
                    return self.normalize(candidate, destination)
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
                return self.normalize(candidate, destination)
            except OSError as error:
                destination.with_suffix(destination.suffix + ".tmp").unlink(missing_ok=True)
                raise PreviewError(f"darktable's preview for {source.name} could not be read") from error

    def _normalize_bytes(self, data: bytes, destination: Path) -> Path:
        with tempfile.NamedTemporaryFile(suffix=".jpg") as temporary:
            temporary.write(data)
            temporary.flush()
            return self.normalize(Path(temporary.name), destination)

    def normalize(self, source: Path, destination: Path) -> Path:
        """Write `source` into `destination` as an EXIF-transposed sRGB JPEG at most `max_size` on its long
        edge, through a sibling temporary so `destination` is never half-written."""
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
    warning = ("Sony reduced-size lossless raw; darktable cannot decode it. darktable's refine pass is unavailable and "
               "culling uses the embedded preview. JPEG export is unaffected.") if unsupported else None
    number = lambda key: value if isinstance(value := item.get(key), (int, float)) and not isinstance(value, bool) else None
    latitude, longitude, altitude = number("GPSLatitude"), number("GPSLongitude"), number("GPSAltitude")  # -n: signed decimals
    if latitude is None or longitude is None:  # half a fix is no fix
        latitude = longitude = altitude = None
    return {"captured_at": _normalize_exif_date(captured), "camera_id": f"{model}:{serial}", "exposure_seconds": exposure,
            "raw_warning": warning, "latitude": latitude, "longitude": longitude, "altitude": altitude}


# The frame view's EXIF tab: a fixed list, so paths and file-system tags in exiftool's output never reach the browser.
EXIF_SUMMARY = (("Make", "Make"), ("Model", "Camera"), ("LensModel", "Lens"), ("FocalLength", "Focal length"),
                ("FocalLengthIn35mmFormat", "35 mm equivalent"), ("FNumber", "Aperture"), ("ExposureTime", "Shutter speed"),
                ("ISO", "ISO"), ("ExposureCompensation", "Exposure compensation"), ("ExposureProgram", "Exposure program"),
                ("MeteringMode", "Metering"), ("FocusMode", "Focus mode"), ("Flash", "Flash"), ("WhiteBalance", "White balance"),
                ("DateTimeOriginal", "Captured"), ("OffsetTimeOriginal", "Time zone"), ("ImageSize", "Image size"),
                ("Megapixels", "Megapixels"), ("ShutterCount", "Shutter count"), ("Software", "Firmware"),
                ("Artist", "Artist"), ("Copyright", "Copyright"))


def read_exif_summary(path: Path) -> list[tuple[str, str]]:
    """(label, value) for each EXIF_SUMMARY tag the file carries, in exiftool's readable form."""
    exiftool = shutil.which("exiftool")
    if not exiftool:
        raise PreviewError("exiftool is required to read EXIF")
    try:
        result = subprocess.run([exiftool, "-json", *(f"-{tag}" for tag, _ in EXIF_SUMMARY), str(path)],
                                capture_output=True, text=True, timeout=30, check=False)
        item = json.loads(result.stdout)[0] if result.returncode == 0 else None
    except (subprocess.SubprocessError, json.JSONDecodeError, IndexError, TypeError):
        item = None
    if not isinstance(item, dict):
        raise PreviewError("EXIF could not be read from this file")
    shown = {"FNumber": "f/{}", "ExposureTime": "{} s"}
    return [(label, shown.get(tag, "{}").format(item[tag])) for tag, label in EXIF_SUMMARY if item.get(tag) not in (None, "")]


def _normalize_exif_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.strptime(value[:19], "%Y:%m:%d %H:%M:%S")
        return parsed.isoformat()
    except ValueError:
        return None



THUMB_EDGE = 480  # contact-sheet tiles are ~224 CSS px; 480 covers 2× displays


def thumbnail_for(preview: Path, cache_dir: Path, crop: tuple[float, float, float, float] | None = None) -> Path:
    """Small JPEG for a managed preview, written once below the library cache; the name carries the preview's mtime.

    `crop` is the selected normalized (left, top, width, height), so the contact sheet shows what export bakes in.
    """
    suffix = f"-{hashlib.sha1(repr(crop).encode()).hexdigest()[:8]}" if crop else ""
    target = cache_dir / f"{preview.stem}-{preview.stat().st_mtime_ns}{suffix}.jpg"
    if target.is_file():
        return target
    cache_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(preview) as image:
        image.draft("RGB", (THUMB_EDGE * 2, THUMB_EDGE * 2))  # cheap JPEG decode at reduced scale
        image = ImageOps.exif_transpose(image).convert("RGB")
        if crop:
            left, top, width, height = crop
            image = image.crop((round(left * image.width), round(top * image.height),
                                round((left + width) * image.width), round((top + height) * image.height)))
        image.thumbnail((THUMB_EDGE, THUMB_EDGE), Image.Resampling.LANCZOS)
        with tempfile.NamedTemporaryFile(suffix=".jpg", dir=cache_dir, delete=False) as temporary:
            image.save(temporary, "JPEG", quality=82)
    Path(temporary.name).replace(target)
    return target


def full_size_for(source: Path, cache_dir: Path, fingerprint: str) -> Path | None:
    """The largest JPEG the camera embedded in the original, cached by fingerprint; None without one or without exiftool."""
    target = cache_dir / f"{fingerprint}.jpg"
    if target.is_file():  # served even when the original's volume is unplugged
        return target
    source.stat()  # an unreadable original with no cached copy raises OSError for the caller
    exiftool = shutil.which("exiftool")
    if not exiftool:
        return None
    for tag in ("JpgFromRaw", "PreviewImage"):
        try:
            result = subprocess.run([exiftool, "-b", f"-{tag}", str(source)], capture_output=True, timeout=60, check=False)
        except subprocess.SubprocessError:
            continue
        if result.returncode == 0 and result.stdout.startswith(b"\xff\xd8"):
            cache_dir.mkdir(parents=True, exist_ok=True)
            with Image.open(io.BytesIO(result.stdout)) as opened:
                image = ImageOps.exif_transpose(opened).convert("RGB")
            with tempfile.NamedTemporaryFile(suffix=".jpg", dir=cache_dir, delete=False) as temporary:
                image.save(temporary, "JPEG", quality=92)
            Path(temporary.name).replace(target)
            return target
    return None


def prune_cache(cache_dir: Path, live: set[str]) -> int:
    """Remove every cached JPEG whose name is not in `live`; returns the count removed."""
    if not cache_dir.is_dir():
        return 0
    removed = 0
    for cached in cache_dir.glob("*.jpg"):
        if cached.name not in live:
            cached.unlink(missing_ok=True)
            removed += 1
    return removed


def prune_thumbnails(cache_dir: Path, previews: Iterable[Path]) -> int:
    """Remove every cached thumbnail, cropped or not, that no existing preview would name; returns the count removed."""
    if not cache_dir.is_dir():
        return 0
    live = {f"{preview.stem}-{preview.stat().st_mtime_ns}" for preview in previews if preview.is_file()}
    # ponytail: a superseded crop's thumb lives until its preview changes; a few KB each.
    return prune_cache(cache_dir, {cached.name for cached in cache_dir.glob("*.jpg")
                                   if cached.stem in live or cached.stem.rsplit("-", 1)[0] in live})
