# Settings View and AI Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Settings view in the web app, and a switch that sends all advice to this Mac, to the photographer's own server (Base) or to an online service (Online).

**Architecture:** Saved settings are rows in the catalog's existing `preferences` table, laid over the environment-built `Settings` by one new module. The existing chat client (`OmlxTagger`) learns an `engine` that decides URL rule, key source, two request details and the error label; everything above it keeps calling `Library.tagger_for(role)`. Keys live in the macOS Keychain behind a three-function module.

**Tech Stack:** Python 3 stdlib + Pillow (no new dependency), `unittest`, `/usr/bin/security`, plain HTML/CSS/JS in `src/lumen/static/`.

**Spec:** `docs/superpowers/specs/2026-11-21-settings-and-ai-engine-design.md`. Read it and `AGENTS.md` before starting.

## Global Constraints

- Pillow stays the only runtime Python dependency.
- The HTTP server stays loopback-only; every new mutation route goes through `_trusted_mutation()` and validates its JSON strictly (exact key sets, exact types; `bool` is not an `int`).
- Never send to the browser, log, or print: an API key, a source path, a fingerprint, raw model output.
- No test contacts a network, the Keychain, or oMLX. Tests patch `lumen.library.get_server_status`, `lumen.diagnostics.get_server_status`, `lumen.library.OmlxTagger` as today, and inject the `security` runner and the URL opener.
- No AI vendor or model-family names in tracked files or commit messages beyond those already present. No co-author trailers.
- Engine names in code and JSON are exactly `mac`, `base`, `online`; their labels in every message and on the page are exactly `This Mac`, `Base`, `Online`.
- Existing messages on engine `mac` do not change (tests pin "Local oMLX request failed", "is not loaded in oMLX").
- Run tests warning-strict with the model variable unset: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest tests.test_<module> -v`. Full suite: `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest discover -s tests -v`. After any `app.js` edit: `node --check src/lumen/static/app.js`.
- Exports are out of scope: do not touch export destinations.
- Work on branch `settings-ai-engine`. One commit per task, message in the style of `git log` (plain sentence, no prefix required).

## Review Focus

Conditions the spec implies that a person will hit. Each has a test in the task named.

1. The saved engine is Online but its key was removed in Keychain Access: advice is refused with "Online has no key: add one in Settings", not a 401 dump (Task 5).
2. The address is typed the way each service documents it, which differs: a bare host (`http://pc.local:11434`), a host with `/v1/`, or a longer base path (`https://api.example.com/v1beta/compat/`). All three must reach `<base>/chat/completions`, never `/v1/v1/...` and never a stripped path (Task 4).
3. The remote answers an error (`401` with `{"error": {"message": "…"}}`, or an HTML page): the refusal names the engine and carries the service's reason clipped to 200 characters, never the whole body (Task 4).
4. A saved value is unreadable (hand-edited row, older version) or the catalog does not exist yet: that setting falls back to its default and the server and CLI still start (Task 1).
5. Base is asleep when the photographer switches to it, or when a server with Base saved starts: the switch is refused and the engine stays as it was; a started server shows "AI: Base · not reachable" and refuses advice readably without hanging the status poll (Tasks 5 and 7).

---

### Task 1: Saved settings store

**Files:**
- Create: `src/lumen/preferences.py`, `tests/test_preferences.py`
- Modify: `src/lumen/config.py` (new fields), `src/lumen/cli.py` (`main`, after the `--library` override is applied to `settings`)

**Interfaces:**
- Produces, in `config.Settings` (all defaulted): `ai_engine: str = "mac"`, `base_url: str = ""`, `base_fast_model: str = ""`, `base_review_model: str = ""`, `online_url: str = ""`, `online_fast_model: str = ""`, `online_review_model: str = ""`; and `Settings.model_for(role: str) -> str` (on `mac`: `omlx_fast_model` / `review_model` as `omlx.ROLES` does today; on `base`/`online`: `<engine>_<role>_model`).
- Produces, in `preferences`:
  - `ENGINES = ("mac", "base", "online")`, `ENGINE_LABELS = {"mac": "This Mac", "base": "Base", "online": "Online"}`
  - `SAVED: dict[str, str | None]`, setting key (identical to the `Settings` field name) to the shell variable that fixes it: `{"ai_engine": None, "base_url": None, "base_fast_model": None, "base_review_model": None, "online_url": None, "online_fast_model": None, "online_review_model": None, "near_duplicate_distance": "LUMEN_NEAR_DUP_DISTANCE", "geocode_online": "LUMEN_GEOCODE_ONLINE"}`
  - `validate(key: str, value) -> object`: returns the stored form or raises `ValueError` with a sentence for the page. `ai_engine` in `ENGINES`; `near_duplicate_distance` an `int` 0 to 64; `geocode_online` a `bool`; model names `str`, at most 200 characters, no control characters; URLs `str` (rule added in Task 4, any string for now); unknown key: `ValueError("unknown setting: <key>")`.
  - `overlay(settings: Settings, catalog: Catalog) -> Settings`: `dataclasses.replace` with every saved, valid, not-shell-fixed value. A shell-fixed key (its variable is in `os.environ`) keeps the environment's value.
  - `load(settings: Settings) -> Settings`: `overlay` through a `Catalog` opened on `settings.database_path` when that file exists, else `settings` unchanged.
  - `save(catalog: Catalog, changes: dict) -> None`: validates all, refuses a shell-fixed key with `ValueError("<key> is set by your shell (<VAR>)")`, then writes each as JSON text under preference key `setting.<key>`. All or nothing.
  - `describe(settings: Settings, catalog: Catalog) -> list[dict]`: one `{"key", "value", "source", "editable"}` per `SAVED` key, `source` one of `"shell"`, `"saved"`, `"default"`, `editable` false only for `"shell"`.

