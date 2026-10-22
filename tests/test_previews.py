import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from lumen.previews import ExifToolPreviewProvider, fingerprint_file, read_image_dimensions


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
            real_normalize = provider._normalize

            def normalize(candidate, output):
                if candidate == source:
                    raise OSError("Pillow has no HEIC decoder")
                return real_normalize(candidate, output)

            def fake_run(command, **kwargs):
                output = Path(command[-1])
                Image.new("RGB", (80, 60), (1, 2, 3)).save(output, format="JPEG")
                return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

            with patch.object(provider, "_normalize", side_effect=normalize), patch(
                "lumen.previews.shutil.which", side_effect=lambda name: "/usr/bin/sips" if name == "sips" else None
            ), patch("lumen.previews.subprocess.run", side_effect=fake_run):
                result = provider.create_preview(source, destination)

        self.assertEqual(destination, result)


if __name__ == "__main__":
    unittest.main()
