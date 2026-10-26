# Develop Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add IEDT-style develop controls — editable histogram/tone curve, brightness, white balance, contrast, sharpness, noise reduction — to the frame dialog, and bake them into exported JPEGs.

**Architecture:** A Swift helper built on `CIRAWFilter` renders raws with develop settings applied, compiled at bootstrap like the existing `vision_analyzer`. Settings are stored per photo in a `photo_develop` table beside `photo_edits`, sent to the browser as JSON, and passed to the helper at export time. The helper replaces `sips` as the decoder for raw JPEG export.

**Tech Stack:** Python 3 stdlib + Pillow, Swift (Core Image, no new Python dependency), SQLite, vanilla JS with native controls.

**Spec:** `docs/superpowers/specs/2026-10-26-develop-controls-design.md`

## Global Constraints

- Never edit, move, rename or delete camera originals. The helper only reads its source; every destination is inside the library.
- Pillow stays the only runtime Python dependency.
- The server is loopback-only; mutation routes keep host/origin checks and strict JSON validation.
- No source path, fingerprint, or helper stderr reaches the browser.
- No AI vendor or model names in tracked files or commit messages. Refer to the shared guide as `AGENTS.md`.
- Tests run warning-strict: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest discover -s tests -v`. No test invokes Core Image, darktable, or the network.
- Preserve native controls, keyboard tab behaviour, live announcements, and the charcoal/amber/cyan workbench.
- Apple Silicon macOS only.

## Review Focus

Input classes the spec implies but which no task's happy path exercises. Each has a test assigned to the task that owns the code.

1. **A tone curve with non-ascending or duplicate x values** — the browser can send any five points; `CIToneCurve` needs x strictly ascending. Expect rejection at the route with a clear message, not a wedged render. *(Task 2 validation, Task 4 route.)*
2. **The helper writing nothing, hanging, or exiting non-zero** — a corrupt raw or a killed process must surface a public message and leave no partial preview, not raise a traceback into the response. *(Task 1.)*
3. **A develop save racing an export of the same photo** — both mutate; the save must take the mutation lock the way `_change_crop` does, or an export can render settings that were half-written. *(Task 3.)*
4. **An unbounded `long_edge` from the browser** — a request for a 40000px preview is a loopback resource-exhaustion path. Expect clamping to the preview maximum. *(Task 4.)*
5. **Develop settings surviving a source file that changed underneath them** — `_export_one` already refuses on a fingerprint mismatch; saved develop settings must be invalidated the same way rather than silently applying to different pixels. *(Task 3.)*

---

### Task 1: The native raw developer

**Files:**
- Create: `tools/raw_developer.swift`
- Create: `src/lumen/rawdevelop.py`
- Create: `tests/test_rawdevelop.py`
- Modify: `scripts/bootstrap.sh` (compile step beside `vision_analyzer`)
- Modify: `src/lumen/config.py` (after `vision_analyzer_path`, line 92)
- Modify: `src/lumen/diagnostics.py:84` (report beside `vision_analyzer`)
- Modify: `tests/test_scripts.py`, `tests/test_cli.py` (doctor reports the helper)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Settings.raw_developer_path -> Path` (`<library>/cache/bin/raw_developer`)
  - `lumen.rawdevelop.DevelopRender` — frozen dataclass: `path: Path`, `width: int`, `height: int`, `histogram: tuple[float, ...]`, `neutral_temperature: float`, `neutral_tint: float`
  - `lumen.rawdevelop.render_raw(source: Path, destination: Path, executable: Path, *, settings: dict[str, object] | None = None, long_edge: int = 0, image_format: str = "tiff16", histogram: bool = False, runner=subprocess.run) -> DevelopRender`
  - `lumen.rawdevelop.DevelopError` — raised with a message safe to show the photographer.

- [ ] **Step 1: Write the failing tests**

In `tests/test_rawdevelop.py`, with a stub executable written by the test (a `#!/bin/sh` script echoing canned JSON, `chmod 0o755`), mirroring how `analyze_faces` is exercised:

