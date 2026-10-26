"""Render raw frames with develop settings applied, through the native helper."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

RENDER_TIMEOUT_SECONDS = 300


class DevelopError(RuntimeError):
    """A render failed. The message is safe to show the photographer."""


@dataclass(frozen=True)
class DevelopRender:
    path: Path
    width: int
    height: int
    histogram: tuple[float, ...]
    neutral_temperature: float
    neutral_tint: float


def render_raw(
    source: Path,
    destination: Path,
    executable: Path,
    *,
    settings: dict[str, object] | None = None,
    long_edge: int = 0,
    image_format: str = "tiff16",
    histogram: bool = False,
    runner=subprocess.run,
) -> DevelopRender:
    """Develop `source` into `destination`, returning the render's size and histogram."""
    name = source.name
    request = {
        "source": str(source),
        "destination": str(destination),
        "format": image_format,
        "long_edge": long_edge,
        "histogram": histogram,
        "settings": {key: value for key, value in (settings or {}).items() if value is not None},
    }
    try:
        completed = runner(
            [str(executable)],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=RENDER_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise DevelopError(f"Developing {name} took too long and was stopped.") from None
    except (OSError, subprocess.SubprocessError):
        raise DevelopError(f"The raw developer could not be run for {name}.") from None
    if completed.returncode != 0:
        raise DevelopError(f"The raw developer could not develop {name}.")
    try:
        payload = json.loads(completed.stdout)
        render = DevelopRender(
            path=destination,
            width=int(payload["width"]),
            height=int(payload["height"]),
            histogram=tuple(float(bucket) for bucket in payload.get("histogram") or ()),
            neutral_temperature=float(payload["neutral_temperature"]),
            neutral_tint=float(payload["neutral_tint"]),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        raise DevelopError(f"The raw developer returned an unreadable result for {name}.") from None
    if not destination.is_file():
        raise DevelopError(f"The raw developer wrote no image for {name}.")
    return render
