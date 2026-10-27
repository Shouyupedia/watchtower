package auth

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strings"

	ref "github.com/distribution/reference"
	log "github.com/shouyupedia/watchtower/internal/logging"
	"github.com/shouyupedia/watchtower/pkg/registry/helpers"
	"github.com/shouyupedia/watchtower/pkg/registry/httpclient"
	"github.com/shouyupedia/watchtower/pkg/types"
)

// ChallengeHeader is the HTTP Header containing challenge instructions
const ChallengeHeader = "WWW-Authenticate"

// maxTokenResponseSize is the largest token response body that will be read from a registry
const maxTokenResponseSize = 1 << 20

// challengeParam matches a single key=value (or key="value") pair in a WWW-Authenticate header
var challengeParam = regexp.MustCompile(`(\w+)\s*=\s*(?:"([^"]*)"|([^,\s]*))`)

// GetToken fetches a token for the registry hosting the provided image
func GetToken(ctx context.Context, container types.Container, registryAuth string) (string, error) {
	normalizedRef, err := ref.ParseNormalizedNamed(container.ImageName())
	if err != nil {
		return "", err
	}

	URL := GetChallengeURL(normalizedRef)
	log.WithField("URL", URL.String()).Debug("Built challenge URL")

	v, cached := challenges.get(URL.Host)
	if cached {
		log.WithField("header", v).Debug("Using cached challenge")
	} else {
		if v, err = fetchChallenge(ctx, URL); err != nil {
			return "", err
		}
		if v != "" {
			challenges.put(URL.Host, v, challengeTTL)
		}
	}

	scheme, _, _ := strings.Cut(strings.TrimSpace(v), " ")
	switch strings.ToLower(scheme) {
	case "basic":
		if registryAuth == "" {
			return "", fmt.Errorf("no credentials available")
		}

		return fmt.Sprintf("Basic %s", registryAuth), nil
	case "bearer":
		return GetBearerHeader(ctx, v, normalizedRef, registryAuth)
	}

	return "", errors.New("unsupported challenge type from registry")
}

func fetchChallenge(ctx context.Context, URL url.URL) (string, error) {
	req, err := GetChallengeRequest(ctx, URL)
	if err != nil {
		return "", err
	}

	res, err := httpclient.Get().Do(req)
	if err != nil {
		return "", err
	}
	defer func() { _ = res.Body.Close() }()
	v := res.Header.Get(ChallengeHeader)

	log.WithFields(log.Fields{
		"status": res.Status,
		"header": v,
	}).Debug("Got response to challenge request")
	return v, nil
}

// GetChallengeRequest creates a request for getting challenge instructions
func GetChallengeRequest(ctx context.Context, URL url.URL) (*http.Request, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, URL.String(), nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "*/*")
	req.Header.Set("User-Agent", "Watchtower (Docker)")
	return req, nil
}

// GetBearerHeader tries to fetch a bearer token from the registry based on the challenge instructions
func GetBearerHeader(ctx context.Context, challenge string, imageRef ref.Named, registryAuth string) (string, error) {
	authURL, err := GetAuthURL(challenge, imageRef)
	if err != nil {
		return "", err
	}

	cacheKey := tokenCacheKey(authURL.String(), registryAuth)
	if header, found := tokens.get(cacheKey); found {
		log.Debug("Using cached token")
		return header, nil
	}

	var r *http.Request
	if r, err = http.NewRequestWithContext(ctx, http.MethodGet, authURL.String(), nil); err != nil {
		return "", err
	}

	if registryAuth != "" {
		log.Debug("Credentials found.")
		// CREDENTIAL: Uncomment to log registry credentials
		// log.Tracef("Credentials: %v", registryAuth)
		r.Header.Add("Authorization", fmt.Sprintf("Basic %s", registryAuth))
	} else {
		log.Debug("No credentials found.")
	}

	authResponse, err := httpclient.Get().Do(r)
	if err != nil {
		return "", err
	}
	defer func() { _ = authResponse.Body.Close() }()

	if authResponse.StatusCode != http.StatusOK {
		return "", fmt.Errorf("token server responded with %q", authResponse.Status)
	}

	body, err := io.ReadAll(io.LimitReader(authResponse.Body, maxTokenResponseSize))
	if err != nil {
		return "", err
	}

	tokenResponse := &types.TokenResponse{}
	if err = json.Unmarshal(body, tokenResponse); err != nil {
		return "", err
	}

	token := tokenResponse.Token
	if token == "" {
		token = tokenResponse.AccessToken
	}
	if token == "" {
		return "", errors.New("token server response did not contain a token")
	}

	header := fmt.Sprintf("Bearer %s", token)
	tokens.put(cacheKey, header, tokenTTL(tokenResponse.ExpiresIn))
	return header, nil
}

// GetAuthURL from the instructions in the challenge
func GetAuthURL(challenge string, imageRef ref.Named) (*url.URL, error) {
	raw := strings.TrimSpace(challenge)
	if len(raw) >= len("bearer") && strings.EqualFold(raw[:len("bearer")], "bearer") {
		raw = raw[len("bearer"):]
	}

	values := make(map[string]string)
	for _, m := range challengeParam.FindAllStringSubmatch(raw, -1) {
		val := m[2]
		if val == "" {
			val = m[3]
		}
		values[strings.ToLower(m[1])] = val
	}

	log.WithFields(log.Fields{
		"realm":   values["realm"],
		"service": values["service"],
	}).Debug("Checking challenge header content")
	if values["realm"] == "" || values["service"] == "" {
		return nil, fmt.Errorf("challenge header did not include all values needed to construct an auth url")
	}

	authURL, err := url.Parse(values["realm"])
	if err != nil {
		return nil, fmt.Errorf("invalid realm in challenge header: %w", err)
	}
	if authURL.Host == "" {
		return nil, fmt.Errorf("realm in challenge header has no host")
	}
	// The realm is controlled by the registry (or whoever sits in between), and credentials are sent to it.
	// Never allow it to downgrade the connection unless certificate verification has been disabled on purpose.
	if authURL.Scheme != "https" && (authURL.Scheme != "http" || !httpclient.InsecureSkipVerify()) {
		return nil, fmt.Errorf("refusing to use non-https realm %q for token authentication", authURL.Scheme+"://"+authURL.Host)
	}

	q := authURL.Query()
	q.Add("service", values["service"])

	scopeImage := ref.Path(imageRef)

	scope := fmt.Sprintf("repository:%s:pull", scopeImage)
	log.WithFields(log.Fields{"scope": scope, "image": imageRef.Name()}).Debug("Setting scope for auth token")
	q.Add("scope", scope)

	authURL.RawQuery = q.Encode()
	return authURL, nil
}

// GetChallengeURL returns the URL to check auth requirements
// for access to a given image
func GetChallengeURL(imageRef ref.Named) url.URL {
	host, _ := helpers.GetRegistryAddress(imageRef.Name())

	URL := url.URL{
		Scheme: "https",
		Host:   host,
		Path:   "/v2/",
	}
	return URL
}
