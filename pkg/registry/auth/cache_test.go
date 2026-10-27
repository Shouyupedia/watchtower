package auth

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	ref "github.com/distribution/reference"
	"github.com/shouyupedia/watchtower/pkg/registry/httpclient"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func freezeTime(t *testing.T) *time.Time {
	t.Helper()
	current := time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)
	now = func() time.Time { return current }
	t.Cleanup(func() { now = time.Now; ResetCache() })
	ResetCache()
	return &current
}

func TestTTLCacheExpires(t *testing.T) {
	current := freezeTime(t)
	var c ttlCache

	c.put("key", "value", time.Minute)
	v, ok := c.get("key")
	assert.True(t, ok)
	assert.Equal(t, "value", v)

	*current = current.Add(59 * time.Second)
	_, ok = c.get("key")
	assert.True(t, ok)

	*current = current.Add(2 * time.Second)
	_, ok = c.get("key")
	assert.False(t, ok, "entries must not outlive their ttl")
}

func TestTTLCacheIgnoresNonPositiveTTL(t *testing.T) {
	var c ttlCache
	c.put("key", "value", 0)
	c.put("key", "value", -time.Second)
	_, ok := c.get("key")
	assert.False(t, ok)
}

func TestTokenTTL(t *testing.T) {
	assert.Equal(t, 30*time.Second, tokenTTL(0), "unknown lifetimes default to the 60s of the specification")
	assert.Equal(t, 270*time.Second, tokenTTL(300))
	assert.Equal(t, maxTokenTTL, tokenTTL(86400), "long lived tokens are not kept forever")
	assert.LessOrEqual(t, tokenTTL(20), time.Duration(0), "tokens about to expire are not reused")
}

type tokenServer struct {
	*httptest.Server
	requests atomic.Int32
	expires  int
}

func newTokenServer(t *testing.T, expiresIn int) *tokenServer {
	t.Helper()
	ts := &tokenServer{expires: expiresIn}
	ts.Server = httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := ts.requests.Add(1)
		_, _ = fmt.Fprintf(w, `{"token":"t%d","expires_in":%d}`, n, ts.expires)
	}))
	httpclient.SetInsecureSkipVerify(true)
	t.Cleanup(func() {
		httpclient.SetInsecureSkipVerify(false)
		ts.Close()
	})
	return ts
}

func bearer(t *testing.T, ts *tokenServer, image, creds string) string {
	t.Helper()
	imageRef, err := ref.ParseNormalizedNamed(image)
	require.NoError(t, err)
	challenge := fmt.Sprintf(`Bearer realm="%s/token",service="registry"`, ts.URL)
	header, err := GetBearerHeader(context.Background(), challenge, imageRef, creds)
	require.NoError(t, err)
	return header
}

func TestTokensAreReusedUntilTheyExpire(t *testing.T) {
	current := freezeTime(t)
	ts := newTokenServer(t, 300)

	first := bearer(t, ts, "ghcr.io/user/image", "")
	second := bearer(t, ts, "ghcr.io/user/image", "")

	assert.Equal(t, first, second)
	assert.EqualValues(t, 1, ts.requests.Load(), "the second lookup must be served from the cache")

	*current = current.Add(10 * time.Minute)
	third := bearer(t, ts, "ghcr.io/user/image", "")
	assert.NotEqual(t, first, third)
	assert.EqualValues(t, 2, ts.requests.Load())
}

func TestTokensAreKeptApartPerScopeAndCredentials(t *testing.T) {
	freezeTime(t)
	ts := newTokenServer(t, 300)

	a := bearer(t, ts, "ghcr.io/user/one", "creds-a")
	b := bearer(t, ts, "ghcr.io/user/two", "creds-a")
	c := bearer(t, ts, "ghcr.io/user/one", "creds-b")

	assert.EqualValues(t, 3, ts.requests.Load())
	assert.NotEqual(t, a, b, "different repositories need different tokens")
	assert.NotEqual(t, a, c, "a token must never be handed to someone with other credentials")
}

func TestShortLivedTokensAreNotCached(t *testing.T) {
	freezeTime(t)
	ts := newTokenServer(t, 20)

	bearer(t, ts, "ghcr.io/user/image", "")
	bearer(t, ts, "ghcr.io/user/image", "")

	assert.EqualValues(t, 2, ts.requests.Load())
}
