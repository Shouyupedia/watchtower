#!/usr/bin/env bash
# Verify that every external GitHub Action `uses:` reference is pinned to a
# 40-character commit SHA with a `# vX.Y.Z` tag comment, and that the SHA
# matches the peeled commit of that tag on GitHub.
#
# Parses workflow and composite-action YAML via scripts/extract_action_uses.go
# (using tools/go.mod's pinned gopkg.in/yaml.v3), so both block-style and
# flow-style step mappings are inspected.
#
# Usage: bash scripts/verify-action-pins.sh [--exclude <relpath> ...] [file ...]
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

go_version="$(tr -d '[:space:]' < .go-version)"
if [ -z "$go_version" ]; then
  echo "verify-action-pins: .go-version is empty" >&2
  exit 1
fi
export GOTOOLCHAIN="go${go_version}"

excludes=()
explicit_files=()
while [ $# -gt 0 ]; do
  case "$1" in
  --exclude)
    shift
    [ $# -gt 0 ] || { echo "error: --exclude requires a path" >&2; exit 2; }
    excludes+=("$1")
    ;;
  -*)
    echo "error: unknown argument '$1'" >&2
    exit 2
    ;;
  *)
    explicit_files+=("$1")
    ;;
  esac
  shift
done

is_excluded() {
  local target="$1"
  local ex
  for ex in "${excludes[@]+"${excludes[@]}"}"; do
    if [ "$target" = "$ex" ]; then
      return 0
    fi
  done
  return 1
}

files=()
if [ "${#explicit_files[@]}" -gt 0 ]; then
  for f in "${explicit_files[@]}"; do
    if ! is_excluded "$f"; then
      files+=("$f")
    fi
  done
else
  while IFS= read -r f; do
    if ! is_excluded "$f"; then
      files+=("$f")
    fi
  done < <(
    find .github/workflows .github/actions -type f \( -name '*.yml' -o -name '*.yaml' \) 2>/dev/null | sed 's|^\./||' | sort
  )
fi

if [ "${#files[@]}" -eq 0 ]; then
  echo "verify-action-pins: FAIL: no workflow or action YAML files to inspect" >&2
  exit 1
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

extracted="$tmp/uses.tsv"
go run -modfile=tools/go.mod scripts/extract_action_uses.go "${files[@]}" > "$extracted"

declare -A cache=()
failures=0

while IFS=$'\t' read -r file lineno ref comment; do
  [ -n "$file" ] || continue
  # Skip local actions / reusable workflows / docker://
  case "$ref" in
  ./*|docker://*) continue ;;
  esac
  if [[ ! "$ref" =~ ^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(/[^@]+)?@([0-9a-f]{40})$ ]]; then
    echo "FAIL: $file:$lineno: not pinned to a 40-char commit SHA: $ref" >&2
    failures=$((failures + 1))
    continue
  fi
  repo="${BASH_REMATCH[1]}"
  sha="${BASH_REMATCH[3]}"
  tag="${comment%%[[:space:]]*}"
  if [[ ! "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "FAIL: $file:$lineno: missing '# vX.Y.Z' tag comment for $repo@$sha (got '# $comment')" >&2
    failures=$((failures + 1))
    continue
  fi
  key="${repo}@${tag}"
  if [ -z "${cache[$key]+x}" ]; then
    out="$(git ls-remote "https://github.com/${repo}.git" "refs/tags/${tag}" "refs/tags/${tag}^{}" 2>/dev/null || true)"
    peeled="$(printf '%s\n' "$out" | awk -v t="refs/tags/${tag}^{}" '$2 == t { print $1 }')"
    direct="$(printf '%s\n' "$out" | awk -v t="refs/tags/${tag}" '$2 == t { print $1 }')"
    resolved="${peeled:-$direct}"
    cache[$key]="$resolved"
  fi
  resolved="${cache[$key]}"
  if [ -z "$resolved" ]; then
    echo "FAIL: $file:$lineno: tag $tag not found in https://github.com/${repo}.git" >&2
    failures=$((failures + 1))
  elif [ "$resolved" != "$sha" ]; then
    echo "FAIL: $file:$lineno: $repo@$tag resolves to $resolved, not $sha" >&2
    failures=$((failures + 1))
  else
    echo "OK:   $file:$lineno: $repo@$sha ($tag)"
  fi
done < "$extracted"

if [ "$failures" -gt 0 ]; then
  echo "verify-action-pins: FAIL ($failures issue(s))" >&2
  exit 1
fi
echo "verify-action-pins: ok (${#cache[@]} unique action tag(s) verified)"
