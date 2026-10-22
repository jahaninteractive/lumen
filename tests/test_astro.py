import dataclasses
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lumen.astro import (
    SirilCapabilities, UnsupportedSirilVersion, load_manifest, prepare_siril_job, probe_siril, siril_script,
)

SIRIL = SirilCapabilities("/Apps/Siril.app/Contents/MacOS/siril-cli", (1, 4, 4))

FULL_SCRIPT = """requires 1.4.0 1.5.0
cd biases
convert bias -out=../process
cd ../process
stack bias rej w 3 3 -nonorm -out=../masters/bias_stacked
cd ..
cd flats
convert flat -out=../process
cd ../process
calibrate flat -bias=../masters/bias_stacked
stack pp_flat rej w 3 3 -norm=mul -out=../masters/pp_flat_stacked
cd ..
cd darks
convert dark -out=../process
cd ../process
stack dark rej w 3 3 -nonorm -out=../masters/dark_stacked
cd ..
cd lights
convert light -out=../process
cd ../process
calibrate light -dark=../masters/dark_stacked -flat=../masters/pp_flat_stacked -cc=dark -cfa -equalize_cfa -debayer
register pp_light
stack r_pp_light rej w 3 3 -norm=addscale -output_norm -rgb_equal -filter-wfwhm=3k -filter-round=3k -out=result
load result
mirrorx -bottomup
savetif ../result
cd ..
close
"""

LIGHTS_ONLY_SCRIPT = """requires 1.4.0 1.5.0
cd lights
convert light -debayer -out=../process
cd ../process
register light
stack r_light rej w 3 3 -norm=addscale -output_norm -rgb_equal -filter-wfwhm=3k -filter-round=3k -out=result
load result
mirrorx -bottomup
savetif ../result
cd ..
close
"""


class AstroPreparationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.outputs = self.root / "Lumen Library ✨" / "outputs"
        self.outputs.mkdir(parents=True)
        self.night = self.root / "Night sky ✨ 2026"
        self.night.mkdir()
        self.lights = [self.frame(self.night / f"frame 0{index} é.ARW", f"light {index}") for index in range(1, 5)]

    def frame(self, path: Path, content: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode())
        return path

    def calibration(self, name: str, count: int) -> Path:
        folder = self.root / f"{name} frames ü"
        for index in range(count):
            self.frame(folder / f"{name} {index}.ARW", f"{name} {index}")
        return folder

    def prepare(self, **overrides):
        options = {"output_root": self.outputs, "job_id": 7, "lights": self.lights, "siril": SIRIL}
        return prepare_siril_job(**(options | overrides))

    def test_job_is_self_contained_under_managed_astro_directory(self):
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in self.lights]
        manifest = self.prepare()
        job = self.outputs / "astro" / "7"
        self.assertEqual(str(job), manifest.job_dir)
        self.assertEqual(
            [SIRIL.executable, "-d", str(job), "-i", str(job / "siril.config"), "-o", "-s", str(job / "stack.ssf")],
            list(manifest.command),
        )
        for path in job.rglob("*"):
            self.assertTrue(path.absolute().is_relative_to(job))
        self.assertTrue((job / "manifest.json").is_file())
        self.assertEqual("", (job / "siril.config").read_text())
        links = sorted((job / "lights").iterdir())
        self.assertEqual([f"light_0000{index}.arw" for index in range(1, 5)], [link.name for link in links])
        self.assertEqual(self.lights, [link.resolve() for link in links])
        self.assertEqual(before, [(path.read_bytes(), path.stat().st_mtime_ns) for path in self.lights])

    def test_manifest_records_source_fingerprints(self):
        manifest = self.prepare()
        for frame, source in zip(manifest.lights.frames, self.lights):
            self.assertEqual(str(source), frame.source)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), frame.fingerprint)
            self.assertEqual(source.stat().st_size, frame.size)
            self.assertEqual(source.stat().st_mtime_ns, frame.mtime_ns)
            self.assertEqual("light", frame.kind)

    def test_unicode_and_space_paths_round_trip_and_never_enter_the_script(self):
        manifest = self.prepare(darks=self.calibration("dark", 2))
        job = Path(manifest.job_dir)
        raw = (job / "manifest.json").read_bytes()
        self.assertIn("Night sky ✨ 2026/frame 01 é.ARW".encode(), raw)
        self.assertEqual(manifest, load_manifest(job / "manifest.json", self.outputs))
        script = (job / "stack.ssf").read_bytes()
        script.decode("ascii")
        for forbidden in (b"Night", b"Library", b"frames", b"/"):
            if forbidden == b"/":
                self.assertFalse(any(line.startswith(b"/") or b" /" in line for line in script.splitlines()))
            else:
                self.assertNotIn(forbidden, script)

    def test_excluded_lights_are_recorded_but_not_linked(self):
        manifest = self.prepare(excluded=[self.lights[1]])
        self.assertEqual([str(path) for path in (self.lights[0], *self.lights[2:])], [frame.source for frame in manifest.lights.frames])
        self.assertEqual((str(self.lights[1]),), manifest.excluded_lights)
        self.assertEqual(3, len(list((Path(manifest.job_dir) / "lights").iterdir())))

    def test_invalid_light_selection_is_rejected_without_leaving_a_job(self):
        for options in (
            {"excluded": [self.root / "elsewhere.ARW"]},
            {"excluded": self.lights[1:]},
            {"lights": [self.lights[0], self.lights[0]]},
            {"lights": [self.night]},
            {"lights": [self.lights[0], self.root / "missing.ARW"]},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.prepare(**options)
            self.assertFalse((self.outputs / "astro" / "7").exists())

    def test_optional_calibration_folders_use_only_supported_frames_in_name_order(self):
        darks = self.calibration("dark", 2)
        (darks / ".DS_Store").write_bytes(b"finder")
        (darks / "notes.txt").write_text("log")
        (darks / "nested").mkdir()
        self.frame(darks / "nested" / "deep.ARW", "nested")
        manifest = self.prepare(darks=darks)
        self.assertEqual(str(darks), manifest.darks.folder)
        self.assertEqual([str(darks / "dark 0.ARW"), str(darks / "dark 1.ARW")], [frame.source for frame in manifest.darks.frames])
        self.assertEqual(["darks/dark_00001.arw", "darks/dark_00002.arw"], [frame.link for frame in manifest.darks.frames])
        self.assertIsNone(manifest.flats.folder)
        self.assertEqual((), manifest.flats.frames)
        script = (Path(manifest.job_dir) / "stack.ssf").read_text()
        self.assertIn("calibrate light -dark=../masters/dark_stacked -cc=dark -cfa -debayer\n", script)
        self.assertNotIn("flat", script)

    def test_missing_or_empty_calibration_warns_without_blocking(self):
        empty = self.root / "empty flats"
        empty.mkdir()
        manifest = self.prepare(flats=empty)
        self.assertEqual(
            (
                "No dark frames: thermal noise and hot pixels will not be subtracted.",
                f"Flat folder {empty} contains no supported frames.",
                "No flat frames: vignetting and dust shadows will not be corrected.",
                "No bias frames: read-noise offset will not be subtracted.",
            ),
            manifest.warnings,
        )
        self.assertEqual(LIGHTS_ONLY_SCRIPT, (Path(manifest.job_dir) / "stack.ssf").read_text())
        with self.assertRaises(ValueError):
            self.prepare(job_id=8, darks=self.root / "no such folder")

    def test_single_frame_calibration_is_skipped_with_a_warning(self):
        # Siril 1.4.4 builds no sequence from one converted frame, so its master stack would fail the run.
        darks = self.calibration("dark", 1)
        manifest = self.prepare(darks=darks, flats=self.calibration("flat", 2), biases=self.calibration("bias", 2))
        self.assertEqual(
            (f"Dark folder {darks} has only one frame; at least two are needed to build a master.",
             "No dark frames: thermal noise and hot pixels will not be subtracted."),
            manifest.warnings,
        )
        self.assertEqual((), manifest.darks.frames)
        self.assertEqual(str(darks), manifest.darks.folder)
        self.assertFalse((Path(manifest.job_dir) / "darks").exists())
        self.assertNotIn("dark", (Path(manifest.job_dir) / "stack.ssf").read_text())

    def test_trailing_lights_warn_and_excluded_lights_do_not(self):
        metrics = {self.lights[2]: {"trailing_warning": True}, self.lights[3]: {"trailing_warning": True}, self.lights[0]: {"trailing_warning": False}}
        manifest = self.prepare(excluded=[self.lights[3]], astro_metrics=metrics, darks=self.calibration("dark", 2),
                                flats=self.calibration("flat", 2), biases=self.calibration("bias", 2))
        self.assertEqual(("Light frame 03 é.ARW shows star trailing.",), manifest.warnings)

    def test_full_calibration_generates_the_validated_script(self):
        manifest = self.prepare(darks=self.calibration("dark", 3), flats=self.calibration("flat", 3), biases=self.calibration("bias", 3))
        self.assertEqual(FULL_SCRIPT, (Path(manifest.job_dir) / "stack.ssf").read_text())
        self.assertEqual(hashlib.sha256(FULL_SCRIPT.encode()).hexdigest(), manifest.script_sha256)

    def test_bias_without_darks_calibrates_lights_and_flats_without_bias_stack_plainly(self):
        script = siril_script(SIRIL, darks=False, flats=False, biases=True)
        self.assertIn("calibrate light -bias=../masters/bias_stacked -debayer\nregister pp_light\n", script)
        script = siril_script(SIRIL, darks=True, flats=False, biases=True)
        self.assertIn("calibrate light -dark=../masters/dark_stacked -cc=dark -cfa -debayer\n", script)
        self.assertIn("stack bias rej w 3 3 -nonorm -out=../masters/bias_stacked\n", script)
        script = siril_script(SIRIL, darks=True, flats=True, biases=False)
        self.assertIn("stack flat rej w 3 3 -norm=mul -out=../masters/pp_flat_stacked\n", script)
        self.assertIn("calibrate light -dark=../masters/dark_stacked -flat=../masters/pp_flat_stacked -cc=dark -cfa -equalize_cfa -debayer\n", script)
        script = siril_script(SIRIL, darks=False, flats=True, biases=False)
        self.assertIn("convert flat -out=../process\ncd ../process\nstack flat rej w 3 3 -norm=mul", script)
        self.assertIn("calibrate light -flat=../masters/pp_flat_stacked -equalize_cfa -debayer\n", script)

    def test_script_generation_is_version_gated(self):
        for version in ((1, 3, 6), (1, 5, 0), (2, 0, 0)):
            with self.subTest(version=version), self.assertRaises(UnsupportedSirilVersion):
                siril_script(SirilCapabilities(SIRIL.executable, version), darks=True, flats=True, biases=True)
        self.assertTrue(siril_script(SirilCapabilities(SIRIL.executable, (1, 4, 0)), darks=False, flats=False, biases=False).startswith("requires 1.4.0 1.5.0\n"))
        with self.assertRaises(UnsupportedSirilVersion):
            self.prepare(siril=SirilCapabilities(SIRIL.executable, (1, 2, 6)))
        self.assertFalse((self.outputs / "astro" / "7").exists())

    def test_metrics_for_missing_unselected_photos_do_not_block(self):
        metrics = {self.root / "deleted elsewhere.ARW": {"trailing_warning": True}, self.lights[0]: {"trailing_warning": True}}
        manifest = self.prepare(astro_metrics=metrics, darks=self.calibration("dark", 2), flats=self.calibration("flat", 2), biases=self.calibration("bias", 2))
        self.assertEqual(("Light frame 01 é.ARW shows star trailing.",), manifest.warnings)

    def test_excluded_lights_cannot_be_reused_as_calibration(self):
        folder = self.root / "mixed"
        folder.mkdir()
        lights = [self.frame(folder / f"frame {index}.ARW", f"mixed {index}") for index in range(3)]
        self.frame(folder / "other.ARW", "other")
        with self.assertRaises(ValueError):
            self.prepare(lights=[*self.lights[:2], *lights], excluded=lights, darks=folder)
        self.assertFalse((self.outputs / "astro" / "7").exists())

    def test_unsupported_light_types_are_rejected(self):
        for name in ("preview.jpg", "no suffix"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.prepare(lights=[*self.lights[:2], self.frame(self.night / name, "x")])

    def test_source_changing_while_fingerprinted_is_rejected(self):
        def touch_then_hash(path):
            path.write_bytes(path.read_bytes() + b"!")
            return "0" * 64
        with patch("lumen.astro.fingerprint_file", side_effect=touch_then_hash), self.assertRaisesRegex(ValueError, "changed"):
            self.prepare()
        self.assertFalse((self.outputs / "astro" / "7").exists())

    def test_failure_mid_preparation_removes_only_the_new_job(self):
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in self.lights]
        real_symlink = __import__("os").symlink
        calls = []
        def flaky(source, link):
            calls.append(link)
            if len(calls) == 3:
                raise OSError("disk full")
            real_symlink(source, link)
        with patch("lumen.astro.os.symlink", side_effect=flaky), self.assertRaises(OSError):
            self.prepare()
        self.assertFalse((self.outputs / "astro" / "7").exists())
        self.assertTrue((self.outputs / "astro").is_dir())
        self.assertEqual(before, [(path.read_bytes(), path.stat().st_mtime_ns) for path in self.lights])

    def test_load_manifest_rejects_tampered_jobs(self):
        manifest = self.prepare(darks=self.calibration("dark", 2))
        job = Path(manifest.job_dir)
        original = (job / "manifest.json").read_text(encoding="utf-8")
        payload = json.loads(original)
        tampered = {
            "script hash": lambda data: data | {"script_sha256": "0" * 64},
            "foreign job dir": lambda data: data | {"job_dir": str(self.root)},
            "escaping link": lambda data: data | {"lights": data["lights"] | {"frames": [data["lights"]["frames"][0] | {"link": "../../../evil.arw"}]}},
            "foreign command": lambda data: data | {"command": ["/bin/sh", "-c", "true"]},
            "duplicate link": lambda data: data | {"lights": data["lights"] | {"frames": [data["lights"]["frames"][0]] * 2}},
            "relative source": lambda data: data | {"lights": data["lights"] | {"frames": [data["lights"]["frames"][0] | {"source": "frame.ARW"}]}},
            "text size": lambda data: data | {"lights": data["lights"] | {"frames": [data["lights"]["frames"][0] | {"size": "7"}]}},
            "numeric source": lambda data: data | {"lights": data["lights"] | {"frames": [data["lights"]["frames"][0] | {"source": 7}]}},
            "numeric link": lambda data: data | {"lights": data["lights"] | {"frames": [data["lights"]["frames"][0] | {"link": 7}]}},
            "text job id": lambda data: data | {"job_id": "7"},
        }
        (job / "manifest.json").chmod(0o644)
        for name, change in tampered.items():
            with self.subTest(name=name):
                (job / "manifest.json").write_text(json.dumps(change(json.loads(original))), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_manifest(job / "manifest.json", self.outputs)
        (job / "manifest.json").write_text(original, encoding="utf-8")
        self.assertEqual(manifest, load_manifest(job / "manifest.json", self.outputs))
        (job / "stack.ssf").chmod(0o644)
        (job / "stack.ssf").write_text("requires 1.4.0 1.5.0\nshell rm -rf ~\n")
        with self.assertRaises(ValueError):
            load_manifest(job / "manifest.json", self.outputs)

    def test_load_manifest_requires_the_configured_library(self):
        manifest = self.prepare()
        other = self.root / "Other Library" / "outputs"
        with self.assertRaises(ValueError):
            load_manifest(Path(manifest.job_dir) / "manifest.json", other)
        copied = other / "astro" / "7"
        copied.parent.mkdir(parents=True)
        __import__("shutil").copytree(manifest.job_dir, copied, symlinks=True)
        with self.assertRaises(ValueError):
            load_manifest(copied / "manifest.json", self.outputs)

    def test_prepared_jobs_are_immutable(self):
        manifest = self.prepare()
        job = Path(manifest.job_dir)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            manifest.warnings = ()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            manifest.lights.frames[0].fingerprint = "0" * 64
        self.assertIsInstance(manifest.lights.frames, tuple)
        for name in ("manifest.json", "stack.ssf"):
            self.assertEqual(0, (job / name).stat().st_mode & 0o222)
        with self.assertRaises(FileExistsError):
            self.prepare()
        self.assertEqual(manifest, load_manifest(job / "manifest.json", self.outputs))

    def test_job_paths_cannot_escape_managed_outputs(self):
        for job_id in (0, -1, True, "7", 7.0):
            with self.subTest(job_id=job_id), self.assertRaises(ValueError):
                self.prepare(job_id=job_id)
        outside = self.root / "outside"
        outside.mkdir()
        (self.outputs / "astro").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual([], list(outside.iterdir()))

    def test_manifest_json_is_versioned_and_complete(self):
        manifest = self.prepare(darks=self.calibration("dark", 2))
        payload = json.loads((Path(manifest.job_dir) / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(1, payload["schema"])
        self.assertEqual(7, payload["job_id"])
        self.assertEqual({"executable": SIRIL.executable, "version": "1.4.4"}, payload["siril"])
        self.assertEqual(("stack.ssf", "siril.log", "result.tif"), (payload["script"], payload["log"], payload["result"]))
        self.assertEqual(["lights", "darks", "flats", "biases"], [key for key in payload if key in {"lights", "darks", "flats", "biases"}])


class SirilProbeTests(unittest.TestCase):
    def test_probe_parses_macos_banner_with_argument_array(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "Siril is started as macOS application\nsiril 1.4.4\n", "")

        capabilities = probe_siril("/Apps/My Siril ✨/siril-cli", runner=runner)
        self.assertEqual([["/Apps/My Siril ✨/siril-cli", "--version"]], calls)
        self.assertEqual((1, 4, 4), capabilities.version)
        self.assertEqual("1.4.4", capabilities.version_text)
        self.assertTrue(capabilities.supported)

    def test_probe_rejects_unrecognized_or_failed_output(self):
        for completed in (
            subprocess.CompletedProcess([], 0, "Siril is started as macOS application\n", ""),
            subprocess.CompletedProcess([], 1, "siril 1.4.4\n", ""),
        ):
            with self.subTest(completed=completed), self.assertRaises(RuntimeError):
                probe_siril("siril-cli", runner=lambda command, **kwargs: completed)

    def test_doctor_reports_parsed_siril_version_and_support(self):
        from lumen.config import Settings
        from lumen.diagnostics import doctor_report
        from lumen.tools import ToolStatus

        def status(name, arguments):
            return ToolStatus(name, name == "siril-cli", "/Apps/siril-cli" if name == "siril-cli" else None,
                              "Siril is started as macOS application" if name == "siril-cli" else None)

        with tempfile.TemporaryDirectory() as directory, patch("lumen.diagnostics.tool_status", side_effect=status), patch(
            "lumen.diagnostics.probe_siril", return_value=SirilCapabilities("/Apps/siril-cli", (1, 4, 4))
        ), patch("lumen.diagnostics.get_server_status", side_effect=ValueError("offline")):
            report = doctor_report(Settings(library_dir=Path(directory)))
        self.assertEqual("1.4.4", report["tools"]["siril-cli"]["version"])
        self.assertTrue(report["tools"]["siril-cli"]["supported"])
        with tempfile.TemporaryDirectory() as directory, patch("lumen.diagnostics.tool_status", side_effect=status), patch(
            "lumen.diagnostics.probe_siril", side_effect=RuntimeError("no version")
        ), patch("lumen.diagnostics.get_server_status", side_effect=ValueError("offline")):
            report = doctor_report(Settings(library_dir=Path(directory)))
        self.assertFalse(report["tools"]["siril-cli"]["supported"])


if __name__ == "__main__":
    unittest.main()
