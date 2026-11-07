"""Map layers from the photographer's files: GPX tracks and waypoints, GeoJSON features, and the geometry helpers
that decide what is worth drawing at a zoom level. `fetch_region` is the one network call, made only when the
photographer runs `lumen map-region`; the web server never calls it."""

from __future__ import annotations

import gzip
import http.client
import json
import math
import sqlite3
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from . import __version__

GPX_NS = "{http://www.topografix.com/GPX/1/1}"
# Below these zooms a kind is clutter (every road in a country at once); kinds not listed always draw.
KIND_MIN_ZOOM = {"road": 6, "place": 5, "river": 3, "lake": 3, "sea": 3, "mountain": 3, "plain": 3, "desert": 3,
                 "region": 3, "peak": 6}
# Natural Earth names its files by layer, so the file name is the only kind a bare NE download carries. Order
# matters: the first marker found wins, so the border file ("..._boundary_lines_land") never reads as land.
NE_FILE_KINDS = (
    ("geography_regions_elevation_points", "peak"),
    ("geography_regions", "terrain"),
    ("geography_marine", "sea"),
    ("coastline", "coastline"),
    ("admin_0_boundary", "border"),
    ("ne_10m_land", "land"),
    ("rivers", "river"),
    ("lakes", "lake"),
    ("roads", "road"),
    ("populated_places", "place"),
)
TERRAIN_KINDS = frozenset({"mountain", "plain", "desert", "region"})
# A "terrain" file's features are kinded by their lower-cased Natural Earth `featurecla`, else "region".
FEATURE_CLASSES = {
    **dict.fromkeys(("range/mtn", "foothills", "plateau"), "mountain"),
    **dict.fromkeys(("plain", "lowland", "basin", "delta", "valley", "depression", "tundra", "wetlands"), "plain"),
    "desert": "desert",
}
# Classes skipped in any file but the lakes layer: too broad to label, drawn by another layer, or a joke.
DROPPED_CLASSES = frozenset({"continent", "dragons-be-here", "lake", "null island"})
PARK_BOUNDARIES = frozenset({"protected_area", "national_park"})
# Stored source names are "<tag>/<file name>", the tag being the folder's place in MapIndex's source_dirs, so the
# same file name in two folders never collides and no directory reaches the index or the browser.
SOURCE_TAGS = ("base", "regions", "overlays")
# Lines and polygons are clipped to the viewport grown by this fraction of its size on each side, so a small pan
# has edges.
VIEWPORT_PAD = 0.1
# Bumped when parsing changes what a file indexes as; an index of another format is emptied and rebuilt.
MAP_FORMAT = 2
MAP_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, summary TEXT);
CREATE TABLE IF NOT EXISTS features(id INTEGER PRIMARY KEY, source TEXT, kind TEXT, name TEXT, rank INTEGER,
    geometry TEXT);
