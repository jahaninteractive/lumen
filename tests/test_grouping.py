import unittest
from datetime import datetime, timedelta, timezone

from lumen.domain import Photo
from lumen.grouping import TimeGapGroupingPolicy


def photo(path: str, seconds: float | None, camera: str = "A", exposure: float | None = None) -> Photo:
    captured = None
    if seconds is not None:
        captured = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)
    return Photo(path=path, fingerprint=path, captured_at=captured, camera_id=camera, exposure_seconds=exposure)


def long_exposures(spacing: float, exposure: float | None = 30.0, count: int = 10) -> list[Photo]:
    return [photo(f"{index:02d}.ARW", index * spacing, exposure=exposure) for index in range(count)]


class GroupingTests(unittest.TestCase):
    def test_groups_same_camera_within_gap(self):
        groups = TimeGapGroupingPolicy(2.0).group(
            [photo("c.ARW", 5), photo("a.ARW", 0), photo("b.ARW", 1.9)]
        )
        self.assertEqual([["a.ARW", "b.ARW"], ["c.ARW"]], [[p.path for p in g] for g in groups])

    def test_separates_cameras_even_at_same_time(self):
        groups = TimeGapGroupingPolicy(2.0).group([photo("a.ARW", 0, "A"), photo("b.ARW", 0, "B")])
        self.assertEqual([["a.ARW"], ["b.ARW"]], [[p.path for p in g] for g in groups])

    def test_missing_dates_are_single_deterministic_groups(self):
        groups = TimeGapGroupingPolicy(2.0).group([photo("z.ARW", None), photo("a.ARW", None)])
        self.assertEqual([["a.ARW"], ["z.ARW"]], [[p.path for p in g] for g in groups])

    def test_long_exposures_group_when_gap_fits_exposure(self):
        groups = TimeGapGroupingPolicy(2.0).group(long_exposures(31))
        self.assertEqual(1, len(groups))
        self.assertEqual(10, len(groups[0]))

    def test_long_exposures_group_through_in_camera_noise_reduction(self):
        groups = TimeGapGroupingPolicy(2.0).group(long_exposures(62))
        self.assertEqual([10], [len(group) for group in groups])

    def test_long_exposures_split_beyond_doubled_exposure(self):
        groups = TimeGapGroupingPolicy(2.0).group(long_exposures(90))
        self.assertEqual(10, len(groups))

    def test_unknown_exposure_keeps_plain_gap(self):
        groups = TimeGapGroupingPolicy(2.0).group(long_exposures(2.5, exposure=None, count=3))
        self.assertEqual(3, len(groups))
        groups = TimeGapGroupingPolicy(2.0).group(long_exposures(2.0, exposure=None, count=3))
        self.assertEqual(1, len(groups))


if __name__ == "__main__":
    unittest.main()
