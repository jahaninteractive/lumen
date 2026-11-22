#!/bin/zsh
# Smoke test for a built Lumen.app: the shell starts the bundled server, /api/status
# finds exiftool and both helpers, a killed shell takes the server with it, and
# nothing was written into the bundle, a lumen package on PYTHONPATH is ignored, and the uninstall
# hook trashes what each choice says and refuses folders it must never touch.
# Uses throwaway libraries and vendor folders; the uninstall cases put them in the Trash.
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
# A lumen package on PYTHONPATH that cannot import: the bundled Python must ignore it (-I).
mkdir -p "$work/shadow/lumen"
echo 'raise RuntimeError("the shadowing lumen package was imported")' > "$work/shadow/lumen/__init__.py"
PYTHONPATH="$work/shadow" "$app/Contents/MacOS/Lumen" > "$work/out" 2> "$work/err" &
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

# Uninstall's trash steps, headless: each case on its own scratch vendor and library, named so
# that what lands in the Trash is recognisable. The hook refuses without both variables.
(
  unset LUMEN_LIBRARY
  mkdir -p "$work/refused/lumen-smoke-vendor"
  LUMEN_VENDOR="$work/refused/lumen-smoke-vendor" LUMEN_APP_UNINSTALL=keep "$app/Contents/MacOS/Lumen" 2>/dev/null \
    && fail "uninstall ran without LUMEN_LIBRARY"
  [[ -d "$work/refused/lumen-smoke-vendor" ]] || fail "a refused uninstall moved the vendor folder"
)
# Folders the guard refuses: an empty vendor (read as unset), a relative one, and a library holding the vendor.
mkdir -p "$work/guard/lumen-smoke-vendor" "$work/guard/lumen-smoke-library/lumen-smoke-vendor"
for vendor library in \
    "" "$work/guard/lumen-smoke-library" \
    lumen-smoke-vendor "$work/guard/lumen-smoke-library" \
    "$work/guard/lumen-smoke-library/lumen-smoke-vendor" "$work/guard/lumen-smoke-library"; do
  (cd "$work/guard" && LUMEN_VENDOR="$vendor" LUMEN_LIBRARY="$library" LUMEN_APP_UNINSTALL=all \
    "$app/Contents/MacOS/Lumen" 2>/dev/null) && fail "uninstall ran with LUMEN_VENDOR='$vendor' LUMEN_LIBRARY='$library'"
  [[ -d "$work/guard/lumen-smoke-vendor" && -d "$work/guard/lumen-smoke-library/lumen-smoke-vendor" ]] \
    || fail "a refused uninstall moved a folder (LUMEN_VENDOR='$vendor')"
done
typeset -A remains=(keep "./catalog.sqlite3 ./outputs/jpeg/a.jpg" data "./outputs/jpeg/a.jpg" exports "./catalog.sqlite3" all "")
for choice in keep data exports all; do
  vendor="$work/$choice/lumen-smoke-vendor" library="$work/$choice/lumen-smoke-library"
  mkdir -p "$vendor" "$library/outputs/jpeg"
  touch "$vendor/tool" "$library/catalog.sqlite3" "$library/outputs/jpeg/a.jpg"
  LUMEN_VENDOR="$vendor" LUMEN_LIBRARY="$library" LUMEN_APP_UNINSTALL=$choice "$app/Contents/MacOS/Lumen" \
    || fail "uninstall $choice exited $?"
  [[ ! -e "$vendor" ]] || fail "uninstall $choice left the vendor folder"
  if [[ $choice == all ]]; then
    [[ ! -e "$library" ]] || fail "uninstall all left the library folder"
  else
    left="$(cd "$library" && find . -type f | sort | tr '\n' ' ')"
    [[ "${left% }" == "${remains[$choice]}" ]] || fail "uninstall $choice left: $left"
  fi
done

echo "Lumen app smoke test passed."
