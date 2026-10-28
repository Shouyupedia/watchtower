package registry

import (
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/docker/cli/cli/config/types"
)

func TestEnvironmentCredentialsAreRegistryScoped(t *testing.T) {
	t.Setenv("REPO_USER", "dummy-user")
	t.Setenv("REPO_PASS", "dummy-pass")
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "config.json"), []byte(`{"auths":{}}`), 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("DOCKER_CONFIG", dir)
	for _, tc := range []struct {
		host  string
		image string
		want  bool
	}{
		{"", "containrrr/watchtower", true},
		{"", "other.example/team/image", false},
		{"registry.example:5000", "registry.example:5000/team/image", true},
		{"registry.example:5000", "registry.example/team/image", false},
		{"registry.example", "sub.registry.example/team/image", false},
		{"registry.example", "containrrr/watchtower", false},
		{"docker.io", "containrrr/watchtower", true},
	} {
		t.Run(tc.host+"/"+tc.image, func(t *testing.T) {
			t.Setenv("REPO_HOST", tc.host)
			auth, err := EncodedAuth(tc.image)
			if err != nil {
				t.Fatal(err)
			}
			if (auth != "") != tc.want {
				t.Fatalf("unexpected credentials for %s", tc.image)
			}
			if tc.want {
				data, err := base64.URLEncoding.DecodeString(auth)
				if err != nil {
					t.Fatal(err)
				}
				var config types.AuthConfig
				if err := json.Unmarshal(data, &config); err != nil || config.Username != os.Getenv("REPO_USER") || config.Password != os.Getenv("REPO_PASS") {
					t.Fatal("unexpected encoded credentials")
				}
			}
		})
	}
}
