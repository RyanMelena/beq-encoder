#!/bin/sh
# The runtime user (PUID) has no home in the image: give it one under /config.
set -e
mkdir -p "$HOME"
exec "$@"
