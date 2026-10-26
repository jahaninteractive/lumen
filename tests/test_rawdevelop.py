import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from lumen.rawdevelop import DevelopError, DevelopRender, render_raw

RESPONSE = {
    "width": 1100,
    "height": 733,
    "histogram": [0.5522, 0.0256],
    "neutral_temperature": 3490.1663,
    "neutral_tint": -9.4,
}


class RenderRawTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "DSC02733.ARW"
        self.source.write_bytes(b"not really a raw")
        self.destination = self.root / "out.tif"
        self.request_log = self.root / "request.json"

    def _stub(self, body: str) -> Path:
        executable = self.root / "raw_developer"
        executable.write_text(f"#!/bin/sh\ncat > '{self.request_log}'\n{body}\n")
        executable.chmod(0o755)
        return executable

    def _responder(self, response: dict | None = None) -> Path:
        payload = json.dumps(RESPONSE if response is None else response)
        return self._stub(f"printf '%s' > '{self.destination}'\nprintf '%s' '{payload}'")

    def _request(self) -> dict:
        return json.loads(self.request_log.read_text())

    def test_render_raw_sends_the_request_as_json_on_stdin(self):
        render = render_raw(
            self.source,
            self.destination,
            self._responder(),
            settings={"exposure_ev": 0.85, "tint": None},
            long_edge=1100,
            histogram=True,
        )

        request = self._request()
        self.assertEqual(request["source"], str(self.source))
        self.assertEqual(request["destination"], str(self.destination))
        self.assertEqual(request["long_edge"], 1100)
        self.assertEqual(request["format"], "tiff16")
        self.assertTrue(request["histogram"])
        self.assertEqual(request["settings"], {"exposure_ev": 0.85})
        self.assertIsInstance(render, DevelopRender)
        self.assertEqual(self.destination, render.path)

    def test_render_raw_returns_the_histogram_and_neutral_white_balance(self):
        render = render_raw(self.source, self.destination, self._responder(), histogram=True)

        self.assertEqual(render.histogram, (0.5522, 0.0256))
        self.assertEqual(render.neutral_temperature, 3490.1663)
        self.assertEqual(render.neutral_tint, -9.4)
        self.assertEqual((1100, 733), (render.width, render.height))

    def test_render_raw_omits_unset_settings_entirely(self):
        render_raw(self.source, self.destination, self._responder(), settings={})

        self.assertEqual(self._request()["settings"], {})

    def test_render_raw_passes_the_requested_image_format(self):
        render_raw(self.source, self.destination, self._responder(), image_format="jpeg")

        self.assertEqual("jpeg", self._request()["format"])

    def test_render_raw_raises_develop_error_when_the_helper_exits_non_zero(self):
        executable = self._stub("echo 'unable to decode' >&2\nexit 3")

        with self.assertRaises(DevelopError) as caught:
            render_raw(self.source, self.destination, executable)

        message = str(caught.exception)
        self.assertIn("DSC02733.ARW", message)
        self.assertNotIn(str(self.root), message)
        self.assertNotIn("Traceback", message)

    def test_render_raw_raises_develop_error_when_the_helper_writes_no_output_file(self):
        executable = self._stub(f"printf '%s' '{json.dumps(RESPONSE)}'")

        with self.assertRaises(DevelopError) as caught:
            render_raw(self.source, self.destination, executable)

        self.assertIn("DSC02733.ARW", str(caught.exception))
        self.assertNotIn(str(self.root), str(caught.exception))

    def test_render_raw_raises_develop_error_on_unparseable_stdout(self):
        executable = self._stub(f"printf '%s' > '{self.destination}'\nprintf 'not json'")

        with self.assertRaises(DevelopError) as caught:
            render_raw(self.source, self.destination, executable)

        self.assertNotIn(str(self.root), str(caught.exception))

    def test_render_raw_raises_develop_error_on_timeout(self):
        def runner(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="raw_developer", timeout=300)

        with self.assertRaises(DevelopError) as caught:
            render_raw(self.source, self.destination, self.root / "raw_developer", runner=runner)

        self.assertIn("DSC02733.ARW", str(caught.exception))
        self.assertNotIn(str(self.root), str(caught.exception))

    def test_render_raw_raises_develop_error_when_the_executable_is_missing(self):
        with self.assertRaises(DevelopError) as caught:
            render_raw(self.source, self.destination, self.root / "missing")

        self.assertNotIn(str(self.root), str(caught.exception))


if __name__ == "__main__":
    unittest.main()
