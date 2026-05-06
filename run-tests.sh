#!/usr/bin/env bash
# Run dedupe-images tests via uv. Pulls all needed deps (incl. pytest +
# piexif for fixture EXIF) inline; no venv setup required.
set -e
cd "$(dirname "$0")"
exec uv run \
    --with pytest \
    --with piexif \
    --with Pillow \
    --with imagehash \
    --with pillow-heif \
    pytest tests/ "$@"
