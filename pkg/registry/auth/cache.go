package auth

import (
	"crypto/sha256"
	"encoding/hex"
	"sync"
	"time"
)

const (
	// challengeTTL is how long the challenge a registry responded with is remembered. It only names the token
	// server, which hardly ever changes.
	challengeTTL = 10 * time.Minute
	// defaultTokenLifetime is what the registry token specification assumes if the token server does not say
	defaultTokenLifetime = 60 * time.Second
	// tokenExpiryMargin makes sure a token is replaced before it expires while a request using it is in flight
	tokenExpiryMargin = 30 * time.Second
	// maxTokenTTL puts a ceiling on how long a token is reused, whatever the token server claims
	maxTokenTTL = 5 * time.Minute
	// pruneThreshold is the number of entries after which expired ones are dropped
	pruneThreshold = 512
)

// now can be replaced in tests
var now = time.Now

type cacheEntry struct {
	value   string
	expires time.Time
}

// ttlCache is a small cache of strings that expire. Watchtower checks many containers on every run, most of them
// against the same few registries, so remembering challenges and tokens saves two thirds of the requests.
type ttlCache struct {
	mu      sync.Mutex
	entries map[string]cacheEntry
}

func (c *ttlCache) get(key string) (string, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	entry, found := c.entries[key]
	if !found || !now().Before(entry.expires) {
		delete(c.entries, key)
		return "", false
	}
	return entry.value, true
}

func (c *ttlCache) put(key, value string, ttl time.Duration) {
	if ttl <= 0 {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.entries == nil {
		c.entries = map[string]cacheEntry{}
	}
	if len(c.entries) >= pruneThreshold {
		for k, e := range c.entries {
			if !now().Before(e.expires) {
				delete(c.entries, k)
			}
		}
	}
	c.entries[key] = cacheEntry{value: value, expires: now().Add(ttl)}
}

func (c *ttlCache) clear() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.entries = nil
}

var (
	challenges ttlCache
	tokens     ttlCache
)

// ResetCache forgets all remembered challenges and tokens
func ResetCache() {
	challenges.clear()
	tokens.clear()
}

// tokenCacheKey identifies a token. The credentials are part of it, so that nobody gets a token they didn't ask
// for, but only their hash is kept.
func tokenCacheKey(authURL, registryAuth string) string {
	sum := sha256.Sum256([]byte(authURL + "\x00" + registryAuth))
	return hex.EncodeToString(sum[:])
}

// tokenTTL works out for how long a token with the given lifetime (in seconds, 0 if unknown) may be reused
func tokenTTL(expiresIn int) time.Duration {
	lifetime := time.Duration(expiresIn) * time.Second
	if lifetime <= 0 {
		lifetime = defaultTokenLifetime
	}
	return min(lifetime-tokenExpiryMargin, maxTokenTTL)
}
