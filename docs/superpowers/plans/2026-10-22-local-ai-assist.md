# Local AI Assist Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the two local oMLX vision models take repetitive judgement work off the photographer (keeper tagging with species and scene keywords, reject suggestions, burst critique, astro triage, captions, a daily digest, and text find) while every model output stays advisory, recorded in provenance, and applied only by an explicit photographer action.

**Architecture:** `omlx.py` grows one strict parser and one `OmlxTagger` method per feature; the tagger is constructed per role (`fast` or `review`) by a single `Library.tagger_for(role)` helper that reads live `/api/status` and refuses a role whose model is not loaded. Bulk work (tagging, reject scan, astro triage) rides the existing `start_tag_job` worker rule and the single `LocalJobRunner` slot; one-shot work (critique, caption, digest) runs synchronously under `_mutation()` like `review_composition`. All results land in the existing `tags.data` JSON and provenance events, so sidecars, filters, and the inspector pick them up through code paths that already exist.

**Tech Stack:** Python 3.12+, SQLite JSON1, Pillow (only runtime dependency), `urllib` to loopback oMLX, vanilla HTML/CSS/JS, unittest with the Node `vm` harness in `tests/test_web.py`.

**Spec:** `docs/superpowers/specs/2026-10-22-local-ai-assist-design.md` (builds on `docs/superpowers/specs/2026-10-22-travel-volume-culling-design.md`, whose plan is fully landed on `main`: background jobs, decisions, filters, share export).

## Global Constraints

- **Advisory only.** No feature changes a pick, decision, crop, keyword, or export without an explicit photographer action. The one exception is keyword application inside the tag job, which the photographer requested by running it and can undo per keyword. "Use this pick", "Reject", "Keep", "Reject all shown", "Add", "Exclude flagged lights", and digest overwrite are all button presses; none fires on render, poll, or page load.
- **Live model-status gating.** Before any job or one-shot call starts, Lumen reads oMLX `/api/status` and refuses a role whose model is not in `loaded_models`, with the public message `"<role> model <name> is not loaded in oMLX"`. Configured names are never trusted over live status. The browser disables AI buttons from `/api/status` `omlx_roles[<role>].loaded`, and the server re-checks on every request.
- **Two roles.** `Settings.omlx_fast_model` (`LUMEN_OMLX_FAST_MODEL`, default `Qwen3-VL-8B-Instruct-4bit`) for tagging, species, reject scan, astro triage; `Settings.omlx_review_model` (`LUMEN_OMLX_REVIEW_MODEL`, default `settings.omlx_model`) for burst critique, captions, digest, and the existing composition review.
- **Transport and prompts.** Every model call goes through `OmlxTagger` methods in `omlx.py`: loopback-only URL, JSON-only prompt, a strict parser that rejects missing or extra fields, bounded `max_tokens`, base64 `image_url` parts, and the sentence "Do not identify a person." in every image prompt. Digest sends no images.
- **Provenance and privacy.** Every call records `model`, `role`, `confidence`, and `raw_response` in a provenance event. Browser responses carry parsed fields only; `/api/photos/<id>` and job status never include `raw_response`, source paths, fingerprints, or API keys.
- **Vocabulary.** `<library>/vocabulary.json` is `{"schema_version": 1, "categories": {"wildlife": [...], "scene": [...], "conditions": [...], "activity": [...]}}`. Lumen copies the shipped starter from `src/lumen/data/vocabulary.json` into the library on first use if absent and never overwrites an existing file. Models may add at most three free-text keywords, stored as `tags.data.suggested`; species below confidence 0.6 are suggested, not applied.
- **Jobs.** Bulk model work runs through the local job runner, one heavy job at a time, with progress labels (basenames only), cancel between items, and per-item failure capture (`tag_failed` pattern) that never stops the job.
- **Library-only outputs.** Vocabulary, digests (`<library>/digests/<date>.md`), and every other output stay inside the configured library. Camera originals are never touched.
- **Tests.** Tests use fake taggers and Pillow-generated images; no test calls oMLX. Live status is faked by patching `lumen.library.get_server_status` (or `lumen.diagnostics.get_server_status`) to return `{"version", "default_model", "loaded_models"}`. Run `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest discover -s tests -v` and `node --check src/lumen/static/app.js` before any task is called complete.
- **Catalog.** `tags.data` is a JSON blob; new keys (`species`, `suggested`, `problems`, `sky`, `issues`, `caption`, `alt_text`) are optional and added by read-modify-write of the existing row, never by replacing unrelated keys. No schema migration is needed.
- **Accessibility.** Native controls, visible focus, 44 px targets, live announcements, non-colour cue for every state, charcoal/amber/cyan palette; shortcuts never fire when the event target is an input, textarea, select, or dialog form control (existing `handleCullingKey` rule).
- **Review.** Each task ends with an independent review (fresh reviewer, spec and this plan in hand) against the spec feature, these constraints, and the task's Review Focus lines; findings are fixed before the task's commit.

## Review Focus

- **Stale model status between page load and click.** The photographer unloads a model after the page rendered its buttons enabled; the server must still answer 400 `"<role> model <name> is not loaded in oMLX"` without creating a job row or touching the catalog. Task 1 tests the server re-check; Task 2 tests that a job row is not created on refusal.
- **Model returns a keyword outside the vocabulary spelled with different case or whitespace.** `" Gentoo Penguin "` must match vocabulary entry `gentoo penguin` and be applied once, not duplicated and not demoted to suggested; Task 1 tests case-insensitive, whitespace-trimmed matching with the vocabulary's own spelling stored.
- **Critique or triage on a group whose photo set changed mid-call.** If a frame is removed or the group merged while the review model is answering, the returned `frame_id` may no longer be in the group; the result must be recorded as `burst_review_failed`, the pick untouched, and the browser told the burst changed. Task 3 tests a fake tagger that removes a photo during the call.
- **"Reject all shown" after the list changed.** The confirmation states a count; if the suggestion list re-rendered between confirmation and submit (a poll finished, a key press rejected one row), the request must carry the exact photo ids shown at confirmation time and the server must reject only those, 400 if any is no longer suggested. Task 2 tests ids-not-count semantics.
- **Digest for a day with zero frames or an unloaded review model.** `lumen digest 2026-10-23` on an empty day writes the facts table with zero counts and the sentence "No frames were imported for this day." and never calls the model; with frames but no review model loaded it writes the facts table, appends "Narrative unavailable: review model <name> is not loaded in oMLX", exits 0, and records `digest_written` with `narrative: false`. Task 6 tests both.

---

### Task 1: Model roles, live gating, vocabulary, keeper tagging with species and scene

**Files:**
- Modify: `src/lumen/config.py` (`omlx_fast_model`, `omlx_review_model`, `vocabulary_path`, `digest_dir`)
- Create: `src/lumen/data/vocabulary.json` (starter vocabulary; add `"lumen" = ["data/*.json", ...]` to `[tool.setuptools.package-data]` or the equivalent in `pyproject.toml`)
- Create: `src/lumen/vocabulary.py` (`load_vocabulary`, `match_keywords`)
- Modify: `src/lumen/omlx.py` (`TagResult` gains `species`, `conditions`; `parse_tag_response(raw, vocabulary)`; `OmlxTagger.tag(preview, vocabulary)`; `OmlxTagger.role`)
- Modify: `src/lumen/diagnostics.py` (`omlx_roles` in the report)
- Modify: `src/lumen/library.py` (`tagger_for`, `tag_photo` species/scene handling, `start_tag_job(scope=...)`)
- Modify: `src/lumen/web.py` (`GET /api/vocabulary`, `POST /api/photos/tag` with scope, `/api/status` roles)
- Modify: `src/lumen/cli.py` (`tag --keepers`, `Settings(...)` copy in `main` carries the new fields, `doctor` prints roles)
- Modify: `src/lumen/static/index.html`, `app.js`, `styles.css` (Export panel "Tag keepers with local AI" button, inspector keywords with "Add" buttons, role lines in system status)
- Test: `tests/test_omlx.py`, `tests/test_vocabulary.py` (new), `tests/test_library.py`, `tests/test_web.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `Library.start_tag_job` worker rule, `Library.keeper_ids()`, `Catalog.save_tags/get_tags`, `get_server_status`, `doctor_report`, `/api/photos/tag-all` route and `#tag-all` button (kept; `tag-all` becomes `scope: "all"` internally).
- Produces:
  - `Settings(omlx_fast_model: str = "Qwen3-VL-8B-Instruct-4bit", omlx_review_model: str | None = None)`; `Settings.review_model -> str` property returning `omlx_review_model or omlx_model`; `Settings.vocabulary_path -> library_dir / "vocabulary.json"`; `Settings.digest_dir -> library_dir / "digests"`. `from_environment` reads `LUMEN_OMLX_FAST_MODEL` and `LUMEN_OMLX_REVIEW_MODEL`.
  - `ROLES = {"fast": "omlx_fast_model", "review": "review_model"}` in `omlx.py`; `OmlxTagger(base_url, model, api_key=None, *, role: str = "review")` stores `self.role`; `_complete` unchanged apart from a `max_tokens` keyword (default 500).
  - `vocabulary.load_vocabulary(settings) -> dict[str, list[str]]`: copies the packaged starter to `settings.vocabulary_path` when absent (atomic write), then loads it; raises `ValueError("vocabulary.json is invalid: <reason>")` on bad shape (non-dict categories, non-string entries). `vocabulary.all_keywords(vocab) -> list[str]` (flattened, order preserved). `vocabulary.match_keywords(candidates: list[str], vocab) -> tuple[list[str], list[str]]` returns `(applied, suggested)`: a candidate matches when `candidate.strip().casefold()` equals a vocabulary entry's `casefold()`, and the applied list stores the vocabulary spelling once; non-matching candidates go to `suggested` trimmed, deduplicated case-insensitively, capped at 3.
  - `TagResult(description, subjects, scene, technical, confidence, raw_response, species: list[dict] = (), conditions: list[str] = ())` where each species item is `{"name": str, "confidence": float}`. `parse_tag_response(raw, *, allow_species: bool = True)` accepts the existing fields plus optional `species` (list of `{name, confidence}` with confidence a finite number 0..1) and optional `conditions` (string list); any other key is rejected with `TagResponseError("oMLX tag response contains unexpected fields")`. Existing tests that pass only the old five fields keep passing.
  - `OmlxTagger.tag(preview, timeout=240, *, vocabulary: dict[str, list[str]] | None = None) -> TagResult`: when a vocabulary is given, the prompt lists each category and its keywords and asks for `species` and `conditions` as above, with "Choose keywords from the list where one fits; add at most three others. Do not identify a person."
  - `Library.tagger_for(role: str) -> OmlxTagger`: `role in ROLES` else `ValueError`; model = `getattr(settings, ROLES[role])`; reads `get_server_status(settings.omlx_url)`; raises `RuntimeError(f"{role} model {model} is not loaded in oMLX")` when the model is absent or oMLX is offline (`OmlxError` is wrapped with the same message plus `"; oMLX is offline"`); returns `OmlxTagger(settings.omlx_url, model, role=role)`. Web and CLI construct taggers only through this helper from now on (composition review switches to `tagger_for("review")`).
  - `Library.tag_photo(photo_id, tagger, *, job_id=None)`: loads the vocabulary, calls `tagger.tag(preview, vocabulary=vocab)`, computes `applied, suggested = match_keywords(result.subjects + result.scene_keywords + result.conditions, vocab)` where `scene_keywords` is `[result.scene]` when non-empty, adds species with `confidence >= 0.6` whose name matches the vocabulary to `applied` (deduplicated) and every other species name to the end of `suggested` (the cap of three applies to free-text keywords only; species are already labelled suggestions); stores `tags.data = {description, subjects: applied, scene, technical, confidence, species, conditions, suggested}` keeping any existing `problems/sky/issues/caption/alt_text` keys; event `tagged` details gain `role`, `applied`, `suggested`. A `tag_failed` event gains `role`.
  - `Library.start_tag_job(tagger, *, scope: str | list[int] = "keepers", retag: bool = False) -> Job`: `scope` is `"keepers"` (photo ids from `keeper_ids()`), `"all"`, or a non-empty list of distinct ints that exist (else `ValueError("unknown photo ID: n")`); manifest `{"kind":"tag","scope": "keepers"|"all"|[ids], "retag", "role": tagger.role, "model": tagger.model}` and at the end `failed_photo_ids` and `summary`. Untagged-only unless `retag`.
  - `Library.add_suggested_keyword(photo_id, keyword) -> dict`: moves one keyword from `suggested` to `subjects` (400-style `ValueError` when not suggested), appends event `keyword_added` `{"keyword"}`, rewrites the sidecar, returns `{"photo_id", "subjects", "suggested"}`. `Library.remove_keyword(photo_id, keyword) -> dict`: removes from `subjects` into `suggested`, event `keyword_removed`. These are the per-keyword undo the spec requires.
  - `doctor_report` adds `"omlx_roles": {"fast": {"model", "loaded": bool}, "review": {"model", "loaded": bool}}` from the same live status read; `/api/status` carries it through unchanged.
  - Web: `GET /api/vocabulary` → `{"categories": {...}}`; `POST /api/photos/tag {"scope": "keepers"|"all"|[ints], "retag"?: bool}` → 202 job status, strict keys, 409 on another heavy job, 400 `"fast model <name> is not loaded in oMLX"` without creating a job; `POST /api/photos/tag-all` stays as an alias that calls the same code with `scope: "all"`; `POST /api/photos/<id>/keywords {"action": "add"|"remove", "keyword": str}` → 200 `{"photo_id","subjects","suggested"}`; `/api/photos/<id>` returns `tags` with `data` including `suggested` and `species` but strips nothing new (there is no raw text in `tags.data`).
  - CLI: `lumen tag --keepers [--retag] [--json]` and `lumen tag --all` both go through the job path; `lumen tag --photo N` uses `tagger_for("fast")`. `lumen doctor` text output prints `fast: <model> (loaded|not loaded)` and `review: ...`; exit code unchanged.
  - UI: Export panel gets `button#tag-keepers` "Tag keepers with local AI (N keepers)" before the preset fieldset, disabled when `state.busy`, when `keeperCount` is 0, or when `state.status.omlx_roles.fast.loaded` is false, with `#tag-keepers-availability` text "Fast model <name> is not loaded in oMLX. Load it in oMLX, then reload this page." when disabled for that reason. Inspector: `ul#keywords` of applied keywords, each `<li>` text plus a "Remove" button (`aria-label="Remove keyword <k>"`), and `ul#suggested-keywords` whose items have an "Add" button (`aria-label="Add keyword <k>"`); species appear as text "Species: gentoo penguin (82%)" inside the applied list when applied, inside suggested otherwise. `#system-status` text becomes `"<summary> · fast <model> loaded|not loaded · review <model> loaded|not loaded"`.

- [ ] **Step 1: Write failing tests.**

`tests/test_vocabulary.py`:

