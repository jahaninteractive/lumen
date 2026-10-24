import os
import tempfile
import unittest
from pathlib import Path

from lumen.geonames import Place, PlaceIndex, distance_km


def row(geonameid, name, lat, lon, feature_class, feature_code, country, population=0):
    columns = [str(geonameid), name, name, "", str(lat), str(lon), feature_class, feature_code, country,
               "", "", "", "", "", str(population), "", "", "UTC", "2024-01-01"]
    return "\t".join(columns) + "\n"


def write_fixture(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "countryInfo.txt").write_text(
        "#ISO\tISO3\tISO-Numeric\tfips\tCountry\n"
        "ZA\tZAF\t710\tSF\tSouth Africa\tPretoria\n"
        "AQ\tATA\t010\tAY\tAntarctica\t\n",
        encoding="utf-8",
    )
    (directory / "ZA.txt").write_text(
        row(1, "Madikwe Game Reserve", -24.74351, 26.25732, "P", "PPL", "ZA")
        + row(2, "Tlokweng", -24.67, 25.98, "P", "PPL", "ZA", 55508)
        + row(3, "Abjaterskop", -24.8, 26.17, "T", "HLL", "ZA")
        + row(7, "Holiday Inn Cape Town Waterfront", -33.9173, 18.42, "S", "HTL", "ZA")
        + row(8, "Cape Town", -33.9, 18.42, "P", "PPLA", "ZA", 3433441)
        + "not\ta\tvalid\tline\n",
        encoding="utf-8",
    )
    (directory / "AQ.txt").write_text(
        row(4, "Neko Harbour", -64.8333, -62.5333, "H", "HBR", "AQ")
        + row(5, "Weddell Sea", -75, -35, "H", "SEA", "AQ"),
        encoding="utf-8",
    )
    (directory / "readme.txt").write_text("ignored\n", encoding="utf-8")


class PlaceIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.source = root / "geonames"
        write_fixture(self.source)
        self.index = PlaceIndex(self.source, root / "cache" / "geonames.sqlite")
        self.addCleanup(self.index.close)

    def test_reserve_by_name_beats_nearer_hill(self):
        place = self.index.lookup(-24.80, 26.22)
        self.assertEqual(("Madikwe Game Reserve", "South Africa", "PPL", "geonames", False),
                         (place.name, place.country, place.feature_code, place.source, place.near))
        self.assertAlmostEqual(7.3, place.distance_km, places=1)
        self.assertEqual("Madikwe Game Reserve, South Africa", place.label)

    def test_antarctic_harbour_and_far_sea(self):
        harbour = self.index.lookup(-64.84, -62.53)
        self.assertEqual(("Neko Harbour", "Antarctica", False), (harbour.name, harbour.country, harbour.near))
        sea = self.index.lookup(-74, -40)
        self.assertEqual(("Weddell Sea", True), (sea.name, sea.near))
        self.assertGreater(sea.distance_km, 25)
        self.assertEqual("near Weddell Sea, Antarctica", sea.label)

    def test_landmark_name_on_a_building_does_not_beat_the_town(self):
        self.assertEqual("Cape Town", self.index.lookup(-33.92, 18.42).name)

    def test_nowhere_returns_none(self):
        self.assertIsNone(self.index.lookup(0, 0))

    def test_ensure_skips_malformed_lines_and_rebuilds_only_on_change(self):
        self.assertEqual({"files": 2, "places": 7, "rebuilt": True}, self.index.ensure())
        self.assertFalse(self.index.ensure()["rebuilt"])
        za = self.source / "ZA.txt"
        stat = za.stat()
        os.utime(za, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
        self.assertTrue(self.index.ensure()["rebuilt"])

    def test_online_place_is_found_and_survives_rebuild(self):
        self.index.add("Kaapsehoop", "South Africa", 10.0, 10.0)
        place = self.index.lookup(10.01, 10.0)
        self.assertEqual(("Kaapsehoop", "online", "ONLINE"), (place.name, place.source, place.feature_code))
        za = self.source / "ZA.txt"
        os.utime(za, ns=(za.stat().st_atime_ns, za.stat().st_mtime_ns + 5_000_000_000))
        self.assertEqual(8, self.index.ensure()["places"])
        self.assertEqual("Kaapsehoop", self.index.lookup(10.01, 10.0).name)

    def test_missing_dir_and_country_fallbacks(self):
        root = Path(self.tmp.name)
        with PlaceIndex(root / "absent", root / "cache" / "empty.sqlite") as empty:
            self.assertEqual({"files": 0, "places": 0}, {k: empty.ensure()[k] for k in ("files", "places")})
            self.assertIsNone(empty.lookup(-24.8, 26.22))
        bare = root / "bare"
        bare.mkdir()
        (bare / "AQ.txt").write_text(row(4, "Neko Harbour", -64.8333, -62.5333, "H", "HBR", "AQ")
                                     + row(6, "Somewhere", 1, 1, "P", "PPL", "XX"), encoding="utf-8")
        with PlaceIndex(bare, root / "cache" / "bare.sqlite") as index:
            self.assertEqual("Antarctica", index.lookup(-64.84, -62.53).country)
            self.assertEqual("XX", index.lookup(1, 1).country)

    def test_distance_is_equirectangular_km(self):
        self.assertAlmostEqual(111.2, distance_km(0, 0, 1, 0), places=1)
        self.assertEqual(Place("A", "B", 1.0, "PPL", "geonames").near, False)


if __name__ == "__main__":
    unittest.main()
