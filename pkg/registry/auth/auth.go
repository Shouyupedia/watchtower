package auth

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"

	"github.com/containrrr/watchtower/pkg/registry/helpers"
	"github.com/containrrr/watchtower/pkg/registry/httpclient"
	"github.com/containrrr/watchtower/pkg/types"
	ref "github.com/distribution/reference"
	"github.com/sirupsen/logrus"
)

// ChallengeHeader is the HTTP Header containing challenge instructions
const ChallengeHeader = "WWW-Authenticate"

// GetToken fetches a token for the registry hosting the provided image
func GetToken(container types.Container, registryAuth string) (string, error) {
	normalizedRef, err := ref.ParseNormalizedNamed(container.ImageName())
	if err != nil {
		return "", err
	}

	URL := GetChallengeURL(normalizedRef)
	logrus.WithField("URL", URL.String()).Debug("Built challenge URL")

	var req *http.Request
	if req, err = GetChallengeRequest(URL); err != nil {
		return "", err
	}

	client := httpclient.New()
	var res *http.Response
	if res, err = client.Do(req); err != nil {
		return "", err
	}
	defer res.Body.Close()
	v := res.Header.Get(ChallengeHeader)

	logrus.WithFields(logrus.Fields{
		"status": res.Status,
		"header": v,
	}).Debug("Got response to challenge request")

	challenge := strings.ToLower(v)
	if strings.HasPrefix(challenge, "basic") {
		if registryAuth == "" {
			return "", fmt.Errorf("no credentials available")
		}

		return fmt.Sprintf("Basic %s", registryAuth), nil
	}
	if strings.HasPrefix(challenge, "bearer") {
		return GetBearerHeader(v, normalizedRef, registryAuth)
	}

	return "", errors.New("unsupported challenge type from registry")
}

// GetChallengeRequest creates a request for getting challenge instructions
func GetChallengeRequest(URL url.URL) (*http.Request, error) {
	req, err := http.NewRequest("GET", URL.String(), nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "*/*")
	req.Header.Set("User-Agent", "Watchtower (Docker)")
	return req, nil
}

// GetBearerHeader tries to fetch a bearer token from the registry based on the challenge instructions
func GetBearerHeader(challenge string, imageRef ref.Named, registryAuth string) (string, error) {
	client := httpclient.New()
	authURL, err := GetAuthURL(challenge, imageRef)

	if err != nil {
		return "", err
	}

	var r *http.Request
	if r, err = http.NewRequest("GET", authURL.String(), nil); err != nil {
		return "", err
	}

	if registryAuth != "" {
		logrus.Debug("Credentials found.")
		r.Header.Add("Authorization", fmt.Sprintf("Basic %s", registryAuth))
	} else {
		logrus.Debug("No credentials found.")
	}

	var authResponse *http.Response
	if authResponse, err = client.Do(r); err != nil {
		return "", err
	}
	defer authResponse.Body.Close()
	if authResponse.StatusCode != http.StatusOK {
		return "", fmt.Errorf("token endpoint returned %s", authResponse.Status)
	}

	const maxTokenResponseBytes = 1024 * 1024
	body, err := io.ReadAll(io.LimitReader(authResponse.Body, maxTokenResponseBytes+1))
	if err != nil {
		return "", err
	}
	if len(body) > maxTokenResponseBytes {
		return "", fmt.Errorf("token response exceeds size limit")
	}
	tokenResponse := &types.TokenResponse{}

	err = json.Unmarshal(body, tokenResponse)
	if err != nil {
		return "", err
	}
	if tokenResponse.Token == "" {
		return "", fmt.Errorf("token endpoint returned an empty token")
	}

	return fmt.Sprintf("Bearer %s", tokenResponse.Token), nil
}

// GetAuthURL from the instructions in the challenge
func GetAuthURL(challenge string, imageRef ref.Named) (*url.URL, error) {
	scheme, raw, ok := strings.Cut(strings.TrimSpace(challenge), " ")
	if !ok || !strings.EqualFold(scheme, "bearer") {
		return nil, fmt.Errorf("unsupported auth challenge")
	}

	pairs := strings.Split(raw, ",")
	values := make(map[string]string, len(pairs))

	for _, pair := range pairs {
		trimmed := strings.Trim(pair, " ")
		if key, val, ok := strings.Cut(trimmed, "="); ok {
			values[strings.ToLower(strings.TrimSpace(key))] = strings.Trim(strings.TrimSpace(val), `"`)
		}
	}
	logrus.WithFields(logrus.Fields{
		"realm":   values["realm"],
		"service": values["service"],
	}).Debug("Checking challenge header content")
	if values["realm"] == "" || values["service"] == "" {

		return nil, fmt.Errorf("challenge header did not include all values needed to construct an auth url")
	}

	authURL, err := url.Parse(values["realm"])
	if err != nil {
		return nil, fmt.Errorf("invalid auth realm: %w", err)
	}
	if authURL.Scheme != "https" || authURL.Hostname() == "" || authURL.User != nil || authURL.Fragment != "" {
		return nil, fmt.Errorf("auth realm must be an absolute HTTPS URL without user info or fragment")
	}
	q := authURL.Query()
	q.Set("service", values["service"])

	scopeImage := ref.Path(imageRef)

	scope := fmt.Sprintf("repository:%s:pull", scopeImage)
	logrus.WithFields(logrus.Fields{"scope": scope, "image": imageRef.Name()}).Debug("Setting scope for auth token")
	q.Set("scope", scope)

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
