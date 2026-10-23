import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from lumen.domain import Photo, PhotoMetrics, ProvenanceEvent
from lumen.sidecars import SidecarWriter


class SidecarTests(unittest.TestCase):
    def test_writes_atomic_json_and_xmp_with_portable_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = SidecarWriter(Path(directory))
            photo = Photo(
                id=42,
                path="/photos/a & b.ARW",
                fingerprint="abcdef123456",
                captured_at=None,
                camera_id="Sony:A1",
            )
            metrics = PhotoMetrics(sharpness=0.8, exposure=0.7, clipping=0.01)
            events = [
                ProvenanceEvent(
                    id=1,
                    photo_id=42,
                    kind="analyzed",
                    created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    details={"tool": "Pillow"},
                )
            ]

            json_path, xmp_path = writer.write(
                photo,
                metrics,
                events,
                selected=True,
                description="Bird & branch",
                keywords=["bird", "red & blue"],
            )

            payload = json.loads(json_path.read_text())
            self.assertEqual(1, payload["schema_version"])
            self.assertEqual(42, payload["photo_id"])
            self.assertEqual("abcdef123456", payload["source"]["fingerprint"])
            self.assertTrue(payload["selection"]["best_in_group"])
            self.assertEqual("analyzed", payload["provenance"][0]["kind"])
            self.assertIn("Bird &amp; branch", xmp_path.read_text())
            self.assertFalse(any(Path(directory).glob("*.tmp")))

    def test_xmp_carries_alt_text_in_iptc_core_only_when_present(self):
        with_alt = SidecarWriter._xmp("d", [], rating=0, alt_text="Two penguins on ice")
        self.assertIn('xmlns:Iptc4xmpCore="http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/"', with_alt)
        self.assertIn("<Iptc4xmpCore:AltTextAccessibility><rdf:Alt><rdf:li xml:lang=\"x-default\">Two penguins on ice</rdf:li>", with_alt)
        self.assertNotIn("AltTextAccessibility", SidecarWriter._xmp("d", [], rating=0))

    def test_sidecars_carry_decision_and_minus_one_xmp_rating_for_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = SidecarWriter(Path(directory))
            photo = Photo(id=7, path="/photos/a.ARW", fingerprint="abcdef123456", captured_at=None, camera_id="A1")
            metrics = PhotoMetrics(sharpness=0.8, exposure=0.7, clipping=0.01)
            for decision, rating, selected, expected in (
                ("reject", 4, False, "-1"), ("flag", 3, False, "3"), ("none", 0, True, "0"), ("none", 2, True, "2"),
            ):
                with self.subTest(decision=decision, rating=rating):
                    json_path, xmp_path = writer.write(photo, metrics, [], selected=selected, decision=decision, rating=rating)
                    payload = json.loads(json_path.read_text())
                    self.assertEqual((decision, rating), (payload["decision"], payload["rating"]))
                    self.assertIn(f'xmp:Rating="{expected}"', xmp_path.read_text(), "selection alone never implies a 5-star rating")


if __name__ == "__main__":
    unittest.main()
