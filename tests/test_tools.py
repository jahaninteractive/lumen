import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lumen.pipelines import PipelineRegistry
from lumen.tools import find_tool, tool_status


def make_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\necho tool 1.0\n")
    path.chmod(0o755)
    return path


class FindToolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.vendor = self.root / "vendor" / "apps"
        self.vendor.mkdir(parents=True)
        patcher = patch("lumen.tools.VENDOR_APPS", self.vendor)
        patcher.start()
        self.addCleanup(patcher.stop)

    def bundled(self, app: str, name: str) -> Path:
        return make_executable(self.vendor / app / "Contents" / "MacOS" / name)

    def test_path_lookup_wins_over_project_local_bundle(self):
        self.bundled("darktable.app", "darktable-cli")
        with patch("lumen.tools.shutil.which", return_value="/opt/bin/darktable-cli"):
            self.assertEqual("/opt/bin/darktable-cli", find_tool("darktable-cli"))

    def test_falls_back_to_project_local_bundles_when_path_lookup_fails(self):
        darktable = self.bundled("darktable.app", "darktable-cli")
        siril = self.bundled("Siril.app", "siril-cli")
        with patch("lumen.tools.shutil.which", return_value=None):
            self.assertEqual(str(darktable), find_tool("darktable-cli"))
            self.assertEqual(str(siril), find_tool("siril-cli"))

    def test_missing_bundle_is_unavailable(self):
        with patch("lumen.tools.shutil.which", return_value=None):
            self.assertIsNone(find_tool("siril-cli"))

    def test_unbundled_tools_have_no_fallback(self):
        self.bundled("darktable.app", "exiftool")
        with patch("lumen.tools.shutil.which", return_value=None):
            self.assertIsNone(find_tool("exiftool"))

    def test_rejects_executable_symlink_escaping_bundle(self):
        outside = make_executable(self.root / "outside" / "siril-cli")
        link = self.vendor / "Siril.app" / "Contents" / "MacOS" / "siril-cli"
        link.parent.mkdir(parents=True)
        link.symlink_to(outside)
        with patch("lumen.tools.shutil.which", return_value=None):
            self.assertIsNone(find_tool("siril-cli"))

    def test_rejects_bundle_symlinked_elsewhere(self):
        make_executable(self.root / "elsewhere.app" / "Contents" / "MacOS" / "darktable-cli")
        (self.vendor / "darktable.app").symlink_to(self.root / "elsewhere.app", target_is_directory=True)
        with patch("lumen.tools.shutil.which", return_value=None):
            self.assertIsNone(find_tool("darktable-cli"))

    def test_allows_symlink_that_stays_inside_bundle(self):
        real = make_executable(self.vendor / "Siril.app" / "Contents" / "Resources" / "bin" / "siril-cli")
        (self.vendor / "Siril.app" / "Contents" / "MacOS").mkdir(parents=True)
        (self.vendor / "Siril.app" / "Contents" / "MacOS" / "siril-cli").symlink_to(real)
        with patch("lumen.tools.shutil.which", return_value=None):
            self.assertEqual(str(real), find_tool("siril-cli"))

    def test_rejects_non_executable_or_directory(self):
        target = self.bundled("Siril.app", "siril-cli")
        target.chmod(0o644)
        (self.vendor / "darktable.app" / "Contents" / "MacOS" / "darktable-cli").mkdir(parents=True)
        with patch("lumen.tools.shutil.which", return_value=None):
            self.assertIsNone(find_tool("siril-cli"))
            self.assertIsNone(find_tool("darktable-cli"))

    def test_doctor_and_pipeline_registry_use_the_fallback(self):
        darktable = self.bundled("darktable.app", "darktable-cli")
        siril = self.bundled("Siril.app", "siril-cli")
        with patch("lumen.tools.shutil.which", return_value=None):
            status = tool_status("siril-cli")
            registry = PipelineRegistry()
            capabilities = registry.capabilities()
            pipeline = registry.darktable(self.root / "outputs")
        self.assertTrue(status.available)
        self.assertEqual(str(siril), status.path)
        self.assertEqual("tool 1.0", status.version)
        self.assertEqual(str(darktable), capabilities["darktable"].executable)
        self.assertEqual(str(siril), capabilities["siril"].executable)
        self.assertEqual(str(darktable), pipeline.executable)


if __name__ == "__main__":
    unittest.main()
