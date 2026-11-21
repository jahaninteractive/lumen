import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from lumen import omlx, preferences
from lumen.catalog import Catalog
from lumen.config import Settings


class PreferencesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        shell = {var for var in preferences.SAVED.values() if var}
        self._env = patch.dict(os.environ, {k: v for k, v in os.environ.items() if k not in shell}, clear=True)
        self._env.start()
        self.settings = Settings(library_dir=Path(self.tmp) / "library")
        self.catalog = Catalog(self.settings.database_path)

    def tearDown(self):
        self.catalog.close()
        self._env.stop()
        self._tmp.cleanup()

    def test_saved_value_overlays_default(self):
        preferences.save(self.catalog, {"near_duplicate_distance": 8, "geocode_online": True})
        loaded = preferences.overlay(self.settings, self.catalog)
        self.assertEqual((loaded.near_duplicate_distance, loaded.geocode_online), (8, True))
        row = next(r for r in preferences.describe(self.settings, self.catalog) if r["key"] == "geocode_online")
        self.assertEqual(row, {"key": "geocode_online", "value": True, "source": "saved", "editable": True})

    def test_shell_variable_wins_and_is_not_editable(self):
        with patch.dict(os.environ, {"LUMEN_NEAR_DUP_DISTANCE": "12"}):
            with self.assertRaisesRegex(ValueError, r"set by your shell \(LUMEN_NEAR_DUP_DISTANCE\)"):
                preferences.save(self.catalog, {"near_duplicate_distance": 8})
            row = next(r for r in preferences.describe(self.settings, self.catalog) if r["key"] == "near_duplicate_distance")
            self.assertEqual((row["source"], row["editable"]), ("shell", False))

    def test_shell_variable_ignores_a_saved_value(self):
        preferences.save(self.catalog, {"near_duplicate_distance": 8})
        with patch.dict(os.environ, {"LUMEN_NEAR_DUP_DISTANCE": "12"}):
            self.assertEqual(preferences.overlay(self.settings, self.catalog).near_duplicate_distance, 12)

    def test_describe_lists_every_saved_key_with_defaults(self):
        rows = preferences.describe(self.settings, self.catalog)
        self.assertEqual([r["key"] for r in rows], list(preferences.SAVED))
        self.assertEqual(rows[0], {"key": "ai_engine", "value": "mac", "source": "default", "editable": True})

    def test_save_is_all_or_nothing(self):
        with self.assertRaises(ValueError):
            preferences.save(self.catalog, {"geocode_online": True, "near_duplicate_distance": 65})
        self.assertFalse(preferences.overlay(self.settings, self.catalog).geocode_online)

    def test_unreadable_saved_value_falls_back_to_default(self):
        self.catalog.set_preference("setting.near_duplicate_distance", "not json{")
        self.catalog.set_preference("setting.ai_engine", '"cloud"')
        loaded = preferences.overlay(self.settings, self.catalog)
        self.assertEqual((loaded.near_duplicate_distance, loaded.ai_engine), (12, "mac"))

    def test_load_without_a_catalog_returns_settings_unchanged(self):
        fresh = Settings(library_dir=Path(self.tmp) / "none")
        self.assertIs(preferences.load(fresh), fresh)
        self.assertFalse(fresh.database_path.exists())

    def test_load_reads_the_catalog_on_disk(self):
        preferences.save(self.catalog, {"ai_engine": "base", "base_url": "http://127.0.0.1:9000/v1"})
        loaded = preferences.load(self.settings)
        self.assertEqual((loaded.ai_engine, loaded.base_url), ("base", "http://127.0.0.1:9000/v1"))

    def test_validate_rejects_bool_as_distance_and_unknown_key(self):
        with self.assertRaises(ValueError):
            preferences.validate("near_duplicate_distance", True)
        with self.assertRaisesRegex(ValueError, "^unknown setting: nope$"):
            preferences.validate("nope", 1)
        self.assertEqual(preferences.validate("near_duplicate_distance", 0), 0)
        self.assertEqual(preferences.validate("near_duplicate_distance", 64), 64)
        with self.assertRaises(ValueError):
            preferences.validate("geocode_online", 1)
        with self.assertRaises(ValueError):
            preferences.validate("ai_engine", "cloud")

    def test_validate_model_names(self):
        self.assertEqual(preferences.validate("base_fast_model", "small"), "small")
        for bad in ("x" * 201, "a\nb", "a\x85b", 3):
            with self.assertRaises(ValueError):
                preferences.validate("online_review_model", bad)

    def test_service_presets_are_pinned(self):
        self.assertEqual(preferences.SERVICE_PRESETS, (
            {"name": "Anthropic", "url": "https://api.anthropic.com/v1"},
            {"name": "Google", "url": "https://generativelanguage.googleapis.com/v1beta/openai"},
            {"name": "OpenAI", "url": "https://api.openai.com/v1"},
            {"name": "Qwen (Alibaba Cloud)", "url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"},
        ))

    def test_every_service_preset_is_a_valid_online_address(self):
        for preset in preferences.SERVICE_PRESETS:
            with self.subTest(preset=preset["name"]):
                self.assertEqual(omlx.normalise_engine_url("online", preset["url"]), preset["url"])

    def test_validate_addresses_stores_the_normalised_form(self):
        self.assertEqual(preferences.validate("base_url", " http://pc.local:11434/ "), "http://pc.local:11434/v1")
        self.assertEqual(preferences.validate("online_url", "https://api.example.com/v1/"), "https://api.example.com/v1")
        self.assertEqual(preferences.validate("base_url", ""), "")
        self.assertEqual(preferences.validate("online_url", ""), "")
        with self.assertRaisesRegex(ValueError, "^Online needs an https address$"):
            preferences.validate("online_url", "http://api.example.com")
        with self.assertRaisesRegex(ValueError, "^Base needs an http or https address$"):
            preferences.validate("base_url", "ftp://pc")
        for bad in (3, None, "https://k:s@api.example.com"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                preferences.validate("online_url", bad)

    def test_model_for_follows_the_engine(self):
        s = replace(self.settings, ai_engine="base", base_fast_model="small", base_review_model="big")
        self.assertEqual((s.model_for("fast"), s.model_for("review")), ("small", "big"))
        o = replace(self.settings, ai_engine="online", online_fast_model="f", online_review_model="r")
        self.assertEqual((o.model_for("fast"), o.model_for("review")), ("f", "r"))
        self.assertEqual(self.settings.model_for("review"), self.settings.review_model)
        self.assertEqual(self.settings.model_for("fast"), self.settings.omlx_fast_model)


if __name__ == "__main__":
    unittest.main()
