import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import urllib.error
from unittest import mock
from urllib.parse import parse_qs

from lumen.mapdata import (KIND_MIN_ZOOM, Feature, MapIndex, TrackPoint, decimate, fetch_region, overpass_query,
                           overpass_to_geojson, parse_geojson, parse_gpx, pixel_degrees, position_at)

HEADER = '<?xml version="1.0" encoding="UTF-8"?>\n<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1" creator="test">\n'

# The later segment comes first in the file, so sorting is observable; one point has no <time>.
TRACK_GPX = HEADER + """<trk>
<name>Tuesday morning </name>
<trkseg>
<trkpt lat="-24.7697526" lon="26.1816550"><ele>1132.128</ele><time>2026-10-20T05:34:00.829Z</time></trkpt>
<trkpt lat="-24.7697888" lon="26.1814875"><ele>1131.999</ele><time>2026-10-20T05:34:10.092Z</time></trkpt>
<trkpt lat="-24.7697938" lon="26.1814650"><ele>1132.456</ele><time>2026-10-20T05:34:20.793Z</time></trkpt>
</trkseg>
<trkseg>
<trkpt lat="-24.7598285" lon="26.1713301"><ele>1133.488</ele><time>2026-10-20T03:34:14.393Z</time></trkpt>
<trkpt lat="-24.7598830" lon="26.1711461"><ele>1134.610</ele></trkpt>
<trkpt lat="-24.7599422" lon="26.1710003"><time>2026-10-20T03:34:21.535Z</time></trkpt>
</trkseg>
</trk>
<wpt lat="-50.4684135517" lon="-28.7991254899">
<ele>14.5677914701</ele>
<time>2026-11-01T07:57:41Z</time>
<name><![CDATA[Wypt 026]]></name>
<desc><![CDATA[1 Nov 2026  5:57 am]]></desc>
<extensions>
<fpdata xmlns="http://www.motionx.com/GPX-Extensions/1/1" sfv="10"><bin enc="base64">ESwAAAAB</bin></fpdata>
</extensions>
</wpt>
</gpx>
"""

UNTIMED_GPX = HEADER + """<rte><rtept lat="1.0" lon="2.0"/><rtept lat="1.5" lon="2.5"/></rte>
<trk><trkseg><trkpt lat="3.0" lon="4.0"/><trkpt lat="3.1" lon="4.1"/></trkseg></trk>
</gpx>
"""

FEATURES = {
    "type": "FeatureCollection",
    "features": [
        {"type": "Feature", "properties": {"scalerank": 3, "name": "N1"},
         "geometry": {"type": "LineString", "coordinates": [[18.123456789, -33.987654321], [18.2, -33.9]]}},
        {"type": "Feature", "properties": {"boundary": "national_park", "name": "Madikwe"},
         "geometry": {"type": "Polygon", "coordinates": [[[26.1, -24.9], [26.4, -24.9], [26.4, -24.6], [26.1, -24.9]]]}},
        {"type": "Feature", "properties": {"kind": "place", "name": "Cape Town"},
         "geometry": {"type": "Point", "coordinates": [18.42, -33.92]}},
    ],
}


