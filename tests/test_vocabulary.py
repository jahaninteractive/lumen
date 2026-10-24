import json
import tempfile
import unittest
from pathlib import Path

from lumen.config import Settings
from lumen.vocabulary import forget_keyword, learn_keywords, all_keywords, load_blocked, load_vocabulary, match_keywords


class VocabularyTests(unittest.TestCase):
    def test_starter_is_copied_once_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(library_dir=Path(directory))
            settings.ensure_directories()
            first = load_vocabulary(settings)
            self.assertIn("gentoo penguin", first["wildlife"])
            settings.vocabulary_path.write_text(json.dumps({"schema_version": 1, "categories": {"wildlife": ["orca"]}}))
            self.assertEqual({"wildlife": ["orca"]}, load_vocabulary(settings))

    def test_invalid_file_is_reported_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(library_dir=Path(directory))
            settings.ensure_directories()
            settings.vocabulary_path.write_text('{"categories": {"wildlife": [1]}}')
            with self.assertRaisesRegex(ValueError, "vocabulary.json is invalid"):
                load_vocabulary(settings)
            self.assertEqual('{"categories": {"wildlife": [1]}}', settings.vocabulary_path.read_text())

    def test_matching_is_case_and_whitespace_insensitive_and_caps_suggestions(self):  # Review Focus
        vocab = {"wildlife": ["gentoo penguin"], "scene": ["ice", "zodiac"]}
        applied, suggested = match_keywords([" Gentoo Penguin ", "ICE", "ice", "leopard", "Leopard", "krill", "skua", "fog"], vocab)
        self.assertEqual(["gentoo penguin", "ice"], applied)
        self.assertEqual(["leopard", "krill", "skua"], suggested)
        self.assertEqual(["gentoo penguin", "ice", "zodiac"], all_keywords(vocab))

    def test_learn_keywords_appends_new_words_once_and_keeps_the_file_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(library_dir=Path(directory))
            load_vocabulary(settings)
            added = learn_keywords(settings, {"learned": ["Impala", "ice", "impala", " "], "wildlife": ["lion"]})
            self.assertEqual(["Impala", "lion"], added, "existing words (any case) and blanks are not re-added")
            vocab = load_vocabulary(settings)
            self.assertEqual(["Impala"], vocab["learned"])
            self.assertIn("lion", vocab["wildlife"])
            self.assertEqual([], learn_keywords(settings, {"learned": ["IMPALA"]}))
            self.assertEqual(1, json.loads(settings.vocabulary_path.read_text())["schema_version"])

    def test_forget_keyword_blocks_relearning(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(library_dir=Path(directory))
            load_vocabulary(settings)
            self.assertEqual([], load_blocked(settings))
            self.assertTrue(forget_keyword(settings, "Ice"))
            self.assertNotIn("ice", [word.casefold() for word in all_keywords(load_vocabulary(settings))])
            self.assertEqual(["ice"], load_blocked(settings))
            self.assertEqual([], learn_keywords(settings, {"learned": ["ICE"]}))
            self.assertFalse(forget_keyword(settings, "ice"), "already blocked and absent: nothing changed")
            self.assertTrue(forget_keyword(settings, " Krill "), "a word never learned is still blocked")
            self.assertEqual(["ice", "krill"], load_blocked(settings))

    def test_blocked_key_is_optional_and_schema_version_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(library_dir=Path(directory))
            settings.ensure_directories()
            settings.vocabulary_path.write_text(json.dumps({"schema_version": 1, "categories": {"learned": ["orca"]}}))
            self.assertEqual([], load_blocked(settings))
            forget_keyword(settings, "orca")
            payload = json.loads(settings.vocabulary_path.read_text())
            self.assertEqual({"schema_version": 1, "categories": {"learned": []}, "blocked": ["orca"]}, payload)
            learn_keywords(settings, {"learned": ["skua"]})
            self.assertEqual(["orca"], json.loads(settings.vocabulary_path.read_text())["blocked"], "learning keeps the blocked list")


if __name__ == "__main__":
    unittest.main()
