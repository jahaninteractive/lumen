from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Collection, Mapping, Sequence

from PIL import Image

from .pipelines import SirilPipeline
from .previews import fingerprint_file

MANIFEST_SCHEMA = 1
# Script syntax below was validated against Siril 1.4.4; `requires` refuses other series too.
SUPPORTED_SIRIL = ((1, 4, 0), (1, 5, 0))
ASTRO_SUFFIXES = {".arw", ".fit", ".fits", ".fts", ".tif", ".tiff"}
CALIBRATION_KINDS = ("dark", "flat", "bias")
MISSING_WARNINGS = {
    "dark": "No dark frames: thermal noise and hot pixels will not be subtracted.",
    "flat": "No flat frames: vignetting and dust shadows will not be corrected.",
    "bias": "No bias frames: read-noise offset will not be subtracted.",
}# Coarse progress markers from Siril's "Running command: X" log lines, in script order.
STAGES = (("Running command: calibrate", 0.3), ("Running command: register", 0.5), ("Running command: savetif", 0.9))
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# tracked: global star alignment; untracked: two-pass registration cropped to the common area (drifting wide fields).
RECIPES = ("tracked", "untracked")
STACK_OPTIONS = "rej w 3 3 -norm=addscale -output_norm -rgb_equal -filter-wfwhm=3k -filter-round=3k -out=result"


