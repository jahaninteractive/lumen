# Leica and Nikon Raws Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Leica `.dng` and Nikon `.nef` files import, preview, cull, develop and export exactly as Sony `.arw` does.

**Architecture:** Both formats decode through the route ARW already takes (Core Image via `raw_developer`), so the
work is one shared raw list plus the four places that assumed Sony: preview choice, capture metadata, two
hard-coded `ARW` checks, and develop controls a file's decoder does not offer.

**Tech Stack:** Python 3 stdlib + Pillow, `unittest`, Swift (Core Image `CIRAWFilter`), exiftool, vanilla JS.

**Spec:** `docs/superpowers/specs/2026-11-22-leica-nikon-raws-design.md`

## Global Constraints

- Read `AGENTS.md` and `docs/HANDOFF.md` first. Branch `feature/leica-nikon-raws` off `main`.
- Never write, move or rename a camera original; derived files stay inside the library.
- Pillow is the only runtime dependency. No downloads.
- No AI vendor or model-family names in tracked files or commit messages; no co-author or session trailers.
  Commit messages are one plain sentence saying what changed, as `git log` shows.
- Red-green: each test is seen failing before the code. Suite command:
  `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest discover -s tests -v`; focused:
  `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_<module> -v`; and
  `node --check src/lumen/static/app.js` after any JS edit.
- No test reads `Examples/`, runs exiftool on a real raw, or runs the helper: inject runners as the suite does.
- Nothing in a browser response names a source path, fingerprint or serial.
- Preserve native controls, keyboard behaviour and live announcements in the Develop panel.
- The sample raws in `Examples/` (every `.dng`/`.DNG`/`.nef`/`.NEF`) are never committed.

## Review Focus

1. A Sony `.arw` behaves exactly as before: its preview still comes from an embedded JPEG and its full-size view
   from the embedded full-size JPEG (3504 of 3584 px on the reduced-size sample), never a render. Task 3.
2. A Leica file on a library whose helper is missing (not re-bootstrapped) still imports with some preview
   through the old `sips`/darktable fallbacks instead of failing the import. Task 3.
3. A frame whose saved develop settings include a control its decoder lacks (settings copied across a burst
   from a colour frame to a monochrome one) fails that one frame with a readable reason, not the job. Task 4.
4. A real camera fix on the equator or the prime meridian (latitude 0 with a real longitude) is kept; only
   exactly 0, 0 is dropped. Task 2.
5. An upper-case extension (`.DNG`, `.NEF`) is treated the same as lower-case everywhere the list is used. Task 1.

---

### Task 1: One raw list

**Files:**
- Modify: `src/lumen/domain.py:16`, `src/lumen/pipelines.py:108`, `src/lumen/astro.py:26`,
  `src/lumen/library.py:2185`, the photo-detail payload for `GET /api/photos/<id>` (where `format` is
  serialised), `src/lumen/static/app.js:4671`
- Test: `tests/test_ingest.py`, `tests/test_pipelines.py`, `tests/test_astro.py`, `tests/test_library.py`,
  `tests/test_web.py`

**Interfaces:**
- Produces: `domain.RAW_EXTENSIONS: frozenset[str] = {".arw", ".dng", ".nef"}` (re-exported unchanged as
  `pipelines.RAW_EXTENSIONS`); `domain.is_raw(path: str | Path) -> bool` (suffix, case-insensitive);
  `is_raw: bool` on the photo detail JSON.

- [ ] **Step 1: Failing tests**
  - `test_ingest`: `test_dng_and_nef_are_scanned` — a folder holding `a.DNG`, `b.nef`, `c.arw`, `d.txt` scans to
    the first three.
  - `test_pipelines`: `test_dng_and_nef_export_through_the_raw_developer` — for `x.dng` and `x.NEF`, the export
    settings record `decoder == "coreimage"`; `RAW_EXTENSIONS is domain.RAW_EXTENSIONS`.
  - `test_astro`: `test_dng_and_nef_lights_are_accepted` — `prepare_siril_job` with two `.dng` lights does not
    raise "lights must be RAW, FITS, or TIFF frames".
  - `test_library`: `test_develop_copy_reaches_a_nef_burst_mate` — copy-to-burst onto a frame with
    `format="NEF"` counts it as copied, not `skipped_non_raw`.
  - `test_web`: `test_photo_detail_says_whether_the_frame_is_raw` — `is_raw` is `True` for a `.dng` photo and
    `False` for a `.jpg`.
