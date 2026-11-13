#!/usr/bin/env bash
# Opens a pull request when truehdd or dee_shim has a newer release than the Dockerfile
# pins. Run by .github/workflows/tooling-updates.yml; needs gh, authenticated by GH_TOKEN.
#
#   tooling-update.sh truehdd|dee-shim
#
# DRY_RUN=1 edits the Dockerfile and prints the diff and the PR body, with no git or
# GitHub writes. LATEST=<tag> stands in for the latest release (for testing).
set -euo pipefail

tool=${1:?usage: tooling-update.sh truehdd|dee-shim}
dockerfile=Dockerfile

arg() { sed -n "s/^ARG $1=//p" "$dockerfile"; }
set_arg() { sed -i "s|^ARG $1=.*|ARG $1=$2|" "$dockerfile"; }
newer() { [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -n1)" = "$2" ]; }
latest_release() { gh release view --repo "$1" --json tagName --jq .tagName; }

recheck='docs/maintaining.md: "Re-check a new DEE, dee_shim, or truehdd"'
original=$(mktemp)
cp "$dockerfile" "$original"
body=$(mktemp)

case "$tool" in
  truehdd)
    repo=truehdd/truehdd
    current=$(arg TRUEHDD_VERSION)
    pinned=$(arg TRUEHDD_COMMIT)
    latest=${LATEST:-$(latest_release "$repo")}
    newer "$current" "$latest" || { echo "truehdd $current: no newer release ($latest)"; exit 0; }
    set_arg TRUEHDD_VERSION "$latest"
    built="the $current release"
    pin_note=""
    if [ -n "$pinned" ]; then
      # The image builds a commit after the release in its place: back to the release only if
      # the release includes that commit.
      built="commit \`${pinned:0:7}\` (after $current)"
      status=$(gh api "repos/$repo/compare/$pinned...$latest" --jq .status 2>/dev/null || true)
      if [ "$status" = ahead ] || [ "$status" = identical ]; then
        set_arg TRUEHDD_COMMIT ""
        pin_note="This release includes \`${pinned:0:7}\`, so \`TRUEHDD_COMMIT\` is cleared and the image uses the release again."
      else
        pin_note="**This release does not include \`${pinned:0:7}\`** (compare: ${status:-unknown}). \`TRUEHDD_COMMIT\` is kept, and the image build fails until you either clear it (going back to a decoder without that commit's fixes) or keep the pin and close this PR."
      fi
    fi
    cat >"$body" <<EOF
truehdd [$current → $latest](https://github.com/$repo/releases/tag/$latest) ([changes](https://github.com/$repo/compare/${pinned:-$current}...$latest)). The image built $built. $pin_note

CI builds the image with it, but the tests run a fake truehdd, so nothing has exercised the new build yet. Before merging:

- [ ] Encode a TrueHD Atmos excerpt and a channel-based one and read the checks in the job log ($recheck). The job log should say "position update times read to the sample".
- [ ] Bump \`ENCODER_REVISION\` in \`app/beq_encoder/assess.py\` if the decoder's output changes.
- [ ] Update "Known limits" in the README and docs/truehd-encoding.md where this release changes what they describe.
EOF
    ;;

  dee-shim)
    repo=truehdd/dee-shim
    current=$(arg DEE_SHIM_VERSION)
    latest=${LATEST:-$(latest_release "$repo")}
    newer "$current" "$latest" || { echo "dee_shim $current: no newer release ($latest)"; exit 0; }
    commit=$(gh api "repos/$repo/commits/$latest" --jq .sha)
    # The toolchain the release was built with, from its release workflow.
    rust=$(gh api "repos/$repo/contents/.github/workflows/release.yml?ref=$latest" \
             -H 'Accept: application/vnd.github.raw' \
           | sed -n 's/^ *RUST_TOOLCHAIN: *"\{0,1\}\([0-9][0-9.]*\)"\{0,1\}.*/\1/p' | head -n1)
    rust_note="Rust $rust, from the release's \`release.yml\`."
    if [ -z "$rust" ]; then
      rust=$(arg DEE_SHIM_RUST)
      rust_note="**Rust toolchain not found** in the release's \`release.yml\`: kept $rust; check it."
    fi
    set_arg DEE_SHIM_VERSION "$latest"
    set_arg DEE_SHIM_COMMIT "$commit"
    set_arg DEE_SHIM_RUST "$rust"

    cat >"$body" <<EOF
dee_shim [$current → $latest](https://github.com/$repo/releases/tag/$latest) ([changes](https://github.com/$repo/compare/$current...$latest)), commit \`${commit:0:7}\`. $rust_note

CI builds the image with it, but the tests run a fake dee_shim, so nothing has exercised the new build yet. Before merging:

- [ ] Run the start-up probe, then encode a TrueHD Atmos excerpt and a 5.1 WAV and read the checks in the job log ($recheck).
- [ ] Bump \`ENCODER_REVISION\` in \`app/beq_encoder/assess.py\` if the encoder's output changes.
- [ ] Update docs/truehd-encoding.md and "Known limits" in the README where this release changes what they describe.
EOF
    ;;

  *) echo "unknown tool: $tool" >&2; exit 2 ;;
esac

branch="tooling/$tool-$latest"
title="Update $tool to $latest"

if [ -n "${DRY_RUN:-}" ]; then
  diff -u "$original" "$dockerfile" || true
  printf '\n--- %s (%s)\n' "$title" "$branch"
  cat "$body"
  exit 0
fi

# One PR per release: an open one is left alone, and a closed one means it was declined.
if [ "$(gh pr list --head "$branch" --state all --json number --jq length)" != 0 ]; then
  echo "$branch already has a pull request"
  exit 0
fi

base=$(gh repo view --json defaultBranchRef --jq .defaultBranchRef.name)
git switch -q -c "$branch"
git commit -q -am "$title"
git push -q -f origin "$branch"
gh pr create --base "$base" --head "$branch" --title "$title" --body-file "$body"
# A pull request opened with the workflow's own token starts no workflows, so start CI.
gh workflow run ci.yml --ref "$branch"
