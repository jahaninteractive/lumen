import tempfile
import unittest
from datetime import date
from pathlib import Path

from PIL import Image

from lumen.config import Settings
from lumen.digest import day_facts, render_facts_table
from lumen.library import Library


def _frames(root, day="2026:10:22", count=3):
    for index in range(count):
        image = Image.new("RGB", (120, 90), (index * 40, 0, 0)); exif = Image.Exif(); exif[36867] = f"{day} 10:00:{index:02d}"
        image.save(root / f"d{index}.jpg", exif=exif)


class DigestTests(unittest.TestCase):
    def test_facts_count_frames_keepers_rejects_keywords_and_top_frames_for_the_day_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); _frames(root)
            (root / "other").mkdir()
            _frames(root / "other", "2026:10:21", 1)
            with Library(Settings(library_dir=root / "library")) as library:
                library.import_path(root)
                photos = sorted(library.catalog.list_photos(), key=lambda p: p.path)
                library.set_decision(photos[1].id, "reject")
                library.catalog.save_tags(photos[0].id, {"description": "d", "subjects": ["ice", "gentoo penguin"]}, "m")
                library.catalog.save_tags(photos[2].id, {"description": "d", "subjects": ["ice"]}, "m")
                facts = day_facts(library, date(2026, 10, 22))
        self.assertEqual((3, 1, 1), (facts["frames"], facts["rejects"], facts["bursts"]))
        self.assertEqual([["ice", 2], ["gentoo penguin", 1]], facts["keywords"])
        self.assertEqual(2, len(facts["top_frames"]))
        table = render_facts_table(facts)
        self.assertIn("# Lumen digest 2026-10-22", table); self.assertIn("| Frames | 3 |", table); self.assertIn("| ice | 2 |", table)

    def test_empty_day_renders_sentence_without_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            with Library(Settings(library_dir=Path(directory) / "library")) as library:
                facts = day_facts(library, date(2026, 10, 23))
        text = render_facts_table(facts)
        self.assertIn("No frames were imported for this day.", text); self.assertNotIn("|", text)

    def test_places_count_frames_per_place_and_skip_placeless_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); _frames(root)
            with Library(Settings(library_dir=root / "library")) as library:
                library.import_path(root)
                bare = render_facts_table(day_facts(library, date(2026, 10, 22)))
                for photo in sorted(library.catalog.list_photos(), key=lambda p: p.path)[:2]:
                    library.catalog.update_place(photo.id, place="Madikwe Game Reserve", country="South Africa")
                facts = day_facts(library, date(2026, 10, 22))
        self.assertEqual([("Madikwe Game Reserve, South Africa", 2)], facts["places"])
        table = render_facts_table(facts)
        self.assertIn("| Place | Frames |", table); self.assertIn("| Madikwe Game Reserve, South Africa | 2 |", table)
        self.assertIn("No places recorded.", bare)

if __name__ == "__main__":
    unittest.main()
