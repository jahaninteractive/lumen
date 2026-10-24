from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json
import math
from pathlib import Path
from statistics import median
import subprocess

from PIL import Image, ImageFilter, ImageStat

from .domain import PhotoMetrics


@dataclass(frozen=True, slots=True)
class RegionObservation:
    """An immutable normalized rectangle in top-left image coordinates."""

    x: float
    y: float
    width: float
    height: float
    confidence: float | None = None

    @property
    def rect(self) -> tuple[float, float, float, float]:
        return self.x, self.y, self.width, self.height


@dataclass(frozen=True, slots=True)
class SubjectRegions:
    image_width: int
    image_height: int
    faces: tuple[RegionObservation, ...]
    saliency_objects: tuple[RegionObservation, ...]


def _bounded_grayscale(path: Path, max_size: int = 1024) -> Image.Image:
    with Image.open(path) as source:
        image = source.convert("L")
    image.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    return image


def _dhash(image: Image.Image) -> str:
    pixels = image.resize((9, 8), Image.Resampling.LANCZOS).tobytes()
    bits = 0
    for row in range(8):
        for column in range(8):
            bits = bits << 1 | (pixels[row * 9 + column] > pixels[row * 9 + column + 1])
    return f"{bits:016x}"


def difference_hash(path: Path) -> str:
    """16 hex chars: 9x8 grayscale, bit set where a pixel is brighter than its right neighbour, row-major."""
    return _dhash(_bounded_grayscale(path))


