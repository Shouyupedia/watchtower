# Securing watchtower

Watchtower can create, stop and delete containers, so whoever controls it (or the Docker socket it uses) controls the host. This page collects the ways to reduce that exposure.

## Verify what you run

Release images and archives are built by the release workflow, and signed with [cosign](https://github.com/sigstore/cosign) using the workflow's identity (keyless). Archives also come with an SBOM.

```bash
# Verify an image
cosign verify ghcr.io/shouyupedia/watchtower:<version> \
  --certificate-identity-regexp '^https://github.com/shouyupedia/watchtower/\.github/workflows/release\.yml@' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com

# Verify the checksums of a release archive, then the archive itself
cosign verify-blob checksums.txt --bundle checksums.txt.sigstore.json \
  --certificate-identity-regexp '^https://github.com/shouyupedia/watchtower/\.github/workflows/release\.yml@' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
sha256sum --check --ignore-missing checksums.txt
```

Prefer pinning the image by version or digest over `latest` if you do not want to follow new releases unattended.

## Do not hand out the Docker socket

Mounting `/var/run/docker.sock` gives root on the host to anything in the container. A socket proxy limits the API calls that get through. This example uses [docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy):

```yaml
services:
  socket-proxy:
    image: tecnativa/docker-socket-proxy
    environment:
      CONTAINERS: 1
      IMAGES: 1
      NETWORKS: 1
      EXEC: 1       # only needed for lifecycle hooks
      POST: 1
      DELETE: 1
      ALLOW_START: 1
      ALLOW_STOP: 1
      ALLOW_RESTARTS: 1
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    networks: [watchtower]

  watchtower:
    image: ghcr.io/shouyupedia/watchtower
    environment:
      DOCKER_HOST: tcp://socket-proxy:2375
    networks: [watchtower]

networks:
  watchtower:
    internal: true
```

!!! note
    The set of permissions above follows the proxy's documentation and has not been exercised against every watchtower feature. Test it with `--run-once --debug` and loosen what your setup needs. Watchtower itself needs to reach the registries, so the network it uses for that cannot be `internal` unless a registry mirror is available on it.

## HTTP API

-   Always set `--http-api-token` to a long random value. It can be a path to a file (for example a Docker secret).
-   Bind the API to where it is needed: `--http-api-listen 127.0.0.1:8080`, or an address on a private network.
-   The token travels in the `Authorization` header, so serve the API over TLS with `--http-api-tls-cert` and `--http-api-tls-key`, or put a TLS terminating proxy in front of it.
-   Updates are triggered with `POST /v1/update`. Request bodies are ignored and limited to 1 MiB.

## Registries

Watchtower verifies the TLS certificates of registries, both for its own requests and (through the Docker daemon) for pulls. Only use `--registry-tls-skip-verify` for registries with self-signed certificates that you cannot add to the trust store, and prefer fixing the trust store: the flag also affects the credentials sent to the registry's token server. Token servers must be served over HTTPS.

Credentials passed through `REPO_USER` and `REPO_PASS`, or a Docker `config.json`, are sent to the registry that hosts the image and to the token server it names.

## Notifications

E-mail notifications always use TLS (implicit TLS on port 465, STARTTLS otherwise) and the server certificate is always verified.

Notification URLs and tokens can contain secrets. They are visible in the container's environment, so prefer passing them as files (Docker secrets) where the option supports it. The startup message only lists the names of the notification services, never their URLs. Trace logging, however, prints sensitive information such as registry credentials and tokens: never leave `--trace` enabled in production.

## Lifecycle hooks

Hooks run commands inside your containers with `sh -c`. They are set through container labels, so anyone who can create containers or edit labels can run code in the containers watchtower manages. Only enable `--enable-lifecycle-hooks` if that fits your trust model.
