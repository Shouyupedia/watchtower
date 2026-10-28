package httpclient

import (
	"fmt"
	"net/http"
	"time"
)

// New bounds registry requests and keeps redirects on the original origin.
func New() *http.Client {
	return &http.Client{
		Timeout: 30 * time.Second,
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			if len(via) >= 10 {
				return fmt.Errorf("too many registry redirects")
			}
			original := via[0].URL
			if req.URL.Scheme != original.Scheme || req.URL.Host != original.Host {
				return fmt.Errorf("registry redirect changes origin")
			}
			return nil
		},
	}
}
