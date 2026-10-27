#!/bin/sh
# Build mediamtx with the keyframe gate (gortsplib-keyframe-gate.patch):
# every RTSP reader starts on an IDR instead of mid-GOP, and a reader that
# loses a packet on a full write queue resumes at the next IDR. See
# TECHNICAL.md §11.
#
#   mediamtx-fork/build.sh <mediamtx version, e.g. 1.12.2> <output binary>
#
# Needs go, git and network access (module + go:generate downloads). The
# Dockerfile runs it in a golang build stage; GOARCH picks the target.
set -eu

VERSION="$1"
case "$2" in /*) OUT="$2" ;; *) OUT="$PWD/$2" ;; esac
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

git clone -q --depth 1 --branch "v$VERSION" https://github.com/bluenviron/mediamtx.git "$WORK/mediamtx"
cd "$WORK/mediamtx"

# patch the gortsplib version this mediamtx release pins, as a local module
go mod download github.com/bluenviron/gortsplib/v4
cp -r "$(go list -m -f '{{.Dir}}' github.com/bluenviron/gortsplib/v4)" "$WORK/gortsplib"
chmod -R u+w "$WORK/gortsplib"
patch -d "$WORK/gortsplib" -p1 < "$HERE/gortsplib-keyframe-gate.patch"
go mod edit -replace "github.com/bluenviron/gortsplib/v4=$WORK/gortsplib"

go generate ./...
# `mediamtx --version` tells the patched build from a stock one
printf 'v%s-keyframegate' "$VERSION" > internal/core/VERSION

CGO_ENABLED=0 go build -trimpath -ldflags '-s -w' -o "$OUT" .
echo "built $OUT ($("$OUT" --version 2>/dev/null || echo "cross-compiled for ${GOARCH:-host}"))"
