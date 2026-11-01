#!/bin/zsh
# Gets a fresh clone to a working `lumen doctor`: checks the platform, installs
# uv and exiftool with Homebrew, syncs Python, compiles the native helpers, and
# fetches GeoNames, the Natural Earth map, darktable, Siril, oMLX and the two
# models. Anything already present is reported as ready and skipped, so
# re-running is safe. Park, road and water detail for one area is not fetched
# here: run `uv run lumen map-region` for that.
#
#   zsh scripts/bootstrap.sh                 ask before each optional download
#   zsh scripts/bootstrap.sh --yes           accept every default (also when stdin is not a terminal)
#   zsh scripts/bootstrap.sh --no-download   check, sync and compile only; fetch nothing
#   zsh scripts/bootstrap.sh --check         report uv, exiftool and swiftc, change nothing
set -eu

project_root="$(cd "$(dirname "$0")/.." && pwd)"

check_command() {
  if command -v "$1" >/dev/null 2>&1; then
    echo "$1: ready"
  else
    echo "$1: missing" >&2
    return 1
  fi
}

if [[ "${1:-}" == "--check" ]]; then
  missing=0
  check_command uv || missing=1
  check_command exiftool || missing=1
  check_command swiftc || missing=1
  exit "$missing"
fi

no_download=0
assume_yes=0
for arg in "$@"; do
  case "$arg" in
    --no-download) no_download=1 ;;
    --yes) assume_yes=1 ;;
    *) echo "usage: zsh scripts/bootstrap.sh [--yes] [--no-download] | --check" >&2; exit 2 ;;
  esac
done
[[ -t 0 ]] || assume_yes=1

# Pinned downloads. A new version means a new URL and a new hash here.
darktable_url="https://github.com/darktable-org/darktable/releases/download/release-5.6.2/darktable-5.6.2-arm64.dmg"
darktable_sha256="6ff88e58a2a59cb07b0a1502fea7205e68cd783380a33a3ce7bda68ec29def0c"
siril_url="https://free-astro.org/download/siril-1.4.4-arm64-3.dmg"
siril_sha256="9ac8a10566a209baebc4f28ac6c9cbd5e2c7c27f030e66812c2f32d7b6239507"
omlx_release="https://github.com/jundot/omlx/releases/download/v0.7.2"
omlx_sequoia_sha256="86e50d13ecbdf8531a5e39684619b0b0a609ec06e1c1fab866daa74e0e47b32e"
omlx_tahoe_sha256="0ceddaf7a8901d440bc88acb3e033b294ad4d1b45b57d02fb73833c839d270b9"
geonames_url="https://download.geonames.org/export/dump/"
natural_earth_url="https://raw.githubusercontent.com/nvkelso/natural-earth-vector/ca96624a56bd078437bca8184e78163e5039ad19/geojson/"
natural_earth_layers=(
  ne_10m_coastline.geojson 6f75ae0e0de157b14946e2255eb1f5486d9a13819032e26d4610852d296788f6
  ne_10m_admin_0_boundary_lines_land.geojson 74d9c16229c095fde65943a9919e337682f044bcebccb120764f38edf3b70f4a
  ne_10m_rivers_lake_centerlines.geojson bb854a900ecbd3b408df46d5e16e3e0f974ba55993f9d8b5c26e855273c0905a
  ne_10m_lakes.geojson 2d036f53dedec578001c5c30c2959ee7d4eebc1306900fa4367c49929ec8f2d9
  ne_10m_roads.geojson 66a0c7b438e92fd124822cc5921cfa11042f48c294ade5e0f03f2c6640fd0248
  ne_10m_populated_places_simple.geojson fd3fa867a320cbd5c5b6bb5bc550afeec2939fb2cef688e508007282a55ac42f
)
fast_model="mlx-community/Qwen3-VL-8B-Instruct-4bit"
review_model="mlx-community/Qwen3.8-27B-4bit"

skipped=()

# ask "question" Y|N: succeeds for yes; Enter, --yes and a non-terminal stdin take the default.
ask() {
  local reply=""
  if (( assume_yes )); then
    echo "$1 $2"
  else
    read -r "reply?$1 " || true
  fi
  [[ "${reply:-$2}" == [Yy]* ]]
}

fetch() {
  [[ -f "$2" ]] && return 0
  echo "Downloading $1"
  # --retry-all-errors and -C -: a reset connection resumes the .part file instead of failing the run.
  curl -fL --retry 3 --retry-all-errors -C - -o "$2.part" "$1" && mv "$2.part" "$2"
}

verify_sha256() {
  local got
  got="$(shasum -a 256 "$1" | cut -d ' ' -f 1)"
  if [[ "$got" != "$2" ]]; then
    echo "${1:t}: sha256 mismatch" >&2
    echo "  expected $2" >&2
    echo "  got      $got" >&2
    echo "Delete $1 and re-run, or check the release page for a changed file." >&2
    exit 1
  fi
}

mount_point=""
detach_dmg() {
  if [[ -n "$mount_point" ]]; then
    hdiutil detach "$mount_point" -quiet || true
    rmdir "$mount_point" 2>/dev/null || true
    mount_point=""
  fi
}
trap detach_dmg EXIT