```python
def test_render_raw_sends_the_request_as_json_on_stdin(self):
    # stub writes its stdin to a side file; assert the parsed request
    self.assertEqual(request["source"], str(source))
    self.assertEqual(request["long_edge"], 1100)
    self.assertEqual(request["format"], "tiff16")
    self.assertEqual(request["settings"], {"exposure_ev": 0.85})

def test_render_raw_returns_the_histogram_and_neutral_white_balance(self):
    self.assertEqual(render.histogram, (0.5522, 0.0256))
    self.assertEqual(render.neutral_temperature, 3490.1663)

def test_render_raw_omits_unset_settings_entirely(self):
    # settings={} must send {}, never zeros — Core Image's per-file defaults must survive
    self.assertEqual(request["settings"], {})

def test_render_raw_raises_develop_error_when_the_helper_exits_non_zero(self):
    with self.assertRaises(DevelopError) as caught:
        render_raw(...)
    self.assertNotIn("Traceback", str(caught.exception))

def test_render_raw_raises_develop_error_when_the_helper_writes_no_output_file(self):
def test_render_raw_raises_develop_error_on_unparseable_stdout(self):
def test_render_raw_raises_develop_error_on_timeout(self):   # runner raises TimeoutExpired
def test_render_raw_raises_develop_error_when_the_executable_is_missing(self):
```

- [ ] **Step 2: Run them and watch them fail**

Run: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_rawdevelop -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'lumen.rawdevelop'`

- [ ] **Step 3: Write `src/lumen/rawdevelop.py`**

`render_raw` builds the request dict, `json.dumps` it to the subprocess's stdin with a 300 s timeout, and parses one JSON object from stdout. Every failure path — non-zero exit, `OSError`, `SubprocessError`, `TimeoutExpired`, `JSONDecodeError`, a missing destination file — raises `DevelopError` with a message that names the source file's **basename only**, never its path. Keys whose value is `None` are dropped from `settings` before sending.

- [ ] **Step 4: Write `tools/raw_developer.swift`**

Reads one JSON object from stdin, writes one to stdout. Builds a `CIRAWFilter(imageURL:)`, sets `scaleFactor` from `long_edge` when non-zero (never upscaling), applies only the keys present in `settings` per the spec's mapping table, chains `CIFilter.toneCurve` when `curve` is present, and writes `.RGBA16` TIFF or JPEG (quality 0.92) in the sRGB colour space. When `histogram` is true it runs `CIFilter.areaHistogram` with `count: 64` over the output extent and returns the luminance buckets. Exits non-zero with a message on stderr if the file cannot be decoded.

- [ ] **Step 5: Add the bootstrap compile step and the settings path**

In `scripts/bootstrap.sh`, compile `tools/raw_developer.swift` into `$native_bin/raw_developer` with the same `CLANG_MODULE_CACHE_PATH` invocation as `vision_analyzer`, and echo its path. Add `Settings.raw_developer_path` returning `self.cache_dir / "bin" / "raw_developer"`.

- [ ] **Step 6: Report it in doctor**

In `diagnostics.doctor_report`, add a `"raw_developer"` block beside `"vision_analyzer"` with `available` and `path`. Update the `tests/test_cli.py` and `tests/test_scripts.py` assertions that enumerate the report's keys.

- [ ] **Step 7: Run the full suite**

Run: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest discover -s tests -v`
Expected: PASS

- [ ] **Step 8: Verify the helper on a real raw, by hand**

Run `zsh scripts/bootstrap.sh`, then pipe a request for a 1100px JPEG of any `.ARW` in the library and confirm a plausible image and 64 histogram buckets come back. This is the only step that touches Core Image; it is not a test.

- [ ] **Step 9: Commit**

```bash
git add tools/raw_developer.swift src/lumen/rawdevelop.py tests/test_rawdevelop.py scripts/bootstrap.sh src/lumen/config.py src/lumen/diagnostics.py tests/test_cli.py tests/test_scripts.py
git commit -m "feat: render raws with develop settings through a native Core Image helper"
```

---

### Task 2: Develop settings and their storage

