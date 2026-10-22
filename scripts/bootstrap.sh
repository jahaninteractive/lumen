#!/bin/zsh
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

check_command uv
check_command exiftool
check_command swiftc

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  echo "Lumen's supported target is Apple Silicon macOS." >&2
  exit 1
fi

uv sync --project "$project_root"

library_root="${LUMEN_LIBRARY:-$HOME/Pictures/Lumen Library}"
native_bin="$library_root/cache/bin"
module_cache="$library_root/cache/swift-module-cache"
mkdir -p "$native_bin" "$module_cache"
CLANG_MODULE_CACHE_PATH="$module_cache" swiftc \
  "$project_root/tools/vision_analyzer.swift" \
  -o "$native_bin/vision_analyzer"

echo "Lumen is installed."
echo "Native face/eye analyzer: $native_bin/vision_analyzer"
echo "Next: uv run --project \"$project_root\" lumen doctor"

