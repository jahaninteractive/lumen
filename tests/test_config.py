import os
import unittest
from pathlib import Path
from unittest.mock import patch

from lumen import config
from lumen.config import Settings


class ConfigPathTests(unittest.TestCase):
    def test_vendor_root_defaults_to_the_checkout(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LUMEN_VENDOR", None)
            self.assertEqual(Path(config.__file__).resolve().parents[2] / "vendor", config.vendor_root())

    def test_lumen_vendor_moves_places_and_map(self):
        with patch.dict(os.environ, {"LUMEN_VENDOR": "/tmp/lv x"}):
            os.environ.pop("LUMEN_GEONAMES_DIR", None)
            os.environ.pop("LUMEN_MAP_DIR", None)
            settings = Settings.from_environment()
        self.assertEqual(Path("/tmp/lv x/data/geonames"), settings.geonames_dir)
        self.assertEqual(Path("/tmp/lv x/data/map"), settings.map_dir)

    def test_geonames_dir_variable_still_wins(self):
        with patch.dict(os.environ, {"LUMEN_VENDOR": "/tmp/lv", "LUMEN_GEONAMES_DIR": "/tmp/g"}):
            self.assertEqual(Path("/tmp/g"), Settings.from_environment().geonames_dir)

    def test_helper_dir_moves_both_helpers(self):
        with patch.dict(os.environ, {"LUMEN_HELPER_DIR": "/tmp/h"}):
            settings = Settings.from_environment()
        self.assertEqual(Path("/tmp/h/vision_analyzer"), settings.vision_analyzer_path)
        self.assertEqual(Path("/tmp/h/raw_developer"), settings.raw_developer_path)

    def test_helpers_default_to_the_library_cache(self):
        settings = Settings(library_dir=Path("/tmp/lib"))
        self.assertEqual(Path("/tmp/lib/cache/bin/raw_developer"), settings.raw_developer_path)


if __name__ == "__main__":
    unittest.main()
