"""Map layers from the photographer's files: GPX tracks and waypoints, GeoJSON features, and the geometry helpers
that decide what is worth drawing at a zoom level."""

from __future__ import annotations

import json
import sqlite3
import xml.etree.ElementTree as ET
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

GPX_NS = "{http://www.topografix.com/GPX/1/1}"
# Below these zooms a kind is clutter (every road in a country at once); kinds not listed always draw.
KIND_MIN_ZOOM = {"road": 6, "place": 5, "river": 3, "lake": 3}
# Natural Earth names its files by layer, so the file name is the only kind a bare NE download carries.
NE_FILE_KINDS = (
    ("coastline", "coastline"),
    ("admin_0_boundary", "border"),
    ("rivers", "river"),
    ("lakes", "lake"),
    ("roads", "road"),
    ("populated_places", "place"),
)
PARK_BOUNDARIES = frozenset({"protected_area", "national_park"})
# Stored source names are "<tag>/<file name>", the tag being the folder's place in MapIndex's source_dirs, so the
# same file name in two folders never collides and no directory reaches the index or the browser.
SOURCE_TAGS = ("base", "regions", "overlays")
# Lines are clipped to the viewport grown by this fraction of its size on each side, so a small pan has edges.
VIEWPORT_PAD = 0.1
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
    """Rounded copy of a GeoJSON coordinate array; TypeError when it is not nested lists of numbers."""
    if not isinstance(coordinates, list) or not coordinates:
        raise TypeError("coordinates must be non-empty lists")
    if isinstance(coordinates[0], (int, float)):
        return [round(float(value), 5) for value in coordinates]
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
        return float(element.get("lat")), float(element.get("lon"))
    except (TypeError, ValueError):
        raise ValueError(f"{element.tag.removeprefix(GPX_NS)} without a readable lat/lon") from None


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
            return kind
    if properties.get("boundary") in PARK_BOUNDARIES or properties.get("leisure") == "nature_reserve":
        return "park"
    if properties.get("waterway"):
        return "river"
    if properties.get("natural") == "water":
        return "lake"
    if properties.get("highway"):
        return "road"
    return "other"


def parse_geojson(text: str, *, source_name: str = "") -> list[Feature]:
    """Features of a FeatureCollection, kinded by an explicit `kind` property, then the Natural Earth file name,
    then OSM-style tags, else "other". Features without coordinates are skipped. Raises ValueError for anything
    that is not a FeatureCollection. A malformed feature is skipped so one bad record cannot sink a layer file."""
    data = json.loads(text)
    if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
        raise ValueError("not a GeoJSON FeatureCollection")
    features = []
    for item in data.get("features") or []:
        if not isinstance(item, dict):
            continue
        geometry = item.get("geometry")
        properties = item.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        if not isinstance(geometry, dict) or not isinstance(geometry.get("type"), str):
            continue
        try:
            coordinates = _rounded(geometry.get("coordinates"))
        except (TypeError, ValueError, IndexError, OverflowError):
            continue
        features.append(Feature(
            _kind(properties, source_name),
            str(properties.get("name") or ""),
            _rank(properties.get("scalerank")),
            {"type": geometry["type"], "coordinates": coordinates},
        ))
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


def _drawn(geometry: dict, step: float, box: tuple[float, float, float, float]) -> dict | None:
    """Geometry clipped to `box` (lines only: clipping a ring distorts its fill) and decimated by `step`; None when
    no part of a line touches the box. A clipped line can come back as a MultiLineString."""
    kind, coordinates = geometry["type"], geometry["coordinates"]
    if kind in ("LineString", "MultiLineString"):
        parts = [coordinates] if kind == "LineString" else coordinates
        runs = [decimate(run, step) for part in parts for run in _clip(part, box)]
        if not runs:
            return None
        return {"type": "LineString", "coordinates": runs[0]} if len(runs) == 1 else \
            {"type": "MultiLineString", "coordinates": runs}
    if kind == "Polygon":
        coordinates = [decimate(ring, step) for ring in coordinates]
    elif kind == "MultiPolygon":
        coordinates = [[decimate(ring, step) for ring in polygon] for polygon in coordinates]
    return {"type": kind, "coordinates": coordinates}


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
        except (ValueError, UnicodeDecodeError, OSError) as error:
            features, summary = [], {"error": str(error)}
        db = self._db
        db.execute("INSERT INTO sources(path, size, mtime_ns, summary) VALUES (?, ?, ?, ?)",
                   (name, stat.st_size, stat.st_mtime_ns, json.dumps(summary)))
        for feature in features:
            row = db.execute("INSERT INTO features(source, kind, name, rank, geometry) VALUES (?, ?, ?, ?, ?)",
                             (name, feature.kind, feature.name, feature.rank, json.dumps(feature.geometry)))
            db.execute("INSERT INTO features_rtree VALUES (?, ?, ?, ?, ?)", (row.lastrowid, *feature.bbox))
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
        seen, found = set(), []
        for west, east in spans:
            box = (min_lat, max_lat, west, east)
            rows = self._db.execute(
                "SELECT f.id, f.kind, f.name, f.rank, f.geometry FROM features_rtree r JOIN features f ON f.id = r.id "
                "WHERE r.max_lat >= ? AND r.min_lat <= ? AND r.max_lon >= ? AND r.min_lon <= ? ORDER BY f.id",
                box,
            )
            for row_id, kind, name, rank, geometry in rows:
                if row_id in seen or KIND_MIN_ZOOM.get(kind, 0) > zoom:
                    continue
                seen.add(row_id)
                drawn = _drawn(json.loads(geometry), step, _padded(box))
                if drawn is not None:
                    found.append({"kind": kind, "name": name, "rank": rank, "geometry": drawn})
        return found

    def summaries(self) -> list[dict]:
        return [{"name": path, "summary": json.loads(summary)}
                for path, summary in self._db.execute("SELECT path, summary FROM sources ORDER BY path")]
