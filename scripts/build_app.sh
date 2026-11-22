#!/bin/zsh
# Builds dist/Lumen.app and dist/Lumen-<version>.dmg: a relocatable Python with the lumen
# package, exiftool, both native helpers and the Swift shell in one bundle.
#
#   zsh scripts/build_app.sh                            unsigned; runs on this Mac only
#   zsh scripts/build_app.sh --sign "<identity>"        Developer ID signed, hardened runtime
#   zsh scripts/build_app.sh --sign "<identity>" --notarize <keychain-profile>
#
# Needs uv, swiftc and network once (Python 3.13, the hatchling backend, Pillow, exiftool).
set -eu

project_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_root"

usage() {
  echo 'usage: zsh scripts/build_app.sh [--sign "<identity>"] [--notarize <keychain-profile>]' >&2
  exit 2
}

identity=""
notary_profile=""
while (( $# )); do
  case "$1" in
    --sign) (( $# > 1 )) || usage; identity="$2"; shift ;;
    --notarize) (( $# > 1 )) || usage; notary_profile="$2"; shift ;;
    *) usage ;;
  esac
  shift
done
if [[ -n "$notary_profile" && -z "$identity" ]]; then
  echo "--notarize needs --sign: Apple only notarizes signed apps." >&2
  exit 2
fi

# Pinned exiftool. exiftool.org links its downloads to SourceForge, which keeps old versions;
# the hash is the one published at https://exiftool.org/checksums.txt.
exiftool_version="13.64"
exiftool_url="https://sourceforge.net/projects/exiftool/files/Image-ExifTool-$exiftool_version.tar.gz/download"
exiftool_sha256="dcb9f6be1f6705ee826a92d1522e3fec1d0b3749cdc061b03c46873d5fe204fc"

version="$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml)"
target=arm64-apple-macos13.5
build="$project_root/build"
app="$build/Lumen.app"
contents="$app/Contents"
resources="$contents/Resources"
rm -rf "$app" "$build/dmg" "$build/exiftool" "$build/Lumen.iconset"
mkdir -p "$contents/MacOS" "$contents/Helpers" "$resources/bin" "$project_root/dist" "$project_root/vendor/downloads"

# 1-2. Info.plist
usage_text="Lumen reads the photos you choose to import. It never changes them."
cat > "$contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>au.com.jahan-interactive.lumen</string>
  <key>CFBundleName</key><string>Lumen</string>
  <key>CFBundleExecutable</key><string>Lumen</string>
  <key>CFBundleIconFile</key><string>Lumen</string>
  <key>CFBundleShortVersionString</key><string>$version</string>
  <key>CFBundleVersion</key><string>$version</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>13.5</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSDesktopFolderUsageDescription</key><string>$usage_text</string>
  <key>NSDocumentsFolderUsageDescription</key><string>$usage_text</string>
  <key>NSDownloadsFolderUsageDescription</key><string>$usage_text</string>
  <key>NSRemovableVolumesUsageDescription</key><string>$usage_text</string>
  <key>NSNetworkVolumesUsageDescription</key><string>$usage_text</string>
</dict>
</plist>
EOF
plutil -lint -s "$contents/Info.plist"

# 3. Python: uv's standalone build is relocatable, so a plain copy runs from inside the bundle.
uv python install 3.13
python_home="$(dirname "$(dirname "$(uv python find --managed-python 3.13)")")"
cp -R "$python_home" "$resources/python"
python="$resources/python/bin/python3"
stdlib="$resources/python/lib/python3.13"
# Tk, the test suite, pip and headers are never used by the server. The bin/ scripts
# other than python itself carry the build Mac's absolute path in their #! line.
rm -rf "$stdlib"/{test,idlelib,tkinter,ensurepip,turtledemo} "$stdlib"/lib-dynload/_tkinter.*.so \
  "$stdlib"/site-packages/pip "$stdlib"/site-packages/pip-*.dist-info \
  "$resources/python"/lib/{tcl*,tk*,itcl*,thread*,libtcl*} \
  "$resources/python"/{include,share,BUILD} \
  "$resources/python"/bin/{idle*,pip*,pydoc*,python3-config,python3.13-config}
uv pip install --python "$python" --target "$stdlib/site-packages" --link-mode copy "$project_root"
rm -rf "$stdlib/site-packages/bin"  # the lumen entry script: absolute #!, and the shell runs python -m lumen
"$python" -I -m compileall -q "$resources/python/lib"

# 4. exiftool: a Perl script that finds its lib/ beside itself; macOS ships /usr/bin/perl.
tarball="$project_root/vendor/downloads/Image-ExifTool-$exiftool_version.tar.gz"
if [[ ! -f "$tarball" ]]; then
  echo "Downloading $exiftool_url"
  # --retry-all-errors and -C -: a reset connection resumes the .part file instead of failing the run.
  if ! curl -fL --retry 3 --retry-all-errors -C - -o "$tarball.part" "$exiftool_url"; then
    echo "exiftool download failed from $exiftool_url" >&2
    exit 1
  fi
  mv "$tarball.part" "$tarball"
fi
got="$(shasum -a 256 "$tarball" | cut -d ' ' -f 1)"
if [[ "$got" != "$exiftool_sha256" ]]; then
  echo "${tarball:t}: sha256 mismatch (expected $exiftool_sha256, got $got). Delete it and re-run." >&2
  exit 1
fi
mkdir -p "$build/exiftool"
tar -xzf "$tarball" -C "$build/exiftool" --strip-components 1
cp "$build/exiftool/exiftool" "$resources/bin/exiftool"
cp -R "$build/exiftool/lib" "$resources/bin/lib"

# 5. Helpers and the shell. -O for the developer and the shell, as bootstrap does.
module_cache="$build/swift-module-cache"
CLANG_MODULE_CACHE_PATH="$module_cache" swiftc -target "$target" \
  "$project_root/tools/vision_analyzer.swift" -o "$contents/Helpers/vision_analyzer"
CLANG_MODULE_CACHE_PATH="$module_cache" swiftc -O -target "$target" \
  "$project_root/tools/raw_developer.swift" -o "$contents/Helpers/raw_developer"
CLANG_MODULE_CACHE_PATH="$module_cache" swiftc -O -target "$target" \
  "$project_root/tools/lumen_app.swift" -o "$contents/MacOS/Lumen"

# 6. The setup window runs the same script as the shell route.
cp "$project_root/scripts/bootstrap.sh" "$resources/bootstrap.sh"

# 7. Icon: the mark is 16x16 pixel art, so each size is a whole-number nearest-neighbour
# enlargement of the SVG's rects (no SVG renderer here would keep the edges hard).
mkdir -p "$build/Lumen.iconset"
"$python" -I -B - "$project_root/src/lumen/static/mark.svg" "$build/Lumen.iconset" <<'EOF'
import re, sys
from PIL import Image
mark = Image.new("RGBA", (16, 16))
for x, y, colour in re.findall(r'<rect x="(\d+)" y="(\d+)" width="1" height="1" fill="(#[0-9a-f]{6})"', open(sys.argv[1]).read()):
    mark.putpixel((int(x), int(y)), tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5)) + (255,))
for size in (16, 32, 128, 256, 512):
    mark.resize((size, size), Image.NEAREST).save(f"{sys.argv[2]}/icon_{size}x{size}.png")
    mark.resize((size * 2, size * 2), Image.NEAREST).save(f"{sys.argv[2]}/icon_{size}x{size}@2x.png")
EOF
iconutil -c icns "$build/Lumen.iconset" -o "$resources/Lumen.icns"

# The smoke test reads "nothing wrote into the bundle" as no .pyc newer than Info.plist.
touch "$contents/Info.plist"

# 8. Signing: every Mach-O (found by content, not extension), deepest first, then the app.
if [[ -n "$identity" ]]; then
  find "$app" -type f -print0 | while IFS= read -r -d '' file_path; do
    file -b "$file_path" | grep -q '^Mach-O' && print -r -- "$file_path"
  done | awk -F/ '{ print NF "\t" $0 }' | sort -rn | cut -f 2- | while IFS= read -r file_path; do
    codesign --force --options runtime --timestamp --sign "$identity" "$file_path"
  done
  codesign --force --options runtime --timestamp --sign "$identity" "$app"
  codesign --verify --deep --strict "$app"
else
  # Unsigned build: on Apple Silicon each Mach-O still needs the ad-hoc signature it came with.
  "$python" -I -B -c 'import lumen, PIL._imaging' || { echo "The bundled Python does not run." >&2; exit 1; }
fi

rm -rf "$project_root/dist/Lumen.app"
mv "$app" "$project_root/dist/Lumen.app"
app="$project_root/dist/Lumen.app"

# 9. Disk image: the app beside an Applications link to drag it onto.
dmg="$project_root/dist/Lumen-$version.dmg"
mkdir -p "$build/dmg"
ditto "$app" "$build/dmg/Lumen.app"
ln -s /Applications "$build/dmg/Applications"
hdiutil create -volname Lumen -srcfolder "$build/dmg" -format UDZO -ov "$dmg"
rm -rf "$build/dmg"
if [[ -n "$identity" ]]; then
  codesign --force --timestamp --sign "$identity" "$dmg"
fi
if [[ -n "$notary_profile" ]]; then
  xcrun notarytool submit "$dmg" --keychain-profile "$notary_profile" --wait
  xcrun stapler staple "$dmg"
  spctl -a -t open --context context:primary-signature -v "$dmg"
fi

echo "Built $app"
echo "Built $dmg"
