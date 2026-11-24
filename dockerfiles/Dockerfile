FROM --platform=$BUILDPLATFORM golang:1.26.11-alpine@sha256:ecb67f987aa1a57794c91d19ade7ae0209f2f0f5496aaef9e6ae85ddf92d02ab AS builder

ARG TARGETOS
ARG TARGETARCH
ARG TARGETVARIANT
# use a git tag or `git describe --tags` output, for example "v2.0.0"
ARG WATCHTOWER_VERSION=dev

RUN apk add --no-cache \
    ca-certificates \
    tzdata

WORKDIR /watchtower
COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH GOARM=${TARGETVARIANT#v} \
    go build -o /out/watchtower \
    -ldflags "-s -w -X github.com/containrrr/watchtower/internal/meta.Version=$WATCHTOWER_VERSION" .

FROM scratch
LABEL "com.centurylinklabs.watchtower"="true"

COPY --from=builder \
    /etc/ssl/certs/ca-certificates.crt \
    /etc/ssl/certs/ca-certificates.crt
COPY --from=builder \
    /usr/share/zoneinfo \
    /usr/share/zoneinfo

EXPOSE 8080

COPY --from=builder /out/watchtower /

HEALTHCHECK CMD [ "/watchtower", "--health-check"]

ENTRYPOINT ["/watchtower"]
