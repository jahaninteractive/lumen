#!/bin/zsh
set -eu

project_root="$(cd "$(dirname "$0")/.." && pwd)"
work_dir="$(mktemp -d -t lumen-smoke.XXXXXX)"
trap 'rm -rf "$work_dir"' EXIT

export LUMEN_SMOKE_ROOT="$work_dir"
uv run --project "$project_root" python -c \
  'from pathlib import Path; from PIL import Image; import os; root=Path(os.environ["LUMEN_SMOKE_ROOT"]); Image.new("RGB", (160, 120), (96, 112, 128)).save(root / "sample.jpg")'
uv run --project "$project_root" lumen --library "$work_dir/library" import "$work_dir/sample.jpg" --json
uv run --project "$project_root" lumen --library "$work_dir/library" groups --json
uv run --project "$project_root" python -m unittest discover -s "$project_root/tests" -v

echo "Lumen smoke test passed."