- [ ] **Step 1: Write the failing tests** in `tests/test_preferences.py` (temporary library dir, `Catalog(settings.database_path)` as other tests open it):

```python
def test_saved_value_overlays_default(self):
    preferences.save(catalog, {"near_duplicate_distance": 8, "geocode_online": True})
    loaded = preferences.overlay(settings, catalog)
    self.assertEqual((loaded.near_duplicate_distance, loaded.geocode_online), (8, True))

def test_shell_variable_wins_and_is_not_editable(self):
    with patch.dict(os.environ, {"LUMEN_NEAR_DUP_DISTANCE": "12"}):
        with self.assertRaisesRegex(ValueError, r"set by your shell \(LUMEN_NEAR_DUP_DISTANCE\)"):
            preferences.save(catalog, {"near_duplicate_distance": 8})
        row = next(r for r in preferences.describe(settings, catalog) if r["key"] == "near_duplicate_distance")
        self.assertEqual((row["source"], row["editable"]), ("shell", False))

def test_save_is_all_or_nothing(self):
    with self.assertRaises(ValueError):
        preferences.save(catalog, {"geocode_online": True, "near_duplicate_distance": 65})
    self.assertFalse(preferences.overlay(settings, catalog).geocode_online)

def test_unreadable_saved_value_falls_back_to_default(self):   # Review Focus 4
    catalog.set_preference("setting.near_duplicate_distance", "not json{")
    catalog.set_preference("setting.ai_engine", '"cloud"')
    loaded = preferences.overlay(settings, catalog)
    self.assertEqual((loaded.near_duplicate_distance, loaded.ai_engine), (12, "mac"))

def test_load_without_a_catalog_returns_settings_unchanged(self):   # Review Focus 4
    fresh = Settings(library_dir=Path(self.tmp) / "none")
    self.assertIs(preferences.load(fresh), fresh)
    self.assertFalse(fresh.database_path.exists())

def test_validate_rejects_bool_as_distance_and_unknown_key(self): ...   # True is not an int; "nope" -> "unknown setting: nope"

def test_model_for_follows_the_engine(self):
    s = replace(settings, ai_engine="base", base_fast_model="small", base_review_model="big")
    self.assertEqual((s.model_for("fast"), s.model_for("review")), ("small", "big"))
    self.assertEqual(settings.model_for("review"), settings.review_model)
```

- [ ] **Step 2: Run** `tests.test_preferences`; expected: import error for `lumen.preferences`.
- [ ] **Step 3: Implement** the fields, `model_for`, and `preferences.py` to the interfaces above.
- [ ] **Step 4: Lay saved settings over the CLI's.** In `cli.main`, once `settings` has its final `library_dir`, `settings = preferences.load(settings)`. Add to `tests/test_cli.py`: a library with `setting.near_duplicate_distance` saved as `8` makes `doctor --json` (or the nearest command that reports it; if none does, assert through `patch("lumen.cli.Library")` that the `Settings` it receives has `near_duplicate_distance == 8`).
- [ ] **Step 5: Run** `tests.test_preferences`, `tests.test_cli`; expected: pass.
- [ ] **Step 6: Commit.**

### Task 2: Settings routes

**Files:**
- Modify: `src/lumen/web.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `preferences.describe`, `preferences.save`, `preferences.overlay`, `preferences.load`.
- Produces: `GET /api/settings` answers `{"settings": [<describe rows>], "keys": {"base": false, "online": false}}` (`keys` becomes live in Task 3). `POST /api/settings` takes a non-empty object of setting keys, saves, rebinds the handler's `settings` with `preferences.overlay(settings, library.catalog)` (the same `nonlocal settings` rebinding `/api/role-model` uses, so a session role-model switch on `mac` survives), and answers the same body as the GET. `make_server` calls `preferences.load(settings)` once at the top.

- [ ] **Step 1: Write the failing tests** (follow the existing server fixtures in `tests/test_web.py`):

```python
def test_settings_round_trip_applies_to_the_running_server(self):
    status, body = self.post("/api/settings", {"geocode_online": True})
    self.assertEqual(status, 200)
    row = next(r for r in body["settings"] if r["key"] == "geocode_online")
    self.assertEqual((row["value"], row["source"]), (True, "saved"))
    self.assertTrue(self.get("/api/status")[1]["geonames"]["online"])   # the live server now uses it

