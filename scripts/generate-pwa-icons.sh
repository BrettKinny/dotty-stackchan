#!/usr/bin/env bash
# Generate PWA icons from the source SVG. Requires ImageMagick (magick or convert).
#
# The bridge dashboard manifest ( /ui/manifest.json ) already serves the
# source SVG and the iOS apple-touch-icon.png. Some Android browsers prefer
# raster icons at 192/512 for the install card / splash; this script
# renders them once into bridge/static/ so they ship as plain static files.
set -euo pipefail
cd "$(dirname "$0")/.."
SRC="bridge/assets/dotty-icon.svg"
OUT="bridge/static"
mkdir -p "$OUT"
for size in 192 512; do
  magick -background none -density 384 "$SRC" -resize "${size}x${size}" "$OUT/icon-${size}.png"
done
echo "Generated icon-192.png and icon-512.png in $OUT"