class GpxTests(unittest.TestCase):
    def test_gpx_tracks_and_waypoints_become_features(self):
        features, points = parse_gpx(TRACK_GPX)
        tracks = [f for f in features if f.kind == "track"]
        waypoints = [f for f in features if f.kind == "waypoint"]
        self.assertEqual([f.name for f in tracks], ["Tuesday morning "])
        self.assertEqual(tracks[0].geometry["type"], "LineString")
        self.assertEqual(len(tracks[0].geometry["coordinates"]), 6)  # the untimed point still draws
        self.assertEqual(tracks[0].geometry["coordinates"][0], [26.18165, -24.76975])
        self.assertEqual([f.name for f in waypoints], ["Wypt 026"])
        self.assertEqual(waypoints[0].geometry, {"type": "Point", "coordinates": [-28.79913, -50.46841]})
        self.assertEqual(len(points), 5)
        self.assertEqual([p.time for p in points], sorted(p.time for p in points))
        self.assertEqual(points[0], TrackPoint(
            datetime(2026, 10, 20, 3, 34, 14, 393000, tzinfo=timezone.utc), -24.7598285, 26.1713301, 1133.488))
        self.assertIsNone(points[1].ele)
        self.assertEqual(points[0].time.utcoffset().total_seconds(), 0)

    def test_gpx_without_times_has_no_span(self):
        features, points = parse_gpx(UNTIMED_GPX)
        self.assertEqual([(f.kind, f.name) for f in features], [("track", "Track 1"), ("track", "Track 2")])
        self.assertEqual(points, [])

    def test_gpx_rejects_other_xml(self):
        for text in ('<gpx xmlns="http://www.topografix.com/GPX/1/0" version="1.0"/>', "<svg/>"):
            with self.assertRaisesRegex(ValueError, "^not a GPX 1.1 document$"):
                parse_gpx(text)

    def test_gpx_times_are_utc_whatever_the_designator(self):
        text = HEADER + """<trk><trkseg>
<trkpt lat="1" lon="2"><time>2026-10-20T05:34:00+02:00</time></trkpt>
<trkpt lat="1.1" lon="2.1"><time>2026-10-20T03:35:00</time></trkpt>
</trkseg></trk></gpx>"""
        _, points = parse_gpx(text)
        self.assertEqual([p.time for p in points], [
            datetime(2026, 10, 20, 3, 34, tzinfo=timezone.utc), datetime(2026, 10, 20, 3, 35, tzinfo=timezone.utc)])
        self.assertTrue(all(p.time.tzinfo is timezone.utc for p in points))

    def test_gpx_malformed_input_raises_value_error(self):
        cases = {
            "this is not xml <": "^not a GPX 1.1 document$",
            HEADER + '<trk><trkseg><trkpt lat="1"/></trkseg></trk></gpx>': "^trkpt without a readable lat/lon$",
            HEADER + '<trk><trkseg><trkpt lat="1" lon="2"><time>yesterday</time></trkpt></trkseg></trk></gpx>':
                "^unreadable time 'yesterday'$",
        }
        for text, message in cases.items():
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                parse_gpx(text)


