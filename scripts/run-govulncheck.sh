#!/usr/bin/env bash
# Run govulncheck across all eight release targets (the six Linux/Windows
# targets plus darwin/amd64 and darwin/arm64) and evaluate each report with
# scripts/govulncheck-gate.py.
#
# Builds govulncheck once for the host platform using the Go toolchain pinned in
# .go-version, places that toolchain's bin directory first on PATH so
# govulncheck's child `go list` invocations match the compiler that built it,
# and then sets GOOS/GOARCH/GOARM only on the scanner subprocess for each
# target.
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

go_version="$(tr -d '[:space:]' < .go-version)"
if [ -z "$go_version" ]; then
  echo "run-govulncheck: .go-version is empty" >&2
  exit 1
fi

export GOTOOLCHAIN="go${go_version}"

goroot="$(go env GOROOT)"
if command -v cygpath > /dev/null 2>&1; then
  goroot_posix="$(cygpath -u "$goroot")"
else
  goroot_posix="$goroot"
fi
export PATH="${goroot_posix}/bin:${PATH}"

goexe="$(go env GOEXE)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

govulncheck_bin="${tmp}/govulncheck${goexe}"
go build -modfile=tools/go.mod -o "$govulncheck_bin" golang.org/x/vuln/cmd/govulncheck

# Release targets matching goreleaser.yml.
targets=(
  "linux/amd64/"
  "linux/386/"
  "linux/arm/6"
  "linux/arm64/"
  "windows/amd64/"
  "windows/386/"
  "darwin/amd64/"
  "darwin/arm64/"
)

for spec in "${targets[@]}"; do
  IFS='/' read -r goos goarch goarm <<< "$spec"
  label="${goos}/${goarch}${goarm:+/v${goarm}}"
  report="${tmp}/govulncheck-${goos}-${goarch}${goarm:+-v${goarm}}.json"
  echo "=== govulncheck: ${label} ==="

  scanner_exit=0
  if [ -n "$goarm" ]; then
    GOOS="$goos" GOARCH="$goarch" GOARM="$goarm" \
      "$govulncheck_bin" -format json ./... > "$report" || scanner_exit=$?
  else
    GOOS="$goos" GOARCH="$goarch" \
      "$govulncheck_bin" -format json ./... > "$report" || scanner_exit=$?
  fi

  python -I scripts/govulncheck-gate.py --scanner-exit "$scanner_exit" "$report"
done