CREATE INDEX IF NOT EXISTS features_source ON features(source);
CREATE VIRTUAL TABLE IF NOT EXISTS features_rtree USING rtree(id, min_lat, max_lat, min_lon, max_lon);
"""


@dataclass(frozen=True)
class Feature:
    kind: str
    name: str
    rank: int
    geometry: dict

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        """(min_lat, max_lat, min_lon, max_lon) over every vertex of the geometry."""
        positions = list(_positions(self.geometry["coordinates"]))
        lons = [p[0] for p in positions]
        lats = [p[1] for p in positions]
        return min(lats), max(lats), min(lons), max(lons)


@dataclass(frozen=True)
class TrackPoint:
    time: datetime  # timezone-aware UTC, so geotagging can offset camera-local times against it
    lat: float
    lon: float
    ele: float | None


def _positions(coordinates):
    if coordinates and isinstance(coordinates[0], (int, float)):
        yield coordinates
    else:
        for part in coordinates:
            yield from _positions(part)


def _rounded(coordinates):
    """Rounded copy of a GeoJSON coordinate array; TypeError when it is not nested lists of positions, each at
    least two finite numbers."""
    if not isinstance(coordinates, list) or not coordinates:
        raise TypeError("coordinates must be non-empty lists")
    if isinstance(coordinates[0], (int, float)):
        values = [float(value) for value in coordinates]
        if len(values) < 2 or not all(map(math.isfinite, values)):
            raise TypeError("a position needs two finite numbers")
        return [round(value, 5) for value in values]
    return [_rounded(part) for part in coordinates]


def _gpx_time(text: str) -> datetime:
    """UTC-aware whatever the file wrote: Z, an offset (converted), or no designator (GPX times are UTC)."""
    try:
        moment = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        raise ValueError(f"unreadable time {text!r}") from None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _lat_lon(element: ET.Element) -> tuple[float, float]:
    try:
        lat, lon = float(element.get("lat")), float(element.get("lon"))
    except (TypeError, ValueError):
        lat = lon = math.nan
    if not (math.isfinite(lat) and math.isfinite(lon)):
        raise ValueError(f"{element.tag.removeprefix(GPX_NS)} without a readable lat/lon")
    return lat, lon


def parse_gpx(text: str) -> tuple[list[Feature], list[TrackPoint]]:
    """Tracks and routes as one LineString each (segments joined), waypoints as Points, plus every timed track
    point in time order across all tracks and segments. Untimed points still draw but cannot date a photo, so they
    are left out of the points. Raises ValueError for anything that is not GPX 1.1."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise ValueError("not a GPX 1.1 document") from None
    if root.tag != GPX_NS + "gpx":
        raise ValueError("not a GPX 1.1 document")
    features: list[Feature] = []
    points: list[TrackPoint] = []
    lines = [(trk, trk.iter(GPX_NS + "trkpt")) for trk in root.iter(GPX_NS + "trk")]
    lines += [(rte, rte.iter(GPX_NS + "rtept")) for rte in root.iter(GPX_NS + "rte")]
    for number, (line, vertices) in enumerate(lines, start=1):
        coordinates = []
        for vertex in vertices:
            lat, lon = _lat_lon(vertex)
            coordinates.append([lon, lat])
            when = vertex.findtext(GPX_NS + "time")
            if line.tag == GPX_NS + "trk" and when:
                points.append(TrackPoint(_gpx_time(when.strip()), lat, lon, _ele(vertex)))
        if len(coordinates) >= 2:
            name = line.findtext(GPX_NS + "name") or f"Track {number}"
            features.append(Feature("track", name, 0, {"type": "LineString", "coordinates": _rounded(coordinates)}))
    for wpt in root.iter(GPX_NS + "wpt"):
        lat, lon = _lat_lon(wpt)
        position = [lon, lat]
        name = wpt.findtext(GPX_NS + "name") or ""
        features.append(Feature("waypoint", name, 0, {"type": "Point", "coordinates": _rounded(position)}))
    points.sort(key=lambda point: point.time)
    return features, points


def position_at(points: list[TrackPoint], when: datetime, max_gap: timedelta) -> tuple[float, float, float | None] | None:
    """(lat, lon, ele) of the time-sorted track at `when`: interpolated between the bracketing points when they are
    less than `max_gap` apart, else the nearer point when it is within `max_gap`, else None. Elevation is
    interpolated only when both points carry one."""
    index = bisect_left(points, when, key=lambda point: point.time)
    before = points[index - 1] if index else None
    after = points[index] if index < len(points) else None
    if before and after and after.time - before.time < max_gap:
        share = (when - before.time) / (after.time - before.time)
        ele = None if before.ele is None or after.ele is None else before.ele + (after.ele - before.ele) * share
        return (before.lat + (after.lat - before.lat) * share, before.lon + (after.lon - before.lon) * share, ele)
    nearest = min((p for p in (before, after) if p), key=lambda point: abs(point.time - when), default=None)
    if nearest is None or abs(nearest.time - when) > max_gap:
        return None
    return nearest.lat, nearest.lon, nearest.ele


def _ele(vertex: ET.Element) -> float | None:
    text = vertex.findtext(GPX_NS + "ele")
    try:
        return float(text) if text else None
    except ValueError:
        raise ValueError(f"unreadable elevation {text!r}") from None


