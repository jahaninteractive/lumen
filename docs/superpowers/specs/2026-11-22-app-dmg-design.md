# Lumen.app and its DMG: design

Date: 2026-11-22. Status: approved by the photographer, 2026-11-23.

## Intent

Photographers who are not at home in a terminal have asked for Lumen. They
cannot be expected to run a script, start a server or restart one. They get a
signed, notarized `Lumen.app` in a DMG: drag it to Applications, double-click,
and the workbench opens in their default browser. Quitting the app stops the
server. Optional tools and data are chosen from tick boxes in a window, not
from a shell.

The source checkout with `scripts/bootstrap.sh` stays, for people who want it.
The two routes must not drift apart.

## Goals

- A double-click on a Mac with nothing installed (no Homebrew, no uv, no Xcode
  tools) gives a working Lumen: import, cull, previews, develop, raw and camera
  JPEG export.
- One window offers the optional pieces (Places, Map, darktable, Siril) with
  their sizes; each can be added later.
- Quit stops the server. A crashed app leaves no server behind.
- One list of download URLs, hashes and install steps serves both the app and
  the shell route.
- The web server's contract is unchanged: loopback only, and it never fetches
  from the internet for tools or data.

## Non-goals

- Automatic updates. A new version is a new DMG dragged over the old app.
- Local AI in the app's setup (decided by the photographer, 2026-11-22). oMLX
  must be opened once before it has a model folder, its server is started by
  hand, and the models are 6 to 22 GB; none of that suits the people this is
  for. The setup window carries one line instead: AI advice comes from a
  service connected in Settings (Base or Online), or from this Mac by
  following the guide. The shell route keeps installing oMLX and the models
  exactly as today, and an app user who installs oMLX that way gets `This Mac`
  working with no further change.
- A release build in CI. The build runs on the photographer's Mac, where the
  signing certificate lives.
- Intel Macs, a Mac App Store build, a sandboxed build.
- The Homebrew tap in `docs/BACKLOG.md`. This replaces it as the next step.

## Anti-goals

- A second copy of the download list (in Swift, in Python or in a plist).
- A window that reimplements the workbench or any part of it.
- The app writing anywhere but the Lumen library,
  `~/Library/Application Support/Lumen/` and its own copy in `/Applications`.

## Approaches considered

1. **A small native Swift app that runs the server and the bootstrap script**
   (chosen). The app owns the Dock icon, the server process and the setup
   window. Downloads happen in the app shell, by running the same
   `bootstrap.sh` the shell route uses, so the web server stays offline.
2. **Setup inside the web page.** A Tools section in Settings with download
   buttons. No native window, but the server would fetch from the internet,
   which the contract forbids, and a native stub is still needed for the Dock
   and Quit.
3. **PyInstaller plus a `.pkg` installer.** Native tick boxes, but they are
   offered once at install, signing PyInstaller output is fiddly, and a
   download inside a pkg script reports progress and failure badly.

The photographer left the how to the builder and raised one requirement after
seeing these: the shell route stays, and the two must be kept in sync. Option 1
answers that by having one script, described next.

The photographer also asked whether the DMG should hold a separate installer
and uninstaller instead of the app. Weighed for a non-technical user and for
support over later releases, the single app won: Set Up and Uninstall are
always where the app is (the DMG is usually deleted), there is one program to
sign, notarize and test per release instead of three, an upgrade is a
replaced app rather than an installer coping with old versions and half
installs, and an in-app uninstall only has to know its own version. The
installer's one advantage, a guided first minute, is covered by the
move-to-Applications offer below.

## Keeping the two routes in sync

`scripts/bootstrap.sh` is the only place that knows what can be downloaded,
from where, with which hash, and how it is installed. The app ships a copy of
the script inside the bundle and runs it. The script gains:

- `--dest <dir>`: where `apps/`, `data/` and `downloads/` go. Default is the
  checkout's `vendor/`, so the shell route behaves as today.
- `--downloads-only`: skip steps 2 to 4 (Homebrew, command-line tools,
  `uv sync`, `swiftc`), which the app carries inside it, and steps 8 and 9
  (oMLX and the models), which the app does not offer. `--list` under this
  flag leaves those three components out.
