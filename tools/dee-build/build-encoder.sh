#!/bin/sh
# Builds the Linux dee_dthd_encoder from the macOS one at /in/dee_dthd_encoder and puts it
# in /out (your DEE folder). Arguments are passed on to `dthd-mac2linux build`, for example
# --verify, --allow-toolchain-drift or --allow-unpinned-payload.
#
# FORCE=1 replaces a dee_dthd_encoder that /out already holds.
set -eu

payload=/in/dee_dthd_encoder
out=/out
name=dee_dthd_encoder

fail() { echo "ERROR: $*" >&2; exit 1; }

[ -f "$payload" ] || fail "no macOS $name at $payload: mount it with
  -v /path/to/macos/$name:$payload:ro"
[ -d "$out" ] || fail "no output folder at $out: mount your DEE folder with -v \"\$DEE_PATH\":$out"
[ -w "$out" ] || fail "$out is not writable: mount your DEE folder without :ro"
if [ -e "$out/$name" ] && [ -z "${FORCE:-}" ]; then
  fail "$out/$name exists already. Run again with -e FORCE=1 to replace it."
fi

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# The builder writes work files beside its checkout's target folder, so build as root in
# the image and hand the result to the owner of the DEE folder.
dthd-mac2linux build --repo "$M2L_REPO" --sources "$M2L_SOURCES" \
  --payload "$payload" --out "$work/dist" --jobs "$(nproc)" "$@"

owner=$(stat -c '%u:%g' "$out")
for f in "$name" BUILDINFO.json SHA256SUMS; do
  # SHA256SUMS also lists the symbol file, which the encoder does not need.
  [ "$f" = SHA256SUMS ] && sed -i "/$name\.sym\$/d" "$work/dist/$f"
  cp "$work/dist/$f" "$out/$f.tmp"
  chown "$owner" "$out/$f.tmp"
  mv -f "$out/$f.tmp" "$out/$f"
done
chmod 0755 "$out/$name"

echo
echo "Wrote $out/$name:"
( cd "$out" && sha256sum -c SHA256SUMS )
for beside in libmediainfo.so.0 license.lic; do
  [ -e "$out/$beside" ] || echo "NOTE: $out has no $beside. dthd-mac2linux expects the encoder beside Linux DEE's."
done