def test_settings_refuses_unknown_empty_and_non_object(self): ...   # {} -> 400; {"nope": 1} -> 400 "unknown setting: nope"; [] -> 400
def test_settings_post_is_refused_cross_origin(self): ...            # same assertion style as the existing cross-origin tests, 403
def test_settings_never_reports_a_key(self):
    self.assertNotIn("api_key", json.dumps(self.get("/api/settings")[1]))
def test_saved_settings_are_loaded_when_the_server_starts(self): ...   # save, build a new server on the same library, GET shows source "saved"
```

- [ ] **Step 2: Run**; expected: 404s.
- [ ] **Step 3: Implement** both routes. A `ValueError` from `preferences.save` reaches the browser through the existing `public_message` 400 path.
- [ ] **Step 4: Run** `tests.test_web`; expected: pass.
- [ ] **Step 5: Commit.**

### Task 3: Keychain keys

**Files:**
- Create: `src/lumen/keychain.py`, `tests/test_keychain.py`
- Modify: `src/lumen/web.py`; Test: `tests/test_web.py`

**Interfaces:**
- Produces, in `keychain` (service name `Lumen`, account = engine, only `base` and `online` accepted, else `ValueError`):
  - `KEY_PATTERN = re.compile(r"[A-Za-z0-9._\-+/=:~]{8,512}")`
  - `get_key(engine: str, run=subprocess.run) -> str | None`: `["/usr/bin/security", "find-generic-password", "-s", "Lumen", "-a", engine, "-w"]`, `None` on a non-zero exit.
  - `set_key(engine: str, key: str, run=subprocess.run) -> None`: `KEY_PATTERN.fullmatch` or `ValueError("that does not look like an API key")`; runs `["/usr/bin/security", "-i"]` with `input=f'add-generic-password -U -s Lumen -a {engine} -w "{key}"\n'`. The key is never an argv element. Non-zero exit: `RuntimeError("could not save the key to the Keychain")`.
  - `delete_key(engine: str, run=subprocess.run) -> None`: `delete-generic-password -s Lumen -a <engine>`; a missing item is not an error.
- Produces routes: `POST /api/settings/key` body exactly `{"engine", "key"}` answers `{"engine", "key_saved": true}`; `DELETE /api/settings/key/<engine>` answers `{"engine", "key_saved": false}`; `GET /api/settings` `keys` now reports `get_key(...) is not None` per engine. `web.py` calls the functions as `keychain.get_key(...)` so tests patch `lumen.web.keychain`.

- [ ] **Step 1: Write the failing tests.**

```python
def test_set_key_never_puts_the_key_on_a_command_line(self):
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs)); return subprocess.CompletedProcess(argv, 0, "", "")
    keychain.set_key("online", "sk-abc12345", run=run)
    argv, kwargs = calls[0]
    self.assertNotIn("sk-abc12345", " ".join(argv))
    self.assertIn('-a online -w "sk-abc12345"', kwargs["input"])

