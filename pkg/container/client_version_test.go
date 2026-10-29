package container

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	sdkClient "github.com/moby/moby/client"
)

func TestNewClientAPIVersion(t *testing.T) {
	tests := []struct {
		name          string
		daemonVersion string
		envVersion    string
		wantPath      string
	}{
		{"negotiates up to a current daemon's version", "1.52", "", "/v1.52/containers/json"},
		{"negotiates down to an older daemon's version", "1.41", "", "/v1.41/containers/json"},
		{"uses the version from DOCKER_API_VERSION as is", "1.52", "1.44", "/v1.44/containers/json"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var requested string
			daemon := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path == "/_ping" {
					w.Header().Set("API-Version", tt.daemonVersion)
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
			if _, err := client.api.ContainerList(context.Background(), sdkClient.ContainerListOptions{}); err != nil {
				t.Fatalf("listing containers: %v", err)
			}
			if requested != tt.wantPath {
				t.Fatalf("requested %q, want %q", requested, tt.wantPath)
			}
		})
	}
}
