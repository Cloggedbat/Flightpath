#!/usr/bin/env bash
# Copy the Python engine into the Android app. Run after editing engine/.
set -e
cd "$(dirname "$0")"
SRC=engine/flightpath
DST=android/app/src/main/python/flightpath
cp engine/ui/index.html "$SRC/ui/index.html"
rm -rf "$DST"
mkdir -p "$DST"
cp -R "$SRC"/. "$DST"/
find "$DST" -name __pycache__ -type d -prune -exec rm -rf {} +
echo "engine synced into android/"
