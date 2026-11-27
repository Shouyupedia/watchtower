## Origin & Acknowledgements

This repository is an independently maintained project derived from the original [Watchtower](https://github.com/containrrr/watchtower) project (now archived).

- **Original repository:** https://github.com/containrrr/watchtower
- **Original documentation:** https://containrrr.dev/watchtower/
- **License:** Continues to be distributed under the [Apache License 2.0](LICENSE.md), preserving all original copyright and attribution notices.

**My utmost respect and deepest gratitude go to the original Watchtower project and all its maintainers and contributors.**

Their outstanding work laid the foundation for this project. None of this would have been possible without their dedication and contributions.

## Quick Start

```
docker run -d \
  --name watchtower \
  --restart unless-stopped \
  -v /var/run/docker.sock:/var/run/docker.sock \
  ghcr.io/shouyupedia/watchtower:latest \
  --cleanup \
  --interval 300 \
  --label-enable
```

## macOS binaries

Release archives for running watchtower directly on macOS (`watchtower_darwin_arm64.tar.gz` and `watchtower_darwin_amd64.tar.gz`) are included starting with [GitHub releases](https://github.com/shouyupedia/watchtower/releases) published after `v1.0.0`. See [macOS binaries](docs/usage-overview.md#macos-binaries) for verification and first-run instructions.

## Verify a release

Download the archive and checksum file for the same version. Starting with `v1.0.1`, verify their GitHub build provenance before extracting the binary. Get the full commit SHA for the release tag from the official repository. If you have the tag locally, use `git rev-parse 'refs/tags/v1.0.1^{commit}'`. Use the filenames and tag for your platform and version. [GitHub releases](https://github.com/shouyupedia/watchtower/releases)

```bash
repo=shouyupedia/watchtower
release_sha='<full commit SHA for v1.0.1>'
for file in watchtower_darwin_arm64.tar.gz watchtower_1.0.1_checksums.txt; do
  gh attestation verify "$file" --repo "$repo" \
    --signer-workflow "$repo/.github/workflows/release.yml" \
    --source-digest "$release_sha" --deny-self-hosted-runners || exit 1
done
```

After authenticating to GHCR, verify the versioned image by its `sha256:` digest. You can find the digest with `docker buildx imagetools inspect ghcr.io/$repo:1.0.1`. Use the same digest when pulling the verified image.

```bash
gh attestation verify "oci://ghcr.io/$repo@<sha256:digest>" --repo "$repo" \
  --signer-workflow "$repo/.github/workflows/docker-publish.yml" \
  --source-digest "$release_sha" --deny-self-hosted-runners
```

### Resume an interrupted release

If archives or an image still need building, use **Re-run jobs** on the original scheduled or tag-push run in GitHub Actions. A rerun keeps the original commit SHA used by GitHub to sign provenance. A new run from a newer `main` can resume a draft whose archives and image are already recorded with valid attestations, or finish promoting a published release. If the original run is unavailable and a build is still needed, leave the reservation in place for maintainer review.