```python
import json, tempfile, unittest
from pathlib import Path
from lumen.config import Settings
from lumen.vocabulary import load_vocabulary, match_keywords, all_keywords


class VocabularyTests(unittest.TestCase):
    def test_starter_is_copied_once_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(library_dir=Path(directory))
            settings.ensure_directories()
            first = load_vocabulary(settings)
            self.assertIn("gentoo penguin", first["wildlife"])
            settings.vocabulary_path.write_text(json.dumps({"schema_version": 1, "categories": {"wildlife": ["orca"]}}))
            self.assertEqual({"wildlife": ["orca"]}, load_vocabulary(settings))

    def test_invalid_file_is_reported_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(library_dir=Path(directory))
            settings.ensure_directories()
            settings.vocabulary_path.write_text('{"categories": {"wildlife": [1]}}')
            with self.assertRaisesRegex(ValueError, "vocabulary.json is invalid"):
                load_vocabulary(settings)
            self.assertEqual('{"categories": {"wildlife": [1]}}', settings.vocabulary_path.read_text())

    def test_matching_is_case_and_whitespace_insensitive_and_caps_suggestions(self):  # Review Focus
        vocab = {"wildlife": ["gentoo penguin"], "scene": ["ice", "zodiac"]}
        applied, suggested = match_keywords([" Gentoo Penguin ", "ICE", "ice", "leopard", "Leopard", "krill", "skua", "fog"], vocab)
        self.assertEqual(["gentoo penguin", "ice"], applied)
        self.assertEqual(["leopard", "krill", "skua"], suggested)
        self.assertEqual(["gentoo penguin", "ice", "zodiac"], all_keywords(vocab))
```

`tests/test_omlx.py` additions:

```python
    def test_tag_response_accepts_species_and_conditions_and_rejects_extra_fields(self):
        raw = json.dumps({"description": "Two penguins on ice", "subjects": ["gentoo penguin"], "scene": "ice",
                          "technical": [], "confidence": 0.9,
                          "species": [{"name": "gentoo penguin", "confidence": 0.82}], "conditions": ["overcast"]})
        result = parse_tag_response(raw)
        self.assertEqual([{"name": "gentoo penguin", "confidence": 0.82}], result.species)
        self.assertEqual(["overcast"], result.conditions)
        for bad in ({"species": [{"name": "x"}]}, {"species": [{"name": "x", "confidence": 2}]}, {"people": ["a"]}):
            with self.subTest(bad=bad), self.assertRaises(TagResponseError):
                parse_tag_response(json.dumps({"description": "d", "subjects": [], "scene": "", "technical": [], "confidence": 0.5} | bad))

    def test_tag_prompt_lists_vocabulary_and_forbids_identifying_people(self):
        tagger = OmlxTagger("http://127.0.0.1:8000", "fast-model", api_key="k", role="fast")
        captured = {}

        def fake_complete(content, timeout, **kwargs):
            captured["content"] = content
            captured["kwargs"] = kwargs
            return json.dumps({"description": "d", "subjects": [], "scene": "", "technical": [], "confidence": 0.5, "species": [], "conditions": []})

        with tempfile.TemporaryDirectory() as directory:
            preview = Path(directory) / "p.jpg"
            Image.new("RGB", (8, 8)).save(preview)
            with patch.object(tagger, "_complete", fake_complete):
                tagger.tag(preview, vocabulary={"wildlife": ["gentoo penguin"], "scene": ["ice"]})
        text = captured["content"][0]["text"]
        self.assertIn("wildlife: gentoo penguin", text)
        self.assertIn("Do not identify a person.", text)
        self.assertEqual("fast", tagger.role)
```

`tests/test_library.py` additions:

```python
    def test_tagger_for_refuses_roles_whose_model_is_not_loaded(self):
        live = {"version": "0.7.2", "default_model": None, "loaded_models": ["Qwen3-VL-8B-Instruct-4bit"]}
        with tempfile.TemporaryDirectory() as directory, patch("lumen.library.get_server_status", return_value=live):
            with Library(Settings(library_dir=Path(directory))) as library:
                self.assertEqual(("fast", "Qwen3-VL-8B-Instruct-4bit"), (library.tagger_for("fast").role, library.tagger_for("fast").model))
                with self.assertRaisesRegex(RuntimeError, "review model Qwen3.8-27B-4bit is not loaded in oMLX"):
                    library.tagger_for("review")
                with self.assertRaises(ValueError):
                    library.tagger_for("judge")
        with tempfile.TemporaryDirectory() as directory, patch("lumen.library.get_server_status", side_effect=OmlxError("down")):
            with Library(Settings(library_dir=Path(directory))) as library:
                with self.assertRaisesRegex(RuntimeError, "fast model .* is not loaded in oMLX; oMLX is offline"):
                    library.tagger_for("fast")

    def test_tag_job_keepers_scope_applies_vocabulary_and_high_confidence_species_only(self):
        class FakeTagger:
            model, role = "fast-model", "fast"
            calls = []

            def tag(self, preview, *, vocabulary=None):
                FakeTagger.calls.append(sorted(vocabulary))
                return TagResult("Penguins", ["Gentoo Penguin", "snow", "krill"], "ice", [], 0.9, '{"raw": true}',
                                 species=[{"name": "gentoo penguin", "confidence": 0.82}, {"name": "chinstrap penguin", "confidence": 0.4}],
                                 conditions=["overcast", "spindrift"])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(3):
                Image.new("RGB", (120, 90), (index, 0, 0)).save(root / f"p{index}.jpg")
            with Library(Settings(library_dir=root / "library")) as library:
                library.import_path(root)
                photos = library.catalog.list_photos()
                library.set_decision(photos[2].id, "reject")  # three single-frame bursts: two keepers, one rejected
                job = library.start_tag_job(FakeTagger(), scope="keepers")
                library.wait_for_job(job.id)
                status = library.job_status(job.id)
                data = library.catalog.get_tags(photos[0].id)["data"]
                event = [e for e in library.catalog.events_for(photos[0].id) if e.kind == "tagged"][-1]
                sidecar = json.loads(library.sidecars.paths(photos[0])[0].read_text())
                xmp = library.sidecars.paths(photos[0])[1].read_text()
                self.assertIsNone(library.catalog.get_tags(photos[2].id))
        self.assertEqual("succeeded", status["status"])
        self.assertEqual(2, status["processed"])
        self.assertEqual(["gentoo penguin", "ice", "overcast"], data["subjects"])
        self.assertEqual(["snow", "krill", "spindrift"], data["suggested"][:3])
        self.assertIn("chinstrap penguin", data["suggested"])
        self.assertEqual("fast", event.details["role"])
        self.assertEqual('{"raw": true}', event.details["raw_response"])
        self.assertEqual(["gentoo penguin", "ice", "overcast"], sidecar["keywords"])
        self.assertIn("<rdf:li>gentoo penguin</rdf:li>", xmp)
        self.assertNotIn("krill", xmp)
        self.assertEqual(2, len(FakeTagger.calls))
        self.assertIn("wildlife", FakeTagger.calls[0])

    def test_tag_job_explicit_ids_validate_and_keyword_add_remove_round_trip(self):
        class FakeTagger:
            model, role = "fast-model", "fast"
            def tag(self, preview, *, vocabulary=None):
                return TagResult("d", ["ice"], "", [], 0.9, "{}", species=[], conditions=[])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new("RGB", (120, 90)).save(root / "p.jpg")
            with Library(Settings(library_dir=root / "library")) as library:
                library.import_path(root / "p.jpg")
                photo_id = library.catalog.list_photos()[0].id
                with self.assertRaisesRegex(ValueError, "unknown photo ID: 99"):
                    library.start_tag_job(FakeTagger(), scope=[photo_id, 99])
                with self.assertRaises(ValueError):
                    library.start_tag_job(FakeTagger(), scope=[])
                job = library.start_tag_job(FakeTagger(), scope=[photo_id])
                library.wait_for_job(job.id)
                library.catalog.save_tags(photo_id, library.catalog.get_tags(photo_id)["data"] | {"suggested": ["krill"]}, "fast-model")
                added = library.add_suggested_keyword(photo_id, "krill")
                self.assertEqual((["ice", "krill"], []), (added["subjects"], added["suggested"]))
                removed = library.remove_keyword(photo_id, "ice")
                self.assertEqual((["krill"], ["ice"]), (removed["subjects"], removed["suggested"]))
                with self.assertRaises(ValueError):
                    library.add_suggested_keyword(photo_id, "orca")
                kinds = [e.kind for e in library.catalog.events_for(photo_id)]
                self.assertEqual(["keyword_added", "keyword_removed"], kinds[-2:])
```

`tests/test_web.py` additions (in the existing server test class, using `self.post_json`/`self.get_json`; patch `lumen.library.get_server_status` for the whole test):

```python
    def test_tag_scope_route_gates_on_live_model_and_never_creates_a_job_when_refused(self):  # Review Focus
        class FakeTagger:
            model, role = "fast-model", "fast"
            def __init__(self, *args, **kwargs): self.role = kwargs.get("role", "review")
            def tag(self, preview, *, vocabulary=None):
                from lumen.omlx import TagResult
                return TagResult("A frame", ["ice"], "", [], 0.9, "{}", species=[], conditions=[])

        offline = {"version": None, "default_model": None, "loaded_models": []}
        with patch("lumen.library.get_server_status", return_value=offline):
            code, body = self.post_json("/api/photos/tag", {"scope": "keepers"})
            self.assertEqual((400, {"error": "fast model Qwen3-VL-8B-Instruct-4bit is not loaded in oMLX"}), (code, body))
            self.assertEqual([], self.get_json("/api/jobs")[1]["jobs"])
        loaded = {"version": None, "default_model": None, "loaded_models": ["Qwen3-VL-8B-Instruct-4bit"]}
        Image.new("RGB", (120, 90)).save(self.root / "photo.jpg")
        with patch("lumen.library.get_server_status", return_value=loaded), patch("lumen.library.OmlxTagger", FakeTagger):
            self.wait_job(self.post_json("/api/import", {"path": str(self.root / "photo.jpg")})[1]["job_id"])
            for payload in ({"scope": "some"}, {"scope": [1, 1]}, {"scope": ["1"]}, {"scope": "keepers", "extra": 1}, {}):
                with self.subTest(payload=payload):
                    self.assertEqual(400, self.post_json("/api/photos/tag", payload)[0])
            code, job = self.post_json("/api/photos/tag", {"scope": "keepers"})
            self.assertEqual(202, code)
            final = self.wait_job(job["job_id"])
            self.assertEqual("succeeded", final["status"])
            self.assertNotIn("raw_response", json.dumps(final))
            code, status = self.get_json("/api/status")
            self.assertEqual({"model": "Qwen3-VL-8B-Instruct-4bit", "loaded": True}, status["omlx_roles"]["fast"])
            self.assertFalse(status["omlx_roles"]["review"]["loaded"])
            code, vocab = self.get_json("/api/vocabulary")
            self.assertIn("wildlife", vocab["categories"])
            photo = self.get_json("/api/photos/1")[1]
            self.assertEqual(["ice"], photo["tags"]["data"]["subjects"])
            self.assertNotIn("raw_response", json.dumps(photo))
            self.assertEqual(403, self.post_json("/api/photos/1/keywords", {"action": "remove", "keyword": "ice"}, headers={"Origin": "https://attacker.example"})[0])
            self.assertEqual(400, self.post_json("/api/photos/1/keywords", {"action": "drop", "keyword": "ice"})[0])
            code, body = self.post_json("/api/photos/1/keywords", {"action": "remove", "keyword": "ice"})
            self.assertEqual((200, [], ["ice"]), (code, body["subjects"], body["suggested"]))

    def test_export_panel_has_keeper_tag_button_and_inspector_lists_keywords_with_add_remove(self):
        html = (Path(__file__).parent.parent / "src/lumen/static/index.html").read_text()
        self.assertIn('id="tag-keepers"', html)
        self.assertIn('id="tag-keepers-availability"', html)
        js = (Path(__file__).parent.parent / "src/lumen/static/app.js").read_text()
        self.assertIn("omlx_roles", js)
        self.assertIn("suggested-keywords", js)

    def test_javascript_keywords_add_and_remove_without_reload_and_tag_keepers_gates_on_role(self):
        self.run_javascript_cleanup_regression("keywords")
```

Node scenario `keywords` (add to the harness script in `run_javascript_cleanup_regression`, following the `decisions` scenario style): `/api/status` returns `omlx_roles: {fast: {model: 'Qwen3-VL-8B-Instruct-4bit', loaded: scenario !== 'keywords-unloaded'}, review: {...loaded: false}}`; `/api/photos/1` returns `tags: {data: {subjects: ['ice'], suggested: ['krill'], species: [{name: 'gentoo penguin', confidence: 0.82}]}}`; assert `#tag-keepers` text is `Tag keepers with local AI (2 keepers)` and enabled; clicking the suggested "Add" button posts `/api/photos/1/keywords {action:'add', keyword:'krill'}` and the inspector re-renders with `krill` in `#keywords` and no `load()` call; the announcer says `Keyword krill added to <name>`. Scenario `keywords-unloaded`: `#tag-keepers` disabled and `#tag-keepers-availability` text starts with `Fast model Qwen3-VL-8B-Instruct-4bit is not loaded in oMLX`.

`tests/test_cli.py` additions:

```python
    def test_tag_keepers_runs_job_and_doctor_reports_both_roles(self):
        class FakeTagger:
            calls = 0
            def __init__(self, *args, **kwargs):
                self.model, self.role = "fast-model", kwargs.get("role", "review")
            def tag(self, preview, *, vocabulary=None):
                FakeTagger.calls += 1
                return TagResult("d", ["ice"], "", [], 0.9, "{}", species=[], conditions=[])

        live = {"version": "0.7.2", "default_model": None, "loaded_models": ["Qwen3-VL-8B-Instruct-4bit"]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(2):
                Image.new("RGB", (120, 90), (index, 0, 0)).save(root / f"p{index}.jpg")
            main(["--library", str(root / "library"), "import", str(root)])
            output = io.StringIO()
            with patch("lumen.library.OmlxTagger", FakeTagger), patch("lumen.library.get_server_status", return_value=live), redirect_stdout(output):
                code = main(["--library", str(root / "library"), "tag", "--keepers", "--json"])
            self.assertEqual(0, code)
            self.assertEqual(2, FakeTagger.calls)
            self.assertEqual({"tagged": 2, "skipped": 0, "failed": 0}, json.loads(output.getvalue())["result"]["summary"])
            output = io.StringIO()
            with patch("lumen.diagnostics.get_server_status", return_value=live), patch("lumen.diagnostics.tool_status", return_value=ToolStatus("t", False, None, None)), patch("lumen.diagnostics._find_model", return_value=None), redirect_stdout(output):
                main(["--library", str(root / "library"), "doctor", "--json"])
            roles = json.loads(output.getvalue())["omlx_roles"]
            self.assertEqual({"fast": {"model": "Qwen3-VL-8B-Instruct-4bit", "loaded": True}, "review": {"model": "Qwen3.8-27B-4bit", "loaded": False}}, roles)
```

- [ ] **Step 2: Run the focused modules and confirm they fail for the right reasons.**

