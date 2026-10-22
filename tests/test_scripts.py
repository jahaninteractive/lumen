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

    def test_web_ui_displays_live_omlx_summary(self):
        script = Path("src/lumen/static/app.js").read_text()
        self.assertIn("status.omlx_live.summary", script)


if __name__ == "__main__":
    unittest.main()