class GeoJsonTests(unittest.TestCase):
    def test_geojson_kinds_by_property_filename_and_tags(self):
        road, park, place = parse_geojson(json.dumps(FEATURES), source_name="ne_10m_roads.geojson")
        self.assertEqual((road.kind, road.rank, road.name), ("road", 3, "N1"))
        self.assertEqual(road.geometry["coordinates"][0], [18.12346, -33.98765])
        self.assertEqual((place.kind, place.rank, place.name), ("place", 0, "Cape Town"))
        park_only = {"type": "FeatureCollection", "features": [FEATURES["features"][1]]}
        self.assertEqual([f.kind for f in parse_geojson(json.dumps(park_only))], ["park"])
        lakes = parse_geojson(json.dumps(FEATURES), source_name="ne_10m_lakes.geojson")
        self.assertEqual([f.kind for f in lakes], ["lake", "lake", "place"])
        untagged = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [0, 0]}}]}
        self.assertEqual(parse_geojson(json.dumps(untagged))[0].kind, "other")
        with self.assertRaisesRegex(ValueError, "^not a GeoJSON FeatureCollection$"):
            parse_geojson(json.dumps(FEATURES["features"][0]))

    def test_geojson_skips_malformed_features(self):
        good = FEATURES["features"][2]
        collection = {"type": "FeatureCollection", "features": [
            good,
            "not a feature",
            {"type": "Feature", "properties": {}, "geometry": {"coordinates": [1, 2]}},
            {"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": ["a"]}},
            {"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [26.1]}},
            {"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [float("nan"), 1]}},
            {"type": "Feature", "properties": {}, "geometry": {"type": "LineString", "coordinates": [[1, 2], [3]]}},
            {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [[]]}},
        ]}
        self.assertEqual([f.name for f in parse_geojson(json.dumps(collection))], ["Cape Town"])

    def test_gpx_refuses_a_non_finite_position(self):
        text = HEADER + '<wpt lat="nan" lon="2"/></gpx>'
        with self.assertRaisesRegex(ValueError, "^wpt without a readable lat/lon$"):
            parse_gpx(text)

    def test_geojson_scalerank_falls_back_to_zero(self):
        ranks = [{"scalerank": "3.0"}, {"scalerank": "high"}, {"scalerank": None}, {}]
        collection = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": props, "geometry": {"type": "Point", "coordinates": [0, 0]}}
            for props in ranks]}
        self.assertEqual([f.rank for f in parse_geojson(json.dumps(collection))], [3, 0, 0, 0])

    def test_natural_earth_physical_files_kind_by_class_case_insensitively(self):
        polys = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"FEATURECLA": "Range/mtn", "NAME": "Waterberg", "SCALERANK": 2},
             "geometry": {"type": "Polygon", "coordinates": [[[27, -24], [28, -24], [28, -25], [27, -25], [27, -24]]]}},
            {"type": "Feature", "properties": {"FEATURECLA": "Plain", "NAME": "Bushveld", "SCALERANK": 3}, "geometry": {"type": "Point", "coordinates": [27.5, -24.5]}},
            {"type": "Feature", "properties": {"FEATURECLA": "Desert", "NAME": "Kalahari", "SCALERANK": 1}, "geometry": {"type": "Point", "coordinates": [21, -24]}},
            {"type": "Feature", "properties": {"FEATURECLA": "Island", "NAME": "Robben", "SCALERANK": 6}, "geometry": {"type": "Point", "coordinates": [18.4, -33.8]}},
            {"type": "Feature", "properties": {"FEATURECLA": "Continent", "NAME": "Africa", "SCALERANK": 0}, "geometry": {"type": "Point", "coordinates": [20, 0]}},
            {"type": "Feature", "properties": {"FEATURECLA": "Lake", "NAME": "Caspian", "SCALERANK": 0}, "geometry": {"type": "Point", "coordinates": [50, 42]}},
        ]}
        found = parse_geojson(json.dumps(polys), source_name="ne_10m_geography_regions_polys.geojson")
        self.assertEqual([(f.kind, f.name, f.rank) for f in found],
                         [("mountain", "Waterberg", 2), ("plain", "Bushveld", 3), ("desert", "Kalahari", 1), ("region", "Robben", 6)])
        points = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"featurecla": "cape", "name": "Cape Point", "scalerank": 4}, "geometry": {"type": "Point", "coordinates": [18.5, -34.3]}}]}
        self.assertEqual(parse_geojson(json.dumps(points), source_name="ne_10m_geography_regions_points.geojson")[0].kind, "region")

    def test_peaks_carry_their_elevation_in_the_name(self):
        peaks = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"featurecla": "mountain", "name": "Kilimanjaro", "elevation": 5895, "scalerank": 1}, "geometry": {"type": "Point", "coordinates": [37.35, -3.07]}},
            {"type": "Feature", "properties": {"featurecla": "pass", "name": "Sani Pass", "scalerank": 5}, "geometry": {"type": "Point", "coordinates": [29.3, -29.6]}},
        ]}
        found = parse_geojson(json.dumps(peaks), source_name="ne_10m_geography_regions_elevation_points.geojson")
        self.assertEqual([(f.kind, f.name) for f in found], [("peak", "Kilimanjaro, 5895 m"), ("peak", "Sani Pass")])

    def test_land_and_sea_files_split_multipolygons_and_drop_null_island(self):
        land = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"featurecla": "Land", "scalerank": 0}, "geometry": {"type": "MultiPolygon", "coordinates": [
                [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]], [[[10, 10], [11, 10], [11, 11], [10, 11], [10, 10]]]]}},
            {"type": "Feature", "properties": {"featurecla": "Null island", "scalerank": 0}, "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0.1, 0], [0.1, 0.1], [0, 0]]]}},
        ]}
        found = parse_geojson(json.dumps(land), source_name="ne_10m_land.geojson")
        self.assertEqual([(f.kind, f.geometry["type"]) for f in found], [("land", "Polygon"), ("land", "Polygon")])
        self.assertEqual(found[1].geometry["coordinates"][0][0], [10, 10])
        seas = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"featurecla": "gulf", "name": "False Bay", "scalerank": 3},
             "geometry": {"type": "Polygon", "coordinates": [[[18.4, -34.1], [18.9, -34.1], [18.9, -34.4], [18.4, -34.4], [18.4, -34.1]]]}}]}
        self.assertEqual([(f.kind, f.name) for f in parse_geojson(json.dumps(seas), source_name="ne_10m_geography_marine_polys.geojson")], [("sea", "False Bay")])

    def test_lakes_file_keeps_its_lake_class(self):
        lakes = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"featurecla": "Lake", "name": "Victoria", "scalerank": 0},
             "geometry": {"type": "Polygon", "coordinates": [[[32, -1], [34, -1], [34, -3], [32, -1]]]}}]}
        self.assertEqual([(f.kind, f.name) for f in parse_geojson(json.dumps(lakes), source_name="ne_10m_lakes.geojson")],
                         [("lake", "Victoria")])

    def test_border_file_is_not_caught_by_the_land_marker(self):
        border = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {}, "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}}]}
        self.assertEqual(parse_geojson(json.dumps(border), source_name="ne_10m_admin_0_boundary_lines_land.geojson")[0].kind, "border")

    def test_decimate_keeps_ends_and_drops_close_vertices(self):
        coords = [[0, 0], [0.01, 0], [0.2, 0], [0.21, 0.05], [0.21, 0.3], [0.22, 0.3]]
        self.assertEqual(decimate(coords, 0.1), [[0, 0], [0.2, 0], [0.21, 0.3], [0.22, 0.3]])
        self.assertEqual(decimate([[1, 1]], 0.1), [[1, 1]])

    def test_feature_bbox(self):
        polygon = Feature("park", "P", 0, {"type": "Polygon", "coordinates": [[[26.1, -24.9], [26.4, -24.6], [26.2, -24.7]]]})
        self.assertEqual(polygon.bbox, (-24.9, -24.6, 26.1, 26.4))
        point = Feature("place", "", 0, {"type": "Point", "coordinates": [18.42, -33.92]})
        self.assertEqual(point.bbox, (-33.92, -33.92, 18.42, 18.42))

    def test_zoom_helpers(self):
        self.assertEqual(KIND_MIN_ZOOM, {"road": 6, "place": 5, "river": 3, "lake": 3, "sea": 3, "mountain": 3,
                                         "plain": 3, "desert": 3, "region": 3, "peak": 6})
        self.assertEqual(pixel_degrees(0), 360 / 256)
        self.assertEqual(pixel_degrees(2), 360 / 1024)


