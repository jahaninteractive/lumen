# Map: where the frames were taken, with GPS tracks over an offline base map

Date: 2026-11-01. Decided in chat with the photographer. Trigger: a Madikwe game drive on 20 Oct 2026 with the phone's GPS tracker running (MotionX, GPX 1.1) and the camera fixing most frames, but no way to see the two together.

## Intent

A Map tab that shows where the catalogued frames were taken, on a world map that works with no network, with the photographer's GPX tracks and waypoints drawn over it. Frames the camera did not fix can be placed on a track by capture time. "Topo" was discussed and settled as **boundaries, rivers, lakes, roads, coastline, place names**; no terrain shading (elevation data is hundreds of MB per country and does not survive the photographer's 400 KB/s satellite link).

## Goals

1. **Base map, offline, installed by bootstrap.** `scripts/bootstrap.sh` fetches Natural Earth 1:10m GeoJSON layers into `vendor/data/map/` the same way it fetches GeoNames: optional, asked before downloading (`--yes` takes it, `--no-download` skips it), each file pinned to one commit of `nvkelso/natural-earth-vector` on GitHub with a SHA-256, resumable, a failed layer listed under Skipped. Layers and sizes as measured on 2026-11-01 at commit `ca96624a`:

   | layer | file | bytes |
   |---|---|---|
   | coastline | `ne_10m_coastline.geojson` | 10.1 MB |
   | country borders | `ne_10m_admin_0_boundary_lines_land.geojson` | 2.3 MB |
   | rivers | `ne_10m_rivers_lake_centerlines.geojson` | 7.3 MB |
   | lakes | `ne_10m_lakes.geojson` | 5.0 MB |
   | roads | `ne_10m_roads.geojson` | 50.5 MB |
   | place names | `ne_10m_populated_places_simple.geojson` | 4.9 MB |

   About 80 MB total, roughly four minutes on the photographer's link. Env `LUMEN_MAP_DIR` (`Settings.map_dir`, default `vendor/data/map/`, `None` for plain `Settings(...)`) names the folder, mirroring `geonames_dir`.

2. **Map index.** `src/lumen/mapdata.py` builds `<library>/cache/map.sqlite` from every `*.geojson` in the map folder (installed base layers), `<library>/map/regions/` (goal 6) and `<library>/map/overlays/` (goal 4). The manifest is **per source file** (path, size, mtime in a `sources` table), so a dropped GPX or a fetched region adds or replaces only its own rows and never reparses the 50 MB roads file. The build runs lazily on the first map request under the mutation lock, and `GET /api/map/features` answers `{"building": true}` while another thread holds it so the tab can show "Building map index…" instead of a second rebuild; bootstrap does not pre-build (it has no library). One row per feature: `layer`, `kind` (coastline, border, river, lake, road, place, park, track, waypoint, other), `name`, `rank` (road/river importance where the source has it), the coordinates as compact JSON with 5-decimal rounding, and an R-tree on the feature's bounding box. `GET /api/map/features?bbox=S,W,N,E&zoom=Z` returns the features whose box intersects the viewport, decimated server side so no two consecutive vertices are closer than about one screen pixel at that zoom, and drops layers that are noise at that scale (roads and place names below a zoom threshold the builder picks; everything above ~500 km view is coastline, borders, lakes and major rivers). Response is GeoJSON-shaped JSON with only `kind`, `name`, `rank`, `coordinates`; no source paths. Features are rendered on a `<canvas>` in Web Mercator in `app.js` with no map library (none is installed and the server stays Pillow-only).

3. **Frames on the map.** The tab has a two-way toggle, **Shown frames** (whatever the Review tab's current filter holds, so after an import it is the new batch) and **Whole catalog**. `GET /api/map/photos?scope=shown|all` (shown re-sends the Review tab's current filter parameters) returns `id`, `latitude`, `longitude`, `captured_at`, `place`, `decision`, `rating`, `thumb_url` for every frame with a fix, no paths. Frames cluster when closer than ~24 px: a dot with a count. A lone frame, or a cluster at a zoom where its members separate, draws its thumb from the existing `/thumb/<id>` route (every catalogued frame has a managed preview, so frames from any past import show a thumb, not only the current batch). Click or Enter on a frame opens the usual frame dialog; click on a cluster zooms into it. Frames without a fix are listed in a side panel as "n frames have no position" with the geotag affordance from goal 5. The map tab also opens from the frame dialog's coordinates (a "Show on map" link beside the place) centred on that frame.