def test_set_key_refuses_quotes_spaces_and_short_keys(self): ...   # 'a"b; rm', "has space", "short" -> ValueError, run never called
def test_get_key_is_none_when_missing(self): ...                    # returncode 44 -> None
def test_only_base_and_online_have_keys(self): ...                  # "mac" -> ValueError
# tests/test_web.py, with patch("lumen.web.keychain") as fake:
def test_key_route_saves_and_never_echoes(self): ...   # response has no "key" field; fake.set_key called with ("online", "sk-abc12345")
def test_key_route_refuses_extra_fields_and_mac(self): ...
def test_delete_key_route(self): ...
```

- [ ] **Step 2: Run**; expected: fail.
- [ ] **Step 3: Implement** module and routes. The `DELETE` route goes where the existing `do_DELETE` routes live.
- [ ] **Step 4: Verify the real command by hand, once** (this is the only step that touches the Keychain; it uses a throwaway account and removes it):

```sh
printf 'add-generic-password -U -s LumenPlanCheck -a base -w "check-12345678"\n' | /usr/bin/security -i
/usr/bin/security find-generic-password -s LumenPlanCheck -a base -w    # prints check-12345678
/usr/bin/security delete-generic-password -s LumenPlanCheck -a base
```

If `security -i` does not accept the quoted form, stop and report; do not fall back to passing the key as an argument.
- [ ] **Step 5: Run** `tests.test_keychain`, `tests.test_web`; expected: pass.
- [ ] **Step 6: Commit.**

### Task 4: The client learns engines

**Files:**
- Modify: `src/lumen/omlx.py`, `src/lumen/preferences.py` (URL validation)
- Test: `tests/test_omlx.py`, `tests/test_preferences.py`

**Interfaces:**
- Produces, in `omlx`:
  - `normalise_engine_url(engine: str, url: str) -> str`: strips whitespace and trailing `/`. For `base`/`online` the result is the service's **API base**, path included: a URL with no path gets `/v1` appended, a URL with a path is kept as typed. For `mac` a trailing `/v1` is stripped (today's form). `api_base(engine, url) -> str` returns `url + "/v1"` on `mac` and `url` otherwise; every request is `api_base + "/chat/completions"` or `api_base + "/models"`. `mac`: loopback host only, message unchanged ("oMLX endpoint must be a loopback http(s) URL"). `base`: scheme `http` or `https` and a host, else `ValueError("Base needs an http or https address")`. `online`: `https` and a host, else `ValueError("Online needs an https address")`. URLs carrying a username, password, query or fragment are refused for every engine.
  - `OmlxTagger(base_url, model, api_key=None, *, role="review", engine="mac")`: URL through `normalise_engine_url`; `self.engine`; the key falls back to `_local_api_key()` on `mac` only.
  - `_image_content` becomes an instance method. On `mac` unchanged. On `base`/`online`: `long_edge=0` is treated as `MODEL_IMAGE_LONG_EDGE`, and the image is always decoded and re-saved as JPEG (quality 90, `convert("RGB")`, no `exif=` argument), whatever its size or format.
  - `_complete` stays the only function that knows the wire format (the seam for a second format later; see the spec's "Room for other kinds of engine"). `chat_template_kwargs` in the payload on `mac` only. `temperature: 0` on `mac` and `base` only: at least one large online service answers 400 to any temperature below 1, so `online` omits the field. Error text prefix from `{"mac": "Local oMLX", "base": "Base", "online": "Online"}`. On an `HTTPError`, read at most 2000 bytes of the body; if it is JSON with `error.message` (or `error` as a string) append that reason clipped to 200 characters, otherwise append nothing beyond the status line. After a successful reply call `record_usage(self.engine, result.get("usage"))`.
  - `record_usage(engine: str, usage) -> None` and `usage_snapshot() -> dict`: a module-level, lock-guarded tally `{engine: {"prompt_tokens": int, "completion_tokens": int, "calls": int, "unreported": int}}` for engines that have made a call. A reply whose `usage` is not a dict with two non-negative ints counts in `unreported`, not as zero tokens. `reset_usage()` for tests.
  - `list_models(engine: str, url: str, api_key: str | None, timeout: float = 10) -> list[str]`: `GET <url>/v1/models`, no redirects, returns `data[].id` strings sorted, or `[]` when the service answers 404 (it is reachable but offers no listing); other failures raise `OmlxError` with the engine's label, same reason rule as `_complete`.
- `preferences.validate` for `base_url` / `online_url`: `""` is allowed (clears it); otherwise `omlx.normalise_engine_url(<engine>, value)` and store the normalised form.

- [ ] **Step 1: Write the failing tests** (follow how `tests/test_omlx.py` already fakes the opener):

```python
def test_url_rules_per_engine(self):
    self.assertEqual(normalise_engine_url("base", " http://pc.local:11434/v1/ "), "http://pc.local:11434/v1")   # Review Focus 2
    self.assertEqual(normalise_engine_url("base", "http://pc.local:11434"), "http://pc.local:11434/v1")
    self.assertEqual(normalise_engine_url("online", "https://api.example.com/v1beta/compat/"), "https://api.example.com/v1beta/compat")
    self.assertEqual(normalise_engine_url("mac", "http://127.0.0.1:8000/v1"), "http://127.0.0.1:8000")
    for engine, url in (("online", "http://api.example.com"), ("base", "ftp://pc"), ("mac", "http://192.168.1.5:8000"),
                        ("base", "http://user:pw@pc.local"), ("online", "https://api.example.com/v1?key=1")):
        with self.assertRaises(ValueError): normalise_engine_url(engine, url)

def test_remote_request_omits_template_kwargs_and_mac_keeps_them(self): ...
def test_online_omits_temperature_and_base_and_mac_send_zero(self): ...
def test_requests_go_to_the_api_base(self): ...   # online base "https://api.example.com/v1beta/compat" -> POST .../v1beta/compat/chat/completions; mac -> .../v1/chat/completions
def test_remote_never_reads_the_local_omlx_key(self): ...   # patch _local_api_key to return "local"; engine="base", api_key=None -> no Authorization header

def test_remote_image_is_always_reencoded_without_metadata(self):
    # a 640x480 JPEG saved with an EXIF block holding GPS (tag 0x8825) and a serial number; smaller than the long edge
    url = tagger_base._image_content(path)["image_url"]["url"]
    sent = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
    self.assertEqual(len(sent.getexif()), 0)
    self.assertEqual(tagger_mac._image_content(path)["image_url"]["url"].split(",", 1)[1],
                     base64.b64encode(path.read_bytes()).decode())           # mac unchanged