Run: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_vocabulary tests.test_omlx tests.test_library tests.test_web tests.test_cli -v`
Expected: `ModuleNotFoundError: lumen.vocabulary`, `TypeError` for unknown `species`/`role` keywords, `AttributeError: tagger_for`, 404s for the new routes, missing HTML ids.

- [ ] **Step 3: Implement settings, vocabulary, parser, and tagger.**

`config.py`: add the two fields and properties; copy the `Settings(...)` construction in `cli.main` so it carries `omlx_fast_model`, `omlx_review_model` (use `dataclasses.replace(settings, library_dir=...)` instead, which is shorter and future-proof).

`src/lumen/data/vocabulary.json`:

```json
{
  "schema_version": 1,
  "categories": {
    "wildlife": ["gentoo penguin", "chinstrap penguin", "adelie penguin", "emperor penguin", "weddell seal", "crabeater seal", "leopard seal", "southern elephant seal", "antarctic fur seal", "humpback whale", "minke whale", "orca", "wandering albatross", "black-browed albatross", "southern giant petrel", "cape petrel", "snow petrel", "antarctic tern", "skua", "kelp gull", "blue-eyed shag"],
    "scene": ["iceberg", "sea ice", "glacier", "pack ice", "zodiac", "ship", "research station", "mountain", "bay", "beach", "colony"],
    "conditions": ["aurora", "milky way", "night sky", "twilight", "golden hour", "overcast", "fog", "snowfall", "wind", "calm water"],
    "activity": ["feeding", "porpoising", "hauling out", "nesting", "swimming", "breaching", "landing", "hiking", "kayaking"]
  }
}
```

`vocabulary.py`:

```python
from __future__ import annotations
import json
from importlib import resources
from .config import Settings
from .sidecars import SidecarWriter

MAX_SUGGESTED = 3


def load_vocabulary(settings: Settings) -> dict[str, list[str]]:
    path = settings.vocabulary_path
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        SidecarWriter._atomic_write(path, resources.files("lumen").joinpath("data/vocabulary.json").read_text())
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"vocabulary.json is invalid: {error}") from error
    categories = payload.get("categories") if isinstance(payload, dict) else None
    if not isinstance(categories, dict) or not all(
            isinstance(name, str) and isinstance(words, list) and all(isinstance(w, str) and w.strip() for w in words)
            for name, words in categories.items()):
        raise ValueError("vocabulary.json is invalid: categories must map names to lists of keywords")
    return {name: [w.strip() for w in words] for name, words in categories.items()}


def all_keywords(vocab: dict[str, list[str]]) -> list[str]:
    return [word for words in vocab.values() for word in words]


def match_keywords(candidates: list[str], vocab: dict[str, list[str]]) -> tuple[list[str], list[str]]:
    canonical = {word.casefold(): word for word in all_keywords(vocab)}
    applied, suggested, seen = [], [], set()
    for candidate in candidates:
        key = candidate.strip().casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        if key in canonical:
            applied.append(canonical[key])
        elif len(suggested) < MAX_SUGGESTED:
            suggested.append(candidate.strip())
    return applied, suggested
```

`omlx.py`: `ROLES = {"fast": "omlx_fast_model", "review": "review_model"}`; `TagResult` gains `species: tuple[dict, ...] = ()` and `conditions: tuple[str, ...] = ()` (use lists in the parser output; keep the dataclass frozen). In `parse_tag_response`, after the existing checks:

```python
    allowed = {"description", "subjects", "scene", "technical", "confidence", "species", "conditions"}
    if set(payload) - allowed:
        raise TagResponseError("oMLX tag response contains unexpected fields", raw)
    species = payload.get("species", [])
    if not isinstance(species, list) or not all(
            isinstance(item, dict) and set(item) == {"name", "confidence"} and isinstance(item["name"], str) and item["name"].strip()
            and isinstance(item["confidence"], (int, float)) and not isinstance(item["confidence"], bool)
            and math.isfinite(item["confidence"]) and 0 <= item["confidence"] <= 1 for item in species):
        raise TagResponseError("oMLX species must be a list of {name, confidence} objects", raw)
    conditions = payload.get("conditions", [])
    if not isinstance(conditions, list) or not all(isinstance(value, str) for value in conditions):
        raise TagResponseError("oMLX conditions must be a list of strings", raw)
```

and pass `species=[{"name": i["name"].strip(), "confidence": float(i["confidence"])} for i in species]`, `conditions=[c.strip() for c in conditions if c.strip()]`. `OmlxTagger.__init__` takes `role: str = "review"`. `tag()` builds the prompt: when `vocabulary` is given, append `"Keyword list by category: " + "; ".join(f"{name}: {', '.join(words)}" ...)` and ask for `species (array of {name, confidence}) and conditions (string array)` with the sentence from Interfaces. `_complete(content, timeout, *, max_tokens=500)`.

- [ ] **Step 4: Implement library, diagnostics, web, CLI, and UI.**

`library.py`: import `ROLES, get_server_status` from `.omlx`, `load_vocabulary, match_keywords` from `.vocabulary`.

```python
    def tagger_for(self, role: str) -> OmlxTagger:
        if role not in ROLES:
            raise ValueError(f"unknown model role: {role}")
        model = getattr(self.settings, ROLES[role])
        try:
            loaded = get_server_status(self.settings.omlx_url)["loaded_models"]
        except (OmlxError, ValueError):
            raise RuntimeError(f"{role} model {model} is not loaded in oMLX; oMLX is offline") from None
        if model not in loaded:
            raise RuntimeError(f"{role} model {model} is not loaded in oMLX")
        return OmlxTagger(self.settings.omlx_url, model, role=role)
```

`tag_photo`: load the vocabulary once per call (cheap JSON read), call `tagger.tag(Path(photo.preview_path), vocabulary=vocab)`, merge as in Interfaces, and `save_tags(photo_id, (existing_data | new_fields), tagger.model)` so later-task keys survive a retag. `start_tag_job(tagger, *, scope="keepers", retag=False)`: validate scope before creating the job; `photos = [catalog.get_photo(i) for i in ids]`. `add_suggested_keyword`/`remove_keyword` are `@_coordinated` and rewrite the sidecar. `review_composition` callers switch to `tagger_for("review")` in web and CLI.

`diagnostics.py`: compute `omlx_roles` inside the existing live-status try block (`loaded_models` is `[]` when offline).

`web.py`: `GET /api/vocabulary`; `POST /api/photos/tag` with `_tag_scope(payload)` validation (`scope` required; `"keepers"`, `"all"`, or list of distinct positive ints; `retag` bool); `/api/photos/tag-all` calls the same handler with `scope="all"`; `/api/photos/<id>/keywords` strict `{"action","keyword"}`; the public-message `job_route` prefix tuple gains `"/api/photos/tag"`. Taggers come from `library.tagger_for(...)`; a `RuntimeError` whose text ends with `is not loaded in oMLX` (or `; oMLX is offline`) is answered 400 with that text verbatim (it contains no paths).

`cli.py`: `tag` gains `--keepers` (mutually exclusive with `--all` and `--photo`); job path uses `library.tagger_for("fast")`; `doctor` text mode prints the two role lines.

`index.html`/`app.js`/`styles.css`: `#tag-keepers` + `#tag-keepers-availability` in the Export panel; `renderInspector` renders `#keywords`/`#suggested-keywords` with Remove/Add buttons calling `post('/api/photos/<id>/keywords', ...)`, updating `state.details.tags.data` and re-rendering in place, announcing `Keyword <k> added to <name>` / `removed from`; `updateWorkflow` computes `const fastLoaded = state.status?.omlx_roles?.fast?.loaded === true` and sets the button label `Tag keepers with local AI (${keepers} keeper${keepers === 1 ? '' : 's'})`; `load()` appends the role lines to `#system-status`. Keyword buttons get `min-height: 44px` via the existing `.secondary` rule.

- [ ] **Step 5: Run the focused modules, then the full suite and node check.**

Run: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_vocabulary tests.test_omlx tests.test_library tests.test_web tests.test_cli -v`, then `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest discover -s tests -v`, then `node --check src/lumen/static/app.js`.
Expected: PASS, no `ResourceWarning`, no leaked thread warnings. Existing `test_doctor_reports_required_and_optional_capabilities` still sees `omlx_model.name == "Qwen3.8-27B-4bit"`.

- [ ] **Step 6: Independent review** of Task 1 against spec features 0 and 1, the Global Constraints (advisory keyword application with per-keyword undo, live gating, vocabulary ownership, privacy), and its two Review Focus lines; fix findings red-green.

- [ ] **Step 7: Commit.**

```bash
git add pyproject.toml src/lumen/config.py src/lumen/data/vocabulary.json src/lumen/vocabulary.py src/lumen/omlx.py src/lumen/diagnostics.py src/lumen/library.py src/lumen/web.py src/lumen/cli.py src/lumen/static tests/test_vocabulary.py tests/test_omlx.py tests/test_library.py tests/test_web.py tests/test_cli.py
git commit -m "feat: model roles, live gating, vocabulary, and keeper tagging with species"
```

### Task 2: Reject suggestions and the review panel

**Files:**
- Modify: `src/lumen/omlx.py` (`PROBLEMS`, `RejectReview`, `parse_reject_response`, `OmlxTagger.review_rejects`)
- Modify: `src/lumen/library.py` (`start_reject_review_job`, `reject_suggestions`, `apply_reject_suggestions`)
- Modify: `src/lumen/web.py` (`POST /api/photos/review-rejects`, `GET /api/photos/reject-suggestions`, `POST /api/photos/reject-suggestions/decide`)
- Modify: `src/lumen/cli.py` (`review-rejects`)
- Modify: `src/lumen/static/index.html`, `app.js`, `styles.css` (Review tab)
- Test: `tests/test_omlx.py`, `tests/test_library.py`, `tests/test_web.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `Library.tagger_for("fast")`, `Library.keeper_ids()`, `Library.set_decision(photo_id, decision)`, the Task 1 job worker rule and `tags.data` read-modify-write, `watchJob`, `decide()` in `app.js`.
- Produces:
  - `PROBLEMS = ("eyes_closed", "motion_blur", "out_of_focus", "subject_cut_off", "obstructed", "misfire", "severely_under_exposed", "severely_over_exposed")`; `RejectReview(problems: list[dict], suggest_reject: bool, confidence: float, raw_response: str)` where each problem is `{"kind": one of PROBLEMS, "confidence": float}`.
  - `parse_reject_response(raw) -> RejectReview`: object with exactly `{"problems", "suggest_reject", "confidence"}`; `problems` list of `{"kind","confidence"}` with `kind in PROBLEMS`, no duplicate kinds; `suggest_reject` a bool; `confidence` finite 0..1; else `RejectResponseError(message, raw)`.
  - `OmlxTagger.review_rejects(preview: Path, timeout=240) -> RejectReview`: single image, prompt lists the eight problem kinds, asks for JSON only, "Do not identify a person.", `max_tokens=300`.
  - `Library.start_reject_review_job(tagger, *, scope: str | list[int] = "keepers") -> Job` (kind `review_rejects`): same scope rules and worker loop as `start_tag_job`; per photo under `_mutation()`: `tags.data["problems"] = {"items": [{"kind","confidence"}], "kinds": [...], "suggest_reject": bool, "confidence": float, "reviewed_at": iso}` merged into the existing row (creating a row with only `problems` if the photo is untagged, `model` = tagger model); event `reject_suggested` `{model, role, problems, suggest_reject, confidence, raw_response}`; failure event `reject_review_failed` with the `tag_failed` shape; sidecar rewritten per item. Never calls `set_decision`. Manifest `{"kind":"review_rejects","scope","role","model"}` plus `summary {"reviewed","skipped","failed","suggested"}` and `failed_photo_ids`.
  - `Library.reject_suggestions() -> list[dict]`: photos whose `problems.suggest_reject` is true and `decision != "reject"`, each `{"photo_id", "name", "group_id", "problems": [{"kind","confidence"}], "confidence", "preview_url"}` ordered by `confidence` descending then id. (`preview_url` is built in web; library returns `preview_kind` and `fingerprint16` only through the existing `/api/groups` shape, so web composes it the same way `/api/photos/<id>` does.)
  - `Library.apply_reject_suggestions(photo_ids: list[int], decision: str) -> dict`: `decision in {"reject", "keep"}`; every id must currently be in `reject_suggestions()` else `ValueError("photo n is not a current reject suggestion")` and nothing is changed; `reject` calls `set_decision(id, "reject")`; `keep` sets `tags.data["problems"]["dismissed_at"]` so the row leaves the list without touching the decision; event `reject_suggestion_decided` `{"decision"}` per photo; returns `{"decided": [ids], "remaining": int}`.
  - Web: `POST /api/photos/review-rejects {"scope"}` → 202, 409 on busy, 400 on unloaded fast model (same gate as tag); `GET /api/photos/reject-suggestions` → `{"suggestions": [...], "count": n}`; `POST /api/photos/reject-suggestions/decide {"photo_ids": [ints], "decision": "reject"|"keep"}` → 200 `{"decided","remaining"}`, 400 if any id is not currently suggested (ids-not-count semantics, Review Focus).
  - CLI: `lumen review-rejects [--keepers|--all] [--json]` runs the job and prints status; exit 2 on failure.
  - UI: new tab `Review` between Export and Astro (`#review-tab`/`#review-panel`): `button#scan-rejects` "Scan keepers for problems" (gated like `#tag-keepers` on the fast role), `p#review-count` "N suggested rejects", `ul#reject-list` with one `li.reject-row[data-photo-id]` per suggestion: lazy `img` (empty alt), name, problems as text "motion blur 84%, eyes closed 61%", and two native buttons "Reject" (`aria-label="Reject <name>"`) and "Keep" (`aria-label="Keep <name>"`). Keys inside the panel when a row's button has focus: `X` rejects the row, `K` keeps it, `ArrowDown`/`ArrowUp` move focus between rows; handled by a keydown listener on `#reject-list` that ignores input/textarea/select targets. `button#reject-all-shown` opens `dialog#reject-all-confirm` whose text is exactly `Reject all N shown frames? This marks them rejected; you can restore any frame from the inspector.` with N the count at open time and a hidden list of the ids shown; its Confirm posts those ids. Each decision posts, removes the row in place, updates `state.rejectedCount`/`keeperCount` through the existing `decide` bookkeeping (call `refreshFrame`), and announces `"<name> rejected"` / `"<name> kept"`.

- [ ] **Step 1: Write failing tests.**

`tests/test_omlx.py`:

```python
    def test_reject_response_requires_known_problem_kinds_and_exact_fields(self):
        good = json.dumps({"problems": [{"kind": "motion_blur", "confidence": 0.84}], "suggest_reject": True, "confidence": 0.8})
        result = parse_reject_response(good)
        self.assertEqual(([{"kind": "motion_blur", "confidence": 0.84}], True), (result.problems, result.suggest_reject))
        for bad in ({"problems": [{"kind": "ugly", "confidence": 0.5}], "suggest_reject": False, "confidence": 0.5},
                    {"problems": [], "suggest_reject": "yes", "confidence": 0.5},
                    {"problems": [], "suggest_reject": False, "confidence": 0.5, "note": "x"},
                    {"problems": [{"kind": "misfire", "confidence": 0.5}, {"kind": "misfire", "confidence": 0.6}], "suggest_reject": True, "confidence": 0.5}):
            with self.subTest(bad=bad), self.assertRaises(RejectResponseError):
                parse_reject_response(json.dumps(bad))
```

`tests/test_library.py`:

