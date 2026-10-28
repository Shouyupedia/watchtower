package auth_test

import (
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/containrrr/watchtower/pkg/registry/auth"
	ref "github.com/distribution/reference"
)

type tokenTransport func(*http.Request) (*http.Response, error)

func (fn tokenTransport) RoundTrip(r *http.Request) (*http.Response, error) { return fn(r) }

type tokenBody struct {
	io.Reader
	closed bool
}

func (b *tokenBody) Close() error { b.closed = true; return nil }

type brokenTokenBody struct{}

func (brokenTokenBody) Read([]byte) (int, error) { return 0, errors.New("broken response") }

func TestAuthRealmValidation(t *testing.T) {
	image, _ := ref.ParseNormalizedNamed("example.com/team/image")
	for _, realm := range []string{
		"https://%zz/token", "http://example.com/token", "/token",
		"https:///token", "https://user@example.com/token", "https://example.com/token#fragment",
	} {
		t.Run(realm, func(t *testing.T) {
			if u, err := auth.GetAuthURL(`Bearer realm="`+realm+`",service="Registry"`, image); err == nil || u != nil {
				t.Fatalf("invalid realm accepted: %v, %v", u, err)
			}
		})
	}
	u, err := auth.GetAuthURL(`Bearer Realm="https://example.com/Token?key=AbC&scope=old",Service="Registry"`, image)
	if err != nil {
		t.Fatal(err)
	}
	if u.Path != "/Token" || u.Query().Get("key") != "AbC" || u.Query().Get("service") != "Registry" {
		t.Fatalf("case-sensitive realm values changed: %s", u)
	}
	if scopes := u.Query()["scope"]; len(scopes) != 1 || scopes[0] != "repository:team/image:pull" {
		t.Fatalf("incorrect scope: %v", scopes)
	}
}

func TestBearerResponseHandling(t *testing.T) {
	image, _ := ref.ParseNormalizedNamed("example.com/team/image")
	original := http.DefaultTransport
	t.Cleanup(func() { http.DefaultTransport = original })
	for _, tc := range []struct {
		name   string
		status int
		body   io.Reader
		valid  bool
	}{
		{"valid", http.StatusOK, strings.NewReader(`{"token":"AbC"}`), true},
		{"empty", http.StatusOK, strings.NewReader(`{"token":""}`), false},
		{"invalid-json", http.StatusOK, strings.NewReader(`not json`), false},
		{"error-status", http.StatusUnauthorized, strings.NewReader(`{"token":"AbC"}`), false},
		{"oversized", http.StatusOK, strings.NewReader(strings.Repeat("x", 1024*1024+1)), false},
		{"read-error", http.StatusOK, brokenTokenBody{}, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			body := &tokenBody{Reader: tc.body}
			http.DefaultTransport = tokenTransport(func(r *http.Request) (*http.Response, error) {
				if r.Header.Get("Authorization") != "Basic dummy" {
					t.Fatal("missing credentials")
				}
				return &http.Response{StatusCode: tc.status, Status: http.StatusText(tc.status), Body: body, Header: make(http.Header)}, nil
			})
			token, err := auth.GetBearerHeader(`Bearer realm="https://example.com/Token",service="Registry"`, image, "dummy")
			if (err == nil) != tc.valid || (tc.valid && token != "Bearer AbC") {
				t.Fatalf("unexpected result: %q, %v", token, err)
			}
			if !body.closed {
				t.Fatal("token response body not closed")
			}
		})
	}
}
