# Settings page and AI engine: design

Date: 2026-11-21. Status: approved by the photographer 2026-11-21.

## Intent

Lumen is configured only through shell variables and its advice only runs on a model server on the same Mac. Both keep out anyone who is not the author. Two changes fix that:

1. A **Settings page** in the web app, so thresholds, addresses and keys are set by clicking, not by exporting variables.
2. An **AI engine** choice, so the advice can run on this Mac (today), on the photographer's own computer elsewhere on their network (**Base**), or on an online service (**Online**) for people who accept that trade.

A signed, double-click installer is the third project in this round and gets its own design after these two.

## Goals

- Every option that is a shell variable today can be seen on one page, and those that are safe to change can be changed there and survive a restart.
- One switch sends all advice to This Mac, Base or Online. Any server that speaks the standard chat completions API (`POST <base>/chat/completions`, optionally `GET <base>/models`) works with its API address, an optional key and a model name. That covers the local model servers people run at home and the chat-compatible endpoints the large online services publish (checked against three services' documentation on 2026-11-21: all accept a bearer key and base64 image parts, and their base paths differ).
- When advice leaves this Mac the page says so at all times, and a batch asks before it starts.
- Token use reported by the engine is counted and shown.

## Non-goals

- A different engine per model role (fast on this Mac, review elsewhere). The design leaves room; nothing is built.
- Integrations written for one named service. One generic form covers them; a named preset only fills in that form's address.
- A spend ledger across sessions, prices or cost estimates in money. Tokens for the running server only.
- Writing JPEG exports, or anything else derived, outside the library. Wanted (disk is short on the photographer's Mac) but taken out of this round on 2026-11-21: how exports are published needs its own investigation first. See `docs/BACKLOG.md`.
- Changing the library folder from the page.
- A Restart server action. The Server section is its place; it needs its own rules (refuse while an export or astro job runs) and is not built this round.
- An Astro section. The Astro tab already remembers its place choice; a section appears when there is a real option to hold.
- Reaching Base from outside the home network. Lumen is given a URL; a VPN is the photographer's business.
- Tuning local model speed. The October study is closed (`docs/model-speed/HANDOFF.md`).

## Anti-goals

- Advice going off this Mac without the photographer having chosen it, or without the page showing it.
- A key readable from the browser, the settings store, a log, `lumen doctor`, or a process listing.
- A remote engine slowing the page: `/api/status` is polled and must never wait on a network call to Base or Online.

## Constraints

- Pillow stays the only runtime Python dependency. Keys use the macOS Keychain through `/usr/bin/security`.
- The local HTTP server stays loopback-only; new mutation routes keep host/origin checks and strict JSON validation.
- Source paths, fingerprints, keys and raw model output never reach the browser.
- Camera originals are never edited, moved, renamed or deleted.
- No AI vendor or model-family names in tracked files beyond those already there, with one exception the photographer agreed on 2026-11-21: the list of services Lumen can connect to (the Settings presets, their one definition in code, the tests that pin it, and the matching table in the README and guide). The rule exists to keep the project agnostic about what built it; naming what it can talk to is a different thing. Model names are still never preset.
- No test contacts a network, the Keychain, or oMLX.
- The product contract in `AGENTS.md` changes in one place and must be reworded as part of this work:
  - "Do not add network services or cloud processing" becomes: advice runs on this Mac unless the photographer has switched the AI engine in Settings; nothing else is sent anywhere.

## Approaches

**AI engine**

1. *One engine switch* (chosen). This Mac, Base or Online; all advice goes to the chosen one. One thing to understand, one marker to show.
2. *Engine per role.* Tagging stays local and free while review goes elsewhere. More useful on a big cull, but two of everything and more ways to be confused about where a frame went. Can be added on top of 1.
3. *One integration per online service.* Rejected: the services share the chat completions API, so this is code for no gain.

**Where settings are kept** (builder's call, made here)

The catalog already has a `preferences` key/value table (`catalog.py`). Settings go there under `setting.<name>` keys; no new file. Keys go to the Keychain, never the table.

## Milestone 1: Settings

### Precedence

Shell variable, then saved setting, then default. A value fixed by a shell variable is shown read-only with "set by your shell (`LUMEN_…`)". This keeps the author's shell and the test suite working unchanged.

Saved settings apply to the command line as well as the web page: `lumen tag` uses the saved engine, and an import uses the saved near-duplicate distance. `cli.main` builds its settings before a catalog is open today, so the saved values are laid over them once it is.

### What the page holds

| Section | Setting | Editable | Notes |
|---|---|---|---|
| AI engine | engine, Base URL/key/models, Online URL/key/models, session tokens | yes | Milestone 2 |
| Library | library folder | no | shown only |
| Library | burst gap (seconds) | yes | already a saved preference (`burst_gap_seconds`, `POST /api/burst-gap`) that regroups at once; the page reuses that route and says so |
| Library | near-duplicate distance | yes | read only when bursts are regrouped, so the page says it applies at the next regroup or import; saving does not regroup |
| Places and map | look up missing place names online (`geocode_online`) | yes | off by default; label says coordinates are sent to the lookup service |
| Places and map | GeoNames folder, map folder | no | shown only |

### Routes

- `GET /api/settings`: each setting as `{key, value, source: "shell"|"saved"|"default", editable}`. Never a key; a key is reported only as `key_saved: true|false` per engine.
- `POST /api/settings` `{<key>: <value>, …}`: strict validation per key, unknown keys refused, a shell-fixed key refused. Applies to the running server and is saved. Follow how `POST /api/role-model` already swaps the frozen `Settings` in the running server.

### The page

Settings is a full view in the main area, not a dialog, so it has room for more settings and for actions later.

```
 Lumen  local RAW workbench  0.6.0        AI: Base · darktable ✓ · Siril ✓
┌──────────┬───────────────────────────────────────────────────────────────┐
│ AI engine│ Add photos  Review  Assist  Astro  Map  Export  Clear      ⚙  │
│ [Base ▾] ├───────────────────────────────────────────────────────────────┤
│ Review   │ Settings                                                      │
│ [model ▾]│ ┌────────────┐                                                │
│ Fast     │ │ AI engine ◂│  Where the advice runs                         │
│ [model ▾]│ │ Library    │  ( ) This Mac   (•) Base   ( ) Online          │
│          │ │ Places/map │                                                │
│ Batches  │ │ Server     │  Base    URL [http://192.168.1.20:11434]       │
│ …        │ │            │          Key  saved ✓  [Replace]   [Test]      │
│          │ └────────────┘          ✓ 6 models found                      │
│          │                Online   URL [                     ]           │
│          │                         Key  not set   [Add key]  [Test]      │
│          │                Previews and prompts leave this Mac on Base    │
│          │                and Online. Originals never do.                │
│          │                Tokens this session: 12,480                    │
│          │                                         [Save]  [Discard]     │
└──────────┴───────────────────────────────────────────────────────────────┘
```

- **Entry.** A gear button labelled "Settings" at the far right of the tab row, set apart from the workflow tabs because it is not a step in finishing a shoot. It opens the Settings view in place of the workflow panel; the header status line and the batch rail stay visible. Keyboard behaviour, focus on entry and the remembered-tab behaviour follow the existing tabs; whether the gear is a member of the tablist or a separate button that shows the view is the builder's call, as long as it reads correctly to a screen reader and the arrow-key tab behaviour is preserved.
- **Sections.** A list down the left of the view, one section shown at a time: AI engine, Library, Places and map, Server. A new area later (Astro, Export) is one more entry; nothing else moves.
- **One row per setting, always the same parts.** Name, native control, one plain sentence of help, and when a change applies ("now", "at the next regroup or import", "after a restart"). A shell-fixed setting is disabled with its reason.
- **Settings and actions look different.** Settings in a section are saved by that section's Save button and nothing changes until it is pressed; Discard puts the fields back. Leaving a section with unsaved changes keeps them and marks the section in the list. Actions (Test, Replace key, and later Restart) are secondary buttons that act at once and say in a line what they do.
- **Feedback.** A save or a refusal is announced through the live region, and a refusal's text sits beside the field it concerns.
- **Server section.** Read-only this round: the address the server listens on, the version, and the existing `stale` reasons from `/api/status`, which today appear only in the status line. This is where a Restart action goes when it is built.
- **Quick switch in the rail.** An AI engine select sits above the existing Review model and Fast model selects. An engine that has no URL (or, for Online, no key) is disabled there with "Set up in Settings". Choosing one switches and saves at once, the same as choosing it in Settings and pressing Save.

## Milestone 2: AI engine

### Engines

| Engine | URL rule | Key | Model list | Model "ready" means |
|---|---|---|---|---|
| This Mac | loopback only, as today (`LUMEN_OMLX_URL`) | oMLX key, as today | oMLX `/api/status` `loaded_models` | loaded, as today |
| Base | any `http` or `https` URL | optional | `GET <base>/models` | listed, or typed when the server has no listing |
| Online | `https` only | required | `GET <base>/models` | listed, or typed when the service has no listing |

The saved address is the service's API base as its documentation gives it, path included (services differ: `/v1`, `/v1beta/compat`, `/compatible-mode/v1`). A bare host gets `/v1`. Lumen appends `/chat/completions` and `/models` and never rewrites the path.

Plain `http` is allowed for Base because home model servers serve it; it is refused for Online because the key and the photos would cross the internet in the clear. Redirects stay refused for every engine.

The loopback rule lives in two places today, `OmlxTagger.__init__` and `get_server_status`, and `Library.tagger_for` calls the second on every request; those are the points where the engine's own rule takes over.

Each remote engine stores its own URL and its own fast and review model names, so switching back and forth loses nothing. The existing Fast model and Review model selects in the rail offer the two configured names on This Mac, as today, and the engine's listed models on Base and Online. `cli.serve_settings` (start review on whichever model is loaded) applies to This Mac only.

The engine choice is saved and survives a restart. It is off (This Mac) until the photographer changes it.

### Service presets

The Online form has a Service select: Anthropic, Google, OpenAI, and Other, matching `preferences.SERVICE_PRESETS`. Qwen (Alibaba Cloud) is not a preset because its address is per workspace; the photographer enters it through Other. Choosing a service fills the address field with that service's documented chat-compatible API base; Other leaves it for typing. The address stays an ordinary editable field and nothing else about the engine depends on the choice, which is not saved. The Base form has no select, because the host is the photographer's own machine: a hint line gives the usual addresses (`http://<your PC>:11434/v1` for Ollama, `http://<your PC>:1234/v1` for LM Studio). The list has one definition, served to the page with the settings. Each address is checked against the service's own documentation when it is added; a service whose address cannot be confirmed is left out.

### Keys

Stored as Keychain generic passwords, service `Lumen`, account `base` or `online`, written and read through `/usr/bin/security`. The key must not appear on a command line (use `security -i` and feed the command on stdin, or an equivalent the builder verifies). Routes:

- `POST /api/settings/key` `{engine, key}`: write only.
- `DELETE /api/settings/key/<engine>`.

The page shows "saved" or "not set" and a Replace button. Tests inject the `security` runner.

### Requests

One client keeps doing the work (`OmlxTagger._complete`); the engine decides the URL, key and two details:

- `chat_template_kwargs` is sent to This Mac only; a strict service may refuse an unknown field.
- `temperature: 0` is sent to This Mac and Base only. One large online service's documentation says any temperature below 1 is a 400 on its current models, so Online omits it and takes the service's default.
- Burst critique sends full-size frames today (`long_edge=0`). On Base and Online it is capped at `MODEL_IMAGE_LONG_EDGE`.

Known risk: some services want a differently named token-limit field on some models, and one service describes its chat-compatible endpoint as meant for testing and comparison, with its native API the supported path. The build is tested against fakes of the documented API only; the builder sends nothing to a real Base or Online service. A refusal must reach the photographer as a readable message naming the engine and carrying the service's own reason. The photographer makes the first real calls with their own server and key, and any request adaptation is a follow-up once a real refusal has been seen.

Error text names the engine in use ("Base request failed: …") in place of "Local oMLX".

### What leaves the Mac, and what never does

Sent to the chosen engine: resized previews (long edge 1024), the prompts (which can carry a place name, the photographer's vocabulary, develop settings and a histogram summary, and the photographer's typed feedback), the text typed into Expand search, the digest's facts (the day's date, its counts, keywords, place names, and the top frames' file names, scores and bursts), and a stack review's debrief text with the stacked preview. A bare file name is not a source path and stays in the digest prompt.

Never sent: camera originals, source paths, fingerprints, coordinates, or image metadata. Today `_image_content` re-encodes only an image larger than the long edge; a smaller preview, and anything at `long_edge=0`, goes out as the file's own bytes with its EXIF. On Base and Online every image is re-encoded without metadata, and a test pins that the bytes carry no EXIF (so no GPS and no serial number).

### Showing it

- `#system-status` leads with "AI: Base" or "AI: Online" whenever the engine is not This Mac, and the rail's AI engine select shows the same.
- A **Test** button per engine (`POST /api/settings/test` `{engine}`) answers `{ok, models}` or `{ok: false, error}`, and is the only thing that fills the model selects for that engine besides a switch to it.
- The remote model list is fetched on Test and on switching engine, then held in the server. `/api/status` reports from that held list and never calls Base or Online itself.

### Tokens

Each reply's `usage` (prompt and completion tokens) is added to an in-process counter per engine, reset when the server starts, reported on `/api/status` as `ai_usage` and shown in Settings and beside the engine marker. A reply without `usage` counts as unknown, shown as "not reported", not as zero.

### Batches

A batch is a request that makes more than one model call: `POST /api/photos/tag` (and `tag-all`), `/api/photos/assist`, `/api/photos/cull`, `/api/photos/review-rejects`, `/api/groups/<id>/triage`, and the CLI's `tag`, `assist`, `review-rejects` and `triage`. Critiquing one burst (`POST /api/groups/<id>/critique`, `lumen critique`) is one call the photographer asked for on one burst and is not a batch. When the engine is not This Mac:

- The browser asks first, using the existing confirm-dialog pattern: "This sends 840 frames to Online. Continue?"
- The server enforces it: the batch route refuses without `confirm_remote: true` in the body.
- The CLI equivalents print the engine and the count and refuse without `--yes`.

Single-frame advice (a caption, a develop suggestion, a crop review) runs without asking; the marker is the notice.

### Diagnostics

`lumen doctor` and `/api/status` report `ai_engine`, the engine's URL host (not the key), and the per-role model state for the current engine in the existing `omlx_roles` shape.

## Room for other kinds of engine

Not built this round; written down so the build does not close the door.

- **A service's native API.** `OmlxTagger._complete` is the only function that knows the wire format: it takes content parts, a system prompt and a token limit, and returns text. A second format (different path, key header, image-part shape and usage field names) is a second branch there, chosen by a new saved setting per engine. Nothing above `_complete` changes. Trigger: a service whose chat-compatible endpoint proves unreliable or is withdrawn.
- **Judgment classifiers.** A newer kind of model answers typed questions (yes/no with a probability, one of a set, a score) about supplied state and images, and writes no prose. That fits the fast role's work, which is classification already (reject problems from a fixed list, sky-frame triage classes, vocabulary tags), and does not fit the review role (captions, rationales, digest). It would be a second tagger class with the same method names as `OmlxTagger` for the fast-role methods, returned by `Library.tagger_for("fast")`. It needs the engine-per-role non-goal built first, since review would stay on a chat model. The one such API read on 2026-11-21 takes images only behind an explicit opt-in header and reports usage as `input_tokens`/`output_tokens`.
- **What the build must keep true for this:** all advice goes through `Library.tagger_for(role)`; callers use only the tagger's task methods and never its URL or payload; `record_usage` takes token counts, not a reply in one service's shape.

## Left to the builder

- Internal names, how the frozen `Settings` grows, and whether the client class is renamed.
- Timeouts for Base and Online (the photographer's link is slow and drops; a remote call should fail readably, not hang the panel).
- Exact wording of labels and refusals, in the voice of the existing page.
- Styling of the Settings view within the charcoal/amber/cyan workbench, and its layout at narrow widths.

## Housekeeping

Red-green tests, warning-strict. Version 0.6.0 in `src/lumen/__init__.py` and `pyproject.toml`, a `CHANGELOG.md` line, `README.md` and `docs/GUIDE.md` sections for Settings and the AI engine (including the plain statement of what leaves the Mac), the two contract lines in `AGENTS.md`, and `docs/HANDOFF.md` after each milestone.
