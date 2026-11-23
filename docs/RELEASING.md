# Releasing Lumen.app

How a new disk image is built, checked and published. Written so that someone
who was not there for the first release can do the next one. `AGENTS.md`
("Lumen.app") describes what the build contains; `docs/INSTALL-NOTES.md` is
the tester's view of the install.

## What is where

- `scripts/build_app.sh`: builds `dist/Lumen.app` and
  `dist/Lumen-<version>.dmg`; signs and notarizes when asked.
- `scripts/smoke_app.sh <app>`: launches the built app headless on scratch
  folders and checks it.
- `scripts/release_app.sh`: publishes the disk image as a GitHub release.
- `tools/lumen_app.swift`: the app shell (setup, uninstall, About windows).
- `scripts/bootstrap.sh`: the one list of optional downloads; the app runs a
  copy of it. A new darktable or Siril version is a new URL and hash here.
- The version is `__version__` in `src/lumen/__init__.py` and `version` in
  `pyproject.toml` (a test pins them together); it names the disk image and
  the release tag.

## Needs, on the build Mac

- Apple Silicon Mac with `uv`, `swiftc` (Xcode command-line tools) and `gh`
  signed in to GitHub.
- In the login Keychain: the certificate "Developer ID Application: Jahan
  Interactive Pty Ltd (37G62P2WM5)" (check with
  `security find-identity -v -p codesigning`) and the `notarytool` profile
  `lumen-notary` (made once with `xcrun notarytool store-credentials`; the
  Apple ID and its app-specific password are never written to a file).
  The certificate was issued in November 2026 and lasts five years. Only the
  account holder can create one, at developer.apple.com under Certificates.
- The build's downloads are cached after the first build (uv's Python 3.13,
  Pillow, the build backend, exiftool; about 33 MB). With the caches warm,
  `UV_OFFLINE=1` keeps the build off the network apart from signing
  timestamps and notarization. On a slow link, ask before a first build.

## Steps

1. **Version and changelog.** Bump both version strings, add the section to
   `CHANGELOG.md` (the release notes are taken from it), commit.
2. **Tests.**
   `env -u LUMEN_OMLX_MODEL uv run python -W error -m unittest discover -s tests`
   and `node --check src/lumen/static/app.js`.
3. **Build signed, and smoke-test.**
   ```sh
   UV_OFFLINE=1 zsh scripts/build_app.sh --sign "Developer ID Application: Jahan Interactive Pty Ltd (37G62P2WM5)"; echo "exit $?"
   zsh scripts/smoke_app.sh dist/Lumen.app
   ```
   Read the exit status itself; a pipe through `tail` hides a failed build.
4. **Look at the windows** if the shell changed. This renders them without a
   screen (light and `-dark`), using scratch folders only:
   ```sh
   LUMEN_APP_SNAPSHOT=<dir> LUMEN_VENDOR=<scratch> LUMEN_LIBRARY=<scratch> dist/Lumen.app/Contents/MacOS/Lumen
   ```
5. **Notarize.** The same build command with `--notarize lumen-notary` added.
   It uploads the disk image to Apple (a few minutes), staples it and checks
   it with `spctl`. Ask the photographer before uploading. Smoke-test again.
6. **Try it on a clean Mac** when the shell, the setup downloads or the
   bundle's contents changed: open the disk image, Move, set up, import,
   export, uninstall. `docs/HANDOFF.md` lists what has never been seen on a
   real screen.
7. **Push `main`**, then publish:
   ```sh
   zsh scripts/release_app.sh             # checks only
   zsh scripts/release_app.sh --publish   # with the photographer's go-ahead
   ```
   It refuses unless the disk image is stapled, newer than the last change to
   the app's sources, and `HEAD` is what `origin/main` has. It creates release
   `v<version>` on `jahaninteractive/lumen` with two files: the versioned
   `Lumen-<version>.dmg` and the same file as `Lumen-latest.dmg`. Run again
   for the same version, it replaces that release's files.
8. **Record it** in `docs/HANDOFF.md`: the notarization submission id, the
   disk image's size and SHA-256, the release URL.

## Links for the website

- Always the newest: 
  `https://github.com/jahaninteractive/lumen/releases/latest/download/Lumen-latest.dmg`
- One version: 
  `https://github.com/jahaninteractive/lumen/releases/download/v<version>/Lumen-<version>.dmg`

GitHub serves "latest" from the newest release that is not a draft or
pre-release, so every release must carry `Lumen-latest.dmg` or that link
breaks; the script always uploads both. Earlier releases and their files stay
until someone deletes them; GitHub sets no limit on how many are kept (each
file may be up to 2 GB).

## What a change needs

- **Python or page only:** steps 1 to 5, 7, 8. A clean-Mac run is optional.
- **The Swift shell or its windows:** all steps; look at the snapshots.
- **A new optional tool version:** edit `scripts/bootstrap.sh` (URL, SHA-256,
  size text), run `tests.test_scripts`, then all steps, installing that tool
  on the clean Mac.
- **A new exiftool or Python:** the pins are at the top of
  `scripts/build_app.sh` (exiftool's hash comes from exiftool.org's
  checksums); this needs downloads.
- **Never change** the bundle identifier `au.com.jahan-interactive.lumen`; it
  keys the privacy permissions people have granted.

## Known gaps

- No automatic updates: people download the new disk image and choose Move.
- The notarization ticket is stapled to the disk image, not to the app inside
  it, so a first launch with no internet is untested.
- Nothing builds in CI; the certificate lives on the build Mac.