# A dense wiggle near Madikwe: 200 vertices 0.001 degrees apart, so low zooms keep far fewer of them.
WIGGLE = [[26.2 + i * 0.001, -24.8 + (i % 2) * 0.0005] for i in range(200)]
LAYER = {
    "type": "FeatureCollection",
    "features": [
        {"type": "Feature", "properties": {"kind": "border", "name": "Wiggle"},
         "geometry": {"type": "LineString", "coordinates": WIGGLE}},
        {"type": "Feature", "properties": {"kind": "road", "name": "R49"},
         "geometry": {"type": "LineString", "coordinates": [[26.1, -24.7], [26.3, -24.7]]}},
        {"type": "Feature", "properties": {"kind": "park", "name": "Madikwe"},
         "geometry": {"type": "Polygon", "coordinates": [[[26.1, -24.9], [26.4, -24.9], [26.4, -24.6], [26.1, -24.9]]]}},
        {"type": "Feature", "properties": {"kind": "place", "name": "Cape Town"},
         "geometry": {"type": "Point", "coordinates": [18.42, -33.92]}},
        {"type": "Feature", "properties": {"kind": "island", "name": "Taveuni"},
         "geometry": {"type": "Point", "coordinates": [179.5, -16.8]}},
    ],
}
MADIKWE = (-24.95, -24.55, 26.05, 26.55)


