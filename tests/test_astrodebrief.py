import json
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lumen.astrodebrief import (output_stem, parse_siril_log, read_fits_header, remove_process, rename_outputs,
                                write_frames_json)

FIXTURES = Path(__file__).parent / "fixtures" / "siril"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class ParseSirilLogTests(unittest.TestCase):
    def test_parses_tracked_all_skipped(self) -> None:
        run = parse_siril_log(fixture("tracked-skipped.log"))
        self.assertEqual((run.recipe, run.reference, run.reference_stars), ("tracked", 1, 26))
        self.assertEqual(run.reference_fwhm_xy, (13.40, 9.72))
        self.assertEqual(run.skipped, ((2, 7), (3, 7), (4, 6)))
        self.assertTrue(run.aborted)
        self.assertFalse(run.succeeded)
        self.assertEqual(sorted(run.star_counts), [26, 59, 62, 69])  # reference included
        self.assertEqual(run.matches, ())
        self.assertIsNone(run.stacked)

    def test_parses_tracked_success(self) -> None:
        run = parse_siril_log(fixture("tracked-ok.log"))
        self.assertEqual((run.registered, run.failed, run.stacked, run.succeeded), (4, 0, 4, True))
        self.assertEqual([m.image for m in run.matches], [2, 3, 4])
        first = run.matches[0]
        self.assertEqual(
            (first.pairs, first.rotation, first.dx, first.dy, first.fwhm, first.roundness),
            (33, -0.117, -16.72, -7.30, 13.56, 0.73),
        )
        self.assertEqual(run.reference_fwhm_xy, (14.06, 10.38))
        self.assertEqual(run.skipped, ())
        self.assertFalse(run.aborted)

    def test_parses_untracked_reference_choice(self) -> None:
        run = parse_siril_log(fixture("untracked-ok.log"))
        self.assertEqual((run.recipe, run.reference, run.registered, run.failed), ("untracked", 1, 3, 0))
        self.assertIsNone(run.reference_fwhm_xy)
        self.assertEqual({m.image for m in run.matches}, {2, 3})
        self.assertEqual(sorted(run.star_counts), [59, 62, 69])
        self.assertEqual(run.stacked, 3)
        self.assertIs(run.succeeded, True)

    def test_ignores_ansi_and_garbage(self) -> None:
        run = parse_siril_log("\x1b[31mlog: x\x1b[0m\nnonsense")
        self.assertEqual(run.recipe, "")
        for field in ("reference", "reference_stars", "reference_fwhm_xy", "registered", "failed", "stacked"):
            self.assertIsNone(getattr(run, field), field)
        self.assertEqual((run.star_counts, run.matches, run.skipped), ((), (), ()))
        self.assertFalse(run.aborted or run.succeeded)


def fits_header(path: Path, **cards: str) -> None:
    """A minimal FITS header: 80-character cards, END, padded to one 2880-byte block, then data bytes."""
    lines = [f"{key:<8}= {value:>20}" for key, value in cards.items()] + ["END"]
    path.write_bytes("".join(line.ljust(80) for line in lines).ljust(2880).encode("ascii") + b"\xff" * 100)


def light_cards(exposure: str = "20.", **extra: str) -> dict:
    return {"SIMPLE": "T", "DATE-OBS": "'2026-10-19T00:29:40'", "EXPTIME": exposure, "APERTURE": "4.",
            "ISOSPEED": "3200.", "FOCALLEN": "28.", "XPIXSZ": "5.07000017166138",
            "INSTRUME": "'Sony ILCE-7CM2'", **extra}


def staged_job(root: Path, count: int) -> tuple[Path, SimpleNamespace]:
    job = root / "job"
    (job / "lights").mkdir(parents=True)
    frames = []
    for index in range(1, count + 1):
        link = f"lights/light_{index:05d}.arw"
        (job / link).write_bytes(b"raw")
        frames.append(SimpleNamespace(link=link))
    return job, SimpleNamespace(job_dir=str(job), lights=SimpleNamespace(frames=tuple(frames)))


