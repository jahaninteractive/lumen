import os
import tempfile
import unittest
import io
import json
import urllib.error
from pathlib import Path
from unittest.mock import patch

from lumen import geonames
from lumen.geonames import Place, PlaceIndex, distance_km, online_lookup


def row(geonameid, name, lat, lon, feature_class, feature_code, country, population=0, alternates=""):
    columns = [str(geonameid), name, name, alternates, str(lat), str(lon), feature_class, feature_code, country,
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

    def test_manual_place_beats_geonames_and_survives_rebuild(self):
        self.index.add("Our camp", "South Africa", -24.74, 26.26, source="manual", feature_code="MANUAL")
        place = self.index.lookup(-24.745, 26.257)
        self.assertEqual(("Our camp", "manual"), (place.name, place.source))
        za = self.source / "ZA.txt"
        os.utime(za, ns=(za.stat().st_atime_ns, za.stat().st_mtime_ns + 5_000_000_000))
        self.assertTrue(self.index.ensure()["rebuilt"])
        place = self.index.lookup(-24.745, 26.257)
        self.assertEqual(("Our camp", "manual"), (place.name, place.source))
        self.index.add("Our tented camp", "South Africa", -24.74, 26.26, source="manual", feature_code="MANUAL")
        self.assertEqual("Our tented camp", self.index.lookup(-24.745, 26.257).name, "a newer remember near the fix wins")

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

    def test_inverted_comma_names_take_the_english_alternate(self):
        source = Path(self.tmp.name) / "inverted"
        source.mkdir()
        (source / "AQ.txt").write_text(
            row(11, "Neko, puerto", -64.84, -62.53, "H", "HBR", "AQ",
                alternates="Neko Harbor,Neko Harbour,Neko  puerto")
            + row(12, "Irizar, isla", -65.0, -64.0, "T", "ISL", "AQ", alternates="Irizar Island,Irizar  isla")
            + row(13, "Straggle, islas", -66.0, -66.0, "T", "ISLS", "AQ",
                  alternates="Llanquihue Islands,Llanquihue  Islas,Straggle Islands,Straggle  islas")
            + row(14, "Williams Field, Antarctic", -77.87, 167.06, "S", "AIRF", "AQ", alternates="NZWD"),
            encoding="utf-8",
        )
        with PlaceIndex(source, Path(self.tmp.name) / "cache" / "inverted.sqlite") as index:
            names = [index.lookup(*point).name for point in ((-64.84, -62.53), (-65.0, -64.0), (-66.0, -66.0), (-77.87, 167.06))]
        self.assertEqual(["Neko Harbour", "Irizar Island", "Straggle Islands", "Williams Field, Antarctic"], names)

    def test_distance_is_equirectangular_km(self):
        self.assertAlmostEqual(111.2, distance_km(0, 0, 1, 0), places=1)
        self.assertEqual(Place("A", "B", 1.0, "PPL", "geonames").near, False)


def nominatim(payload):
    calls = []

    def opener(request, timeout):
        calls.append((request.full_url, request.get_header("User-agent"), timeout))
        return io.BytesIO(json.dumps(payload).encode())
    return opener, calls


class OnlineLookupTests(unittest.TestCase):
    def setUp(self):
        for patcher in (patch.object(geonames, "_last_online_call", None), patch("lumen.geonames.time.sleep")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_reserve_beats_town_and_is_an_online_place(self):
        opener, calls = nominatim({"name": "", "address": {"town": "Zeerust", "nature_reserve": "Madikwe",
                                                           "country": "South Africa"}})
        place = online_lookup(-24.8, 26.2, opener=opener)
        self.assertEqual(Place("Madikwe", "South Africa", 0.0, "ONLINE", "online"), place)
        self.assertFalse(place.near)
        url, agent, timeout = calls[0]
        self.assertEqual("https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat=-24.8&lon=26.2&zoom=10", url)
        self.assertEqual(("Lumen/1.0 (local photo manager)", 5.0), (agent, timeout))

    def test_name_then_missing_country(self):
        opener, _ = nominatim({"name": "Somewhere", "address": {"village": "Elsewhere"}})
        self.assertEqual(("Somewhere", ""), (lambda p: (p.name, p.country))(online_lookup(1, 1, opener=opener)))

    def test_network_error_or_no_name_is_none(self):
        def broken(request, timeout):
            raise urllib.error.URLError("offline")
        self.assertIsNone(online_lookup(1, 1, opener=broken))
        opener, _ = nominatim({"error": "Unable to geocode"})
        self.assertIsNone(online_lookup(1, 1, opener=opener))

    def test_back_to_back_calls_wait_one_second(self):
        opener, _ = nominatim({"name": "A", "address": {"country": "B"}})
        with patch("lumen.geonames.time.monotonic", side_effect=[100.0, 100.0, 100.25, 100.25]), \
                patch("lumen.geonames.time.sleep") as sleep:
            online_lookup(1, 1, opener=opener)
            sleep.assert_not_called()
            online_lookup(1, 1, opener=opener)
        sleep.assert_called_once()
        self.assertGreaterEqual(sleep.call_args.args[0], 0.75)


if __name__ == "__main__":
    unittest.main()
