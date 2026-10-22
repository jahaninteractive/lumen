import unittest

from lumen.analysis import RegionObservation, SubjectRegions
from lumen.composition import CropRect, generate_candidates


class CompositionTests(unittest.TestCase):
    def _regions(self, faces=(), saliency=(), size=(6000, 4000)):
        return SubjectRegions(size[0], size[1], tuple(faces), tuple(saliency))

    def _by_kind(self, candidates, kind):
        return next(candidate for candidate in candidates if candidate.kind == kind)

    def test_rectangles_produce_bounded_integer_pixel_boxes(self):
        rect = CropRect(0.1, 0.2, 0.5, 0.5)

        self.assertEqual((60, 80, 360, 280), rect.pixel_box(600, 400))

    def test_evidence_produces_scene_and_tighter_candidates_with_honest_rationales(self):
        regions = self._regions(saliency=(RegionObservation(0.4, 0.3, 0.1, 0.2, 0.9),))

        candidates = generate_candidates(6000, 4000, regions)
        scene = self._by_kind(candidates, "subject-in-scene")
        tight = self._by_kind(candidates, "tighter-subject")

        self.assertEqual("padded attention region", scene.rationale)
        self.assertEqual("padded attention region", tight.rationale)
        self.assertGreater(scene.rect.width * scene.rect.height, tight.rect.width * tight.rect.height)
        self.assertEqual(2, sum(candidate.primary for candidate in candidates if candidate.kind != "original"))
        self.assertEqual(
            ["subject-in-scene", "tighter-subject"],
            [candidate.kind for candidate in candidates if candidate.kind != "original" and candidate.primary],
        )

    def test_emits_native_landscape_portrait_and_square_aspects_without_ratio_drift(self):
        landscape = generate_candidates(
            6000, 4000, self._regions(faces=(RegionObservation(0.42, 0.3, 0.12, 0.2),))
        )
        portrait = generate_candidates(
            4000, 6000, self._regions(faces=(RegionObservation(0.42, 0.3, 0.12, 0.2),), size=(4000, 6000))
        )

        self.assertTrue({"native", "3:2", "4:5", "1:1"}.issubset({c.aspect for c in landscape}))
        self.assertTrue({"native", "3:2", "4:5", "1:1"}.issubset({c.aspect for c in portrait}))
        for width, height, candidates in ((6000, 4000, landscape), (4000, 6000, portrait)):
            for candidate in candidates:
                x0, y0, x1, y1 = candidate.rect.pixel_box(width, height)
                if candidate.aspect == "3:2":
                    target = 3 / 2 if width >= height else 2 / 3
                    self.assertLessEqual(abs((x1 - x0) / (y1 - y0) - target), 1 / (y1 - y0))
                elif candidate.aspect == "4:5":
                    target = 5 / 4 if width >= height else 4 / 5
                    self.assertLessEqual(abs((x1 - x0) / (y1 - y0) - target), 1 / (y1 - y0))
                elif candidate.aspect == "1:1":
                    self.assertLessEqual(abs((x1 - x0) - (y1 - y0)), 1)

    def test_corner_faces_stay_inside_the_candidate_at_all_four_corners(self):
        for x, y in ((0.0, 0.0), (0.975, 0.0), (0.0, 0.96), (0.975, 0.96)):
            face = RegionObservation(x, y, 0.025, 0.04)
            candidates = generate_candidates(6000, 4000, self._regions(faces=(face,)))
            evidence_candidates = [candidate for candidate in candidates if candidate.kind != "original"]

            self.assertTrue(evidence_candidates)
            for candidate in evidence_candidates:
                left, top, right, bottom = candidate.pixel_box
                self.assertLessEqual(left, round(face.x * 6000))
                self.assertLessEqual(top, round(face.y * 4000))
                self.assertGreaterEqual(right, round((face.x + face.width) * 6000))
                self.assertGreaterEqual(bottom, round((face.y + face.height) * 4000))

    def test_face_safe_padding_contains_the_face_and_rationale_names_padding(self):
        face = RegionObservation(0.42, 0.3, 0.08, 0.14)

        candidates = generate_candidates(6000, 4000, self._regions(faces=(face,)))
        tight = self._by_kind(candidates, "tighter-subject")
        scene = self._by_kind(candidates, "subject-in-scene")
        left, top, right, bottom = tight.rect.pixel_box(6000, 4000)

        self.assertEqual("padded face", tight.rationale)
        self.assertLessEqual(left, round(face.x * 6000))
        self.assertLessEqual(top, round(face.y * 4000))
        self.assertGreaterEqual(right, round((face.x + face.width) * 6000))
        self.assertGreaterEqual(bottom, round((face.y + face.height) * 4000))
        self.assertGreater(scene.rect.width * scene.rect.height, tight.rect.width * tight.rect.height)

    def test_rejects_non_original_crops_below_eight_megapixels(self):
        candidates = generate_candidates(3000, 2000, self._regions(faces=(RegionObservation(0.4, 0.3, 0.1, 0.2),), size=(3000, 2000)))

        self.assertEqual(["original"], [candidate.kind for candidate in candidates])

    def test_exactly_eight_megapixels_is_accepted_for_a_non_original_crop(self):
        from lumen.composition import _is_acceptable_non_original

        self.assertTrue(_is_acceptable_non_original(8_000_000))
        self.assertFalse(_is_acceptable_non_original(7_999_999))

    def test_marks_retained_candidates_below_twelve_megapixels_with_exact_resolution(self):
        candidates = generate_candidates(4000, 3000, self._regions(faces=(RegionObservation(0.4, 0.3, 0.1, 0.2),), size=(4000, 3000)))
        warning_candidates = [candidate for candidate in candidates if candidate.kind != "original" and candidate.megapixels < 12]

        self.assertTrue(warning_candidates)
        for candidate in warning_candidates:
            self.assertEqual(candidate.pixel_count / 1_000_000, candidate.megapixels)
            self.assertIsNotNone(candidate.resolution_warning)

    def test_deduplicates_identical_geometry_after_pixel_quantization(self):
        regions = self._regions(saliency=(RegionObservation(0.4, 0.3, 0.1, 0.2, 0.9),))

        candidates = generate_candidates(6000, 4000, regions)
        keys = [(c.aspect, c.rect.pixel_box(6000, 4000)) for c in candidates]

        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(candidates), len({c.id for c in candidates}))

    def test_weak_or_missing_evidence_returns_original_and_one_centered_fallback(self):
        weak = self._regions(saliency=(RegionObservation(0.1, 0.2, 0.2, 0.3, 0.49),))

        for regions in (None, weak, self._regions(size=(3000, 2000))):
            candidates = generate_candidates(6000, 4000, regions)
            self.assertEqual(2, len(candidates))
            fallback = candidates[1]
            self.assertEqual("centered fallback", fallback.rationale)
            self.assertAlmostEqual(0.5, fallback.rect.left + fallback.rect.width / 2, delta=1 / 6000)
            self.assertAlmostEqual(0.5, fallback.rect.top + fallback.rect.height / 2, delta=1 / 4000)
            self.assertEqual("original", candidates[0].kind)

    def test_dimension_mismatch_fails_closed_to_centered_fallback(self):
        regions = self._regions(faces=(RegionObservation(0.1, 0.2, 0.2, 0.3),), size=(3000, 2000))

        candidates = generate_candidates(6000, 4000, regions)

        self.assertEqual(["original", "centered-fallback"], [candidate.kind for candidate in candidates])

    def test_rejects_dimensions_far_above_supported_photography(self):
        for dimensions in ((10**200, 1), (1, 10**200), (10**200, 10**200)):
            with self.subTest(dimensions=dimensions), self.assertRaises(ValueError):
                generate_candidates(*dimensions, None)

    def test_rejects_dimensions_above_the_supported_pixel_bound(self):
        for dimensions in ((100_001, 1), (50_000, 30_000)):
            with self.subTest(dimensions=dimensions), self.assertRaises(ValueError):
                generate_candidates(*dimensions, None)

    def test_accepts_the_documented_supported_pixel_bound(self):
        candidates = generate_candidates(100_000, 10_000, None)

        self.assertEqual("original", candidates[0].kind)
        self.assertEqual(1_000_000_000, candidates[0].pixel_count)

    def test_exact_twelve_megapixels_has_no_resolution_warning(self):
        candidates = generate_candidates(
            4000,
            3000,
            self._regions(faces=(RegionObservation(0.4, 0.3, 0.1, 0.2),), size=(4000, 3000)),
        )

        original = next(candidate for candidate in candidates if candidate.pixel_count == 12_000_000)
        self.assertIsNone(original.resolution_warning)

    def test_saliency_at_exact_confidence_threshold_is_reliable_evidence(self):
        regions = self._regions(saliency=(RegionObservation(0.4, 0.3, 0.1, 0.2, 0.5),))

        candidates = generate_candidates(6000, 4000, regions)

        self.assertTrue(any(candidate.kind == "subject-in-scene" for candidate in candidates))
        self.assertFalse(any(candidate.kind == "centered-fallback" for candidate in candidates))

    def test_reversing_face_and_saliency_observations_preserves_ordered_candidates(self):
        faces = (
            RegionObservation(0.2, 0.25, 0.08, 0.12),
            RegionObservation(0.65, 0.55, 0.07, 0.1),
        )
        saliency = (
            RegionObservation(0.35, 0.35, 0.12, 0.15, 0.8),
            RegionObservation(0.5, 0.2, 0.05, 0.09, 0.7),
        )

        first = generate_candidates(6000, 4000, self._regions(faces, saliency))
        reversed_evidence = generate_candidates(6000, 4000, self._regions(faces[::-1], saliency[::-1]))

        self.assertEqual(
            [(candidate.id, candidate.pixel_box) for candidate in first],
            [(candidate.id, candidate.pixel_box) for candidate in reversed_evidence],
        )

    def test_candidate_id_ignores_rationale_but_changes_with_generated_geometry(self):
        face = RegionObservation(0.4, 0.3, 0.1, 0.2)
        saliency = RegionObservation(0.4, 0.3, 0.1, 0.2, 0.5)
        moved_saliency = RegionObservation(0.55, 0.3, 0.1, 0.2, 0.5)

        from_face = generate_candidates(6000, 4000, self._regions(faces=(face,)))
        from_saliency = generate_candidates(6000, 4000, self._regions(saliency=(saliency,)))
        from_moved_saliency = generate_candidates(6000, 4000, self._regions(saliency=(moved_saliency,)))
        select_scene = lambda candidates: next(
            candidate
            for candidate in candidates
            if candidate.kind == "subject-in-scene" and candidate.aspect == "3:2"
        )
        face_candidate = select_scene(from_face)
        saliency_candidate = select_scene(from_saliency)
        moved_candidate = select_scene(from_moved_saliency)

        self.assertEqual("padded face", face_candidate.rationale)
        self.assertEqual("padded attention region", saliency_candidate.rationale)
        self.assertEqual(face_candidate.pixel_box, saliency_candidate.pixel_box)
        self.assertEqual(face_candidate.id, saliency_candidate.id)
        self.assertNotEqual(saliency_candidate.pixel_box, moved_candidate.pixel_box)
        self.assertNotEqual(saliency_candidate.id, moved_candidate.id)


if __name__ == "__main__":
    unittest.main()
