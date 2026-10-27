package types

// TokenResponse is returned by the registry on successful authentication
type TokenResponse struct {
	Token       string `json:"token"`
	AccessToken string `json:"access_token"`
	// ExpiresIn is the lifetime of the token in seconds, if the registry says so
	ExpiresIn int `json:"expires_in"`
}