def _rank(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _kind(properties: dict, source_name: str) -> str:
    if properties.get("kind"):
        return str(properties["kind"])
    for marker, kind in NE_FILE_KINDS:
        if marker in source_name:
            if kind == "terrain":
                return FEATURE_CLASSES.get(str(properties.get("featurecla") or "").lower(), "region")
            return kind
    if properties.get("boundary") in PARK_BOUNDARIES or properties.get("leisure") == "nature_reserve":
        return "park"
    if properties.get("waterway"):
        return "river"
    if properties.get("natural") == "water":
        return "lake"
    if properties.get("natural") == "coastline":
        return "coastline"
    if properties.get("highway"):
        return "road"
    return "other"


def parse_geojson(text: str, *, source_name: str = "") -> list[Feature]:
    """Features of a FeatureCollection, kinded by an explicit `kind` property, then the Natural Earth file name,
    then OSM-style tags, else "other". Property names are read case-insensitively. Features without coordinates,
    and features of a DROPPED_CLASSES class outside the lakes layer, are skipped; a MultiPolygon becomes one
    Polygon feature per member. Raises ValueError for anything that is not a FeatureCollection. A malformed
    feature is skipped so one bad record cannot sink a layer file."""
    data = json.loads(text)
    if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
        raise ValueError("not a GeoJSON FeatureCollection")
    features = []
    for item in data.get("features") or []:
        if not isinstance(item, dict):
            continue
        geometry = item.get("geometry")
        properties = item.get("properties")
        props = {str(k).lower(): v for k, v in properties.items()} if isinstance(properties, dict) else {}
        if not isinstance(geometry, dict) or not isinstance(geometry.get("type"), str):
            continue
        kind = _kind(props, source_name)
        # The lakes layer's own class is "Lake"; only the physical files' lake labels are dropped.
        if kind != "lake" and str(props.get("featurecla") or "").lower() in DROPPED_CLASSES:
            continue
        try:
            coordinates = _rounded(geometry.get("coordinates"))
        except (TypeError, ValueError, IndexError, OverflowError):
            continue
        name = str(props.get("name") or "")
        if name.isupper() and props.get("name_en"):  # Natural Earth spells some regions in capitals ("NAMIB DESERT")
            name = str(props["name_en"])
        if kind == "peak":
            try:
                elevation = f"{int(float(props['elevation']))} m"
                name = f"{name}, {elevation}" if name else elevation
            except (KeyError, TypeError, ValueError, OverflowError):
                pass
        rank = _rank(props.get("scalerank"))
        if geometry["type"] == "MultiPolygon":
            features.extend(Feature(kind, name, rank, {"type": "Polygon", "coordinates": member})
                            for member in coordinates)
        else:
            features.append(Feature(kind, name, rank, {"type": geometry["type"], "coordinates": coordinates}))
    return features


def decimate(coords: list[list[float]], step: float) -> list[list[float]]:
    """Drops vertices closer than `step` degrees (in both lon and lat) to the last kept one; the first and last
    vertices always survive so a line keeps its ends."""
    if len(coords) < 2:
        return list(coords)
    kept = [coords[0]]
    for vertex in coords[1:-1]:
        last = kept[-1]
        if abs(vertex[0] - last[0]) >= step or abs(vertex[1] - last[1]) >= step:
            kept.append(vertex)
    kept.append(coords[-1])
    return kept


def pixel_degrees(zoom: float) -> float:
    """Degrees of longitude one 256-pixel-tile pixel spans at `zoom`."""
    return 360 / (256 * 2 ** zoom)


def _segment_hits(a: list[float], b: list[float], box: tuple[float, float, float, float]) -> bool:
    min_lat, max_lat, west, east = box
    return (min(a[0], b[0]) <= east and max(a[0], b[0]) >= west
            and min(a[1], b[1]) <= max_lat and max(a[1], b[1]) >= min_lat)


def _clip(coords: list[list[float]], box: tuple[float, float, float, float]) -> list[list[list[float]]]:
    """Runs of the line whose segments touch `box`, so a coastline 4,000 km long ships only the stretch on screen.
    A segment counts when its bounding box touches, which keeps the first vertex outside at each end of a run. The
    line is split where it leaves the box and comes back, so no straight chord is drawn across the viewport."""
    runs, run = [], []
    for a, b in zip(coords, coords[1:]):
        if _segment_hits(a, b, box):
            if not run:
                run = [a]
            run.append(b)
        elif run:
            runs.append(run)
            run = []
    if run:
        runs.append(run)
    return runs


def clip_ring(ring: list[list[float]], box: tuple[float, float, float, float]) -> list[list[float]]:
    """Sutherland-Hodgman clip of a `[lon, lat]` ring to `box` (min_lat, max_lat, west, east). The clipped ring
    runs along the box edge where the original left it, so its fill stays inside; [] when fewer than three distinct
    vertices remain. A closed ring comes back closed."""
    min_lat, max_lat, west, east = box
    closed = len(ring) > 1 and ring[0] == ring[-1]
    points = ring[:-1] if closed else list(ring)
    for axis, bound, sign in ((0, west, 1), (0, east, -1), (1, min_lat, 1), (1, max_lat, -1)):
        clipped = []
        for index, current in enumerate(points):
            previous = points[index - 1]
            current_in, previous_in = sign * (current[axis] - bound) >= 0, sign * (previous[axis] - bound) >= 0
            if current_in != previous_in:
                t = (bound - previous[axis]) / (current[axis] - previous[axis])
                crossing = [previous[0] + t * (current[0] - previous[0]), previous[1] + t * (current[1] - previous[1])]
                crossing[axis] = bound
                clipped.append([round(value, 5) for value in crossing])
            if current_in:
                clipped.append(current)
        points = clipped
    if len({tuple(point) for point in points}) < 3:
        return []
    return points + [points[0]] if closed else points


def point_in_ring(lon: float, lat: float, ring: list[list[float]]) -> bool:
    """Even-odd test of a position against a `[lon, lat]` ring; a third (altitude) value is ignored."""
    inside = False
    for p, q in zip(ring, ring[1:] + ring[:1]):
        x1, y1, x2, y2 = p[0], p[1], q[0], q[1]
        if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def _drawn(geometry: dict, step: float, box: tuple[float, float, float, float]) -> dict | None:
    """Geometry clipped to `box` and decimated by `step`; None when nothing of it is left in the box. A clipped
    line can come back as a MultiLineString; a polygon's rings are clipped along the box edge and empty holes
    dropped."""
    kind, coordinates = geometry["type"], geometry["coordinates"]
    if kind in ("LineString", "MultiLineString"):
        parts = [coordinates] if kind == "LineString" else coordinates
        runs = [decimate(run, step) for part in parts for run in _clip(part, box)]
        if not runs:
            return None
        return {"type": "LineString", "coordinates": runs[0]} if len(runs) == 1 else \
            {"type": "MultiLineString", "coordinates": runs}
    if kind in ("Polygon", "MultiPolygon"):
        polygons = []
        for polygon in [coordinates] if kind == "Polygon" else coordinates:
            rings = [clip_ring(ring, box) for ring in polygon]
            if rings and rings[0]:
                polygons.append([decimate(ring, step) for ring in rings if ring])
        if not polygons:
            return None
        coordinates = polygons[0] if kind == "Polygon" else polygons
    return {"type": kind, "coordinates": coordinates}


# Single and multi forms of the geometries a viewport across 180 degrees can split in two.
MERGED_TYPES = {"LineString": "MultiLineString", "MultiLineString": "MultiLineString",
                "Polygon": "MultiPolygon", "MultiPolygon": "MultiPolygon"}


def _merged(first: dict, second: dict) -> dict:
    """One multi geometry holding the parts of two drawn halves of the same line or polygon."""
    parts = []
    for geometry in (first, second):
        single = MERGED_TYPES[geometry["type"]] != geometry["type"]
        parts.extend([geometry["coordinates"]] if single else geometry["coordinates"])
    return {"type": MERGED_TYPES[first["type"]], "coordinates": parts}


def _padded(box: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    min_lat, max_lat, west, east = box
    lat_pad, lon_pad = (max_lat - min_lat) * VIEWPORT_PAD, (east - west) * VIEWPORT_PAD
    return min_lat - lat_pad, max_lat + lat_pad, west - lon_pad, east + lon_pad


def _parse_source(path: Path) -> tuple[list[Feature], dict]:
    """Features of one layer file plus its summary; ValueError when the file is unreadable as its type."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".gpx":
        features, points = parse_gpx(text)
        span = [points[0].time.isoformat(), points[-1].time.isoformat()] if points else None
    else:
        features, span = parse_geojson(text, source_name=path.name), None
    return features, {
        "features": len(features),
        "tracks": sum(f.kind == "track" for f in features),
        "waypoints": sum(f.kind == "waypoint" for f in features),
        "span": span,
    }


class MapIndex:
    """SQLite index over the GeoJSON and GPX files in `source_dirs`, reparsing a file only when it changes."""

    def __init__(self, index_path: Path, source_dirs: list[Path]):
        self.index_path = Path(index_path)
        self.source_dirs = [Path(d) for d in source_dirs]
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> "MapIndex":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    @property
    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(self.index_path)
            self._connection.executescript(MAP_SCHEMA)
            if self._connection.execute("PRAGMA user_version").fetchone()[0] != MAP_FORMAT:
                with self._connection:  # the manifest keys on size and mtime, which a parser change does not touch
                    for table in ("sources", "features", "features_rtree"):
                        self._connection.execute(f"DELETE FROM {table}")
                    self._connection.execute(f"PRAGMA user_version = {MAP_FORMAT}")
        return self._connection

    def _source_files(self) -> dict[str, Path]:
        files = {}
        for tag, folder in zip(SOURCE_TAGS, self.source_dirs):
            if folder.is_dir():
                for path in sorted([*folder.glob("*.geojson"), *folder.glob("*.gpx")]):
                    files[f"{tag}/{path.name}"] = path
        return files

    def _forget(self, name: str) -> None:
        db = self._db
        db.execute("DELETE FROM features_rtree WHERE id IN (SELECT id FROM features WHERE source = ?)", (name,))
        db.execute("DELETE FROM features WHERE source = ?", (name,))
        db.execute("DELETE FROM sources WHERE path = ?", (name,))

    def _load(self, name: str, path: Path, stat) -> bool:
        """Indexes one file; a file that fails to parse is still recorded, with its error as the summary."""
        try:
            features, summary = _parse_source(path)
            boxes = [feature.bbox for feature in features]
        except OSError as error:  # its str() names the absolute path, which must not reach the browser
            features, boxes, summary = [], [], {"error": error.strerror or type(error).__name__}
        except (ValueError, UnicodeDecodeError, IndexError, TypeError) as error:
            features, boxes, summary = [], [], {"error": str(error)}
        db = self._db
        db.execute("INSERT INTO sources(path, size, mtime_ns, summary) VALUES (?, ?, ?, ?)",
                   (name, stat.st_size, stat.st_mtime_ns, json.dumps(summary)))
        for feature, box in zip(features, boxes):
            row = db.execute("INSERT INTO features(source, kind, name, rank, geometry) VALUES (?, ?, ?, ?, ?)",
                             (name, feature.kind, feature.name, feature.rank, json.dumps(feature.geometry)))
            db.execute("INSERT INTO features_rtree VALUES (?, ?, ?, ?, ?)", (row.lastrowid, *box))
        return "error" not in summary

    def ensure(self) -> dict:
        files = self._source_files()
        db = self._db
        stored = {path: (size, mtime) for path, size, mtime in db.execute("SELECT path, size, mtime_ns FROM sources")}
        reparsed, failed = [], []
        with db:
            for name in stored.keys() - files.keys():
                self._forget(name)
            for name, path in files.items():
                try:
                    stat = path.stat()
                except FileNotFoundError:
                    continue  # vanished between the listing and now; the next ensure() drops its rows
                if stored.get(name) == (stat.st_size, stat.st_mtime_ns):
                    continue
                self._forget(name)
                reparsed.append(name)
                if not self._load(name, path, stat):
                    failed.append(name)
        return {
            "sources": db.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
            "features": db.execute("SELECT COUNT(*) FROM features").fetchone()[0],
            "reparsed": reparsed,
            "failed": failed,
        }

    def query(self, min_lat: float, max_lat: float, min_lon: float, max_lon: float, *, zoom: float) -> list[dict]:
        """Features drawable at `zoom` that intersect the viewport, decimated to about one vertex per pixel.
        `min_lon > max_lon` is a viewport across 180 degrees."""
        spans = [(min_lon, max_lon)] if min_lon <= max_lon else [(min_lon, 180.0), (-180.0, max_lon)]
        step = pixel_degrees(zoom)
        seen, found, entries = set(), [], {}
        for west, east in spans:
            box = (min_lat, max_lat, west, east)
            rows = self._db.execute(
                "SELECT f.id, f.kind, f.name, f.rank, f.geometry FROM features_rtree r JOIN features f ON f.id = r.id "
                "WHERE r.max_lat >= ? AND r.min_lat <= ? AND r.max_lon >= ? AND r.min_lon <= ? ORDER BY f.id",
                box,
            )
            for row_id, kind, name, rank, geometry in rows:
                if KIND_MIN_ZOOM.get(kind, 0) > zoom or (kind in ("sea", "region") and rank > zoom - 1):
                    continue  # sea and region are never filled, and the browser names them only at rank <= zoom - 1
                geometry = json.loads(geometry)
                # A row met again in the second span is a line or polygon across 180 degrees: clip it to this span
                # too and merge, so the half beyond 180 is not lost. A point is whole the first time.
                if row_id in seen and geometry["type"] not in MERGED_TYPES:
                    continue
                seen.add(row_id)
                drawn = _drawn(geometry, step, _padded(box))
                if drawn is None:
                    continue
                if row_id in entries:
                    entries[row_id]["geometry"] = _merged(entries[row_id]["geometry"], drawn)
                else:
                    entries[row_id] = {"kind": kind, "name": name, "rank": rank, "geometry": drawn}
                    found.append(entries[row_id])
        return found

    def terrain_at(self, lat: float, lon: float) -> list[str]:
        """Names of the terrain polygons containing the position, smallest bounding box first, each once."""
        rows = self._db.execute(
            "SELECT f.name, f.geometry, (r.max_lat - r.min_lat) * (r.max_lon - r.min_lon) AS area "
            "FROM features_rtree r JOIN features f ON f.id = r.id "
            "WHERE ? BETWEEN r.min_lat AND r.max_lat AND ? BETWEEN r.min_lon AND r.max_lon "
            f"AND f.kind IN ({', '.join('?' * len(TERRAIN_KINDS))}) ORDER BY area, f.id",
            (lat, lon, *sorted(TERRAIN_KINDS)),
        )
        names = []
        for name, geometry, _area in rows:
            geometry = json.loads(geometry)
            if not name or name in names or geometry["type"] not in ("Polygon", "MultiPolygon"):
                continue
            polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
            if any(point_in_ring(lon, lat, polygon[0])
                   and not any(point_in_ring(lon, lat, hole) for hole in polygon[1:]) for polygon in polygons):
                names.append(name)
        return names

    def is_empty(self) -> bool:
        """True while no source is indexed, as a format change leaves the index until the next ensure()."""
        return self._db.execute("SELECT 1 FROM sources LIMIT 1").fetchone() is None

    def summaries(self) -> list[dict]:
        return [{"name": path, "summary": json.loads(summary)}
                for path, summary in self._db.execute("SELECT path, summary FROM sources ORDER BY path")]


# Overpass regions

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# Overpass is a shared public service; a bigger box is a country-sized query it will time out on anyway.
MAX_REGION_SQUARE_DEGREES = 4
# Overpass's front end refuses generic agents and OSM policy asks clients to name the app and a contact.
OVERPASS_USER_AGENT = f"Lumen/{__version__} (+https://github.com/jahaninteractive/lumen)"


def overpass_query(south: float, west: float, north: float, east: float) -> str:
    box = f"({south},{west},{north},{east})"
    return ("[out:json][timeout:90];("
            f'way["boundary"~"^(protected_area|national_park)$"]{box};'
            f'relation["boundary"~"^(protected_area|national_park)$"]{box};'
            f'way["leisure"="nature_reserve"]{box};'
            f'relation["leisure"="nature_reserve"]{box};'
            f'way["highway"~"^(motorway|trunk|primary|secondary|tertiary|unclassified|track)$"]{box};'
            f'way["waterway"~"^(river|stream)$"]{box};'
            f'way["natural"="water"]{box};'
            f'way["natural"="coastline"]{box};'
            ");out geom;")


def _overpass_line(geometry) -> list[list[float]]:
    return [[point["lon"], point["lat"]] for point in geometry or []]


def overpass_to_geojson(payload: dict) -> dict:
    """A FeatureCollection of an `out geom` answer: ways as lines (closed water, boundary or leisure ways as
    polygons), relations as the MultiLineString of their outer members. Properties are the OSM tags plus `kind`
    and `name`."""
    features = []
    for element in payload.get("elements") or []:
        tags = element.get("tags") or {}
        if element.get("type") == "way":
            line = _overpass_line(element.get("geometry"))
            if len(line) < 2:
                continue
            # A closed water, boundary or leisure way is an area; any other closed way (a ring road) stays a line.
            area = line[0] == line[-1] and (tags.get("natural") == "water" or "boundary" in tags or "leisure" in tags)
            geometry = {"type": "Polygon", "coordinates": [line]} if area else {"type": "LineString", "coordinates": line}
        elif element.get("type") == "relation":
            lines = [_overpass_line(member["geometry"]) for member in element.get("members") or []
                     if member.get("role") == "outer" and member.get("geometry")]
            if not lines:
                continue
            geometry = {"type": "MultiLineString", "coordinates": lines}
        else:
            continue
        properties = {**tags, "kind": _kind(tags, ""), "name": tags.get("name", "")}
        features.append({"type": "Feature", "properties": properties, "geometry": geometry})
    return {"type": "FeatureCollection", "features": features}


def fetch_region(name: str, bbox: tuple[float, float, float, float], out_dir: Path, *, opener=None,
                 timeout: float = 120.0) -> Path:
    """Asks Overpass for the park, road, water and coastline detail inside `bbox` (south, west, north, east) and writes it to
    `out_dir/<name>.geojson`. Raises ValueError for a bad name or box and RuntimeError when Overpass fails."""
    south, west, north, east = bbox
    if not (south < north and west < east):
        raise ValueError("bounding box must be south,west,north,east with south < north and west < east")
    if (north - south) * (east - west) > MAX_REGION_SQUARE_DEGREES:
        raise ValueError("bounding box larger than 4 square degrees")
    stem = Path(name).name.lstrip(".").removesuffix(".geojson")
    if not stem:
        raise ValueError("region name required")
    request = urllib.request.Request(OVERPASS_URL, data=urlencode({"data": overpass_query(*bbox)}).encode(),
                                     headers={"User-Agent": OVERPASS_USER_AGENT, "Accept-Encoding": "gzip"}, method="POST")
    try:
        with (opener or urllib.request.urlopen)(request, timeout=timeout) as response:
            status = getattr(response, "status", None) or response.getcode()
            body = response.read()
            if response.headers.get("Content-Encoding") == "gzip":  # Overpass JSON shrinks several-fold
                try:
                    body = gzip.decompress(body)
                except (EOFError, zlib.error):
                    raise RuntimeError(f"Overpass answered incomplete: the connection dropped after {len(body)} bytes") from None
    except urllib.error.HTTPError as error:
        error.close()
        raise RuntimeError(f"Overpass answered {error.code}") from None
    except urllib.error.URLError as error:
        raise RuntimeError(f"Overpass answered {error.reason}") from None
    except http.client.IncompleteRead as error:  # the body was cut short; a chunked read raises this, not OSError
        raise RuntimeError(f"Overpass answered incomplete: the connection dropped after {len(error.partial)} bytes") from None
    except OSError as error:  # a timeout or reset during the read is not wrapped in URLError
        raise RuntimeError(f"Overpass answered {error.strerror or str(error) or 'timed out'}") from None
    if status != 200:
        raise RuntimeError(f"Overpass answered {status}")
    try:
        payload = json.loads(body)
    except ValueError:  # a body cut short mid-JSON, gzipped or not
        raise RuntimeError(f"Overpass answered incomplete: the connection dropped after {len(body)} bytes") from None
    if not isinstance(payload, dict):
        raise RuntimeError("Overpass answered something other than a JSON object")
    remark = str(payload.get("remark", ""))
    if "error" in remark.lower():  # a query timeout or memory limit arrives as a 200 with partial elements
        raise RuntimeError(f"Overpass answered {remark}")
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{stem}.geojson"
    partial = target.with_name(target.name + ".part")
    try:
        partial.write_text(json.dumps(overpass_to_geojson(payload)), encoding="utf-8")
        partial.replace(target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return target