**Files:**
- Modify: `src/lumen/domain.py` (beside `CropRect`, line 18)
- Modify: `src/lumen/catalog.py` (schema at line 181, methods beside `get_crop` at line 435)
- Modify: `tests/test_catalog.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `lumen.domain.DevelopSettings` — frozen dataclass with `fields: dict[str, float | tuple[tuple[float, float], ...]]`, holding **only** the keys the photographer set. `to_dict() -> dict`, `DevelopSettings.from_dict(data: dict) -> DevelopSettings`, `is_empty -> bool`.
  - `lumen.domain.DEVELOP_RANGES: dict[str, tuple[float, float]]` — the spec's table verbatim: `exposure_ev` (-3.0, 3.0), `temperature_k` (2000, 12000), `tint` (-150, 150), `contrast` (0.0, 1.0), `sharpness` (0.0, 1.0), `luminance_nr` (0.0, 1.0), `color_nr` (0.0, 1.0).
  - `Catalog.get_develop(photo_id) -> DevelopSettings | None`
  - `Catalog.save_develop(photo_id: int, settings: DevelopSettings, source_fingerprint: str) -> DevelopSettings`
  - `Catalog.clear_develop(photo_id: int) -> DevelopSettings | None`
  - `Catalog.invalidate_develop(photo_id: int, previous_fingerprint: str, source_fingerprint: str) -> DevelopSettings | None`
  - `Catalog.develop_event_ids(photo_id: int) -> list[int]`

- [ ] **Step 1: Write the failing tests**

```python
def test_develop_settings_reject_an_out_of_range_value(self):
    with self.assertRaises(ValueError):
        DevelopSettings({"exposure_ev": 4.0})

def test_develop_settings_reject_an_unknown_key(self):
def test_develop_settings_reject_a_non_finite_value(self):      # nan, inf
def test_develop_settings_reject_a_bool(self):                  # True is not 1.0 here
def test_develop_settings_reject_a_curve_without_five_points(self):
def test_develop_settings_reject_a_curve_whose_x_values_do_not_ascend(self):
    with self.assertRaises(ValueError):
        DevelopSettings({"curve": ((0, 0), (0.5, 0.2), (0.5, 0.4), (0.75, 0.8), (1, 1))})
def test_develop_settings_reject_a_curve_point_outside_zero_to_one(self):
def test_develop_settings_round_trip_only_the_fields_that_were_set(self):
    self.assertEqual(DevelopSettings.from_dict({"contrast": 0.55}).to_dict(), {"contrast": 0.55})

def test_save_develop_round_trips_through_the_catalog(self):
def test_save_develop_records_a_develop_changed_event(self):
def test_clear_develop_records_a_develop_cleared_event_with_the_previous_settings(self):
def test_clear_develop_returns_none_when_nothing_was_saved(self):
def test_invalidate_develop_records_the_fingerprints_and_removes_the_row(self):
def test_save_develop_rejects_an_unknown_photo_id(self):
```

- [ ] **Step 2: Run them and watch them fail**

Run: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_catalog -v`
Expected: FAIL, `ImportError: cannot import name 'DevelopSettings'`

- [ ] **Step 3: Implement `DevelopSettings` in `src/lumen/domain.py`**

Validate in `__post_init__` the way `CropRect` does: reject bools explicitly (`isinstance(value, bool)`), require `math.isfinite`, require each key in `DEVELOP_RANGES` and its value inside the range. The curve is five `(x, y)` pairs, both coordinates in `0..1`, x **strictly** ascending.

- [ ] **Step 4: Add the table and the catalog methods**

Add to the `CREATE TABLE IF NOT EXISTS` block — new tables need no `_MIGRATIONS` entry, since the block runs on every open:

```sql
CREATE TABLE IF NOT EXISTS photo_develop (
    photo_id INTEGER PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE,
    settings_json TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

The four methods mirror `save_crop` / `clear_crop` / `invalidate_crop` exactly, including `_insert_event` inside the same `with self.connection:` block. Event kinds: `develop_changed`, `develop_cleared`, `develop_invalidated_source_replaced`.

- [ ] **Step 5: Run the tests**

Run: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_catalog -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/lumen/domain.py src/lumen/catalog.py tests/test_catalog.py
git commit -m "feat: store per-frame develop settings beside crops"
```

---

### Task 3: Saving, clearing, and the contact sheet

**Files:**
- Modify: `src/lumen/domain.py:12` (`PREVIEW_KINDS`)
- Modify: `src/lumen/library.py` (new methods beside `clear_crop`, line 915; refine skip at `_refine_one`, line 645)
- Modify: `tests/test_library.py`

