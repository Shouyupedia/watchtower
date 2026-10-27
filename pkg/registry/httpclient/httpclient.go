// Package httpclient provides the shared HTTP client used for talking to container registries.
package httpclient

import (
	"crypto/tls"
	"net/http"
	"sync/atomic"
	"time"
)

// RequestTimeout is the upper bound for a single registry request, including reading the body
const RequestTimeout = 30 * time.Second

var (
	insecure atomic.Bool
	client   atomic.Pointer[http.Client]
)

func init() {
	client.Store(build(false))
}

// SetInsecureSkipVerify controls whether TLS certificates presented by registries are verified.
// It is meant to be called once during start-up; certificate verification is enabled by default.
func SetInsecureSkipVerify(skip bool) {
	insecure.Store(skip)
	client.Store(build(skip))
}

// InsecureSkipVerify returns true if certificate verification for registries has been disabled
func InsecureSkipVerify() bool {
	return insecure.Load()
}

// Get returns the shared registry HTTP client
func Get() *http.Client {
	return client.Load()
}

func build(skipVerify bool) *http.Client {
	tr := http.DefaultTransport.(*http.Transport).Clone()
	tr.TLSClientConfig = &tls.Config{
		MinVersion:         tls.VersionTLS12,
		InsecureSkipVerify: skipVerify, //nolint:gosec // opt-in through --registry-tls-skip-verify
	}
	tr.MaxIdleConnsPerHost = 4
	return &http.Client{Transport: tr, Timeout: RequestTimeout}
}
