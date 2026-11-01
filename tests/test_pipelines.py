import subprocess
import io
import tempfile
import unittest
import json
from xml.etree import ElementTree
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageCms, JpegImagePlugin

from lumen.pipelines import DarktablePipeline, PipelineRegistry, SirilPipeline, validate_watermark
from lumen.rawdevelop import DevelopRender


def render_raw_stub(calls, *, size=(80, 60), xmp=None, icc=True):
    """Stand-in for the native raw developer: records the request and writes an sRGB TIFF carrying camera EXIF
    and a GPS IFD, as the real helper does (measured against a Sony ARW). Never runs Core Image."""
    def render(source, destination, executable, *, settings=None, long_edge=0, image_format="tiff16",
               histogram=False, timeout=None, runner=None):
        calls.append({"source": Path(source), "destination": Path(destination), "executable": Path(executable),
                      "settings": settings, "format": image_format, "long_edge": long_edge, "timeout": timeout})
        exif = Image.Exif()
        exif[271], exif[272] = "Sony", "ILCE-7CM2"
        exif[34665] = {36867: "2026:10:21 10:00:00", 34855: 640, 42036: "FE 70-200mm F4 G OSS II"}
        exif[34853] = {1: "S", 2: (25.0, 44.0, 12.0), 5: 0, 6: 1180.0}
        Image.new("RGB", size, "red").save(
            destination, format="TIFF", exif=exif.tobytes(), **({"tiffinfo": {700: xmp}} if xmp else {}),
            **({"icc_profile": ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()} if icc else {}))
        return DevelopRender(path=Path(destination), width=size[0], height=size[1],
                             histogram={}, defaults={}, lens_correction_supported=False, neutral_temperature=5500.0, neutral_tint=0.0)
    return render


def os_probe_and_exiftool(test, command, **kwargs):
    """Runner for the raw-developer route: the OS probe and the MakerNote/IPTC transplant are the only
    subprocesses it may make."""
    if command[0].endswith("exiftool"):
        return subprocess.CompletedProcess(command, 0, "", "")
    test.assertTrue(command[0].endswith("sw_vers"), f"nothing but the OS probe may run: {command}")
    return subprocess.CompletedProcess(command, 0, "26.5.1\n", "")