**Interfaces:**
- Consumes: `render_raw`, `DevelopError`, `Settings.raw_developer_path` (Task 1); `DevelopSettings`, `Catalog.save_develop` / `get_develop` / `clear_develop` / `invalidate_develop` (Task 2).
- Produces:
  - `Library.save_develop(photo_id: int, settings: DevelopSettings) -> DevelopSettings`
  - `Library.clear_develop(photo_id: int) -> None`
  - `Library.develop_for(photo_id: int) -> DevelopSettings | None`

- [ ] **Step 1: Write the failing tests**

Patch `lumen.library.render_raw` with a fake that writes a small JPEG to the destination; no test invokes Core Image.

```python
def test_save_develop_rerenders_the_preview_and_marks_it_developed(self):
    self.assertEqual(self.catalog.get_photo(photo_id).preview_kind, "developed")

def test_save_develop_remeasures_metrics(self):
    self.assertNotEqual(before.sharpness, after.sharpness)

def test_save_develop_leaves_the_group_pick_untouched(self):
    # the decisive test: a develop save must never move Best
    self.assertEqual(self.catalog.group_pick(group_id), pick_before)

def test_clear_develop_rerenders_with_no_settings_and_marks_it_refined(self):
    self.assertEqual(sent["settings"], {})
    self.assertEqual(self.catalog.get_photo(photo_id).preview_kind, "refined")

def test_refine_previews_skips_a_developed_frame(self):
def test_save_develop_invalidates_when_the_source_fingerprint_changed(self):   # Review Focus 5
def test_save_develop_takes_the_mutation_lock(self):                           # Review Focus 3
def test_save_develop_surfaces_a_public_message_when_the_helper_fails(self):
    self.assertNotIn(str(source), message)
def test_save_develop_leaves_the_old_preview_in_place_when_the_render_fails(self):
```

- [ ] **Step 2: Run them and watch them fail**

Run: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_library -v`
Expected: FAIL, `AttributeError: 'Library' object has no attribute 'save_develop'`

- [ ] **Step 3: Add `developed` to `PREVIEW_KINDS`**

`PREVIEW_KINDS = frozenset({"embedded", "refined", "developed"})`.

- [ ] **Step 4: Implement the two methods in `src/lumen/library.py`**

Both are `@_coordinated` and take `self._mutation()` like `_change_crop`. Each: verifies the photo exists and its fingerprint still matches (invalidating via `Catalog.invalidate_develop` and raising if not), renders a new preview with `render_raw` into a temporary and normalises it atomically through `self.previews`, re-measures with `measure_image` plus `analyze_faces` and stores the metrics, calls `Catalog.update_preview` with `developed` (save) or `refined` (clear), and rewrites the sidecar. It does **not** call `_rerank_group`. A `DevelopError` leaves the existing preview and metrics untouched.

- [ ] **Step 5: Skip developed frames in refine**

In the refine loop, treat a photo whose `preview_kind` is `developed` the way an already-refined frame is treated, and count it as skipped.

- [ ] **Step 6: Make the contact sheet see the new preview**

A develop save rewrites the preview at the same path, so the fingerprint-based `?v=` cache key does not change and the browser serves the old tile. Add the preview file's mtime to the cache key wherever the contact sheet builds a preview URL. Test: `test_preview_url_changes_after_a_develop_save`. Note that the preview filename on disk is `<fingerprint>-2048.jpg`, not the `<fingerprint>.jpg` the design document writes — follow the code.

- [ ] **Step 7: Run the tests**

Run: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_library -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/lumen/domain.py src/lumen/library.py tests/test_library.py
git commit -m "feat: saving develop settings re-renders the preview and re-measures without re-ranking"
```

---

### Task 4: The HTTP routes

**Files:**
- Modify: `src/lumen/web.py` (GET near line 297, POST near line 572, DELETE handler)
- Modify: `tests/test_web.py`

**Interfaces:**
- Consumes: `Library.save_develop` / `clear_develop` / `develop_for` (Task 3), `render_raw` (Task 1), `DevelopSettings` (Task 2).
- Produces: three routes.
  - `POST /api/photos/<id>/develop/preview` — body `{"settings": {...}, "long_edge": int}` → `{"image": "data:image/jpeg;base64,…", "histogram": [...], "neutral_temperature": float, "neutral_tint": float}`
  - `POST /api/photos/<id>/develop` — body `{"settings": {...}}` → `{"photo_id": int, "develop": {...}}`
  - `DELETE /api/photos/<id>/develop` → `{"photo_id": int, "develop": null}`

