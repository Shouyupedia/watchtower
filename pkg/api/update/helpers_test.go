package update_test

import (
	"context"
	"net/http"
)

func contextWithCancel(req *http.Request) (*http.Request, context.CancelFunc) {
	ctx, cancel := context.WithCancel(req.Context())
	return req.WithContext(ctx), cancel
}
