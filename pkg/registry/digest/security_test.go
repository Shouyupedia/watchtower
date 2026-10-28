package digest_test

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/containrrr/watchtower/pkg/registry/digest"
)

func TestDigestRejectsUntrustedTLS(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Error("credentials reached a server with an untrusted certificate")
		w.Header().Set(digest.ContentDigestHeader, "sha256:dummy")
	}))
	defer server.Close()
	if _, err := digest.GetDigest(server.URL, "Bearer dummy"); err == nil {
		t.Fatal("untrusted TLS certificate accepted")
	}
}

func TestDigestRejectsInvalidURL(t *testing.T) {
	if _, err := digest.GetDigest("https://%zz/", "Bearer dummy"); err == nil {
		t.Fatal("invalid URL accepted")
	}
}
