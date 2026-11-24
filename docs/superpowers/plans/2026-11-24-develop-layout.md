# Develop Tab Layout Implementation Plan

> **For agentic workers:** one lead and one implementer per task. The lead owns this plan, hands out one task
> at a time, reviews every diff and runs the gates. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** not started. Written 2026-11-24 for a fresh session. The photographer has not yet chosen between
the options; the plan below is the recommended path (A, then B). Ask before Task 1 if that is still the choice.

**Goal:** The Develop tab's right column stops scrolling on a landscape frame, by moving the picture tools
(Straighten) and then the suggestion ("Where to start") under the photo. No control changes what it does.

**Spec:** `docs/superpowers/specs/2026-11-24-develop-layout-design.md`

**Tech Stack:** `src/lumen/static/index.html`, `app.js`, `styles.css`, `tests/test_web.py`. No server change.

## Global Constraints

- Read `AGENTS.md` and `docs/HANDOFF.md` first. Work in a worktree on a branch off `main`
  (`feature/develop-layout`). Nothing merges until the photographer has looked.
- No server, helper or catalogue change. If a task seems to need one, stop and report.
- Every control keeps its id, label, behaviour, saved value and announcements. Only position, grouping and
  the CSS that lays them out change.
- Native controls, visible focus, keyboard reachability and live announcements are preserved. No second
  live region.
- No downloads (satellite link): prefix uv with `UV_OFFLINE=1`; copy `.venv` from the main checkout into the
  worktree rather than syncing.
- No AI vendor or model-family names in tracked files or commit messages; no trailers. One plain sentence
  per commit.
- Suite: `env -u LUMEN_OMLX_MODEL UV_OFFLINE=1 uv run python -W error -m unittest discover -s tests`
  (1113 tests on 2026-11-24; the count may only go up), and `node --check src/lumen/static/app.js`.
- One commit per task. A task changes only the files it names.

## How the lead works

1. **Baseline.** Suite and `node --check` green on `main`; record the count. Take "before" screenshots of
   the Develop tab (see Screenshots) and keep them outside the repo.
2. **Per task.** Hand the implementer the task text, these constraints and the spec. Review the whole diff,
   run the suite, and read the "after" screenshots yourself before telling the photographer anything.
3. **The JS harness is not a browser.** Its DOM stub does not lay anything out, and until 0.10.0 it did not
   even detach nodes: Draw horizon passed every test and did nothing in the real page. Every task here ends
   with a real page driven headlessly, not only with harness tests.
4. **Pause for the photographer after Task 1 and after Task 2.** Layout is theirs to judge.

## Screenshots

Headless Chromium is in `~/Library/Caches/ms-playwright/chromium_headless_shell-*/`; drive it over
`--remote-debugging-port` with a small stdlib websocket client (no new dependency; about 50 lines). Serve the
worktree on a scratch library (`LUMEN_LIBRARY=<scratch> LUMEN_HELPER_DIR=<dir with both helpers built from
the worktree> uv run lumen serve --port 8766`), never the photographer's library or port 8765. Import one
landscape and one portrait raw from `Examples/`. Shots needed, before and after: 1456x900 and 900x1000, each
with a landscape and a portrait frame, Develop tab, with a suggestion showing if a model is available and
with Straighten in use (grid visible).

## Review Focus (what must not break)

1. **Nothing changes meaning.** Saving, Discard, Reset, Reset all, Apply to every frame, Auto (including its
   on-demand measure), Draw horizon, the grid, parking a suggestion, and every announcement behave exactly as
   before. The burst copy still leaves each frame's angle alone.
2. **The overlay still works.** `#straighten-overlay` and `#crop-overlay` stay children of `#loupe-viewport`
   and survive a preview load (the 0.10.0 bug: `showPreview` replaces the viewport's children).
3. **Other tabs are untouched.** Nothing new shows under the photo on Frame, Crop or EXIF; the crop tab's
   pointer handling is unchanged.
4. **Keyboard order reads in visual order**, and focus is never trapped or lost when a tab is switched.
5. **Narrow and portrait.** Below 980 px it is one column in a sensible order; a portrait frame does not
   push the band off the dialog or overlap it.
6. **Pinned tests change only on purpose.** `test_develop_controls_match_the_server_ranges` pins that each
   range's slider and number are inside `#develop-panel` and pins the heading order. Prefer a layout that
   keeps both true (see Task 1's first step); if one must change, the test says why.

---

### Task 1: Straighten under the photo (option A)

**Files:** `src/lumen/static/index.html`, `src/lumen/static/styles.css`, `src/lumen/static/app.js` only if
the details-opening code no longer applies, `tests/test_web.py`.

- [ ] First decide how, and write the decision in the report: (a) keep the Straighten block inside
      `#develop-panel` in the DOM and place it under the photo with CSS grid on the frame view's layout, or
      (b) move it in the DOM into a band under `#loupe-viewport`. Prefer (a) if the dialog's grid allows it:
      the pinned tests and the tab panel's accessibility tree stay as they are. If (a) makes keyboard order
      disagree with visual order, use (b) and change the pinned test with the reason.
- [ ] The strip: slider, number, Auto, Draw horizon, Reset on one row where width allows, megapixels left
      and the hint line beneath, "Set per frame; not copied to the burst." kept. It is not collapsible any
      more (remove the `<details>` and the code that opens it; keep every control id).
- [ ] Visible only while the Develop tab is selected; hidden with the rest of the workspace when Develop is
      unavailable (non-raw, offline).
- [ ] Tests: reading order; hidden on the other tabs; the existing `develop-straighten` harness scenario
      passes; screenshots as listed.

**Must not change:** anything in Review Focus 1 to 3. **Pause for the photographer.**

### Task 2: The suggestion under the photo (option B)

**Files:** as Task 1.

- [ ] Move "Where to start" (status line, rationale, "What to change", Suggest settings, Discard the
      suggestion, Park the handles here, the per-panel task line) into the band, after the Straighten
      strip. Rationale text is set at a comfortable reading width (no wider than about 75 characters).
- [ ] The band scrolls with the dialog, not inside itself.
- [ ] Tests: reading order; the `develop-advice` style harness scenarios pass unchanged; screenshots with a
      long rationale (600 characters, the cap) at both widths.

**Must not change:** the suggestion's wording, routes or what is sent. **Pause for the photographer**: this
is the step they may not keep (the rationale sits away from the histogram).

### Task 3: Guide, changelog, handoff

**Files:** `docs/GUIDE.md`, `CHANGELOG.md`, `docs/HANDOFF.md`, `AGENTS.md` (the Straighten bullet's
position sentence), `src/lumen/__init__.py` and `pyproject.toml` and `uv.lock` (patch version), and the
guide's Develop still in `docs/images/` if one is retaken.

- [ ] Say where Straighten and the suggestion now sit. Retake the Develop still only if the photographer
      asks; note it as out of date otherwise.

---

## Manual checks (photographer)

- A landscape and a portrait frame at their usual window size: nothing overlaps, nothing important needs
  scrolling.
- Draw horizon with a real mouse, at fit and at Full size.
- Tab through the Develop tab once with the keyboard.
- Safari, if used.

## Rollback

All on `feature/develop-layout` until merged. The change is markup and CSS; reverting the commits restores
the 0.10.0 layout with no data to migrate.