class PositionAtTests(unittest.TestCase):
    def test_interpolates_inside_a_gap_and_snaps_to_the_nearest_point_outside_it(self):
        at = lambda minute, second=0: datetime(2026, 10, 22, 6, minute, second, tzinfo=timezone.utc)
        points = [TrackPoint(at(0), -33.0, 18.0, 10.0), TrackPoint(at(0, 10), -33.1, 18.2, 20.0),
                  TrackPoint(at(30), -34.0, 19.0, None)]
        gap = timedelta(minutes=10)
        lat, lon, ele = position_at(points, at(0, 5), gap)
        self.assertEqual((-33.05, 18.1, 15.0), (round(lat, 6), round(lon, 6), ele))
        self.assertEqual((-33.1, 18.2, 20.0), position_at(points, at(5), gap), "past the segment: its last point")
        self.assertIsNone(position_at(points, at(15), gap), "in the gap, more than ten minutes from either end")
        self.assertEqual((-34.0, 19.0, None), position_at(points, at(30), gap))
        self.assertEqual((-33.0, 18.0, 10.0), position_at(points, at(0) - timedelta(minutes=9), gap))
        self.assertIsNone(position_at(points, at(41), gap))
        self.assertIsNone(position_at([], at(0), gap))
        lat, lon, ele = position_at(points, at(15, 5), timedelta(hours=1))
        self.assertIsNone(ele, "one end without an elevation gives none")


class MapIndexTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.base = self.root / "base"
        self.overlays = self.root / "overlays"
        self.base.mkdir()
        self.overlays.mkdir()
        (self.base / "layer.geojson").write_text(json.dumps(LAYER))
        (self.overlays / "tuesday.gpx").write_text(TRACK_GPX)
        (self.overlays / "untimed.gpx").write_text(UNTIMED_GPX)
        self.index = MapIndex(self.root / "map.sqlite", [self.base, self.root / "missing", self.overlays])
        self.addCleanup(self.index.close)

    def test_ensure_indexes_sources_once_and_reparses_only_changes(self):
        first = self.index.ensure()
        self.assertEqual(first["sources"], 3)
        self.assertEqual(first["features"], len(LAYER["features"]) + 2 + 2)
        self.assertEqual(sorted(first["reparsed"]), ["base/layer.geojson", "overlays/tuesday.gpx", "overlays/untimed.gpx"])
        self.assertEqual(first["failed"], [])
        self.assertEqual(self.index.ensure()["reparsed"], [])
        track = self.overlays / "tuesday.gpx"
        stat = track.stat()
        os.utime(track, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
        self.assertEqual(self.index.ensure()["reparsed"], ["overlays/tuesday.gpx"])
        track.unlink()
        after = self.index.ensure()
        self.assertEqual((after["sources"], after["features"], after["reparsed"]), (2, len(LAYER["features"]) + 2, []))
        self.assertEqual([f["name"] for f in self.index.query(-25, -24, 26, 27, zoom=12) if f["kind"] == "track"], [])

    def test_ensure_records_a_broken_file_and_keeps_going(self):
        (self.base / "broken.gpx").write_text("<nope/>")
        result = self.index.ensure()
        self.assertEqual(result["failed"], ["base/broken.gpx"])
        self.assertEqual(result["sources"], 4)
        broken = [s for s in self.index.summaries() if s["name"] == "base/broken.gpx"]
        self.assertEqual(broken, [{"name": "base/broken.gpx", "summary": {"error": "not a GPX 1.1 document"}}])

    def test_same_file_name_in_two_folders_does_not_collide(self):
        (self.overlays / "layer.geojson").write_text(json.dumps(LAYER))
        names = [s["name"] for s in (self.index.ensure(), self.index.summaries())[1]]
        self.assertIn("base/layer.geojson", names)
        self.assertIn("overlays/layer.geojson", names)
        self.assertFalse(any(str(self.root) in name for name in names))

    def test_query_returns_only_intersecting_features_without_paths(self):
        self.index.ensure()
        features = self.index.query(*MADIKWE, zoom=12)
        self.assertEqual({f["name"] for f in features}, {"Wiggle", "R49", "Madikwe", "Tuesday morning "})
        for feature in features:
            self.assertEqual(set(feature), {"kind", "name", "rank", "geometry"})

    def test_query_drops_roads_below_their_zoom(self):
        self.index.ensure()
        self.assertIn("R49", [f["name"] for f in self.index.query(*MADIKWE, zoom=KIND_MIN_ZOOM["road"])])
        self.assertNotIn("R49", [f["name"] for f in self.index.query(*MADIKWE, zoom=KIND_MIN_ZOOM["road"] - 1)])

    def test_query_decimates_at_low_zoom(self):
        self.index.ensure()

        def vertices(zoom):
            return len(next(f for f in self.index.query(*MADIKWE, zoom=zoom) if f["name"] == "Wiggle")["geometry"]["coordinates"])

        self.assertEqual(vertices(12), len(WIGGLE))
        self.assertLess(vertices(2), vertices(12))
        self.assertEqual(vertices(2), 2)

    def test_query_handles_a_viewport_across_the_antimeridian(self):
        self.index.ensure()
        names = [f["name"] for f in self.index.query(-20, -10, 170, -170, zoom=8)]
        self.assertEqual(names, ["Taveuni"])
        self.assertEqual([f["name"] for f in self.index.query(-20, -10, 170, 175, zoom=8)], [])

    def test_query_clips_long_lines_to_the_viewport(self):
        # 10,000 vertices from lon 0 to 50 along lat -24.7, so about 200 fall in Madikwe's half-degree box.
        coast = [[i * 0.005, -24.7] for i in range(10_000)]
        ring = [[round(26.2 + i * 0.01, 5), round(-24.8 + (i % 2) * 0.01, 5)] for i in range(20)] + [[26.2, -24.8]]
        layer = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"kind": "coastline", "name": "Coast"},
             "geometry": {"type": "LineString", "coordinates": coast}},
            {"type": "Feature", "properties": {"kind": "park", "name": "Ring"},
             "geometry": {"type": "Polygon", "coordinates": [ring]}},
        ]}
        (self.base / "coast.geojson").write_text(json.dumps(layer))
        self.index.ensure()
        features = {f["name"]: f["geometry"] for f in self.index.query(-24.8, -24.6, 26.2, 26.4, zoom=9)}
        clipped = features["Coast"]["coordinates"]
        self.assertEqual(features["Coast"]["type"], "LineString")
        self.assertLess(len(clipped), 200)
        self.assertIn([26.3, -24.7], clipped)
        self.assertLessEqual(clipped[0][0], 26.2 - 0.02)  # the padded box keeps an edge beyond the viewport
        self.assertLess(clipped[-1][0], 26.5)
        self.assertEqual(features["Ring"]["coordinates"], [ring])  # rings are never clipped

    def test_query_splits_a_line_that_leaves_and_reenters_the_viewport(self):
        hook = [[26.0, -24.7], [26.3, -24.7], [26.6, -24.7], [26.6, -20.0], [26.4, -20.0], [26.4, -24.75], [26.2, -24.75]]
        layer = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"kind": "border", "name": "Hook"},
             "geometry": {"type": "LineString", "coordinates": hook}}]}
        (self.base / "hook.geojson").write_text(json.dumps(layer))
        self.index.ensure()
        hooked = next(f for f in self.index.query(-24.8, -24.6, 26.2, 26.45, zoom=12) if f["name"] == "Hook")
        self.assertEqual(hooked["geometry"]["type"], "MultiLineString")
        self.assertEqual(len(hooked["geometry"]["coordinates"]), 2)

    def test_ensure_records_an_unreadable_file_as_failed(self):
        (self.base / "folder.geojson").mkdir()
        (self.overlays / "dir.gpx").mkdir()
        result = self.index.ensure()
        self.assertEqual(sorted(result["failed"]), ["base/folder.geojson", "overlays/dir.gpx"])
        error = {s["name"]: s["summary"] for s in self.index.summaries()}["overlays/dir.gpx"]["error"]
        self.assertTrue(error)
        self.assertNotIn("/", error)

    def test_ensure_records_a_feature_that_cannot_be_boxed_and_keeps_going(self):
        for coordinates in ([26.1], [[]]):
            bad = Feature("other", "", 0, {"type": "Point", "coordinates": coordinates})
            with self.subTest(coordinates=coordinates), \
                    mock.patch("lumen.mapdata._parse_source", return_value=([bad], {"features": 1})):
                (self.overlays / "bad.gpx").write_text(str(coordinates))
                result = self.index.ensure()
                self.assertIn("overlays/bad.gpx", result["failed"])
                summary = {s["name"]: s["summary"] for s in self.index.summaries()}["overlays/bad.gpx"]
                self.assertIn("error", summary)

    def test_summaries_report_track_counts_and_span(self):
        self.index.ensure()
        summaries = {s["name"]: s["summary"] for s in self.index.summaries()}
        self.assertEqual(summaries["overlays/tuesday.gpx"], {
            "features": 2, "tracks": 1, "waypoints": 1,
            "span": ["2026-10-20T03:34:14.393000+00:00", "2026-10-20T05:34:20.793000+00:00"],
        })
        self.assertEqual(summaries["overlays/untimed.gpx"], {"features": 2, "tracks": 2, "waypoints": 0, "span": None})
        self.assertEqual(summaries["base/layer.geojson"],
                         {"features": len(LAYER["features"]), "tracks": 0, "waypoints": 0, "span": None})


