from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    library_dir: Path
    omlx_url: str = "http://127.0.0.1:8000"
    omlx_model: str = "Qwen3.8-27B-4bit"
    burst_gap_seconds: float = 2.0

    @classmethod
    def from_environment(cls) -> "Settings":
        root = Path(os.environ.get("LUMEN_LIBRARY", "~/Pictures/Lumen Library")).expanduser()
        return cls(
            library_dir=root,
            omlx_url=os.environ.get("LUMEN_OMLX_URL", "http://127.0.0.1:8000"),
            omlx_model=os.environ.get("LUMEN_OMLX_MODEL", "Qwen3.8-27B-4bit"),
        )

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
    def trash_dir(self) -> Path:
        return self.library_dir / "trash"

    @property
    def vision_analyzer_path(self) -> Path:
        return self.cache_dir / "bin" / "vision_analyzer"

    def ensure_directories(self) -> None:
        for path in (self.library_dir, self.preview_dir, self.sidecar_dir, self.output_dir, self.cache_dir, self.trash_dir):
            path.mkdir(parents=True, exist_ok=True)

