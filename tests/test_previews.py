import io
import json
import sys
import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from lumen.config import Settings
from lumen.library import Library
from lumen.previews import (NIKON_HE_WARNING, ExifToolPreviewProvider, PreviewError, fingerprint_file, full_size_for, prune_thumbnails, read_exif_summary,
                            read_image_dimensions, read_metadata, thumbnail_for)


def write_fake_darktable(directory: Path, sharp: tuple[str, ...] = ()) -> str:
    """Executable stand-in for darktable-cli: renders the source at 2048 px (noise for names in `sharp`),
    records its arguments beside itself, and exits 3 for sources whose name contains FAIL."""
    script = directory / "darktable-cli"
    script.write_text(f'''#!{sys.executable}
import json, sys
from pathlib import Path
from PIL import Image
source, output = sys.argv[1], sys.argv[2]
with open({str(directory / "calls.jsonl")!r}, "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if "FAIL" in Path(source).name:
    sys.exit(3)
if any(name in Path(source).name for name in {sharp!r}):
    image = Image.effect_noise((2048, 1536), 64).convert("RGB")
else:
    with Image.open(source) as opened:
        image = opened.convert("RGB").resize((2048, 1536))
image.save(output, format="JPEG", quality=95)
''')
    script.chmod(0o755)
    return str(script)


