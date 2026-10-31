# Contributing to Lumen

Lumen is one photographer's culling tool, shared as is. Fixes and small,
well-tested improvements are welcome. Read `AGENTS.md` first: it is the
project contract (originals are never touched, the server stays loopback-only,
Pillow is the only runtime Python dependency) and applies to people as much as
to coding agents.

## Set up

You need an Apple Silicon Mac with `uv`, ExifTool and the Xcode command-line
tools (`brew install uv exiftool`, `xcode-select --install`). Then:

```sh
zsh scripts/bootstrap.sh
uv run lumen doctor
```

darktable, Siril, GeoNames and a running oMLX are optional for development.
No test needs them, a real `.ARW`, or a network connection; tests build their
own fixtures and patch every model call.

## Before you open a pull request

```sh
uv run python -W error -m unittest discover -s tests -v
node --check src/lumen/static/app.js
```

Both must pass warning-strict. CI runs the same two commands on a macOS
runner. The rules of the house:

- **Red-green.** A behaviour change starts with a failing test in `tests/`.
- **Small diffs that reuse what is there.** Look for an existing helper before
  writing one. No new runtime dependencies.
- **Privacy at the browser boundary.** Nothing served to the browser may carry
  source paths, fingerprints, API keys or raw model output. Mutation routes keep
  their host/origin check and strict JSON validation.
- **Accessibility is part of the design.** Keep native controls, keyboard tab
  behaviour and live announcements working; test at phone width too.
- **Advisory AI.** Model output suggests; the photographer decides. Crop,
  develop and pick proposals never apply themselves.
- Describe your tool by what it does, not what built it; keep vendor and model
  names of coding assistants out of files and commit messages.

Write commit messages as `type: what changed and why`, as in the existing
history. Update `docs/HANDOFF.md` only if you are changing something it
describes as current; `docs/BACKLOG.md` is for work you deliberately leave out.

## Reporting bugs

Open an issue with the `lumen doctor --json` output (it names no secrets) and
the steps that reproduce the problem. For anything security-related, see
`SECURITY.md`.
