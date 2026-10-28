# Audit lead review and remediation

This is a source-level remediation record for the 22 unvalidated proposals in
the supplied incomplete audit. It is not a completed security audit, independent
verification, or a claim of demonstrated exploitation. Original audit files were
not changed. No severity is assigned here.

## Source changes

| Original lead | Disposition |
| --- | --- |
| HTTP auth realm credentials | Require an absolute HTTPS realm without user info or fragment; reject cross-origin redirects. |
| Environment credentials sent to every registry | Scope environment credentials to Docker Hub or explicit `REPO_HOST`; otherwise use registry-specific Docker config. |
| Ignored realm parse error | Return parse and validation errors before accessing the URL. Preserve case-sensitive realm values. |
| Unbounded token response | Limit token responses to 1 MiB, check status/read/JSON/empty-token errors, and close the body. |
| Registry requests without a deadline | Apply a 30-second total timeout to challenge, token, and digest requests. |
| Digest TLS peer verification disabled | Use the standard verifying HTTP transport, with bounded same-origin redirects. |
| Container name partial matching | Anchor the entire regular expression and allow only the optional leading slash. Handle empty names safely. |
| Exec timeout starts after output read | Start one context deadline before exec creation; propagate it through attach/start/inspect and close attached I/O on cancellation. Retain at most 1 MiB of output. |
| Post-update timeout accessed before inspect error | Check inspection errors before dereferencing the container. |
| SMTP verification flag disables encryption | Reject the unsupported legacy TLS exception instead of creating an unencrypted SMTP URL. Normal configuration retains encryption defaults. |
| API token comparison timing hypothesis | Harden comparison using fixed-size SHA-256 digests and constant-time equality; no feasible token-recovery claim is made. Empty configured tokens are rejected. |
| API pending updates and cancellation | Limit image-specific admission to 16 pending/running requests per handler, honor cancellation before execution, and keep locks per handler. Busy unfiltered scans still skip as before. |
| API unbounded body/stdout copy | Discard rather than log bodies, limit them to 64 KiB, return 413 on overflow, and set server header/read/idle limits. |
| Published development revision differs | Build the checked-out source rather than cloning mutable remote `main`. Fetch full history for version metadata in the publishing job. The image build itself was not run. |
| Nil report after dependency/listing failure | Return a non-nil report on both failure paths. Dependency cycles mark the batch skipped and return an error without restarting containers. |
| Preview errors inserted into HTML | Insert errors as text. Query parameters can populate the template, so this is not dismissed as manual self-input only. Browser exploit execution was not performed. |
| Dependency cycle aborts batch | Consolidated with the nil-report root cause. Keep the safe batch abort rather than guessing a valid dependency order. |
| Runtime user cleared when equal to image default | Preserve the running user, including when it equals the old image default. Docker inspection does not reveal whether it was explicitly set. |

## Remaining or conditional leads

| Original lead | Disposition |
| --- | --- |
| Workload label impersonates another Watchtower | Unchanged. Labels are the existing instance-discovery mechanism; workload-label authority and Docker administrative authority need deployment-specific review. Safely changing identity/cleanup requires a separate compatibility design, not just another spoofable label or image-name check. |
| HTTP API cleartext bearer transport | Conditional on topology. The listener remains HTTP for reverse-proxy deployments. Document TLS termination/private networking and bind sample published ports to loopback. Verify actual exposure with the deployment owner. |
| Blocking HTTP API skips signal/update drain | Unchanged robustness issue. API-only shutdown still needs lifecycle design and tests. No separate lower-trust signal authority was established. |
| Public demo token | Not a secret disclosure by itself. Mark examples as local-only and bind the API example to loopback. Replace the matching Watchtower/Prometheus tokens before deployment; example changes do not secure existing deployments. |

## Compatibility notes

- Non-Docker-Hub environment authentication now requires `REPO_HOST=host[:port]`.
  Docker config credentials remain scoped per registry.
- Self-signed registry certificates must be trusted by the process. HTTP token
  realms and cross-origin redirects are rejected; providers using redirects must
  expose a supported endpoint. Digest failures retain the existing pull fallback.
- Legacy email `tls-skip-verify=true` now fails configuration. Configure trusted
  certificates rather than relying on that option to remove encryption.
- User identity no longer follows a changed image default automatically. A new
  image must support the existing user or the operator must explicitly recreate
  the workload with the intended identity.
- The self-contained Dockerfile now builds its local context; the previous
  `WATCHTOWER_VERSION` clone selector is no longer used. Check out the desired
  revision before building.

## Validation performed and gaps

- `git diff --check` passed.
- Release workflow and sample configuration YAML parsed successfully.
- All three inline preview script blocks passed `node --check` without execution.
- Source assertions verified removal of disabled digest verification, the unsafe
  error HTML sink, stdout request copying, and the mutable release-source clone.
- Added regression coverage for realm validation/case handling, token response
  closure/limits/errors, redirect policy, untrusted TLS rejection, credential
  scope, exact name matching, hook timeout I/O, inspection failure, preserved
  user identity, dependency/listing failures, API bounds/cancellation/lock
  isolation, empty API tokens, and SMTP configuration.
- A Go toolchain (checksum-verified) and a read-only module cache were set up
  inside WSL, outside the repository. Tests ran under `bwrap` with all
  namespaces unshared, loopback-only networking, a read-only source tree, a
  tmpfs `/tmp`, a separate scratch directory, and `prlimit` CPU/memory/process/
  file-size bounds. No live registry credentials or external endpoints were used.
- `go vet ./...`, `go test ./...` and `go test -race -count=1 ./...` passed in
  that environment, and the new security regression tests were run explicitly
  with `-v` and passed.
- `go test -count=3 ./internal/actions` fails with Ginkgo v1 ("You may only call
  It from within a Describe..."). The same failure occurs on unmodified `HEAD`,
  so it is a runner limitation of repeated runs, not a regression.
- Changed Go files are gofmt-clean after normalising CRLF working-tree line
  endings, except `pkg/api/api_test.go` (pre-existing spacing issues also
  present at `HEAD`); `pkg/types/update_params.go` is also unformatted at `HEAD`
  and was left untouched to keep the diff focused.
- `pkg/api/api_test.go` is now gofmt-clean (checked after LF normalisation).
- Docker is not available inside WSL and a real image build needs network
  access for the base images, so no image was built. Instead the builder
  stage's compile and test steps (static `CGO_ENABLED=0` build with the
  version ldflag, then `go test ./...`) were reproduced in the offline sandbox:
  the build produced a statically linked ELF binary and all tests passed.
- The committed binary and live registry/SMTP endpoints were not executed; the
  Dockerfile `FROM`/`COPY` stages and the workflow changes are verified by
  inspection and YAML parsing only.
- The original audit validator/coverage blockers and independent verification
  requirements remain unresolved; this patch does not complete that audit.
