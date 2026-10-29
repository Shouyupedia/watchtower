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
