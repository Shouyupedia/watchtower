package httpclient

import (
	"net/http"
	"testing"
	"time"
)

func TestClientPolicy(t *testing.T) {
	client := New()
	if client.Timeout != 30*time.Second {
		t.Fatal("registry requests must have a total deadline")
	}
	original, _ := http.NewRequest("GET", "https://registry.example/token", nil)
	for _, tc := range []struct {
		url     string
		allowed bool
	}{
		{"https://registry.example/next", true},
		{"http://registry.example/next", false},
		{"https://sub.registry.example/next", false},
		{"https://other.example/next", false},
		{"https://registry.example:444/next", false},
	} {
		t.Run(tc.url, func(t *testing.T) {
			req, _ := http.NewRequest("GET", tc.url, nil)
			err := client.CheckRedirect(req, []*http.Request{original})
			if (err == nil) != tc.allowed {
				t.Fatalf("unexpected redirect decision: %v", err)
			}
		})
	}
	via := make([]*http.Request, 10)
	via[0] = original
	if err := client.CheckRedirect(original, via); err == nil {
		t.Fatal("redirect loops must be bounded")
	}
}
