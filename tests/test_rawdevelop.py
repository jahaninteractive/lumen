import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from lumen.rawdevelop import DevelopError, DevelopRender, _partial_path, render_raw

RESPONSE = {
    "width": 1100,
    "height": 733,
    "histogram": {"y": [0.5522, 0.0256], "r": [0.4, 0.1], "g": [0.5, 0.2], "b": [0.6, 0.3]},
    "defaults": {"contrast": 0.0, "sharpness": 1.0, "luminance_nr": 0.0041, "color_nr": 0.5},
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
        # What the native helper renders into before renaming onto the destination.
        self.partial = _partial_path(self.destination)
        self.request_log = self.root / "request.json"

    def assertNothingLeftBehind(self, label: str) -> None:
        self.assertFalse(self.destination.exists(), f"{label} left a truncated image behind")
        self.assertFalse(self.partial.exists(), f"{label} left the helper's temp file behind")

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

        self.assertEqual(render.histogram, {"y": (0.5522, 0.0256), "r": (0.4, 0.1),
                                            "g": (0.5, 0.2), "b": (0.6, 0.3)})
        self.assertEqual(render.neutral_temperature, 3490.1663)
        self.assertEqual(render.neutral_tint, -9.4)
        self.assertEqual((1100, 733), (render.width, render.height))

    def test_render_raw_reports_the_decoder_defaults(self):
        """The panel parks each handle on these, so an invented mid-range would mislead."""
        render = render_raw(self.source, self.destination, self._responder())

        self.assertEqual({"contrast": 0.0, "sharpness": 1.0, "luminance_nr": 0.0041, "color_nr": 0.5},
                         render.defaults)

    def test_render_raw_ignores_defaults_it_cannot_read(self):
        for shape in ([0.5], {"sharpness": "lots"}, "nonsense", {}):
            with self.subTest(shape=shape):
                render = render_raw(self.source, self.destination,
                                    self._responder({**RESPONSE, "defaults": shape}))
                self.assertEqual({}, render.defaults)

    def test_render_raw_ignores_a_histogram_that_is_not_four_channels(self):
        """An un-rebuilt helper returns a bare list; the render still succeeds."""
        for shape in ([0.1, 0.2], {"y": "not buckets"}, "nonsense", {}):
            with self.subTest(shape=shape):
                render = render_raw(self.source, self.destination,
                                    self._responder({**RESPONSE, "histogram": shape}),
                                    histogram=True)
                self.assertEqual({}, render.histogram)
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

    def test_render_raw_gives_the_helper_the_caller_s_timeout(self):
        seen = []

        def runner(*args, **kwargs):
            seen.append(kwargs["timeout"])
            raise subprocess.TimeoutExpired(cmd="raw_developer", timeout=kwargs["timeout"])

        for call in ({}, {"timeout": 600}):
            with self.subTest(call=call), self.assertRaises(DevelopError):
                render_raw(self.source, self.destination, self.root / "raw_developer", runner=runner, **call)
        self.assertEqual([300, 600], seen, "an export asks for longer than an interactive render")

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

    def test_render_raw_rejects_a_stale_destination_left_by_an_earlier_render(self):
        self.destination.write_bytes(b"an earlier render")
        executable = self._stub(f"printf '%s' '{json.dumps(RESPONSE)}'")

        with self.assertRaises(DevelopError):
            render_raw(self.source, self.destination, executable)

        self.assertFalse(self.destination.exists(), "the stale render must not survive as this run's output")

    def test_render_raw_leaves_no_file_behind_when_the_render_fails(self):
        # A helper interrupted mid-write leaves both a part-written destination and its
        # temp sibling; neither may survive into the photographer's library.
        half_written = (
            f"printf 'half a tiff' > '{self.destination}'\n"
            f"printf 'half a tiff' > '{self.partial}'\n"
        )
        failures = {
            "non-zero exit": self._stub(half_written + "exit 3"),
            "unparseable stdout": self._stub(half_written + "printf 'not json'"),
            "malformed payload": self._stub(half_written + """printf '%s' '{"width":"wide"}'"""),
            "overflowing payload": self._stub(half_written + """printf '%s' '{"width":1e400,"height":1,"neutral_temperature":0,"neutral_tint":0}'"""),
        }
        for label, executable in failures.items():
            with self.subTest(label):
                with self.assertRaises(DevelopError):
                    render_raw(self.source, self.destination, executable)
                self.assertNothingLeftBehind(label)

    def test_render_raw_leaves_no_file_behind_when_the_render_times_out(self):
        self.destination.write_bytes(b"half a tiff")
        self.partial.write_bytes(b"half a tiff")

        def runner(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="raw_developer", timeout=300)

        with self.assertRaises(DevelopError):
            render_raw(self.source, self.destination, self.root / "raw_developer", runner=runner)

        self.assertNothingLeftBehind("a timeout")

    def test_render_raw_refuses_to_write_over_the_camera_original(self):
        original = self.source.read_bytes()
        link = self.root / "alias.ARW"
        os.symlink(self.source, link)
        hard_link = self.root / "hardlink.ARW"
        os.link(self.source, hard_link)

        for destination in (self.source, link, hard_link):
            with self.subTest(str(destination)):
                with self.assertRaises(DevelopError) as caught:
                    render_raw(self.source, destination, self._responder())
                self.assertIn("DSC02733.ARW", str(caught.exception))
                self.assertNotIn(str(self.root), str(caught.exception))

        self.assertEqual(original, self.source.read_bytes())
        self.assertFalse(self.request_log.exists(), "the helper must never be run against the original")


@unittest.skipUnless(shutil.which("swiftc"), "swiftc is required to build the native helper")
class NativeHelperRequestTests(unittest.TestCase):
    """The helper itself, built from source. No request here reaches Core Image:
    every one is rejected during validation, before the raw is opened."""

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.executable = Path(cls.directory.name) / "raw_developer"
        build = subprocess.run(
            ["swiftc", "-O", "tools/raw_developer.swift", "-o", str(cls.executable)],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if build.returncode != 0:
            raise AssertionError(f"tools/raw_developer.swift does not compile:\n{build.stderr}")

    def _run(self, request: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(self.executable)],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def test_a_present_but_invalid_field_fails_rather_than_rendering_without_it(self):
        base = {"source": "/nowhere/DSC00001.ARW", "destination": str(Path(self.directory.name) / "out.tif")}
        invalid = {
            "a string where a number belongs": {"settings": {"exposure_ev": "0.5"}},
            "a boolean where a number belongs": {"settings": {"exposure_ev": True}},
            "a string long edge": {"long_edge": "1100"},
            "a negative long edge": {"long_edge": -5},
            "settings that are not an object": {"settings": []},
            "a curve of the wrong length": {"settings": {"curve": [[0, 0], [1, 1]]}},
            "a curve that does not ascend": {"settings": {"curve": [[0, 0], [0.5, 0.2], [0.25, 0.5], [0.75, 0.8], [1, 1]]}},
            "a misspelled setting": {"settings": {"exposure": 0.5}},
            "an unsupported format": {"format": "png"},
            "a non-boolean histogram flag": {"histogram": "yes"},
        }
        for label, overrides in invalid.items():
            with self.subTest(label):
                completed = self._run(base | overrides)
                # Exit 2 is "bad request", raised before the raw is opened. Any other
                # code would mean the helper got as far as rendering without the setting.
                self.assertEqual(2, completed.returncode, f"{label} was silently ignored")
                self.assertTrue(completed.stderr.strip(), f"{label} reported nothing on stderr")
                self.assertEqual("", completed.stdout.strip())

    def test_an_absent_field_is_not_an_invalid_one(self):
        # Reaches the decoder and fails there, proving validation let the request through.
        completed = self._run({
            "source": "/nowhere/DSC00001.ARW",
            "destination": str(Path(self.directory.name) / "out.tif"),
            "settings": {},
        })

        self.assertNotEqual(2, completed.returncode)
        self.assertIn("DSC00001.ARW", completed.stderr)


if __name__ == "__main__":
    unittest.main()
