#!/usr/bin/env bash
# Verify that a GoReleaser snapshot in dist/ matches the release contract:
# the six archives shipped in v1.0.0 plus the two macOS archives added in
# PR 4b (8 archives, 9 release files including the checksums file).
#
# Contract (see docs/superpowers/plans/2026-11-24-auto-maintenance-plan.md,
# 2.5 and 4b.2):
#   * exactly 8 archives, no extras (in particular no windows_arm64):
#       watchtower_linux_{386,amd64,arm64v8,armhf}.tar.gz
#       watchtower_windows_{386,amd64}.zip
#       watchtower_darwin_{amd64,arm64}.tar.gz
#   * exactly one checksums file, watchtower_<version>_checksums.txt, with one
#     line per archive in "<sha256>  <file>" form, verifiable with sha256sum -c
#   * nothing else at the top level of dist/ except GoReleaser's own build
#     metadata; any other file or directory fails the contract
#   * every archive contains watchtower (watchtower.exe on Windows) and
#     LICENSE.md
#   * every archived binary matches its corresponding build output byte-for-byte
#     and carries the injected version string
#
# The version check reads the binary with `strings` instead of executing it:
# watchtower has no --version flag (`-v` is --tlsverify) and prints its version
# only in the startup log, after connecting to a Docker daemon. Executing the
# snapshot binary therefore needs a live daemon and may hang; searching the
# image for the exact ldflags value is deterministic and runs on any host.
#
# Usage: bash scripts/check-release-contract.sh [dist-dir]
#   dist-dir defaults to ./dist relative to the repository root.
#
# Environment:
#   VERSION  expected version string. When set, the checksums filename must be
#            exactly watchtower_<VERSION>_checksums.txt; otherwise the version
#            is read from the checksums filename.
#
# The script is read-only with respect to the release: it lists/extracts
# archives and never executes them.
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

dist="${1:-dist}"
if [ ! -d "$dist" ]; then
  echo "check-release-contract: no dist directory at $dist" >&2
  echo "run: go tool -modfile=tools/go.mod goreleaser release --snapshot --clean" >&2
  exit 1
fi

fail() {
  echo "check-release-contract: FAIL: $*" >&2
  exit 1
}

archives=(
  watchtower_linux_386.tar.gz
  watchtower_linux_amd64.tar.gz
  watchtower_linux_arm64v8.tar.gz
  watchtower_linux_armhf.tar.gz
  watchtower_windows_386.zip
  watchtower_windows_amd64.zip
  watchtower_darwin_amd64.tar.gz
  watchtower_darwin_arm64.tar.gz
)

# Build metadata that GoReleaser always writes next to the archives.
metadata_files=(
  artifacts.json
  config.yaml
  metadata.json
)

# Per-target build directories that GoReleaser leaves in dist/, each
# holding exactly one binary (watchtower.exe on Windows).
target_dirs=(
  watchtower_linux_386_sse2
  watchtower_linux_amd64_v1
  watchtower_linux_arm_6
  watchtower_linux_arm64_v8.0
  watchtower_windows_386_sse2
  watchtower_windows_amd64_v1
  watchtower_darwin_amd64_v1
  watchtower_darwin_arm64_v8.0
)

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# 1. Exactly the eight archives.
for name in "${archives[@]}"; do
  [ -f "$dist/$name" ] || fail "missing archive $name"
done

# 2. Exactly one checksums file, and its name must match the contract.
find "$dist" -maxdepth 1 -type f -name '*checksums*.txt' -printf '%f\n' | sort > "$tmp/checksum-files.txt"
[ "$(wc -l < "$tmp/checksum-files.txt" | tr -d '[:space:]')" -eq 1 ] \
  || fail "expected exactly one checksums file, found:"$'\n'"$(cat "$tmp/checksum-files.txt")"
checksums="$(cat "$tmp/checksum-files.txt")"
expected_version="${VERSION:-}"
if [ -n "$expected_version" ]; then
  [ "$checksums" = "watchtower_${expected_version}_checksums.txt" ] \
    || fail "checksums file is $checksums, expected watchtower_${expected_version}_checksums.txt"
  version="$expected_version"
else
  case "$checksums" in
  watchtower_*_checksums.txt) ;;
  *) fail "checksums file is not named watchtower_<version>_checksums.txt: $checksums" ;;
  esac
  version="${checksums#watchtower_}"
  version="${version%_checksums.txt}"
  [ -n "$version" ] || fail "cannot derive a version from $checksums"
fi
echo "check-release-contract: checksums file: $checksums (version $version)"

# 3. Nothing else may look like a release artifact. Reject every regular file
#    anywhere under dist/ that is not one of the eight archives or the checksums
#    file; this catches extra archives in any format (.tar.xz, .tgz, ...) that
#    an allowlist of extensions would miss. The per-target build directories
#    must be exactly the eight expected ones.
find "$dist" -type f -printf '%P\n' | sort > "$tmp/all-files.txt"
{
  printf '%s\n' "${archives[@]}"
  printf '%s\n' "$checksums"
  printf '%s\n' "${metadata_files[@]}"
  for dir in "${target_dirs[@]}"; do
    case "$dir" in
    watchtower_windows_*) printf '%s/watchtower.exe\n' "$dir" ;;
    *) printf '%s/watchtower\n' "$dir" ;;
    esac
  done
} | sort > "$tmp/expected-artifacts.txt"
awk 'NR == FNR { allowed[$0] = 1; next } !($0 in allowed)' \
  "$tmp/expected-artifacts.txt" "$tmp/all-files.txt" > "$tmp/unexpected-files.txt"
