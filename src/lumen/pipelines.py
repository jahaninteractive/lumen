from __future__ import annotations

import shutil
import io
import math
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from xml.etree import ElementTree

from PIL import Image, ImageCms, ImageDraw, ImageFont, ImageOps

from .previews import fingerprint_file
from .rawdevelop import render_raw
from .sidecars import SidecarWriter
from .tools import find_tool


EXPORT_PRESETS = {
    "full": {"long_edge": None, "quality": 92, "include_gps": True},
    "share": {"long_edge": 2048, "quality": 85, "include_gps": False},
    "web": {"long_edge": 1600, "quality": 82, "include_gps": False},
}


def validate_watermark(value: object) -> str | None:
    """Blank means no watermark; whitespace-only or unprintable text is an error."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 80 or not value.isprintable():
        raise ValueError("watermark must be 1 to 80 printable characters")
    return value.strip()


def _public_provenance(settings: dict) -> dict:
    """Export record embedded in shareable JPEGs: no absolute paths or source fingerprint."""
    public = {key: value for key, value in settings.items() if key not in ("command", "source_fingerprint")}
    if public.get("profile"):
        public["profile"] = Path(public["profile"]).name
    return public


def _descriptive_xmp(packet: bytes | str | None) -> dict:
    """Read the sharing fields only; never copy the original XMP packet."""
    if not packet:
        return {}
    try:
        root = ElementTree.fromstring(packet)
    except (ElementTree.ParseError, ValueError):
        return {}
    ns = {"rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
          "dc": "http://purl.org/dc/elements/1.1/",
          "xmp": "http://ns.adobe.com/xap/1.0/"}
    fields = {}
    for item in root.findall(".//rdf:Description", ns):
        value = item.get(f"{{{ns['xmp']}}}Rating")
        if value is None:
            value = item.findtext("xmp:Rating", namespaces=ns)
        if value is not None:
            try:
                rating = int(value)
                if 0 <= rating <= 5:
                    fields["rating"] = rating
            except ValueError:
                pass
        description = item.find("dc:description", ns)
        if description is not None:
            alternatives = description.findall("rdf:Alt/rdf:li", ns)
            chosen = next((alternative for alternative in alternatives if alternative.get("{http://www.w3.org/XML/1998/namespace}lang") == "x-default"), alternatives[0] if alternatives else description)
            fields["description"] = chosen.text or ""
        elif (value := item.get(f"{{{ns['dc']}}}description")) is not None:
            fields["description"] = value
        subject = item.find("dc:subject", ns)
        if subject is not None:
            fields["keywords"] = [keyword.text for keyword in subject.findall(".//rdf:li", ns) if keyword.text]
    return fields


@dataclass(frozen=True, slots=True)
class Capability:
    name: str
    available: bool
    executable: str | None
    purpose: str


@dataclass(frozen=True, slots=True)
class PipelineResult:
    success: bool
    returncode: int
    output: Path
    command: tuple[str, ...]
    message: str


@dataclass(frozen=True, slots=True)
class JpegExportResult(PipelineResult):
    settings: dict
    output_fingerprint: str


# Apple's Core Image raw pipeline (the native raw developer) decodes every raw this library holds, including the
# Sony reduced-size lossless ARWs darktable's rawspeed refuses; darktable renders non-raws and XMP profiles.
RAW_EXTENSIONS = frozenset({".arw"})
DECODERS = frozenset({"darktable", "coreimage"})
# A full-size export render keeps the budget the macOS decoder had; the interactive preview keeps the shorter
# default in `rawdevelop`, so a slow export never makes the Develop tab feel hung.
EXPORT_RENDER_TIMEOUT_SECONDS = 600


class DarktablePipeline:
    def __init__(self, executable: str, output_root: Path, raw_developer: Path | None = None):
        self.executable = executable
        self.output_root = output_root.resolve()
        self.raw_developer = raw_developer

    def export_jpeg(
        self, source: Path, profile: Path | None = None, *,
        crop: dict[str, object] | None = None, description: str | None = None,
        keywords: list[str] | None = None, rating: int | None = None, alt_text: str | None = None,
        caption_used: bool = False, reserved_paths: set[Path] | None = None, preset: str = "full",
        develop: dict[str, object] | None = None, stem_suffix: str | None = None,
        watermark: str | None = None, place: str | None = None, country: str | None = None,
        subfolder: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> JpegExportResult:
        if preset not in EXPORT_PRESETS:
            raise ValueError(f"unknown export preset: {preset!r}")
        options = EXPORT_PRESETS[preset]
        watermark = validate_watermark(watermark)
        runner = runner or subprocess.run
        source = source.resolve()
        # The caller names the batch's folder; the containment check below still has the last word.
        directory = (self.output_root / "jpeg" / (subfolder or "")).resolve()
        if not directory.is_relative_to(self.output_root) or not directory.is_relative_to(self.output_root / "jpeg"):
            raise ValueError("JPEG output must stay inside the managed output directory")
        directory.mkdir(parents=True, exist_ok=True)
        stem = (f"{source.stem}-lumen" + ("" if preset == "full" else f"-{preset}")
                + (f"-{stem_suffix}" if stem_suffix else ""))
        destination = directory / f"{stem}.jpg"
        reserved = {path.resolve() for path in reserved_paths or ()}
        # A profile keeps the darktable route reachable; a non-raw has to take it, since the raw developer only
        # decodes raws. Everything else — every raw in this library — renders through the raw developer.
        decoder = "darktable" if profile is not None or source.suffix.lower() not in RAW_EXTENSIONS else "coreimage"
        if decoder not in DECODERS:  # Provenance has to name the renderer; a new route declares itself here.
            raise ValueError(f"unknown export decoder: {decoder!r}")
        settings = {
            "preset": preset, "quality": options["quality"], "long_edge": options["long_edge"],
            "color_space": "sRGB", "include_gps": options["include_gps"], "caption_used": caption_used,
            "watermark": watermark is not None,
            "profile": str(profile.resolve()) if profile else None,
            "crop": crop, "source_fingerprint": "", "tool_version": "unknown", "decoder": decoder,
            "develop": develop, "command": [],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        command: tuple[str, ...] = ()
        published: Path | None = None
        work: Path | None = None
        try:
            if profile is not None and not profile.is_file():
                raise ValueError(f"profile does not exist: {profile}")
            if crop is not None:
                left, top, width, height = (crop[key] for key in ("left", "top", "width", "height"))
                if any(not isinstance(value, (int, float)) or isinstance(value, bool) for value in (left, top, width, height)) or not all(math.isfinite(value) for value in (left, top, width, height)) or not (
                    0 <= left < 1 and 0 <= top < 1 and width > 0 and height > 0
                    and left + width <= 1 and top + height <= 1
                ):
                    raise ValueError("crop must be a bounded normalized rectangle")
            if develop and decoder == "darktable":
                # Rendering through darktable with none of the develop settings applied would publish a JPEG that
                # disagrees with the preview the photographer approved.
                raise ValueError("saved develop settings need the raw developer, which cannot render "
                                 + ("an XMP profile: export with one or the other" if profile is not None
                                    else f"a {source.suffix or 'non-raw'} file: develop applies to raws only"))
            if decoder == "coreimage":
                if self.raw_developer is None or not Path(self.raw_developer).is_file():
                    raise ValueError("the raw developer is missing; run scripts/bootstrap.sh to build it")
                version = runner(["/usr/bin/sw_vers", "-productVersion"], capture_output=True, text=True, timeout=30, check=False)
                settings["tool_version"] = f"macOS {version.stdout.strip() or 'unknown'} Core Image"
            else:
                version = runner([self.executable, "--version"], capture_output=True, text=True, timeout=30, check=False)
                if version.returncode != 0:
                    raise RuntimeError(version.stderr.strip() or "could not probe darktable version")
                settings["tool_version"] = version.stdout.strip() or version.stderr.strip() or "unknown"
            with tempfile.TemporaryDirectory(prefix=".export-", dir=directory) as workspace:
                work = Path(workspace)
                (work / "source").mkdir()
                staged_source = work / "source" / source.name
                shutil.copyfile(source, staged_source)
                settings["source_fingerprint"] = fingerprint_file(staged_source)
                render = work / "render.tif"
                if decoder == "coreimage":
                    # DevelopError carries a photographer-safe message and is handled like any other failure below.
                    render_raw(staged_source, render, Path(self.raw_developer), settings=develop,
                               timeout=EXPORT_RENDER_TIMEOUT_SECONDS, runner=runner)
                    rendered = PipelineResult(True, 0, render, (str(self.raw_developer),), "")
                else:
                    rendered = self.run(staged_source, render, profile, runner=runner)
                command = rendered.command
                settings["command"] = list(command)
                if not rendered.success:
                    return JpegExportResult(False, rendered.returncode, destination, command, rendered.message, settings, "")
                with Image.open(rendered.output) as opened:
                    source_exif = opened.getexif()
                    descriptive = _descriptive_xmp(source_exif.get(700))
                    exif = Image.Exif()
                    for tag in (270, 271, 272, 305, 306, 315, 33432, 18246, 18249, 40091, 40092, 40093, 40094, 40095):
                        if tag in source_exif:
                            exif[tag] = source_exif[tag]
                    capture = dict(source_exif.get_ifd(34665))
                    capture.pop(40965, None)  # Interop IFD offset is meaningless in the new file.
                    capture[40961] = 1  # EXIF sRGB
                    if options["include_gps"] and (gps := source_exif.get_ifd(34853)):
                        exif[34853] = dict(gps)
                    exif[274] = 1  # Pixels are transposed below.
                    effective_description = description if description is not None else descriptive.get("description", source_exif.get(270, ""))
                    raw_keywords = source_exif.get(40094, b"")
                    exif_keywords = bytes(raw_keywords).decode("utf-16le", errors="replace").rstrip("\0")
                    effective_keywords = keywords if keywords is not None else descriptive.get("keywords", exif_keywords.split(";") if exif_keywords else [])
                    exif[270] = effective_description
                    exif[40094] = (";".join(effective_keywords) + "\0").encode("utf-16le")
                    effective_rating = rating if rating is not None else descriptive.get("rating", source_exif.get(18246, 0))
                    exif[18246] = max(0, effective_rating)  # EXIF Rating is unsigned; a rejected frame's -1 lives in the XMP.
                    image = ImageOps.exif_transpose(opened).convert("RGB")
                    if icc := opened.info.get("icc_profile"):
                        image = ImageCms.profileToProfile(image, ImageCms.ImageCmsProfile(io.BytesIO(icc)), ImageCms.createProfile("sRGB"), outputMode="RGB")
                    else:
                        raise ValueError(f"{decoder} render has no ICC profile; cannot guarantee sRGB")
                    if crop is not None:
                        image = image.crop((round(left * image.width), round(top * image.height), round((left + width) * image.width), round((top + height) * image.height)))
                    if long_edge := options["long_edge"]:
                        image.thumbnail((long_edge, long_edge), Image.Resampling.LANCZOS)  # Never upscales.
                    if watermark:
                        size = max(14, image.height // 40)
                        inset = round(0.02 * min(image.size))
                        ImageDraw.Draw(image).text((image.width - inset, image.height - inset), watermark, fill="white",
                                                   font=ImageFont.load_default(size=size), anchor="rd",
                                                   stroke_width=max(1, size // 12), stroke_fill="black")
                    capture[40962], capture[40963] = image.size  # After any crop and resize.
                    exif[34665] = capture
                    temporary = work / "finished.jpg"
                    image.save(temporary, format="JPEG", quality=options["quality"],
                               exif=exif, icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes(),
                               xmp=SidecarWriter._xmp(
                                   effective_description, effective_keywords, rating=effective_rating, provenance=_public_provenance(settings), crop=crop,
                                   develop=develop, alt_text=alt_text,
                                   place=place, country=country,  # A place name travels with every preset; GPS does not.
                               ).encode("utf-8"))
                if 37500 in capture:
                    # Pillow copies MakerNote bytes but not their absolute offsets; exiftool rebases them.
                    exiftool = find_tool("exiftool")
                    if not exiftool:
                        raise RuntimeError("exiftool is required to preserve the camera MakerNote")
                    copied = runner([exiftool, "-q", "-overwrite_original", "-TagsFromFile", str(rendered.output), "-MakerNotes", str(temporary)],
                                    capture_output=True, text=True, timeout=120, check=False)
                    if copied.returncode != 0:
                        raise RuntimeError(f"MakerNote copy failed: {(copied.stderr or copied.stdout).strip()[:200]}")
                fingerprint = fingerprint_file(temporary)
                # A hard link publishes the complete file atomically and fails if
                # another export claimed this name in the meantime.
                suffix = 0
                while True:
                    try:
                        if destination in reserved:
                            raise FileExistsError(f"historical artifact path is reserved: {destination}")
                        os.link(temporary, destination)
                        published = destination
                        break
                    except FileExistsError:
                        suffix += 1
                        destination = directory / f"{stem}-{suffix}.jpg"
            return JpegExportResult(True, 0, destination, command, "JPEG exported", settings, fingerprint)
        except Exception as error:
            errors = [str(error)]
            if published is not None:
                try:
                    published.unlink(missing_ok=True)
                except Exception as cleanup_error:
                    errors.append(f"JPEG cleanup failed ({published}): {cleanup_error}")
            if work is not None and work.exists():
                try:
                    shutil.rmtree(work)
                except Exception as cleanup_error:
                    errors.append(f"workspace cleanup failed ({work}): {cleanup_error}")
            return JpegExportResult(False, 1, destination, command, "; ".join(errors), settings, "")

    def command(self, source: Path, destination: Path, profile: Path | None = None) -> list[str]:
        destination = destination.resolve()
        try:
            destination.relative_to(self.output_root)
        except ValueError as error:
            raise ValueError("pipeline output must stay inside the managed output directory") from error
        arguments = [self.executable, str(source.resolve())]
        if profile is not None:
            arguments.append(str(profile.resolve()))
        # Pin EXIF (1) + geotag (4) export so renders don't depend on the user's darktablerc.
        arguments.extend([str(destination), "--width", "0", "--height", "0",
                          "--core", "--conf", "plugins/lighttable/export/metadata_flags=5"])
        return arguments

    def run(
        self,
        source: Path,
        destination: Path,
        profile: Path | None = None,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> PipelineResult:
        source = source.resolve()
        destination = destination.resolve()
        if source == destination:
            raise ValueError("pipeline output cannot replace its source original")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix=f"{destination.stem}.partial-",
            suffix=destination.suffix,
            dir=destination.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        temporary_path.unlink()
        command = self.command(source, temporary_path, profile)
        try:
            completed = runner(command, capture_output=True, text=True, timeout=1800, check=False)
        except subprocess.TimeoutExpired:
            temporary_path.unlink(missing_ok=True)
            return PipelineResult(False, 124, destination, tuple(command), "darktable timed out after 1800 seconds")
        success = completed.returncode == 0 and temporary_path.exists()
        message = completed.stderr.strip() or completed.stdout.strip()
        if success:
            temporary_path.replace(destination)
        else:
            temporary_path.unlink(missing_ok=True)
        if completed.returncode == 0 and not success:
            message = "darktable exited successfully but produced no output"
        return PipelineResult(success, completed.returncode, destination, tuple(command), message)


class SirilPipeline:
    def __init__(self, executable: str, output_root: Path):
        self.executable = executable
        self.output_root = output_root.resolve()

    def command(self, script: Path) -> list[str]:
        script = script.resolve()
        try:
            script.relative_to(self.output_root)
        except ValueError as error:
            raise ValueError("Siril scripts must stay inside the managed output directory") from error
        if script.suffix.lower() not in {".ssf", ".siril"}:
            raise ValueError("Siril hook requires an .ssf or .siril script")
        return [self.executable, "-s", str(script)]

    def job_command(self, job_dir: Path) -> list[str]:
        """Managed astro batch run: job-local cwd and config, offline, argument array only."""
        job_dir = job_dir.resolve()
        if job_dir.parent != self.output_root / "astro":
            raise ValueError("Siril jobs must run inside the managed astro output directory")
        return [self.executable, "-d", str(job_dir), "-i", str(job_dir / "siril.config"), "-o", "-s", str(job_dir / "stack.ssf")]

    def run(
        self,
        script: Path,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> PipelineResult:
        command = self.command(script)
        try:
            completed = runner(command, capture_output=True, text=True, timeout=7200, check=False)
        except subprocess.TimeoutExpired:
            return PipelineResult(False, 124, script, tuple(command), "Siril timed out after 7200 seconds")
        message = completed.stderr.strip() or completed.stdout.strip()
        return PipelineResult(completed.returncode == 0, completed.returncode, script, tuple(command), message)


class PipelineRegistry:
    def capabilities(self) -> dict[str, Capability]:
        darktable = find_tool("darktable-cli")
        siril = find_tool("siril-cli")
        return {
            "darktable": Capability(
                "darktable",
                darktable is not None,
                darktable,
                "RAW export and denoising with a user-supplied XMP profile",
            ),
            "siril": Capability(
                "siril",
                siril is not None,
                siril,
                "Astrophotography calibration and stacking scripts",
            ),
        }

    def darktable(self, output_root: Path, raw_developer: Path | None = None) -> DarktablePipeline:
        executable = find_tool("darktable-cli")
        if not executable:
            raise RuntimeError("darktable-cli is not installed")
        return DarktablePipeline(executable, output_root, raw_developer)

    def siril(self, output_root: Path) -> SirilPipeline:
        executable = find_tool("siril-cli")
        if not executable:
            raise RuntimeError("siril-cli is not installed")
        return SirilPipeline(executable, output_root)
