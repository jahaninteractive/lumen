import os
import tempfile
import unittest
from pathlib import Path

from lumen import diagnostics
from lumen.config import Settings


class StaleSourceTests(unittest.TestCase):
    """A server keeps the Python it loaded at startup while serving static files from disk.

    The page therefore always looks current, and a mismatch surfaces as whatever validation
    error happens to fire first. Twice it cost a debugging session, so the server says so.
    """

    def setUp(self):
        self.settings = Settings(library_dir=Path("/nowhere"))

    def test_a_server_running_the_code_on_disk_is_not_stale(self):
        self.assertEqual([], diagnostics.stale_sources(self.settings))

    def test_a_server_whose_python_changed_since_startup_says_so(self):
        reasons = diagnostics.stale_sources(self.settings, loaded_digest="something else")

        self.assertEqual(1, len(reasons), reasons)
        self.assertIn("restart", reasons[0].lower())
        self.assertIn("lumen serve", reasons[0])

    def test_a_helper_older_than_its_source_says_to_rebuild(self):
        """The Swift helper is compiled by hand, so an edited source is a real mismatch."""
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory)
            settings = Settings(library_dir=library)
            helper = settings.raw_developer_path
            helper.parent.mkdir(parents=True, exist_ok=True)
            helper.write_bytes(b"binary")
            os.utime(helper, (1, 1))  # older than the source in the repo

            reasons = diagnostics.stale_sources(settings)

            self.assertEqual(1, len(reasons), reasons)
            self.assertIn("bootstrap.sh", reasons[0])

            os.utime(helper, None)  # rebuilt just now
            self.assertEqual([], diagnostics.stale_sources(settings))

    def test_no_reason_names_an_absolute_path(self):
        """These reach the browser, where the contract forbids source paths."""
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(library_dir=Path(directory))
            helper = settings.raw_developer_path
            helper.parent.mkdir(parents=True, exist_ok=True)
            helper.write_bytes(b"binary")
            os.utime(helper, (1, 1))
            reasons = diagnostics.stale_sources(settings, loaded_digest="changed")
        self.assertEqual(2, len(reasons), "both conditions, so both messages are checked")
        for reason in reasons:
            self.assertNotRegex(reason, r"(^|\s)/|[A-Za-z]:\\", reason)
            self.assertNotIn(str(Path.home()), reason)
