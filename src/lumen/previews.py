from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps


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

        darktable = shutil.which("darktable-cli")
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


def read_metadata(path: Path) -> dict[str, str | None]:
    exiftool = shutil.which("exiftool")
    if not exiftool:
        return {"captured_at": None, "camera_id": "unknown"}
    try:
        result = subprocess.run(
            [exiftool, "-json", "-DateTimeOriginal", "-CreateDate", "-Model", "-SerialNumber", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.SubprocessError:
        return {"captured_at": None, "camera_id": "unknown"}
    if result.returncode != 0:
        return {"captured_at": None, "camera_id": "unknown"}
    try:
        item = json.loads(result.stdout)[0]
    except (json.JSONDecodeError, IndexError, TypeError):
        return {"captured_at": None, "camera_id": "unknown"}
    captured = item.get("DateTimeOriginal") or item.get("CreateDate")
    model = item.get("Model") or "unknown"
    serial = item.get("SerialNumber") or "unknown"
    return {"captured_at": _normalize_exif_date(captured), "camera_id": f"{model}:{serial}"}


def _normalize_exif_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.strptime(value[:19], "%Y:%m:%d %H:%M:%S")
        return parsed.isoformat()
    except ValueError:
        return None