def test_remote_burst_frames_are_capped(self): ...   # 3000 px source, long_edge=0, engine online -> max(sent.size) == 1024; mac -> 3000

def test_remote_error_carries_the_services_reason_clipped(self):   # Review Focus 3
    # HTTP 401, body {"error": {"message": "Incorrect API key provided. " + "x" * 500}}
    with self.assertRaises(OmlxError) as caught: tagger_online.caption(path)
    message = str(caught.exception)
    self.assertTrue(message.startswith("Online request failed:"))
    self.assertIn("Incorrect API key provided.", message)
    self.assertLess(len(message), 320)
def test_remote_html_error_body_is_not_passed_on(self): ...   # 502 with "<html>…" -> message has no "<html"
def test_mac_error_text_is_unchanged(self): ...                # still "Local oMLX request failed: …"

def test_usage_is_tallied_and_missing_usage_is_unreported(self):
    # two online replies: one with usage {"prompt_tokens": 1200, "completion_tokens": 80}, one without usage
    self.assertEqual(usage_snapshot()["online"], {"prompt_tokens": 1200, "completion_tokens": 80, "calls": 2, "unreported": 1})

def test_list_models_reads_ids_and_refuses_redirects(self): ...   # {"data": [{"id": "b"}, {"id": "a"}]} -> ["a", "b"]
```

- [ ] **Step 2: Run**; expected: fail.
- [ ] **Step 3: Implement.** Keep `get_server_status` as it is (it stays the `mac` status call).
- [ ] **Step 4: Run** `tests.test_omlx`, `tests.test_preferences`; expected: pass, including every pre-existing test.
- [ ] **Step 5: Commit.**

### Task 5: Engine wiring in the library, diagnostics and server

**Files:**
- Modify: `src/lumen/omlx.py` (model cache), `src/lumen/library.py` (`tagger_for`), `src/lumen/diagnostics.py`, `src/lumen/web.py`, `src/lumen/cli.py` (`serve_settings`, `doctor` text output)
- Test: `tests/test_omlx.py`, `tests/test_library.py`, `tests/test_web.py`, `tests/test_cli.py`

**Interfaces:**
- Produces, in `omlx`: `held_models(engine: str) -> list[str] | None` (what the last successful listing returned; `None` when never listed or the last attempt failed), `refresh_models(engine, url, api_key, timeout=10) -> list[str]` (calls `list_models`, stores, returns; on `OmlxError` clears the held list and re-raises), `reset_models()` for tests.
- `Library.tagger_for(role)` on `base`/`online` (label `L`, model `m = settings.model_for(role)`), in this order, each a `RuntimeError`:
  1. no URL saved: `"<L> has no address: add one in Settings"`
  2. `online` with `keychain.get_key("online")` `None`: `"Online has no key: add one in Settings"`
  3. `held_models` is `None`: call `refresh_models`; an `OmlxError` becomes `"<L> did not answer: <reason>"`
  4. `m == ""`: `"choose a <role> model for <L> in Settings"`
  5. the held list is not empty and `m` is not in it: `"<role> model <m> is not offered by <L>"` (an empty list means the service has no listing; the typed name is used as it is)
  then `OmlxTagger(url, m, api_key=keychain.get_key(engine), role=role, engine=engine)`. On `mac` nothing changes. `library.py` calls `keychain.get_key` so tests patch `lumen.library.keychain`.
- `diagnostics.doctor_report` adds `"ai_engine"`, `"ai_engine_host"` (hostname and port of the engine's URL, `None` on `mac`), `"ai_engine_reachable"` (`None` on `mac`; else `held_models(engine) is not None`), `"ai_usage": usage_snapshot()`; `omlx_roles` on a remote engine is `{role: {"model": model_for(role), "loaded": model in (held_models(engine) or [])}}`. The report never calls Base or Online.
- `web.py`:
  - `/api/status` `review_models`: on `mac` the two configured names as today; on a remote engine `held_models(engine) or []`.
  - `POST /api/role-model`: on `mac` unchanged (session only). On a remote engine the model must be in the held list when that list is not empty, and it is saved as `<engine>_<role>_model` through `preferences.save`, then overlaid.
  - `POST /api/settings/test` body exactly `{"engine"}` (`base` or `online`): uses the saved URL and Keychain key, calls `refresh_models`, answers `{"ok": true, "models": [...]}` or `{"ok": false, "error": "<reason>"}` with status 200 either way.
  - `POST /api/settings` when the result changes `ai_engine`, or changes the URL of the current engine: for a remote target run checks 1 and 2 above, then `refresh_models`; any failure is a 400 and nothing is saved (the engine stays as it was). Clearing the URL of the current engine is refused: `"<L> is in use: switch engine first"`.
  - `make_server`, after `preferences.load`: when the loaded engine is remote, one `refresh_models` attempt in a daemon thread, failures swallowed (they show as not reachable).
- `cli.serve_settings` returns `settings` untouched when `settings.ai_engine != "mac"`. `lumen doctor` text output prints `AI engine: <label>` and, for a remote engine, its host.

- [ ] **Step 1: Write the failing tests.**

```python
# tests/test_library.py  (patch lumen.library.keychain, lumen.library.OmlxTagger; seed omlx.refresh via patch("lumen.omlx.list_models"))
def test_tagger_for_base_builds_a_base_tagger(self): ...     # OmlxTagger called with ("http://pc.local:11434", "small", api_key=None, role="fast", engine="base")
def test_tagger_for_online_without_a_key_says_so(self):      # Review Focus 1
    with self.assertRaisesRegex(RuntimeError, "Online has no key: add one in Settings"): library.tagger_for("review")
