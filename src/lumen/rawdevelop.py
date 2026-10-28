"""Render raw frames with develop settings applied, through the native helper."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

RENDER_TIMEOUT_SECONDS = 300  # Interactive renders; a full-size export asks for longer.


class DevelopError(RuntimeError):
    """A render failed. The message is safe to show the photographer."""


HISTOGRAM_CHANNELS = ("y", "r", "g", "b")
DEVELOP_AMOUNTS = ("contrast", "sharpness", "luminance_nr", "color_nr")


@dataclass(frozen=True)
class DevelopRender:
    path: Path
    width: int
    height: int
    # Rec. 709 luma and the three display channels, keyed by HISTOGRAM_CHANNELS.
    # Empty when the helper reported none, or reported a shape this version does not know.
    histogram: dict[str, tuple[float, ...]]
    # What the decoder itself applies when nothing is asked of it, keyed by DEVELOP_AMOUNTS.
    # The panel parks each unset handle here; empty when the helper reported none.
    defaults: dict[str, float]
    # Whether this file carries a profile Core Image can correct against; the panel only
    # offers the checkbox when it does, because not every body and lens pair has one.
    lens_correction_supported: bool
    neutral_temperature: float
    neutral_tint: float


def _histogram(reported: object) -> dict[str, tuple[float, ...]]:
    """The four channels, or nothing.

    A helper binary built before the four-channel change reports a bare list. Rather than
    fail the whole render over a drawing aid, drop the histogram and keep the image: the
    photographer sees an empty chart and a correct preview, and `scripts/bootstrap.sh`
    rebuilds the helper.
    """
    if not isinstance(reported, dict):
        return {}
    channels: dict[str, tuple[float, ...]] = {}
    for channel in HISTOGRAM_CHANNELS:
        buckets = reported.get(channel)
        if not isinstance(buckets, list):
            return {}
        channels[channel] = tuple(float(bucket) for bucket in buckets)
    return channels


def _defaults(reported: object) -> dict[str, float]:
    """The decoder's own baseline for the four amounts, or nothing.

    Same contract as _histogram: a helper that predates this field, or reports a shape this
    version does not know, costs the panel its parked positions, not the render.
    """
    if not isinstance(reported, dict):
        return {}
    amounts: dict[str, float] = {}
    for amount in DEVELOP_AMOUNTS:
        value = reported.get(amount)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return {}
        amounts[amount] = float(value)
    return amounts


def describe_histogram(histogram: dict[str, tuple[float, ...]]) -> str:
    """One sentence a vision model can read, in place of 1024 bucket counts.

    The model cannot judge clipping from a JPEG, and a photographer learning to read the chart
    needs the numbers named rather than shown. Buckets are area shares of the frame, so each
    figure below is a percentage of the pixels; they are divided by their own sum anyway, so a
    helper that ever reports counts instead describes the same frame.
    """
    luma = histogram.get("y")
    if not luma or len(luma) < 3:
        return ""
    last = len(luma) - 1
    total = sum(luma)
    if total <= 0:
        return ""
    reached, marks = 0.0, {}
    for index, share in enumerate(luma):
        reached += share
        for name, point in (("darkest", 0.01), ("median", 0.5), ("brightest", 0.99)):
            if name not in marks and reached >= point * total:
                marks[name] = round(index * 100 / last)
    note = (f"This render's histogram, in display values 0 to 100: {luma[0] / total:.1%} of pixels are crushed at "
            f"black and {luma[last] / total:.1%} are blown at white; the darkest 1% sit at {marks['darkest']}, the "
            f"median at {marks['median']}, the brightest 1% at {marks['brightest']}.")
    means = []
    for channel in ("r", "g", "b"):
        buckets = histogram.get(channel)
        weight = sum(buckets) if buckets else 0
        if not buckets or weight <= 0:
            return note
        mean = sum(index * share for index, share in enumerate(buckets)) / weight
        means.append(f"{channel.upper()} {round(mean * 100 / max(1, len(buckets) - 1))}")
    # The three means together are the cast: a photographer reads "R 50, G 25, B 13" as warm.
    return f"{note} Channel means: {', '.join(means)}."


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
    timeout: float = RENDER_TIMEOUT_SECONDS,
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
            timeout=timeout,
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
            histogram=_histogram(payload.get("histogram")),
            defaults=_defaults(payload.get("defaults")),
            lens_correction_supported=payload.get("lens_correction_supported") is True,
            neutral_temperature=float(payload["neutral_temperature"]),
            neutral_tint=float(payload["neutral_tint"]),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, OverflowError):
        raise failed(f"The raw developer returned an unreadable result for {name}.") from None
    if not destination.is_file():
        raise failed(f"The raw developer wrote no image for {name}.")
    return render