- `--only <a,b,c>`: fetch just these components and take yes for each. Keys:
  `places`, `map`, `darktable`, `siril`, `omlx`, `fast-model`, `review-model`.
- `--list`: print one tab-separated line per component (key, label, size, what
  it is for, `ready` or `missing`, default on or off) and exit. No network.

The setup window builds its tick boxes from `--list` and runs
`--downloads-only --dest … --only …` with the ticked keys, showing the script's
output as its progress log and its `Skipped:` lines as what to retry. The app
therefore knows no component by name. Adding or updating a tool is an edit to
the script alone, as it is today.

Country codes for GeoNames go through the existing
`LUMEN_GEONAMES_COUNTRIES` variable from one text field in the window.

## What is inside the app

```
Lumen.app/Contents/
  MacOS/Lumen                 the Swift shell
  Helpers/vision_analyzer     prebuilt
  Helpers/raw_developer       prebuilt, -O
  Resources/python/           a relocatable CPython with Pillow and the lumen package
  Resources/bin/exiftool      plus its lib/ (a Perl script; macOS ships Perl)
  Resources/bootstrap.sh
  Resources/Lumen.icns        from static/mark.svg
```

Nothing optional is bundled: darktable and Siril are 535 MB together and are
other people's signed apps.

## The shell

About 200 lines of Swift (AppKit), compiled with `swiftc` like the helpers; no
Xcode project.

- **Launch:** start `python -m lumen serve` as a child, wait until
  `/api/status` answers, open the default browser at it. The first launch
  opens the setup window first; "Not now" is allowed and nothing is required.
- **Dock icon clicked while running:** open the browser at Lumen again.
- **Launched from the disk image** (or any read-only or translocated path):
  offer "Move to Applications", copy the app there, replacing an older copy
  that is not running, and relaunch from there. "Not now" runs it in place.
