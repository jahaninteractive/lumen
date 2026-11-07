"""Read what Siril's log says about registration and stacking, for the astro debrief."""
from __future__ import annotations

import re
from dataclasses import dataclass

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