def hamming(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


def measure_image(path: Path) -> PhotoMetrics:
    image = _bounded_grayscale(path)
    edges = image.filter(
        ImageFilter.Kernel((3, 3), (0, -1, 0, -1, 4, -1, 0, -1, 0), scale=1, offset=128)
    )
    if edges.width > 2 and edges.height > 2:
        edges = edges.crop((1, 1, edges.width - 1, edges.height - 1))
    variance = ImageStat.Stat(edges).var[0]
    sharpness = variance / (variance + 500.0)

    histogram = image.histogram()
    count = max(1, sum(histogram))
    mean = sum(index * value for index, value in enumerate(histogram)) / (255.0 * count)
    clipping = (sum(histogram[:3]) + sum(histogram[253:])) / count
    tonal_balance = max(0.0, 1.0 - abs(mean - 0.46) / 0.54)
    exposure = tonal_balance * (1.0 - clipping) ** 2
    return PhotoMetrics(
        sharpness=_unit(sharpness),
        exposure=_unit(exposure),
        clipping=_unit(clipping),
        dhash=_dhash(image),
    )


def analyze_astro(path: Path) -> dict[str, float | int | bool]:
    image = _bounded_grayscale(path, max_size=512)
    histogram = image.histogram()
    midpoint = sum(histogram) // 2
    cumulative = 0
    background = 0
    for index, value in enumerate(histogram):
        cumulative += value
        if cumulative >= midpoint:
            background = index
            break

    threshold = min(245, max(80, background + 45))
    width, height = image.size
    pixels = image.load()
    visited = bytearray(width * height)
    sizes: list[int] = []
    elongations: list[float] = []

    for y in range(height):
        for x in range(width):
            offset = y * width + x
            if visited[offset] or pixels[x, y] < threshold:
                continue
            queue = deque([(x, y)])
            visited[offset] = 1
            component: list[tuple[int, int]] = []
            while queue:
                current_x, current_y = queue.popleft()
                component.append((current_x, current_y))
                for ny in range(max(0, current_y - 1), min(height, current_y + 2)):
                    for nx in range(max(0, current_x - 1), min(width, current_x + 2)):
                        neighbor = ny * width + nx
                        if not visited[neighbor] and pixels[nx, ny] >= threshold:
                            visited[neighbor] = 1
                            queue.append((nx, ny))
            if 1 <= len(component) <= 100:
                xs = [point[0] for point in component]
                ys = [point[1] for point in component]
                component_width = max(xs) - min(xs) + 1
                component_height = max(ys) - min(ys) + 1
                sizes.append(len(component))
                elongations.append(max(component_width, component_height) / max(1, min(component_width, component_height)))

    trailing = bool(elongations and median(elongations) > 2.5)
    clipping = sum(histogram[253:]) / max(1, sum(histogram))
    return {
        "background_brightness": round(background / 255.0, 4),
        "highlight_clipping": round(clipping, 4),
        "star_count": len(sizes),
        "median_elongation": round(median(elongations), 3) if elongations else 0.0,
        "trailing_warning": trailing,
    }


def _vision_payload(raw: str) -> dict[str, object]:
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as error:
        raise ValueError("macOS Vision returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("macOS Vision returned an invalid result")
    return payload


def parse_vision_result(raw: str) -> dict[str, float | int | None]:
    payload = _vision_payload(raw)
    schema_version = payload.get("schema_version")
    face_count = payload.get("face_count")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version not in (1, 2)
        or not isinstance(face_count, int)
        or isinstance(face_count, bool)
        or face_count < 0
    ):
        raise ValueError("macOS Vision returned an unsupported result")
    result: dict[str, float | int | None] = {"face_count": face_count}
    for key in ("face_quality", "eye_quality"):
        value = payload.get(key)
        if value is not None and (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not 0 <= value <= 1
            or isinstance(value, float) and not math.isfinite(value)
        ):
            raise ValueError(f"macOS Vision returned an invalid {key}")
        result[key] = float(value) if value is not None else None
    return result


def _valid_dimension(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _parse_observation(value: object) -> RegionObservation | None:
    if not isinstance(value, dict):
        return None
    coordinates = [value.get(key) for key in ("x", "y", "width", "height")]
    if any(
        not isinstance(number, (int, float))
        or isinstance(number, bool)
        or isinstance(number, float) and not math.isfinite(number)
        for number in coordinates
    ):
        return None
    x, y, width, height = coordinates
    if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
        return None
    confidence = value.get("confidence")
    if "confidence" in value:
        if (
            not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not 0 <= confidence <= 1
            or isinstance(confidence, float) and not math.isfinite(confidence)
        ):
            return None
        confidence = float(confidence)
    # Vision reports normalized bottom-left coordinates; consumers use top-left.
    x, y, width, height = float(x), float(y), float(width), float(height)
    top = 1.0 - y - height
    boundary_tolerance = math.ulp(1.0)
    if -boundary_tolerance <= top < 0.0:
        top = 0.0
    if 1.0 < top + height <= 1.0 + boundary_tolerance:
        top = 1.0 - height
    if x < 0.0 or top < 0.0 or x + width > 1.0 or top + height > 1.0:
        return None
    return RegionObservation(x, top, width, height, confidence)


def parse_vision_regions(raw: str) -> SubjectRegions:
    payload = _vision_payload(raw)
    if (
        payload.get("schema_version") != 2
        or payload.get("coordinate_system") != "normalized_bottom_left"
        or not _valid_dimension(payload.get("image_width"))
        or not _valid_dimension(payload.get("image_height"))
        or not isinstance(payload.get("faces"), list)
        or not isinstance(payload.get("saliency_objects"), list)
    ):
        raise ValueError("macOS Vision returned an invalid region envelope")
    parse_vision_result(raw)
    faces = tuple(region for item in payload["faces"] if (region := _parse_observation(item)) is not None)
    saliency_objects = tuple(
        region for item in payload["saliency_objects"] if (region := _parse_observation(item)) is not None
    )
    return SubjectRegions(
        image_width=payload["image_width"],  # type: ignore[arg-type]
        image_height=payload["image_height"],  # type: ignore[arg-type]
        faces=faces,
        saliency_objects=saliency_objects,
    )


def apply_vision_result(metrics: PhotoMetrics, result: dict[str, float | int | None]) -> None:
    metrics.face_quality = result.get("face_quality")  # type: ignore[assignment]
    metrics.eye_quality = result.get("eye_quality")  # type: ignore[assignment]


def analyze_faces(path: Path, executable: Path) -> dict[str, float | int | None] | None:
    if not executable.is_file():
        return None
    try:
        completed = subprocess.run(
            [str(executable), str(path)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if completed.returncode != 0:
            return None
        return parse_vision_result(completed.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def analyze_subject_regions(path: Path, executable: Path) -> SubjectRegions | None:
    try:
        if not executable.is_file():
            return None
        completed = subprocess.run(
            [str(executable), str(path)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if completed.returncode != 0:
            return None
        return parse_vision_regions(completed.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _unit(value: float) -> float:
    return min(1.0, max(0.0, float(value)))

