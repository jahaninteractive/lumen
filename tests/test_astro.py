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
                f"Flat folder {empty.name} contains no supported frames.",
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
            (f"Dark folder {darks.name} has only one frame; at least two are needed to build a master.",
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


FAKE_SIRIL = """
import sys, time
from pathlib import Path
from PIL import Image
scenario = sys.argv[1]
print("log: Welcome to siril 1.4.4 for macos (aarch64) - CLI", flush=True)
print("log: Running command: calibrate", flush=True)
print("log: Running command: register", flush=True)
if scenario == "hang":
    Image.new("I;16", (8, 6)).save("result.tif")
    time.sleep(60)
print("log: Running command: savetif", flush=True)
if scenario == "success":
    Image.new("I;16", (8, 6)).save("result.tif")
elif scenario == "eight_bit":
    Image.new("RGB", (8, 6)).save("result.tif")
elif scenario == "nonzero":
    print("log: Error in line 5 ('stack'): invalid arguments.", flush=True)
    sys.exit(3)
print("log: Script execution finished successfully.", flush=True)
"""


class AstroJobTests(unittest.TestCase):
    def setUp(self):
        from PIL import Image
        from lumen.config import Settings
        from lumen.library import Library

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.night = self.root / "Night ✨"
        self.night.mkdir()
        for index in range(3):
            exif = Image.Exif()
            exif[34665] = {36867: f"2026:10:21 22:00:0{index}"}
            Image.new("RGB", (64, 48), (index, 0, 0)).save(self.night / f"light {index} é.tif", exif=exif.tobytes())
        self.lights = sorted(self.night.iterdir())
        self.settings = Settings(library_dir=self.root / "Lumen Library ✨")
        self.library = Library(self.settings)
        self.addCleanup(self.library.close)
        self.library.import_path(self.night)
        self.group = self.library.list_groups()[0]
        self.photo_ids = [photo["id"] for photo in self.group["photos"]]
        self.launches = []
        self.scenario = "success"
        self.real_popen = subprocess.Popen  # patching lumen.astro.subprocess.Popen patches the global name too
        patcher = patch("lumen.library.find_tool", return_value=SIRIL.executable)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("lumen.library.probe_siril", return_value=SIRIL)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("lumen.astro.subprocess.Popen", side_effect=self.fake_popen)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_popen(self, command, **kwargs):
        import sys
        self.launches.append(list(command))
        return self.real_popen([sys.executable, "-c", FAKE_SIRIL, self.scenario], **kwargs)

    def snapshot(self):
        return [(path.read_bytes(), path.stat().st_mtime_ns) for path in self.lights]

    def prepare(self, **overrides):
        options = {"confirmed": True}
        return self.library.prepare_astro_job(self.group["id"], **(options | overrides))

    def run_and_wait(self, job_id, **options):
        self.library.run_astro_job(job_id, **options)
        self.assertTrue(self.library.wait_for_job(job_id, timeout=10))
        return self.library.catalog.get_job(job_id)

    def test_prepare_creates_queued_job_whose_directory_matches_its_id(self):
        job = self.prepare(excluded_photo_ids=[self.photo_ids[2]])
        job_dir = self.settings.output_dir / "astro" / str(job.id)
        self.assertEqual(("astro", "queued", 0.0), (job.kind, job.status, job.progress))
        self.assertEqual(str(job_dir / "manifest.json"), job.manifest_path)
        self.assertEqual(str(job_dir / "siril.log"), job.log_path)
        manifest = load_manifest(job_dir / "manifest.json", self.settings.output_dir)
        self.assertEqual([str(path) for path in self.lights[:2]], [frame.source for frame in manifest.lights.frames])
        self.assertEqual((str(self.lights[2]),), manifest.excluded_lights)
        self.assertEqual([job.id], [item.id for item in self.library.catalog.list_jobs()])

    def test_prepare_requires_confirmation_and_valid_selection(self):
        for options in ({"confirmed": False}, {"excluded_photo_ids": [999]}, {"excluded_photo_ids": self.photo_ids[:2]}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.prepare(**options)
        with self.assertRaises(ValueError):
            self.library.prepare_astro_job(999, confirmed=True)
        self.assertEqual([], self.library.catalog.list_jobs())
        self.assertFalse((self.settings.output_dir / "astro").exists() and any((self.settings.output_dir / "astro").iterdir()))

    def test_cancel_during_preparation_leaves_no_prepared_job(self):
        from lumen import library as library_module

        def cancel_midway(**options):
            manifest = prepare_siril_job(**options)
            self.library.catalog.update_job(manifest.job_id, status="cancelled")
            return manifest

        with patch.object(library_module, "prepare_siril_job", side_effect=cancel_midway), self.assertRaisesRegex(ValueError, "cancelled during preparation"):
            self.prepare()
        jobs = self.library.catalog.list_jobs()
        self.assertEqual(["cancelled"], [job.status for job in jobs])
        self.assertEqual("pending", jobs[0].manifest_path)

    def test_prepare_refuses_lights_changed_since_import(self):
        self.lights[0].write_bytes(self.lights[0].read_bytes() + b"!")
        with self.assertRaisesRegex(ValueError, "since import"):
            self.prepare()
        self.assertEqual([], self.library.catalog.list_jobs())

    def test_failed_preparation_leaves_no_job_row_or_directory(self):
        with patch("lumen.library.probe_siril", return_value=SirilCapabilities(SIRIL.executable, (1, 2, 6))):
            with self.assertRaises(UnsupportedSirilVersion):
                self.prepare()
        with patch("lumen.library.find_tool", return_value=None), self.assertRaisesRegex(RuntimeError, "siril-cli"):
            self.prepare()
        self.assertEqual([], self.library.catalog.list_jobs())
        self.assertFalse(any((self.settings.output_dir / "astro").glob("*")))

    def test_successful_run_streams_log_and_catalogs_tiff_with_provenance(self):
        before = self.snapshot()
        job = self.prepare(excluded_photo_ids=[self.photo_ids[2]])
        finished = self.run_and_wait(job.id)
        self.assertEqual(("succeeded", 1.0, None), (finished.status, finished.progress, finished.error))
        result = Path(job.manifest_path).parent / "result.tif"
        self.assertEqual(str(result), finished.result_path)
        self.assertEqual(1, len(self.launches))
        self.assertEqual(load_manifest(Path(job.manifest_path), self.settings.output_dir).command, tuple(self.launches[0]))
        log = Path(job.log_path).read_text(encoding="utf-8")
        self.assertIn("log: Running command: register", log)
        self.assertIn("Script execution finished successfully", log)
        artifacts = self.library.catalog.list_artifacts()
        self.assertEqual(1, len(artifacts))
        artifact = artifacts[0]
        self.assertEqual(("astro_tiff", None, str(result)), (artifact.kind, artifact.photo_id, artifact.path))
        self.assertEqual(hashlib.sha256(result.read_bytes()).hexdigest(), artifact.output_fingerprint)
        self.assertEqual(job.id, artifact.settings["job_id"])
        self.assertEqual(self.photo_ids[:2], artifact.settings["light_photo_ids"])
        self.assertEqual("1.4.4", artifact.settings["siril_version"])
        self.assertEqual(0, artifact.settings["exit_code"])
        self.assertEqual((16,), tuple(artifact.settings["bits_per_sample"]))
        for photo_id in self.photo_ids[:2]:
            events = [event for event in self.library.catalog.events_for(photo_id) if event.kind == "astro_stacked"]
            self.assertEqual(1, len(events))
            self.assertEqual(artifact.id, events[0].details["artifact_id"])
            sidecar = json.loads(self.library.sidecars.paths(self.library.catalog.get_photo(photo_id))[0].read_text())
            self.assertIn("astro_stacked", [event["kind"] for event in sidecar["provenance"]])
        self.assertEqual([], [event for event in self.library.catalog.events_for(self.photo_ids[2]) if event.kind == "astro_stacked"])
        self.assertEqual(1, len([event for event in self.library.catalog.events_for(None) if event.kind == "astro_stacked"]))
        self.assertEqual(before, self.snapshot())

    def test_changed_or_missing_frames_block_launch(self):
        original = self.lights[1].read_bytes()
        for name, damage in (("changed", lambda path: path.write_bytes(original + b"!")), ("missing", lambda path: path.unlink())):
            with self.subTest(name=name):
                job = self.prepare()
                damage(self.lights[1])
                finished = self.run_and_wait(job.id)
                self.assertEqual("failed", finished.status)
                self.assertIn(name, finished.error)
                self.assertEqual([], self.launches)
                self.assertEqual([], self.library.catalog.list_artifacts())
                self.assertIn(name, Path(job.log_path).read_text(encoding="utf-8"))
                self.lights[1].write_bytes(original)

    def test_bookkeeping_failure_after_a_good_stack_leaves_no_artifact_or_events(self):
        job = self.prepare()
        with patch.object(self.library.sidecars, "write", side_effect=OSError("disk full")):
            finished = self.run_and_wait(job.id)
        self.assertEqual("failed", finished.status)
        self.assertIn("disk full", finished.error)
        self.assertIsNone(finished.result_path)
        self.assertTrue((Path(job.manifest_path).parent / "result.tif").is_file())
        self.assertEqual([], self.library.catalog.list_artifacts())
        for photo_id in self.photo_ids:
            self.assertEqual([], [event for event in self.library.catalog.events_for(photo_id) if event.kind == "astro_stacked"])
            sidecar = json.loads(self.library.sidecars.paths(self.library.catalog.get_photo(photo_id))[0].read_text())
            self.assertNotIn("astro_stacked", [event["kind"] for event in sidecar["provenance"]])
        self.assertEqual([], [event for event in self.library.catalog.events_for(None) if event.kind == "astro_stacked"])

    def test_existing_outputs_block_launch(self):
        job = self.prepare()
        (Path(job.manifest_path).parent / "result.tif").write_bytes(b"stale")
        finished = self.run_and_wait(job.id)
        self.assertEqual("failed", finished.status)
        self.assertIn("already contains", finished.error)
        self.assertEqual([], self.launches)

    def test_timeout_fails_the_job_and_reaps_the_process(self):
        self.scenario = "hang"
        before = self.snapshot()
        job = self.prepare()
        finished = self.run_and_wait(job.id, timeout_seconds=0.5)
        self.assertEqual(before, self.snapshot())
        self.assertEqual("failed", finished.status)
        self.assertIn("timed out", finished.error)
        self.assertIsNone(finished.result_path)
        self.assertEqual([], self.library.catalog.list_artifacts())

    def test_nonzero_exit_fails_without_artifact(self):
        self.scenario = "nonzero"
        before = self.snapshot()
        job = self.prepare()
        finished = self.run_and_wait(job.id)
        self.assertEqual(before, self.snapshot())
        self.assertEqual("failed", finished.status)
        self.assertIn("exit code 3", finished.error)
        self.assertIn("invalid arguments", Path(job.log_path).read_text(encoding="utf-8"))
        self.assertEqual([], self.library.catalog.list_artifacts())

    def test_zero_exit_without_declared_tiff_or_with_wrong_depth_fails(self):
        for scenario, message in (("no_tiff", "did not produce"), ("eight_bit", "16-bit")):
            with self.subTest(scenario=scenario):
                self.scenario = scenario
                job = self.prepare()
                finished = self.run_and_wait(job.id)
                self.assertEqual("failed", finished.status)
                self.assertIn(message, finished.error)
                self.assertIsNone(finished.result_path)
        self.assertEqual([], self.library.catalog.list_artifacts())

    def test_cancellation_stops_only_the_owned_process_and_never_succeeds(self):
        import time
        self.scenario = "hang"
        job = self.prepare()
        self.library.run_astro_job(job.id)
        deadline = time.monotonic() + 10
        log_path = Path(job.log_path)
        while not log_path.exists() or "Running command: register" not in log_path.read_text(encoding="utf-8", errors="replace"):
            self.assertLess(time.monotonic(), deadline, "fake Siril never started")
            time.sleep(0.05)
        from lumen.library import Library
        with Library(self.settings) as other:  # Another Library in this process does not own the runner.
            with self.assertRaisesRegex(RuntimeError, "another job runner"):
                other.cancel_job(job.id)
        before = self.snapshot()
        self.assertTrue(self.library.cancel_job(job.id))
        self.assertTrue(self.library.wait_for_job(job.id, timeout=10))
        finished = self.library.catalog.get_job(job.id)
        self.assertEqual(("cancelled", None, None), (finished.status, finished.result_path, finished.error))
        self.assertEqual(before, self.snapshot())
        self.assertEqual([], self.library.catalog.list_artifacts())
        self.assertIn("Running command: register", Path(job.log_path).read_text(encoding="utf-8"))
        self.assertTrue(Path(job.manifest_path).is_file())
        with self.assertRaises(ValueError):
            self.library.run_astro_job(job.id)

    def test_queued_job_can_be_cancelled_without_starting_a_runner(self):
        import threading
        job = self.prepare()
        self.assertTrue(self.library.cancel_job(job.id))
        self.assertEqual("cancelled", self.library.catalog.get_job(job.id).status)
        self.assertNotIn("lumen-local-job-runner", [thread.name for thread in threading.enumerate()])
        self.assertTrue(self.library.cancel_job(job.id))
        with self.assertRaises(ValueError):
            self.library.cancel_job(job.id + 100)

    def test_run_rejects_non_astro_or_tampered_jobs(self):
        job = self.prepare()
        (Path(job.manifest_path).parent / "stack.ssf").chmod(0o644)
        (Path(job.manifest_path).parent / "stack.ssf").write_text("requires 1.4.0 1.5.0\nclose\n")
        with self.assertRaises(ValueError):
            self.library.run_astro_job(job.id)
        self.assertEqual("queued", self.library.catalog.get_job(job.id).status)
        other = self.library.catalog.create_job(kind="export", manifest_path="/x/m.json", log_path="/x/l.log")
        with self.assertRaises(ValueError):
            self.library.run_astro_job(other.id)
        self.assertEqual([], self.launches)


class StageProgressTests(unittest.TestCase):
    def test_coarse_stages_are_monotonic_and_ignore_noise(self):
        from lumen.astro import stage_progress
        self.assertIsNone(stage_progress("log: Reading FITS: file light_00001.fit"))
        self.assertIsNone(stage_progress("progress: Rejection stacking in progress..., 0.00%"))
        stages = [stage_progress(f"log: Running command: {command}") for command in ("cd", "convert", "calibrate", "register", "stack", "savetif")]
        self.assertEqual([None, None, 0.3, 0.5, None, 0.9], stages)


class ExecuteJobCancellationTests(unittest.TestCase):
    def test_cancellation_after_a_clean_exit_still_records_no_result(self):
        import sys
        from lumen.astro import execute_job

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            night = root / "night"
            night.mkdir()
            lights = [(night / f"l{index}.tif", (night / f"l{index}.tif").write_bytes(b"light"))[0] for index in range(2)]
            outputs = root / "outputs"
            outputs.mkdir()
            manifest = prepare_siril_job(output_root=outputs, job_id=3, lights=lights, siril=SIRIL)
            real_popen = subprocess.Popen

            class Context:
                # Cancellation arrives while the process is still running, but the process then exits 0 with a result.
                cancelled = False

                def wait_for_cancel(self, timeout):
                    self.cancelled = True
                    return True

                def update_progress(self, progress):
                    pass

                def register_process(self, process):
                    pass

                def clear_process(self, process):
                    pass

            def popen(command, **kwargs):
                return real_popen([sys.executable, "-c", FAKE_SIRIL, "success"], **kwargs)

            execution = execute_job(Context(), manifest, output_root=outputs, popen=popen)
            self.assertIsNone(execution)
            self.assertTrue((Path(manifest.job_dir) / "result.tif").is_file())
            self.assertIn("cancelled by the photographer", (Path(manifest.job_dir) / "siril.log").read_text())