```python
    def test_reject_review_job_stores_problems_and_never_changes_decisions(self):
        class FakeTagger:
            model, role = "fast-model", "fast"
            def review_rejects(self, preview):
                blurry = preview.stat().st_size % 2 == 0  # deterministic per generated preview
                return RejectReview([{"kind": "motion_blur", "confidence": 0.84}] if blurry else [], blurry, 0.8, '{"r":1}')

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(4):
                Image.new("RGB", (120 + index, 90), (index * 20, 0, 0)).save(root / f"p{index}.jpg")
            with Library(Settings(library_dir=root / "library")) as library:
                library.import_path(root)
                job = library.start_reject_review_job(FakeTagger(), scope="all")
                library.wait_for_job(job.id)
                photos = library.catalog.list_photos()
                self.assertTrue(all(photo.decision == "none" for photo in photos))
                suggestions = library.reject_suggestions()
                self.assertEqual(sorted(s["photo_id"] for s in suggestions),
                                 sorted(p.id for p in photos if library.catalog.get_tags(p.id)["data"]["problems"]["suggest_reject"]))
                first = photos[0].id
                event = [e for e in library.catalog.events_for(first) if e.kind == "reject_suggested"][-1]
                self.assertEqual(("fast", '{"r":1}'), (event.details["role"], event.details["raw_response"]))
                self.assertEqual("succeeded", library.job_status(job.id)["status"])

    def test_apply_reject_suggestions_uses_ids_not_counts_and_keep_dismisses(self):  # Review Focus
        class FakeTagger:
            model, role = "fast-model", "fast"
            def review_rejects(self, preview):
                return RejectReview([{"kind": "misfire", "confidence": 0.9}], True, 0.9, "{}")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(3):
                Image.new("RGB", (120, 90), (index, 0, 0)).save(root / f"p{index}.jpg")
            with Library(Settings(library_dir=root / "library")) as library:
                library.import_path(root)
                library.wait_for_job(library.start_reject_review_job(FakeTagger(), scope="all").id)
                a, b, c = [s["photo_id"] for s in library.reject_suggestions()]
                library.set_decision(c, "reject")  # decided elsewhere while the list was on screen
                with self.assertRaisesRegex(ValueError, f"photo {c} is not a current reject suggestion"):
                    library.apply_reject_suggestions([a, c], "reject")
                self.assertEqual("none", library.catalog.get_photo(a).decision)
                self.assertEqual({"decided": [a], "remaining": 1}, library.apply_reject_suggestions([a], "reject"))
                self.assertEqual("reject", library.catalog.get_photo(a).decision)
                self.assertEqual({"decided": [b], "remaining": 0}, library.apply_reject_suggestions([b], "keep"))
                self.assertEqual("none", library.catalog.get_photo(b).decision)
                self.assertIn("dismissed_at", library.catalog.get_tags(b)["data"]["problems"])
                with self.assertRaises(ValueError):
                    library.apply_reject_suggestions([b], "maybe")
```

`tests/test_web.py`:

```python
    def test_reject_suggestion_routes_gate_validate_and_decide_by_ids(self):
        class FakeTagger:
            def __init__(self, *args, **kwargs): self.model, self.role = "fast-model", kwargs.get("role", "review")
            def review_rejects(self, preview):
                from lumen.omlx import RejectReview
                return RejectReview([{"kind": "misfire", "confidence": 0.9}], True, 0.9, "{}")

        loaded = {"version": None, "default_model": None, "loaded_models": ["Qwen3-VL-8B-Instruct-4bit"]}
        for index in range(2):
            Image.new("RGB", (120, 90), (index, 0, 0)).save(self.root / f"p{index}.jpg")
        with patch("lumen.library.get_server_status", return_value=loaded), patch("lumen.library.OmlxTagger", FakeTagger):
            self.wait_job(self.post_json("/api/import", {"path": str(self.root)})[1]["job_id"])
            code, job = self.post_json("/api/photos/review-rejects", {"scope": "keepers"})
            self.assertEqual(202, code)
            self.assertEqual("succeeded", self.wait_job(job["job_id"])["status"])
            code, body = self.get_json("/api/photos/reject-suggestions")
            self.assertEqual(2, body["count"])
            self.assertNotIn("raw_response", json.dumps(body))
            self.assertTrue(all(s["preview_url"].startswith("/preview/") for s in body["suggestions"]))
            ids = [s["photo_id"] for s in body["suggestions"]]
            self.assertEqual(403, self.post_json("/api/photos/reject-suggestions/decide", {"photo_ids": ids, "decision": "reject"}, headers={"Origin": "https://attacker.example"})[0])
            for payload in ({"photo_ids": ids}, {"photo_ids": [], "decision": "reject"}, {"photo_ids": ids, "decision": "reject", "count": 2}, {"photo_ids": [ids[0], 99], "decision": "reject"}):
                with self.subTest(payload=payload):
                    self.assertEqual(400, self.post_json("/api/photos/reject-suggestions/decide", payload)[0])
            self.assertEqual("none", self.get_json(f"/api/photos/{ids[0]}")[1]["photo"]["decision"])
            code, body = self.post_json("/api/photos/reject-suggestions/decide", {"photo_ids": ids, "decision": "reject"})
            self.assertEqual((200, sorted(ids), 0), (code, sorted(body["decided"]), body["remaining"]))
        with patch("lumen.library.get_server_status", return_value={"version": None, "default_model": None, "loaded_models": []}):
            self.assertEqual(400, self.post_json("/api/photos/review-rejects", {"scope": "keepers"})[0])
            self.assertFalse(any(j["kind"] == "review_rejects" and j["status"] in ("queued", "running") for j in self.get_json("/api/jobs")[1]["jobs"]))

    def test_review_panel_has_native_rows_keys_and_counted_confirmation(self):
        html = (Path(__file__).parent.parent / "src/lumen/static/index.html").read_text()
        for needle in ('id="review-tab"', 'id="review-panel"', 'id="scan-rejects"', 'id="reject-list"', 'id="reject-all-shown"', 'id="reject-all-confirm"'):
            self.assertIn(needle, html)
        js = (Path(__file__).parent.parent / "src/lumen/static/app.js").read_text()
        self.assertIn("Reject all ${", js)

    def test_javascript_review_panel_decides_rows_by_button_and_key_and_confirms_count(self):
        self.run_javascript_cleanup_regression("review-rejects")
```

Node scenario `review-rejects`: `GET /api/photos/reject-suggestions` returns two rows; rendering alone posts nothing (assert the recorded POSTs are empty after render); pressing `X` with focus on row 1's Reject button posts `decide {photo_ids:[1], decision:'reject'}` and removes the row; clicking row 2's Keep posts `keep`; with both rows back, `#reject-all-shown` opens the dialog whose text contains `Reject all 2 shown frames?`, and confirming posts `{photo_ids:[1,2], decision:'reject'}`; `load()` is never called; announcer messages recorded. Update the harness tab list to include `review` so the tablist test keeps passing (`test_finishing_tabs…` now expects six tabs).

`tests/test_cli.py`:

```python
    def test_review_rejects_command_runs_job_and_reports_summary(self):
        class FakeTagger:
            def __init__(self, *args, **kwargs): self.model, self.role = "fast-model", kwargs.get("role", "review")
            def review_rejects(self, preview):
                return RejectReview([], False, 0.7, "{}")

        live = {"version": None, "default_model": None, "loaded_models": ["Qwen3-VL-8B-Instruct-4bit"]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new("RGB", (120, 90)).save(root / "p.jpg")
            main(["--library", str(root / "library"), "import", str(root)])
            output = io.StringIO()
            with patch("lumen.library.OmlxTagger", FakeTagger), patch("lumen.library.get_server_status", return_value=live), redirect_stdout(output):
                code = main(["--library", str(root / "library"), "review-rejects", "--json"])
        self.assertEqual(0, code)
        self.assertEqual({"reviewed": 1, "skipped": 0, "failed": 0, "suggested": 0}, json.loads(output.getvalue())["result"]["summary"])
```

- [ ] **Step 2: Run the focused modules; expect `ImportError: RejectReview`, `AttributeError: start_reject_review_job`, 404s, and missing ids.**

Run: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_omlx tests.test_library tests.test_web tests.test_cli -v`

- [ ] **Step 3: Implement the parser and tagger method.**

```python
PROBLEMS = ("eyes_closed", "motion_blur", "out_of_focus", "subject_cut_off", "obstructed", "misfire",
            "severely_under_exposed", "severely_over_exposed")


class RejectResponseError(OmlxError):
    def __init__(self, message, raw_response=None):
        super().__init__(message); self.raw_response = raw_response


@dataclass(frozen=True, slots=True)
class RejectReview:
    problems: list[dict]
    suggest_reject: bool
    confidence: float
    raw_response: str


def _strip_fence(text: str, error_type) -> str:  # shared by every parser from now on
    ...


