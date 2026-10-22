from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import hashlib
import math

from .analysis import SubjectRegions
from .domain import CropRect, MAX_IMAGE_DIMENSION, MAX_IMAGE_PIXELS


MIN_CROP_PIXELS = 8_000_000
WARNING_CROP_PIXELS = 12_000_000
MIN_SALIENCY_CONFIDENCE = 0.5
SCENE_PADDING = 2.0
TIGHT_PADDING = 0.18
# Supports sources up to 1 gigapixel and 100,000 pixels on either side.
@dataclass(frozen=True, slots=True)
class CropCandidate:
    id: str
    kind: str
    aspect: str
    rect: CropRect
    pixel_box: tuple[int, int, int, int]
    pixel_count: int
    remaining_megapixels: float
    rationale: str
    primary: bool
    revealable: bool
    resolution_warning: str | None

    @property
    def megapixels(self) -> float:
        return self.remaining_megapixels


def _validate_dimensions(width: int, height: int) -> None:
    if (
        not isinstance(width, int)
        or isinstance(width, bool)
        or not isinstance(height, int)
        or isinstance(height, bool)
        or width <= 0
        or height <= 0
    ):
        raise ValueError("image dimensions must be positive integers")
    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
        raise ValueError("image dimensions exceed the supported maximum")
    if width * height > MAX_IMAGE_PIXELS:
        raise ValueError("image pixel count exceeds the supported maximum")


def _is_acceptable_non_original(pixel_count: int) -> bool:
    return pixel_count >= MIN_CROP_PIXELS


def _rect_from_box(box: tuple[int, int, int, int], width: int, height: int) -> CropRect:
    left, top, right, bottom = box
    return CropRect(left / width, top / height, (right - left) / width, (bottom - top) / height)


def _aspect_ratio(label: str, width: int, height: int) -> Fraction:
    if label == "native":
        return Fraction(width, height)
    if label == "3:2":
        return Fraction(3, 2) if width >= height else Fraction(2, 3)
    if label == "4:5":
        return Fraction(5, 4) if width >= height else Fraction(4, 5)
    return Fraction(1, 1)


def _union_pixels(regions: SubjectRegions, width: int, height: int) -> tuple[int, int, int, int] | None:
    observations = list(regions.faces)
    observations.extend(
        item for item in regions.saliency_objects
        if item.confidence is not None and item.confidence >= MIN_SALIENCY_CONFIDENCE
    )
    if not observations:
        return None
    left = min(round(item.x * width) for item in observations)
    top = min(round(item.y * height) for item in observations)
    right = max(round((item.x + item.width) * width) for item in observations)
    bottom = max(round((item.y + item.height) * height) for item in observations)
    return left, top, right, bottom


def _padded_box(
    bounds: tuple[int, int, int, int], width: int, height: int, padding: float
) -> tuple[int, int, int, int]:
    left, top, right, bottom = bounds
    pad_x = math.ceil((right - left) * padding)
    pad_y = math.ceil((bottom - top) * padding)
    return max(0, left - pad_x), max(0, top - pad_y), min(width, right + pad_x), min(height, bottom + pad_y)