def test_tagger_for_unreachable_base_says_it_did_not_answer(self): ...   # list_models raises OmlxError("Base request failed: timed out") -> "Base did not answer: "
def test_tagger_for_names_a_missing_or_unoffered_model(self): ...        # "" -> "choose a fast model for Base in Settings"; "gone" -> "fast model gone is not offered by Base"
def test_tagger_for_mac_is_unchanged(self): ...                          # existing tests keep passing untouched

# tests/test_web.py
def test_switching_to_an_unreachable_base_is_refused_and_keeps_the_engine(self):   # Review Focus 5
    self.post("/api/settings", {"base_url": "http://pc.local:11434"})
    with patch("lumen.omlx.list_models", side_effect=OmlxError("Base request failed: timed out")):
        status, body = self.post("/api/settings", {"ai_engine": "base"})
    self.assertEqual(status, 400)
    self.assertEqual(self.get("/api/status")[1]["ai_engine"], "mac")
def test_switching_to_online_needs_a_url_and_a_key(self): ...
def test_status_never_calls_the_remote_engine(self):
    # engine base saved and reachable once; then patch lumen.omlx.list_models with side_effect=AssertionError
    status, body = self.get("/api/status"); self.assertEqual(status, 200)
    self.assertEqual((body["ai_engine"], body["ai_engine_host"]), ("base", "pc.local:11434"))
def test_started_server_with_unreachable_base_reports_it(self): ...   # Review Focus 5: ai_engine_reachable is False, status answers
def test_test_route_lists_models_or_reports_the_reason(self): ...
def test_role_model_on_a_remote_engine_is_saved(self): ...             # survives a new server on the same library
def test_a_service_without_a_model_listing_still_works(self): ...       # list_models -> []: switch succeeds, a typed model name builds a tagger
def test_status_and_settings_never_contain_the_key(self): ...          # key "sk-abc12345" in the fake keychain; not in either body

# tests/test_cli.py
def test_serve_settings_leaves_a_remote_engine_alone(self): ...
def test_doctor_names_the_engine(self): ...
```

- [ ] **Step 2: Run**; expected: fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `tests.test_omlx tests.test_library tests.test_web tests.test_cli tests.test_stale`; expected: pass.
- [ ] **Step 5: Commit.**

### Task 6: Batches ask first on a remote engine

**Files:**
- Modify: `src/lumen/library.py`, `src/lumen/web.py`, `src/lumen/cli.py`
- Test: `tests/test_library.py`, `tests/test_web.py`, `tests/test_cli.py`

**Interfaces:**
- Produces `Library.batch_frame_count(kind: str, **scope) -> int` for `kind` in `"tag"`, `"assist"`, `"cull"`, `"review_rejects"`, `"triage"`: the number of frames the matching `start_*_job` would send with the same arguments. It must reuse the selection each job already makes (extract it if it is inline); it must not be a second query that can drift.
- Web: `POST /api/photos/tag`, `/api/photos/tag-all`, `/api/photos/assist`, `/api/photos/cull`, `/api/photos/review-rejects`, `/api/groups/<id>/triage` accept an optional `confirm_remote` that must be a `bool`. On a remote engine without `confirm_remote: true` they answer **409** `{"confirm_remote": {"engine": "<name>", "label": "<Label>", "frames": <n>}}` and start nothing. On `mac` the field is accepted and ignored. `POST /api/groups/<id>/critique` and every single-frame route are untouched.
- CLI: `tag`, `assist`, `review-rejects`, `triage` gain `--yes`. On a remote engine without it they print `This sends <n> frames to <Label>. Run again with --yes to continue.` to stderr and return 2.

- [ ] **Step 1: Write the failing tests.**

```python
def test_remote_batch_without_confirmation_is_a_409_with_the_count(self):
    status, body = self.post("/api/photos/tag", {"scope": "keepers"})      # engine base, 3 keepers untagged
    self.assertEqual((status, body), (409, {"confirm_remote": {"engine": "base", "label": "Base", "frames": 3}}))
    self.assertEqual(self.get("/api/jobs")[1]["jobs"], [])                  # nothing started (use the existing jobs listing)