if [ -s "$tmp/unexpected-files.txt" ]; then
  cat "$tmp/unexpected-files.txt" >&2
  fail "dist/ contains files outside the release contract (archives must be exactly the eight listed)"
fi
find "$dist" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort > "$tmp/actual-dirs.txt"
printf '%s\n' "${target_dirs[@]}" | sort > "$tmp/expected-dirs.txt"
if ! diff -u "$tmp/expected-dirs.txt" "$tmp/actual-dirs.txt" > "$tmp/dirs.diff"; then
  cat "$tmp/dirs.diff" >&2
  fail "dist/ contains unexpected directories"
fi

# 4. The checksums file lists exactly the eight archives, one well-formed line each.
line_count="$(wc -l < "$dist/$checksums" | tr -d '[:space:]')"
[ "$line_count" -eq "${#archives[@]}" ] || fail "$checksums has $line_count lines, expected ${#archives[@]}"
for name in "${archives[@]}"; do
  grep -Eq "^[0-9a-f]{64}  ${name}$" "$dist/$checksums" \
    || fail "$checksums has no well-formed sha256 line for $name"
done

(cd "$dist" && sha256sum -c "$checksums" > "$tmp/sha256.out" 2>&1) \
  || { cat "$tmp/sha256.out" >&2; fail "sha256sum -c $checksums failed"; }
echo "check-release-contract: checksums verified ($line_count entries)"

# 5. Every archive contains the binary and LICENSE.md, its packaged binary
#    matches the corresponding build output byte-for-byte, and it carries the
#    injected version string.
command -v cmp > /dev/null 2>&1 || fail "the 'cmp' tool is required for archive binary verification"
command -v strings > /dev/null 2>&1 || fail "the 'strings' tool is required for the version check"

declare -A archive_target_dir=(
  [watchtower_linux_386.tar.gz]=watchtower_linux_386_sse2
  [watchtower_linux_amd64.tar.gz]=watchtower_linux_amd64_v1
  [watchtower_linux_arm64v8.tar.gz]=watchtower_linux_arm64_v8.0
  [watchtower_linux_armhf.tar.gz]=watchtower_linux_arm_6
  [watchtower_windows_386.zip]=watchtower_windows_386_sse2
  [watchtower_windows_amd64.zip]=watchtower_windows_amd64_v1
  [watchtower_darwin_amd64.tar.gz]=watchtower_darwin_amd64_v1
  [watchtower_darwin_arm64.tar.gz]=watchtower_darwin_arm64_v8.0
)

for name in "${archives[@]}"; do
  case "$name" in
  *.zip) unzip -Z1 "$dist/$name" > "$tmp/list.txt" ;;
  *.tar.gz) tar -tzf "$dist/$name" > "$tmp/list.txt" ;;
  *) fail "no listing rule for archive $name" ;;
  esac
  sort -o "$tmp/list.txt" "$tmp/list.txt"
  case "$name" in
  watchtower_windows_*) expected_binary=watchtower.exe ;;
  *) expected_binary=watchtower ;;
  esac
  printf '%s\n%s\n' LICENSE.md "$expected_binary" | sort > "$tmp/expected-list.txt"
  if ! diff -u "$tmp/expected-list.txt" "$tmp/list.txt" > "$tmp/list.diff"; then
    cat "$tmp/list.diff" >&2
    fail "$name does not contain exactly LICENSE.md and $expected_binary"
  fi

  extract_dir="$tmp/extract/$name"
  mkdir -p "$extract_dir"
  case "$name" in
  *.zip) unzip -qq -o "$dist/$name" "$expected_binary" -d "$extract_dir" ;;
  *.tar.gz) tar -xzf "$dist/$name" -C "$extract_dir" "$expected_binary" ;;
  esac

  packaged_bin="$extract_dir/$expected_binary"
  built_bin="$dist/${archive_target_dir[$name]}/$expected_binary"
  [ -f "$packaged_bin" ] || fail "failed to extract $expected_binary from $name"
  [ -f "$built_bin" ] || fail "built binary missing: $built_bin"
  cmp -s "$packaged_bin" "$built_bin" \
    || fail "$name:$expected_binary differs from built binary $built_bin"

  strings "$packaged_bin" > "$tmp/packaged-binary.strings" 2> /dev/null
  grep -Fxq -- "$version" "$tmp/packaged-binary.strings" \
    || fail "injected version $version not found in $name:$expected_binary (ldflags injection missing?)"
done
echo "check-release-contract: archive contents, binary byte identity, and injected version $version verified for all ${#archives[@]} archives"

echo "check-release-contract: ok"
