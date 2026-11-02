# Map Terrain and Styles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Map tab gets land, sea names, named mountains, plains, deserts and peaks from five more Natural Earth layers, a Workbench / Natural dark / Natural light style select, and the frame dialog names the terrain a frame was shot in.

**Architecture:** `mapdata.parse_geojson` learns the five files (case-insensitive properties, a `featurecla` to kind table, MultiPolygons split one row per polygon) and `_drawn` clips polygon rings to the viewport with Sutherland-Hodgman so the land layer ships only what is on screen. `MapIndex.terrain_at` does an R-tree then even-odd point-in-polygon lookup; `Library.terrain_for` exposes it and `GET /api/photos/<id>` passes it as `photo.terrain`. The canvas gains a per-style palette, land fill, terrain tints and labels, and peaks.

**Tech Stack:** Python stdlib (`json`, `sqlite3` R-tree), vanilla JS canvas, zsh bootstrap.

**Spec:** `docs/superpowers/specs/2026-11-02-map-terrain-and-styles-design.md`

## Global Constraints

- Originals untouched; loopback only; strict JSON; no path, fingerprint or source name reaches the browser (features carry `kind`, `name`, `rank`, `geometry` only).
- Pillow is the only runtime Python dependency; the clipping and point-in-polygon code is stdlib arithmetic.
- Tests: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_<module> -v`; `node --check src/lumen/static/app.js`. Tests build fixtures and never read `vendor/data`.
- No AI vendor or model names and no scratchpad paths in tracked files or commit messages; the guide is `AGENTS.md`.
- Branch `feature/map-terrain`. 0.3.0 is still unreleased, so no version bump; add to the `## Unreleased` Map paragraph of `CHANGELOG.md` and to `docs/HANDOFF.md` in the last task.
- Nothing is downloaded: the five files are already in `vendor/downloads/natural-earth/` with the hashes in the spec. The server never contacts the internet for the map.
- Keyboard: the style select is a native `<select>`; the charcoal/amber/cyan workbench around the canvas is unchanged.

## Review Focus

- A polygon ring that crosses the viewport edge comes back closed and fills correctly; one wholly outside is dropped; a polygon whose outer ring is clipped away returns None. (Task 2 tests.)
- A land polygon reaching latitude -90 (Antarctica) or crossing the antimeridian: the clip keeps the ring within the box, and the query across 180 degrees returns it once. (Task 2 `test_query_clips_a_polygon_across_the_antimeridian_once`.)
- A region point exactly on a ring vertex or edge: `terrain_at` must not raise; either answer is fine. (Task 2 `test_terrain_at_tolerates_a_point_on_an_edge`.)
- A frame whose fix is in the sea or whose library has no index yet: `terrain` is absent, never an empty-list crash in the dialog. (Task 4 tests.)
- The regions file's upper-case `NAME`/`SCALERANK` and the points file's lower-case `featurecla` both kind correctly; a `Continent` feature never reaches the index. (Task 1 tests.)

---

### Task 1: New kinds and parsing in `mapdata`

**Files:**
- Modify: `src/lumen/mapdata.py` (`KIND_MIN_ZOOM`, `NE_FILE_KINDS`, `_kind`, `parse_geojson`, about lines 22-32 and 180-224)
- Test: `tests/test_mapdata.py` (`GeoJsonTests`)

**Interfaces:**
- Produces: kinds `land`, `sea`, `mountain`, `plain`, `desert`, `region`, `peak` from `parse_geojson(text, source_name=...)`; `TERRAIN_KINDS = frozenset({"mountain", "plain", "desert", "region"})`; `FEATURE_CLASSES: dict[str, str]` (lower-cased `featurecla` to kind); `DROPPED_CLASSES = frozenset({"continent", "dragons-be-here", "lake", "null island"})`.

- [ ] **Step 1: Write the failing tests** in `GeoJsonTests`:

```python
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

def test_border_file_is_not_caught_by_the_land_marker(self):
    border = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}}]}
    self.assertEqual(parse_geojson(json.dumps(border), source_name="ne_10m_admin_0_boundary_lines_land.geojson")[0].kind, "border")
```

Also extend `test_zoom_helpers` (or add `test_kind_min_zoom_for_terrain`) asserting `KIND_MIN_ZOOM` has `sea: 3`, `mountain: 3`, `plain: 3`, `desert: 3`, `region: 3`, `peak: 6` and no `land` entry.

