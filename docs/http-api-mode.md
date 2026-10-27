Watchtower provides an HTTP API mode that enables an HTTP endpoint that can be requested to trigger container updating. The current available endpoint list is:

-   `POST /v1/update` - triggers an update for all of the containers monitored by this Watchtower instance.

!!! warning "GET is deprecated"
    `GET /v1/update` is still accepted for backwards compatibility, but is deprecated and will be removed in a future release. Use `POST`. Other methods are rejected with `405`.

The update runs synchronously, and the request returns `200` once it has finished. If an update is already running, a request without an image filter does not start a second one and returns `202`. A request for specific images waits for its turn (at most 10 may be waiting, further ones get `429`). Request bodies are ignored and limited to 1 MiB.

---

To enable this mode, use the flag `--http-api-update`. For example, in a Docker Compose config file:

```yaml
version: '3'

services:
  app-monitored-by-watchtower:
    image: myapps/monitored-by-watchtower
    labels:
      - "com.centurylinklabs.watchtower.enable=true"

  watchtower:
    image: containrrr/watchtower
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    command: --debug --http-api-update
    environment:
      - WATCHTOWER_HTTP_API_TOKEN=mytoken
    labels:
      - "com.centurylinklabs.watchtower.enable=false"
    ports:
      - 8080:8080
```

By default, enabling this mode prevents periodic polls (i.e. what is specified using `--interval` or `--schedule`). To run periodic updates regardless, pass `--http-api-periodic-polls`.

Notice that there is an environment variable named WATCHTOWER_HTTP_API_TOKEN. To prevent external services from accidentally triggering image updates, all of the requests have to contain a "Token" field, valued as the token defined in WATCHTOWER_HTTP_API_TOKEN, in their headers. In this case, there is a port bind to the host machine, allowing to request localhost:8080 to reach Watchtower. The following `curl` command would trigger an image update:

```bash
curl -X POST -H "Authorization: Bearer mytoken" localhost:8080/v1/update
```

---

In order to update only certain images, the image names can be provided as URL query parameters. The following `curl` command would trigger an update for the images `foo/bar` and `foo/baz`:

```bash
curl -X POST -H "Authorization: Bearer mytoken" "localhost:8080/v1/update?image=foo/bar,foo/baz"
```

---

The API listens on `:8080` by default. Use `--http-api-listen` to change the address (for example `127.0.0.1:8080`), and `--http-api-tls-cert` together with `--http-api-tls-key` to serve it over HTTPS.
