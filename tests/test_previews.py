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
from lumen.previews import ExifToolPreviewProvider, PreviewError, fingerprint_file, full_size_for, prune_thumbnails, read_image_dimensions, read_metadata, thumbnail_for


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

    def test_full_size_for_extracts_jpg_from_raw_first_and_caches_by_fingerprint(self):
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
                self.assertEqual(["-JpgFromRaw"], calls)
                with Image.open(target) as saved:
                    self.assertEqual((640, 427), saved.size)
                self.assertEqual(target, full_size_for(source, cache, "abc"))
                self.assertEqual(["-JpgFromRaw"], calls, "a cached full-size image never runs exiftool again")

                calls.clear()
                buffer = io.BytesIO()  # no embedded JPEG under either tag
                self.assertIsNone(full_size_for(source, cache, "def"))
                self.assertEqual(["-JpgFromRaw", "-PreviewImage"], calls)
                with self.assertRaises(OSError):
                    full_size_for(root / "missing.ARW", cache, "ghi")
                self.assertEqual(target, full_size_for(root / "missing.ARW", cache, "abc"), "a cached copy outlives an unplugged volume")
            with patch("lumen.previews.shutil.which", return_value=None):
                self.assertIsNone(full_size_for(source, cache, "jkl"))

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


if __name__ == "__main__":
    unittest.main()