- [ ] **Step 2: Run** `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_mapdata -v` — the new tests fail (kinds come back `other`, MultiPolygon unsplit).

- [ ] **Step 3: Implement in `mapdata.py`:**
  - Properties are read through a lower-cased copy: `props = {str(k).lower(): v for k, v in properties.items()}` once per feature, used for `name`, `scalerank`, `featurecla`, `elevation`, `kind`.
  - `NE_FILE_KINDS` gains, before the existing entries, `("geography_regions_elevation_points", "peak")`, `("geography_regions", "terrain")`, `("geography_marine", "sea")`, and after `admin_0_boundary`, `("ne_10m_land", "land")`. Keep the tuple ordered so the border file matches `admin_0_boundary` first.
  - `FEATURE_CLASSES` maps lower-cased classes: range/mtn, foothills, plateau → `mountain`; plain, lowland, basin, delta, valley, depression, tundra, wetlands → `plain`; desert → `desert`. A file-kind of `terrain` resolves through this table, else `region`.
  - `DROPPED_CLASSES` features are skipped in `parse_geojson` (any file).
  - `peak`: name is `f"{name}, {int(elevation)} m"` when `elevation` parses as a number, else `name`.
  - A `MultiPolygon` becomes one `Feature` per member polygon with `{"type": "Polygon", "coordinates": member}`; applies to every file (GPX is untouched).
  - `KIND_MIN_ZOOM` as in Step 1.

- [ ] **Step 4: Run the module's tests** — all pass, including the existing `test_geojson_kinds_by_property_filename_and_tags`.

- [ ] **Step 5: Commit** `feat: map index kinds land, sea, terrain and peaks from the Natural Earth physical layers`

---

### Task 2: Polygon clipping and `terrain_at`

**Files:**
- Modify: `src/lumen/mapdata.py` (`_drawn` about line 268; `MapIndex` about line 394)
- Test: `tests/test_mapdata.py` (`GeoJsonTests`, `MapIndexTests`)

**Interfaces:**
- Produces: `clip_ring(ring: list[list[float]], box: tuple[float, float, float, float]) -> list[list[float]]` (Sutherland-Hodgman against `(min_lat, max_lat, west, east)`; returns `[]` when fewer than three distinct vertices remain; a closed input stays closed); `MapIndex.terrain_at(lat: float, lon: float) -> list[str]` (names of `TERRAIN_KINDS` polygons containing the point, smallest bounding-box area first, duplicates removed, empty names skipped); `point_in_ring(lon, lat, ring) -> bool` even-odd.

- [ ] **Step 1: Write the failing tests:**

```python
# GeoJsonTests
def test_clip_ring_keeps_a_crossing_ring_closed_and_drops_an_outside_one(self):
    box = (0, 10, 0, 10)
    square = [[5, 5], [15, 5], [15, 15], [5, 15], [5, 5]]
    clipped = clip_ring(square, box)
    self.assertEqual(clipped[0], clipped[-1])
    self.assertEqual(sorted(map(tuple, clipped[:-1])), [(5, 5), (5, 10), (10, 5), (10, 10)])
    self.assertEqual(clip_ring([[20, 20], [30, 20], [30, 30], [20, 20]], box), [])
    inside = [[1, 1], [2, 1], [2, 2], [1, 1]]
    self.assertEqual(clip_ring(inside, box), inside)

def test_point_in_ring_even_odd(self):
    ring = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
    self.assertTrue(point_in_ring(5, 5, ring))
    self.assertFalse(point_in_ring(15, 5, ring))

# MapIndexTests (add a terrain layer file to setUp's base folder: a "mountain" Polygon "Waterberg" over
# lon 27-28 lat -25..-24 with a hole lon 27.4-27.6 lat -24.6..-24.4, a "plain" Polygon "Bushveld" over lon 25-30
# lat -27..-22, and a "land" Polygon over lon 10-40 lat -40..0)
def test_query_clips_polygons_to_the_padded_viewport_and_drops_those_outside(self):
    self.index.ensure()
    land = [f for f in self.index.query(-25, -24, 27, 28, zoom=10) if f["kind"] == "land"]
    self.assertEqual(len(land), 1)
    lons = [lon for lon, _ in land[0]["geometry"]["coordinates"][0]]
    self.assertTrue(min(lons) >= 26.8 and max(lons) <= 28.2)  # inside the 10% padded box
    self.assertEqual(land[0]["geometry"]["coordinates"][0][0], land[0]["geometry"]["coordinates"][0][-1])
    self.assertEqual([f for f in self.index.query(50, 51, 100, 101, zoom=10) if f["kind"] == "land"], [])

def test_query_clips_a_polygon_across_the_antimeridian_once(self):
    (self.base / "fiji.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"featurecla": "Land"}, "geometry": {"type": "Polygon",
         "coordinates": [[[179, -17], [180, -17], [180, -18], [179, -18], [179, -17]]]}}]}))
    (self.base / "fiji.geojson").rename(self.base / "ne_10m_land.geojson")
    self.index.ensure()
    found = [f for f in self.index.query(-19, -16, 178, -178, zoom=8) if f["kind"] == "land"]
    self.assertEqual(len(found), 1)

def test_terrain_at_names_containing_polygons_smallest_first(self):
    self.index.ensure()
    self.assertEqual(self.index.terrain_at(-24.2, 27.2), ["Waterberg", "Bushveld"])
    self.assertEqual(self.index.terrain_at(-24.5, 27.5), ["Bushveld"])  # inside the hole
    self.assertEqual(self.index.terrain_at(-30, 27), [])
    self.assertEqual(self.index.terrain_at(-24.2, 27.2), self.index.terrain_at(-24.2, 27.2))

def test_terrain_at_tolerates_a_point_on_an_edge(self):
    self.index.ensure()
    self.index.terrain_at(-24, 27)  # a vertex; must not raise
```