- [ ] **Step 1: Write the failing tests**

```python
def test_develop_preview_returns_an_image_and_a_histogram(self):
def test_develop_preview_clamps_an_oversized_long_edge(self):          # Review Focus 4
    self.assertLessEqual(sent["long_edge"], MAX_PREVIEW_EDGE)
def test_develop_preview_rejects_a_negative_long_edge(self):
def test_develop_rejects_an_unknown_settings_key(self):
def test_develop_rejects_a_curve_whose_x_values_do_not_ascend(self):   # Review Focus 1
def test_develop_rejects_extra_top_level_keys(self):
def test_develop_rejects_a_cross_origin_post(self):
def test_develop_rejects_a_non_loopback_host(self):
def test_develop_clear_returns_null_develop(self):
def test_develop_response_contains_no_source_path(self):
    self.assertNotIn("/Volumes", body)
def test_develop_helper_failure_returns_a_public_message(self):
```

- [ ] **Step 2: Run them and watch them fail**

Run: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_web -v`
Expected: FAIL with 404 responses

- [ ] **Step 3: Implement the routes**

Follow the `/api/photos/(\d+)/crop` block exactly: `re.fullmatch` on the path, reject any payload whose `keys()` differ from the expected set, build `DevelopSettings.from_dict` inside a `try` that re-raises as a `ValueError` with a public message, and respond with `self._send_json`. `long_edge` is clamped to `MAX_PREVIEW_EDGE` (the previews module's `max_size`) and must be a positive int. The preview route never persists.

- [ ] **Step 4: Run the tests**

Run: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_web -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/lumen/web.py tests/test_web.py
git commit -m "feat: routes to render, save and clear a frame's develop settings"
```

---

### Task 5: Applying one frame's settings to its burst

**Files:**
- Modify: `src/lumen/library.py` (beside `start_export_job`, line 1343)
- Modify: `src/lumen/web.py`
- Modify: `tests/test_library.py`, `tests/test_web.py`

**Interfaces:**
- Consumes: `Library.save_develop` (Task 3), the `create_prepared_job` / `_submit_job` / `_write_job_files` pattern at `library.py:1343`.
- Produces:
  - `Library.start_develop_group_job(photo_id: int) -> Job` — job kind `develop_group`
  - `POST /api/photos/<id>/develop/apply-to-group` → `library.job_status(job.id)` with `HTTPStatus.ACCEPTED`

- [ ] **Step 1: Write the failing tests**

```python
def test_apply_to_group_writes_the_settings_to_every_frame_in_the_group(self):
def test_apply_to_group_includes_rejected_frames(self):
def test_apply_to_group_skips_and_counts_non_raw_members(self):
    self.assertEqual(summary["skipped_non_raw"], 1)
def test_apply_to_group_skips_and_counts_frames_whose_original_is_missing(self):
    self.assertEqual(summary["skipped_offline"], 1)
def test_apply_to_group_records_one_develop_applied_to_group_event(self):
def test_apply_to_group_refuses_when_the_source_frame_has_no_develop_settings(self):
def test_apply_to_group_reports_progress_per_frame(self):
```

- [ ] **Step 2: Run them and watch them fail**

Run: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_library -v`
Expected: FAIL, `AttributeError: … 'start_develop_group_job'`

- [ ] **Step 3: Implement the job**

Model it on `start_export_job`: resolve the source photo's `group_id`, take `catalog.group_photo_ids(group_id)` as the frame list, and for each frame in a `worker._mutation()` block call `save_develop` with the source frame's settings. Count `applied`, `skipped_non_raw`, `skipped_offline`, `failed`. One `develop_applied_to_group` event records the source photo id and the ids written. Honour `context.cancelled` and call `context.update_progress` per frame.

- [ ] **Step 4: Add the route**

Beside the other job-starting POSTs, requiring an empty JSON object, responding with `HTTPStatus.ACCEPTED`.

- [ ] **Step 5: Run the tests**

Run: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_library tests.test_web -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/lumen/library.py src/lumen/web.py tests/test_library.py tests/test_web.py
git commit -m "feat: apply one frame's develop settings across its burst as a background job"
```

