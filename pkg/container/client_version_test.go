package container

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/docker/docker/api/types"
)

func TestNewClientAPIVersion(t *testing.T) {
	tests := []struct {
		name       string
		envVersion string
		wantPath   string
	}{
		{"negotiates with the daemon when no version is set", "", "/v1.41/containers/json"},
		{"uses the version from DOCKER_API_VERSION as is", "1.44", "/v1.44/containers/json"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var requested string
			daemon := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path == "/_ping" {
					w.Header().Set("API-Version", "1.41")
					return
				}
				requested = r.URL.Path
				_, _ = w.Write([]byte("[]"))
			}))
			defer daemon.Close()

			t.Setenv("DOCKER_HOST", daemon.URL)
			t.Setenv("DOCKER_TLS_VERIFY", "")
			t.Setenv("DOCKER_CERT_PATH", "")
			t.Setenv("DOCKER_API_VERSION", tt.envVersion)

			client := NewClient(ClientOptions{}).(dockerClient)
			if _, err := client.api.ContainerList(context.Background(), types.ContainerListOptions{}); err != nil {
				t.Fatalf("listing containers: %v", err)
			}
			if requested != tt.wantPath {
				t.Fatalf("requested %q, want %q", requested, tt.wantPath)
			}
		})
	}
}
