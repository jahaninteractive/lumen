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

    def test_sidecar_carries_lumen_develop_for_a_developed_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = SidecarWriter(Path(directory))
            photo = Photo(id=7, path="/photos/a.ARW", fingerprint="abcdef123456", captured_at=None, camera_id="A1")
            metrics = PhotoMetrics(sharpness=0.8, exposure=0.7, clipping=0.01)
            _, developed = writer.write(photo, metrics, [], selected=False,
                                        develop={"exposure_ev": 0.5, "contrast": 0.3})
            self.assertIn('<lumen:develop>{"contrast": 0.3, "exposure_ev": 0.5}</lumen:develop>', developed.read_text())
            _, plain = writer.write(photo, metrics, [], selected=False)
            self.assertNotIn("lumen:develop", plain.read_text())
            self.assertNotIn("lumen:develop", SidecarWriter._xmp("d", [], rating=0, develop={}))

    def test_place_is_in_json_and_xmp_location_and_country_only_when_present(self):
        from xml.etree import ElementTree
        with tempfile.TemporaryDirectory() as directory:
            writer = SidecarWriter(Path(directory))
            photo = Photo(id=9, path="/photos/a.ARW", fingerprint="abcdef123456", captured_at=None, camera_id="A1")
            metrics = PhotoMetrics(sharpness=0.8, exposure=0.7, clipping=0.01)
            json_path, xmp_path = writer.write(photo, metrics, [], selected=False, place="Madikwe Game Reserve", country="South Africa")
            payload = json.loads(json_path.read_text())
            self.assertEqual(("Madikwe Game Reserve", "South Africa"), (payload["place"], payload["place_country"]))
            xmp = xmp_path.read_text()
            self.assertIn("<Iptc4xmpCore:Location>Madikwe Game Reserve</Iptc4xmpCore:Location>", xmp)
            self.assertIn("<photoshop:Country>South Africa</photoshop:Country>", xmp)
            self.assertIn('xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"', xmp)
        hostile = SidecarWriter._xmp("d", [], rating=0, place="R&D Reserve <test>", country="A & B")
        root = ElementTree.fromstring(hostile)
        self.assertEqual("R&D Reserve <test>", root.find(".//{http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/}Location").text)
        self.assertEqual("A & B", root.find(".//{http://ns.adobe.com/photoshop/1.0/}Country").text)
        bare = SidecarWriter._xmp("d", [], rating=0)
        self.assertNotIn("Location", bare)
        self.assertNotIn("photoshop", bare)

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
