#!/usr/bin/env bash
# Build the repository's pinned Go tools into .tools/ (git-ignored).
#
# Tool versions come from tools/go.mod + tools/go.sum; the Go toolchain comes
# from .go-version. Both local development and CI use this script so the
# invocation is identical everywhere.
#
# Usage: bash scripts/tools.sh [tool...]
#   No arguments builds every tool listed below.
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

if [ ! -f .go-version ]; then
  echo "scripts/tools.sh: .go-version not found at $repo_root" >&2
  exit 1
fi

go_version="$(tr -d '[:space:]' < .go-version)"
if [ -z "$go_version" ]; then
  echo "scripts/tools.sh: .go-version is empty" >&2
  exit 1
fi

# Pin the toolchain: the go command may otherwise silently download/use a
# different toolchain than .go-version declares.
export GOTOOLCHAIN="go${go_version}"

# Import path of each tool binary. Keep the list sorted.
staticcheck_pkg="honnef.co/go/tools/cmd/staticcheck"
govulncheck_pkg="golang.org/x/vuln/cmd/govulncheck"
goreleaser_pkg="github.com/goreleaser/goreleaser/v2"
actionlint_pkg="github.com/rhysd/actionlint/cmd/actionlint"

all_tools="staticcheck govulncheck goreleaser actionlint"
selected="${*:-$all_tools}"

pkgs=()
for tool in $selected; do
  case "$tool" in
  staticcheck) pkgs+=("$staticcheck_pkg") ;;
  govulncheck) pkgs+=("$govulncheck_pkg") ;;
  goreleaser) pkgs+=("$goreleaser_pkg") ;;
  actionlint) pkgs+=("$actionlint_pkg") ;;
  *)
    echo "scripts/tools.sh: unknown tool '$tool' (expected one of: $all_tools)" >&2
    exit 1
    ;;
  esac
done

mkdir -p .tools

# Build from the repository root so that go.mod/go.sum resolution uses the
# tools module through -modfile, while the build itself runs in the repo.
go build -modfile=tools/go.mod -o .tools/ "${pkgs[@]}"
