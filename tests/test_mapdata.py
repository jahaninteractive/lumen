import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from lumen.mapdata import KIND_MIN_ZOOM, Feature, MapIndex, TrackPoint, decimate, parse_geojson, parse_gpx, pixel_degrees

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
        ]}
        self.assertEqual([f.name for f in parse_geojson(json.dumps(collection))], ["Cape Town"])

    def test_geojson_scalerank_falls_back_to_zero(self):
        ranks = [{"scalerank": "3.0"}, {"scalerank": "high"}, {"scalerank": None}, {}]
        collection = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": props, "geometry": {"type": "Point", "coordinates": [0, 0]}}
            for props in ranks]}
        self.assertEqual([f.rank for f in parse_geojson(json.dumps(collection))], [3, 0, 0, 0])

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
        self.assertEqual(KIND_MIN_ZOOM, {"road": 6, "place": 5, "river": 3, "lake": 3})
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


if __name__ == "__main__":
    unittest.main()