OVERPASS = {"elements": [
    {"type": "way", "id": 1, "tags": {"natural": "water", "name": "Pan"},
     "geometry": [{"lat": -24.7, "lon": 26.2}, {"lat": -24.7, "lon": 26.3}, {"lat": -24.8, "lon": 26.3},
                  {"lat": -24.7, "lon": 26.2}]},
    {"type": "way", "id": 2, "tags": {"highway": "track"},
     "geometry": [{"lat": -24.6, "lon": 26.1}, {"lat": -24.9, "lon": 26.5}]},
    {"type": "way", "id": 3, "tags": {"highway": "track"}, "geometry": [{"lat": -24.6, "lon": 26.1}]},
    {"type": "relation", "id": 4, "tags": {"boundary": "protected_area", "name": "Madikwe"}, "members": [
        {"type": "way", "role": "outer", "geometry": [{"lat": -24.6, "lon": 26.1}, {"lat": -24.6, "lon": 26.5}]},
        {"type": "way", "role": "outer", "geometry": [{"lat": -24.6, "lon": 26.5}, {"lat": -24.9, "lon": 26.5}]},
        {"type": "way", "role": "inner", "geometry": [{"lat": -24.7, "lon": 26.2}, {"lat": -24.8, "lon": 26.2}]},
        {"type": "node", "role": "outer"},
    ]},
]}


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200):
        self.body, self.status = body, status

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class OverpassTests(unittest.TestCase):
    def test_query_names_the_box_in_south_west_north_east_order(self):
        query = overpass_query(-24.95, 26.05, -24.55, 26.55)
        self.assertTrue(query.startswith("[out:json][timeout:90];(way[\"boundary\"~\"^(protected_area|national_park)$\"]"
                                         "(-24.95,26.05,-24.55,26.55);"))
        self.assertTrue(query.endswith('way["natural"="water"](-24.95,26.05,-24.55,26.55););out geom;'))
        self.assertEqual(7, query.count("(-24.95,26.05,-24.55,26.55)"))

    def test_geojson_from_ways_and_relations(self):
        collection = overpass_to_geojson(OVERPASS)
        self.assertEqual("FeatureCollection", collection["type"])
        water, road, park = collection["features"]
        self.assertEqual("Polygon", water["geometry"]["type"])
        self.assertEqual([[[26.2, -24.7], [26.3, -24.7], [26.3, -24.8], [26.2, -24.7]]], water["geometry"]["coordinates"])
        self.assertEqual({"natural": "water", "name": "Pan", "kind": "lake"}, water["properties"])
        self.assertEqual(("LineString", "road", ""), (road["geometry"]["type"], road["properties"]["kind"], road["properties"]["name"]))
        self.assertEqual("MultiLineString", park["geometry"]["type"])
        self.assertEqual([[[26.1, -24.6], [26.5, -24.6]], [[26.5, -24.6], [26.5, -24.9]]], park["geometry"]["coordinates"])
        self.assertEqual(("park", "Madikwe"), (park["properties"]["kind"], park["properties"]["name"]))
        self.assertEqual(["lake", "road", "park"], [f.kind for f in parse_geojson(json.dumps(collection))])

    def test_closed_road_stays_a_line_and_relation_without_outers_is_skipped(self):
        ring = [{"lat": -24.7, "lon": 26.2}, {"lat": -24.7, "lon": 26.3}, {"lat": -24.7, "lon": 26.2}]
        collection = overpass_to_geojson({"elements": [
            {"type": "way", "tags": {"highway": "track"}, "geometry": ring},
            {"type": "relation", "tags": {"boundary": "protected_area"}, "members": [
                {"type": "way", "role": "inner", "geometry": ring}, {"type": "way", "role": "outer"}]},
        ]})
        self.assertEqual(["LineString"], [feature["geometry"]["type"] for feature in collection["features"]])

    def test_fetch_region_posts_the_query_and_writes_the_file(self):
        requests = []

        def opener(request, timeout):
            requests.append((request, timeout))
            return FakeResponse(json.dumps(OVERPASS).encode())

        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "regions"
            path = fetch_region("../.madikwe", (-24.95, 26.05, -24.55, 26.55), out, opener=opener)
            self.assertEqual(out / "madikwe.geojson", path)
            self.assertEqual(3, len(json.loads(path.read_text())["features"]))
            self.assertEqual(["madikwe.geojson"], [p.name for p in out.iterdir()])
        request, timeout = requests[0]
        self.assertEqual(("https://overpass-api.de/api/interpreter", "POST", 120.0),
                         (request.full_url, request.get_method(), timeout))
        agent = request.get_header("User-agent")
        self.assertTrue(agent.startswith("Lumen/"))
        self.assertIn("https://github.com/jahaninteractive/lumen", agent)
        query = parse_qs(request.data.decode())["data"][0]
        self.assertIn("out geom", query)
        self.assertIn("(-24.95,26.05,-24.55,26.55)", query)

    def test_fetch_region_refuses_bad_boxes_and_names_without_asking(self):
        def opener(request, timeout):
            raise AssertionError("no request expected")

        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            with self.assertRaisesRegex(ValueError, "bounding box larger than 4 square degrees"):
                fetch_region("big", (-26.0, 25.0, -23.0, 27.0), out, opener=opener)
            for bbox in ((-24.55, 26.05, -24.95, 26.55), (-24.95, 26.55, -24.55, 26.05)):
                with self.assertRaises(ValueError):
                    fetch_region("flip", bbox, out, opener=opener)
            with self.assertRaisesRegex(ValueError, "region name required"):
                fetch_region("..", (-24.95, 26.05, -24.55, 26.55), out, opener=opener)

    def test_fetch_region_reports_http_and_network_failures(self):
        def refused(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {}, None)

        def offline(request, timeout):
            raise urllib.error.URLError("timed out")

        def reset(request, timeout):
            raise ConnectionResetError(54, "Connection reset by peer")

        cases = ((lambda request, timeout: FakeResponse(b"{}", 504), "Overpass answered 504"),
                 (refused, "Overpass answered 429"), (offline, "Overpass answered timed out"),
                 (reset, "Overpass answered Connection reset by peer"))
        with tempfile.TemporaryDirectory() as directory:
            for opener, message in cases:
                with self.assertRaisesRegex(RuntimeError, message):
                    fetch_region("madikwe", (-24.95, 26.05, -24.55, 26.55), Path(directory), opener=opener)
            self.assertEqual([], list(Path(directory).iterdir()))

    def test_fetch_region_refuses_an_overpass_runtime_error_remark(self):
        body = json.dumps({"remark": "runtime error: Query timed out in \"query\" at line 1 after 91 seconds.",
                           "elements": OVERPASS["elements"][:1]}).encode()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "Overpass answered runtime error: Query timed out"):
                fetch_region("madikwe", (-24.95, 26.05, -24.55, 26.55), Path(directory),
                             opener=lambda request, timeout: FakeResponse(body))
            self.assertEqual([], list(Path(directory).iterdir()))


if __name__ == "__main__":
    unittest.main()