- [ ] **Step 2: Run** `tests.test_mapdata` — the new tests fail.

- [ ] **Step 3: Implement:** `clip_ring` clips successively against the four box edges (lon >= west, lon <= east, lat >= min_lat, lat <= max_lat), intersecting on `[lon, lat]`; close the output when the input was closed. `_drawn` for `Polygon`/`MultiPolygon`: clip each ring with `clip_ring` against `box` (already padded by the caller), drop empty rings, return None when the outer ring is empty, then decimate. `terrain_at`: `SELECT f.name, f.geometry, (r.max_lat-r.min_lat)*(r.max_lon-r.min_lon) AS area FROM features_rtree r JOIN features f ON f.id = r.id WHERE ? BETWEEN r.min_lat AND r.max_lat AND ? BETWEEN r.min_lon AND r.max_lon AND f.kind IN (...) ORDER BY area`, then `point_in_ring` over the outer ring and not over any hole (support `MultiPolygon` too for Overpass regions). Stdlib only.

- [ ] **Step 4: Run** `tests.test_mapdata` — all pass; also `tests.test_web` and `tests.test_library` still pass (the lake fixtures are now clipped).

- [ ] **Step 5: Commit** `feat: map polygons clipped to the viewport and a terrain lookup for a position`

---

### Task 3: Bootstrap pins the five layers and the real files are measured

**Files:**
- Modify: `scripts/bootstrap.sh:55-62` (`natural_earth_layers`), `:228` (prompt text)
- Test: `tests/test_scripts.py:48-54`

- [ ] **Step 1: Extend `test_bootstrap_pins_the_natural_earth_layers`** so the layer tuple also contains `ne_10m_land`, `ne_10m_geography_regions_polys`, `ne_10m_geography_regions_points`, `ne_10m_geography_regions_elevation_points`, `ne_10m_geography_marine_polys`, and assert `"about 100 MB"` is in `live`.

- [ ] **Step 2: Run** `tests.test_scripts` — fails.

- [ ] **Step 3: Add the five lines** to `natural_earth_layers` with the SHA-256s from the spec's table, and change the prompt to `(about 100 MB)`. Update the header comment if it counts layers.

- [ ] **Step 4: Run** `tests.test_scripts` — passes. Then, locally only (nothing fetched): `cp vendor/downloads/natural-earth/*.geojson vendor/data/map/` and verify with `shasum -a 256 -c` against the spec's hashes. Measure with a one-off script in the scratchpad (not tracked): build a `MapIndex` over `vendor/data/map` into a scratchpad sqlite, time `ensure()`, then time `query(-34.0, -33.8, 18.3, 18.6, zoom=12)` and `len(json.dumps(result))`. Acceptance: under 300 KB and 300 ms. Record both numbers for Task 5's handoff note. If over, stop and report the numbers instead of changing the design.