class PipelineTests(unittest.TestCase):
    def raw_pipeline(self, root):
        """The helper itself is patched out; the file only has to exist, as bootstrap leaves it."""
        helper = root / "bin" / "raw_developer"
        helper.parent.mkdir(exist_ok=True)
        helper.touch()
        return DarktablePipeline("darktable-cli", root / "outputs", raw_developer=helper)

    def test_export_renders_a_raw_through_the_raw_developer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "DSC00712.ARW"
            source.write_bytes(b"raw")
            calls = []
            with patch("lumen.pipelines.render_raw", side_effect=render_raw_stub(calls)):
                result = self.raw_pipeline(root).export_jpeg(
                    source, runner=lambda command, **kwargs: os_probe_and_exiftool(self, command, **kwargs))
            self.assertTrue(result.success, result.message)
            self.assertEqual("coreimage", result.settings["decoder"])
            self.assertEqual("macOS 26.5.1 Core Image", result.settings["tool_version"])
            self.assertIsNone(result.settings["develop"])
            self.assertEqual("DSC00712-lumen.jpg", result.output.name)
            self.assertTrue(result.output.is_file())
            self.assertTrue(result.command[0].endswith("raw_developer"), result.command)
            self.assertEqual(1, len(calls))
            self.assertEqual(root / "bin" / "raw_developer", calls[0]["executable"])
            self.assertEqual(("tiff16", 0), (calls[0]["format"], calls[0]["long_edge"]))
            self.assertEqual(source.name, calls[0]["source"].name)
            self.assertNotEqual(source, calls[0]["source"], "the staged copy renders, never the original")
            self.assertEqual(600, calls[0]["timeout"], "a full-size export render keeps the longer budget")

    def test_raw_export_carries_camera_metadata_and_keeps_gps_only_for_the_full_preset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "DSC00712.ARW"
            source.write_bytes(b"raw")
            pipeline = self.raw_pipeline(root)
            for preset, keeps_gps in (("full", True), ("share", False), ("web", False)):
                with self.subTest(preset=preset):
                    with patch("lumen.pipelines.render_raw", side_effect=render_raw_stub([], size=(2400, 1600))):
                        result = pipeline.export_jpeg(
                            source, preset=preset, develop={"exposure_ev": 0.5},
                            runner=lambda command, **kwargs: os_probe_and_exiftool(self, command, **kwargs))
                    self.assertTrue(result.success, result.message)
                    with Image.open(result.output) as image:
                        exif = image.getexif()
                        capture = exif.get_ifd(34665)
                        self.assertEqual(("Sony", "ILCE-7CM2"), (exif[271], exif[272]))
                        self.assertEqual("2026:10:21 10:00:00", capture[36867])
                        self.assertEqual(640, capture[34855])
                        self.assertEqual("FE 70-200mm F4 G OSS II", capture[42036])
                        self.assertEqual(keeps_gps, bool(dict(exif.get_ifd(34853))))
                        self.assertIn("sRGB", ImageCms.getProfileDescription(
                            ImageCms.ImageCmsProfile(io.BytesIO(image.info["icc_profile"]))))

    def test_raw_export_refuses_a_render_without_an_icc_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "DSC00712.ARW"
            source.write_bytes(b"raw")
            with patch("lumen.pipelines.render_raw", side_effect=render_raw_stub([], icc=False)):
                result = self.raw_pipeline(root).export_jpeg(
                    source, runner=lambda command, **kwargs: os_probe_and_exiftool(self, command, **kwargs))
            self.assertFalse(result.success)
            self.assertIn("ICC profile", result.message)
            self.assertIn("coreimage", result.message)
            self.assertEqual([], list((root / "outputs" / "jpeg").glob("*.jpg")))

    def test_export_refuses_a_decoder_provenance_cannot_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "DSC00712.ARW"
            source.write_bytes(b"raw")
            with patch("lumen.pipelines.DECODERS", frozenset({"darktable"})), \
                    patch("lumen.pipelines.render_raw", side_effect=AssertionError("nothing may render")), \
                    self.assertRaises(ValueError) as refusal:
                self.raw_pipeline(root).export_jpeg(source, runner=lambda *args, **kwargs: self.fail("nothing may run"))
            self.assertIn("coreimage", str(refusal.exception))

    def test_export_passes_the_stored_develop_settings_to_the_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "DSC00712.ARW"
            source.write_bytes(b"raw")
            develop = {"exposure_ev": 0.85, "contrast": 0.55, "curve": [[0, 0.06], [0.5, 0.5], [1, 0.96]]}
            calls = []
            with patch("lumen.pipelines.render_raw", side_effect=render_raw_stub(calls)):
                result = self.raw_pipeline(root).export_jpeg(
                    source, develop=develop, runner=lambda command, **kwargs: os_probe_and_exiftool(self, command, **kwargs))
            self.assertTrue(result.success, result.message)
            self.assertEqual(develop, calls[0]["settings"])
            self.assertEqual(develop, result.settings["develop"])
            with Image.open(result.output) as image:
                self.assertIn(b"<lumen:develop>", image.info["xmp"])
                self.assertIn(b"exposure_ev", image.info["xmp"])

    def test_export_publishes_a_camera_jpeg_without_a_renderer(self):
        """A camera JPEG is already finished: the camera's own pixels are the render, declared sRGB by EXIF
        with no ICC profile, as a Sony body writes them. Nothing but exiftool may run."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exif = Image.Exif()
            exif[271], exif[272] = "Sony", "ILCE-7CM2"
            exif[34665] = {36867: "2026:11:01 06:57:00", 40961: 1}
            exif[34853] = {1: "S", 2: (50.0, 36.0, 51.69), 3: "W", 4: (70.0, 0.0, 0.0)}
            for name in ("DSC04639.JPG", "DSC04640.jpeg"):
                with self.subTest(name=name):
                    source = root / name
                    Image.new("RGB", (80, 60), "red").save(source, format="JPEG", quality=95, exif=exif.tobytes())

                    def runner(command, **kwargs):
                        self.assertTrue(command[0].endswith("exiftool"), f"nothing but exiftool may run: {command}")
                        return subprocess.CompletedProcess(command, 0, "", "")

                    with patch("lumen.pipelines.render_raw", side_effect=AssertionError("a camera JPEG needs no renderer")):
                        result = DarktablePipeline("darktable-cli", root / "outputs").export_jpeg(source, runner=runner)
                    self.assertTrue(result.success, result.message)
                    self.assertEqual("camera", result.settings["decoder"])
                    self.assertEqual([], result.settings["command"])
                    self.assertEqual(f"Pillow {Image.__version__}", result.settings["tool_version"])
                    with Image.open(result.output) as saved:
                        self.assertEqual((80, 60), saved.size)
                        self.assertEqual("ILCE-7CM2", saved.getexif()[272])
                        self.assertIn(34853, saved.getexif(), "the camera's GPS survives the full preset")
            untagged = root / "untagged.jpg"  # no EXIF at all: sRGB by the JPEG convention every viewer applies
            Image.new("RGB", (80, 60), "red").save(untagged, format="JPEG")
            self.assertTrue(DarktablePipeline("darktable-cli", root / "outputs").export_jpeg(untagged, runner=runner).success)
            exif[34665] = {40961: 0xFFFF}  # a body set to AdobeRGB declares "uncalibrated"
            adobe = root / "adobe.jpg"
            Image.new("RGB", (80, 60), "red").save(adobe, format="JPEG", exif=exif.tobytes())
            refused = DarktablePipeline("darktable-cli", root / "outputs").export_jpeg(adobe, runner=runner)
            self.assertFalse(refused.success)
            self.assertIn("not sRGB", refused.message)

    def test_full_export_of_a_camera_jpeg_keeps_the_camera_quantization_and_subsampling(self):
        """A Sony body writes its JPEG at about quality 100 with 4:2:2 chroma; the full preset's 92 / 4:2:0 would throw
        that away. Only the full preset keeps the camera's tables: share and web are size budgets."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "DSC04639.JPG"
            exif = Image.Exif()
            exif[34665] = {40961: 1}
            Image.new("RGB", (80, 60), "red").save(source, format="JPEG", quality=97, subsampling=0, exif=exif.tobytes())
            runner = lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", "")
            pipeline = DarktablePipeline("darktable-cli", root / "outputs")
            full = pipeline.export_jpeg(source, runner=runner)
            share = pipeline.export_jpeg(source, preset="share", runner=runner)
            self.assertTrue(full.success and share.success, (full.message, share.message))
            with Image.open(source) as camera, Image.open(full.output) as kept, Image.open(share.output) as budgeted:
                self.assertEqual(camera.quantization, kept.quantization)
                self.assertEqual(JpegImagePlugin.get_sampling(camera), JpegImagePlugin.get_sampling(kept))
                self.assertNotEqual(camera.quantization, budgeted.quantization)

    def test_full_export_of_an_uncropped_camera_jpeg_is_lossless(self):
        """Full preset, no crop, no watermark: the camera's entropy-coded data is published as-is and every piece
        of metadata Lumen adds goes in through exiftool, so the pixels never pass through a decoder."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "DSC04639.JPG"
            exif = Image.Exif()
            exif[274] = 6  # the body's rotation tag stays, since nothing transposes the pixels
            exif[34665] = {40961: 1}
            Image.new("RGB", (80, 60), "red").save(source, format="JPEG", quality=97, exif=exif.tobytes())
            commands = []

            packets = []

            def runner(command, **kwargs):
                commands.append(command)
                packets.extend(Path(arg[6:]).read_text() for arg in command if arg.startswith("-XMP<="))
                return subprocess.CompletedProcess(command, 0, "", "")

            pipeline = DarktablePipeline("darktable-cli", root / "outputs")
            result = pipeline.export_jpeg(source, description="A caption", keywords=["sony"], rating=4, runner=runner)
            self.assertTrue(result.success, result.message)
            self.assertEqual(source.read_bytes(), result.output.read_bytes(), "the camera's bytes, before exiftool")
            self.assertEqual("lossless", result.settings["quality"])
            transplant = commands[-1]
            self.assertIn("-MakerNotes", transplant)
            self.assertIn("-EXIF:ImageDescription=A caption", transplant)
            self.assertIn("-XPKeywords=sony", transplant)
            self.assertIn("-IFD0:Rating=4", transplant)
            self.assertIn("-IPTC:Caption-Abstract=A caption", transplant)
            self.assertEqual(1, len(packets), transplant)
            self.assertIn("A caption", packets[0])
            self.assertTrue(packets[0].startswith("<x:xmpmeta"), "exiftool rejects a packet led by an XML declaration")
            for cropped in (dict(crop={"left": 0, "top": 0, "width": .5, "height": .5}), dict(preset="share"), dict(watermark="x")):
                with self.subTest(**cropped):
                    again = pipeline.export_jpeg(source, runner=runner, **cropped)
                    self.assertTrue(again.success, again.message)
                    self.assertNotEqual(source.read_bytes(), again.output.read_bytes())

    def test_export_renders_other_non_raws_through_darktable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def render(command, **kwargs):
                if command[1:] == ["--version"]:
                    return subprocess.CompletedProcess(command, 0, "darktable 5.2", "")
                if command[0].endswith("exiftool"):
                    return subprocess.CompletedProcess(command, 0, "", "")
                Image.new("RGB", (80, 60), "red").save(command[-8], format="TIFF",
                                                       icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
                return subprocess.CompletedProcess(command, 0, "", "")

            for name in ("DSC3.png", "DSC4.tif", "DSC5.tiff", "DSC6.heic"):
                with self.subTest(name=name):
                    source = root / name
                    source.write_bytes(b"not a raw")
                    with patch("lumen.pipelines.render_raw", side_effect=AssertionError("the raw developer cannot take a non-raw")):
                        result = self.raw_pipeline(root).export_jpeg(source, runner=render)
                    self.assertTrue(result.success, result.message)
                    self.assertEqual("darktable", result.settings["decoder"])
                    self.assertEqual("darktable 5.2", result.settings["tool_version"])

    def test_export_renders_through_darktable_when_a_profile_is_given(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "DSC00712.ARW"
            source.write_bytes(b"raw")
            profile = root / "look.xmp"
            profile.write_text("<x:xmpmeta/>")

            def render(command, **kwargs):
                if command[1:] == ["--version"]:
                    return subprocess.CompletedProcess(command, 0, "darktable 5.2", "")
                if command[0].endswith("exiftool"):
                    return subprocess.CompletedProcess(command, 0, "", "")
                self.assertIn(str(profile.resolve()), command)
                Image.new("RGB", (80, 60), "red").save(command[-8], format="TIFF",
                                                       icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("lumen.pipelines.render_raw", side_effect=AssertionError("a profile belongs to darktable")):
                result = self.raw_pipeline(root).export_jpeg(source, profile, runner=render)
            self.assertTrue(result.success, result.message)
            self.assertEqual("darktable", result.settings["decoder"])

    def test_export_refuses_when_a_profile_and_develop_settings_are_both_present(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "DSC00712.ARW"
            source.write_bytes(b"raw")
            profile = root / "look.xmp"
            profile.write_text("<x:xmpmeta/>")
            with patch("lumen.pipelines.render_raw", side_effect=AssertionError("nothing may render")):
                result = self.raw_pipeline(root).export_jpeg(
                    source, profile, develop={"exposure_ev": 0.5},
                    runner=lambda *args, **kwargs: self.fail("nothing may run"))
            self.assertFalse(result.success)
            self.assertIn("profile", result.message)
            self.assertIn("develop", result.message)
            self.assertEqual([], list((root / "outputs" / "jpeg").iterdir()))

    def test_export_refuses_with_an_actionable_message_when_the_raw_developer_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "DSC00712.ARW"
            source.write_bytes(b"raw")
            pipeline = DarktablePipeline("darktable-cli", root / "outputs")
            result = pipeline.export_jpeg(source, runner=lambda *args, **kwargs: self.fail("nothing may run"))
            self.assertFalse(result.success)
            self.assertIn("bootstrap", result.message)

    def test_the_untouched_companion_is_named_per_preset_and_carries_no_develop_block(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "DSC1.ARW"
            source.write_bytes(b"raw")
            pipeline = self.raw_pipeline(root)
            runner = lambda command, **kwargs: os_probe_and_exiftool(self, command, **kwargs)
            with patch("lumen.pipelines.render_raw", side_effect=render_raw_stub([])):
                developed = pipeline.export_jpeg(source, develop={"exposure_ev": 0.5}, runner=runner)
                companion = pipeline.export_jpeg(source, develop=None, stem_suffix="original", runner=runner)
                web = pipeline.export_jpeg(source, develop=None, stem_suffix="original", preset="web", runner=runner)
            for result in (developed, companion, web):
                self.assertTrue(result.success, result.message)
            self.assertEqual(["DSC1-lumen.jpg", "DSC1-lumen-original.jpg", "DSC1-lumen-web-original.jpg"],
                             [result.output.name for result in (developed, companion, web)])
            with Image.open(developed.output) as image:
                self.assertIn(b"<lumen:develop>", image.info["xmp"])
            for result in (companion, web):
                with Image.open(result.output) as image:
                    self.assertNotIn(b"<lumen:develop>", image.info["xmp"])
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
            source = root / "source.tif"
            source.write_bytes(b"raw")
            def render(command, **kwargs):
                if command[1:] == ["--version"]:
                    return subprocess.CompletedProcess(command, 0, "darktable 5.2", "")
                if command[0].endswith("exiftool"):
                    return subprocess.CompletedProcess(command, 0, "", "")
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

    def test_jpeg_export_embeds_alt_text_only_when_given(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "DSC00713.ARW"
            source.write_bytes(b"raw")
            render = lambda command, **kwargs: os_probe_and_exiftool(self, command, **kwargs)
            pipeline = self.raw_pipeline(root)
            with patch("lumen.pipelines.render_raw", side_effect=render_raw_stub([])):
                with_alt = pipeline.export_jpeg(source, runner=render, alt_text="Red square")
                without = pipeline.export_jpeg(source, runner=render)
            with Image.open(with_alt.output) as image:
                self.assertIn(b"<Iptc4xmpCore:AltTextAccessibility><rdf:Alt><rdf:li xml:lang=\"x-default\">Red square", image.info["xmp"])
            with Image.open(without.output) as image:
                self.assertNotIn(b"AltTextAccessibility", image.info["xmp"])

    def test_jpeg_export_carries_place_in_xmp_for_every_preset_without_gps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "DSC00713.ARW"
            source.write_bytes(b"raw")
            render = lambda command, **kwargs: os_probe_and_exiftool(self, command, **kwargs)
            pipeline = self.raw_pipeline(root)
            with patch("lumen.pipelines.render_raw", side_effect=render_raw_stub([])):
                full = pipeline.export_jpeg(source, runner=render, place="Madikwe Game Reserve", country="South Africa")
                share = pipeline.export_jpeg(source, runner=render, preset="share", place="Madikwe Game Reserve", country="South Africa")
            for result in (full, share):
                with Image.open(result.output) as image:
                    xmp = ElementTree.fromstring(image.info["xmp"])
                    gps = image.getexif().get_ifd(34853)
                self.assertEqual("Madikwe Game Reserve", xmp.find(".//{http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/}Location").text)
                self.assertEqual("South Africa", xmp.find(".//{http://ns.adobe.com/photoshop/1.0/}Country").text)
            self.assertEqual({}, dict(gps), "share keeps the place but still strips GPS")

    def test_jpeg_export_preserves_metadata_and_original_with_collision_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.tif"
            source.write_bytes(b"original RAW")
            adjacent = source.with_suffix(".jpg.xmp")
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
                    # The second export passes no caption or keywords, so only the prefix is common; the
                    # IPTC arguments have their own test.
                    self.assertEqual(["/usr/bin/exiftool", "-q", "-overwrite_original",
                                      "-TagsFromFile", str(work / "source" / "source.tif"), "-MakerNotes"], command[:6])
                    self.assertEqual(str(work / "finished.jpg"), command[-1])
                    self.assertTrue(Path(command[-1]).is_file())
                    return subprocess.CompletedProcess(command, 0, "", "")
                staged_source = Path(command[1])
                self.assertNotEqual(source, staged_source)
                self.assertTrue(staged_source.is_relative_to((root / "outputs" / "jpeg").resolve()))
                self.assertEqual(b"original RAW", staged_source.read_bytes())
                self.assertFalse(staged_source.with_suffix(".jpg.xmp").exists())
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

    def test_iptc_and_makernote_are_read_from_the_original_not_the_render(self):
        """Core Image's TIFF carries no MakerNote, so the transplant has to read the staged original, and
        Pillow cannot write IPTC IIM at all, so the caption and keywords ride along on the same call."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.tif"
            source.write_bytes(b"raw")
            pipeline = DarktablePipeline("darktable-cli", root / "outputs")
            commands = []

            def render(command, **kwargs):
                commands.append(command)
                if command[1:] == ["--version"]:
                    return subprocess.CompletedProcess(command, 0, "darktable 5.2", "")
                if command[0] == "/usr/bin/exiftool":
                    return subprocess.CompletedProcess(command, 0, "", "")
                Image.new("RGB", (8, 6), "red").save(  # No EXIF at all: the render knows nothing.
                    command[-8], format="TIFF",
                    icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("lumen.pipelines.find_tool", return_value="/usr/bin/exiftool"):
                result = pipeline.export_jpeg(source, description="A red bird \u2014 overcast",
                                              keywords=["bird", "sky"], runner=render)
            self.assertTrue(result.success, result.message)
            copy = next(command for command in commands if command[0] == "/usr/bin/exiftool")
            work = Path(copy[-1]).parent
            self.assertEqual([
                "/usr/bin/exiftool", "-q", "-overwrite_original",
                "-TagsFromFile", str(work / "source" / "source.tif"), "-MakerNotes",
                "-IPTC:CodedCharacterSet=UTF8", "-IPTC:Caption-Abstract=A red bird \u2014 overcast",
                "-IPTC:Keywords=bird", "-IPTC:Keywords=sky", str(work / "finished.jpg"),
            ], copy)

    def test_export_without_caption_or_keywords_writes_no_iptc_block(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.tif"
            source.write_bytes(b"raw")
            pipeline = DarktablePipeline("darktable-cli", root / "outputs")
            commands = []

            def render(command, **kwargs):
                commands.append(command)
                if command[1:] == ["--version"]:
                    return subprocess.CompletedProcess(command, 0, "darktable 5.2", "")
                if command[0] == "/usr/bin/exiftool":
                    return subprocess.CompletedProcess(command, 0, "", "")
                Image.new("RGB", (8, 6), "red").save(
                    command[-8], format="TIFF",
                    icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("lumen.pipelines.find_tool", return_value="/usr/bin/exiftool"):
                result = pipeline.export_jpeg(source, runner=render)
            self.assertTrue(result.success, result.message)
            copy = next(command for command in commands if command[0] == "/usr/bin/exiftool")
            self.assertEqual([], [argument for argument in copy if argument.startswith("-IPTC:")])

    def test_makernote_transplant_failure_or_missing_exiftool_fails_export(self):
        for exiftool, outcome in (("/usr/bin/exiftool", 1), (None, None)):
            with self.subTest(exiftool=exiftool), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "source.tif"
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
            source = root / "source.tif"
            source.write_bytes(b"raw")
            pipeline = DarktablePipeline("darktable-cli", root / "outputs")
            def fail(command, **kwargs):
                if command[1:] == ["--version"]:
                    return subprocess.CompletedProcess(command, 0, "darktable 5.2", "")
                if command[0].endswith("exiftool"):
                    return subprocess.CompletedProcess(command, 0, "", "")
                Path(command[-8]).write_bytes(b"partial")
                raise OSError("runner unavailable")
            result = pipeline.export_jpeg(source, runner=fail)
            self.assertFalse(result.success)
            self.assertIn("runner unavailable", result.message)
            self.assertEqual([], list((root / "outputs" / "jpeg").iterdir()))

    def test_jpeg_export_applies_normalized_crop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.tif"
            source.write_bytes(b"raw")
            def render(command, **kwargs):
                if command[1:] == ["--version"]:
                    return subprocess.CompletedProcess(command, 0, "darktable 5.2", "")
                if command[0].endswith("exiftool"):
                    return subprocess.CompletedProcess(command, 0, "", "")
                Image.new("RGB", (80, 60), "red").save(command[-8], format="TIFF", icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
                return subprocess.CompletedProcess(command, 0, "", "")
            result = DarktablePipeline("darktable-cli", root / "outputs").export_jpeg(source, crop={"left": .25, "top": 0, "width": .5, "height": 1}, runner=render)
            self.assertTrue(result.success, result.message)
            with Image.open(result.output) as image:
                self.assertEqual((40, 60), image.size)

    def test_jpeg_crop_uses_exact_full_resolution_pixels_and_keeps_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.tif"
            source.write_bytes(b"raw source")
            before = source.read_bytes()
            def render(command, **kwargs):
                if command[1:] == ["--version"]:
                    return subprocess.CompletedProcess(command, 0, "darktable 5.2", "")
                if command[0].endswith("exiftool"):
                    return subprocess.CompletedProcess(command, 0, "", "")
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
            if command[0].endswith("exiftool"):
                return subprocess.CompletedProcess(command, 0, "", "")
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
            source = root / "source.tif"
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
            source = root / "source.tif"
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
                pipeline.export_jpeg(source, preset="x", runner=self.preset_render((80, 60)))

    def test_a_typed_position_fills_a_render_without_gps_for_the_full_preset_only(self):
        def bare(command, **kwargs):
            completed = self.preset_render((80, 60))(command, **kwargs)
            if command[1:] != ["--version"] and not command[0].endswith("exiftool"):
                with Image.open(command[-8]) as image:
                    exif = image.getexif()
                    del exif[34853]
                    image.save(command[-8], format="TIFF", exif=exif.tobytes(), icc_profile=image.info["icc_profile"])
            return completed
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.tif"
            source.write_bytes(b"raw")
            pipeline = DarktablePipeline("darktable-cli", root / "outputs")
            full = pipeline.export_jpeg(source, gps=(-33.92, 18.42), runner=bare)
            camera = pipeline.export_jpeg(source, gps=(-33.92, 18.42), runner=self.preset_render((80, 60)))
            web = pipeline.export_jpeg(source, gps=(-33.92, 18.42), preset="web", runner=bare)
            for result in (full, camera, web):
                self.assertTrue(result.success, result.message)
            with Image.open(full.output) as image:
                gps = image.getexif().get_ifd(34853)
                self.assertEqual(("S", "E"), (gps[1], gps[3]))
                self.assertAlmostEqual(33.92, gps[2][0] + gps[2][1] / 60 + gps[2][2] / 3600, places=5)
                self.assertAlmostEqual(18.42, gps[4][0] + gps[4][1] / 60 + gps[4][2] / 3600, places=5)
            with Image.open(camera.output) as image:
                self.assertEqual("N", image.getexif().get_ifd(34853)[1], "the camera's own fix wins")
            with Image.open(web.output) as image:
                self.assertEqual({}, dict(image.getexif().get_ifd(34853)))

    def test_web_preset_resizes_to_1600_long_edge_at_quality_82_without_gps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.tif"
            source.write_bytes(b"raw")
            result = DarktablePipeline("darktable-cli", root / "outputs").export_jpeg(
                source, preset="web", runner=self.preset_render((4000, 2600)))
            self.assertTrue(result.success, result.message)
            self.assertEqual("source-lumen-web.jpg", result.output.name)
            self.assertEqual({"preset": "web", "quality": 82, "long_edge": 1600, "include_gps": False, "watermark": False},
                             {key: result.settings[key] for key in ("preset", "quality", "long_edge", "include_gps", "watermark")})
            with Image.open(result.output) as image:
                self.assertEqual((1600, 1040), image.size)
                self.assertEqual({}, dict(image.getexif().get_ifd(34853)))

    def test_watermark_marks_bottom_right_only_and_records_a_flag_not_the_text(self):
        def grey(image):
            image.paste((128, 128, 128), (0, 0, *image.size))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.tif"
            source.write_bytes(b"raw")
            result = DarktablePipeline("darktable-cli", root / "outputs").export_jpeg(
                source, preset="web", watermark="\u00a9 N. David", runner=self.preset_render((2000, 1200), paint=grey))
            self.assertTrue(result.success, result.message)
            self.assertTrue(result.settings["watermark"])
            with Image.open(result.output) as opened:
                image = opened.convert("L")
                self.assertEqual((1600, 960), image.size)
                inset = round(0.02 * 960)
                corner = image.crop((1600 - inset - 300, 960 - inset - 40, 1600 - inset, 960 - inset))
                low, high = corner.getextrema()
                self.assertGreater(high, 200)  # White fill.
                self.assertLess(low, 60)  # Black stroke.
                top_left = image.crop((0, 0, 400, 300)).getextrema()
                self.assertTrue(all(abs(value - 128) <= 3 for value in top_left), top_left)
                xmp = opened.info["xmp"]
                provenance = json.loads(ElementTree.fromstring(xmp).find(".//{https://lumen.local/ns/1.0/}export").text)
                self.assertIs(True, provenance["watermark"])
                self.assertNotIn(b"N. David", xmp if isinstance(xmp, bytes) else xmp.encode())

    def test_validate_watermark(self):
        self.assertIsNone(validate_watermark(None))
        self.assertIsNone(validate_watermark(""))
        self.assertEqual("\u00a9 N. David", validate_watermark("  \u00a9 N. David "))
        self.assertEqual("x" * 80, validate_watermark("x" * 80))
        for bad in ("  ", "x" * 81, "a\nb", 5, ["x"]):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "^watermark must be 1 to 80 printable characters$"):
                validate_watermark(bad)

    def test_share_filenames_use_share_suffix_and_collision_numbering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.tif"
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
            source = root / "source.tif"
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
                source = root / "source.tif"
                source.write_bytes(b"raw")
                def render(command, **kwargs):
                    if command[1:] == ["--version"]:
                        return subprocess.CompletedProcess(command, 0, "darktable 5.2", "")
                    if command[0].endswith("exiftool"):
                        return subprocess.CompletedProcess(command, 0, "", "")
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
                pipeline.export_jpeg(root / "source.tif")
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