- [ ] **Step 2: Run them; each fails for the stated reason.**
- [ ] **Step 3: Implement.** Define `RAW_EXTENSIONS` and `is_raw` in `domain.py`; build `SUPPORTED_EXTENSIONS`
  and `ASTRO_SUFFIXES` as `RAW_EXTENSIONS | {...the rest...}`; `pipelines` imports the name from `domain`;
  `library.py:2185` becomes `not is_raw(other.path)`; the detail payload adds `is_raw`; `app.js:4671` becomes
  `const raw = detail.is_raw === true;`.
- [ ] **Step 4: Run the five test modules and `node --check`; all pass.**
- [ ] **Step 5: Commit** — "Leica DNG and Nikon NEF files are raws wherever Sony ARW is, from one list."

### Task 2: Capture metadata

**Files:**
- Modify: `src/lumen/previews.py:168-205` (`read_metadata`)
- Test: `tests/test_previews.py`

**Interfaces:**
- Produces: unchanged return shape. New module constant
  `NIKON_HE_WARNING = "Nikon high-efficiency raw; darktable cannot decode it. darktable's refine pass and XMP-profile exports are unavailable and culling uses the embedded preview. JPEG export is unaffected."`

- [ ] **Step 1: Failing tests** (patching `subprocess.run` to return exiftool JSON, as the module's tests do)
  - `test_serial_falls_back_to_camera_serial_number` — `{"Model": "LEICA M (Typ 240)", "CameraSerialNumber": 4851623}`
    gives `camera_id == "LEICA M (Typ 240):4851623"`.
  - `test_a_masked_serial_is_unknown` — `{"Model": "M9 Digital Camera", "SerialNumber": "*******"}` gives
    `camera_id == "M9 Digital Camera:unknown"`.
  - `test_a_zero_zero_fix_is_no_fix` — `GPSLatitude: 0, GPSLongitude: 0` gives `latitude is None and longitude is None and altitude is None`.
  - `test_an_equator_fix_is_kept` — `GPSLatitude: 0, GPSLongitude: 36.8` gives `latitude == 0 and longitude == 36.8`.
  - `test_high_efficiency_nef_warns` — a `.nef` path with `NEFCompression: 14` (and `13`) gives
    `raw_warning == NIKON_HE_WARNING`; `NEFCompression: 1` gives `None`; a `.arw` with photometric 6 still
    gives the Sony warning.
- [ ] **Step 2: Run; they fail.**
- [ ] **Step 3: Implement.** Add `-CameraSerialNumber` and `-NEFCompression` to the exiftool arguments. Serial
  is the first of `SerialNumber`, `CameraSerialNumber` whose string form contains a letter or digit, else
  `unknown`. Drop the fix when both coordinates equal 0.
- [ ] **Step 4: Run `tests.test_previews`; pass.**
- [ ] **Step 5: Commit** — "Capture metadata reads a Leica serial, drops a 0,0 fix and warns on a high-efficiency Nikon raw."

### Task 3: Previews and the full-size view

**Files:**
- Modify: `src/lumen/previews.py:62-135` (`ExifToolPreviewProvider`), `:273-301` (`full_size_for`),
  `src/lumen/library.py:257`, `src/lumen/web.py:572`
- Test: `tests/test_previews.py`, `tests/test_web.py`

**Interfaces:**
- Consumes: `domain.is_raw`; `rawdevelop.render_raw(source, destination, executable, *, long_edge, image_format="jpeg")`.
- Produces: `previews.MIN_EMBEDDED_PREVIEW = 1000`;
  `ExifToolPreviewProvider(max_size: int = 2048, raw_developer: Path | None = None)`;
  `full_size_for(source, cache_dir, fingerprint, raw_developer: Path | None = None) -> Path | None`.
  `Library` passes `settings.raw_developer_path`; so does the `/full/<id>` route.

- [ ] **Step 1: Failing tests** (exiftool `-b` output faked per tag with small in-memory JPEGs of chosen sizes;
  `render_raw` patched to write a JPEG and record its `long_edge`)
  - `test_preview_takes_the_largest_embedded_jpeg` — tags `PreviewImage` 640 px, `JpgFromRaw` 4000 px: the
    preview is made from the 4000 px one.
  - `test_preview_renders_a_raw_whose_embedded_jpeg_is_a_thumbnail` — a `.dng` with only a 160 px
    `PreviewImage`, helper present: `render_raw` called once with `long_edge == 2048`.
  - `test_preview_renders_a_raw_with_no_embedded_jpeg` — a `.dng` with no tags: same.
  - `test_preview_without_the_helper_falls_back_as_before` — same file, `raw_developer=None`: `render_raw` not
    called, `sips` runner is.
  - `test_arw_preview_never_renders` — `.arw` with a 1616 px `PreviewImage` and 7008 px `JpgFromRaw`:
    `render_raw` not called.
  - `test_full_size_renders_when_the_embedded_jpeg_is_far_smaller_than_the_raw` — `.dng`, exiftool `ImageSize`
    `"9536 6336"`, `JpgFromRaw` 2112 px: `render_raw` called with `long_edge == 0`.
  - `test_full_size_keeps_a_near_full_embedded_jpeg` — `.arw`, `ImageSize` `"3584 2560"`, `JpgFromRaw` 3504 px:
    embedded used, `render_raw` not called.
  - `test_full_size_without_the_helper_keeps_the_embedded_jpeg` — the 2112 px case with `raw_developer=None`
    returns the embedded one.
- [ ] **Step 2: Run; they fail.**
- [ ] **Step 3: Implement.** One private helper `_largest_embedded(source) -> bytes | None` tries `JpgFromRaw`,
  `OtherImage`, `PreviewImage` and returns the JPEG with the longest edge. `create_preview` uses it when its
  long edge is at least `MIN_EMBEDDED_PREVIEW`; otherwise, for a raw with a helper file present, it renders at
  `max_size` and normalises; a `DevelopError` or no helper falls through to the existing `sips` and darktable
  code, which finally accepts a sub-threshold embedded JPEG rather than raising. `full_size_for` renders at
  `long_edge=0` (JPEG, then saved at quality 92 like the embedded path) when the file is a raw, the helper
  exists, and the embedded long edge is under half the long edge of exiftool `-n -ImageSize`, or there is none.
- [ ] **Step 4: Run `tests.test_previews` and `tests.test_web`; pass.**
- [ ] **Step 5: Commit** — "A raw with no usable embedded JPEG gets its preview and full-size view from the raw developer."

### Task 4: Controls a file's decoder does not offer

**Files:**
- Modify: `tools/raw_developer.swift:196-232,325`, `src/lumen/rawdevelop.py:34,192`,
  `src/lumen/library.py:1523-1526`, `src/lumen/web.py:1088`, `src/lumen/static/app.js` (the develop panel's
  `lensCorrection` state at `:8`, `:4637-4641`, `:4663`, `:4714`), every `DevelopRender(...)` in the tests
- Test: `tests/test_rawdevelop.py`, `tests/test_library.py`, `tests/test_web.py`

**Interfaces:**
- Produces: helper JSON key `"supported": {"contrast", "sharpness", "luminance_nr", "color_nr", "lens_correction": bool}`
  replacing `"lens_correction_supported"`; `DevelopRender.supported: Mapping[str, bool]` replacing
  `lens_correction_supported` (a key missing from the helper's answer reads as supported, except
  `lens_correction`, which reads as unsupported as today); the develop-preview route returns `"supported"`
  replacing `"lens_correction"`.

- [ ] **Step 1: Failing tests**
  - `test_rawdevelop`: `test_render_reports_supported_controls` — payload
    `"supported": {"contrast": false, "sharpness": false, "luminance_nr": false, "color_nr": false, "lens_correction": false}`
    gives `render.supported["sharpness"] is False`; a payload with no `supported` key gives
    `supported["sharpness"] is True and supported["lens_correction"] is False`.
  - `test_library`: `test_advice_drops_controls_the_decoder_lacks` — a suggestion of
    `{"sharpness": 0.6, "exposure_ev": 0.3}` on a render with `sharpness` unsupported is stored as
    `{"exposure_ev": 0.3}`.
  - `test_library`: `test_copy_to_burst_reports_a_frame_whose_decoder_refuses_a_setting` — `save_develop` raising
    `DevelopError` for one mate counts it `failed`, logs its name and reason, and the other mates are copied.
  - `test_web`: `test_develop_preview_returns_supported_controls` — the route's JSON has
    `supported == {...}` and no `lens_correction` key.
- [ ] **Step 2: Run; they fail.**
- [ ] **Step 3: Implement the Python and JS.** Generalise the lens-correction strip in `library.py` to every
  unsupported key. In `app.js`, `state.develop.supported` replaces `lensCorrection`; each of the five controls
  is `disabled` when its flag is `false`, with the existing lens-box hint pattern and the text
  "This file's decoder has no <control label> control." in the control's described-by line.
- [ ] **Step 4: Implement the Swift.** Emit `supported` from `rawFilter.isContrastSupported`,
  `isSharpnessSupported`, `isLuminanceNoiseReductionSupported`, `isColorNoiseReductionSupported`,
  `isLensCorrectionSupported`. A requested numeric setting whose flag is false fails with
  `fail("this file's decoder has no \(name) control", invalidRequest)`, as lens correction does now.
- [ ] **Step 5: Run the three test modules, `node --check`, then rebuild the helper:**
  `zsh scripts/bootstrap.sh --no-download`. Expected: helper compiles; `uv run lumen doctor --json` reports
  the raw developer available.
- [ ] **Step 6: Commit** — "The Develop panel disables the controls a file's decoder does not offer."

### Task 5: Real files, words and version

**Files:**
- Modify: `.gitignore`, `README.md`, `docs/GUIDE.md`, `src/lumen/static/index.html:644`,
  `src/lumen/static/app.js:1581`, `AGENTS.md`, `CHANGELOG.md`, `src/lumen/__init__.py`, `pyproject.toml`,
  `docs/HANDOFF.md`, `docs/BACKLOG.md`

- [ ] **Step 1: Ignore the samples.** Add `Examples/*.dng`, `Examples/*.DNG`, `Examples/*.nef`, `Examples/*.NEF`
  to `.gitignore`; `git status --short` shows none of them.
- [ ] **Step 2: Real run in a scratch library** (never the photographer's own; its server holds the job lock):
  `LUMEN_LIBRARY=<scratch>/lib zsh scripts/bootstrap.sh --no-download`, then
  `LUMEN_LIBRARY=<scratch>/lib env -u LUMEN_OMLX_MODEL uv run lumen import Examples --json`. Expected: ten
  frames imported, none failed. Then check and record, per sample:
  - preview long edge is at least 1000 px (both older Leicas rendered; the others embedded);
  - `camera_id` carries a serial for the M240, M11s, Q3, D800, Z 8, Z 9 and `unknown` for the M9;
  - no frame has a fix at 0, 0;
  - both Z files carry the high-efficiency warning and the D800 none;
  - `uv run lumen export --photo <id> --json` succeeds for each; exiftool on the JPEG shows the original's
    `Make`, a MakerNote, and IPTC `CodedCharacterSet`;
  - on the M11 Monochrom, `supported` is all false and a save with `sharpness` is refused with the helper's
    reason; on the colour M11 all four numeric flags are true;
  - on the Q3, `supported.lens_correction` is true and an export with `lens_correction: false` differs from one
    without the key;
  - develop-preview render time, warm, three runs each, for the M11, Z 9 and a Sony file.
- [ ] **Step 3: Look at it.** Start `lumen serve` on the scratch library on a spare port and, with the
  screenshot harness, capture the contact sheet and the Develop tab for the M11 Monochrom (four controls
  disabled with their reason) and the Z 8 (warning shown). Fix what looks wrong before going on.
- [ ] **Step 4: Words.** README, guide, About text and the import promise name Sony, Leica and Nikon raws; the
  guide lists the bodies measured (M9, M (Typ 240), M11, M11 Monochrom, Q3, D800, Z 8, Z 9), what is untested (a
  straight-from-camera Q file, an SL file, a lossless-compressed Z file, a real astro stack from either maker), that M bodies record an
  unreliable aperture, and the measured M11 develop time. `AGENTS.md` "Product contract" and "Current tool
  state" gain the raw list, the preview rule, the metadata rules and `supported`. Version 0.7.0 in both files;
  one `CHANGELOG.md` entry; `docs/HANDOFF.md` "Resume here"; untested items into `docs/BACKLOG.md`.
- [ ] **Step 5: Full suite and `node --check`.** Expected: all pass, warning-strict, count reported.
- [ ] **Step 6: Commit** — "0.7.0: the README, guide, changelog and handoff cover Leica and Nikon raws and what was measured."
