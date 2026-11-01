#!/bin/bash
# Build the prototype image and run run-proto.sh in it as an unprivileged user.
#   bash vmtest/kids-proto/run-in-docker.sh <unpacked-OtterTube-dir> [<screenshot-dir>]
# <unpacked-OtterTube-dir> is electron-builder's release/linux-unpacked (it holds
# the ottertube-desktop binary). Screenshots land in <screenshot-dir> (default:
# a fresh temporary directory, printed at the end).
set -euo pipefail
APP=${1:?usage: run-in-docker.sh <unpacked-OtterTube-dir> [<screenshot-dir>]}
OUT=${2:-$(mktemp -d)}
HERE=$(cd "$(dirname "$0")" && pwd)
[ -x "$APP/ottertube-desktop" ] || { echo "no ottertube-desktop in $APP" >&2; exit 2; }
mkdir -p "$OUT"; chmod 777 "$OUT"
docker build -q -t kids-kiosk-proto "$HERE" | tail -1
docker run --rm --shm-size=512m -u kid -e HOME=/home/kid \
    -v "$HERE":/proto:ro -v "$OUT":/out -v "$APP":/app:ro \
    kids-kiosk-proto bash /proto/run-proto.sh
echo "screenshots: $OUT"