---

### Task 6: Export

**Files:**
- Modify: `src/lumen/pipelines.py` (`DECODERS` line 108, `export_jpeg` line 116, delete `_run_sips` line 265)
- Modify: `src/lumen/library.py` (`ExportRequest` line 98, `_prepare_export` line 1319, `_export_one` line 1373)
- Modify: `src/lumen/sidecars.py` (`_xmp`)
- Modify: `src/lumen/previews.py:196` (the stale `raw_warning` text)
- Modify: `tests/test_pipelines.py`, `tests/test_library.py`, `tests/test_sidecars.py`

**Interfaces:**
- Consumes: `render_raw`, `DevelopError` (Task 1); `Catalog.get_develop` (Task 2).
- Produces:
  - `ExportRequest` gains `keep_original: bool = False`
  - `DarktablePipeline.export_jpeg` gains `develop: dict[str, object] | None = None` and `stem_suffix: str | None = None` (appended to the output stem after the preset, for the untouched companion)
  - `DECODERS = frozenset({"darktable", "coreimage"})`

- [ ] **Step 1: Write the failing tests**

```python
def test_export_renders_a_raw_through_the_raw_developer(self):
    self.assertEqual(settings["decoder"], "coreimage")

def test_export_passes_the_stored_develop_settings_to_the_helper(self):
def test_export_renders_a_non_raw_through_darktable(self):              # .jpg/.png/.heic still work
def test_export_renders_through_darktable_when_a_profile_is_given(self):
def test_export_refuses_when_a_profile_and_develop_settings_are_both_present(self):
def test_export_refuses_with_an_actionable_message_when_raw_developer_is_missing(self):
    self.assertIn("bootstrap", result.message)
def test_export_records_the_develop_settings_in_provenance(self):
def test_keep_original_produces_two_outputs_with_distinct_names(self):
    self.assertEqual(names, {"DSC1-lumen.jpg", "DSC1-lumen-original.jpg"})
def test_keep_original_names_the_companion_per_preset(self):
    self.assertIn("DSC1-lumen-web-original.jpg", names)
def test_keep_original_does_nothing_for_a_frame_with_no_develop_settings(self):
def test_sidecar_carries_lumen_develop_for_a_developed_frame(self):
def test_untouched_companion_xmp_has_no_lumen_develop_block(self):
```

- [ ] **Step 2: Run them and watch them fail**

Run: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_pipelines tests.test_library tests.test_sidecars -v`
Expected: FAIL

- [ ] **Step 3: Replace the `sips` render step in `pipelines.py`**

Delete `_run_sips`, `SIPS` and `SRGB_PROFILE`. In `export_jpeg`, choose the render: a profile or a non-raw suffix routes to `self.run(...)` (darktable) as today; otherwise `render_raw` into `work / "render.tif"` with the develop settings. Raise a `ValueError` naming both when `profile` and `develop` are given together. Record the chosen decoder in `settings["decoder"]` and the develop settings in `settings["develop"]`. The existing ICC check, EXIF copy, crop, resize, watermark and MakerNote rebase are untouched.

- [ ] **Step 4: Wire develop and `keep_original` through `library.py`**

In `_prepare_export`, raise a `ValueError` naming `scripts/bootstrap.sh` when any requested photo is a raw and `settings.raw_developer_path` is not a file. In `_export_one`, replace the `decoder = "macos" if …` line by reading `self.catalog.get_develop(photo.id)` and passing it as `develop`. When `request.keep_original` and the frame has develop settings, call `pipeline.export_jpeg` a second time with `develop=None` and a `stem_suffix` of `original`, saving a second artifact and a second `exported` event.

- [ ] **Step 5: Add `lumen:develop` to the sidecar**

In `sidecars.py`'s `_xmp`, write a `lumen:develop` block listing the set fields, beside the existing `lumen:crop`, when develop settings are passed; omit it entirely otherwise.

- [ ] **Step 6: Correct the stale raw warning**

Rewrite the `raw_warning` text at `previews.py:196` so it no longer promises a macOS decoder with default settings. It should say that darktable cannot decode the raw, so refined previews are unavailable and culling uses the embedded preview — export is no longer affected.

- [ ] **Step 7: Run the full suite**

Run: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest discover -s tests -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/lumen/pipelines.py src/lumen/library.py src/lumen/sidecars.py src/lumen/previews.py tests/
git commit -m "feat: export bakes develop settings into the JPEG and can keep the untouched version"
```

