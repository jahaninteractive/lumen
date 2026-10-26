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


def _partial_path(destination: Path) -> Path:
    """The sibling the helper renders into before renaming. Must match raw_developer.swift."""
    return destination.with_name(f".{destination.name}.partial")


def _discard(destination: Path) -> None:
    """Leave nothing behind that a later step could mistake for a finished render, and no
    stray temp file either: a helper killed mid-write cannot clean up after itself."""
    for path in (destination, _partial_path(destination)):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


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

    def failed(message: str) -> DevelopError:
        _discard(destination)
        return DevelopError(message)

    # The camera original is never written, whatever a caller asks for.
    try:
        # resolve() catches symlinks and the plain case; samefile also catches a hard
        # link and a case-insensitive filesystem, but only once the destination exists.
        writes_over_the_original = destination.resolve() == source.resolve() or (
            destination.exists() and source.samefile(destination)
        )
    except OSError:
        raise DevelopError(f"The raw developer could not resolve a path for {name}.") from None
    if writes_over_the_original:
        raise DevelopError(f"The raw developer refused to write over the original {name}.")

    # Clear any earlier render first, so "the file exists" can only mean this run wrote it.
    _discard(destination)
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
        raise failed(f"Developing {name} took too long and was stopped.") from None
    except (OSError, subprocess.SubprocessError, ValueError):
        raise failed(f"The raw developer could not be run for {name}.") from None
    if completed.returncode != 0:
        raise failed(f"The raw developer could not develop {name}.")
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
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, OverflowError):
        raise failed(f"The raw developer returned an unreadable result for {name}.") from None
    if not destination.is_file():
        raise failed(f"The raw developer wrote no image for {name}.")
    return render
