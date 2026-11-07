"""Read what Siril's log says about registration and stacking, for the astro debrief."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .tools import find_tool

# Kept here rather than imported from lumen.astro, which will import this module.
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_NUM = r"(-?\d+(?:\.\d+)?)"

_METHOD = re.compile(r"Registration: processing using method: (Two-Pass )?Global Star Alignment")
_REFERENCE_BLOCK = re.compile(r"Reference Image:")
_REFERENCE_STARS = re.compile(r"Found (\d+) stars in reference")
_REFERENCE_FWHM = re.compile(r"FWHM([xy]):\s+" + _NUM)
_CHOSEN = re.compile(r"choosing image (\d+) as new reference")
_STARS = re.compile(r"Found (\d+) .*stars in image")
_MATCHING = re.compile(r"Matching stars in image (\d+): done")
_SKIPPED = re.compile(r"Not enough star pairs \((\d+)\): Image (\d+) skipped")
_TOTAL = re.compile(r"Total: (\d+) failed, (\d+) registered")
_ABORTED = re.compile(r"Registration aborted|No image was registered")
_STACKED = re.compile(r"(\d+) images have been stacked")
_SUCCEEDED = re.compile(r"Script execution finished successfully")
# Lines inside a "Matching stars" block; the ones a FrameMatch keeps are named, the rest only continue it.
_FIELD = re.compile(
    r"log: (?:(?P<name>Pair matches after fitting|rotation|dx|dy|FWHM|roundness):\s+" + _NUM
    + r"|(?:Initial pair matches|Inliers|scaleX|scaleY|scale):)"
)
_FIELD_NAMES = {
    "Pair matches after fitting": "pairs", "rotation": "rotation", "dx": "dx", "dy": "dy",
    "FWHM": "fwhm", "roundness": "roundness",
}


@dataclass(frozen=True, slots=True)
class FrameMatch:
    """One "Matching stars in image k: done" block."""
    image: int
    pairs: int | None = None
    rotation: float | None = None
    dx: float | None = None
    dy: float | None = None
    fwhm: float | None = None
    roundness: float | None = None


@dataclass(frozen=True, slots=True)
class SirilRun:
    recipe: str  # "tracked" | "untracked" | "" when the log names no method
    reference: int | None
    reference_stars: int | None
    reference_fwhm_xy: tuple[float, float] | None  # tracked only
    star_counts: tuple[int, ...]  # every "Found N ... stars in image" line, unordered
    matches: tuple[FrameMatch, ...]
    skipped: tuple[tuple[int, int], ...]  # (image, pairs)
    registered: int | None
    failed: int | None
    aborted: bool
    stacked: int | None
    succeeded: bool


def parse_siril_log(text: str) -> SirilRun:
    recipe = ""
    reference = reference_stars = registered = failed = stacked = None
    fwhm: dict[str, float] = {}
    star_counts: list[int] = []
    matches: list[FrameMatch] = []
    skipped: list[tuple[int, int]] = []
    aborted = succeeded = False
    block: dict | None = None

    for raw in text.splitlines():
        line = _ANSI.sub("", raw).strip()
        if block is not None:
            field = _FIELD.match(line)
            if field:
                if field["name"]:
                    key = _FIELD_NAMES[field["name"]]
                    block[key] = int(field[2]) if key == "pairs" else float(field[2])
                continue
            matches.append(FrameMatch(**block))
            block = None
        if m := _MATCHING.search(line):
            block = {"image": int(m[1])}
        elif m := _METHOD.search(line):
            recipe = "untracked" if m[1] else "tracked"
        elif _REFERENCE_BLOCK.search(line):
            reference = 1
        elif m := _REFERENCE_STARS.search(line):
            reference_stars = int(m[1])
        elif m := _REFERENCE_FWHM.search(line):
            fwhm[m[1]] = float(m[2])
        elif m := _CHOSEN.search(line):
            reference = int(m[1])
        elif m := _STARS.search(line):
            star_counts.append(int(m[1]))
        elif m := _SKIPPED.search(line):
            skipped.append((int(m[2]), int(m[1])))
        elif m := _TOTAL.search(line):
            failed, registered = int(m[1]), int(m[2])
        elif _ABORTED.search(line):
            aborted = True
        elif m := _STACKED.search(line):
            stacked = int(m[1])
        elif _SUCCEEDED.search(line):
            succeeded = True
    if block is not None:
        matches.append(FrameMatch(**block))

    return SirilRun(
        recipe=recipe,
        reference=reference,
        reference_stars=reference_stars,
        reference_fwhm_xy=(fwhm["x"], fwhm["y"]) if {"x", "y"} <= fwhm.keys() else None,
        star_counts=tuple(star_counts),
        matches=tuple(matches),
        skipped=tuple(skipped),
        registered=registered,
        failed=failed,
        aborted=aborted,
        stacked=stacked,
        succeeded=succeeded,
    )


FITS_BLOCK = 2880
_FITS_MAX_BLOCKS = 36  # A converted light's header is three blocks; never read into the pixel data.
_FITS_STRING = re.compile(r"'((?:[^']|'')*)'")
_STEM_FIELD = re.compile(r"[^A-Za-z0-9.-]")
_STABILIZATION = {0: "Off", 1: "On"}


def read_fits_header(path: Path) -> dict[str, str | float | int | bool]:
    """Value cards of a FITS primary header (80-character ASCII cards in 2880-byte blocks), up to END."""
    header: dict[str, str | float | int | bool] = {}
    with Path(path).open("rb") as handle:
        for _ in range(_FITS_MAX_BLOCKS):
            block = handle.read(FITS_BLOCK)
            if not block:
                break
            for start in range(0, len(block), 80):
                card = block[start:start + 80].decode("ascii", "replace")
                key = card[:8].strip()
                if key == "END":
                    return header
                if card[8:10] != "= ":
                    continue
                value = card[10:].strip()
                if value.startswith("'"):
                    match = _FITS_STRING.match(value)
                    header[key] = match.group(1).replace("''", "'").rstrip() if match else ""
                    continue
                value = value.split("/", 1)[0].strip()
                if value in ("T", "F"):
                    header[key] = value == "T"
                    continue
                for kind in (int, float):
                    try:
                        header[key] = kind(value)
                        break
                    except ValueError:
                        pass
    return header


def _number(value: Any, kind: Callable[[float], Any] = float) -> Any:
    return kind(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def write_frames_json(job: Path, manifest: Any, *, runner: Callable[..., Any] = subprocess.run) -> dict:
    """Keep what the converted lights' FITS headers and the raws' EXIF say, so process/ can go."""
    job = Path(job)
    lights = [job / frame.link for frame in manifest.lights.frames]
    exif: dict[str, dict] = {}
    online = False
    exiftool = find_tool("exiftool")
    if exiftool and lights:
        command = [str(exiftool), "-json", "-n", "-ImageStabilization", "-MaxApertureValue", *map(str, lights)]
        try:
            completed = runner(command, capture_output=True, text=True, timeout=60, check=False)
            online = completed.returncode == 0
            rows = json.loads(completed.stdout or "[]")
            exif = {row.get("SourceFile"): row for row in rows if isinstance(row, dict)}
        except (OSError, subprocess.SubprocessError, ValueError, AttributeError, TypeError):
            online = False
    frames = []
    for position, light in enumerate(lights, 1):
        fit = job / "process" / f"light_{position:05d}.fit"
        if not fit.is_file():
            continue
        header = read_fits_header(fit)
        row = exif.get(str(light), {})
        pixel = _number(header.get("XPIXSZ"))
        max_aperture = _number(row.get("MaxApertureValue"))
        instrument = header.get("INSTRUME")
        frames.append({
            "position": position, "name": light.name,
            "date_obs": header.get("DATE-OBS") if isinstance(header.get("DATE-OBS"), str) else None,
            "exposure": _number(header.get("EXPTIME")), "iso": _number(header.get("ISOSPEED"), int),
            "aperture": _number(header.get("APERTURE")), "focal": _number(header.get("FOCALLEN")),
            "pixel_um": round(pixel, 2) if pixel is not None else None,
            "instrument": instrument if isinstance(instrument, str) and instrument else None,
            "stabilization": _STABILIZATION.get(row.get("ImageStabilization")),
            "max_aperture": round(max_aperture, 1) if max_aperture is not None else None,
        })
    record = {"frames": frames, "originals_online": online}
    (job / "frames.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def remove_process(job: Path) -> str:
    """Delete exactly <job>/process (Siril's multi-GB intermediates); returns a log line, never raises."""
    process = Path(job) / "process"
    try:
        if process.is_symlink():
            return "refused to remove process/: it is a symbolic link"
        if not process.exists():
            return "no process/ folder to remove"
        if process.resolve().parent != Path(job).resolve() or not process.is_dir():
            return "refused to remove process/: it is not a folder inside the job"
        shutil.rmtree(process)
    except OSError as error:
        return f"could not remove process/: {error.strerror or error}"
    return "removed Siril's process/ folder"


def output_stem(frames: list[dict], captured_at: datetime | None) -> str | None:
    """YYYY-MM-DD_HHMM_<camera>_<focal>mm_f<aperture>_<count>x<exposure>s_ISO<iso>; missing values drop out."""
    if not frames:
        return None

    def first(key: str) -> Any:
        return next((frame[key] for frame in frames if frame.get(key) is not None), None)

    fields = [captured_at.strftime("%Y-%m-%d"), captured_at.strftime("%H%M")] if captured_at else []
    if instrument := first("instrument"):
        words = str(instrument).split(None, 1)
        fields.append(words[-1])
    if (focal := first("focal")) is not None:
        fields.append(f"{focal:g}mm")
    if (aperture := first("aperture")) is not None:
        fields.append(f"f{aperture:g}")
    exposures = sorted(frame["exposure"] for frame in frames if frame.get("exposure") is not None)
    if exposures:
        low, high = exposures[0], exposures[-1]
        fields.append(f"{len(frames)}x{low:g}s" if low == high else f"{len(frames)}x{low:g}-{high:g}s")
    if (iso := first("iso")) is not None:
        fields.append(f"ISO{iso:g}")
    stem = "_".join(field for field in (_STEM_FIELD.sub("", field) for field in fields) if field)
    return stem or None


def rename_outputs(job: Path, stem: str) -> tuple[Path, Path | None]:
    """result.tif/preview.jpg -> <stem>.tif/.jpg; refuses (ValueError) a name already in the folder."""
    job = Path(job)
    moves = [(job / "result.tif", job / f"{stem}.tif")]
    if (job / "preview.jpg").is_file():
        moves.append((job / "preview.jpg", job / f"{stem}.jpg"))
    for _, target in moves:
        if target.exists() or target.is_symlink():
            raise ValueError(f"{target.name} already exists in the job folder")
    for source, target in moves:
        source.rename(target)
    return moves[0][1], moves[1][1] if len(moves) > 1 else None
