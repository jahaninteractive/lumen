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

from PIL import Image, ImageCms, ImageOps

from .previews import fingerprint_file
from .sidecars import SidecarWriter
from .tools import find_tool


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


class DarktablePipeline:
    def __init__(self, executable: str, output_root: Path):
        self.executable = executable
        self.output_root = output_root.resolve()

    def export_jpeg(
        self, source: Path, profile: Path | None = None, *,
        crop: dict[str, object] | None = None, description: str | None = None,
        keywords: list[str] | None = None, rating: int | None = None,
        reserved_paths: set[Path] | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> JpegExportResult:
        runner = runner or subprocess.run
        source = source.resolve()
        directory = (self.output_root / "jpeg").resolve()
        if not directory.is_relative_to(self.output_root):
            raise ValueError("JPEG output must stay inside the managed output directory")
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"{source.stem}-lumen.jpg"
        reserved = {path.resolve() for path in reserved_paths or ()}
        settings = {
            "quality": 92, "color_space": "sRGB", "include_gps": False,
            "profile": str(profile.resolve()) if profile else None,
            "crop": crop, "source_fingerprint": "", "tool_version": "unknown",
            "command": [],
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
                rendered = self.run(staged_source, work / "render.tif", profile, runner=runner)
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
                    capture = source_exif.get_ifd(34665)
                    approved = {tag: capture[tag] for tag in (33434, 33437, 34850, 34855, 36864, 36867, 36868, 36880, 36881, 36882, 37377, 37378, 37380, 37381, 37383, 37385, 37386, 37521, 37522, 41986, 41987, 41988, 41989, 42032, 42033, 42034, 42035, 42036, 42037) if tag in capture}
                    approved[40961] = 1  # EXIF sRGB
                    exif[34665] = approved
                    exif[274] = 1  # Pixels are transposed below.
                    effective_description = description if description is not None else descriptive.get("description", source_exif.get(270, ""))
                    raw_keywords = source_exif.get(40094, b"")
                    exif_keywords = bytes(raw_keywords).decode("utf-16le", errors="replace").rstrip("\0")
                    effective_keywords = keywords if keywords is not None else descriptive.get("keywords", exif_keywords.split(";") if exif_keywords else [])
                    exif[270] = effective_description
                    exif[40094] = (";".join(effective_keywords) + "\0").encode("utf-16le")
                    effective_rating = rating if rating is not None else descriptive.get("rating", source_exif.get(18246, 0))
                    exif[18246] = effective_rating
                    image = ImageOps.exif_transpose(opened).convert("RGB")
                    if icc := opened.info.get("icc_profile"):
                        image = ImageCms.profileToProfile(image, ImageCms.ImageCmsProfile(io.BytesIO(icc)), ImageCms.createProfile("sRGB"), outputMode="RGB")
                    else:
                        raise ValueError("darktable render has no ICC profile; cannot guarantee sRGB")
                    if crop is not None:
                        image = image.crop((round(left * image.width), round(top * image.height), round((left + width) * image.width), round((top + height) * image.height)))
                    temporary = work / "finished.jpg"
                    image.save(temporary, format="JPEG", quality=92,
                               exif=exif, icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes(),
                               xmp=SidecarWriter._xmp(
                                   effective_description, effective_keywords, effective_rating == 5,
                                   rating=effective_rating, provenance=settings, crop=crop,
                               ).encode("utf-8"))
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
                        destination = directory / f"{source.stem}-lumen-{suffix}.jpg"
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
        arguments.extend([str(destination), "--width", "0", "--height", "0"])
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

    def darktable(self, output_root: Path) -> DarktablePipeline:
        executable = find_tool("darktable-cli")
        if not executable:
            raise RuntimeError("darktable-cli is not installed")
        return DarktablePipeline(executable, output_root)

    def siril(self, output_root: Path) -> SirilPipeline:
        executable = find_tool("siril-cli")
        if not executable:
            raise RuntimeError("siril-cli is not installed")
        return SirilPipeline(executable, output_root)
