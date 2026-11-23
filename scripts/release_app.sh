#!/bin/zsh
# Publishes the notarized dist/Lumen-<version>.dmg as GitHub release v<version>, with a copy
# named Lumen-latest.dmg so the website can link to
#   https://github.com/jahaninteractive/lumen/releases/latest/download/Lumen-latest.dmg
# Earlier releases keep their own files. Build first: scripts/build_app.sh --sign … --notarize ….
#
#   zsh scripts/release_app.sh             check everything and say what would be published
#   zsh scripts/release_app.sh --publish   create the release (or replace its files) with gh
set -euo pipefail

fail() { echo "release_app: $1" >&2; exit 1; }
cd "${0:A:h}/.."
publish=0
case "${1:-}" in
  "") ;;
  --publish) publish=1 ;;
  *) echo 'usage: zsh scripts/release_app.sh [--publish]' >&2; exit 2 ;;
esac

version="$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml)"
tag="v$version"
dmg="dist/Lumen-$version.dmg"
latest="dist/Lumen-latest.dmg"
notes="dist/release-notes-$version.md"

[[ -f "$dmg" ]] || fail "$dmg is missing; build it with scripts/build_app.sh --sign … --notarize …"
xcrun stapler validate -q "$dmg" || fail "$dmg is not notarized and stapled"
[[ -z "$(git status --porcelain --untracked-files=no)" ]] || fail "there are uncommitted changes"
[[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]] || fail "HEAD is not what origin/main has; push main first (and git fetch)"
# The disk image must be newer than the last commit that could change what is inside it.
built="$(stat -f %m "$dmg")"
changed="$(git log -1 --format=%ct -- src tools scripts/build_app.sh scripts/bootstrap.sh pyproject.toml uv.lock)"
(( built > changed )) || fail "$dmg is older than the last change to the app's sources; rebuild it"
awk -v v="$version" '/^## /{p = ($2 == v); next} p' CHANGELOG.md > "$notes"
[[ -s "$notes" ]] || fail "CHANGELOG.md has no section for $version"
cp "$dmg" "$latest"

echo "Release $tag: $dmg ($(stat -f %z "$dmg") bytes, SHA-256 $(shasum -a 256 "$dmg" | cut -d' ' -f1)) and $latest"
if (( ! publish )); then
  echo "Checks passed. Nothing published; run again with --publish."
  exit 0
fi
if gh release view "$tag" >/dev/null 2>&1; then
  gh release upload "$tag" "$dmg" "$latest" --clobber   # a rebuild of the same version replaces its files
else
  gh release create "$tag" "$dmg" "$latest" --target main --title "Lumen $version" --notes-file "$notes"
fi
gh release view "$tag" --json url --jq .url
