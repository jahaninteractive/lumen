import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from lumen.analysis import (
    SubjectRegions,
    analyze_astro,
    analyze_subject_regions,
    apply_vision_result,
    measure_image,
    parse_vision_result,
    parse_vision_regions,
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


if __name__ == "__main__":
    unittest.main()
