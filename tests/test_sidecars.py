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


if __name__ == "__main__":
    unittest.main()
