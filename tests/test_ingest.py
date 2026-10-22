import hashlib
import json
import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from lumen import ingest
from lumen.ingest import IngestVerifyError, classify_existing, copy_verified, ingest_item, plan_ingest, write_manifest


def snapshot_tree(root: Path) -> dict:
    return {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in sorted(root.rglob("*")) if path.is_file()}


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.card = self.root / "CARD"
        (self.card / "DCIM" / "100MSDCF").mkdir(parents=True)
        for index in (1, 2):
            path = self.card / "DCIM" / "100MSDCF" / f"DSC0000{index}.JPG"
            Image.new("RGB", (64, 48), (index * 80, 30, 30)).save(path)
            os.utime(path, (datetime(2026, 10, 20, 12).timestamp(),) * 2)
        (self.card / "notes.txt").write_text("not a photo")
        self.library = self.root / "library"
        self.library.mkdir()
        self.primary = self.root / "primary"
        self.mirror = self.root / "mirror"
        self.primary.mkdir()
        self.mirror.mkdir()
        self.no_exif = patch("lumen.ingest.read_metadata", return_value={"captured_at": None, "camera_id": "unknown"})
        self.no_exif.start()

    def tearDown(self):
        self.no_exif.stop()
        for path in self.root.rglob("*"):
            if not path.is_symlink():
                path.chmod(0o755)
        self.tmp.cleanup()

    def plan(self, *destinations):
        return plan_ingest(self.card, destinations or (self.primary, self.mirror),
                           library_dir=self.library, today=date(2026, 10, 22))

    def test_plan_rejects_library_nested_and_duplicate_destinations_before_copying(self):
        inside_card = self.card / "backup"
        inside_card.mkdir()
        cases = {
            "library": (self.card, (self.library / "copies",)),
            "inside card": (self.card, (inside_card,)),
            "card inside destination": (self.card, (self.root,)),
            "same as card": (self.card, (self.card,)),
            "duplicate": (self.card, (self.primary, self.root / "primary" / ".")),
            "missing card": (self.root / "missing", (self.primary,)),
            "missing destination": (self.card, (self.root / "unmounted",)),
            "no destination": (self.card, ()),
            "card in library": (self.library, (self.primary,)),
        }
        (self.library / "copies").mkdir()
        for name, (card, destinations) in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                plan_ingest(card, destinations, library_dir=self.library, today=date(2026, 10, 22))
        self.assertEqual([], list(self.primary.iterdir()))
        self.assertEqual([], list((self.library / "copies").iterdir()))

    def test_plan_uses_exif_date_then_mtime_then_today(self):
        first, second = sorted((self.card / "DCIM" / "100MSDCF").iterdir())
        third = self.card / "DSC00003.ARW"
        third.write_bytes(b"raw bytes")
        os.utime(third, (0, 0))
        hidden = self.card / "._DSC00001.JPG"
        hidden.write_bytes(b"apple double")
        link = self.card / "link.JPG"
        link.symlink_to(first)

        def metadata(path):
            return {"captured_at": "2025-01-02T03:04:05" if path == first else None, "camera_id": "unknown"}

        with patch("lumen.ingest.read_metadata", side_effect=metadata):
            items = self.plan()

        by_name = {item.source.name: item for item in items}
        self.assertEqual(["DSC00001.JPG", "DSC00002.JPG", "DSC00003.ARW"], sorted(by_name))
        self.assertEqual("2025-01-02", by_name["DSC00001.JPG"].captured_date)
        self.assertEqual("2026-10-20", by_name["DSC00002.JPG"].captured_date)
        self.assertEqual("2026-10-22", by_name["DSC00003.ARW"].captured_date)
        item = by_name["DSC00001.JPG"]
        self.assertEqual("DCIM/100MSDCF/DSC00001.JPG", item.relative)
        self.assertEqual(first.stat().st_size, item.size)
        self.assertEqual((self.primary / "2025-01-02" / "DCIM/100MSDCF/DSC00001.JPG",
                          self.mirror / "2025-01-02" / "DCIM/100MSDCF/DSC00001.JPG"), item.destinations)
        self.assertEqual(["notes.txt"], ingest.unplanned_files(self.card))

    def test_copy_verified_writes_partial_fsyncs_and_renames_into_place(self):
        source = next((self.card / "DCIM" / "100MSDCF").iterdir())
        destination = self.primary / "2026-10-20" / "DCIM" / source.name
        seen = []
        real_fsync = os.fsync

        def record_fsync(descriptor):
            seen.append(sorted(path.name for path in destination.parent.iterdir()))
            return real_fsync(descriptor)

        with patch("lumen.ingest.os.fsync", side_effect=record_fsync) as fsync:
            digest = copy_verified(source, destination, chunk_size=7)

        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)
        self.assertEqual(source.read_bytes(), destination.read_bytes())
        self.assertGreaterEqual(fsync.call_count, 2, "the file and its directory are both fsynced")
        self.assertEqual([f".{source.name}.lumen-partial"], seen[0], "bytes land in the partial before the rename")
        self.assertEqual([source.name], sorted(path.name for path in destination.parent.iterdir()))

    def test_copy_verified_retries_once_then_fails_without_final_file(self):
        source = next((self.card / "DCIM" / "100MSDCF").iterdir())
        destination = self.primary / "day" / source.name

        def flaky(bad_calls):
            calls = []

            def factory():
                calls.append(1)
                real = hashlib.sha256()
                if len(calls) in bad_calls:
                    class Wrong:
                        update = real.update

                        @staticmethod
                        def hexdigest():
                            return "0" * 64
                    return Wrong()
                return real
            return factory, calls

        factory, calls = flaky({2})  # the first re-read disagrees; the retry agrees
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), copy_verified(source, destination, hasher=factory))
        self.assertEqual(4, len(calls))
        self.assertEqual([source.name], [path.name for path in destination.parent.iterdir()])
        destination.unlink()

        factory, calls = flaky({2, 4})
        with self.assertRaises(IngestVerifyError):
            copy_verified(source, destination, hasher=factory)
        self.assertEqual(4, len(calls), "exactly one retry")
        self.assertEqual([], list(destination.parent.iterdir()), "no final file and no partial remain")

    def test_leftover_partial_is_removed_and_never_counted_as_present(self):
        source = next((self.card / "DCIM" / "100MSDCF").iterdir())
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        destination = self.primary / "day" / source.name
        destination.parent.mkdir()
        partial = destination.with_name(f".{source.name}.lumen-partial")
        partial.write_bytes(source.read_bytes())  # even a complete-looking partial is not a verified copy

        self.assertIsNone(classify_existing(destination, source_hash))
        self.assertFalse(partial.exists())
        destination.write_bytes(b"different")
        self.assertIsNone(classify_existing(destination, source_hash))
        destination.write_bytes(source.read_bytes())
        partial.write_bytes(b"half")
        self.assertEqual("already_present", classify_existing(destination, source_hash))
        self.assertFalse(partial.exists())

    def test_rerun_reports_already_present_and_copies_only_missing_files(self):
        roots = (self.primary, self.mirror)
        first = [ingest_item(item, roots) for item in self.plan()]
        self.assertEqual([{str(self.primary): "copied", str(self.mirror): "copied"}] * 2, [entry["outcomes"] for entry in first])
        removed = self.mirror / "2026-10-20" / first[1]["relative"]
        removed.unlink()
        clash = self.primary / "2026-10-20" / first[0]["relative"]
        before = clash.read_bytes()

        with patch("lumen.ingest.copy_verified", wraps=ingest.copy_verified) as copies:
            second = [ingest_item(item, roots) for item in self.plan()]

        self.assertEqual([removed], [call.args[1] for call in copies.call_args_list])
        self.assertEqual({str(self.primary): "already_present", str(self.mirror): "already_present"}, second[0]["outcomes"])
        self.assertEqual({str(self.primary): "already_present", str(self.mirror): "copied"}, second[1]["outcomes"])
        self.assertEqual(first[1]["sha256"], second[1]["sha256"])
        self.assertEqual({"copied": 1, "verified": 2, "already_present": 1, "failed": 0, "files": 2, "skipped": 0,
                          "card_verified_everywhere": True}, ingest.summarize(second, roots, planned=2))
        self.assertEqual(1, ingest.summarize(second, roots, planned=2, skipped=1)["skipped"])
        self.assertFalse(ingest.summarize(second, roots, planned=2, skipped=1)["card_verified_everywhere"])
        self.assertFalse(ingest.summarize(second[:1], roots, planned=2)["card_verified_everywhere"])

        clash.write_bytes(b"another camera's DSC00001")
        third = ingest_item(self.plan()[0], roots)
        self.assertEqual("failed", third["outcomes"][str(self.primary)])
        self.assertIn("different file", third["error"])
        self.assertEqual(b"another camera's DSC00001", clash.read_bytes(), "an existing destination file is never overwritten")
        self.assertNotEqual(before, clash.read_bytes())
        self.assertFalse(ingest.summarize([third, second[1]], roots, planned=2)["card_verified_everywhere"])

        manifest = self.root / "manifest.json"
        write_manifest(manifest, {"schema_version": 1, "files": second})
        self.assertEqual(second, json.loads(manifest.read_text())["files"])
        self.assertEqual(["manifest.json"], [path.name for path in self.root.iterdir() if path.name.startswith(("manifest", ".manifest"))])

    def test_card_bytes_mtimes_and_listing_are_unchanged_after_ingest(self):
        before = snapshot_tree(self.card)
        listing = sorted(str(path.relative_to(self.card)) for path in self.card.rglob("*"))
        for path in [self.card, *self.card.rglob("*")]:
            path.chmod(0o555 if path.is_dir() else 0o444)  # a write attempt anywhere on the card would raise
        entries = [ingest_item(item, (self.primary, self.mirror)) for item in self.plan()]
        self.assertTrue(all(outcome == "copied" for entry in entries for outcome in entry["outcomes"].values()))
        self.assertEqual(before, snapshot_tree(self.card))
        self.assertEqual(listing, sorted(str(path.relative_to(self.card)) for path in self.card.rglob("*")))


if __name__ == "__main__":
    unittest.main()