---

### Task 7: The Develop tab

**Files:**
- Modify: `src/lumen/static/index.html:455-470` (tab rail and a new panel)
- Modify: `src/lumen/static/app.js` (beside the crop panel wiring, line 2456)
- Modify: `src/lumen/static/styles.css`

**Interfaces:**
- Consumes: the three routes from Task 4 and the apply-to-group route from Task 5.
- Produces: no Python interface.

- [ ] **Step 1: Add the tab and panel markup**

A third `role="tab"` button `#develop-tab` after `#crop-tab`, and a `#develop-panel` `role="tabpanel"` holding: an `<svg id="develop-curve">` with the histogram and the five curve points; five `<details>` sections (Brightness, White Balance, Contrast, Sharpness, Noise Reduction) each containing `<input type="range">` plus a numeric `<input>` and a Reset button; and a `.button-row` with "Reset all" and "Apply to every frame in this burst". `wireTablist('#frame-tabs', 'lumen-frame-tab')` picks the new tab up with no change.

- [ ] **Step 2: Wire the controls**

Sliders fire on `change`, not `input`. Each control writes into a `develop` object holding only the fields the photographer touched; Reset deletes the key. After any change, POST to the preview route with the current viewport long edge, abort any in-flight request first (`AbortController`), and swap the returned `data:` URL into the viewport. The preview is the **whole frame**, uncropped; the Crop tab keeps its own fitted view but now draws its rectangle over this developed render, so a crop is composed on the pixels that will be exported. Save on panel close and on Export. Announce each change through the existing live region.

- [ ] **Step 2a: Disable the tab when the original is offline**

The frame payload already carries the `offline` flag that `/full/<id>` uses. When it is set, leave the Develop tab in the rail but `disabled`, with the existing offline message in the panel, because every render needs the raw. "Apply to every frame in this burst" is disabled the same way.

- [ ] **Step 3: Draw and drag the curve**

Render the histogram buckets as `<rect>`s and the curve as a `<path>` over them. Pointer drag moves the nearest point, clamped inside `0..1` and between its neighbours' x values so the x values always ascend. Each point also carries `tabindex="0"` and responds to arrow keys in 1% steps, announcing its new position — the keyboard fallback the spec asks for.

- [ ] **Step 4: Add the "Also export the untouched version" checkbox**

In the export panel in `index.html`, beside the existing export scope radios, sending `keep_original` with the export request.

- [ ] **Step 5: Style the panel**

Reuse the existing `.tab-rail`, `details` and `.button-row` rules. New rules only for the curve SVG: `--cyan` histogram bars at 40% opacity, an `--amber` curve and points, and a `--cyan` focus ring on a focused point matching `.loupe-viewport:focus-visible`.

- [ ] **Step 6: Check the JavaScript parses**

Run: `node --check src/lumen/static/app.js`
Expected: no output

- [ ] **Step 7: Verify in the running app**

Run `uv run lumen serve --open`, open a frame, move each slider and drag the curve, confirm the viewport updates, then reopen the frame and confirm the settings persisted and the contact sheet thumbnail matches.

- [ ] **Step 8: Commit**

```bash
git add src/lumen/static/
git commit -m "feat: a Develop tab with an editable tone curve and IEDT-style sections"
```

---

### Task 8: Documentation

**Files:**
- Modify: `docs/HANDOFF.md`, `README.md`, `AGENTS.md`

- [ ] **Step 1: Record the state**

In `docs/HANDOFF.md`, note that develop controls are on `main`, that `raw_developer` is built by bootstrap and required for raw export, and that `sips` is gone from the export path. In `AGENTS.md`'s "Current tool state", add the helper beside the `vision_analyzer` and darktable notes. In `README.md`, describe the Develop tab in the frame-view section.

- [ ] **Step 2: Run the full suite one last time**

Run: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest discover -s tests -v` and `node --check src/lumen/static/app.js`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add docs/ README.md AGENTS.md
git commit -m "docs: develop controls on main"
```
