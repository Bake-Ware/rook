#!/usr/bin/env bash
# Install a release of Rook's session host (rook.agents) and make it current.
#
#   tools/agents/install.sh <full git sha> [tag]
#
# Builds ~/rook-agents/releases/<tag>/ from `git archive <sha> rook` (plus a
# REVISION file), checks that it imports with the worker venv's Python, and
# flips ~/rook-agents/current to it (the old target becomes `previous`; see
# rollback.sh). Nothing is restarted: afterwards run
#   systemctl --user restart rook-agent-sessions
#
# Env: ROOK_AGENTS_HOME (default ~/rook-agents), ROOK_AGENTS_PY (default
# ~/.rook-band-worker/venv/bin/python), ROOK_REPO (default: this checkout).
set -euo pipefail

die() { echo "install.sh: $*" >&2; exit 1; }

sha=${1:-}
[[ $sha =~ ^[0-9a-f]{40}$ ]] || die "usage: install.sh <full 40-character git sha> [tag]"
tag=${2:-${sha:0:12}}
[[ $tag =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$ ]] || die "tag: letters, digits, . _ -"

repo=${ROOK_REPO:-$(git -C "$(dirname "$0")" rev-parse --show-toplevel)}
base=${ROOK_AGENTS_HOME:-$HOME/rook-agents}
py=${ROOK_AGENTS_PY:-$HOME/.rook-band-worker/venv/bin/python}
rel=$base/releases/$tag

git -C "$repo" cat-file -e "$sha^{commit}" 2>/dev/null || die "$sha is not a commit in $repo"
[ -e "$rel" ] && die "$rel already exists (pick another tag)"
[ -x "$py" ] || die "no python at $py"

mkdir -p "$base/releases"
tmp=$(mktemp -d "$base/releases/.$tag.XXXXXX")
trap 'rm -rf "$tmp"' EXIT
git -C "$repo" archive "$sha" rook | tar -x -C "$tmp"
echo "$sha" > "$tmp/REVISION"
(cd "$tmp" && PYTHONPATH="$tmp" "$py" -P -c "import rook.agents.host, rook.agents.__main__") \
  || die "the release does not import with $py"
mv "$tmp" "$rel"
trap - EXIT

if [ -L "$base/current" ]; then
  ln -sfn "$(readlink "$base/current")" "$base/previous.new"
  mv -T "$base/previous.new" "$base/previous"
fi
ln -sfn "releases/$tag" "$base/current.new"
mv -T "$base/current.new" "$base/current"
echo "current -> releases/$tag ($sha)"
[ -L "$base/previous" ] && echo "previous -> $(readlink "$base/previous")"
echo "restart with: systemctl --user restart rook-agent-sessions"