class UnsupportedSirilVersion(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SirilCapabilities:
    executable: str
    version: tuple[int, int, int]

    @property
    def version_text(self) -> str:
        return ".".join(map(str, self.version))

    @property
    def supported(self) -> bool:
        return SUPPORTED_SIRIL[0] <= self.version < SUPPORTED_SIRIL[1]


@dataclass(frozen=True, slots=True)
class AstroFrame:
    kind: str
    source: str
    link: str
    fingerprint: str
    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class AstroFrameSet:
    kind: str
    folder: str | None
    frames: tuple[AstroFrame, ...]


@dataclass(frozen=True, slots=True)
class AstroManifest:
    schema: int
    job_id: int
    job_dir: str
    created_at: str
    siril: SirilCapabilities
    command: tuple[str, ...]
    lights: AstroFrameSet
    excluded_lights: tuple[str, ...]
    darks: AstroFrameSet
    flats: AstroFrameSet
    biases: AstroFrameSet
    warnings: tuple[str, ...]
    script: str
    script_sha256: str
    log: str
    result: str
    recipe: str = "tracked"  # Manifests written before recipes existed are tracked.


def probe_siril(executable: str, *, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> SirilCapabilities:
    try:
        completed = runner([executable, "--version"], capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"Siril version probe failed: {error}") from error
    # macOS builds print "Siril is started as macOS application" before the version line.
    match = re.search(r"^siril (\d+)\.(\d+)\.(\d+)\b", f"{completed.stdout}\n{completed.stderr}", re.MULTILINE)
    if completed.returncode != 0 or not match:
        raise RuntimeError("could not determine the installed Siril version")
    return SirilCapabilities(executable, tuple(int(part) for part in match.groups()))


def siril_script(siril: SirilCapabilities, *, darks: bool, flats: bool, biases: bool, recipe: str = "tracked") -> str:
    _require_supported(siril)
    if recipe not in RECIPES:
        raise ValueError("unknown recipe")
    low, high = (".".join(map(str, version)) for version in SUPPORTED_SIRIL)
    lines = [f"requires {low} {high}"]
    if biases:
        lines += ["cd biases", "convert bias -out=../process", "cd ../process",
                  "stack bias rej w 3 3 -nonorm -out=../masters/bias_stacked", "cd .."]
    if flats:
        lines += ["cd flats", "convert flat -out=../process", "cd ../process"]
        if biases:
            lines.append("calibrate flat -bias=../masters/bias_stacked")
        lines += [f"stack {'pp_flat' if biases else 'flat'} rej w 3 3 -norm=mul -out=../masters/pp_flat_stacked", "cd .."]
    if darks:
        lines += ["cd darks", "convert dark -out=../process", "cd ../process",
                  "stack dark rej w 3 3 -nonorm -out=../masters/dark_stacked", "cd .."]
    lines.append("cd lights")
    if darks or flats or biases:
        calibrate = ["calibrate light"]
        if darks:
            calibrate.append("-dark=../masters/dark_stacked")
        elif biases:
            calibrate.append("-bias=../masters/bias_stacked")
        if flats:
            calibrate.append("-flat=../masters/pp_flat_stacked")
        if darks:
            calibrate += ["-cc=dark", "-cfa"]
        if flats:
            calibrate.append("-equalize_cfa")
        calibrate.append("-debayer")
        lines += ["convert light -out=../process", "cd ../process", " ".join(calibrate)]
        sequence = "pp_light"
    else:
        lines += ["convert light -debayer -out=../process", "cd ../process"]
        sequence = "light"
    # register defaults to global star alignment; the stack filters drop k-sigma wFWHM/roundness outliers.
    if recipe == "untracked":  # Both flags verified against Siril 1.4.4's built-in command help.
        lines += [f"register {sequence} -2pass", f"seqapplyreg {sequence} -framing=min"]
    else:
        lines.append(f"register {sequence}")
    lines += [f"stack r_{sequence} {STACK_OPTIONS}",
              "load result", "mirrorx -bottomup", "savetif ../result", "cd ..", "close"]
    return "\n".join(lines) + "\n"


def prepare_siril_job(
    *, output_root: Path, job_id: int, lights: Sequence[Path], siril: SirilCapabilities,
    excluded: Collection[Path] = (), darks: Path | None = None, flats: Path | None = None,
    biases: Path | None = None, astro_metrics: Mapping[Path, Mapping] | None = None, recipe: str = "tracked",
) -> AstroManifest:
    if type(job_id) is not int or job_id <= 0:
        raise ValueError("job ID must be a positive integer")
    _require_supported(siril)
    light_paths = [_source_file(path) for path in lights]
    if any(path.suffix.lower() not in ASTRO_SUFFIXES for path in light_paths):
        raise ValueError("lights must be RAW, FITS, or TIFF frames")
    excluded_paths = {_source_file(path) for path in excluded}
    if len(set(light_paths)) != len(light_paths):
        raise ValueError("a light frame was selected twice")
    if not excluded_paths <= set(light_paths):
        raise ValueError("excluded lights must belong to the selected group")
    selected = [path for path in light_paths if path not in excluded_paths]
    if len(selected) < 2:
        raise ValueError("select at least two lights to stack")

    warnings: list[str] = []
    folders = {"dark": darks, "flat": flats, "bias": biases}
    sources = {"light": selected}
    for kind in CALIBRATION_KINDS:
        sources[kind] = _calibration_frames(kind, folders[kind])
        if folders[kind] is not None and not sources[kind]:
            warnings.append(f"{kind.capitalize()} folder {Path(folders[kind]).name} contains no supported frames.")
        elif len(sources[kind]) == 1:
            # Siril 1.4.4 builds no sequence from one converted frame, so the master stack would fail.
            warnings.append(f"{kind.capitalize()} folder {Path(folders[kind]).name} has only one frame; at least two are needed to build a master.")
            sources[kind] = []
        if not sources[kind]:
            warnings.append(MISSING_WARNINGS[kind])
    # Compare against every group light, not just the selected ones, so excluded lights can't become calibration.
    every = light_paths + [path for kind in CALIBRATION_KINDS for path in sources[kind]]
    if len(set(every)) != len(every):
        raise ValueError("a frame appears in more than one frame set")
    metrics = {Path(path).resolve(): values for path, values in (astro_metrics or {}).items()}
    warnings += [f"Light {path.name} shows star trailing." for path in selected if metrics.get(path, {}).get("trailing_warning")]

    script = siril_script(siril, darks=bool(sources["dark"]), flats=bool(sources["flat"]), biases=bool(sources["bias"]), recipe=recipe)
    frame_sets = {kind: AstroFrameSet(kind, str(folders[kind]) if folders.get(kind) else None,
                                      tuple(_fingerprinted(kind, index, path) for index, path in enumerate(paths, 1)))
                  for kind, paths in sources.items()}

    output_root = output_root.resolve()
    astro_root = output_root / "astro"
    astro_root.mkdir(parents=True, exist_ok=True)
    if astro_root.resolve() != astro_root:
        raise ValueError("astro jobs must stay inside the managed output directory")
    job = astro_root / str(job_id)
    job.mkdir()
    try:
        for frame_set in frame_sets.values():
            for frame in frame_set.frames:
                link = job / frame.link
                link.parent.mkdir(exist_ok=True)
                os.symlink(frame.source, link)
        (job / "siril.config").touch(exist_ok=False)  # Siril -i needs an existing, job-owned file.
        _write_once(job / "stack.ssf", script)
        manifest = AstroManifest(
            schema=MANIFEST_SCHEMA, job_id=job_id, job_dir=str(job),
            created_at=datetime.now(timezone.utc).isoformat(), siril=siril,
            command=tuple(SirilPipeline(siril.executable, output_root).job_command(job)),
            lights=frame_sets["light"], excluded_lights=tuple(str(path) for path in light_paths if path in excluded_paths),
            darks=frame_sets["dark"], flats=frame_sets["flat"], biases=frame_sets["bias"], warnings=tuple(warnings),
            script="stack.ssf", script_sha256=hashlib.sha256(script.encode("ascii")).hexdigest(),
            log="siril.log", result="result.tif", recipe=recipe,
        )
        _write_once(job / "manifest.json", json.dumps(_manifest_payload(manifest), ensure_ascii=False, indent=2) + "\n")
    except BaseException:
        shutil.rmtree(job, ignore_errors=True)
        raise
    return manifest


def load_manifest(path: Path, output_root: Path) -> AstroManifest:
    """Load a prepared job of this library and verify its integrity; source fingerprints are rechecked at launch."""
    job = Path(path).resolve().parent
    if job.parent != output_root.resolve() / "astro":
        raise ValueError("astro manifest is not inside this library's managed astro directory")
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if payload.get("schema") != MANIFEST_SCHEMA:
            raise ValueError("unsupported astro manifest schema")
        major, minor, patch = (int(part) for part in payload["siril"]["version"].split("."))
        siril = SirilCapabilities(payload["siril"]["executable"], (major, minor, patch))
        frame_sets = {name: AstroFrameSet(payload[name]["kind"], payload[name]["folder"],
                                          tuple(AstroFrame(**frame) for frame in payload[name]["frames"]))
                      for name in ("lights", "darks", "flats", "biases")}
        lists = {name: tuple(payload[name]) for name in ("command", "excluded_lights", "warnings")}
        manifest = AstroManifest(**(payload | frame_sets | lists | {"siril": siril}))
        expected_command = tuple(SirilPipeline(siril.executable, output_root).job_command(job))
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError(f"malformed astro manifest: {error}") from error
    if (type(manifest.job_id) is not int or manifest.job_dir != str(job) or job.name != str(manifest.job_id)
            or manifest.command != expected_command):
        raise ValueError("astro manifest does not belong to its managed job directory")
    if (manifest.script, manifest.log, manifest.result) != ("stack.ssf", "siril.log", "result.tif"):
        raise ValueError("astro manifest names unexpected job files")
    if manifest.recipe not in RECIPES:
        raise ValueError("astro manifest names an unknown recipe")
    for name, kind in (("lights", "light"), ("darks", "dark"), ("flats", "flat"), ("biases", "bias")):
        frame_set = getattr(manifest, name)
        if frame_set.kind != kind or any(frame.kind != kind or not isinstance(frame.link, str) or not re.fullmatch(rf"{name}/{kind}_\d{{5}}\.[a-z]+", frame.link)
                                         for frame in frame_set.frames):
            raise ValueError("astro manifest frame links are invalid")
        for frame in frame_set.frames:
            if not (isinstance(frame.source, str) and Path(frame.source).is_absolute() and type(frame.size) is int
                    and type(frame.mtime_ns) is int and isinstance(frame.fingerprint, str)
                    and re.fullmatch(r"[0-9a-f]{64}", frame.fingerprint)):
                raise ValueError("astro manifest frame record is invalid")
    links = [frame.link for name in ("lights", "darks", "flats", "biases") for frame in getattr(manifest, name).frames]
    if len(set(links)) != len(links):
        raise ValueError("astro manifest frame links are duplicated")
    if hashlib.sha256((job / "stack.ssf").read_bytes()).hexdigest() != manifest.script_sha256:
        raise ValueError("Siril script no longer matches its manifest")
    return manifest


def _manifest_payload(manifest: AstroManifest) -> dict:
    payload = {}
    for name in AstroManifest.__slots__:
        value = getattr(manifest, name)
        if isinstance(value, SirilCapabilities):
            value = {"executable": value.executable, "version": value.version_text}
        elif isinstance(value, AstroFrameSet):
            value = {"kind": value.kind, "folder": value.folder,
                     "frames": [{field: getattr(frame, field) for field in AstroFrame.__slots__} for frame in value.frames]}
        elif isinstance(value, tuple):
            value = list(value)
        payload[name] = value
    return payload


def _require_supported(siril: SirilCapabilities) -> None:
    if not siril.supported:
        raise UnsupportedSirilVersion(f"Siril {siril.version_text} is not supported; Lumen scripts are validated for 1.4.x")


def _source_file(path: Path) -> Path:
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise ValueError(f"astro frame is not a readable file: {Path(path).name}")
    return resolved


def _calibration_frames(kind: str, folder: Path | None) -> list[Path]:
    if folder is None:
        return []
    if not Path(folder).is_dir():
        raise ValueError(f"{kind} folder does not exist: {Path(folder).name}")
    return sorted((path.resolve() for path in Path(folder).iterdir()
                   if path.is_file() and not path.name.startswith(".") and path.suffix.lower() in ASTRO_SUFFIXES),
                  key=lambda path: path.name)


def _fingerprinted(kind: str, index: int, source: Path) -> AstroFrame:
    before = source.stat()
    fingerprint = fingerprint_file(source)
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError(f"astro frame changed while it was fingerprinted: {source.name}")
    # Fixed ASCII link names keep source paths (spaces, non-ASCII) out of the Siril script entirely.
    folder = "biases" if kind == "bias" else f"{kind}s"
    return AstroFrame(kind, str(source), f"{folder}/{kind}_{index:05d}{source.suffix.lower()}",
                      fingerprint, after.st_size, after.st_mtime_ns)


def _write_once(path: Path, text: str) -> None:
    with path.open("x", encoding="utf-8") as handle:
        handle.write(text)
    path.chmod(0o444)


@dataclass(frozen=True, slots=True)
class AstroExecution:
    result: Path
    bits_per_sample: tuple[int, ...]
    returncode: int


def stage_progress(line: str) -> float | None:
    for marker, progress in STAGES:
        if marker in line:
            return progress
    return None


def revalidate_frames(manifest: AstroManifest, job: Path) -> None:
    """Every source must still match its prepared size, mtime, and sha256, and every link must still point at it."""
    for frame_set in (manifest.lights, manifest.darks, manifest.flats, manifest.biases):
        for frame in frame_set.frames:
            source = Path(frame.source)
            if not source.is_file():
                raise ValueError(f"source frame is missing: {source.name}")
            link = job / frame.link
            if not link.is_symlink() or os.readlink(link) != frame.source:
                raise ValueError(f"job link was altered: {frame.link}")
            stat = source.stat()
            if (stat.st_size, stat.st_mtime_ns) != (frame.size, frame.mtime_ns) or fingerprint_file(source) != frame.fingerprint:
                raise ValueError(f"source frame changed since preparation: {source.name}")


def verify_tiff(path: Path) -> tuple[int, ...]:
    try:
        with Image.open(path) as image:
            image_format = image.format
            value = image.tag_v2.get(258) if image_format == "TIFF" else None
    except OSError as error:
        raise RuntimeError(f"{path.name} is not a readable TIFF: {error}") from error
    bits = tuple(int(bit) for bit in (value if isinstance(value, tuple) else (value,) if value is not None else ()))
    if image_format != "TIFF" or not bits or any(bit != 16 for bit in bits):
        raise RuntimeError(f"{path.name} is not a 16-bit TIFF (format {image_format}, bits per sample {list(bits)})")
    return bits


def execute_job(context: Any, manifest: AstroManifest, *, output_root: Path, timeout_seconds: float = 7200.0,
                popen: Callable[..., Any] | None = None) -> AstroExecution | None:
    """Run a prepared job's Siril command under the job runner; returns None when cancelled.

    Raises ValueError before launch when frames or the job directory are not as prepared,
    and RuntimeError when Siril times out, exits nonzero, or leaves no 16-bit TIFF.
    """
    job = Path(manifest.job_dir)
    if job.parent != Path(output_root).resolve() / "astro":
        raise ValueError("astro jobs must run inside the managed output directory")
    result = job / manifest.result
    with (job / manifest.log).open("a", encoding="utf-8") as log:
        def note(text: str) -> None:
            log.write(f"lumen: {text}\n")
            log.flush()

        note(f"job {manifest.job_id} launch check at {datetime.now(timezone.utc).isoformat()}")
        try:
            for name in (manifest.result, "process", "masters"):
                if (job / name).exists():
                    raise ValueError(f"job directory already contains outputs ({name}); prepare a new job")
            revalidate_frames(manifest, job)
        except ValueError as error:
            note(f"refusing to launch: {error}")
            raise
        context.update_progress(0.05)
        note("command " + json.dumps(list(manifest.command)))
        process = (popen or subprocess.Popen)(
            list(manifest.command), cwd=str(job), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
        )
        context.register_process(process)
        context.update_progress(0.1)
        reached = latest = 0.1

        def pump() -> None:
            # Only copies output; catalog progress writes happen on the polling thread below.
            nonlocal latest
            for raw in process.stdout:
                line = _ANSI.sub("", raw.rstrip("\n"))
                try:
                    log.write(line + "\n")
                    log.flush()
                except ValueError:  # Log closed after the join timeout; keep draining the pipe.
                    pass
                stage = stage_progress(line)
                if stage is not None and stage > latest:
                    latest = stage

        reader = threading.Thread(target=pump, name=f"lumen-astro-log-{manifest.job_id}", daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout_seconds
        timed_out = False
        while process.poll() is None:
            if latest > reached:
                reached = latest
                context.update_progress(reached)
            if context.wait_for_cancel(0.1):
                break
            if time.monotonic() >= deadline:
                timed_out = True
                _stop(process)
                break
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        reader.join(5)
        if not reader.is_alive():
            process.stdout.close()
        returncode = process.poll()
        if returncode is not None:
            context.clear_process(process)
        if context.cancelled:
            note("cancelled by the photographer; no result was recorded")
            return None
        if timed_out:
            note(f"timed out after {timeout_seconds:g} seconds")
            raise RuntimeError(f"Siril timed out after {timeout_seconds:g} seconds")
        if returncode != 0:
            raise RuntimeError(f"Siril failed with exit code {returncode}; see {manifest.log}")
        if not result.is_file():
            raise RuntimeError(f"Siril exited successfully but did not produce {manifest.result}")
        bits = verify_tiff(result)
        note(f"verified {manifest.result} ({'x'.join(map(str, bits))} bits per sample)")
        context.update_progress(0.95)
        return AstroExecution(result, bits, returncode)


def _stop(process: Any) -> None:
    for signal_process in (process.terminate, process.kill):
        try:
            signal_process()
            process.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            continue
        except OSError:
            return