# install_app name url sha256 bundle dest_dir: download, verify, mount, copy, unmount.
install_app() {
  local dmg="$project_root/vendor/downloads/${2:t}"
  mkdir -p "${dmg:h}" "$5"
  if ! fetch "$2" "$dmg"; then
    echo "$1: download failed" >&2
    skipped+=("$1: download failed from $2; re-run scripts/bootstrap.sh")
    return 0
  fi
  verify_sha256 "$dmg" "$3"
  mount_point="$(mktemp -d -t lumen-dmg)"
  hdiutil attach -nobrowse -readonly -mountpoint "$mount_point" "$dmg" >/dev/null
  rm -rf "$5/$4.part"
  cp -R "$mount_point/$4" "$5/$4.part"
  mv "$5/$4.part" "$5/$4"
  detach_dmg
  echo "$1: installed $5/$4"
}

# unzip_member zip member dest_dir: extract through a .part file so an interrupted run leaves no half file.
unzip_member() {
  [[ -f "$3/$2" ]] && return 0
  unzip -p "$1" "$2" > "$3/$2.part" && mv "$3/$2.part" "$3/$2"
}

# 1. Platform
if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  echo "Lumen's supported target is Apple Silicon macOS." >&2
  exit 1
fi
macos_version="$(sw_vers -productVersion)"
autoload -Uz is-at-least
if ! is-at-least 13.5 "$macos_version"; then
  echo "Lumen needs macOS 13.5 or newer; this Mac runs $macos_version." >&2
  exit 1
fi
macos_major="${macos_version%%.*}"

