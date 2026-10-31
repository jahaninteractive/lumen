# Assessment and recommendations (2026-10-31)

A frank read of Lumen against the market and as a portfolio piece, with the
evidence it rests on and what to do next.

## Evidence

- Full suite: 719 tests pass warning-strict; `node --check` passes.
- `lumen doctor` finds every local tool, both native helpers, and 358k
  GeoNames places. oMLX was offline, so model features were not exercised.
- Live server: status route answers, UI serves, a cross-origin mutation is
  refused with 403. The security contract holds under a probe.
- 28 CLI subcommands, about 30k lines of Python/JS/CSS/tests, 195 commits.

## Product value

Competition: Lightroom, Capture One, Photo Mechanic, FastRawViewer,
darktable, digiKam, and AI cullers (Aftershoot, Narrative Select, Excire).

Lumen's one real differentiator: culling, tagging, captions and develop
advice run entirely offline on a Mac with nothing uploaded. The AI cullers
are cloud-dependent or subscription. That matters to travelling wildlife and
expedition shooters with no bandwidth and a privacy stance.

Hard limits: Sony ARW only, Apple Silicon only, terminal install (uv,
ExifTool, Xcode tools, a separately configured oMLX model), version 0.1.0,
one user, no screenshots, 16 GB machines untested. Most photographers will
not get past the install.

Realistic audience: dozens of technical photographers, not thousands. Expect
a handful of appreciative users, not stars or revenue.

## Portfolio value

This is where the repo earns its keep. A client reading it sees:

- A clear product contract and a security model that holds under a probe.
- Measured engineering decisions recorded in docs (sharpening at half scale,
  the detailAmount measurement) rather than guesses.
- A resumable handoff ledger, backlog, changelog, CI, license, contributing
  and security guides.
- A native Swift helper beside the Python, so it is not a single-language toy.
- Tests that pin real behaviour: version consistency, loopback-only serving.

## Recommendations, in order

1. Screenshots and a 60 to 90 second screen recording of a real cull in the
   README. Nobody installs to find out what a tool does. Biggest gap.
2. One or two sample ARW files as a release asset so the quick start works
   without a Sony camera.
3. A short "how it was built" section naming the measured trade-offs.
   Clients care about judgement more than code volume.
4. Confirm the 16 GB claim on a real machine or drop it to "untested".
5. Install: make `scripts/bootstrap.sh` fetch what it can. Done 2026-10-31
   (version 0.2.0); see below for what it does.

## Installer

Before 0.2.0, `bootstrap.sh` checked for uv, exiftool and swiftc, ran `uv
sync`, and compiled the two Swift helpers. It fetched nothing. The photographer must
separately: install Homebrew, `brew install uv exiftool`, run
`xcode-select --install`, download darktable and Siril disk images by hand
and drag them into `vendor/apps/`, download three GeoNames files, install
oMLX and load a model. Six manual steps before the first `lumen doctor`.

Automated in 0.2.0 inside the existing script (plus oMLX into
`/Applications` and both models by detected memory, which went beyond this
list):

- `brew install uv exiftool` when missing (Homebrew present on nearly every
  developer Mac; if absent, print the one-line Homebrew install and stop).
- `xcode-select --install` when `swiftc` is missing.
- GeoNames: `curl` the three files into `vendor/data/geonames/` (small,
  stable URLs, public domain).
- darktable and Siril: `curl` the Apple Silicon `.dmg`, `hdiutil attach`,
  copy the bundle into `vendor/apps/`, `hdiutil detach`. Pin the verified
  versions (5.6.2, 1.4.4) and check a SHA-256 so a changed download fails
  loudly instead of silently.

Keep it one script with `--check` and a `--no-download` flag; no Homebrew
formula, no `.pkg`, no app bundle. Still out of scope: oMLX and the models.
