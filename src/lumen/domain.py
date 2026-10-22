from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import math
import re
from typing import Any

MAX_IMAGE_DIMENSION = 100_000
MAX_IMAGE_PIXELS = 1_000_000_000
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


@dataclass(slots=True)
class PhotoMetrics:
    sharpness: float
    exposure: float
    clipping: float
    face_quality: float | None = None
    eye_quality: float | None = None
    astro: dict[str, Any] = field(default_factory=dict)


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

    def __post_init__(self) -> None:
        if type(self.id) is not int or self.id <= 0:
            raise ValueError("job ID must be a positive integer")
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
        if not isinstance(self.candidate_id, str) or not re.fullmatch(r"crop-[0-9a-f]{16}", self.candidate_id):
            raise ValueError("crop candidate ID is invalid")
        if not isinstance(self.rect, CropRect):
            raise ValueError("crop rectangle is invalid")
        if not isinstance(self.aspect, str) or self.aspect not in {"native", "3:2", "4:5", "1:1"}:
            raise ValueError("crop aspect is invalid")
        if (type(self.source_width) is not int or type(self.source_height) is not int
                or self.source_width <= 0 or self.source_height <= 0
                or self.source_width > MAX_IMAGE_DIMENSION or self.source_height > MAX_IMAGE_DIMENSION
                or self.source_width * self.source_height > MAX_IMAGE_PIXELS):
            raise ValueError("crop source dimensions are invalid")
        if not isinstance(self.author, str) or self.author not in {"manual", "vision", "omlx-assisted"}:
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

