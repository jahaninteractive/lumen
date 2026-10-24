from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_GEONAMES_DIR = Path(__file__).resolve().parents[2] / "vendor" / "data" / "geonames"


@dataclass(frozen=True, slots=True)
class Settings:
    library_dir: Path
    omlx_url: str = "http://127.0.0.1:8000"
    omlx_model: str = "Qwen3.8-27B-4bit"
    burst_gap_seconds: float = 2.0
    omlx_fast_model: str = "Qwen3-VL-8B-Instruct-4bit"
    omlx_review_model: str | None = None
    near_duplicate_distance: int = 12
    geonames_dir: Path | None = None  # set by from_environment; None means no place index
    geocode_online: bool = False  # opt-in: send coordinates to Nominatim when the local index has no close place

    @classmethod
    def from_environment(cls) -> "Settings":
        root = Path(os.environ.get("LUMEN_LIBRARY", "~/Pictures/Lumen Library")).expanduser()
        raw_distance = os.environ.get("LUMEN_NEAR_DUP_DISTANCE", "12")
        try:
            distance = int(raw_distance)
        except ValueError:
            distance = -1
        if distance < 0:
            raise ValueError(f"LUMEN_NEAR_DUP_DISTANCE must be a non-negative integer, got {raw_distance!r}")
        return cls(
            library_dir=root,
            omlx_url=os.environ.get("LUMEN_OMLX_URL", "http://127.0.0.1:8000"),
            omlx_model=os.environ.get("LUMEN_OMLX_MODEL", "Qwen3.8-27B-4bit"),
            omlx_fast_model=os.environ.get("LUMEN_OMLX_FAST_MODEL", "Qwen3-VL-8B-Instruct-4bit"),
            omlx_review_model=os.environ.get("LUMEN_OMLX_REVIEW_MODEL") or None,
            near_duplicate_distance=distance,
            geonames_dir=Path(os.environ.get("LUMEN_GEONAMES_DIR", str(DEFAULT_GEONAMES_DIR))).expanduser(),
            geocode_online=os.environ.get("LUMEN_GEOCODE_ONLINE", "").strip().lower() in {"1", "true", "yes"},
        )

    @property
    def review_model(self) -> str:
        return self.omlx_review_model or self.omlx_model

    @property
    def vocabulary_path(self) -> Path:
        return self.library_dir / "vocabulary.json"

    @property
    def digest_dir(self) -> Path:
        return self.library_dir / "digests"

    @property
    def database_path(self) -> Path:
        return self.library_dir / "catalog.sqlite3"

    @property
    def mutation_lock_path(self) -> Path:
        return self.library_dir / ".mutations.lock"

    @property
    def preview_dir(self) -> Path:
        return self.library_dir / "previews"

    @property
    def sidecar_dir(self) -> Path:
        return self.library_dir / "sidecars"

    @property
    def output_dir(self) -> Path:
        return self.library_dir / "outputs"

    @property
    def jpeg_output_dir(self) -> Path:
        return self.output_dir / "jpeg"

    @property
    def cache_dir(self) -> Path:
        return self.library_dir / "cache"

    @property
    def geonames_index_path(self) -> Path:
        return self.cache_dir / "geonames.sqlite"

    @property
    def trash_dir(self) -> Path:
        return self.library_dir / "trash"

    @property
    def vision_analyzer_path(self) -> Path:
        return self.cache_dir / "bin" / "vision_analyzer"

    def ensure_directories(self) -> None:
        for path in (self.library_dir, self.preview_dir, self.sidecar_dir, self.output_dir, self.cache_dir, self.trash_dir):
            path.mkdir(parents=True, exist_ok=True)