def _confidence(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1


def parse_reject_response(raw: str) -> RejectReview:
    payload = _json_object(raw, RejectResponseError)  # fence strip + json.loads + dict check
    if set(payload) != {"problems", "suggest_reject", "confidence"}:
        raise RejectResponseError("oMLX reject response must contain only problems, suggest_reject, and confidence", raw)
    problems = payload["problems"]
    if (not isinstance(problems, list)
            or not all(isinstance(p, dict) and set(p) == {"kind", "confidence"} and p["kind"] in PROBLEMS and _confidence(p["confidence"]) for p in problems)
            or len({p["kind"] for p in problems}) != len(problems)):
        raise RejectResponseError("oMLX problems must be unique known kinds with confidence", raw)
    if type(payload["suggest_reject"]) is not bool or not _confidence(payload["confidence"]):
        raise RejectResponseError("oMLX suggest_reject must be boolean and confidence 0 to 1", raw)
    return RejectReview([{"kind": p["kind"], "confidence": float(p["confidence"])} for p in problems],
                        payload["suggest_reject"], float(payload["confidence"]), raw)
```

Refactor the existing two parsers onto `_json_object` only if the diff stays mechanical; otherwise leave them. `OmlxTagger.review_rejects` sends one text part (`"Look for technical failures only: " + ", ".join(PROBLEMS) + ". Return JSON only with problems (array of {kind, confidence}), suggest_reject (boolean), confidence (0 to 1). Do not identify a person."`) and one image part, `max_tokens=300`.

- [ ] **Step 4: Implement library, web, CLI, and the Review tab** per Interfaces. The job worker is a copy of the `start_tag_job` loop with `review_rejects` in place of `tag_photo`; extract the shared loop into `Library._run_photo_job(kind, tagger, scope, retag, per_photo)` so both jobs use one worker (per-photo callable returns `"tagged"|"skipped"|"failed"`). `reject_suggestions` reads `tags` rows with `json_extract(data_json, '$.problems.suggest_reject') = 1 AND json_extract(data_json, '$.problems.dismissed_at') IS NULL` joined to `photos WHERE decision != 'reject'`. The Review tab lives in `index.html` after the Export panel; `renderRejectList(suggestions)` builds rows; the key handler is scoped to `#reject-list` and checks `event.target.closest('input, textarea, select')` first.

- [ ] **Step 5: Run focused modules, the full suite, and `node --check`; expect PASS.**

- [ ] **Step 6: Independent review** of Task 2 against spec feature 3 and the acceptance line "applies a rejection only on button or key press, and 'Reject all shown' states the count first", the advisory rule, the job gate (no job row on refusal), and the Review Focus line on ids-not-count; fix findings.

- [ ] **Step 7: Commit.**

```bash
git add src/lumen/omlx.py src/lumen/library.py src/lumen/web.py src/lumen/cli.py src/lumen/static tests/test_omlx.py tests/test_library.py tests/test_web.py tests/test_cli.py
git commit -m "feat: reject suggestions with a confirm-list review panel"
```

### Task 3: Burst critique

**Files:**
- Modify: `src/lumen/omlx.py` (`BurstCritique`, `parse_burst_response`, `OmlxTagger.critique_burst`)
- Modify: `src/lumen/library.py` (`critique_burst`)
- Modify: `src/lumen/web.py` (`POST /api/groups/<id>/critique`, `GET /api/groups/<id>/critique`)
- Modify: `src/lumen/cli.py` (`critique`)
- Modify: `src/lumen/static/index.html`, `app.js` (inspector critique block)
- Test: `tests/test_omlx.py`, `tests/test_library.py`, `tests/test_web.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `Library.tagger_for("review")`, `Catalog.list_groups`, `Library.set_pick`, `Catalog.append_event`, `review_composition` failure pattern, inspector `renderInspector`.
- Produces:
  - `BurstCritique(frame_id: str, rationale: str, confidence: float, raw_response: str)`; `parse_burst_response(raw, frame_ids: set[str]) -> BurstCritique`: exactly `{"frame_id","rationale","confidence"}`, `frame_id in frame_ids`, rationale 1..300 characters after strip, confidence finite 0..1; else `BurstResponseError(message, raw)`.
  - `OmlxTagger.critique_burst(frames: list[tuple[str, Path]], timeout=240) -> BurstCritique`: 2..8 frames, unique non-empty ids (else `ValueError`); content = one prompt text part ("Compare these frames from one burst for sharpness, expression or pose, timing, framing, and distracting elements. Select exactly one frame_id from: <ids>. Return JSON only with frame_id, rationale (at most 300 characters), confidence (0 to 1). Do not identify a person."), then `Frame <id>` text + image per frame; `max_tokens=400`.
  - `Library.critique_burst(group_id: int, tagger) -> dict` (`@_coordinated`): group must exist with ≥2 non-rejected frames (else `ValueError("burst critique needs at least two frames that are not rejected")`); frames = the group's ranked order from `list_groups`, non-rejected first, capped at 8; ids are `str(photo.id)`; after the call, re-read the group and require the returned id to still be a member (else treat as failure with `"burst changed during review"`, Review Focus). Success: event `burst_reviewed` on the chosen photo with `{model, role, group_id, frame_id: int, candidate_ids: [ints], rationale, confidence, raw_response}`, sidecars of the group rewritten, returns `{"group_id", "photo_id", "name", "rationale", "confidence", "candidate_ids"}`. Failure: event `burst_review_failed` on `photo_id=None` with `{model, role, group_id, error, raw_response, candidate_ids}` and re-raise. The pick is never touched.
  - `Library.latest_critique(group_id) -> dict | None`: the newest `burst_reviewed` event whose `group_id` matches and whose `frame_id` is still in the group, in the same public shape (no `raw_response`).
  - Web: `POST /api/groups/<id>/critique {}` → 200 public shape, 400 `"review model <name> is not loaded in oMLX"` or the public error, 404 unknown group; `GET /api/groups/<id>/critique` → `{"critique": <shape>|null}`. `/api/groups` photos payload is unchanged.
  - CLI: `lumen critique <group-id> [--json]` → prints the shape, exit 2 with `{"error"}` on failure.
  - UI: inspector gets `div#burst-critique` under the ranking paragraph when the selected frame's group has ≥2 non-rejected frames: `button#ask-burst-ai` "Ask local AI which frame is best" (gated on `omlx_roles.review.loaded` with `#burst-ai-availability` text "Review model <name> is not loaded in oMLX." when not), `p#burst-advice` "Local AI suggests frame N (<name>): <rationale> (confidence 72%)" where N is the frame's 1-based position in the burst, and `button#use-burst-pick` "Use this pick" that calls `decide(photo, 'pick', null)` for the suggested photo (existing manual pick path). Loading a group fetches `GET /api/groups/<id>/critique` once per group selection and renders the stored advice; the pick is never changed by render.

- [ ] **Step 1: Write failing tests.**

`tests/test_omlx.py`:

```python
    def test_burst_response_requires_supplied_frame_and_bounded_rationale(self):
        good = json.dumps({"frame_id": "7", "rationale": "Sharpest eye, wing fully extended.", "confidence": 0.72})
        self.assertEqual("7", parse_burst_response(good, {"7", "8"}).frame_id)
        for bad in ({"frame_id": "9", "rationale": "x", "confidence": 0.5}, {"frame_id": "7", "rationale": "x" * 301, "confidence": 0.5},
                    {"frame_id": "7", "rationale": "x", "confidence": 0.5, "second": "8"}, {"frame_id": 7, "rationale": "x", "confidence": 0.5}):
            with self.subTest(bad=bad), self.assertRaises(BurstResponseError):
                parse_burst_response(json.dumps(bad), {"7", "8"})

    def test_critique_burst_sends_labeled_frames_and_validates_count(self):
        tagger = OmlxTagger("http://127.0.0.1:8000", "review-model", api_key="k", role="review")
        captured = {}
        with tempfile.TemporaryDirectory() as directory:
            frames = []
            for index in range(3):
                path = Path(directory) / f"{index}.jpg"; Image.new("RGB", (8, 8)).save(path); frames.append((str(index + 1), path))
            with patch.object(tagger, "_complete", lambda content, timeout, **kw: (captured.update(content=content, kw=kw), json.dumps({"frame_id": "2", "rationale": "ok", "confidence": 0.6}))[1]):
                result = tagger.critique_burst(frames)
            with self.assertRaises(ValueError):
                tagger.critique_burst(frames[:1])
            with self.assertRaises(ValueError):
                tagger.critique_burst(frames * 3)
        self.assertEqual("2", result.frame_id)
        texts = [part["text"] for part in captured["content"] if part["type"] == "text"]
        self.assertIn("Do not identify a person.", texts[0])
        self.assertEqual(["Frame 1", "Frame 2", "Frame 3"], texts[1:])
        self.assertEqual(3, sum(part["type"] == "image_url" for part in captured["content"]))
        self.assertEqual(400, captured["kw"]["max_tokens"])
```

`tests/test_library.py`:

```python
    def _burst(self, root, count=3):
        for index in range(count):
            image = Image.new("RGB", (120, 90), (index * 30, 0, 0))
            exif = Image.Exif(); exif[36867] = f"2026:10:22 20:00:0{index}"
            image.save(root / f"b{index}.jpg", exif=exif)

    def test_burst_critique_records_suggestion_without_changing_the_pick(self):
        class FakeTagger:
            model, role = "review-model", "review"
            def critique_burst(self, frames):
                self.frames = list(frames)
                return BurstCritique(frames[-1][0], "Best timing.", 0.72, '{"r":1}')

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self._burst(root)
            tagger = FakeTagger()
            with Library(Settings(library_dir=root / "library")) as library:
                library.import_path(root)
                group = library.list_groups()[0]
                self.assertEqual(3, len(group["photos"]))
                pick_before = group["automatic_pick_id"]
                result = library.critique_burst(group["id"], tagger)
                group_after = library.list_groups()[0]
                self.assertEqual(pick_before, group_after["automatic_pick_id"])
                self.assertEqual(int(tagger.frames[-1][0]), result["photo_id"])
                self.assertEqual([str(p["id"]) for p in group["photos"]], [fid for fid, _ in tagger.frames])
                event = [e for e in library.catalog.events_for(result["photo_id"]) if e.kind == "burst_reviewed"][-1]
                self.assertEqual(("review", '{"r":1}', group["id"]), (event.details["role"], event.details["raw_response"], event.details["group_id"]))
                self.assertEqual(result | {"raw_response": None}, (library.latest_critique(group["id"]) or {}) | {"raw_response": None})
                self.assertNotIn("raw_response", result)
                library.set_decision(group["photos"][1]["id"], "reject")
                library.set_decision(group["photos"][2]["id"], "reject")
                with self.assertRaisesRegex(ValueError, "at least two frames"):
                    library.critique_burst(group["id"], tagger)

    def test_burst_critique_caps_at_eight_frames_ranked_and_fails_cleanly_when_burst_changes(self):  # Review Focus
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self._burst(root, 10)
            with Library(Settings(library_dir=root / "library")) as library:
                library.import_path(root)
                group = library.list_groups()[0]
                last = group["photos"][-1]["id"]  # ranked last, so outside the 8-frame cap

                class ChangingTagger:
                    model, role = "review-model", "review"
                    def critique_burst(self, frames):
                        self.frames = list(frames)
                        return BurstCritique(str(last), "ok", 0.5, '{"r":2}')  # a member of the burst but not of the supplied set

                tagger = ChangingTagger()
                with self.assertRaises((ValueError, BurstResponseError)):
                    library.critique_burst(group["id"], tagger)
                self.assertEqual([str(p["id"]) for p in group["photos"][:8]], [fid for fid, _ in tagger.frames])
                failed = [e for e in library.catalog.events_for(None) if e.kind == "burst_review_failed"][-1]
                self.assertEqual(([p["id"] for p in group["photos"][:8]], '{"r":2}'), (failed.details["candidate_ids"], failed.details["raw_response"]))

                class RemovingTagger:
                    model, role = "review-model", "review"
                    def critique_burst(self, frames):
                        chosen = int(frames[0][0])
                        with Library(Settings(library_dir=root / "library")) as other:  # the photographer rejects that frame mid-call
                            other.set_decision(chosen, "reject")
                        return BurstCritique(frames[0][0], "ok", 0.5, "{}")

                with self.assertRaisesRegex(ValueError, "burst changed during review"):
                    library.critique_burst(group["id"], RemovingTagger())
                self.assertEqual("burst_review_failed", [e for e in library.catalog.events_for(None)][-1].kind)
                self.assertEqual(group["automatic_pick_id"], library.list_groups()[0]["automatic_pick_id"])
```

(`critique_burst` re-reads the group after the call and requires the returned id to be a non-rejected member; a rejected or removed frame fails with `"burst changed during review"`, a non-supplied id fails in the parser. Both paths record `burst_review_failed` and leave the pick alone.)

`tests/test_web.py`:

```python
    def test_critique_routes_gate_on_review_model_and_never_change_the_pick(self):
        class FakeTagger:
            def __init__(self, *args, **kwargs): self.model, self.role = "review-model", kwargs.get("role", "review")
            def critique_burst(self, frames):
                from lumen.omlx import BurstCritique
                return BurstCritique(frames[-1][0], "Best timing.", 0.72, "{}")

        for index in range(3):
            image = Image.new("RGB", (120, 90), (index * 30, 0, 0)); exif = Image.Exif(); exif[36867] = f"2026:10:22 20:00:0{index}"
            image.save(self.root / f"b{index}.jpg", exif=exif)
        fast_only = {"version": None, "default_model": None, "loaded_models": ["Qwen3-VL-8B-Instruct-4bit"]}
        both = {"version": None, "default_model": None, "loaded_models": ["Qwen3-VL-8B-Instruct-4bit", "Qwen3.8-27B-4bit"]}
        with patch("lumen.library.get_server_status", return_value=fast_only), patch("lumen.library.OmlxTagger", FakeTagger):
            self.wait_job(self.post_json("/api/import", {"path": str(self.root)})[1]["job_id"])
            group = self.get_json("/api/groups")[1]["groups"][0]
            self.assertEqual((400, {"error": "review model Qwen3.8-27B-4bit is not loaded in oMLX"}), self.post_json(f"/api/groups/{group['id']}/critique", {}))
            self.assertEqual({"critique": None}, self.get_json(f"/api/groups/{group['id']}/critique")[1])
        with patch("lumen.library.get_server_status", return_value=both), patch("lumen.library.OmlxTagger", FakeTagger):
            self.assertEqual(403, self.post_json(f"/api/groups/{group['id']}/critique", {}, headers={"Origin": "https://attacker.example"})[0])
            self.assertEqual(400, self.post_json(f"/api/groups/{group['id']}/critique", {"pick": True})[0])
            self.assertEqual(404, self.post_json("/api/groups/999/critique", {})[0])
            code, body = self.post_json(f"/api/groups/{group['id']}/critique", {})
            self.assertEqual(200, code)
            self.assertEqual({"group_id", "photo_id", "name", "rationale", "confidence", "candidate_ids"}, set(body))
            self.assertEqual(group["automatic_pick_id"], self.get_json("/api/groups")[1]["groups"][0]["automatic_pick_id"])
            self.assertEqual(body, self.get_json(f"/api/groups/{group['id']}/critique")[1]["critique"])

    def test_javascript_burst_critique_shows_advice_and_only_use_this_pick_changes_the_pick(self):
        self.run_javascript_cleanup_regression("critique")
```

Node scenario `critique`: group of three; `GET /api/groups/1/critique` returns `{critique: {photo_id: 3, name: 'b2.jpg', rationale: 'Best timing.', confidence: 0.72, candidate_ids: [1,2,3]}}`; after `inspect(photo1)`, `#burst-advice` text is `Local AI suggests frame 3 (b2.jpg): Best timing. (confidence 72%)`; no POST to `/api/photos/*/pick` has happened; clicking `#use-burst-pick` posts `/api/photos/3/pick`; clicking `#ask-burst-ai` posts `/api/groups/1/critique` and re-renders; with `review.loaded: false`, `#ask-burst-ai` is disabled and `#burst-ai-availability` names the model.

`tests/test_cli.py`:

```python
    def test_critique_command_prints_suggestion_and_exits_two_on_failure(self):
        class FakeTagger:
            def __init__(self, *args, **kwargs): self.model, self.role = "review-model", kwargs.get("role", "review")
            def critique_burst(self, frames):
                return BurstCritique(frames[0][0], "Sharpest.", 0.8, "{}")

        live = {"version": None, "default_model": None, "loaded_models": ["Qwen3.8-27B-4bit"]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(2):
                image = Image.new("RGB", (120, 90), (index * 30, 0, 0)); exif = Image.Exif(); exif[36867] = f"2026:10:22 20:00:0{index}"
                image.save(root / f"b{index}.jpg", exif=exif)
            main(["--library", str(root / "library"), "import", str(root)])
            output = io.StringIO()
            with patch("lumen.library.OmlxTagger", FakeTagger), patch("lumen.library.get_server_status", return_value=live), redirect_stdout(output):
                code = main(["--library", str(root / "library"), "critique", "1", "--json"])
            self.assertEqual(0, code)
            self.assertEqual("Sharpest.", json.loads(output.getvalue())["rationale"])
            output = io.StringIO()
            with patch("lumen.library.get_server_status", return_value={"version": None, "default_model": None, "loaded_models": []}), redirect_stdout(output):
                code = main(["--library", str(root / "library"), "critique", "1", "--json"])
            self.assertEqual((2, "review model Qwen3.8-27B-4bit is not loaded in oMLX"), (code, json.loads(output.getvalue())["error"]))
```

- [ ] **Step 2: Run the focused modules; expect import errors, missing methods, 404s.**

- [ ] **Step 3: Implement `omlx.py` additions** (`BurstResponseError`, `BurstCritique`, `parse_burst_response` using `_json_object`, `critique_burst` with `2 <= len(frames) <= 8`, unique ids, labelled parts, `max_tokens=400`).

- [ ] **Step 4: Implement `Library.critique_burst` and `latest_critique`, routes, CLI, and the inspector block** per Interfaces. The group lookup is `next((g for g in self.catalog.list_groups() if g["id"] == group_id), None)`; non-rejected frames first in ranked order, then the first 8. After the tagger returns, re-read the group; if `int(result.frame_id)` is not in the current membership, raise `ValueError("burst changed during review")` inside the try so the failure event is recorded. Web answers 404 only when the group id is unknown before the call. In `app.js`, `state.critique = {groupId, data}` is fetched in `inspect` when `state.selectedGroupId` changes; `renderInspector` builds `#burst-critique`; `#use-burst-pick` calls `decide(suggestedPhoto, 'pick', null, '.frame[data-id="<id>"]')`.

- [ ] **Step 5: Run focused modules, full suite, `node --check`; expect PASS.**

- [ ] **Step 6: Independent review** of Task 3 against spec feature 2 and the acceptance line "returns a frame from the supplied set or fails cleanly; the pick never changes without 'Use this pick'", the 8-frame cap by rank, and the Review Focus line on a changed burst; fix findings.

- [ ] **Step 7: Commit.**

```bash
git add src/lumen/omlx.py src/lumen/library.py src/lumen/web.py src/lumen/cli.py src/lumen/static tests/test_omlx.py tests/test_library.py tests/test_web.py tests/test_cli.py
git commit -m "feat: advisory burst critique with an explicit use-this-pick action"
```

### Task 4: Astro triage

**Files:**
- Modify: `src/lumen/omlx.py` (`SKY_CLASSES`, `SKY_ISSUES`, `AstroTriage`, `parse_astro_response`, `OmlxTagger.triage_astro`)
- Modify: `src/lumen/library.py` (triage inside the tag job for long exposures, `start_triage_job`, `astro_triage(group_id)`)
- Modify: `src/lumen/web.py` (`POST /api/groups/<id>/triage`, `GET /api/groups/<id>/triage`; `/api/groups` carries `astro_cue`)
- Modify: `src/lumen/cli.py` (`triage`)
- Modify: `src/lumen/static/index.html`, `app.js`, `styles.css` (Astro tab per-light triage, "Exclude flagged lights", burst header cue)
- Test: `tests/test_omlx.py`, `tests/test_library.py`, `tests/test_web.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `Library.tagger_for("fast")`, `Photo.exposure_seconds`, `PhotoMetrics.astro` (`star_count`, `trailing_warning`), `renderAstroLights`, `astroLights()`, Task 2's `_run_photo_job`, Task 1 `tags.data` merge.
- Produces:
  - `SKY_CLASSES = ("night_sky", "aurora", "milky_way", "twilight", "cloud", "not_sky")`; `SKY_ISSUES = ("haze", "light_pollution", "trailing", "cloud_band")`; `AstroTriage(sky: str, confidence: float, issues: list[str], raw_response: str)`; `parse_astro_response(raw)`: exactly `{"sky","confidence","issues"}`, `sky in SKY_CLASSES`, confidence finite 0..1, issues a list of unique `SKY_ISSUES`; else `AstroResponseError`.
  - `OmlxTagger.triage_astro(preview: Path, timeout=240) -> AstroTriage`: one image, prompt lists the classes and issues, JSON only, "Do not identify a person.", `max_tokens=200`.
  - `Library._triage_one(photo, tagger, *, job_id)`: calls `triage_astro`, merges `tags.data["sky"] = {"class", "confidence"}` and `tags.data["issues"] = [...]`, event `astro_triaged` `{model, role, sky, confidence, issues, raw_response}`, failure `astro_triage_failed`, sidecar rewritten. Inside the tag job (`tag_photo`'s per-item callable), after a successful tag, any photo with `exposure_seconds is not None and exposure_seconds >= 1.0` is also triaged; a triage failure counts in a separate `summary["triage_failed"]` and never fails the tag.
  - `Library.start_triage_job(tagger, *, group_id: int) -> Job` (kind `triage`): every photo of the group regardless of exposure, through `_run_photo_job`.
  - `Library.astro_triage(group_id) -> dict`: `{"group_id", "lights": [{"photo_id", "name", "sky", "confidence", "issues", "star_count", "trailing_warning", "flagged": bool}], "astro_cue": bool}` where `flagged = sky.class in {"cloud","not_sky"} or "trailing" in issues or "cloud_band" in issues or trailing_warning` (metrics-only flags still work when no triage exists; `sky` is `None` then) and `astro_cue` is true when more than half the group's frames have `sky.class in {"night_sky","aurora","milky_way"}`.
  - `/api/groups` each group gains `"astro_cue": bool` computed from stored tags (no model call); `GET /api/groups/<id>/triage` → the shape; `POST /api/groups/<id>/triage {}` → 202 job (fast gate, 409 busy).
  - CLI: `lumen triage <group-id> [--json]` runs the job, waits, prints `astro_triage(group_id)`.
  - UI: burst header gets a `span.astro-cue` with text `Looks like astro` when `group.astro_cue` (text, not colour only); it never switches tabs. Astro tab: each light label gains a `span.triage` with text like `night sky 91% · trailing` or `not triaged`; `button#triage-astro` "Triage lights with local AI" (fast gate, job); `button#exclude-flagged` "Exclude flagged lights (N)" opens `dialog#exclude-confirm` whose text is `Uncheck N flagged lights? Flagged means cloud, not sky, trailing, or a cloud band. Nothing is stacked until you prepare and run.`; Confirm unchecks exactly those checkboxes (client state only, feeding the existing `excluded_photo_ids` payload) and announces `N lights excluded`. No checkbox changes without the confirm.

- [ ] **Step 1: Write failing tests.**

`tests/test_omlx.py`:

```python
    def test_astro_response_requires_known_sky_class_and_issues(self):
        good = json.dumps({"sky": "milky_way", "confidence": 0.91, "issues": ["haze"]})
        self.assertEqual(("milky_way", ["haze"]), (parse_astro_response(good).sky, parse_astro_response(good).issues))
        for bad in ({"sky": "day", "confidence": 0.5, "issues": []}, {"sky": "cloud", "confidence": 0.5, "issues": ["rain"]},
                    {"sky": "cloud", "confidence": 0.5, "issues": ["haze", "haze"]}, {"sky": "cloud", "confidence": 0.5}):
            with self.subTest(bad=bad), self.assertRaises(AstroResponseError):
                parse_astro_response(json.dumps(bad))
```

`tests/test_library.py`:

```python
    def _lights(self, root, count=4, exposure="15"):
        for index in range(count):
            image = Image.new("RGB", (120, 90), (0, 0, index * 20)); exif = Image.Exif()
            exif[36867] = f"2026:10:22 22:0{index}:00"; exif[33434] = (int(exposure), 1)
            image.save(root / f"l{index}.jpg", exif=exif)

    def test_tag_job_triages_long_exposures_and_triage_job_covers_a_group(self):
        class FakeTagger:
            model, role = "fast-model", "fast"
            triaged = []
            def tag(self, preview, *, vocabulary=None):
                return TagResult("stars", [], "", [], 0.9, "{}", species=[], conditions=[])
            def triage_astro(self, preview):
                FakeTagger.triaged.append(preview.name)
                cloudy = len(FakeTagger.triaged) == 2
                return AstroTriage("cloud" if cloudy else "night_sky", 0.9, ["cloud_band"] if cloudy else [], '{"t":1}')

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self._lights(root)
            Image.new("RGB", (120, 90)).save(root / "day.jpg")  # no exposure → not triaged
            with Library(Settings(library_dir=root / "library")) as library:
                library.import_path(root)
                self.assertTrue(all(p.exposure_seconds == 15.0 for p in library.catalog.list_photos() if p.path.endswith(".jpg") and "day" not in p.path))
                library.wait_for_job(library.start_tag_job(FakeTagger(), scope="all").id)
                self.assertEqual(4, len(FakeTagger.triaged))
                group = next(g for g in library.list_groups() if len(g["photos"]) == 4)
                triage = library.astro_triage(group["id"])
                self.assertTrue(triage["astro_cue"])
                self.assertEqual(1, sum(light["flagged"] for light in triage["lights"]))
                flagged = next(light for light in triage["lights"] if light["flagged"])
                self.assertEqual(("cloud", ["cloud_band"]), (flagged["sky"]["class"], flagged["issues"]))
                event = [e for e in library.catalog.events_for(flagged["photo_id"]) if e.kind == "astro_triaged"][-1]
                self.assertEqual('{"t":1}', event.details["raw_response"])
                self.assertNotIn("raw_response", json.dumps(triage))
                FakeTagger.triaged.clear()
                library.wait_for_job(library.start_triage_job(FakeTagger(), group_id=group["id"]).id)
                self.assertEqual(4, len(FakeTagger.triaged))
                self.assertEqual(sorted(l["photo_id"] for l in triage["lights"]), sorted(p["id"] for p in group["photos"]))

    def test_astro_triage_flags_from_metrics_without_a_model_and_cue_needs_a_majority(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self._lights(root, 3)
            with Library(Settings(library_dir=root / "library")) as library:
                library.import_path(root)
                group = library.list_groups()[0]
                photo = group["photos"][0]
                metrics = library.catalog.get_metrics(photo["id"])
                library.catalog.save_metrics(photo["id"], replace(metrics, astro=metrics.astro | {"trailing_warning": True}), photo["total_score"])
                triage = library.astro_triage(group["id"])
                self.assertFalse(triage["astro_cue"])
                self.assertEqual([photo["id"]], [l["photo_id"] for l in triage["lights"] if l["flagged"]])
                self.assertIsNone(triage["lights"][0]["sky"])
```

`tests/test_web.py`:

```python
    def test_triage_routes_and_groups_cue_never_switch_mode_or_change_exclusions(self):
        class FakeTagger:
            def __init__(self, *args, **kwargs): self.model, self.role = "fast-model", kwargs.get("role", "review")
            def triage_astro(self, preview):
                from lumen.omlx import AstroTriage
                return AstroTriage("night_sky", 0.9, [], "{}")

        for index in range(3):
            image = Image.new("RGB", (120, 90), (0, 0, index * 20)); exif = Image.Exif(); exif[36867] = f"2026:10:22 22:0{index}:00"; exif[33434] = (15, 1)
            image.save(self.root / f"l{index}.jpg", exif=exif)
        loaded = {"version": None, "default_model": None, "loaded_models": ["Qwen3-VL-8B-Instruct-4bit"]}
        with patch("lumen.library.get_server_status", return_value=loaded), patch("lumen.library.OmlxTagger", FakeTagger):
            self.wait_job(self.post_json("/api/import", {"path": str(self.root)})[1]["job_id"])
            group = self.get_json("/api/groups")[1]["groups"][0]
            self.assertFalse(group["astro_cue"])
            self.assertEqual(403, self.post_json(f"/api/groups/{group['id']}/triage", {}, headers={"Origin": "https://attacker.example"})[0])
            self.assertEqual(400, self.post_json(f"/api/groups/{group['id']}/triage", {"all": True})[0])
            code, job = self.post_json(f"/api/groups/{group['id']}/triage", {})
            self.assertEqual(202, code)
            self.assertEqual("succeeded", self.wait_job(job["job_id"])["status"])
            self.assertTrue(self.get_json("/api/groups")[1]["groups"][0]["astro_cue"])
            code, triage = self.get_json(f"/api/groups/{group['id']}/triage")
            self.assertEqual(3, len(triage["lights"]))
            self.assertNotIn("raw_response", json.dumps(triage))

    def test_astro_panel_has_triage_text_and_counted_exclude_confirmation(self):
        html = (Path(__file__).parent.parent / "src/lumen/static/index.html").read_text()
        for needle in ('id="triage-astro"', 'id="exclude-flagged"', 'id="exclude-confirm"'):
            self.assertIn(needle, html)

    def test_javascript_astro_triage_excludes_only_after_confirmation_and_cue_is_text(self):
        self.run_javascript_cleanup_regression("astro-triage")
```

Node scenario `astro-triage`: groups payload has `astro_cue: true` → burst header contains a child with text `Looks like astro` and the selected tab is unchanged; `GET /api/groups/1/triage` returns lights with photo 2 flagged; `#exclude-flagged` text `Exclude flagged lights (1)`; all three light checkboxes stay checked until the dialog's confirm is clicked, after which exactly photo 2's checkbox is unchecked and `astroLights().excluded` equals `[2]`; cancel leaves all checked.

`tests/test_cli.py`:

```python
    def test_triage_command_runs_job_and_prints_lights(self):
        class FakeTagger:
            def __init__(self, *args, **kwargs): self.model, self.role = "fast-model", kwargs.get("role", "review")
            def triage_astro(self, preview):
                return AstroTriage("aurora", 0.8, [], "{}")

        live = {"version": None, "default_model": None, "loaded_models": ["Qwen3-VL-8B-Instruct-4bit"]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(2):
                image = Image.new("RGB", (120, 90)); exif = Image.Exif(); exif[36867] = f"2026:10:22 22:0{index}:00"; exif[33434] = (15, 1)
                image.save(root / f"l{index}.jpg", exif=exif)
            main(["--library", str(root / "library"), "import", str(root)])
            output = io.StringIO()
            with patch("lumen.library.OmlxTagger", FakeTagger), patch("lumen.library.get_server_status", return_value=live), redirect_stdout(output):
                code = main(["--library", str(root / "library"), "triage", "1", "--json"])
        self.assertEqual(0, code)
        payload = json.loads(output.getvalue())
        self.assertEqual((2, True), (len(payload["lights"]), payload["astro_cue"]))
```

- [ ] **Step 2: Run the focused modules; expect import errors, missing methods, missing `astro_cue`, 404s.**

- [ ] **Step 3: Implement `omlx.py`** (`AstroResponseError`, `AstroTriage`, `parse_astro_response`, `triage_astro` with `max_tokens=200`).

- [ ] **Step 4: Implement library, web, CLI, UI** per Interfaces. `astro_triage` reads tags and metrics for the group's photos; `astro_cue` for `/api/groups` is computed in `web.py` from `library.catalog.get_tags` per group photo (one query per group is acceptable at this scale; `# ponytail: per-photo tag reads, batch with json_extract if the sheet slows`). `_run_photo_job` gets an optional `after_item` hook used by the tag job to triage long exposures. The Astro tab reads triage on group selection (`GET /api/groups/<id>/triage`) and after the triage job ends.

- [ ] **Step 5: Run focused modules, full suite, `node --check`; expect PASS.**

- [ ] **Step 6: Independent review** of Task 4 against spec feature 4 and the acceptance line "'Exclude flagged lights' changes the job's selected set only after confirmation", the cue-never-switches-mode rule, the gate, and provenance privacy; fix findings.

- [ ] **Step 7: Commit.**

```bash
git add src/lumen/omlx.py src/lumen/library.py src/lumen/web.py src/lumen/cli.py src/lumen/static tests/test_omlx.py tests/test_library.py tests/test_web.py tests/test_cli.py
git commit -m "feat: astro triage with confirmed exclusion of flagged lights"
```

### Task 5: Captions and alt text

**Files:**
- Modify: `src/lumen/omlx.py` (`Caption`, `parse_caption_response`, `OmlxTagger.caption`)
- Modify: `src/lumen/library.py` (`caption_photo`, `set_caption`, export passes caption/alt text for the share preset)
- Modify: `src/lumen/sidecars.py` (`_xmp(..., alt_text: str | None = None)` → `Iptc4xmpCore:AltTextAccessibility`)
- Modify: `src/lumen/pipelines.py` (`export_jpeg(..., alt_text: str | None = None)`)
- Modify: `src/lumen/web.py` (`POST /api/photos/<id>/caption`, `PUT`-style `POST /api/photos/<id>/caption/edit`)
- Modify: `src/lumen/cli.py` (`caption`)
- Modify: `src/lumen/static/index.html`, `app.js` (inspector caption/alt text inputs)
- Test: `tests/test_omlx.py`, `tests/test_library.py`, `tests/test_pipelines.py`, `tests/test_sidecars.py`, `tests/test_web.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `Library.tagger_for("review")`, `Library.export_jpegs` / `DarktablePipeline.export_jpeg` metadata path, `SidecarWriter._xmp`, `tags.data` merge, inspector.
- Produces:
  - `Caption(caption: str, alt_text: str, raw_response: str)`; `parse_caption_response(raw)`: exactly `{"caption","alt_text"}`, caption 1..200 chars with no `#`, alt text 1..125 chars; else `CaptionResponseError`.
  - `OmlxTagger.caption(preview: Path, timeout=240) -> Caption`: prompt "Write caption (under 200 characters, no hashtags, no emoji) and alt_text (under 125 characters, a literal description of what is visible for a screen reader). Return JSON only. Do not identify a person."; `max_tokens=200`.
  - `Library.caption_photo(photo_id, tagger) -> dict` (`@_coordinated`): merges `tags.data["caption"]`, `tags.data["alt_text"]`, event `caption_written` `{model, role, caption, alt_text, raw_response, author: "model"}`, failure `caption_failed`, sidecar rewritten; returns `{"photo_id","caption","alt_text"}`. `Library.set_caption(photo_id, *, caption: str | None, alt_text: str | None) -> dict`: photographer edit, same limits, event `caption_written` with `author: "photographer"`, empty string clears the field.
  - `SidecarWriter._xmp(description, keywords, *, rating, provenance=None, crop=None, crop_provenance=None, ingest_provenance=None, alt_text: str | None = None)`: when `alt_text`, the `rdf:Description` declares `xmlns:Iptc4xmpCore="http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/"` and includes `<Iptc4xmpCore:AltTextAccessibility><rdf:Alt><rdf:li xml:lang="x-default">…</rdf:li></rdf:Alt></Iptc4xmpCore:AltTextAccessibility>`. Sidecar XMP files carry alt text whenever the tag has it.
  - `DarktablePipeline.export_jpeg(..., alt_text: str | None = None)` passes it to `_xmp`. `Library.export_jpegs`: for `preset == "share"`, `description = data.get("caption") or data.get("description")` and `alt_text = data.get("alt_text")`; for `preset == "full"`, description stays `data.get("description")` and `alt_text=None` (spec: full keeps existing behaviour). Artifact `settings` gain `"caption_used": bool`.
  - Web: `POST /api/photos/<id>/caption {}` → 200 shape (review gate); `POST /api/photos/<id>/caption/edit {"caption": str|null, "alt_text": str|null}` → 200 shape, strict keys and limits.
  - CLI: `lumen caption <photo-id> [--json]`.
  - UI: inspector `label for=caption-input` "Caption" + `input#caption-input maxlength=200`, `label for=alt-input` "Alt text" + `input#alt-input maxlength=125`, `button#save-caption` "Save caption" (posts edit), `button#ask-caption-ai` "Write caption with local AI" (review gate). Typing in these inputs never triggers culling keys (existing rule, but add them to the `culling-keys` scenario).

- [ ] **Step 1: Write failing tests.**

`tests/test_omlx.py`:

```python
    def test_caption_response_enforces_lengths_and_no_hashtags(self):
        good = json.dumps({"caption": "Gentoo penguins porpoise beside the zodiac.", "alt_text": "Three penguins leaping from grey water near a black inflatable boat."})
        self.assertEqual("Gentoo penguins porpoise beside the zodiac.", parse_caption_response(good).caption)
        for bad in ({"caption": "x" * 201, "alt_text": "a"}, {"caption": "fun #trip", "alt_text": "a"}, {"caption": "a", "alt_text": "x" * 126},
                    {"caption": "a", "alt_text": "b", "hashtags": []}, {"caption": "", "alt_text": "b"}):
            with self.subTest(bad=bad), self.assertRaises(CaptionResponseError):
                parse_caption_response(json.dumps(bad))
```

`tests/test_sidecars.py`:

```python
    def test_xmp_carries_alt_text_in_iptc_core_only_when_present(self):
        with_alt = SidecarWriter._xmp("d", [], rating=0, alt_text="Two penguins on ice")
        self.assertIn('xmlns:Iptc4xmpCore="http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/"', with_alt)
        self.assertIn("<Iptc4xmpCore:AltTextAccessibility><rdf:Alt><rdf:li xml:lang=\"x-default\">Two penguins on ice</rdf:li>", with_alt)
        self.assertNotIn("AltTextAccessibility", SidecarWriter._xmp("d", [], rating=0))
```

`tests/test_library.py`:

```python
    def test_caption_is_advisory_editable_and_only_share_export_uses_it(self):
        class FakeTagger:
            model, role = "review-model", "review"
            def caption(self, preview):
                return Caption("Penguins porpoise beside the zodiac.", "Three penguins leaping from grey water.", '{"c":1}')

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new("RGB", (400, 300), (10, 20, 30)).save(root / "p.jpg")
            darktable = write_fake_darktable(root)
            with Library(Settings(library_dir=root / "library")) as library, patch("lumen.pipelines.find_tool", side_effect=lambda name: str(darktable) if name == "darktable-cli" else None):
                library.import_path(root / "p.jpg")
                photo = library.catalog.list_photos()[0]
                library.catalog.save_tags(photo.id, {"description": "Original description", "subjects": ["ice"]}, "fast-model")
                result = library.caption_photo(photo.id, FakeTagger())
                self.assertEqual("Three penguins leaping from grey water.", result["alt_text"])
                event = [e for e in library.catalog.events_for(photo.id) if e.kind == "caption_written"][-1]
                self.assertEqual(("model", '{"c":1}'), (event.details["author"], event.details["raw_response"]))
                edited = library.set_caption(photo.id, caption="Porpoising gentoos.", alt_text=None)
                self.assertEqual(("Porpoising gentoos.", "Three penguins leaping from grey water."), (edited["caption"], edited["alt_text"]))
                with self.assertRaises(ValueError):
                    library.set_caption(photo.id, caption="#nope", alt_text=None)
                self.assertIn("AltTextAccessibility", library.sidecars.paths(photo)[1].read_text())
                share = library.export_jpegs(ExportRequest((photo.id,), None, "share"))[0]
                full = library.export_jpegs(ExportRequest((photo.id,), None, "full"))[0]
            with Image.open(share.output) as image:
                self.assertEqual("Porpoising gentoos.", image.getexif()[270])
                xmp = image.info["xmp"].decode()
                self.assertIn("Three penguins leaping from grey water.", xmp)
            with Image.open(full.output) as image:
                self.assertEqual("Original description", image.getexif()[270])
                self.assertNotIn("AltTextAccessibility", image.info["xmp"].decode())
            self.assertTrue(share.artifact.settings["caption_used"]); self.assertFalse(full.artifact.settings["caption_used"])
```

(Adapt the darktable fake and `ExportRequest` import to the existing export tests in `tests/test_library.py`, e.g. `test_export_records_preset_in_artifact_settings_sidecar_and_event`.)

`tests/test_web.py`:

```python
    def test_caption_routes_gate_validate_and_inspector_has_native_inputs(self):
        class FakeTagger:
            def __init__(self, *args, **kwargs): self.model, self.role = "review-model", kwargs.get("role", "review")
            def caption(self, preview):
                from lumen.omlx import Caption
                return Caption("A caption.", "Alt text.", "{}")

        Image.new("RGB", (120, 90)).save(self.root / "p.jpg")
        both = {"version": None, "default_model": None, "loaded_models": ["Qwen3.8-27B-4bit"]}
        with patch("lumen.library.get_server_status", return_value=both), patch("lumen.library.OmlxTagger", FakeTagger):
            self.wait_job(self.post_json("/api/import", {"path": str(self.root / "p.jpg")})[1]["job_id"])
            self.assertEqual(403, self.post_json("/api/photos/1/caption", {}, headers={"Origin": "https://attacker.example"})[0])
            self.assertEqual(400, self.post_json("/api/photos/1/caption", {"style": "fun"})[0])
            self.assertEqual((200, {"photo_id": 1, "caption": "A caption.", "alt_text": "Alt text."}), self.post_json("/api/photos/1/caption", {}))
            for payload in ({"caption": "x" * 201, "alt_text": None}, {"caption": "a"}, {"caption": "a", "alt_text": "b", "tone": "x"}, {"caption": 1, "alt_text": None}):
                with self.subTest(payload=payload):
                    self.assertEqual(400, self.post_json("/api/photos/1/caption/edit", payload)[0])
            self.assertEqual("Edited.", self.post_json("/api/photos/1/caption/edit", {"caption": "Edited.", "alt_text": None})[1]["caption"])
            self.assertEqual("Edited.", self.get_json("/api/photos/1")[1]["tags"]["data"]["caption"])
        with patch("lumen.library.get_server_status", return_value={"version": None, "default_model": None, "loaded_models": []}):
            self.assertEqual((400, {"error": "review model Qwen3.8-27B-4bit is not loaded in oMLX"}), self.post_json("/api/photos/1/caption", {}))
        html = (Path(__file__).parent.parent / "src/lumen/static/index.html").read_text()
        js = (Path(__file__).parent.parent / "src/lumen/static/app.js").read_text()
        self.assertIn("caption-input", js); self.assertIn("alt-input", js); self.assertIn("maxLength = 125", js)

    def test_javascript_caption_inputs_save_and_never_trigger_culling_keys(self):
        self.run_javascript_cleanup_regression("caption")
```

Node scenario `caption`: inspector renders inputs prefilled from `tags.data`; pressing `x` while focus is on `#caption-input` posts nothing to `/decision`; `#save-caption` posts `/api/photos/1/caption/edit {caption:'Edited.', alt_text:'Alt.'}` and announces `Caption saved for <name>`; `#ask-caption-ai` disabled when `review.loaded` is false.

`tests/test_cli.py`:

```python
    def test_caption_command_prints_caption_and_alt_text(self):
        class FakeTagger:
            def __init__(self, *args, **kwargs): self.model, self.role = "review-model", kwargs.get("role", "review")
            def caption(self, preview):
                return Caption("A caption.", "Alt text.", "{}")

        live = {"version": None, "default_model": None, "loaded_models": ["Qwen3.8-27B-4bit"]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); Image.new("RGB", (120, 90)).save(root / "p.jpg")
            main(["--library", str(root / "library"), "import", str(root / "p.jpg")])
            output = io.StringIO()
            with patch("lumen.library.OmlxTagger", FakeTagger), patch("lumen.library.get_server_status", return_value=live), redirect_stdout(output):
                code = main(["--library", str(root / "library"), "caption", "1", "--json"])
        self.assertEqual((0, "Alt text."), (code, json.loads(output.getvalue())["alt_text"]))
```

- [ ] **Step 2: Run the focused modules; expect import errors, missing `alt_text` keyword, 404s.**

- [ ] **Step 3: Implement `omlx.py` and `sidecars._xmp` alt text.**

- [ ] **Step 4: Implement library, pipelines, web, CLI, inspector inputs** per Interfaces. `set_caption` validation shares one `_check_caption(caption, alt_text)` helper with the parser limits (import the constants `CAPTION_MAX = 200`, `ALT_TEXT_MAX = 125` from `omlx.py`). Inspector inputs are created in `renderInspector` with `maxLength` set from the same constants and `data-no-cull` is unnecessary because `handleCullingKey` already ignores inputs.

- [ ] **Step 5: Run focused modules, full suite (`tests.test_pipelines` included), `node --check`; expect PASS.**

- [ ] **Step 6: Independent review** of Task 5 against spec feature 6 and the acceptance line "Captions and alt text appear in the share JPEG metadata and nowhere in the full-preset export unless the photographer set a description", the review gate, provenance author, and length limits; fix findings.

- [ ] **Step 7: Commit.**

```bash
git add src/lumen/omlx.py src/lumen/library.py src/lumen/sidecars.py src/lumen/pipelines.py src/lumen/web.py src/lumen/cli.py src/lumen/static tests/test_omlx.py tests/test_library.py tests/test_sidecars.py tests/test_web.py tests/test_cli.py
git commit -m "feat: captions and alt text for share exports"
```

### Task 6: Daily digest

**Files:**
- Create: `src/lumen/digest.py` (`day_facts`, `render_facts_table`, `write_digest`)
- Modify: `src/lumen/omlx.py` (`parse_digest_response`, `OmlxTagger.write_digest`)
- Modify: `src/lumen/library.py` (`digest_day`)
- Modify: `src/lumen/web.py` (`POST /api/digests`, `digest_dir` in `/api/status`)
- Modify: `src/lumen/cli.py` (`digest`)
- Modify: `src/lumen/static/index.html`, `app.js` (rail "Write today's digest" button with overwrite confirmation)
- Create: `tests/test_digest.py`
- Test: `tests/test_omlx.py`, `tests/test_library.py`, `tests/test_web.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `Catalog.list_groups`, `Library.keeper_ids`, `Catalog.list_jobs`/`job_status` for astro jobs (`kind == "astro"` with `created_at` on the day), `tags.data.subjects`, `Settings.digest_dir`, `tagger_for("review")`, `SidecarWriter._atomic_write`.
- Produces:
  - `digest.day_facts(library, day: date) -> dict`: `{"date", "frames", "keepers", "rejects", "bursts", "astro_jobs": [{"job_id","status","lights"}], "keywords": [[keyword, count], ...] (top 20 by count then name), "top_frames": [{"photo_id","name","score","burst"}] (five highest `total_score`, non-rejected)}` over photos whose `captured_at` date (local date as stored) equals `day`.
  - `digest.render_facts_table(facts) -> str`: Markdown with a heading `# Lumen digest <date>`, a two-column table of the counts, a keywords table, a top-frames table, and an astro section; with `frames == 0` the body is the heading plus "No frames were imported for this day." and no tables.
  - `parse_digest_response(raw) -> str`: exactly `{"narrative"}`, 1..1500 characters; else `DigestResponseError`. `OmlxTagger.write_digest(facts_markdown: str, timeout=240) -> str`: text-only prompt "Write a short field-notes narrative (3 to 6 sentences, plain prose, no lists, no hashtags) from these facts only. Do not invent species, places, or counts. Return JSON only with narrative." plus the facts; `max_tokens=600`. No image part.
  - `Library.digest_day(day: date, tagger=None, *, overwrite: bool = False) -> dict`: `path = settings.digest_dir / f"{day.isoformat()}.md"`; if it exists and not `overwrite` → `FileExistsError("digest exists; confirm overwrite")`; builds facts and the table; if `frames == 0` the narrative step is skipped; otherwise if `tagger is None` (caller could not get the review model) appends `"\n\n## Narrative\n\nNarrative unavailable: <reason>\n"` where the caller passes `reason` via `unavailable_reason: str | None`; else calls `write_digest` and appends `## Narrative` with the text; on model failure appends `Narrative unavailable: <public error>`; writes atomically; event `digest_written` (`photo_id=None`) `{date, path_name: "<date>.md", frames, narrative: bool, model, role, raw_response}`; returns `{"date", "file": "<date>.md", "frames", "narrative": bool, "overwritten": bool}`. Always exit-0 shaped: a model failure is not an error.
  - Web: `/api/status` gains `digest_dir`; `POST /api/digests {"date": "YYYY-MM-DD", "overwrite"?: bool}` → 200 shape, 409 `{"error": "digest exists; confirm overwrite", "exists": true}` when the file exists and `overwrite` is not true; the review gate is soft here: an unloaded review model produces the facts-only file with the unavailable reason, not a 400.
  - CLI: `lumen digest <YYYY-MM-DD> [--overwrite] [--json]`; prints the shape; exit 2 only on invalid date or existing file without `--overwrite` (with `{"error"}`).
  - UI: `button#write-digest` "Write today's digest" in `#rail-header`; it posts today's local date; a 409 opens `dialog#digest-confirm` "A digest for <date> already exists. Overwrite it?" with Confirm reposting `overwrite: true`; the result announces `Digest written for <date>: <file> (narrative included|facts only)` and shows the file name under the button in `p#digest-result`.

- [ ] **Step 1: Write failing tests.**

`tests/test_digest.py`:

```python
import io, json, tempfile, unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from lumen.config import Settings
from lumen.library import Library
from lumen.digest import day_facts, render_facts_table


def _frames(root, day="2026:10:22", count=3):
    for index in range(count):
        image = Image.new("RGB", (120, 90), (index * 40, 0, 0)); exif = Image.Exif(); exif[36867] = f"{day} 10:00:{index:02d}"
        image.save(root / f"d{index}.jpg", exif=exif)


class DigestTests(unittest.TestCase):
    def test_facts_count_frames_keepers_rejects_keywords_and_top_frames_for_the_day_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); _frames(root); _frames(root / "other", "2026:10:21", 1) if (root / "other").mkdir() is None else None
            with Library(Settings(library_dir=root / "library")) as library:
                library.import_path(root)
                photos = sorted(library.catalog.list_photos(), key=lambda p: p.path)
                library.set_decision(photos[1].id, "reject")
                library.catalog.save_tags(photos[0].id, {"description": "d", "subjects": ["ice", "gentoo penguin"]}, "m")
                library.catalog.save_tags(photos[2].id, {"description": "d", "subjects": ["ice"]}, "m")
                facts = day_facts(library, date(2026, 10, 22))
        self.assertEqual((3, 1, 1), (facts["frames"], facts["rejects"], facts["bursts"]))
        self.assertEqual([["ice", 2], ["gentoo penguin", 1]], facts["keywords"])
        self.assertEqual(2, len(facts["top_frames"]))
        table = render_facts_table(facts)
        self.assertIn("# Lumen digest 2026-10-22", table); self.assertIn("| Frames | 3 |", table); self.assertIn("| ice | 2 |", table)

    def test_empty_day_renders_sentence_without_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            with Library(Settings(library_dir=Path(directory) / "library")) as library:
                facts = day_facts(library, date(2026, 10, 23))
        text = render_facts_table(facts)
        self.assertIn("No frames were imported for this day.", text); self.assertNotIn("|", text)
```

`tests/test_library.py`:

```python
    def test_digest_writes_facts_first_and_narrative_only_when_the_model_answers(self):  # Review Focus
        class FakeTagger:
            model, role = "review-model", "review"
            def __init__(self, fail=False): self.fail = fail; self.prompt = None
            def write_digest(self, facts_markdown):
                self.prompt = facts_markdown
                if self.fail: raise OmlxError("Local oMLX request failed: boom")
                return "A calm day with gentoos."

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(2):
                image = Image.new("RGB", (120, 90), (index * 40, 0, 0)); exif = Image.Exif(); exif[36867] = f"2026:10:22 10:00:0{index}"
                image.save(root / f"d{index}.jpg", exif=exif)
            with Library(Settings(library_dir=root / "library")) as library:
                library.import_path(root)
                tagger = FakeTagger()
                result = library.digest_day(date(2026, 10, 22), tagger)
                path = library.settings.digest_dir / "2026-10-22.md"
                text = path.read_text()
                self.assertTrue(result["narrative"]); self.assertIn("| Frames | 2 |", text); self.assertLess(text.index("| Frames"), text.index("## Narrative"))
                self.assertIn("A calm day with gentoos.", text); self.assertIn("| Frames | 2 |", tagger.prompt)
                with self.assertRaises(FileExistsError):
                    library.digest_day(date(2026, 10, 22), tagger)
                result = library.digest_day(date(2026, 10, 22), FakeTagger(fail=True), overwrite=True)
                text = path.read_text()
                self.assertFalse(result["narrative"]); self.assertIn("| Frames | 2 |", text); self.assertIn("Narrative unavailable: Local oMLX request failed: boom", text)
                result = library.digest_day(date(2026, 10, 22), None, overwrite=True, unavailable_reason="review model Qwen3.8-27B-4bit is not loaded in oMLX")
                self.assertIn("Narrative unavailable: review model Qwen3.8-27B-4bit is not loaded in oMLX", path.read_text())
                empty = library.digest_day(date(2026, 10, 23), FakeTagger(fail=True))
                self.assertEqual((0, False), (empty["frames"], empty["narrative"]))
                self.assertIn("No frames were imported for this day.", (library.settings.digest_dir / "2026-10-23.md").read_text())
                events = [e for e in library.catalog.events_for(None) if e.kind == "digest_written"]
                self.assertEqual(4, len(events)); self.assertEqual("2026-10-22.md", events[0].details["path_name"]); self.assertNotIn("/", events[0].details["path_name"])
```

`tests/test_omlx.py`:

```python
    def test_digest_response_is_a_single_bounded_narrative_and_request_sends_no_image(self):
        self.assertEqual("Calm.", parse_digest_response(json.dumps({"narrative": "Calm."})))
        for bad in ({"narrative": ""}, {"narrative": "x" * 1501}, {"narrative": "a", "title": "b"}, ["a"]):
            with self.subTest(bad=bad), self.assertRaises(DigestResponseError):
                parse_digest_response(json.dumps(bad))
        tagger = OmlxTagger("http://127.0.0.1:8000", "review-model", api_key="k", role="review")
        captured = {}
        with patch.object(tagger, "_complete", lambda content, timeout, **kw: (captured.update(content=content), json.dumps({"narrative": "Calm."}))[1]):
            tagger.write_digest("| Frames | 2 |")
        self.assertTrue(all(part["type"] == "text" for part in captured["content"]))
        self.assertIn("Do not invent species, places, or counts.", captured["content"][0]["text"])
```

`tests/test_web.py`:

```python
    def test_digest_route_confirms_overwrite_and_degrades_without_review_model(self):
        for index in range(2):
            image = Image.new("RGB", (120, 90), (index * 40, 0, 0)); exif = Image.Exif(); exif[36867] = f"2026:10:22 10:00:0{index}"
            image.save(self.root / f"d{index}.jpg", exif=exif)
        none = {"version": None, "default_model": None, "loaded_models": []}
        with patch("lumen.library.get_server_status", return_value=none):
            self.wait_job(self.post_json("/api/import", {"path": str(self.root)})[1]["job_id"])
            self.assertEqual(403, self.post_json("/api/digests", {"date": "2026-10-22"}, headers={"Origin": "https://attacker.example"})[0])
            for payload in ({"date": "22/10/2026"}, {"date": "2026-10-22", "overwrite": "yes"}, {}, {"date": "2026-10-22", "model": "x"}):
                with self.subTest(payload=payload):
                    self.assertEqual(400, self.post_json("/api/digests", payload)[0])
            code, body = self.post_json("/api/digests", {"date": "2026-10-22"})
            self.assertEqual((200, {"date": "2026-10-22", "file": "2026-10-22.md", "frames": 2, "narrative": False, "overwritten": False}), (code, body))
            self.assertEqual((409, True), (lambda r: (r[0], r[1]["exists"]))(self.post_json("/api/digests", {"date": "2026-10-22"})))
            self.assertTrue(self.post_json("/api/digests", {"date": "2026-10-22", "overwrite": True})[1]["overwritten"])
            self.assertIn("digest_dir", self.get_json("/api/status")[1])
            text = (self.library_dir / "digests" / "2026-10-22.md").read_text()
            self.assertIn("Narrative unavailable: review model Qwen3.8-27B-4bit is not loaded in oMLX", text)

    def test_javascript_digest_button_confirms_overwrite_and_announces_result(self):
        self.run_javascript_cleanup_regression("digest")
```

Node scenario `digest`: first POST answers 409 `{error, exists: true}` → `#digest-confirm` opens with the date in its text; Confirm reposts with `overwrite: true`; announcer says `Digest written for <date>: <date>.md (facts only)`; `#digest-result` shows the file name.

`tests/test_cli.py`:

```python
    def test_digest_command_writes_file_and_requires_overwrite_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = Image.new("RGB", (120, 90)); exif = Image.Exif(); exif[36867] = "2026:10:22 10:00:00"; image.save(root / "d.jpg", exif=exif)
            main(["--library", str(root / "library"), "import", str(root / "d.jpg")])
            offline = {"version": None, "default_model": None, "loaded_models": []}
            output = io.StringIO()
            with patch("lumen.library.get_server_status", return_value=offline), redirect_stdout(output):
                code = main(["--library", str(root / "library"), "digest", "2026-10-22", "--json"])
            self.assertEqual((0, False), (code, json.loads(output.getvalue())["narrative"]))
            self.assertTrue((root / "library" / "digests" / "2026-10-22.md").exists())
            output = io.StringIO()
            with patch("lumen.library.get_server_status", return_value=offline), redirect_stdout(output):
                code = main(["--library", str(root / "library"), "digest", "2026-10-22", "--json"])
            self.assertEqual((2, "digest exists; confirm overwrite"), (code, json.loads(output.getvalue())["error"]))
            self.assertEqual(2, main(["--library", str(root / "library"), "digest", "22-10-2026", "--json"]))
```

- [ ] **Step 2: Run the focused modules; expect `ModuleNotFoundError: lumen.digest`, missing methods, 404s.**

- [ ] **Step 3: Implement `digest.py`, `omlx.py` additions, `Library.digest_day`, routes, CLI, rail button** per Interfaces. The web handler and CLI do `try: tagger = library.tagger_for("review"); reason = None except RuntimeError as error: tagger, reason = None, str(error)` then `library.digest_day(day, tagger, overwrite=overwrite, unavailable_reason=reason)`. `Settings.ensure_directories` does not need to create `digest_dir`; `digest_day` creates it. `/api/status` adds `"digest_dir": str(settings.digest_dir.resolve())` beside the other managed paths. Today's date in `app.js` is `new Date().toLocaleDateString('sv-SE')` (ISO in local time).

- [ ] **Step 4: Run focused modules, full suite, `node --check`; expect PASS.**

- [ ] **Step 5: Independent review** of Task 6 against spec feature 5 and the acceptance line "A digest file is written with the facts table even when the model call fails", the no-images rule, library-only output, overwrite confirmation, and its Review Focus line; fix findings.

- [ ] **Step 6: Commit.**

```bash
git add src/lumen/digest.py src/lumen/omlx.py src/lumen/library.py src/lumen/web.py src/lumen/cli.py src/lumen/static tests/test_digest.py tests/test_omlx.py tests/test_library.py tests/test_web.py tests/test_cli.py
git commit -m "feat: daily digest with facts first and an optional local narrative"
```

### Task 7: Natural-language find

**Files:**
- Modify: `src/lumen/catalog.py` (`GroupFilters.where` text clause covers every AI field; `Catalog.match_terms(photo_id, text) -> list[str]`)
- Modify: `src/lumen/web.py` (`/api/photos/<id>?text=` returns `matched`, or `/api/groups` photos carry `matched` when `text` is set)
- Modify: `src/lumen/static/index.html`, `app.js` (rail `#find` input bound to the existing `text` filter; inspector "Matches: …" line)
- Test: `tests/test_catalog.py`, `tests/test_web.py`

**Interfaces:**
- Consumes: `GroupFilters.text`, `parse_group_filters`, `applyFilters()` / `filterQuery()` in `app.js`, `tags.data` keys from Tasks 1, 2, 5.
- Produces:
  - `GroupFilters.where` text clause: `LIKE` against `json_extract(t.data_json, '$.description')`, `'$.subjects'`, `'$.suggested'`, `'$.species'`, `'$.caption'`, `'$.problems.kinds'` (each `json_extract` of an array yields its JSON text, which `LIKE` can search; problem kinds match on the underscore form and on the human form with spaces, so the pattern is applied to `replace(json_extract(...), '_', ' ')` for problems). The same escaped pattern and the `tagged_only` behaviour are unchanged.
  - `Catalog.match_terms(photo_id, text) -> list[str]`: `["description", "keyword: ice", "suggested: krill", "species: gentoo penguin", "caption", "problem: motion blur"]` style labels for every field that contains the text (case-insensitive), empty list when no tags row.
  - `/api/groups` with `text=` set: each photo gains `"matched": [...]` from `match_terms` (empty when not set, key always present so the client code is unconditional).
  - UI: `input#find` (`type="search"`, label "Find", placeholder "penguin, blur, caption words…") in `#rail-header`; it is the same filter as `#filter-text` (both write `text` and stay in sync: `syncFilters` sets both from the URL, input on either updates the other and calls `applyFilters`). The inspector shows `p#match-reasons` "Matches: keyword: ice, caption" when the selected photo has `matched`. Typing in `#find` never triggers culling keys (existing input rule).

- [ ] **Step 1: Write failing tests.**

`tests/test_catalog.py` (extend `seed_filter_photos` or seed inline):

```python
    def test_text_filter_searches_every_local_ai_field_and_match_terms_explains(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "catalog.sqlite3")
            ids = seed_filter_photos(catalog)  # existing helper; returns photo ids
            catalog.save_tags(ids[0], {"description": "Penguins on ice", "subjects": ["gentoo penguin"], "suggested": ["krill"],
                                       "species": [{"name": "gentoo penguin", "confidence": 0.8}], "caption": "Porpoising gentoos",
                                       "problems": {"kinds": ["motion_blur"], "suggest_reject": True}}, "m")
            catalog.save_tags(ids[1], {"description": "Ship at anchor", "subjects": ["ship"]}, "m")
            def hits(text):
                return sorted(p["id"] for g in catalog.list_groups(GroupFilters(text=text, show_rejected=True)) for p in g["photos"])
            self.assertEqual([ids[0]], hits("krill"))
            self.assertEqual([ids[0]], hits("gentoo"))
            self.assertEqual([ids[0]], hits("porpoising"))
            self.assertEqual([ids[0]], hits("motion blur"))
            self.assertEqual([ids[0]], hits("motion_blur"))
            self.assertEqual([ids[1]], hits("ship"))
            self.assertEqual([], hits("100%"))  # LIKE wildcards are escaped
            self.assertEqual(["keyword: gentoo penguin", "species: gentoo penguin", "caption"], catalog.match_terms(ids[0], "gentoo"))
            self.assertEqual(["problem: motion blur"], catalog.match_terms(ids[0], "blur"))
            self.assertEqual([], catalog.match_terms(ids[2], "blur"))
            catalog.close()
```

(`match_terms` checks fields in this fixed order: `description`, each `keyword: <k>`, each `suggested: <k>`, each `species: <name>`, `caption`, each `problem: <kind with spaces>`.)

`tests/test_web.py`:

```python
    def test_groups_report_match_reasons_only_when_text_filter_is_set(self):
        Image.new("RGB", (120, 90)).save(self.root / "p.jpg")
        self.wait_job(self.post_json("/api/import", {"path": str(self.root / "p.jpg")})[1]["job_id"])
        with Library(Settings(library_dir=self.library_dir)) as library:
            library.catalog.save_tags(1, {"description": "d", "subjects": ["ice"], "caption": "Ice floe"}, "m")
        plain = self.get_json("/api/groups")[1]["groups"][0]["photos"][0]
        self.assertEqual([], plain["matched"])
        found = self.get_json("/api/groups?text=ice")[1]["groups"][0]["photos"][0]
        self.assertEqual(["keyword: ice", "caption"], found["matched"])
        self.assertEqual([], self.get_json("/api/groups?text=zodiac")[1]["groups"])

    def test_rail_find_input_is_labelled_and_mirrors_the_text_filter(self):
        html = (Path(__file__).parent.parent / "src/lumen/static/index.html").read_text()
        self.assertIn('<label for="find">Find</label>', html); self.assertIn('id="find"', html)

    def test_javascript_find_input_filters_and_inspector_shows_match_reasons(self):
        self.run_javascript_cleanup_regression("find")
```

Node scenario `find`: typing `ice` into `#find` and dispatching `input` calls `applyFilters` with a query containing `text=ice`; `#filter-text` value becomes `ice`; the groups fetch returns photos with `matched: ['keyword: ice']`; after `inspect(photo1)`, `#match-reasons` text is `Matches: keyword: ice`; pressing `x` with focus in `#find` posts no decision.

- [ ] **Step 2: Run `tests.test_catalog tests.test_web`; expect failing LIKE clauses, missing `match_terms`, missing `matched`, missing `#find`.**

- [ ] **Step 3: Implement** the catalog clause and `match_terms` (Python-side scan of the parsed `data` dict, same casefold contains rule), the `matched` key in `web.py` `/api/groups` loop (`library.catalog.match_terms(photo["id"], filters.text) if filters.text else []`), the rail input, sync logic, and the inspector line.

- [ ] **Step 4: Run focused modules, full suite, `node --check`; expect PASS.**

- [ ] **Step 5: Independent review** of Task 7 against spec feature 7 (no model call, matching terms visible), the input-never-culls rule, and filter escaping; fix findings.

- [ ] **Step 6: Commit.**

```bash
git add src/lumen/catalog.py src/lumen/web.py src/lumen/static tests/test_catalog.py tests/test_web.py
git commit -m "feat: natural-language find across local AI fields with match reasons"
```

---

## Final verification (after Task 7)

- [ ] Run `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest discover -s tests -v` and `node --check src/lumen/static/app.js`; expect PASS. Confirm `grep -rn "127.0.0.1:8000\|urlopen\|build_opener" tests/` shows only patched openers (no test contacts oMLX).
- [ ] Run `uv run lumen doctor --json` and confirm `omlx_roles` reports both roles with live `loaded` flags.
- [ ] Whole-branch independent review against the spec's acceptance criteria list; fix findings; update `docs/HANDOFF.md` and `AGENTS.md` (plan status, new env vars `LUMEN_OMLX_FAST_MODEL`/`LUMEN_OMLX_REVIEW_MODEL`, the `tag --keepers` default scope).
