import re
import subprocess
import unittest
from pathlib import Path


class ScriptTests(unittest.TestCase):
    def test_bootstrap_check_reports_required_commands(self):
        result = subprocess.run(
            ["zsh", "scripts/bootstrap.sh", "--check"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("uv: ready", result.stdout)
        self.assertIn("exiftool: ready", result.stdout)
        self.assertIn("swiftc: ready", result.stdout)

    def test_bootstrap_compiles_both_native_helpers(self):
        # Comments stripped, so a commented-out compile step fails this rather than passing it.
        live = " ".join(line.split("#")[0] for line in Path("scripts/bootstrap.sh").read_text().splitlines())
        for helper in ("vision_analyzer", "raw_developer"):
            self.assertRegex(
                live,
                rf'swiftc[^"]*"\$project_root/tools/{helper}\.swift"\s*\\?\s*-o\s*"\$native_bin/{helper}"',
            )

    def test_bootstrap_parses_as_zsh(self):
        result = subprocess.run(
            ["zsh", "-n", "scripts/bootstrap.sh"], capture_output=True, text=True, timeout=30, check=False
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_bootstrap_pins_and_verifies_its_downloads(self):
        script = Path("scripts/bootstrap.sh").read_text()
        for needle in ("--no-download", "--yes", "darktable-5.6.2", "siril-1.4.4", "oMLX-0.7.2", "shasum -a 256"):
            self.assertIn(needle, script)
        pinned = re.findall(r'\n([a-z_]+_sha256)="([^"]*)"', script)
        self.assertEqual(["darktable_sha256", "siril_sha256", "omlx_sequoia_sha256", "omlx_tahoe_sha256"], [n for n, _ in pinned])
        for name, digest in pinned:
            self.assertRegex(digest, r"^[0-9a-f]{64}$", name)
        # The mounted disk image is detached from an EXIT trap, so a failed copy still unmounts it.
        self.assertRegex(script, r"\ntrap detach_dmg EXIT")
        self.assertRegex(script, r"detach_dmg\(\) \{[^}]*hdiutil detach")

    def test_bootstrap_pins_the_natural_earth_layers(self):
        live = " ".join(line.split("#")[0] for line in Path("scripts/bootstrap.sh").read_text().splitlines())
        self.assertIn("ca96624a56bd078437bca8184e78163e5039ad19", live)
        for layer in ("ne_10m_coastline", "ne_10m_admin_0_boundary_lines_land", "ne_10m_rivers_lake_centerlines",
                      "ne_10m_lakes", "ne_10m_roads", "ne_10m_populated_places_simple", "ne_10m_land",
                      "ne_10m_geography_regions_polys", "ne_10m_geography_regions_points",
                      "ne_10m_geography_regions_elevation_points", "ne_10m_geography_marine_polys"):
            self.assertRegex(live, rf"{layer}\.geojson\S*\s+[0-9a-f]{{64}}")
        self.assertIn('vendor/data/map', live)
        self.assertIn("about 100 MB", live)

    def test_web_ui_reports_live_omlx_health(self):
        script = Path("src/lumen/static/app.js").read_text()
        self.assertIn("status.omlx_live?.online", script)
        self.assertIn("'Local AI online'", script)


if __name__ == "__main__":
    unittest.main()
