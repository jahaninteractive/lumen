import os
import re
import subprocess
import tempfile
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

    def _bootstrap(self, *args, env=None):
        return subprocess.run(["zsh", "scripts/bootstrap.sh", *args], capture_output=True, text=True, timeout=30, check=False, env=env)

    def test_list_prints_every_component_without_touching_anything(self):
        with tempfile.TemporaryDirectory() as dest:
            result = self._bootstrap("--list", "--dest", dest)
            self.assertEqual(0, result.returncode, result.stderr)
            rows = [line.split("\t") for line in result.stdout.splitlines()]
            self.assertEqual(["places", "map", "darktable", "siril", "omlx", "fast-model", "review-model"], [r[0] for r in rows])
            self.assertTrue(all(len(r) == 6 and r[4] in ("ready", "missing") and r[5] in ("on", "off") for r in rows))
            self.assertEqual([], os.listdir(dest))

    def test_downloads_only_list_leaves_local_ai_out(self):
        result = self._bootstrap("--downloads-only", "--list", "--dest", "/nonexistent")
        self.assertEqual(["places", "map", "darktable", "siril"], [l.split("\t")[0] for l in result.stdout.splitlines()])

    def test_list_sees_places_in_a_dest_with_a_space(self):
        with tempfile.TemporaryDirectory() as root:
            dest = Path(root) / "Application Support" / "Lumen"
            (dest / "data" / "geonames").mkdir(parents=True)
            (dest / "data" / "geonames" / "cities500.txt").write_text("")
            rows = dict((l.split("\t")[0], l.split("\t")[4]) for l in self._bootstrap("--list", "--dest", str(dest)).stdout.splitlines())
            self.assertEqual("ready", rows["places"])
            self.assertEqual("missing", rows["map"])

    def test_countries_prints_the_installed_country_files_only(self):
        with tempfile.TemporaryDirectory() as dest:
            result = self._bootstrap("--downloads-only", "--dest", dest, "--countries")
            self.assertEqual((0, ""), (result.returncode, result.stdout), result.stderr)
            geonames = Path(dest) / "data" / "geonames"
            geonames.mkdir(parents=True)
            for name in ("ZA.txt", "AQ.txt", "ZA.zip", "cities500.txt", "countryInfo.txt", "readme.txt", "cities500.zip"):
                (geonames / name).write_text("")
            result = self._bootstrap("--downloads-only", "--dest", dest, "--countries")
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("AQ\nZA\n", result.stdout)
            self.assertEqual(sorted(["ZA.txt", "AQ.txt", "ZA.zip", "cities500.txt", "countryInfo.txt", "readme.txt", "cities500.zip"]),
                             sorted(os.listdir(geonames)))

    def test_downloads_only_places_with_no_country_codes_is_not_skipped(self):
        with tempfile.TemporaryDirectory() as dest:
            geonames = Path(dest) / "data" / "geonames"
            geonames.mkdir(parents=True)
            for name in ("countryInfo.txt", "readme.txt", "cities500.zip", "cities500.txt"):
                (geonames / name).write_text("")
            env = {**os.environ, "LUMEN_GEONAMES_COUNTRIES": ""}
            result = self._bootstrap("--downloads-only", "--dest", dest, "--only", "places", env=env)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("GeoNames cities500: ready", result.stdout)
            self.assertFalse([l for l in result.stdout.splitlines() if l.startswith("Skipped:")], result.stdout)

    def test_only_refuses_an_unknown_or_empty_key(self):
        for keys in ("bogus", "", "map,bogus"):
            result = self._bootstrap("--downloads-only", "--only", keys, "--dest", "/nonexistent")
            self.assertEqual(2, result.returncode, keys)
            self.assertIn("usage:", result.stderr)

    def test_no_path_under_the_checkout_vendor_is_spelled_out(self):
        live = "\n".join(line.split("#")[0] for line in Path("scripts/bootstrap.sh").read_text().splitlines())
        self.assertEqual(1, live.count("$project_root/vendor"))   # only the default of vendor_root

    def test_a_terminated_run_still_detaches_its_disk_image(self):
        script = Path("scripts/bootstrap.sh").read_text()
        self.assertRegex(script, r"\ntrap 'exit 143' TERM INT")

    def test_disk_image_attach_drops_only_the_deprecation_warning(self):
        # Runs the script's own attach line against a fake hdiutil that warns, errors and fails.
        line = next(l for l in Path("scripts/bootstrap.sh").read_text().splitlines() if "hdiutil attach" in l)
        with tempfile.TemporaryDirectory() as root:
            fake = Path(root) / "hdiutil"
            fake.write_text("#!/bin/zsh\n"
                            "echo \"hdiutil: WARNING: 'hdiutil attach' is deprecated. Please use 'diskutil image attach' instead.\" >&2\n"
                            "echo 'hdiutil: attach failed - no such file' >&2\nexit 3\n")
            fake.chmod(0o755)
            body = f'set -eu\nmount_point=/nonexistent dmg=/nonexistent\n{line}\necho survived\n'
            env = {**os.environ, "PATH": f"{root}:{os.environ['PATH']}"}
            result = subprocess.run(["zsh", "-c", body], capture_output=True, text=True, timeout=30, check=False, env=env)
        self.assertEqual(3, result.returncode)
        self.assertNotIn("survived", result.stdout)
        self.assertIn("attach failed", result.stderr)
        self.assertNotIn("deprecated", result.stderr)

    def test_a_sha256_mismatch_removes_the_bad_download(self):
        # fetch skips a file that exists, so a bad one left behind would fail every re-run the same way.
        live = "\n".join(line.split("#")[0] for line in Path("scripts/bootstrap.sh").read_text().splitlines())
        branch = re.search(r'verify_sha256\(\) \{.*?if \[\[ "\$got" != "\$2" \]\]; then(.*?)\n    exit 1', live, re.S)
        self.assertIsNotNone(branch)
        self.assertRegex(branch.group(1), r'\n\s*rm -f "\$1"')
        self.assertIn("Removed it; run again to download it afresh.", branch.group(1))

    def test_a_rerun_still_fetches_a_missing_geonames_file(self):
        live = "\n".join(line.split("#")[0] for line in Path("scripts/bootstrap.sh").read_text().splitlines())
        self.assertNotIn("if ! component_ready places", live)

    def test_build_app_parses_and_signs_for_distribution(self):
        self.assertEqual(0, subprocess.run(["zsh", "-n", "scripts/build_app.sh"], capture_output=True, timeout=30).returncode)
        script = Path("scripts/build_app.sh").read_text()
        for needle in ("au.com.jahan-interactive.lumen", "--options runtime", "--timestamp", "notarytool submit",
                       "stapler staple", "arm64-apple-macos13.5", "LSMinimumSystemVersion", "compileall"):
            self.assertIn(needle, script)
        for source in ("vision_analyzer", "raw_developer", "lumen_app"):
            self.assertIn(f"tools/{source}.swift", script)

    def test_build_app_pins_exiftool(self):
        script = Path("scripts/build_app.sh").read_text()
        self.assertRegex(script, r'\nexiftool_sha256="[0-9a-f]{64}"')

    def test_smoke_app_parses(self):
        self.assertEqual(0, subprocess.run(["zsh", "-n", "scripts/smoke_app.sh"], capture_output=True, timeout=30).returncode)

    def test_web_ui_reports_live_omlx_health(self):
        script = Path("src/lumen/static/app.js").read_text()
        self.assertIn("status.omlx_live?.online", script)
        self.assertIn("'Local AI online'", script)


if __name__ == "__main__":
    unittest.main()
