import unittest

from lumen.domain import Photo, PhotoMetrics
from lumen.scoring import rank_group


def photo(name: str) -> Photo:
    return Photo(path=name, fingerprint=name, captured_at=None, camera_id="A")


class ScoringTests(unittest.TestCase):
    def test_sharpness_and_exposure_weights_renormalize_without_faces(self):
        sharp = (photo("sharp.ARW"), PhotoMetrics(sharpness=1.0, exposure=0.0, clipping=0.0))
        exposed = (photo("exposed.ARW"), PhotoMetrics(sharpness=0.0, exposure=1.0, clipping=0.0))

        ranked = rank_group([exposed, sharp])

        self.assertEqual("sharp.ARW", ranked[0].photo.path)
        self.assertAlmostEqual(2 / 3, ranked[0].total, places=3)
        self.assertAlmostEqual(1 / 3, ranked[1].total, places=3)

    def test_face_and_eye_quality_contribute_when_available(self):
        good_eyes = (
            photo("eyes.ARW"),
            PhotoMetrics(sharpness=0.5, exposure=0.5, clipping=0, face_quality=1.0, eye_quality=1.0),
        )
        poor_eyes = (
            photo("blink.ARW"),
            PhotoMetrics(sharpness=0.5, exposure=0.5, clipping=0, face_quality=0.0, eye_quality=0.0),
        )

        ranked = rank_group([poor_eyes, good_eyes])

        self.assertEqual("eyes.ARW", ranked[0].photo.path)
        self.assertAlmostEqual(0.625, ranked[0].total, places=3)
        self.assertAlmostEqual(0.375, ranked[1].total, places=3)


if __name__ == "__main__":
    unittest.main()
