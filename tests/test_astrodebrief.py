import unittest
from pathlib import Path

from lumen.astrodebrief import parse_siril_log

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


if __name__ == "__main__":
    unittest.main()
