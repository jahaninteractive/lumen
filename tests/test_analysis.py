from array import array
import json
import math
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from lumen.analysis import (
    SubjectRegions,
    analyze_astro,
    analyze_faces,
    analyze_subject_regions,
    apply_vision_result,
    difference_hash,
    feature_prints,
    hamming,
    measure_image,
    parse_feature_print,
    parse_vision_result,
    parse_vision_regions,
    straighten_from_horizon,
)


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _save(self, name: str, image: Image.Image) -> Path:
        path = self.root / name
        image.save(path, format="JPEG", quality=100)
        return path

    def test_flat_frame_hashes_to_zero(self):
        self.assertEqual("0000000000000000", difference_hash(self._save("flat.jpg", Image.new("L", (128, 128), 128))))

    def test_bright_to_dark_gradient_sets_every_bit(self):
        gradient = Image.new("L", (256, 64))
        gradient.putdata([255 - x for _ in range(64) for x in range(256)])
        self.assertEqual("ffffffffffffffff", difference_hash(self._save("gradient.jpg", gradient)))

    def test_hamming_counts_differing_bits(self):
        self.assertEqual(8, hamming("00ff", "0f0f"))
        self.assertEqual(0, hamming("abcd", "abcd"))

    def test_measure_image_records_difference_hash(self):
        dhash = measure_image(self._save("flat.jpg", Image.new("L", (128, 128), 128))).dhash
        self.assertRegex(dhash, r"^[0-9a-f]{16}$")

    def test_checkerboard_is_sharper_than_flat_frame(self):
        flat = Image.new("L", (128, 128), 128)
        checker = Image.new("L", (128, 128))
        pixels = checker.load()
        for y in range(128):
            for x in range(128):
                pixels[x, y] = 255 if (x // 8 + y // 8) % 2 else 0

        flat_metrics = measure_image(self._save("flat.jpg", flat))
        checker_metrics = measure_image(self._save("checker.jpg", checker))

        self.assertLess(flat_metrics.sharpness, 0.02)
        self.assertGreater(checker_metrics.sharpness, flat_metrics.sharpness + 0.5)

    def test_mid_gray_exposure_beats_clipped_white(self):
        middle = measure_image(self._save("middle.jpg", Image.new("L", (128, 128), 118)))
        white = measure_image(self._save("white.jpg", Image.new("L", (128, 128), 255)))

        self.assertGreater(middle.exposure, 0.8)
        self.assertLess(white.exposure, 0.1)
        self.assertGreater(white.clipping, 0.99)

    def test_astro_analysis_finds_isolated_stars_and_reports_bounds(self):
        sky = Image.new("L", (128, 128), 8)
        draw = ImageDraw.Draw(sky)
        for point in ((20, 20), (60, 30), (90, 90), (34, 100)):
            draw.ellipse((point[0] - 1, point[1] - 1, point[0] + 1, point[1] + 1), fill=245)

        result = analyze_astro(self._save("sky.jpg", sky))

        self.assertGreaterEqual(result["star_count"], 4)
        self.assertLessEqual(result["star_count"], 8)
        self.assertGreaterEqual(result["background_brightness"], 0.0)
        self.assertLessEqual(result["background_brightness"], 1.0)
        self.assertIn(result["trailing_warning"], (True, False))

    def test_vision_result_populates_optional_face_and_eye_scores(self):
        metrics = measure_image(self._save("portrait.jpg", Image.new("L", (64, 64), 118)))
        result = parse_vision_result(
            '{"schema_version":1,"face_count":1,"face_quality":0.8,"eye_quality":0.7}'
        )

        apply_vision_result(metrics, result)

        self.assertEqual(1, result["face_count"])
        self.assertAlmostEqual(0.8, metrics.face_quality)
        self.assertAlmostEqual(0.7, metrics.eye_quality)

    def test_schema_two_parses_regions_and_converts_bottom_left_to_top_left(self):
        regions = parse_vision_regions(
            '{"schema_version":2,"coordinate_system":"normalized_bottom_left",'
            '"image_width":4000,"image_height":3000,"face_count":1,'
            '"face_quality":0.8,"eye_quality":0.7,'
            '"faces":[{"x":0.1,"y":0.2,"width":0.3,"height":0.4}],'
            '"saliency_objects":[{"x":0.5,"y":0.1,"width":0.2,"height":0.2,"confidence":0.9}]}'
        )

        self.assertIsInstance(regions, SubjectRegions)
        self.assertEqual((4000, 3000), (regions.image_width, regions.image_height))
        self.assertEqual((0.1, 0.4, 0.3, 0.4), regions.faces[0].rect)
        self.assertEqual((0.5, 0.7, 0.2, 0.2), regions.saliency_objects[0].rect)
        self.assertEqual(0.9, regions.saliency_objects[0].confidence)
        self.assertIsInstance(regions.faces, tuple)

    def test_schema_two_keeps_edge_touching_rectangles_inside_top_left_bounds(self):
        regions = parse_vision_regions(
            '{"schema_version":2,"coordinate_system":"normalized_bottom_left",'
            '"image_width":100,"image_height":100,"face_count":4,'
            '"faces":[{"x":0,"y":0.4,"width":0.2,"height":0.2},'
            '{"x":0.8,"y":0.4,"width":0.2,"height":0.2},'
            '{"x":0.4,"y":0.8,"width":0.2,"height":0.2},'
            '{"x":0.4,"y":0,"width":0.2,"height":0.2}],'
            '"saliency_objects":[]}')

        self.assertEqual(4, len(regions.faces))
        for region in regions.faces:
            self.assertGreaterEqual(region.x, 0.0)
            self.assertGreaterEqual(region.y, 0.0)
            self.assertLessEqual(region.x + region.width, 1.0)
            self.assertLessEqual(region.y + region.height, 1.0)

    def test_schema_two_rejects_malformed_envelope_but_drops_bad_observations(self):
        raw = (
            '{"schema_version":2,"coordinate_system":"normalized_bottom_left",'
            '"image_width":64,"image_height":48,"face_count":0,'
            '"faces":[{"x":0,"y":0,"width":0.2,"height":0.2},'
            '{"x":0.9,"y":0.9,"width":0.2,"height":0.2},'
            '{"x":true,"y":0,"width":0.2,"height":0.2},'
            '{"x":0.1,"y":0.1,"width":0,"height":0.2},'
            '{"x":NaN,"y":0,"width":0.2,"height":0.2}],'
            '"saliency_objects":[{"x":0.1,"y":0.2,"width":0.4,"height":0.3,"confidence":1.2},'
            '{"x":0.1,"y":0.2,"width":0.4,"height":0.3,"confidence":null}]}'
        )
        parsed = parse_vision_regions(raw)

        self.assertEqual(((0.0, 0.8, 0.2, 0.2),), tuple(region.rect for region in parsed.faces))
        self.assertEqual((), parsed.saliency_objects)
        for key, value in (
            ("image_width", True),
            ("image_height", 0),
            ("faces", {}),
            ("saliency_objects", None),
            ("coordinate_system", "top_left"),
        ):
            malformed_payload = json.loads(raw)
            malformed_payload[key] = value
            malformed = json.dumps(malformed_payload)
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                parse_vision_regions(malformed)

    def test_schema_one_scoring_result_remains_compatible(self):
        result = parse_vision_result(
            '{"schema_version":1,"face_count":2,"face_quality":0.6,"eye_quality":0.4}'
        )

        self.assertEqual(2, result["face_count"])
        self.assertEqual(0.6, result["face_quality"])
        self.assertEqual(0.4, result["eye_quality"])

    def test_schema_two_scoring_result_remains_available_to_import_ranking(self):
        result = parse_vision_result(
            '{"schema_version":2,"face_count":1,"face_quality":0.8,"eye_quality":0.7}'
        )

        self.assertEqual(1, result["face_count"])
        self.assertEqual(0.8, result["face_quality"])
        self.assertEqual(0.7, result["eye_quality"])

    def test_region_runner_returns_only_valid_schema_two_output(self):
        executable = self.root / "vision"
        executable.write_text(
            "#!/bin/sh\n"
            "printf '%s' '{\"schema_version\":2,\"coordinate_system\":\"normalized_bottom_left\","
            "\"image_width\":64,\"image_height\":48,\"face_count\":0,"
            "\"faces\":[],\"saliency_objects\":[]}'\n"
        )
        executable.chmod(0o755)

        regions = analyze_subject_regions(self.root / "photo.jpg", executable)

        self.assertIsInstance(regions, SubjectRegions)
        self.assertEqual((64, 48), (regions.image_width, regions.image_height))
        self.assertIsNone(analyze_subject_regions(self.root / "photo.jpg", self.root / "missing"))

    def test_region_runner_fails_closed_for_schema_one_and_nonzero_exit(self):
        executable = self.root / "vision"
        executable.write_text(
            "#!/bin/sh\n"
            "printf '%s' '{\"schema_version\":1,\"face_count\":0}'\n"
        )
        executable.chmod(0o755)

        self.assertIsNone(analyze_subject_regions(self.root / "photo.jpg", executable))

        executable.write_text("#!/bin/sh\nexit 4\n")
        executable.chmod(0o755)
        self.assertIsNone(analyze_subject_regions(self.root / "photo.jpg", executable))

    SCHEMA_3 = (
        '{"schema_version":3,"coordinate_system":"normalized_bottom_left",'
        '"image_width":64,"image_height":48,"face_count":0,'
        '"faces":[],"saliency_objects":[],"feature_print":[0.5,-0.25]}'
    )

    def _script(self, body: str) -> Path:
        executable = self.root / "vision"
        executable.write_text("#!/bin/sh\n" + body)
        executable.chmod(0o755)
        return executable

    def test_parse_vision_result_accepts_schemas_3_and_4_and_regions_accept_them_too(self):
        schema_4 = self.SCHEMA_3.replace('"schema_version":3', '"schema_version":4')
        for raw in (self.SCHEMA_3, schema_4):
            with self.subTest(raw=raw):
                self.assertEqual(0, parse_vision_result(raw)["face_count"])
                regions = parse_vision_regions(raw)
                self.assertEqual((64, 48), (regions.image_width, regions.image_height))
        schema_5 = self.SCHEMA_3.replace('"schema_version":3', '"schema_version":5')
        with self.assertRaises(ValueError):
            parse_vision_result(schema_5)
        with self.assertRaises(ValueError):
            parse_vision_regions(schema_5)

    def test_horizon_is_read_from_schema_4_and_anything_unusable_is_no_measurement(self):
        schema_4 = self.SCHEMA_3.replace('"schema_version":3', '"schema_version":4')
        with_horizon = lambda value: schema_4[:-1] + ',"horizon_deg":%s}' % value
        self.assertEqual(-2.875, parse_vision_result(with_horizon("-2.875"))["horizon_deg"])
        self.assertEqual(3.0, parse_vision_result(with_horizon("3"))["horizon_deg"])
        self.assertIsNone(parse_vision_result(self.SCHEMA_3)["horizon_deg"], "schema 3 predates the horizon")
        self.assertIsNone(parse_vision_result(schema_4)["horizon_deg"])
        for bad in ("null", '"3"', "true", "Infinity", "NaN", "[1]"):
            with self.subTest(bad=bad):
                result = parse_vision_result(with_horizon(bad))
                self.assertIsNone(result["horizon_deg"])
                self.assertEqual(0, result["face_count"], "the rest of the result still stands")
                self.assertEqual(64, parse_vision_regions(with_horizon(bad)).image_width)

    def test_straighten_from_horizon_keeps_the_usable_band_and_rounds_to_the_slider_step(self):
        self.assertIsNone(straighten_from_horizon(None))
        for measured in (0.0, 0.29, -0.29, 8.01, -8.01, 30.0):
            with self.subTest(measured=measured):
                self.assertIsNone(straighten_from_horizon(measured))
        # Vision reports a horizon that rises to the right as a negative angle; leveling it is a clockwise,
        # positive turn. The sign is pinned by the native test below, not by documentation.
        self.assertEqual(2.9, straighten_from_horizon(-2.874999937708556))
        self.assertEqual(-3.1, straighten_from_horizon(3.1250000158132436))
        self.assertEqual(-0.3, straighten_from_horizon(0.3))
        self.assertEqual(8.0, straighten_from_horizon(-8.0))

    def test_parse_feature_print_round_trips_float32_and_rejects_bad_vectors(self):
        self.assertEqual(array("f", [0.5, -0.25]).tobytes(), parse_feature_print(self.SCHEMA_3))
        self.assertIsNone(parse_feature_print('{"schema_version":2,"face_count":0}'))
        for bad in ("[]", '["x"]', "[true]"):
            with self.subTest(bad=bad):
                self.assertIsNone(parse_feature_print('{"schema_version":3,"feature_print":%s}' % bad))

    def test_analyze_faces_carries_the_feature_print(self):
        executable = self._script("printf '%s' '" + self.SCHEMA_3 + "'\n")

        result = analyze_faces(self.root / "photo.jpg", executable)

        self.assertEqual(array("f", [0.5, -0.25]).tobytes(), result["feature_print"])

    def test_feature_prints_runs_one_process_for_every_path_and_skips_failures(self):
        counter = self.root / "runs"
        line_1 = '{"schema_version":3,"face_count":0,"feature_print":[1.0,2.0]}'
        line_3 = '{"schema_version":3,"face_count":0,"feature_print":[3.0,4.0]}'
        executable = self._script(
            f"echo run >> '{counter}'\n"
            f"printf '%s\\n' '{line_1}' '{{\"error\": \"x\"}}' '{line_3}'\n"
        )
        paths = [self.root / f"{index}.jpg" for index in (1, 2, 3)]

        prints = feature_prints(paths, executable)

        self.assertEqual(
            {paths[0]: array("f", [1.0, 2.0]).tobytes(), paths[2]: array("f", [3.0, 4.0]).tobytes()},
            prints,
        )
        self.assertEqual(["run"], counter.read_text().splitlines())

    def test_feature_prints_is_none_for_a_helper_without_batch_mode(self):
        executable = self._script(
            'if [ "$#" -gt 1 ]; then exit 2; fi\n'
            "printf '%s' '" + self.SCHEMA_3 + "'\n"
        )
        paths = [self.root / "1.jpg", self.root / "2.jpg"]

        self.assertIsNone(feature_prints(paths, executable))
        self.assertIsNone(feature_prints(paths, self.root / "missing"))


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("swiftc"), "the Vision helper builds only on macOS with swiftc")
class NativeHorizonTests(unittest.TestCase):
    """The one test that runs the real Vision helper: the sign of a straighten angle is checked against
    what Vision actually measures, never against its documentation."""

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.root = Path(cls.directory.name)
        cls.executable = cls.root / "vision_analyzer"
        build = subprocess.run(["swiftc", "-O", "tools/vision_analyzer.swift", "-o", str(cls.executable)],
                               capture_output=True, text=True, timeout=300, check=False)
        if build.returncode != 0:
            raise AssertionError(f"tools/vision_analyzer.swift does not compile:\n{build.stderr}")

    def horizon(self, rising_right_deg: float) -> Path:
        """Sky over sea, the horizon rising to the right by the given degrees as the picture is seen."""
        width, height = 900, 600
        image = Image.new("RGB", (width, height), (170, 200, 235))
        rise = math.tan(math.radians(rising_right_deg)) * width / 2
        ImageDraw.Draw(image).polygon([(0, height / 2 + rise), (width, height / 2 - rise), (width, height), (0, height)],
                                      fill=(30, 50, 80))
        path = self.root / f"horizon{rising_right_deg}.jpg"
        image.save(path, quality=95)
        return path

    def test_the_corrective_angle_levels_a_tilted_horizon(self):
        for drawn, expected in ((3.0, 3.0), (-3.0, -3.0)):
            with self.subTest(drawn=drawn):
                result = analyze_faces(self.horizon(drawn), self.executable)
                self.assertIsNotNone(result)
                corrective = straighten_from_horizon(result["horizon_deg"])
                # Rising to the right needs a clockwise turn: a positive straighten_deg.
                self.assertIsNotNone(corrective, result)
                self.assertAlmostEqual(expected, corrective, delta=0.5)