class FitsHeaderTests(unittest.TestCase):
    def test_read_fits_header(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "light_00001.fit"
            fits_header(path, **light_cards(), NAXIS="3", FILENAME="'it''s.arw'")
            header = read_fits_header(path)
        self.assertEqual(header["EXPTIME"], 20.0)
        self.assertEqual(header["INSTRUME"], "Sony ILCE-7CM2")
        self.assertEqual(header["XPIXSZ"], 5.07000017166138)
        self.assertEqual(header["NAXIS"], 3)
        self.assertIs(header["SIMPLE"], True)
        self.assertEqual(header["FILENAME"], "it's.arw")
        self.assertNotIn("END", header)


class FramesJsonTests(unittest.TestCase):
    def test_write_frames_json_reads_headers_and_exif(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job, manifest = staged_job(Path(directory), 3)
            (job / "process").mkdir()
            fits_header(job / "process" / "light_00001.fit", **light_cards("20."))
            fits_header(job / "process" / "light_00002.fit", **light_cards("13."))
            calls = []

            def runner(command, **kwargs):
                calls.append((command, kwargs))
                rows = [{"SourceFile": str(job / frame.link), "ImageStabilization": 1, "MaxApertureValue": 4}
                        for frame in manifest.lights.frames]
                return subprocess.CompletedProcess(command, 0, json.dumps(rows), "")

            with patch("lumen.astrodebrief.find_tool", return_value="/bin/exiftool"):
                record = write_frames_json(job, manifest, runner=runner)
            stored = json.loads((job / "frames.json").read_text(encoding="utf-8"))
        self.assertEqual(record, stored)
        self.assertTrue(record["originals_online"])
        self.assertEqual(record["frames"][0], {
            "position": 1, "name": "light_00001.arw", "date_obs": "2026-10-19T00:29:40", "exposure": 20.0,
            "iso": 3200, "aperture": 4.0, "focal": 28.0, "pixel_um": 5.07, "instrument": "Sony ILCE-7CM2",
            "stabilization": "On", "max_aperture": 4.0})
        self.assertEqual([(2, 13.0)], [(frame["position"], frame["exposure"]) for frame in record["frames"][1:]])
        [(command, kwargs)] = calls
        self.assertEqual(["/bin/exiftool", "-json", "-n", "-ImageStabilization", "-MaxApertureValue",
                          *(str(job / frame.link) for frame in manifest.lights.frames)], command)
        self.assertEqual(60, kwargs["timeout"])

    def test_write_frames_json_marks_originals_offline(self) -> None:
        def failing(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, "", "File not found")

        for name, tool, runner in (("runner fails", "/bin/exiftool", failing), ("no exiftool", None, failing)):
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                job, manifest = staged_job(Path(directory), 1)
                (job / "process").mkdir()
                fits_header(job / "process" / "light_00001.fit", **light_cards())
                with patch("lumen.astrodebrief.find_tool", return_value=tool):
                    record = write_frames_json(job, manifest, runner=runner)
                self.assertFalse(record["originals_online"])
                [frame] = record["frames"]
                self.assertEqual((None, None), (frame["stabilization"], frame["max_aperture"]))
                self.assertEqual(20.0, frame["exposure"])


class OutputNameTests(unittest.TestCase):
    captured = datetime(2026, 10, 18, 21, 29, 40)

    @staticmethod
    def frames(*exposures: float, **fields) -> list[dict]:
        base = {"exposure": None, "iso": 3200, "aperture": 4.0, "focal": 28.0, "instrument": "Sony ILCE-7CM2"}
        return [base | fields | {"exposure": exposure} for exposure in exposures]

    def test_output_stem(self) -> None:
        self.assertEqual("2026-10-18_2129_ILCE-7CM2_28mm_f4_4x13-20s_ISO3200",
                         output_stem(self.frames(20.0, 13.0, 15.0, 15.0), self.captured))
        self.assertEqual("2026-10-18_2129_ILCE-7CM2_28mm_f4_4x15s_ISO3200",
                         output_stem(self.frames(15.0, 15.0, 15.0, 15.0), self.captured))
        self.assertEqual("2026-10-18_2129_ILCE-7CM2_28mm_f2.8_1x15s_ISO3200",
                         output_stem(self.frames(15.0, aperture=2.8), self.captured))
        self.assertEqual("ILCE-7CM2_28mm_f4_1x15s_ISO3200", output_stem(self.frames(15.0), None))
        self.assertEqual("2026-10-18_2129_ILCE-7CM2_28mm_f4_3x13-20s_ISO3200",
                         output_stem(self.frames(20.0, 13.0, 15.0, 15.0), self.captured, count=3))
        self.assertEqual("2026-10-18_2129_ILCE-7CM2x_28mm_f4_1x15s_ISO3200",
                         output_stem(self.frames(15.0, instrument="Sony ILCE-7CM2/x"), self.captured))

    def test_output_stem_drops_missing_fields(self) -> None:
        self.assertEqual("2026-10-18_2129_28mm_f4_4x15s",
                         output_stem(self.frames(15.0, 15.0, 15.0, 15.0, iso=None, instrument=None), self.captured))
        self.assertIsNone(output_stem([], self.captured))

    def test_rename_refuses_an_existing_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory)
            for name in ("result.tif", "preview.jpg", "stem.tif"):
                (job / name).write_bytes(name.encode())
            with self.assertRaises(ValueError):
                rename_outputs(job, "stem")
            self.assertEqual({"result.tif", "preview.jpg", "stem.tif"}, {path.name for path in job.iterdir()})
            self.assertEqual(b"stem.tif", (job / "stem.tif").read_bytes())

    def test_rename_moves_the_tiff_and_the_preview(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory)
            (job / "result.tif").write_bytes(b"t")
            self.assertEqual((job / "a.tif", None), rename_outputs(job, "a"))
            (job / "result.tif").write_bytes(b"t")
            (job / "preview.jpg").write_bytes(b"j")
            self.assertEqual((job / "b.tif", job / "b.jpg"), rename_outputs(job, "b"))
            self.assertEqual({"a.tif", "b.tif", "b.jpg"}, {path.name for path in job.iterdir()})


class RemoveProcessTests(unittest.TestCase):
    def test_remove_process_refuses_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as elsewhere:
            job = Path(directory)
            (Path(elsewhere) / "keep.fit").write_bytes(b"x")
            (job / "process").symlink_to(elsewhere)
            line = remove_process(job)
            self.assertIn("refused", line)
            self.assertTrue((Path(elsewhere) / "keep.fit").is_file())
            self.assertTrue((job / "process").is_symlink())

    def test_remove_process_deletes_only_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory)
            for folder in ("process", "masters", "lights"):
                (job / folder).mkdir()
                (job / folder / "a.fit").write_bytes(b"x")
            (job / "frames.json").write_text("{}")
            self.assertIn("removed", remove_process(job))
            self.assertEqual({"masters", "lights", "frames.json"}, {path.name for path in job.iterdir()})
            self.assertIn("no process", remove_process(job))


if __name__ == "__main__":
    unittest.main()
