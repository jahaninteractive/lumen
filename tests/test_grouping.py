import unittest
from datetime import datetime, timedelta, timezone

from lumen.domain import Photo
from lumen.grouping import TimeGapGroupingPolicy


def photo(path: str, seconds: float | None, camera: str = "A") -> Photo:
    captured = None
    if seconds is not None:
        captured = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)
    return Photo(path=path, fingerprint=path, captured_at=captured, camera_id=camera)


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


if __name__ == "__main__":
    unittest.main()
