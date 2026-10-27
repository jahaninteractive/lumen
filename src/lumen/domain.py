from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import math
import re
from types import MappingProxyType
from typing import Any

MAX_IMAGE_DIMENSION = 100_000
MAX_IMAGE_PIXELS = 1_000_000_000
DECISIONS = frozenset({"none", "flag", "reject"})
# "refined" means rendered from the raw rather than from the embedded JPEG; "developed" adds the
# photographer's own develop settings on top.
PREVIEW_KINDS = frozenset({"embedded", "refined", "developed"})
SUPPORTED_EXTENSIONS = frozenset({".arw", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic"})
JOB_STATUSES = frozenset({"queued", "running", "succeeded", "failed", "cancelled", "interrupted"})
MAX_JOB_ERROR_LENGTH = 1000


@dataclass(frozen=True, slots=True)
class CropRect:
    left: float
    top: float
    width: float
    height: float

    def __post_init__(self) -> None:
        values = (self.left, self.top, self.width, self.height)
        if any(not isinstance(value, (int, float)) or isinstance(value, bool) for value in values):
            raise ValueError("crop coordinates must be finite numbers")
        if any(not math.isfinite(value) for value in values):
            raise ValueError("crop coordinates must be finite numbers")
        if (self.left < 0 or self.top < 0 or self.width <= 0 or self.height <= 0
                or self.left + self.width > 1 or self.top + self.height > 1):
            raise ValueError("crop rectangle must be a positive rectangle inside normalized bounds")

    def pixel_box(self, width: int, height: int) -> tuple[int, int, int, int]:
        if (type(width) is not int or type(height) is not int or width <= 0 or height <= 0
                or width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION
                or width * height > MAX_IMAGE_PIXELS):
            raise ValueError("image dimensions are invalid")
        return (max(0, round(self.left * width)), max(0, round(self.top * height)),
                min(width, round((self.left + self.width) * width)),
                min(height, round((self.top + self.height) * height)))


DEVELOP_RANGES = {
    "exposure_ev": (-3.0, 3.0), "temperature_k": (2000, 12000), "tint": (-150, 150),
    "contrast": (0.0, 1.0), "sharpness": (0.0, 1.0), "luminance_nr": (0.0, 1.0), "color_nr": (0.0, 1.0),
    "clarity": (0.0, 1.0),
}
# Applied after the decode rather than being an amount the decoder already has a value for.
DEVELOP_FLAGS = ("lens_correction",)
CURVE_POINTS = 5


def _develop_number(value: Any, low: float, high: float, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"develop {name} must be a finite number")
    # Range check first: it rejects nan/inf and compares huge ints without float conversion.
    if not low <= value <= high:
        raise ValueError(f"develop {name} must be from {low} through {high}")
    return value


@dataclass(frozen=True, slots=True, eq=True)
class DevelopSettings:
    """Only the controls the photographer changed; an absent key leaves the file's own default alone."""
    fields: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.fields, dict):
            raise ValueError("develop settings must be an object")
        clean: dict[str, Any] = {}
        for key, value in self.fields.items():
            if key == "curve":
                if (not isinstance(value, (list, tuple)) or len(value) != CURVE_POINTS
                        or any(not isinstance(p, (list, tuple)) or len(p) != 2 for p in value)):
                    raise ValueError(f"develop curve must be {CURVE_POINTS} (x, y) points")
                clean[key] = tuple((float(_develop_number(x, 0, 1, "curve x")),
                                    float(_develop_number(y, 0, 1, "curve y"))) for x, y in value)
                if any(a[0] >= b[0] for a, b in zip(clean[key], clean[key][1:])):
                    raise ValueError("develop curve x values must strictly ascend")
            elif key in DEVELOP_FLAGS:
                if not isinstance(value, bool):
                    raise ValueError(f"develop {key} must be true or false")
                clean[key] = value
            elif key in DEVELOP_RANGES:
                clean[key] = _develop_number(value, *DEVELOP_RANGES[key], key)
            else:
                raise ValueError(f"unknown develop setting: {key}")
        object.__setattr__(self, "fields", MappingProxyType(clean))

    @property
    def is_empty(self) -> bool:
        return not self.fields

    def to_dict(self) -> dict[str, Any]:
        return {k: [list(p) for p in v] if k == "curve" else v for k, v in self.fields.items()}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DevelopSettings":
        return cls(dict(data) if isinstance(data, dict) else data)


@dataclass(slots=True)
class Photo:
    path: str
    fingerprint: str
    captured_at: datetime | None
    camera_id: str
    id: int | None = None
    preview_path: str | None = None
    format: str = "ARW"
    group_id: int | None = None
    manual_pick: bool = False
    decision: str = "none"
    rating: int = 0
    mirror_path: str | None = None
    preview_kind: str = "embedded"  # "embedded" (camera JPEG), "refined" (rendered from the raw, no develop settings) or "developed" (rendered from the raw with saved develop settings)
    exposure_seconds: float | None = None
    merged_into: str | None = None  # stable_key of the group a manual merge moved this photo into
    latitude: float | None = None  # camera GPS fix, signed decimal degrees; None when the file has none
    longitude: float | None = None
    altitude: float | None = None  # metres
    place: str | None = None  # nearest named place from the local GeoNames index
    place_country: str | None = None

    @property
    def place_label(self) -> str | None:
        """"<place>, <country>" for prompts and digests; a country alone is not a place."""
        return ", ".join(word for word in (self.place, self.place_country) if word) if self.place else None


@dataclass(slots=True)
class PhotoMetrics:
    sharpness: float
    exposure: float
    clipping: float
    face_quality: float | None = None
    eye_quality: float | None = None
    astro: dict[str, Any] = field(default_factory=dict)
    dhash: str | None = None


@dataclass(frozen=True, slots=True)
class ProvenanceEvent:
    id: int
    photo_id: int | None
    kind: str
    created_at: datetime
    details: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Artifact:
    id: int
    photo_id: int | None
    source_name: str
    source_fingerprint: str
    kind: str
    path: str
    settings: dict[str, Any]
    output_fingerprint: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Job:
    id: int
    kind: str
    status: str
    progress: float
    manifest_path: str
    log_path: str
    result_path: str | None
    error: str | None
    created_at: datetime
    updated_at: datetime
    label: str | None = None
    processed: int | None = None
    total: int | None = None

    def __post_init__(self) -> None:
        if type(self.id) is not int or self.id <= 0:
            raise ValueError("job ID must be a positive integer")
        if self.label is not None and (not isinstance(self.label, str) or not 1 <= len(self.label) <= 200):
            raise ValueError("job label must contain 1 to 200 characters")
        for name, count in (("processed", self.processed), ("total", self.total)):
            if count is not None and (type(count) is not int or count < 0):
                raise ValueError(f"job {name} count must be a non-negative integer")
        if self.processed is not None and self.total is not None and self.processed > self.total:
            raise ValueError("job processed count cannot exceed its total")
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise ValueError("job kind must not be empty")
        if not isinstance(self.status, str) or self.status not in JOB_STATUSES:
            raise ValueError("job status is invalid")
        if (not isinstance(self.progress, (int, float)) or isinstance(self.progress, bool)
                or not math.isfinite(self.progress) or not 0 <= self.progress <= 1):
            raise ValueError("job progress must be a finite number from 0 through 1")
        for name, path in (("manifest", self.manifest_path), ("log", self.log_path)):
            if not isinstance(path, str) or not path.strip():
                raise ValueError(f"job {name} path must not be empty")
        if self.result_path is not None and (
                not isinstance(self.result_path, str) or not self.result_path.strip()):
            raise ValueError("job result path must be a non-empty string or None")
        if self.error is not None and (
                not isinstance(self.error, str) or not self.error.strip()
                or len(self.error) > MAX_JOB_ERROR_LENGTH):
            raise ValueError(f"job error must contain 1 to {MAX_JOB_ERROR_LENGTH} characters")
        for name, value in (("created", self.created_at), ("updated", self.updated_at)):
            if (not isinstance(value, datetime) or value.tzinfo is None
                    or value.utcoffset() is None or value.utcoffset().total_seconds() != 0):
                raise ValueError(f"job {name} time must be UTC")
        if self.updated_at < self.created_at:
            raise ValueError("job update time cannot precede creation")
        if self.status == "queued" and (self.progress != 0 or self.result_path is not None or self.error is not None):
            raise ValueError("queued jobs must start clean at zero progress")
        if self.status == "running" and self.error is not None:
            raise ValueError("running jobs cannot have an error")
        if self.status == "succeeded" and (self.progress != 1 or self.error is not None):
            raise ValueError("succeeded jobs require full progress and no error")
        if self.status == "failed" and self.error is None:
            raise ValueError("failed jobs require an error")


MANUAL_CROP_ID = "crop-manual"
MANUAL_ASPECTS = ("free", "3:2", "2:3", "1:1", "4:5")


@dataclass(frozen=True, slots=True)
class PhotoEdit:
    photo_id: int
    candidate_id: str
    rect: CropRect
    aspect: str
    source_width: int
    source_height: int
    author: str
    rationale: str
    selected_at: datetime

    def __post_init__(self) -> None:
        if type(self.photo_id) is not int or self.photo_id <= 0:
            raise ValueError("crop photo ID must be a positive integer")
        if not isinstance(self.candidate_id, str) or not re.fullmatch(r"crop-(?:[0-9a-f]{16}|manual)", self.candidate_id):
            raise ValueError("crop candidate ID is invalid")
        if not isinstance(self.rect, CropRect):
            raise ValueError("crop rectangle is invalid")
        if not isinstance(self.aspect, str) or self.aspect not in {"native", *MANUAL_ASPECTS}:
            raise ValueError("crop aspect is invalid")
        if (type(self.source_width) is not int or type(self.source_height) is not int
                or self.source_width <= 0 or self.source_height <= 0
                or self.source_width > MAX_IMAGE_DIMENSION or self.source_height > MAX_IMAGE_DIMENSION
                or self.source_width * self.source_height > MAX_IMAGE_PIXELS):
            raise ValueError("crop source dimensions are invalid")
        if not isinstance(self.author, str) or self.author not in {"manual", "vision", "omlx-assisted", "photographer"}:
            raise ValueError("crop author is invalid")
        if not isinstance(self.rationale, str) or not self.rationale.strip() or len(self.rationale) > 500:
            raise ValueError("crop rationale must contain 1 to 500 characters")
        if (not isinstance(self.selected_at, datetime) or self.selected_at.tzinfo is None
                or self.selected_at.utcoffset() is None or self.selected_at.utcoffset().total_seconds() != 0):
            raise ValueError("crop selection time must be UTC")

    def to_dict(self) -> dict[str, Any]:
        return {
            "photo_id": self.photo_id,
            "candidate_id": self.candidate_id,
            "rect": {"left": self.rect.left, "top": self.rect.top,
                     "width": self.rect.width, "height": self.rect.height},
            "aspect": self.aspect,
            "source_width": self.source_width,
            "source_height": self.source_height,
            "author": self.author,
            "rationale": self.rationale,
            "selected_at": self.selected_at.isoformat(),
        }