4. **GPX and GeoJSON overlays.** The tab accepts drops and an Add button for `.gpx` and `.geojson` files; `POST /api/map/overlays` (multipart or JSON body with the file text, builder's call, strict validation: XML must parse as GPX 1.1 `http://www.topografix.com/GPX/1/1`, JSON must be a FeatureCollection, 20 MB cap) copies the file into `<library>/map/overlays/<sanitised name>` and reindexes. GPX parsing is `xml.etree` only: `<trk>/<trkseg>/<trkpt>` with `lat`, `lon`, `<ele>`, `<time>`; `<wpt>` with `lat`, `lon`, `<name>`, `<time>`; `<rte>` drawn as a track; `<extensions>` ignored. Tracks draw as a line in the workbench amber, waypoints as pins with their name, GeoJSON features styled by `kind` (a feature whose properties carry `boundary=protected_area`, `leisure=nature_reserve` or `boundary=national_park` is kind `park`, drawn as a dashed outline). The overlay list in the side panel shows each file's name, feature counts, time span and a Remove button (`DELETE /api/map/overlays/<name>`, deletes only inside the overlays folder). Originals never move: the drop copies.

5. **Geotag from a track.** For a track with timestamps, the side panel offers **Place frames on this track**. It takes a UTC offset for the camera clock (default: at dry-run time the server reads `OffsetTimeOriginal` live with exiftool from the first candidate frame whose original is reachable, else the Mac's current zone; no catalog column; editable as `+02:00`) and a maximum gap (default 10 minutes), and shows live how many of the shown frames without a fix fall inside the track's span: `POST /api/map/overlays/<name>/geotag` with `{scope, offset, max_gap_minutes, dry_run}`. Capture times are stored naive camera-local (`previews._normalize_exif_date`), so the offset is applied to the frame, the track's `<time>` is UTC, and the frame takes the position interpolated between the two bracketing track points. Writing goes through the existing typed-position path: the same `location_set` provenance event `Library.set_location` appends, with `source: "track", overlay: <name>`, so `Library.location_typed` makes it editable and removable exactly like a typed fix, and `_resolve_place` runs so the frame gets its place name. Never overwrites a camera fix; a frame that already has a typed position is skipped unless `replace: true`. The result reports placed, skipped, and out-of-span counts, and the browser announces it.

6. **Regional detail fetch, opt-in CLI.** `lumen map-region <name> --bbox S,W,N,E` (also `scripts/bootstrap.sh` help text names it) queries the public Overpass API (`https://overpass-api.de/api/interpreter`) once for the box: `boundary=protected_area|national_park`, `leisure=nature_reserve`, `highway` in `motorway|trunk|primary|secondary|tertiary|unclassified|track`, `waterway=river|stream`, `natural=water`, with `out geom;` so ways arrive with inline coordinates and no node assembly is needed. Converts the Overpass JSON to a GeoJSON FeatureCollection with stdlib only (ways to LineString, closed ways with area tags to Polygon, relations' outer members to MultiLineString; good enough to draw, not a topology-correct multipolygon builder) and writes `<library>/map/regions/<name>.geojson`, which the index picks up on the next request. Box area capped at 4 square degrees, 120 s timeout, one request, user-agent names Lumen, clear error naming the Overpass status on failure. This is a photographer-invoked command like bootstrap's downloads; the web server never calls it. The install test is the Madikwe box: `lumen map-region madikwe --bbox -24.95,26.05,-24.55,26.55`.

7. **Accessibility and the workbench.** Canvas has `role="img"` with an `aria-label` that states the centre, scale and counts; the side panel is the accessible equivalent: a list of overlays and a list of frames in view with place and time, each a native button that opens the frame. Keyboard: arrows pan, `+`/`-` zoom, `0` fits the shown frames, `Tab` moves through the side panel. Live announcements for geotag results and overlay add/remove. Charcoal/amber/cyan: land charcoal, sea a darker charcoal, coast and borders mid grey, water cyan, roads a dim warm grey, tracks amber, frames cyan dots with amber for the selected frame. A scale bar bottom-left.

## Non-goals

Terrain shading or contour lines. Online tile servers. Editing or simplifying tracks. Routing. Printing. Time-scrubbing along a track (listed in `docs/BACKLOG.md`). Region downloads for anything other than the explicit bounding box. Natural Earth `land` polygon fill (the coastline plus a solid background gives the land/sea distinction at the cost of 10 MB fewer; revisit if the fill is missed).

## Anti-goals

The web server contacting the internet. Source paths, fingerprints or absolute paths in map responses. Touching a camera fix. A geotag that silently assumes the wrong clock offset: the dry-run count and the offset field are shown before anything writes, and every write is a `location_set` event that the existing typed-position editing can undo.

## Constraints

Pillow only runtime dependency (`sqlite3`, `xml.etree`, `json`, `urllib`, `math` are stdlib). Loopback server, host/origin and strict JSON on every mutation route. Warning-strict suite green; `node --check`. Tests build tiny GeoJSON and GPX fixtures and never read `vendor/data`; the Overpass client is tested with a patched `urlopen`. Bootstrap `--check` output is pinned by `tests/test_scripts.py` and must not change. Bump version and add a `CHANGELOG.md` line. Update `docs/HANDOFF.md` and `AGENTS.md` "Current tool state" (map folder, map-region command, overlays folder).

## Calls made here

- Projection: Web Mercator, clamped to ±85°; zoom is the integer-plus-fraction scale the usual tile maths uses, so "100 km across the viewport" is a plain conversion. Initial view fits the shown frames with 10 % padding; with no fixed frames it fits the overlays; with nothing, the whole world.
- Clustering is done in the browser from the full photo list (a few thousand points is cheap); feature decimation is done on the server because the road layer is 50 MB.
- The index holds coordinates as JSON text rather than a binary blob: simpler, and the R-tree does the heavy lifting.
- Overlay files keep their names with path separators and leading dots stripped; a duplicate name gets a numeric suffix.
- Track interpolation is linear in time between bracketing points; a frame outside the span by less than `max_gap_minutes` snaps to the nearest end point; beyond that it is out of span.
- The 10 MB `land` layer is not fetched (see Non-goals).
- Regions and overlays are library data, not vendor data: `vendor/data/map/` holds only what bootstrap installs; everything the photographer adds or fetches lands under `<library>/map/`. `Settings` gets `map_dir` (vendor) and derives `map_regions_dir` and `map_overlays_dir` from the library.

## Left to the builder

Zoom thresholds per layer; decimation algorithm; the exact clustering radius; the request shape for overlay upload; how "Shown frames" borrows the Review filter; thumbnail size on the map; canvas hit-testing; how the frame dialog's "Show on map" hands over its centre; test fixture content.
