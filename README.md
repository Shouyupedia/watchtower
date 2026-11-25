## Acknowledgements

This project is a fork of [Watchtower](https://github.com/containrrr/watchtower).

**My utmost respect and deepest gratitude go to the original Watchtower project and all its maintainers and contributors.**

Their outstanding work laid the foundation for this project. None of this would have been possible without their dedication and contributions.

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
