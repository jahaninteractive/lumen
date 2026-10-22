import subprocess
import io
import tempfile
import unittest
import json
from xml.etree import ElementTree
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageCms

from lumen.pipelines import DarktablePipeline, PipelineRegistry, SirilPipeline


class PipelineTests(unittest.TestCase):
    def test_jpeg_export_preserves_only_approved_tiff_xmp_with_explicit_overrides(self):
        xmp_packet = b'''<x:xmpmeta xmlns:x="adobe:ns:meta/">
          <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
            <rdf:Description xmlns:xmp="http://ns.adobe.com/xap/1.0/"
              xmlns:dc="http://purl.org/dc/elements/1.1/"
              xmlns:exif="http://ns.adobe.com/exif/1.0/"
              xmlns:darktable="http://darktable.sf.net/" xmp:Rating="3"
              exif:GPSLatitude="33,55S" darktable:history="private processing">
              <dc:description><rdf:Alt><rdf:li xml:lang="x-default">XMP bird &amp; branch</rdf:li></rdf:Alt></dc:description>
              <dc:subject><rdf:Bag><rdf:li>bird</rdf:li><rdf:li>garden</rdf:li></rdf:Bag></dc:subject>
            </rdf:Description>
          </rdf:RDF></x:xmpmeta>'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.ARW"
            source.write_bytes(b"raw")
            def render(command, **kwargs):
                if command[1:] == ["--version"]:
                    return subprocess.CompletedProcess(command, 0, "darktable 5.2", "")
                Image.new("RGB", (80, 60), "red").save(command[-5], format="TIFF", tiffinfo={700: xmp_packet}, icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
                return subprocess.CompletedProcess(command, 0, "", "")
            pipeline = DarktablePipeline("darktable-cli", root / "outputs")
            for overrides, description, keywords, rating in (
                ({}, "XMP bird & branch", ["bird", "garden"], 3),
                ({"description": "Catalog caption", "keywords": ["keeper"], "rating": 5}, "Catalog caption", ["keeper"], 5),
                ({"description": "", "keywords": [], "rating": 0}, "", [], 0),
            ):
                with self.subTest(overrides=overrides):
                    result = pipeline.export_jpeg(source, runner=render, **overrides)
                    self.assertTrue(result.success, result.message)
                    with Image.open(result.output) as image:
                        exif = image.getexif()
                        self.assertEqual(description, exif[270])
                        stored_keywords = exif[40094].decode("utf-16le").rstrip("\0")
                        self.assertEqual(keywords, stored_keywords.split(";") if stored_keywords else [])
                        self.assertEqual(rating, exif[18246])
                        packet = image.info["xmp"]
                        self.assertNotIn(b"GPSLatitude", packet)
                        self.assertNotIn(b"private processing", packet)
                        self.assertNotIn(34853, exif)
                        parsed = ElementTree.fromstring(packet)
                        ns = {"rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#", "dc": "http://purl.org/dc/elements/1.1/"}
                        self.assertEqual(str(rating), parsed.find(".//rdf:Description", ns).attrib["{http://ns.adobe.com/xap/1.0/}Rating"])
                        self.assertEqual(description, parsed.find(".//dc:description/rdf:Alt/rdf:li", ns).text or "")
                        self.assertEqual(keywords, [item.text for item in parsed.findall(".//dc:subject/rdf:Bag/rdf:li", ns)])

    def test_jpeg_export_preserves_metadata_and_original_with_collision_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.ARW"
            source.write_bytes(b"original RAW")
            adjacent = source.with_suffix(".ARW.xmp")
            adjacent.write_text("must not be implicitly adopted")
            before = (source.read_bytes(), source.stat().st_mtime_ns)
            profile = root / "explicit.xmp"
            profile.write_text("profile")
            pipeline = DarktablePipeline("darktable-cli", root / "outputs")
            commands = []

            def render(command, **kwargs):
                commands.append(command)
                if command[1:] == ["--version"]:
                    return subprocess.CompletedProcess(command, 0, "darktable 5.2", "")
                staged_source = Path(command[1])
                self.assertNotEqual(source, staged_source)
                self.assertTrue(staged_source.is_relative_to((root / "outputs" / "jpeg").resolve()))
                self.assertEqual(b"original RAW", staged_source.read_bytes())
                self.assertFalse(staged_source.with_suffix(".ARW.xmp").exists())
                self.assertEqual(str(profile.resolve()), command[2])
                self.assertEqual(["--width", "0", "--height", "0"], command[-4:])
                exif = Image.Exif()
                exif[271] = "Sony"
                exif[272] = "ILCE-7M4"
                exif[274] = 1
                exif[315] = "Photographer"
                exif[33432] = "Copyright photographer"
                exif[18246] = 3
                exif[34665] = {36867: "2026:10:21 10:00:00", 42036: "FE 50mm"}
                exif[34853] = {1: "N", 2: (1, 2, 3)}
                Image.new("RGB", (80, 60), "red").save(
                    command[-5], format="TIFF", exif=exif.tobytes(),
                    icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes(),
                )
                return subprocess.CompletedProcess(command, 0, "", "")

            first = pipeline.export_jpeg(source, profile, description="A red bird", keywords=["bird"], rating=5, runner=render)
            second = pipeline.export_jpeg(source, profile, runner=render)
            self.assertTrue(first.success, first.message)
            self.assertEqual((root / "outputs" / "jpeg" / "source-lumen.jpg").resolve(), first.output)
            self.assertEqual((root / "outputs" / "jpeg" / "source-lumen-1.jpg").resolve(), second.output)
            with Image.open(first.output) as image:
                self.assertEqual((80, 60), image.size)
                # Standard JPEG luminance table scaled for quality 92.
                self.assertEqual([3, 2, 2, 3, 4, 6, 8, 10], image.quantization[0][:8])
                self.assertIn("sRGB", ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(image.info["icc_profile"]))))
                exif = image.getexif()
                self.assertNotIn(34853, exif)
                self.assertEqual("Sony", exif[271])
                self.assertEqual("ILCE-7M4", exif[272])
                self.assertEqual("Copyright photographer", exif[33432])
                self.assertEqual("2026:10:21 10:00:00", exif.get_ifd(34665)[36867])
                self.assertEqual("FE 50mm", exif.get_ifd(34665)[42036])
                self.assertEqual("A red bird", exif[270])
                self.assertEqual(5, exif[18246])
                self.assertEqual("bird\0", exif[40094].decode("utf-16le"))
                xmp = ElementTree.fromstring(image.info["xmp"])
                provenance = json.loads(xmp.find(".//{https://lumen.local/ns/1.0/}export").text)
                self.assertEqual(92, provenance["quality"])
                self.assertEqual("darktable 5.2", provenance["tool_version"])
                self.assertEqual(str(profile.resolve()), provenance["profile"])
                self.assertEqual(64, len(provenance["source_fingerprint"]))
                self.assertTrue(provenance["created_at"])
            self.assertEqual(before, (source.read_bytes(), source.stat().st_mtime_ns))
            with Image.open(second.output) as image:
                self.assertEqual(3, image.getexif()[18246])
                xmp = ElementTree.fromstring(image.info["xmp"])
                self.assertEqual("3", xmp.find(".//{http://www.w3.org/1999/02/22-rdf-syntax-ns#}Description").attrib["{http://ns.adobe.com/xap/1.0/}Rating"])
            self.assertEqual(2, len(list((root / "outputs" / "jpeg").iterdir())))

    def test_jpeg_export_runner_failure_leaves_no_final_or_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.ARW"
            source.write_bytes(b"raw")
            pipeline = DarktablePipeline("darktable-cli", root / "outputs")
            def fail(command, **kwargs):
                if command[1:] == ["--version"]:
                    return subprocess.CompletedProcess(command, 0, "darktable 5.2", "")
                Path(command[-5]).write_bytes(b"partial")
                raise OSError("runner unavailable")
            result = pipeline.export_jpeg(source, runner=fail)
            self.assertFalse(result.success)
            self.assertIn("runner unavailable", result.message)
            self.assertEqual([], list((root / "outputs" / "jpeg").iterdir()))

    def test_jpeg_export_applies_normalized_crop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.ARW"
            source.write_bytes(b"raw")
            def render(command, **kwargs):
                if command[1:] == ["--version"]:
                    return subprocess.CompletedProcess(command, 0, "darktable 5.2", "")
                Image.new("RGB", (80, 60), "red").save(command[-5], format="TIFF", icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
                return subprocess.CompletedProcess(command, 0, "", "")
            result = DarktablePipeline("darktable-cli", root / "outputs").export_jpeg(source, crop={"left": .25, "top": 0, "width": .5, "height": 1}, runner=render)
            self.assertTrue(result.success, result.message)
            with Image.open(result.output) as image:
                self.assertEqual((40, 60), image.size)

    def test_jpeg_crop_uses_exact_full_resolution_pixels_and_keeps_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.ARW"
            source.write_bytes(b"raw source")
            before = source.read_bytes()
            def render(command, **kwargs):
                if command[1:] == ["--version"]:
                    return subprocess.CompletedProcess(command, 0, "darktable 5.2", "")
                image = Image.new("RGB", (80, 60), "red")
                for x in range(20, 60):
                    for y in range(0, 60):
                        image.putpixel((x, y), (0, 255, 0))
                image.save(command[-5], format="TIFF", icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
                return subprocess.CompletedProcess(command, 0, "", "")

            result = DarktablePipeline("darktable-cli", root / "outputs").export_jpeg(
                source, crop={"left": .25, "top": 0, "width": .5, "height": 1}, runner=render,
            )

            self.assertTrue(result.success, result.message)
            with Image.open(result.output) as image:
                self.assertEqual((40, 60), image.size)
                self.assertGreater(image.convert("RGB").getpixel((20, 30))[1], 220)
            self.assertEqual(before, source.read_bytes())

    def test_jpeg_export_rejects_unknown_color_profile_without_publishing(self):
        for icc in (None, b"invalid ICC"):
            with self.subTest(icc=icc), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "source.ARW"
                source.write_bytes(b"raw")
                def render(command, **kwargs):
                    if command[1:] == ["--version"]:
                        return subprocess.CompletedProcess(command, 0, "darktable 5.2", "")
                    Image.new("RGB", (80, 60), "red").save(command[-5], format="TIFF", icc_profile=icc)
                    return subprocess.CompletedProcess(command, 0, "", "")
                result = DarktablePipeline("darktable-cli", root / "outputs").export_jpeg(source, runner=render)
                self.assertFalse(result.success)
                self.assertEqual([], list((root / "outputs" / "jpeg").iterdir()))

    def test_jpeg_export_refuses_a_symlink_outside_managed_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outputs = root / "outputs"
            outputs.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (outputs / "jpeg").symlink_to(outside, target_is_directory=True)
            pipeline = DarktablePipeline("darktable-cli", outputs)
            with self.assertRaises(ValueError):
                pipeline.export_jpeg(root / "source.ARW")
            self.assertEqual([], list(outside.iterdir()))

    def test_missing_tools_are_reported_as_unavailable(self):
        with tempfile.TemporaryDirectory() as empty, patch("lumen.tools.shutil.which", return_value=None), patch(
            "lumen.tools.VENDOR_APPS", Path(empty)
        ):
            capabilities = PipelineRegistry().capabilities()
        self.assertFalse(capabilities["darktable"].available)
        self.assertFalse(capabilities["siril"].available)

    def test_darktable_builds_argument_array_with_optional_xmp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.ARW"
            profile = root / "denoise.xmp"
            destination = root / "outputs" / "clean.tif"
            source.write_bytes(b"raw")
            profile.write_text("xmp")
            pipeline = DarktablePipeline(executable="/usr/local/bin/darktable-cli", output_root=root / "outputs")

            command = pipeline.command(source, destination, profile)

        self.assertEqual(
            [
                "/usr/local/bin/darktable-cli",
                str(source.resolve()),
                str(profile.resolve()),
                str(destination.resolve()),
                "--width",
                "0",
                "--height",
                "0",
            ],
            command,
        )

    def test_failed_pipeline_returns_diagnostic_without_creating_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.ARW"
            source.write_bytes(b"raw")
            pipeline = DarktablePipeline(executable="darktable-cli", output_root=root / "outputs")

            def failing_runner(*args, **kwargs):
                return subprocess.CompletedProcess(args[0], 3, stdout="", stderr="bad profile")

            result = pipeline.run(source, root / "outputs" / "clean.tif", runner=failing_runner)

        self.assertFalse(result.success)
        self.assertEqual(3, result.returncode)
        self.assertIn("bad profile", result.message)

    def test_output_cannot_escape_managed_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pipeline = DarktablePipeline(executable="darktable-cli", output_root=root / "outputs")
            with self.assertRaises(ValueError):
                pipeline.command(root / "source.ARW", root / "outside.tif")

    def test_output_cannot_replace_the_source(self):
        with tempfile.TemporaryDirectory() as directory:
            output_root = Path(directory) / "outputs"
            output_root.mkdir()
            source = output_root / "original.tif"
            source.write_bytes(b"original")
            pipeline = DarktablePipeline("darktable-cli", output_root)

            with self.assertRaises(ValueError):
                pipeline.run(source, source)

            self.assertEqual(b"original", source.read_bytes())

    def test_siril_hook_only_runs_managed_scripts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_root = root / "outputs"
            output_root.mkdir()
            script = output_root / "stack.ssf"
            script.write_text("requires 1.4.0\n")
            pipeline = SirilPipeline(executable="siril-cli", output_root=output_root)

            self.assertEqual(["siril-cli", "-s", str(script.resolve())], pipeline.command(script))
            with self.assertRaises(ValueError):
                pipeline.command(root / "outside.ssf")

    def test_failed_darktable_run_removes_partial_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.ARW"
            source.write_bytes(b"raw")
            destination = root / "outputs" / "clean.tif"
            pipeline = DarktablePipeline("darktable-cli", root / "outputs")

            def partial_runner(command, **kwargs):
                Path(command[-5]).write_bytes(b"partial")
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="failed")

            result = pipeline.run(source, destination, runner=partial_runner)

            self.assertFalse(result.success)
            self.assertFalse(destination.exists())
            self.assertEqual([], list(destination.parent.glob("*.partial-*")))

    def test_darktable_timeout_returns_failure_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.ARW"
            source.write_bytes(b"raw")
            destination = root / "outputs" / "clean.tif"
            pipeline = DarktablePipeline("darktable-cli", root / "outputs")

            def timeout_runner(*args, **kwargs):
                raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

            result = pipeline.run(source, destination, runner=timeout_runner)

        self.assertFalse(result.success)
        self.assertEqual(124, result.returncode)
        self.assertIn("timed out", result.message)


if __name__ == "__main__":
    unittest.main()