def test_remote_batch_with_confirmation_starts(self): ...                   # 202
def test_confirm_remote_must_be_a_bool(self): ...                           # "yes" -> 400
def test_mac_batches_need_no_confirmation(self): ...                        # existing behaviour, 202 without the field
def test_one_burst_critique_never_asks(self): ...                           # engine online: POST /api/groups/<id>/critique is not a 409
def test_every_batch_route_asks(self): ...                                  # subTest over the six routes
def test_batch_frame_count_matches_what_the_job_sends(self): ...            # count == number of tagger calls the tag job makes
def test_cli_batch_on_remote_engine_needs_yes(self): ...                    # returns 2, stderr "This sends 3 frames to Base."
```

- [ ] **Step 2: Run**; expected: fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `tests.test_library tests.test_web tests.test_cli`; expected: pass.
- [ ] **Step 5: Commit.**

### Task 7: Settings view, engine switch and marker in the page

**Files:**
- Modify: `src/lumen/static/index.html`, `src/lumen/static/app.js`, `src/lumen/static/styles.css`
- Test: `tests/test_web.py` (static markup assertions, in the style already used there for `index.html`)

**Interfaces:**
- Consumes: `GET/POST /api/settings`, `POST /api/settings/key`, `DELETE /api/settings/key/<engine>`, `POST /api/settings/test`, `POST /api/burst-gap`, `POST /api/role-model`, `/api/status` (`ai_engine`, `ai_engine_host`, `ai_engine_reachable`, `ai_usage`, `review_models`, `stale`, `version`, `library`, `geonames.dir`, `map.dir`, `burst_gap_seconds` from where the page already reads it), the 409 `confirm_remote` reply.
- Decisions:
  - **Entry:** an eighth tab in the existing Workflow tablist, `id="settings-tab"`, `aria-controls="settings-panel"`, text `Settings`, pushed to the right end with `margin-left: auto` and a gear glyph that is `aria-hidden`. It gets click, arrow keys and remembering from `wireTablist` with no new code.
  - **Sections:** inside `#settings-panel`, a second tablist `id="settings-sections"` with `aria-orientation="vertical"` and tabs `settings-ai-tab`, `settings-library-tab`, `settings-places-tab`, `settings-server-tab` (labels `AI engine`, `Library`, `Places and map`, `Server`), remembered under `lumen-settings-section`. Extend `wireTablist` so a tablist with `aria-orientation="vertical"` also maps `ArrowDown`/`ArrowUp` to next/previous.
  - **Row shape:** each setting is a `<div class="setting-row">` holding a `<label>`, the native control, `<p class="quiet">` help, and `<p class="applies">` reading one of `Applies now`, `Applies at the next regroup or import`. A shell-fixed setting's control is `disabled` and its help reads `Set by your shell (<VAR>)`.
  - **Save/Discard per section**, both disabled until a field differs from the loaded value; an unsaved section's tab gets `data-unsaved` and the text `(unsaved)` for screen readers. Save posts only the changed keys; success and refusal go through `announce()`, and a refusal's text is put beside the field named by its key when the message starts with one, else under the buttons.
  - **AI engine section:** radios `This Mac` / `Base` / `Online`; per remote engine a URL field, key status text (`saved` / `not set`), buttons `Add key` or `Replace` (reveals a `type="password"` field with `autocomplete="off"`; its value is posted to `/api/settings/key` and the field cleared at once, never stored in `state` or `localStorage`), `Remove`, and `Test` (shows `<n> models found`, `Connected; this service does not list its models, type their names`, or the reason). Under each URL field the help reads `The API address from your service's documentation, for example https://api.example.com/v1`. Per remote engine two text fields, `Fast model` and `Review model`, each with a native `<datalist>` filled from the last Test, so a name can be picked or typed. The fixed sentence `Previews and prompts leave this Mac on Base and Online. Originals never do.` Then `Tokens this session:` per engine that has calls, as `<prompt + completion> tokens`, with `(<n> calls not reported)` when `unreported > 0`.
  - **Library section:** library folder (text, read-only); burst gap (number, saved through `POST /api/burst-gap`, `Applies now: regroups every burst`); near-duplicate distance (number 0 to 64, `Applies at the next regroup or import`).
  - **Places and map section:** checkbox `Look up missing place names online` with help `Sends the frame's coordinates to the lookup service when the local index has no close place.`; GeoNames folder and map folder read-only.
  - **Server section:** address (`location.host`), version, and the `stale` reasons as a list, or `Up to date`. No buttons.
  - **Rail:** in `#role-model-pair`, above the Review model select, `<label for="ai-engine">AI engine</label><select id="ai-engine">` with the three engines; an engine lacking its URL (or Online its key) is a `disabled` option labelled `Base (set up in Settings)`. Changing it posts `{"ai_engine": …}`; on refusal it snaps back and announces the reason. `#role-model-pair` is shown whenever a remote engine is set up, not only when there are two models.
  - **Marker:** `#system-status` text starts with `AI: Base` or `AI: Online` (after any stale reasons, before the tools), with ` · not reachable` appended when `ai_engine_reachable` is false. Nothing is added on `This Mac`.
  - **Batch confirmation:** one new `<dialog id="remote-confirm">` built like `#digest-confirm`. Wrap the batch POSTs: on a 409 carrying `confirm_remote`, show `This sends <frames> frames to <label>. Continue?`; on confirm, resend with `confirm_remote: true`; on cancel, announce `Nothing was sent`. `request()` currently throws on non-2xx; let it expose the parsed body and status on the thrown error so the wrapper can read the 409.