class PreviewTests(unittest.TestCase):
    def test_source_dimensions_are_full_resolution_and_orientation_aware(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "oriented.jpg"
            exif = Image.Exif()
            exif[274] = 6
            Image.new("RGB", (4000, 3000)).save(source, exif=exif)

            self.assertEqual((3000, 4000), read_image_dimensions(source))

    def test_raw_dimensions_use_numeric_exiftool_metadata_and_orientation(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.ARW"
            source.write_bytes(b"raw")
            response = subprocess.CompletedProcess(
                ["exiftool"], 0,
                '[{"ImageWidth":6000,"ImageHeight":4000,"Orientation":6}]', "",
            )
            with patch("lumen.previews.shutil.which", return_value="/usr/bin/exiftool"), patch(
                "lumen.previews.subprocess.run", return_value=response
            ):
                self.assertEqual((4000, 6000), read_image_dimensions(source))

    def test_metadata_reads_gps_as_signed_decimals_and_needs_both_coordinates(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.ARW"
            source.write_bytes(b"raw")
            response = subprocess.CompletedProcess(["exiftool"], 0, '[{"Model":"ILCE-7CM2","GPSLatitude":-24.1234,"GPSLongitude":31.5678,"GPSAltitude":312.4}]', "")
            with patch("lumen.previews.shutil.which", return_value="/usr/bin/exiftool"), patch("lumen.previews.subprocess.run", return_value=response) as run:
                metadata = read_metadata(source)
            self.assertEqual((-24.1234, 31.5678, 312.4), (metadata["latitude"], metadata["longitude"], metadata["altitude"]))
            for flag in ("-GPSLatitude", "-GPSLongitude", "-GPSAltitude"):
                self.assertIn(flag, run.call_args.args[0])
            response.stdout = '[{"Model":"ILCE-7CM2","GPSLatitude":-24.1234}]'
            with patch("lumen.previews.shutil.which", return_value="/usr/bin/exiftool"), patch("lumen.previews.subprocess.run", return_value=response):
                metadata = read_metadata(source)
            self.assertEqual((None, None, None), (metadata["latitude"], metadata["longitude"], metadata["altitude"]), "half a fix is no fix")

    def test_exif_summary_labels_a_fixed_list_and_never_passes_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.ARW"
            source.write_bytes(b"raw")
            response = subprocess.CompletedProcess(["exiftool"], 0, json.dumps([{
                "SourceFile": str(source), "Directory": directory, "Model": "ILCE-7CM2", "LensModel": "FE 24-105mm F4 G OSS",
                "FNumber": 8.0, "ExposureTime": "1/250", "ISO": 100, "FocalLength": "35.0 mm"}]), "")
            with patch("lumen.previews.shutil.which", return_value="/usr/bin/exiftool"), patch("lumen.previews.subprocess.run", return_value=response) as run:
                fields = read_exif_summary(source)
            self.assertEqual([("Camera", "ILCE-7CM2"), ("Lens", "FE 24-105mm F4 G OSS"), ("Focal length", "35.0 mm"),
                              ("Aperture", "f/8.0"), ("Shutter speed", "1/250 s"), ("ISO", "100")], fields)
            self.assertNotIn(directory, json.dumps(fields))
            self.assertNotIn("-a", run.call_args.args[0])
            with patch("lumen.previews.shutil.which", return_value=None), self.assertRaises(PreviewError):
                read_exif_summary(source)

    def test_metadata_reads_exposure_seconds_numerically(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.ARW"
            source.write_bytes(b"raw")
            response = subprocess.CompletedProcess(
                ["exiftool"], 0,
                '[{"DateTimeOriginal":"2026:10:21 22:15:00","Model":"ILCE-7CM2","SerialNumber":"1","ExposureTime":30}]', "",
            )
            with patch("lumen.previews.shutil.which", return_value="/usr/bin/exiftool"), patch(
                "lumen.previews.subprocess.run", return_value=response
            ) as run:
                metadata = read_metadata(source)
            self.assertEqual(30.0, metadata["exposure_seconds"])
            self.assertEqual("2026-10-21T22:15:00", metadata["captured_at"])
            self.assertIn("-n", run.call_args.args[0])
            self.assertIn("-ExposureTime", run.call_args.args[0])
            response.stdout = '[{"Model":"ILCE-7CM2","ExposureTime":"bad"}]'
            with patch("lumen.previews.shutil.which", return_value="/usr/bin/exiftool"), patch(
                "lumen.previews.subprocess.run", return_value=response
            ):
                self.assertIsNone(read_metadata(source)["exposure_seconds"])
            with patch("lumen.previews.shutil.which", return_value=None):
                self.assertIsNone(read_metadata(source)["exposure_seconds"])

    def test_metadata_flags_sony_reduced_size_lossless_raw_that_darktable_cannot_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "DSC00712.ARW"
            source.write_bytes(b"raw")
            response = subprocess.CompletedProcess(["exiftool"], 0, '[{"Model":"ILCE-7CM2","PhotometricInterpretation":6}]', "")
            with patch("lumen.previews.shutil.which", return_value="/usr/bin/exiftool"), patch(
                "lumen.previews.subprocess.run", return_value=response
            ) as run:
                warning = read_metadata(source)["raw_warning"]
            self.assertIn("-SubIFD:PhotometricInterpretation", run.call_args.args[0])
            self.assertIn("lossless", warning)
            self.assertIn("darktable", warning)
            for stdout in ('[{"Model":"ILCE-7CM2","PhotometricInterpretation":32803}]', '[{"Model":"ILCE-7CM2"}]'):
                response.stdout = stdout
                with patch("lumen.previews.shutil.which", return_value="/usr/bin/exiftool"), patch(
                    "lumen.previews.subprocess.run", return_value=response
                ):
                    self.assertIsNone(read_metadata(source)["raw_warning"])
            response.stdout = '[{"PhotometricInterpretation":6}]'
            with patch("lumen.previews.shutil.which", return_value="/usr/bin/exiftool"), patch(
                "lumen.previews.subprocess.run", return_value=response
            ):
                self.assertIsNone(read_metadata(Path(directory) / "photo.jpg")["raw_warning"], "only raw files are flagged")

    def _metadata(self, name: str, item: dict) -> dict:
        response = subprocess.CompletedProcess(["exiftool"], 0, json.dumps([item]), "")
        with patch("lumen.previews.shutil.which", return_value="/usr/bin/exiftool"), patch(
            "lumen.previews.subprocess.run", return_value=response
        ) as run:
            metadata = read_metadata(Path("/nonexistent") / name)
        metadata["args"] = run.call_args.args[0]
        return metadata

    def test_serial_falls_back_to_camera_serial_number(self):
        metadata = self._metadata("L1000001.DNG", {"Model": "LEICA M (Typ 240)", "CameraSerialNumber": 4851623})
        self.assertEqual("LEICA M (Typ 240):4851623", metadata["camera_id"])
        self.assertIn("-CameraSerialNumber", metadata["args"])

    def test_a_masked_serial_is_unknown(self):
        metadata = self._metadata("L1000001.DNG", {"Model": "M9 Digital Camera", "SerialNumber": "*******"})
        self.assertEqual("M9 Digital Camera:unknown", metadata["camera_id"])
        metadata = self._metadata("L1000001.DNG", {"Model": "M9 Digital Camera", "SerialNumber": "*******",
                                                   "CameraSerialNumber": "*******"})
        self.assertEqual("M9 Digital Camera:unknown", metadata["camera_id"])

    def test_a_zero_zero_fix_is_no_fix(self):
        metadata = self._metadata("DSC_0001.NEF", {"Model": "NIKON Z 8", "GPSLatitude": 0, "GPSLongitude": 0, "GPSAltitude": 0})
        self.assertEqual((None, None, None), (metadata["latitude"], metadata["longitude"], metadata["altitude"]))

    def test_an_equator_fix_is_kept(self):
        metadata = self._metadata("DSC_0001.NEF", {"Model": "NIKON Z 8", "GPSLatitude": 0, "GPSLongitude": 36.8, "GPSAltitude": 1795})
        self.assertEqual((0, 36.8, 1795), (metadata["latitude"], metadata["longitude"], metadata["altitude"]))

    def test_high_efficiency_nef_warns(self):
        for compression in (14, 13):
            metadata = self._metadata("DSC_0001.NEF", {"Model": "NIKON Z 8", "NEFCompression": compression})
            self.assertEqual(NIKON_HE_WARNING, metadata["raw_warning"])
        self.assertIn("-NEFCompression", metadata["args"])
        self.assertIsNone(self._metadata("DSC_0001.NEF", {"Model": "NIKON Z 8", "NEFCompression": 1})["raw_warning"])
        sony = self._metadata("DSC00712.ARW", {"Model": "ILCE-7CM2", "PhotometricInterpretation": 6})["raw_warning"]
        self.assertTrue(sony.startswith("Sony reduced-size lossless raw"))

    def test_unknown_source_dimensions_fail_clearly(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "unknown.ARW"
            source.write_bytes(b"raw")
            with patch("lumen.previews.shutil.which", return_value=None):
                with self.assertRaisesRegex(ValueError, "dimensions"):
                    read_image_dimensions(source)

    def test_jpeg_preview_is_bounded_and_source_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jpg"
            destination = root / "cache" / "preview.jpg"
            Image.new("RGB", (2400, 1200), (20, 30, 40)).save(source, quality=95)
            before = source.read_bytes()

            result = ExifToolPreviewProvider(max_size=800).create_preview(source, destination)

            self.assertEqual(destination, result)
            with Image.open(result) as preview:
                self.assertEqual((800, 400), preview.size)
            self.assertEqual(before, source.read_bytes())

    def test_fingerprint_changes_with_content_not_mtime(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a.ARW"
            path.write_bytes(b"raw-one")
            first = fingerprint_file(path)
            path.touch()
            self.assertEqual(first, fingerprint_file(path))
            path.write_bytes(b"raw-two")
            self.assertNotEqual(first, fingerprint_file(path))

    def test_heic_falls_back_to_sips_when_pillow_cannot_decode_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.heic"
            destination = root / "cache" / "preview.jpg"
            source.write_bytes(b"heic")
            provider = ExifToolPreviewProvider()
            real_normalize = provider.normalize

            def normalize(candidate, output):
                if candidate == source:
                    raise OSError("Pillow has no HEIC decoder")
                return real_normalize(candidate, output)

            def fake_run(command, **kwargs):
                output = Path(command[-1])
                Image.new("RGB", (80, 60), (1, 2, 3)).save(output, format="JPEG")
                return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

            with patch.object(provider, "normalize", side_effect=normalize), patch(
                "lumen.previews.shutil.which", side_effect=lambda name: "/usr/bin/sips" if name == "sips" else None
            ), patch("lumen.previews.subprocess.run", side_effect=fake_run):
                result = provider.create_preview(source, destination)

        self.assertEqual(destination, result)

    def test_refined_preview_uses_darktable_and_renames_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "DSC0001.jpg"
            Image.new("RGB", (320, 240), (90, 90, 90)).save(source)
            before = source.read_bytes()
            darktable = write_fake_darktable(root)
            destination = root / "previews" / "abc-2048.jpg"
            destination.parent.mkdir()

            result = ExifToolPreviewProvider().create_refined_preview(source, destination, darktable=darktable)

            self.assertEqual(destination, result)
            with Image.open(result) as preview:
                self.assertEqual((2048, 1536), preview.size)
            self.assertEqual([destination], list(destination.parent.iterdir()), "no temporary render or .tmp remains")
            self.assertEqual(before, source.read_bytes())
            arguments = json.loads((root / "calls.jsonl").read_text().splitlines()[0])
            self.assertEqual(str(source), arguments[0])
            self.assertEqual(destination.parent, Path(arguments[1]).parent.parent, "renders into a temporary beside the destination")
            self.assertEqual(["--width", "2048", "--height", "2048", "--core", "--conf",
                              "plugins/lighttable/export/metadata_flags=5"], arguments[2:])

            failing = root / "FAIL.jpg"
            failing.write_bytes(before)
            with self.assertRaises(PreviewError) as raised:
                ExifToolPreviewProvider().create_refined_preview(failing, root / "previews" / "def-2048.jpg", darktable=darktable)
            self.assertNotIn(str(root), str(raised.exception))
            self.assertEqual([destination], list(destination.parent.iterdir()))

            def silent(command, **kwargs):  # exits 0 but writes nothing
                return subprocess.CompletedProcess(command, 0, b"", b"")

            def slow(command, **kwargs):
                raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))

            for runner in (silent, slow):
                with self.subTest(runner=runner.__name__), self.assertRaises(PreviewError):
                    ExifToolPreviewProvider().create_refined_preview(source, root / "previews" / "ghi-2048.jpg",
                                                                     darktable=darktable, runner=runner)
            self.assertEqual([destination], list(destination.parent.iterdir()))

    def test_prune_thumbnails_keeps_only_live_thumbs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "thumbs"
            previews = [root / "a.jpg", root / "b.jpg"]
            for preview in previews:
                Image.new("RGB", (40, 30), "red").save(preview)
            live = [thumbnail_for(preview, cache) for preview in previews]
            (cache / "a-1.jpg").write_bytes(b"stale mtime")
            (cache / "gone-5.jpg").write_bytes(b"deleted preview")

            self.assertEqual(2, prune_thumbnails(cache, previews))
            self.assertEqual(sorted(live), sorted(cache.iterdir()))
            self.assertEqual(0, prune_thumbnails(root / "missing", previews))

    def test_thumbnail_for_applies_the_selected_crop_and_prune_keeps_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache, preview = root / "thumbs", root / "a.jpg"
            Image.new("RGB", (800, 600), "red").save(preview)
            whole = thumbnail_for(preview, cache)
            cropped = thumbnail_for(preview, cache, (0.25, 0.0, 0.5, 1.0))
            self.assertNotEqual(whole, cropped)
            with Image.open(whole) as image:
                self.assertEqual((480, 360), image.size)
            with Image.open(cropped) as image:
                self.assertEqual((320, 480), image.size)
            self.assertEqual(0, prune_thumbnails(cache, [preview]))

    def test_full_size_for_extracts_the_largest_embedded_jpeg_and_caches_by_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "DSC00001.ARW"
            source.write_bytes(b"raw")
            cache = root / "full"
            buffer = io.BytesIO()
            Image.new("RGB", (640, 427), "blue").save(buffer, "JPEG")
            calls = []

            def run(command, **kwargs):
                calls.append(command[2])
                return subprocess.CompletedProcess(command, 0, buffer.getvalue() if "-JpgFromRaw" in command else b"", b"")

            with patch("lumen.previews.shutil.which", return_value="exiftool"), \
                    patch("lumen.previews.subprocess.run", side_effect=run):
                target = full_size_for(source, cache, "abc")
                self.assertEqual(cache / "abc.jpg", target)
                self.assertEqual(["-JpgFromRaw", "-OtherImage", "-PreviewImage"], calls)
                with Image.open(target) as saved:
                    self.assertEqual((640, 427), saved.size)
                self.assertEqual(target, full_size_for(source, cache, "abc"))
                self.assertEqual(3, len(calls), "a cached full-size image never runs exiftool again")

                calls.clear()
                buffer = io.BytesIO()  # no embedded JPEG under either tag
                self.assertIsNone(full_size_for(source, cache, "def"))
                self.assertEqual(["-JpgFromRaw", "-OtherImage", "-PreviewImage"], calls)
                with self.assertRaises(OSError):
                    full_size_for(root / "missing.ARW", cache, "ghi")
                self.assertEqual(target, full_size_for(root / "missing.ARW", cache, "abc"), "a cached copy outlives an unplugged volume")
            with patch("lumen.previews.shutil.which", return_value=None):
                self.assertIsNone(full_size_for(source, cache, "jkl"))

    def test_full_size_for_a_camera_jpeg_is_the_file_itself_cached_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "DSC00001.JPG"
            Image.new("RGB", (640, 427), "blue").save(source, "JPEG", quality=95)
            cache = root / "full"
            with patch("lumen.previews.subprocess.run", side_effect=AssertionError("a camera JPEG needs no exiftool")):
                target = full_size_for(source, cache, "abc")
                self.assertEqual(cache / "abc.jpg", target)
                self.assertEqual(source.read_bytes(), target.read_bytes(), "the camera's own pixels, not a re-encode")
                self.assertEqual(target, full_size_for(root / "missing.JPG", cache, "abc"), "a cached copy outlives an unplugged volume")

    def test_prune_removes_full_size_images_no_photo_carries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "photo.jpg"
            Image.new("RGB", (40, 30), "red").save(source)
            settings = Settings(library_dir=root / "library")
            with Library(settings) as library:
                library.import_path(source)
                fingerprint = library.catalog.list_photos()[0].fingerprint
                full = settings.cache_dir / "full"
                full.mkdir(parents=True)
                (full / f"{fingerprint}.jpg").write_bytes(b"live")
                (full / "gone.jpg").write_bytes(b"stale")
                library._prune_thumbnails()
            self.assertEqual([full / f"{fingerprint}.jpg"], list(full.iterdir()))


def jpeg(size: tuple[int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, "blue").save(buffer, "JPEG")
    return buffer.getvalue()


class EmbeddedPreviewTests(unittest.TestCase):
    """exiftool answers per tag from `tags` ({tag: (width, height)}); render_raw writes a 3000x2000 JPEG and records its long edge."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.helper = self.root / "raw_developer"
        self.helper.write_bytes(b"")
        self.renders, self.commands = [], []
        self.tags, self.image_size, self.tools = {}, "", {"exiftool"}
        patchers = [patch("lumen.previews.render_raw", side_effect=self.render),
                    patch("lumen.previews.subprocess.run", side_effect=self.exiftool),
                    patch("lumen.previews.shutil.which", side_effect=lambda name: f"/usr/bin/{name}" if name in self.tools else None),
                    patch("lumen.previews.find_tool", return_value=None)]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def render(self, source, destination, executable, *, long_edge=0, image_format="tiff16", **kwargs):
        self.renders.append(long_edge)
        Image.new("RGB", (3000, 2000), "red").save(destination, "JPEG")

    def exiftool(self, command, **kwargs):
        self.commands.append(command)
        if command[0].endswith("sips"):
            Image.new("RGB", (300, 200), "green").save(command[-1], "JPEG")
            return subprocess.CompletedProcess(command, 0, b"", b"")
        if "-ImageSize" in command:
            return subprocess.CompletedProcess(command, 0, self.image_size.encode(), b"")
        size = self.tags.get(command[2][1:])
        return subprocess.CompletedProcess(command, 0, jpeg(size) if size else b"", b"")

    def raw(self, name: str) -> Path:
        source = self.root / name
        source.write_bytes(b"raw")
        return source

    def preview(self, name: str, raw_developer=None) -> tuple[int, int]:
        destination = self.root / "previews" / "p.jpg"
        destination.unlink(missing_ok=True)
        ExifToolPreviewProvider(raw_developer=raw_developer).create_preview(self.raw(name), destination)
        with Image.open(destination) as image:
            return image.size

    def test_preview_takes_the_largest_embedded_jpeg(self):
        self.tags = {"PreviewImage": (640, 427), "JpgFromRaw": (4000, 100)}
        self.assertEqual((2048, 51), self.preview("L1000001.DNG", self.helper))
        self.assertEqual([], self.renders)

    def test_preview_renders_a_raw_whose_embedded_jpeg_is_a_thumbnail(self):
        self.tags = {"PreviewImage": (160, 120)}
        self.assertEqual((2048, 1365), self.preview("L1000001.DNG", self.helper))
        self.assertEqual([2048], self.renders)

    def test_preview_renders_a_raw_with_no_embedded_jpeg(self):
        self.assertEqual((2048, 1365), self.preview("L1000001.DNG", self.helper))
        self.assertEqual([2048], self.renders)

    def test_preview_without_the_helper_falls_back_as_before(self):
        self.tools.add("sips")
        self.tags = {"PreviewImage": (160, 120)}
        self.assertEqual((300, 200), self.preview("L1000001.DNG"))
        self.assertEqual([], self.renders)
        self.assertTrue(any(command[0].endswith("sips") for command in self.commands))
        self.assertEqual((300, 200), self.preview("L1000002.DNG", self.root / "never-bootstrapped"),
                         "a helper path with no file behind it is no helper")
        self.assertEqual([], self.renders)

    def test_preview_accepts_a_small_embedded_jpeg_as_the_last_resort(self):
        self.tags = {"PreviewImage": (160, 120)}
        self.assertEqual((160, 120), self.preview("L1000001.DNG"))

    def test_arw_preview_never_renders(self):
        self.tags = {"PreviewImage": (1616, 1080), "JpgFromRaw": (7008, 10)}
        self.assertEqual((1616, 1080), self.preview("DSC00001.ARW", self.helper))
        self.assertEqual([], self.renders)
        self.image_size = "7008 4672"
        full_size_for(self.raw("DSC00001.ARW"), self.root / "full", "abc", self.helper)
        self.assertEqual([], self.renders, "nor does its full-size view")

    def extractions(self) -> list[str]:
        return [command[2][1:] for command in self.commands if command[1] == "-b"]

    def test_arw_preview_uses_the_preview_image_in_one_call(self):
        self.tags = {"PreviewImage": (1616, 1080), "JpgFromRaw": (7008, 10)}
        self.assertEqual((1616, 1080), self.preview("DSC00001.ARW", self.helper))
        self.assertEqual(1, len(self.commands), "one exiftool run per Sony frame, as before")

    def test_preview_skips_a_thumbnail_sized_preview_image(self):
        self.tags = {"PreviewImage": (640, 427), "OtherImage": (1620, 1080), "JpgFromRaw": (4000, 100)}
        self.assertEqual((1620, 1080), self.preview("DSC_0001.NEF", self.helper))
        self.assertNotIn("JpgFromRaw", self.extractions())

    def test_full_size_reads_the_full_size_jpeg_in_one_call(self):
        self.tags, self.image_size = {"JpgFromRaw": (7008, 4672), "PreviewImage": (1616, 1080)}, "7040 4688"
        self.assertEqual((7008, 4672), self.full_size("DSC00001.ARW", self.helper))
        self.assertEqual(["JpgFromRaw"], self.extractions())
        self.assertEqual([], self.renders)

    def full_size(self, name: str, raw_developer) -> tuple[int, int]:
        target = full_size_for(self.raw(name), self.root / "full", name, raw_developer)
        with Image.open(target) as image:
            return image.size

    def test_full_size_renders_when_the_embedded_jpeg_is_far_smaller_than_the_raw(self):
        self.tags, self.image_size = {"JpgFromRaw": (2112, 1404)}, "9536 6336"
        self.assertEqual((3000, 2000), self.full_size("L1000001.DNG", self.helper))
        self.assertEqual([0], self.renders)
        self.assertEqual([self.root / "full" / "L1000001.DNG.jpg"], list((self.root / "full").iterdir()),
                         "no temporary render remains")

    def test_full_size_keeps_a_near_full_embedded_jpeg(self):
        self.tags, self.image_size = {"JpgFromRaw": (3504, 2336)}, "3584 2560"
        self.assertEqual((3504, 2336), self.full_size("DSC00001.ARW", self.helper))
        self.assertEqual([], self.renders)

    def test_full_size_without_the_helper_keeps_the_embedded_jpeg(self):
        self.tags, self.image_size = {"JpgFromRaw": (2112, 1404)}, "9536 6336"
        self.assertEqual((2112, 1404), self.full_size("L1000001.DNG", None))
        self.assertEqual([], self.renders)


if __name__ == "__main__":
    unittest.main()
