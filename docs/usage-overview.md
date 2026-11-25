Watchtower is itself packaged as a Docker container so installation is as simple as pulling the `ghcr.io/shouyupedia/watchtower` image from the GitHub Container Registry (GHCR). The published tags are multi-arch manifests covering `linux/amd64`, `linux/386`, `linux/arm/v6` and `linux/arm64`, so Docker automatically selects the image matching your host's architecture.

Since the watchtower code needs to interact with the Docker API in order to monitor the running containers, you need to mount _/var/run/docker.sock_ into the container with the `-v` flag when you run it.

Run the `watchtower` container with the following command:

```bash
docker run -d \
  --name watchtower \
  -v /var/run/docker.sock:/var/run/docker.sock \
  ghcr.io/shouyupedia/watchtower
```

If pulling images from private Docker registries, supply registry authentication credentials with the environment variables `REPO_USER` and `REPO_PASS`.
These credentials apply only to Docker Hub unless `REPO_HOST` is set to the exact registry `host[:port]`.
Alternatively, mount the host's docker config file into the container (at the root of the container filesystem `/`).

Passing environment variables:

```bash
docker run -d \
  --name watchtower \
  -e REPO_USER=username \
  -e REPO_PASS=password \
  -v /var/run/docker.sock:/var/run/docker.sock \
  ghcr.io/shouyupedia/watchtower container_to_watch --debug
```

Also check out [this Stack Overflow answer](https://stackoverflow.com/a/30494145/7872793) for more options on how to pass environment variables.

Alternatively if you 2FA authentication setup on Docker Hub then passing username and password will be insufficient.  Instead you can run `docker login` to store your credentials in `$HOME/.docker/config.json` and then mount this config file to make it available to the Watchtower container:

```bash
docker run -d \
  --name watchtower \
  -v $HOME/.docker/config.json:/config.json \
  -v /var/run/docker.sock:/var/run/docker.sock \
  ghcr.io/shouyupedia/watchtower container_to_watch --debug
```

!!! note "Changes to config.json while running"
    If you mount `config.json` in the manner above, changes from the host system will (generally) not be propagated to the
    running container. Mounting files into the Docker daemon uses bind mounts, which are based on inodes. Most
    applications (including `docker login` and `vim`) will not directly edit the file, but instead make a copy and replace
    the original file, which results in a new inode which in turn _breaks_ the bind mount.  
    **As a workaround**, you can create a symlink to your `config.json` file and then mount the symlink in the container. 
    The symlinked file will always have the same inode, which keeps the bind mount intact and will ensure changes
    to the original file are propagated to the running container (regardless of the inode of the source file!).

If you mount the config file as described above, be sure to also prepend the URL for the registry when starting up your
watched image (you can omit the https://). Here is a complete docker-compose.yml file that starts up a docker container
from a private repo on the GitHub Registry and monitors it with watchtower. Note the command argument changing the interval
to 30s rather than the default 24 hours.

```yaml
version: "3"
services:
  cavo:
    image: ghcr.io/<org>/<image>:<tag>
    ports:
      - "443:3443"
      - "80:3080"
  watchtower:
    image: ghcr.io/shouyupedia/watchtower
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /root/.docker/config.json:/config.json
    command: --interval 30
```

## macOS binaries

As an alternative to running the container image, macOS binary archives are included starting with releases published after `v1.0.0`. Download the archive for your Mac from the [Releases page](https://github.com/shouyupedia/watchtower/releases):

- `watchtower_darwin_arm64.tar.gz` for Apple Silicon Macs
- `watchtower_darwin_amd64.tar.gz` for Intel Macs

Both archives contain the `watchtower` executable and `LICENSE.md`. The binaries require **macOS 12 Monterey or newer**, the minimum supported by the Go 1.26 toolchain used for these builds (see the [Go 1.26 release notes](https://go.dev/doc/go1.26)). This minimum follows the Go toolchain: Go 1.27 and later will require macOS 13 Ventura or newer.

Releases published by the automated release pipeline will include a GitHub build provenance attestation; that pipeline is being added in PR 5, so existing releases (including `v1.0.0`) do not have one yet. When an attestation is available, verify it before extracting the archive:

```bash
gh attestation verify watchtower_darwin_arm64.tar.gz --repo shouyupedia/watchtower
```

Then extract the archive and run the binary:

```bash
tar -xzf watchtower_darwin_arm64.tar.gz
./watchtower --help
```

The binary still needs a Docker daemon. If the daemon is not reachable at its default endpoint, pass `--host` or set `DOCKER_HOST` as described in [Remote hosts](remote-hosts.md).

The macOS binaries are not signed with a Developer ID certificate and are not notarized: the Apple Silicon (`darwin/arm64`) binary carries only the ad-hoc signature that the Go linker adds, and the Intel (`darwin/amd64`) binary is not code-signed. macOS Gatekeeper quarantines files downloaded by a browser, so the first run can be blocked. If that happens, remove the quarantine attribute and run the binary again:

```bash
xattr -d com.apple.quarantine ./watchtower
./watchtower --help
```

!!! warning
    Removing the quarantine attribute bypasses Gatekeeper's check. Only do so for an archive you trust, after verifying its provenance attestation when the release provides one.
