"""Read what Siril's log says about registration and stacking, for the astro debrief."""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import median
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


def output_stem(frames: list[dict], captured_at: datetime | None, *, count: int | None = None) -> str | None:
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
        count = len(frames) if count is None else count  # the lights Siril stacked, when its log says
        fields.append(f"{count}x{low:g}s" if low == high else f"{count}x{low:g}-{high:g}s")
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


# Debrief thresholds, named after the design's rules.
ODD_STARS_RATIO = 0.5  # reference stars under half the others' median
ODD_EXPOSURE = 0.25  # reference exposure more than 25% from the others' median
GAP_SECONDS = 120
MIXED_EXPOSURE = 1.25  # longest over shortest
SHARP_FWHM_PX = 6
ROUNDNESS_MIN = 0.8
FWHM_XY_MAX = 1.25
DRIFT_PX = 20
DRIFT_DEG = 0.5
FEW_LIGHTS = 10
SHIP_MAX_S = 3
HANDHELD_S = 0.5
ASSUMED_PIXEL_UM = 4.0

PLACES = ("dark_site", "city", "ship", "handheld")
PLACE_SETTINGS = {
    "dark_site": {"max_s": None, "iso": 6400, "lights": 25, "darks": 10, "steadyshot": "off",
                  "also": "10 darks at the same exposure, ISO and temperature; SteadyShot off on a tripod"},
    "city": {"max_s": None, "iso": 1600, "lights": 25, "darks": 10, "steadyshot": "off",
             "also": "Flats matter more: the glow is uneven. SteadyShot off"},
    "ship": {"max_s": SHIP_MAX_S, "iso": 12800, "lights": 40, "darks": None, "steadyshot": "off",
             "also": "One unbroken burst, SteadyShot off on a tripod, braced against the roll; Untracked recipe"},
    "handheld": {"max_s": HANDHELD_S, "iso": 12800, "lights": 40, "darks": None, "steadyshot": "on",
                 "also": "SteadyShot on; Untracked recipe"},
}
_TRIPOD = ("dark_site", "city", "ship")


def npf_seconds(aperture: float, focal_mm: float, pixel_um: float) -> float:
    """NPF star-trail limit at declination 0 (the worst case)."""
    return (16.856 * aperture + 0.0997 * focal_mm + 13.713 * pixel_um) / focal_mm


def _seconds(value: float) -> str:
    return f"{value:.1f} s" if value < 1 else f"{max(1, round(value))} s"


def _range(values: list[float], unit: str = "") -> str:
    low, high = min(values), max(values)
    return f"{low:g}{unit}" if low == high else f"{low:g}-{high:g}{unit}"


def _frames(positions: list[int]) -> str:
    if len(positions) == 1:
        return f"frame {positions[0]}"
    return "frames " + ", ".join(map(str, positions[:-1])) + f" and {positions[-1]}"


def _gap(seconds: float) -> str:
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes} min {rest} s" if minutes and rest else f"{minutes} min" if minutes else f"{rest} s"


