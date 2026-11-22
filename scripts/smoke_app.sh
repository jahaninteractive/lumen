#!/bin/zsh
# Smoke test for a built Lumen.app: the shell starts the bundled server, /api/status
# finds exiftool and both helpers, a killed shell takes the server with it, and
# nothing was written into the bundle. Uses a throwaway library and vendor folder.
#
#   zsh scripts/smoke_app.sh dist/Lumen.app
set -eu

app="${1:?usage: zsh scripts/smoke_app.sh <path to Lumen.app>}"
app="${app:A}"
fail() {
  echo "Lumen app smoke test failed: $*" >&2
  exit 1
}

work="$(mktemp -d -t lumen-smoke)"
shell_pid=""
cleanup() {
  [[ -n "$shell_pid" ]] && kill -9 "$shell_pid" 2>/dev/null
  rm -rf "$work"
}
trap cleanup EXIT

export LUMEN_APP_HEADLESS=1 LUMEN_LIBRARY="$work/library" LUMEN_VENDOR="$work/vendor"
"$app/Contents/MacOS/Lumen" > "$work/out" 2> "$work/err" &
shell_pid=$!

url=""
for _ in {1..600}; do
  url="$(sed -n 's/^Lumen is running at \(http:[^ ]*\)$/\1/p' "$work/out")"
  [[ -n "$url" ]] && break
  kill -0 "$shell_pid" 2>/dev/null || fail "the app exited before the server started: $(tail -n 20 "$work/err")"
  sleep 0.1
done
[[ -n "$url" ]] || fail "no 'Lumen is running at' line within 60 s"

curl -fsS --max-time 30 "$url/api/status" -o "$work/status.json" || fail "$url/api/status did not answer"
for key in tools.exiftool.available raw_developer.available vision_analyzer.available; do
  [[ "$(plutil -extract "$key" raw -o - "$work/status.json")" == true ]] || fail "$key is not true"
done
built="$(plutil -extract CFBundleShortVersionString raw -o - "$app/Contents/Info.plist")"
served="$(plutil -extract version raw -o - "$work/status.json")"
[[ "$served" == "$built" ]] || fail "server reports version $served, the app is $built"

kill -9 "$shell_pid"
shell_pid=""
gone=0
for _ in {1..100}; do
  curl -s --max-time 1 -o /dev/null "$url/api/status" || { gone=1; break; }
  sleep 0.1
done
(( gone )) || fail "the server still answers 10 s after the app was killed"

[[ -z "$(find "$app" -newer "$app/Contents/Info.plist" -name '*.pyc')" ]] || fail "bytecode was written inside the bundle"

echo "Lumen app smoke test passed."