# 2. Homebrew tools
if (( ! no_download )); then
  brew_missing=()
  for tool in uv exiftool; do
    command -v "$tool" >/dev/null 2>&1 || brew_missing+=("$tool")
  done
  if (( ${#brew_missing} )); then
    # ponytail: Homebrew is only demanded when uv or exiftool is missing; a Mac with both needs no brew.
    if ! command -v brew >/dev/null 2>&1; then
      echo "Homebrew is needed to install ${brew_missing[*]}. Install it with:" >&2
      echo '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"' >&2
      echo "then re-run zsh scripts/bootstrap.sh." >&2
      exit 1
    fi
    brew install "${brew_missing[@]}"
  fi
fi
check_command uv
check_command exiftool

# 3. Xcode command-line tools. /usr/bin/swiftc is a shim that exists on every Mac
# and pops the installer dialog when run, so the tools are detected by their path instead.
if ! xcode-select -p >/dev/null 2>&1 && (( ! no_download )); then
  xcode-select --install || true
  echo "Installing the Apple command-line developer tools. Re-run zsh scripts/bootstrap.sh when that dialog finishes." >&2
  exit 1
fi
check_command swiftc

# 4. Python environment and native helpers
uv sync --project "$project_root"

library_root="${LUMEN_LIBRARY:-$HOME/Pictures/Lumen Library}"
native_bin="$library_root/cache/bin"
module_cache="$library_root/cache/swift-module-cache"
mkdir -p "$native_bin" "$module_cache"
CLANG_MODULE_CACHE_PATH="$module_cache" swiftc \
  "$project_root/tools/vision_analyzer.swift" \
  -o "$native_bin/vision_analyzer"
# -O here but not above: the developer renders while the photographer waits on a slider,
# so its speed is on the interactive path; face analysis runs in the background.
CLANG_MODULE_CACHE_PATH="$module_cache" swiftc -O \
  "$project_root/tools/raw_developer.swift" \
  -o "$native_bin/raw_developer"

if (( no_download )); then
  skipped+=("Downloads (--no-download): GeoNames, Natural Earth map, darktable, Siril, oMLX and models; run zsh scripts/bootstrap.sh without it to fetch them")
else
  # 5. GeoNames
  geonames_dir="$project_root/vendor/data/geonames"
  mkdir -p "$geonames_dir"
  for name in countryInfo.txt readme.txt cities500.zip; do
    fetch "$geonames_url$name" "$geonames_dir/$name" || skipped+=("GeoNames $name: download failed; re-run scripts/bootstrap.sh")
  done
  if [[ -f "$geonames_dir/cities500.zip" ]]; then
    unzip_member "$geonames_dir/cities500.zip" cities500.txt "$geonames_dir"
    echo "GeoNames cities500: ready"
  fi
  if [[ -n "${LUMEN_GEONAMES_COUNTRIES+set}" ]]; then
    countries="$LUMEN_GEONAMES_COUNTRIES"
  elif (( assume_yes )); then
    countries=""
  else
    read -r "countries?Country codes you shoot in, space separated (e.g. ZA AQ), blank to skip: " || countries=""
  fi
  for code in ${=countries}; do
    code="${(U)code}"
    if [[ "$code" != [A-Z][A-Z] ]]; then
      echo "GeoNames: skipping '$code', not a two-letter country code" >&2
      continue
    fi
    if fetch "$geonames_url$code.zip" "$geonames_dir/$code.zip"; then
      unzip_member "$geonames_dir/$code.zip" "$code.txt" "$geonames_dir"
      echo "GeoNames $code: ready"
    else
      skipped+=("GeoNames $code: download failed; re-run with LUMEN_GEONAMES_COUNTRIES=$code")
    fi
  done
  [[ -z "${countries// /}" ]] && skipped+=("GeoNames country files: none chosen; re-run with LUMEN_GEONAMES_COUNTRIES=\"ZA AQ\" for full place detail")

  # 5b. Natural Earth base map
  map_dir="$project_root/vendor/data/map"
  if ask "Download the Natural Earth base map for the Map tab (about 80 MB) into vendor/data/map? [Y/n]" Y; then
    mkdir -p "$map_dir"
    for name sha in "${natural_earth_layers[@]}"; do
      if fetch "$natural_earth_url$name" "$map_dir/$name"; then
        verify_sha256 "$map_dir/$name" "$sha"
      else
        skipped+=("Map layer $name: download failed; re-run scripts/bootstrap.sh, it resumes")
      fi
    done
  else
    skipped+=("Map layers: not downloaded; re-run scripts/bootstrap.sh and answer yes")
  fi

  # 6. darktable
  apps="$project_root/vendor/apps"
  if [[ -x "$apps/darktable.app/Contents/MacOS/darktable-cli" ]]; then
    echo "darktable: ready"
  else
    install_app darktable "$darktable_url" "$darktable_sha256" darktable.app "$apps"
  fi

  # 7. Siril
  if [[ -x "$apps/Siril.app/Contents/MacOS/siril-cli" ]]; then
    echo "Siril: ready"
  else
    install_app Siril "$siril_url" "$siril_sha256" Siril.app "$apps"
  fi

  # 8. oMLX
  if (( macos_major < 15 )); then
    skipped+=("oMLX and models: the AI features need macOS 15 or newer; this Mac runs $macos_version")
  else
    if command -v omlx >/dev/null 2>&1 || [[ -d /Applications/oMLX.app ]]; then
      echo "oMLX: ready"
    elif ask "Install oMLX.app into /Applications? [Y/n]" Y; then
      if (( macos_major >= 26 )); then
        install_app oMLX "$omlx_release/oMLX-0.7.2-macos26-27.dmg" "$omlx_tahoe_sha256" oMLX.app /Applications
      else
        install_app oMLX "$omlx_release/oMLX-0.7.2-macos15-sequoia.dmg" "$omlx_sequoia_sha256" oMLX.app /Applications
      fi
      [[ -d /Applications/oMLX.app ]] && echo "Open oMLX.app once: it installs its omlx command and writes ~/.omlx/settings.json."
    else
      skipped+=("oMLX: not installed; re-run scripts/bootstrap.sh and answer yes, or get it from https://github.com/jundot/omlx/releases")
    fi

    # 9. Models
    memory_gb=$(( $(sysctl -n hw.memsize) / 1073741824 ))
    model_dir=""
    if [[ -f "$HOME/.omlx/settings.json" ]]; then
      model_dir="$(python3 -c 'import json, os, sys
m = json.load(open(sys.argv[1])).get("model", {})
print(os.path.expanduser(m.get("model_dir") or (m.get("model_dirs") or [""])[0]))' "$HOME/.omlx/settings.json" 2>/dev/null || true)"
    fi
    if [[ -n "$model_dir" ]]; then
      echo "Model directory: $model_dir (from ~/.omlx/settings.json)"
    else
      model_dir="$HOME/Models"
      echo "Model directory: $model_dir (oMLX has no settings yet; point oMLX at this folder)"
    fi
    (( memory_gb < 16 )) && echo "This Mac reports $memory_gb GB; Lumen's AI features are untested under 16 GB." >&2

    model_ready() {
      local weights=("$1"/*.safetensors(N))
      [[ -f "$1/config.json" && ${#weights} -gt 0 ]]
    }
    # get_model repo question default
    get_model() {
      local dest="$model_dir/$1"
      if model_ready "$dest"; then
        echo "${1:t}: ready in $dest"
      elif ask "$2" "$3"; then
        uvx --from huggingface_hub hf download "$1" --local-dir "$dest" \
          || skipped+=("${1:t}: download failed; re-run scripts/bootstrap.sh, it resumes")
      else
        skipped+=("${1:t}: not downloaded; re-run scripts/bootstrap.sh and answer yes")
      fi
    }
    get_model "$fast_model" "This Mac reports $memory_gb GB. Download the fast model (6 GB, needed for every AI feature)? [Y/n]" Y
    if (( memory_gb >= 32 )); then
      get_model "$review_model" "Also download the review model (16 GB; worth it with 32 GB or more)? [Y/n]" Y
    else
      get_model "$review_model" "Also download the review model (16 GB; worth it with 32 GB or more)? [y/N]" N
    fi
  fi
fi

echo "Lumen is installed."
echo "Native face/eye analyzer: $native_bin/vision_analyzer"
echo "Native raw developer: $native_bin/raw_developer"
for line in "${skipped[@]}"; do
  echo "Skipped: $line"
done
echo "Next: uv run --project \"$project_root\" lumen doctor"