- [ ] **Step 1: Write the failing markup tests** in `tests/test_web.py`: the served `/` contains `id="settings-tab"` with `aria-controls="settings-panel"` inside the Workflow tablist; `id="settings-sections"` with `aria-orientation="vertical"` and the four section tabs each with a matching `role="tabpanel"`; `id="ai-engine"`; `id="remote-confirm"`; the key field is `type="password"`; the fixed "Previews and prompts leave this Mac" sentence is present.
- [ ] **Step 2: Run**; expected: fail.
- [ ] **Step 3: Build** the markup, styles (existing tokens only; no new colours) and script to the decisions above.
- [ ] **Step 4: Run** `tests.test_web` and `node --check src/lumen/static/app.js`; expected: pass.
- [ ] **Step 5: Drive it for real.** Start `LUMEN_LIBRARY=<scratch> env -u LUMEN_OMLX_MODEL uv run lumen serve --port 8799` on a scratch library with the two `Examples` frames imported, plus a stub chat server on another loopback port (a 30-line `http.server` script in the scratchpad that answers `/v1/models` and `/v1/chat/completions` with a `usage` block). With the headless Chromium driver (stdlib CDP over `--remote-debugging-port`; no new dependency), screenshot and check: the Settings tab at the right end; each section; saving the Base URL as the stub's address, Test showing its models; switching to Base in the rail and the status line reading `AI: Base`; a tag batch raising the confirm dialog with the right count; the token line counting after it; stopping the stub and reloading showing `AI: Base · not reachable`; keyboard only: Tab to the tablist, arrows to Settings, arrows through sections. Fix what the screenshots show. Screenshots go in the scratchpad, not the repo.
- [ ] **Step 6: Commit.**

### Task 8: Contract, documentation and version

**Files:**
- Modify: `AGENTS.md`, `README.md`, `docs/GUIDE.md`, `CHANGELOG.md`, `src/lumen/__init__.py`, `pyproject.toml`, `src/lumen/static/index.html` (About text), `docs/HANDOFF.md`, `docs/superpowers/plans/` list in `AGENTS.md`

- [ ] **Step 1: Product contract** in `AGENTS.md`: replace "Do not add network services or cloud processing." with: "Advice runs on this Mac unless the photographer has switched the AI engine in Settings to Base or Online; then resized, metadata-free previews and prompts go to that engine and nothing else does. Do not add any other network service." Add a "Current tool state" bullet covering: `preferences` (saved settings in the catalog's `preferences` table as `setting.<key>`, shell variable wins), `keychain` (service `Lumen`, accounts `base`/`online`, key never on argv), the three engines and their URL rules, what the client changes per engine, `held_models`/`refresh_models` and that `/api/status` never calls a remote engine, the 409 `confirm_remote` batch rule and `--yes`, the routes, and that no test contacts a network or the Keychain. Add the spec and plan to the Plans list.
- [ ] **Step 2: About dialog and README.** The About text says "Nothing is sent anywhere; the local AI is a model running on this machine." Change it to say that nothing is sent anywhere unless the AI engine is switched in Settings. Fix the same claim wherever `README.md` and `docs/GUIDE.md` make it (`grep -n -i "nothing is sent\|entirely on this Mac\|fully local" README.md docs/GUIDE.md`), and add a "Settings and the AI engine" section to both: where Settings is, what each engine is, exactly what leaves the Mac (resized previews with no metadata; prompts, which can carry a place name, vocabulary, develop settings, a histogram summary, digest facts with file names, typed feedback) and what never does (originals, source paths, fingerprints, coordinates), that Online costs tokens and where the count shows, and that batches ask first.
- [ ] **Step 3: Version 0.6.0** in `src/lumen/__init__.py` and `pyproject.toml`; a `CHANGELOG.md` entry.
- [ ] **Step 4: `docs/HANDOFF.md`**: what shipped, and under open items: first real calls against a real Base and a real Online service are the photographer's (request details such as `temperature: 0` may need adapting once a real refusal is seen); Restart action and exports outside the library are in `docs/BACKLOG.md`. Add the Restart action to `docs/BACKLOG.md`.
- [ ] **Step 5: Run the full suite** and `node --check src/lumen/static/app.js`; expected: all pass. Check the diff against the working-style rule in `AGENTS.md`: no AI vendor or model-family name was added to a tracked file.
- [ ] **Step 6: Commit.**