def _crop_box(
    bounds: tuple[int, int, int, int], width: int, height: int, ratio: Fraction
) -> tuple[int, int, int, int] | None:
    left, top, right, bottom = bounds
    ratio_width, ratio_height = ratio.numerator, ratio.denominator
    scale = max(
        math.ceil(math.sqrt(MIN_CROP_PIXELS / (ratio_width * ratio_height))),
        math.ceil((right - left) / ratio_width),
        math.ceil((bottom - top) / ratio_height),
    )
    max_scale = min(width // ratio_width, height // ratio_height)
    if scale <= 0 or scale > max_scale:
        return None
    crop_width, crop_height = ratio_width * scale, ratio_height * scale
    center_x, center_y = (left + right) / 2, (top + bottom) / 2
    crop_left = min(width - crop_width, max(0, round(center_x - crop_width / 2)))
    crop_top = min(height - crop_height, max(0, round(center_y - crop_height / 2)))
    box = crop_left, crop_top, crop_left + crop_width, crop_top + crop_height
    if not (box[0] <= left and box[1] <= top and box[2] >= right and box[3] >= bottom):
        return None
    return box


def _candidate(
    kind: str,
    aspect: str,
    box: tuple[int, int, int, int],
    width: int,
    height: int,
    rationale: str,
    primary: bool,
) -> CropCandidate:
    rect = _rect_from_box(box, width, height)
    pixel_count = (box[2] - box[0]) * (box[3] - box[1])
    resolution_warning = (
        f"{pixel_count / 1_000_000:.6f} MP remaining; below 12 MP"
        if pixel_count < WARNING_CROP_PIXELS
        else None
    )
    if kind == "original":
        candidate_id = "crop-original"
    else:
        identity = f"{kind}|{aspect}|{width}x{height}|{box[0]},{box[1]},{box[2]},{box[3]}"
        candidate_id = "crop-" + hashlib.sha256(identity.encode("ascii")).hexdigest()[:16]
    return CropCandidate(
        id=candidate_id,
        kind=kind,
        aspect=aspect,
        rect=rect,
        pixel_box=box,
        pixel_count=pixel_count,
        remaining_megapixels=pixel_count / 1_000_000,
        rationale=rationale,
        primary=primary,
        revealable=kind != "original" and not primary,
        resolution_warning=resolution_warning,
    )


def _meaningfully_different(box: tuple[int, int, int, int], other: tuple[int, int, int, int]) -> bool:
    left = max(box[0], other[0])
    top = max(box[1], other[1])
    right = min(box[2], other[2])
    bottom = min(box[3], other[3])
    intersection = max(0, right - left) * max(0, bottom - top)
    area_a = (box[2] - box[0]) * (box[3] - box[1])
    area_b = (other[2] - other[0]) * (other[3] - other[1])
    union = area_a + area_b - intersection
    return intersection / max(1, union) < 0.995


def generate_candidates(width: int, height: int, regions: SubjectRegions | None) -> list[CropCandidate]:
    """Generate deterministic, bounded crop choices from validated Vision regions."""
    _validate_dimensions(width, height)
    original_box = (0, 0, width, height)
    original = _candidate("original", "native", original_box, width, height, "", True)
    candidates = [original]
    if regions is None or (regions.image_width, regions.image_height) != (width, height):
        evidence = None
    else:
        evidence = _union_pixels(regions, width, height)

    if evidence is None:
        for aspect in ("1:1", "3:2", "4:5"):
            ratio = _aspect_ratio(aspect, width, height)
            box = _crop_box((width // 2, height // 2, width // 2, height // 2), width, height, ratio)
            if box is None or box == original_box or not _meaningfully_different(box, original_box):
                continue
            pixel_count = (box[2] - box[0]) * (box[3] - box[1])
            if _is_acceptable_non_original(pixel_count):
                candidates.append(
                    _candidate("centered-fallback", aspect, box, width, height, "centered fallback", True)
                )
                break
        return candidates

    has_faces = bool(regions and regions.faces)
    rationale = "padded face" if has_faces else "padded attention region"
    generated: list[CropCandidate] = []
    for kind, padding in (("subject-in-scene", SCENE_PADDING), ("tighter-subject", TIGHT_PADDING)):
        padded = _padded_box(evidence, width, height, padding)
        for aspect in ("native", "3:2", "4:5", "1:1"):
            if aspect == "native":
                continue
            box = _crop_box(padded, width, height, _aspect_ratio(aspect, width, height))
            if box is None or box == original_box:
                continue
            pixel_count = (box[2] - box[0]) * (box[3] - box[1])
            if not _is_acceptable_non_original(pixel_count):
                continue
            if any(not _meaningfully_different(box, existing.pixel_box) for existing in candidates + generated):
                continue
            generated.append(_candidate(kind, aspect, box, width, height, rationale, False))
    primary_kinds: set[str] = set()
    for candidate in generated:
        is_primary = candidate.kind not in primary_kinds and len(primary_kinds) < 2
        if is_primary:
            primary_kinds.add(candidate.kind)
        candidates.append(
            CropCandidate(
                id=candidate.id,
                kind=candidate.kind,
                aspect=candidate.aspect,
                rect=candidate.rect,
                pixel_box=candidate.pixel_box,
                pixel_count=candidate.pixel_count,
                remaining_megapixels=candidate.remaining_megapixels,
                rationale=candidate.rationale,
                primary=is_primary,
                revealable=not is_primary,
                resolution_warning=candidate.resolution_warning,
            )
        )
    return candidates