- [ ] **Step 5: Commit** `feat: bootstrap fetches the Natural Earth land, terrain, peak and sea-name layers`

---

### Task 4: Terrain reaches the frame dialog

**Files:**
- Modify: `src/lumen/library.py` (near `map_features`, about line 363), `src/lumen/web.py:446-468`, `src/lumen/static/app.js` (`locationLine`, about line 692)
- Test: `tests/test_library.py` (near `test_map_photos_omits_frames_without_a_fix_and_paths`, line 3683), `tests/test_web.py` (near `test_map_features_returns_viewport_features_only`, line 180)

**Interfaces:**
- Consumes: `MapIndex.terrain_at` (Task 2).
- Produces: `Library.terrain_for(photo) -> list[str] | None` — None when the photo has no fix, the library has no map index, or `settings.map_index_path` does not exist yet (no `ensure()` is called, so the dialog never waits on a build); `sqlite3.OperationalError` (a build mid-transaction on another connection) also returns None. `GET /api/photos/<id>` adds `photo.terrain` (a list of strings) only when `terrain_for` returned a list, and skips the lookup when `_MAP_BUILD_LOCK.locked()`.

- [ ] **Step 1: Write the failing tests.** In `tests/test_library.py`, following the fixture pattern of the existing map tests (a `map_dir` with a terrain layer like Task 2's, a photo with a fix inside "Waterberg"):

```python
def test_terrain_for_names_the_regions_round_a_fix_once_the_index_exists(self):
    # before any map request: no index file -> None
    self.assertIsNone(library.terrain_for(photo))
    library.map_features(-25, -24, 27, 28, zoom=10)  # builds the index
    self.assertEqual(library.terrain_for(photo), ["Waterberg", "Bushveld"])
    self.assertIsNone(library.terrain_for(unfixed_photo))
```

In `tests/test_web.py`: `test_photo_detail_carries_terrain_only_when_known` — `GET /api/photos/<id>` has no `terrain` key before `/api/map/features` has been called, and `["Waterberg", "Bushveld"]` after; the response still has none of `path`, `fingerprint`, `preview_path`, `mirror_path`.

- [ ] **Step 2: Run** `tests.test_library tests.test_web` — fail.

- [ ] **Step 3: Implement** `terrain_for` in `library.py` and the `web.py` pass-through (`terrain = None if _MAP_BUILD_LOCK.locked() else library.terrain_for(item["photo"])`, inside the `with Library(settings)` block; `if terrain is not None: photo["terrain"] = terrain`).

- [ ] **Step 4: `locationLine` in `app.js`:** build `const terrain = (record?.terrain || []).join(', ')`; the line becomes the non-empty parts of `[place, terrain]` joined with ` · `, then ` · ` and the degrees when there is a fix; with no fix and no place but terrain, show the terrain alone (a typed place without a fix cannot have terrain, so this only matters for robustness). Keep the existing texts `No location in the file` and `Location: ` for the cases without terrain. Run `node --check src/lumen/static/app.js`.

- [ ] **Step 5: Run** `tests.test_library tests.test_web` — pass.

- [ ] **Step 6: Commit** `feat: the frame dialog names the terrain round a frame's fix`

---

### Task 5: Map styles, natural colours, land, terrain and peaks on the canvas

**Files:**
- Modify: `src/lumen/static/index.html:229-234` (side column, before the Frames fieldset), `src/lumen/static/app.js` (`MAP_ORDER`, `mapColour`, `drawFeature`, `paintMap`, about lines 2540-2660, plus the `map-scope` wiring near line 3096), `src/lumen/static/styles.css` only if the select needs a rule beyond `.map-side select`
- Modify: `CHANGELOG.md` (Unreleased Map paragraph), `docs/HANDOFF.md`, `docs/BACKLOG.md` (drop the "Natural Earth `land` fill" entry), `AGENTS.md` (Map bullet: eleven layers, about 100 MB, the style select, `photo.terrain`)
- Test: `tests/test_web.py:1029` markup pin list

**Interfaces:**
- Consumes: feature kinds and `rank` from Task 1; clipped polygons from Task 2.
- Produces: `#map-style` select, values `workbench` | `natural-dark` | `natural-light`, stored in `localStorage` key `lumen-map-style`; `mapColour(name)` now answers palette names `sea`, `land`, `water`, `waterline`, `mountain`, `plain`, `desert`, `label`, `sealabel`, `line`, `muted`, `charcoal`, `graphite`, `amber`, `cyan`.

- [ ] **Step 1: Markup test.** Add `"map-style"` to the id list at `tests/test_web.py:1029` and assert the select has three `<option>`s with the values above. Run `tests.test_web` — fails.

- [ ] **Step 2: Add the control** to `index.html`: `<label>Map style <select id="map-style"><option value="workbench">Workbench</option><option value="natural-dark">Natural dark</option><option value="natural-light">Natural light</option></select></label>` as the first child of `.map-side`.

- [ ] **Step 3: Palette and style in `app.js`.** `mapStyle()` reads `localStorage` (try/catch, default `workbench`) and sets the select; a `change` listener stores the value, clears `mapColours`, and calls `drawMap()`. `mapColour` builds the palette for the current style: Workbench maps `sea`→`--charcoal`, `land`→`--graphite`, `water`→`--charcoal`, `waterline`→`--cyan`, `label`/`sealabel`→`--muted`, `mountain`/`plain`/`desert`→`null` (no fill), `line`→`--line`. Natural dark and Natural light are literal hex tables as the spec describes (deep blue / pale blue sea, dark warm-grey / cream land, lighter-blue water, brown / green / sand tints, muted / dark labels, paler-blue sea names); the exact values are the implementer's. Frames, clusters, fan, tracks, waypoints and the amber selection keep `--amber`, `--cyan`, `--charcoal` as today in all three styles.

- [ ] **Step 4: Drawing.** `MAP_ORDER = ['land', 'mountain', 'plain', 'desert', 'lake', 'coastline', 'border', 'river', 'road', 'park', 'sea', 'region', 'peak', 'track', 'waypoint', 'place']` (terrain Points share their polygon kinds and sort with them; a Point of kind `mountain`/`plain`/`desert`/`region` draws only its label). In `paintMap` the background fill is `mapColour('sea')`. In `drawFeature`:
  - `land`: trace, `fillStyle = mapColour('land')`, `fill('evenodd')`, no stroke.
  - `lake`: fill `water`, stroke `waterline` at the existing alpha; `river` strokes `waterline`.
  - `mountain`/`plain`/`desert` polygons: when the palette gives a colour, fill it at `globalAlpha` 0.18 (dark) / 0.28 (light), no stroke; then the label rule. `region` polygons: label only.
  - Label rule for any polygon of kind `sea`/`mountain`/`plain`/`desert`/`region`: compute the screen bounding box of the geometry as sent (already clipped); draw `feature.name` centred (`textAlign = 'center'`) at the box centre when `feature.rank <= zoom - 1` and the box's long side is at least 80 px; `sea` labels in `sealabel` and `italic 12px` font, the others in `label`. Reset `textAlign`/font after.
  - `peak` Point: a 5 px filled triangle (apex up) in `label`; from zoom 8 the name to its right as waypoints do.
  Keep `coastline`, `border`, `road`, `park`, `track`, `waypoint`, `place` as they are, with `line`/`muted` from the palette.

- [ ] **Step 5: `node --check src/lumen/static/app.js`** and run `tests.test_web` — pass.

- [ ] **Step 6: Look at it.** Start `uv run lumen serve` against the real library with the files copied in Task 3 and view the Map tab in each style at the Western Cape around zoom 6, 9 and 12: sea and land told apart, False Bay named over the water, the Cederberg or Karoo named over the land, Table Mountain's peak appearing from zoom 6 with its height from 8, frames and clusters unchanged. Switching style redraws without a reload; reloading keeps the choice. Note anything that looks wrong and fix it before the commit.

- [ ] **Step 7: Docs.** CHANGELOG Unreleased Map paragraph: eleven base layers (about 100 MB), land and sea apart, named ranges, plains, deserts, peaks and seas, the three map styles, terrain in the frame dialog. `docs/HANDOFF.md`: a 2026-11-02 terrain milestone with the Task 3 measurements. `docs/BACKLOG.md`: remove the land-fill entry. `AGENTS.md`: update the Map bullet.

- [ ] **Step 8: Full suite** `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest discover -s tests -v` — all pass. Commit `feat: map styles with natural colours, land fill, terrain names and peaks`.