def _time(frame: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(frame["date_obs"])
    except (KeyError, TypeError, ValueError):
        return None


def debrief(run: SirilRun, frames: dict | None, manifest: Any, place: str) -> dict:
    """What happened, what to try with this set, and settings for next time at `place`. Rules only, no I/O."""
    if place not in PLACES:
        raise ValueError(f"unknown place: {place}")
    table = PLACE_SETTINGS[place]
    lights = [Path(frame.link).name for frame in manifest.lights.frames]

    def frame_name(k: int) -> str:
        return f"frame {k} ({lights[k - 1]})" if 0 < k <= len(lights) else f"frame {k}"

    shot = [frame for frame in (frames or {}).get("frames") or [] if isinstance(frame, dict)]
    by_position = {frame.get("position"): frame for frame in shot}
    happened: list[str] = []
    fired: dict[str, str] = {}  # rule -> the value its reason quotes
    reasons: list[str] = []

    # Registration.
    if run.skipped:
        pairs = [pairs for _, pairs in run.skipped]
        happened.append(f"{_frames([image for image, _ in run.skipped]).capitalize()} did not match the reference "
                        f"({_range(pairs)} shared stars; Siril needs more) and "
                        f"{'was' if len(pairs) == 1 else 'were'} skipped.")
    elif run.registered and not run.failed:
        happened.append(f"All {run.registered} frames matched the reference.")
    if run.aborted:
        happened.append("Nothing was stacked: registration aborted.")
    if run.registered and run.stacked is not None and run.stacked < run.registered:
        happened.append(f"The stack's FWHM and roundness filters dropped {run.registered - run.stacked} of "
                        f"{run.registered} registered frames.")

    # Reference odd one out.
    odd: list[str] = []
    odd_pair: tuple[int, int] | None = None
    reference = run.reference
    if reference is not None:
        others = list(run.star_counts)
        if run.reference_stars is not None and run.reference_stars in others:
            others.remove(run.reference_stars)
            if others and run.reference_stars < ODD_STARS_RATIO * median(others):
                odd.append(f"{run.reference_stars} stars against a median of {median(others):g}")
        ref = by_position.get(reference)
        other_frames = [frame for frame in shot if frame is not ref]
        exposures = [frame["exposure"] for frame in other_frames if frame.get("exposure")]
        if ref and ref.get("exposure") and exposures:
            middle = median(exposures)
            if abs(ref["exposure"] - middle) > ODD_EXPOSURE * middle:
                odd.append(f"{ref['exposure']:g} s against {middle:g} s")
        if ref and (ref_time := _time(ref)):
            timed = [(abs((t - ref_time).total_seconds()), (t - ref_time).total_seconds(), frame["position"])
                     for frame in other_frames if (t := _time(frame))]
            if timed:
                distance, signed, neighbour = min(timed)
                if distance > GAP_SECONDS:
                    odd.append(f"taken {_gap(distance)} {'before' if signed > 0 else 'after'} frame {neighbour}")
                    odd_pair = tuple(sorted((reference, neighbour)))
        if odd:
            happened.append(f"{frame_name(reference).capitalize()}, the reference, is the odd one out: "
                            + "; ".join(odd) + ".")

    # Exposures and timing.
    exposed = [(frame["position"], frame["exposure"]) for frame in shot if frame.get("exposure")]
    if exposed and max(e for _, e in exposed) / min(e for _, e in exposed) > MIXED_EXPOSURE:
        values = [e for _, e in exposed]
        happened.append(f"Exposures were mixed ({_range(values, ' s')}): "
                        + ", ".join(f"{e:g}" for e in values[:-1]) + f" and {values[-1]:g} s for "
                        + _frames([p for p, _ in exposed]) + ".")
        fired["mixed"] = _range(values, " s")
    timed = sorted((t, frame["position"]) for frame in shot if (t := _time(frame)))
    gaps = []
    for (t1, p1), (t2, p2) in zip(timed, timed[1:]):
        seconds = (t2 - t1).total_seconds()
        if seconds > GAP_SECONDS and tuple(sorted((p1, p2))) != odd_pair:
            gaps.append(f"frames {p1} and {p2} are {_gap(seconds)} apart")
    if gaps:
        happened.append("The burst has a gap: " + "; ".join(gaps) + " (the sky rotates"
                        + (" and the ship turns" if place == "ship" else "") + " in between).")
        fired["gap"] = gaps[0]

    # Stars.
    fwhm = (sum(run.reference_fwhm_xy) / 2 if run.reference_fwhm_xy
            else median(m.fwhm for m in run.matches if m.fwhm) if any(m.fwhm for m in run.matches) else None)
    if fwhm and fwhm > SHARP_FWHM_PX:
        happened.append(f"Stars are about {fwhm:.0f} px wide; sharp stars are under {SHARP_FWHM_PX} px.")
    roundness = [m.roundness for m in run.matches if m.roundness is not None]
    if roundness and median(roundness) < ROUNDNESS_MIN:
        detail = f"roundness {_range(roundness)}"
    elif not roundness and run.reference_fwhm_xy and min(run.reference_fwhm_xy) > 0 \
            and max(run.reference_fwhm_xy) / min(run.reference_fwhm_xy) > FWHM_XY_MAX:
        detail = "FWHMx {:g} against FWHMy {:g} on the reference".format(*run.reference_fwhm_xy)
    else:
        detail = None
    shifts = [(math.hypot(m.dx or 0, m.dy or 0), abs(m.rotation or 0)) for m in run.matches]
    if shifts:
        pixels, degrees = max(s for s, _ in shifts), max(r for _, r in shifts)
        if pixels > DRIFT_PX or degrees > DRIFT_DEG:
            cause = "the deck turning" if place == "ship" else "the sky turning past a fixed camera"
            happened.append(f"The frame moved up to {pixels:.0f} px and {degrees:.1f}° between shots: {cause}.")
            fired["drift"] = f"{pixels:.0f} px"

    # Settings shot, and the next ones.
    def first(key: str) -> Any:
        return next((frame[key] for frame in shot if frame.get(key) is not None), None)

    focal, shot_aperture, widest = first("focal"), first("aperture"), first("max_aperture")
    pixel = first("pixel_um")
    aperture = widest or shot_aperture
    pitch = pixel or ASSUMED_PIXEL_UM
    if shot and focal and not pixel:
        reasons.append(f"pixel size unknown; assumed {ASSUMED_PIXEL_UM:g} µm")
    if focal and shot_aperture and exposed:
        limit = npf_seconds(shot_aperture, focal, pitch)
        over = [(p, e) for p, e in exposed if e > limit]
        if over:
            happened.append(f"{_frames([p for p, _ in over]).capitalize()} ran {_range([e for _, e in over], ' s')}; "
                            f"stars trail past about {_seconds(limit)} at {focal:g} mm f/{shot_aperture:g}.")
            fired["trail"] = f"{_range([e for _, e in over], ' s')} ran past the {_seconds(limit)} star-trail limit"
    if detail:  # after the trail rule: on a still tripod, exposures past the limit are the likelier cause
        mover = "the deck" if place == "ship" else "the camera"
        cause = (f"exposures past the star-trail limit trail stars, and {mover} may also have moved"
                 if "trail" in fired else f"{mover} moved during each exposure")
        happened.append(f"Stars are stretched one way ({detail}): {cause}.")
        fired["stretched"] = detail
    if frames is not None and shot and not frames.get("originals_online"):
        happened.append("SteadyShot and the lens's widest aperture need the originals; the drive is offline.")
    elif place in _TRIPOD and any(frame.get("stabilization") == "On" for frame in shot):
        happened.append("SteadyShot is on; on a tripod it can add blur.")
        fired["steadyshot"] = ""
    count = len(lights)
    if 0 < count < FEW_LIGHTS:
        target = table["lights"]
        happened.append(f"{count} lights only cut the noise to about 1/{round(math.sqrt(count))}; "
                        f"{target} cut it to about 1/{round(math.sqrt(target))}.")
        fired["few"] = ""
    if not manifest.darks.frames and table["darks"]:
        happened.append("No darks: hot pixels and amp glow stay in the stack.")
        fired["darks"] = ""

    # Try now.
    try_now = []
    recipe = getattr(manifest, "recipe", "") or run.recipe
    failed = bool(run.skipped or run.aborted)
    if odd and failed:
        try_now.append(f"Exclude {frame_name(reference)} and prepare the stack again.")
    if ((failed and not odd) or gaps) and recipe != "untracked":
        try_now.append("Prepare again with the Untracked recipe, which picks the best frame as the reference.")
    if "darks" in fired:
        try_now.append("Add darks if you shot any.")
    if not try_now:
        try_now.append("Nothing to change in this set.")

    # Next time.
    npf = npf_seconds(aperture, focal, pitch) if focal and aperture else None
    if npf is None:
        exposure = _seconds(table["max_s"]) if table["max_s"] else "star-trail limit"
    else:
        exposure = _seconds(min(npf, table["max_s"]) if table["max_s"] else npf)
    parts = [exposure, f"f/{aperture:g}" if aperture else "widest aperture", f"ISO {table['iso']}",
             f"{table['lights']} lights"]
    if table["darks"]:
        parts.append(f"{table['darks']} darks")
    parts.append(f"SteadyShot {table['steadyshot']}")
    shorter = "Half a second" if place == "handheld" else "Shorter"
    deck = " and the deck moved" if place == "ship" else ""
    stretched = "stretched by deck motion" if place == "ship" else "stretched one way"
    if "trail" in fired and "stretched" in fired:
        reasons.append(f"{shorter}: {fired['trail']}, so the stars trailed ({fired['stretched']}){deck}")
    elif "trail" in fired:
        reasons.append(f"{shorter}: {fired['trail']}")
    elif "stretched" in fired:
        reasons.append(f"{shorter}: the stars were {stretched} ({fired['stretched']})")
    if "drift" in fired and place in ("ship", "handheld"):
        reasons.append(f"One unbroken burst, Untracked recipe: the frame drifted up to {fired['drift']}")
    if "gap" in fired:
        reasons.append(f"One unbroken burst: {fired['gap']}")
    if "mixed" in fired:
        reasons.append(f"One exposure for every frame: this set mixed {fired['mixed']}")
    if "few" in fired:
        reasons.append(f"{table['lights']} lights: {count} leave the noise high")
    if "darks" in fired:
        reasons.append(f"{table['darks']} darks: this set had none")
    if "steadyshot" in fired:
        reasons.append("SteadyShot off: it can add blur on a tripod")
    if not reasons:
        reasons.append(table["also"])
    return {"place": place, "happened": happened, "try_now": try_now,
            "next_time": {"settings": " · ".join(parts), "reasons": reasons[:3]}}
