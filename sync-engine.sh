#!/usr/bin/env bash
# Copy the Python engine into the Android app. Run after editing engine/.
set -e
cd "$(dirname "$0")"
rsync -a --delete --exclude __pycache__ engine/flightpath/ android/app/src/main/python/flightpath/
cp engine/ui/index.html engine/flightpath/ui/index.html 2>/dev/null || true
cp engine/ui/index.html android/app/src/main/python/flightpath/ui/index.html
echo "engine synced into android/"