- **Menu:** Open Lumen, Set Up Lumen…, Uninstall Lumen…, Quit.
- **Uninstall Lumen…:** one window, three choices, then stop the server, do
  them, and move the app to the Trash.
  - Downloaded tools and data (`~/Library/Application Support/Lumen/`):
    removed, always. Size shown.
  - The Lumen library, as two tick boxes, both off by default, each with its
    size, worded so that keeping is the obvious choice (split asked for by
    the photographer, 2026-11-23):
    - Lumen's working data: the catalog (ratings, picks, tags, crops, develop
      settings), previews, sidecars and caches. Everything in the library
      except `outputs/`.
    - Exported files: `outputs/` (JPEGs, crops, astro results).
    Camera originals are never touched either way.
  - oMLX and any models are not Lumen's to remove and are left alone.
  - The saved Base and Online keys in the Keychain are removed (through the
    package's own `keychain.delete_key`, before the server stops).
  Everything removed goes to the Trash rather than being deleted, so a
  mistake is recoverable. Only Lumen writes to its Application Support
  folder, so that folder is removed whole.
- **Closing the browser tab does nothing.** The browser does not report it
  reliably. The app stays in the Dock, which is how the photographer gets back.
- **Port:** 8765. If something there already answers `/api/status` as Lumen
  (a `lumen serve` from a checkout, or a server from another copy of the
  app), the app starts no second server on the same library: it says Lumen is
  already running, offers to open it, and explains that Quit will not stop
  that one. If the port is held by something else, a free one is used.
  (Builder's call. A checkout server on another port is not detected; accepted
  and recorded in the backlog.)
- **No orphan server:** the shell holds the server's stdin pipe open. A new
  `serve --exit-with-parent` flag exits the server when that pipe closes, so a
  crashed or force-quit app takes the server with it.
- **Server will not start:** an alert with the last lines of its output.

## Changes to the Python package

Small, and each keeps the checkout behaving as it does now.

- `python -m lumen` works (`__main__.py`).
- `LUMEN_VENDOR`: the root that holds `apps/` and `data/`. Default is the
  checkout's `vendor/`. `config.DEFAULT_GEONAMES_DIR`, `DEFAULT_MAP_DIR` and
  `tools.VENDOR_APPS` derive from it. The app sets it to
  `~/Library/Application Support/Lumen`.
- `LUMEN_HELPER_DIR`: when set, `vision_analyzer_path` and
  `raw_developer_path` point there instead of `<library>/cache/bin/`. The app
  sets it to `Contents/Helpers`.
- `find_tool` gains a last fallback to `/Applications/darktable.app` and
  `/Applications/Siril.app`, with the same inside-the-bundle check, so someone
  who already has them need not download them again.
- exiftool needs no change: the shell puts `Resources/bin` first on the
  server's `PATH`.
- `diagnostics.stale_sources` must stay quiet when the Swift source is absent,
  as it is in the app.
- Messages that tell the photographer to run `scripts/bootstrap.sh` (missing
  helper, first-run card) are wrong inside the app. With `LUMEN_HELPER_DIR`
  set the helpers are always present, so these should not fire; the builder
  checks each such message and rewords any that can still show.

The library stays at `~/Pictures/Lumen Library`. `/api/status` and
`lumen doctor` keep reporting what they do now.

## Building, signing, notarizing

`scripts/build_app.sh` produces `dist/Lumen.app` and
`dist/Lumen-<version>.dmg` (the app and an Applications alias, via
`hdiutil create`).

- Bundle identifier `au.com.jahan-interactive.lumen` (decided by the
  photographer, 2026-11-22). It keys the privacy grants, so it never changes
  after the first release.
- Python comes from `uv python install` (relocatable builds) and the package
  from `uv pip install --target`. `uv` is a build tool only and is not in the
  app. Building needs those downloads once; ask the
  photographer before fetching, with sizes.
- exiftool is copied from the pinned Image-ExifTool distribution, hash pinned
  in the build script.
- Without `--sign` the build is unsigned and runs on the build Mac only. That
  is the development loop.
- `--sign "<Developer ID Application identity>"` signs every Mach-O inside
  (the Python binary, its `.so` and `.dylib` files, both helpers) and
  then the app, with the hardened runtime and a timestamp;
  `--notarize <keychain profile>` submits the DMG with `notarytool` and
  staples it.
- Expected to need no hardened-runtime entitlements, since everything inside
  carries the same team ID. The first signed build is the test of that; any
  entitlement added is recorded with its reason.

Not on this Mac yet: a "Developer ID Application" certificate (only two Apple
Development identities are present) and a `notarytool` keychain profile. The
photographer adds both; everything before signing proceeds without them.

## Privacy prompts

macOS will ask on first use for Pictures, removable volumes (memory cards) and
any Desktop or Documents folder the photographer imports from, naming Lumen.
The folder picker (`osascript`, `choose folder`) runs as a child of the app.
`Info.plist` carries a usage sentence for each. Verified on a clean user
account before release, since the build Mac has already granted them.

## Contract and docs

- `AGENTS.md` product contract: the web server still contacts nothing; the
  app's setup window runs the same pinned, user-chosen downloads as
  `scripts/bootstrap.sh`.
- `README.md` and `docs/GUIDE.md`: installing from the DMG comes first, the
  checkout second.
- `CHANGELOG.md`, version bump, `docs/HANDOFF.md`; `docs/BACKLOG.md` drops the
  Homebrew plan in favour of this.

## Checks

- `tests/test_scripts.py`: `--list` prints every component with no network;
  `--only` with an unknown key is refused; the default run still compiles both
  helpers and pins every download.
- `tests/test_config.py` / `test_tools.py`: `LUMEN_VENDOR`,
  `LUMEN_HELPER_DIR`, the `/Applications` fallback and its bundle check.
- `serve --exit-with-parent` exits when stdin closes.
- One smoke script against the built app: launch, `/api/status` answers with
  exiftool and both helpers available, kill the shell, the server is gone.
- By hand before release: a clean user account on a Mac without Homebrew or
  the command-line tools; Gatekeeper opens the stapled DMG's app offline; each
  tick box installs and `/api/status` then reports the tool.

## Left to the builder

The window's layout and wording, the icon rendering, how progress is shown
beyond the script's own lines, the exact flag parsing in the script, and
whether Quit can cheaply know a job is running.
