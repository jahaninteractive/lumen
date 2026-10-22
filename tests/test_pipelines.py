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
                Image.new("RGB", (80, 60), "red").save(command[-8], format="TIFF", tiffinfo={700: xmp_packet}, icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
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
                        self.assertNotIn(b"private processing", packet)
                        parsed = ElementTree.fromstring(packet)
                        ns = {"rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#", "dc": "http://purl.org/dc/elements/1.1/"}
                        self.assertEqual(str(rating), parsed.find(".//rdf:Description", ns).attrib["{http://ns.adobe.com/xap/1.0/}Rating"])
                        self.assertEqual(description, parsed.find(".//dc:description/rdf:Alt/rdf:li", ns).text or "")
                        self.assertEqual(keywords, [item.text for item in parsed.findall(".//dc:subject/rdf:Bag/rdf:li", ns)])

    def test_jpeg_export_can_render_through_the_macos_decoder_without_darktable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "DSC00712.ARW"
            source.write_bytes(b"raw")
            calls = []

            def render(command, **kwargs):
                calls.append(command)
                self.assertNotEqual("darktable-cli", command[0], "darktable must not run for the macOS decoder")
                if command[0].endswith("sw_vers"):
                    return subprocess.CompletedProcess(command, 0, "26.5.1\n", "")
                self.assertTrue(command[0].endswith("/sips"))
                self.assertIn("--matchTo", command)
                Image.new("RGB", (80, 60), "red").save(command[command.index("--out") + 1], format="TIFF",
                                                     icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
                return subprocess.CompletedProcess(command, 0, "", "")

            pipeline = DarktablePipeline("darktable-cli", root / "outputs")
            result = pipeline.export_jpeg(source, decoder="macos", runner=render)
            self.assertTrue(result.success, result.message)
            self.assertEqual("macos", result.settings["decoder"])
            self.assertEqual("macOS 26.5.1 sips", result.settings["tool_version"])
            self.assertIn("macOS decoder", result.message)
            self.assertTrue(result.command[0].endswith("/sips"))
            with Image.open(result.output) as image:
                self.assertIn(b"macos", image.info["xmp"])

            profile = root / "look.xmp"
            profile.write_text("<x:xmpmeta/>")
            refused = pipeline.export_jpeg(source, profile, decoder="macos", runner=render)
            self.assertFalse(refused.success)
            self.assertIn("darktable", refused.message)
            with self.assertRaises(ValueError):
                pipeline.export_jpeg(source, decoder="gimp", runner=render)

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
                if command[0] == "/usr/bin/exiftool":
                    work = Path(command[-1]).parent
                    self.assertEqual(["/usr/bin/exiftool", "-q", "-overwrite_original", "-TagsFromFile", str(work / "render.tif"), "-MakerNotes", str(work / "finished.jpg")], command)
                    self.assertTrue(Path(command[-1]).is_file())
                    return subprocess.CompletedProcess(command, 0, "", "")
                staged_source = Path(command[1])
                self.assertNotEqual(source, staged_source)
                self.assertTrue(staged_source.is_relative_to((root / "outputs" / "jpeg").resolve()))
                self.assertEqual(b"original RAW", staged_source.read_bytes())
                self.assertFalse(staged_source.with_suffix(".ARW.xmp").exists())
                self.assertEqual(str(profile.resolve()), command[2])
                self.assertEqual(["--width", "0", "--height", "0", "--core", "--conf", "plugins/lighttable/export/metadata_flags=5"], command[-7:])
                exif = Image.Exif()
                exif[271] = "Sony"
                exif[272] = "ILCE-7M4"
                exif[274] = 1
                exif[315] = "Photographer"
                exif[33432] = "Copyright photographer"
                exif[18246] = 3
                exif[34665] = {36867: "2026:10:21 10:00:00", 42036: "FE 50mm", 34864: 2, 37500: b"SONY DSC makernote", 40962: 7032, 40963: 4688}
                exif[34853] = {1: "N", 2: (1.0, 2.0, 3.0), 5: 0, 6: 12.0}
                Image.new("RGB", (80, 60), "red").save(
                    command[-8], format="TIFF", exif=exif.tobytes(),
                    icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes(),
                )
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("lumen.pipelines.find_tool", return_value="/usr/bin/exiftool"):
                first = pipeline.export_jpeg(source, profile, description="A red bird", keywords=["bird"], rating=5, runner=render)
                second = pipeline.export_jpeg(source, profile, runner=render)
            self.assertEqual(2, sum(command[0] == "/usr/bin/exiftool" for command in commands))
            self.assertTrue(first.success, first.message)
            self.assertEqual((root / "outputs" / "jpeg" / "source-lumen.jpg").resolve(), first.output)
            self.assertEqual((root / "outputs" / "jpeg" / "source-lumen-1.jpg").resolve(), second.output)
            with Image.open(first.output) as image:
                self.assertEqual((80, 60), image.size)
                # Standard JPEG luminance table scaled for quality 92.
                self.assertEqual([3, 2, 2, 3, 4, 6, 8, 10], image.quantization[0][:8])
                self.assertIn("sRGB", ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(image.info["icc_profile"]))))
                exif = image.getexif()
                self.assertEqual({1: "N", 2: (1.0, 2.0, 3.0), 5: b"\x00", 6: 12.0}, dict(exif.get_ifd(34853)))
                self.assertEqual(2, exif.get_ifd(34665)[34864])
                self.assertEqual((80, 60), (exif.get_ifd(34665)[40962], exif.get_ifd(34665)[40963]))
                self.assertEqual(b"SONY DSC makernote", exif.get_ifd(34665)[37500])
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
                self.assertEqual("explicit.xmp", provenance["profile"])
                self.assertNotIn("source_fingerprint", provenance)
                self.assertNotIn("command", provenance)
                self.assertTrue(provenance["created_at"])
                self.assertNotIn(str(root.resolve()).encode(), image.info["xmp"])
            self.assertEqual(str(profile.resolve()), first.settings["profile"])
            self.assertEqual(64, len(first.settings["source_fingerprint"]))
            self.assertEqual(before, (source.read_bytes(), source.stat().st_mtime_ns))
            with Image.open(second.output) as image:
                self.assertEqual(3, image.getexif()[18246])
                xmp = ElementTree.fromstring(image.info["xmp"])
                self.assertEqual("3", xmp.find(".//{http://www.w3.org/1999/02/22-rdf-syntax-ns#}Description").attrib["{http://ns.adobe.com/xap/1.0/}Rating"])
            self.assertEqual(2, len(list((root / "outputs" / "jpeg").iterdir())))

    def test_makernote_transplant_failure_or_missing_exiftool_fails_export(self):
        for exiftool, outcome in (("/usr/bin/exiftool", 1), (None, None)):
            with self.subTest(exiftool=exiftool), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "source.ARW"
                source.write_bytes(b"raw")
                pipeline = DarktablePipeline("darktable-cli", root / "outputs")
                def render(command, **kwargs):
                    if command[1:] == ["--version"]:
                        return subprocess.CompletedProcess(command, 0, "darktable 5.2", "")
                    if command[0] == "/usr/bin/exiftool":
                        return subprocess.CompletedProcess(command, outcome, "", "bad maker notes")
                    exif = Image.Exif()
                    exif[34665] = {37500: b"SONY DSC makernote"}
                    Image.new("RGB", (8, 6), "red").save(command[-8], format="TIFF", exif=exif.tobytes(), icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
                    return subprocess.CompletedProcess(command, 0, "", "")
                with patch("lumen.pipelines.find_tool", return_value=exiftool):
                    result = pipeline.export_jpeg(source, runner=render)
                self.assertFalse(result.success)
                self.assertIn("MakerNote", result.message)
                self.assertEqual([], list((root / "outputs" / "jpeg").iterdir()))

    def test_jpeg_export_runner_failure_leaves_no_final_or_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.ARW"
            source.write_bytes(b"raw")
            pipeline = DarktablePipeline("darktable-cli", root / "outputs")
            def fail(command, **kwargs):
                if command[1:] == ["--version"]:
                    return subprocess.CompletedProcess(command, 0, "darktable 5.2", "")
                Path(command[-8]).write_bytes(b"partial")
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
                Image.new("RGB", (80, 60), "red").save(command[-8], format="TIFF", icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
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
                image.save(command[-8], format="TIFF", icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
                return subprocess.CompletedProcess(command, 0, "", "")

            result = DarktablePipeline("darktable-cli", root / "outputs").export_jpeg(
                source, crop={"left": .25, "top": 0, "width": .5, "height": 1}, runner=render,
            )

            self.assertTrue(result.success, result.message)
            with Image.open(result.output) as image:
                self.assertEqual((40, 60), image.size)
                self.assertGreater(image.convert("RGB").getpixel((20, 30))[1], 220)
            self.assertEqual(before, source.read_bytes())

    @staticmethod
    def preset_render(size, *, paint=None):
        """Fake darktable run writing an sRGB TIFF with camera EXIF and a GPS IFD."""
        def render(command, **kwargs):
            if command[1:] == ["--version"]:
                return subprocess.CompletedProcess(command, 0, "darktable 5.2", "")
            exif = Image.Exif()
            exif[271] = "Sony"
            exif[270] = "Rendered caption"
            exif[34665] = {36867: "2026:10:21 10:00:00", 40962: size[0], 40963: size[1]}
            exif[34853] = {1: "N", 2: (1.0, 2.0, 3.0), 5: 0, 6: 12.0}
            image = Image.new("RGB", size, "red")
            if paint:
                paint(image)
            image.save(command[-8], format="TIFF", exif=exif.tobytes(), icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
            return subprocess.CompletedProcess(command, 0, "", "")
        return render

    def test_share_preset_resizes_render_to_2048_long_edge_at_quality_85_without_gps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.ARW"
            source.write_bytes(b"raw")
            result = DarktablePipeline("darktable-cli", root / "outputs").export_jpeg(
                source, description="A red bird", keywords=["bird", "travel"], rating=4, preset="share",
                runner=self.preset_render((4000, 2600)))
            self.assertTrue(result.success, result.message)
            self.assertEqual("source-lumen-share.jpg", result.output.name)
            self.assertEqual({"preset": "share", "quality": 85, "long_edge": 2048, "include_gps": False},
                             {key: result.settings[key] for key in ("preset", "quality", "long_edge", "include_gps")})
            with Image.open(result.output) as image:
                self.assertEqual((2048, 1331), image.size)
                # Standard JPEG luminance table scaled for quality 85.
                self.assertEqual([5, 3, 3, 5, 7, 12, 15, 18], image.quantization[0][:8])
                self.assertIn("sRGB", ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(image.info["icc_profile"]))))
                exif = image.getexif()
                self.assertNotIn(34853, exif)
                self.assertEqual({}, dict(exif.get_ifd(34853)))
                self.assertEqual("Sony", exif[271])
                self.assertEqual("2026:10:21 10:00:00", exif.get_ifd(34665)[36867])
                self.assertEqual((2048, 1331), (exif.get_ifd(34665)[40962], exif.get_ifd(34665)[40963]))
                self.assertEqual("A red bird", exif[270])
                self.assertEqual("bird;travel\0", exif[40094].decode("utf-16le"))
                self.assertEqual(4, exif[18246])
                xmp = ElementTree.fromstring(image.info["xmp"])
                self.assertEqual("4", xmp.find(".//{http://www.w3.org/1999/02/22-rdf-syntax-ns#}Description").attrib["{http://ns.adobe.com/xap/1.0/}Rating"])
                provenance = json.loads(xmp.find(".//{https://lumen.local/ns/1.0/}export").text)
                self.assertEqual(("share", 85, 2048, False), (provenance["preset"], provenance["quality"], provenance["long_edge"], provenance["include_gps"]))

    def test_full_preset_is_unchanged_and_keeps_gps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.ARW"
            source.write_bytes(b"raw")
            pipeline = DarktablePipeline("darktable-cli", root / "outputs")
            default = pipeline.export_jpeg(source, runner=self.preset_render((2400, 1600)))
            explicit = pipeline.export_jpeg(source, preset="full", runner=self.preset_render((2400, 1600)))
            for result in (default, explicit):
                self.assertTrue(result.success, result.message)
                self.assertEqual({"preset": "full", "quality": 92, "long_edge": None, "include_gps": True},
                                 {key: result.settings[key] for key in ("preset", "quality", "long_edge", "include_gps")})
                with Image.open(result.output) as image:
                    self.assertEqual((2400, 1600), image.size)
                    self.assertEqual([3, 2, 2, 3, 4, 6, 8, 10], image.quantization[0][:8])
                    self.assertEqual({1: "N", 2: (1.0, 2.0, 3.0), 5: b"\x00", 6: 12.0}, dict(image.getexif().get_ifd(34853)))
            self.assertEqual(["source-lumen-1.jpg", "source-lumen.jpg"], sorted(path.name for path in (default.output, explicit.output)))
            with self.assertRaises(ValueError):
                pipeline.export_jpeg(source, preset="web", runner=self.preset_render((80, 60)))

    def test_share_filenames_use_share_suffix_and_collision_numbering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.ARW"
            source.write_bytes(b"raw")
            pipeline = DarktablePipeline("darktable-cli", root / "outputs")
            jpeg = (root / "outputs" / "jpeg").resolve()
            render = self.preset_render((80, 60))
            full = pipeline.export_jpeg(source, runner=render)
            first = pipeline.export_jpeg(source, preset="share", runner=render)
            second = pipeline.export_jpeg(source, preset="share", runner=render,
                                          reserved_paths={jpeg / "source-lumen-share-1.jpg"})
            self.assertEqual([jpeg / "source-lumen.jpg", jpeg / "source-lumen-share.jpg", jpeg / "source-lumen-share-2.jpg"],
                             [full.output, first.output, second.output])
            self.assertFalse((jpeg / "source-lumen-share-1.jpg").exists())
            with Image.open(first.output) as image:
                self.assertEqual((80, 60), image.size)  # Share never upscales a small render.

    def test_share_applies_crop_before_resize(self):
        def paint(image):
            image.paste((0, 255, 0), (1250, 0, 3750, 2500))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.ARW"
            source.write_bytes(b"raw")
            result = DarktablePipeline("darktable-cli", root / "outputs").export_jpeg(
                source, crop={"left": .25, "top": 0, "width": .5, "height": 1}, preset="share",
                runner=self.preset_render((5000, 2500), paint=paint))
            self.assertTrue(result.success, result.message)
            with Image.open(result.output) as image:
                self.assertEqual((2048, 2048), image.size)
                rgb = image.convert("RGB")
                for point in ((2, 2), (1024, 1024), (2045, 2045)):
                    self.assertGreater(rgb.getpixel(point)[1], 220)
                    self.assertLess(rgb.getpixel(point)[0], 40)

    def test_jpeg_export_rejects_unknown_color_profile_without_publishing(self):
        for icc in (None, b"invalid ICC"):
            with self.subTest(icc=icc), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "source.ARW"
                source.write_bytes(b"raw")
                def render(command, **kwargs):
                    if command[1:] == ["--version"]:
                        return subprocess.CompletedProcess(command, 0, "darktable 5.2", "")
                    Image.new("RGB", (80, 60), "red").save(command[-8], format="TIFF", icc_profile=icc)
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
                "--core",
                "--conf",
                "plugins/lighttable/export/metadata_flags=5",
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

    def test_siril_job_command_is_an_offline_argument_array_inside_managed_astro(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            output_root = root / "Lumen Library ✨" / "outputs"
            job = output_root / "astro" / "12"
            job.mkdir(parents=True)
            (output_root / "astro" / "linked").symlink_to(root, target_is_directory=True)
            pipeline = SirilPipeline(executable="/Apps/siril-cli", output_root=output_root)
            self.assertEqual(
                ["/Apps/siril-cli", "-d", str(job), "-i", str(job / "siril.config"), "-o", "-s", str(job / "stack.ssf")],
                pipeline.job_command(job),
            )
            for outside in (output_root, output_root / "astro", output_root / "jpeg" / "12", job / "nested", root / "12", output_root / "astro" / "linked"):
                with self.subTest(outside=outside), self.assertRaises(ValueError):
                    pipeline.job_command(outside)

    def test_failed_darktable_run_removes_partial_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.ARW"
            source.write_bytes(b"raw")
            destination = root / "outputs" / "clean.tif"
            pipeline = DarktablePipeline("darktable-cli", root / "outputs")

            def partial_runner(command, **kwargs):
                Path(command[-8]).write_bytes(b"partial")
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
