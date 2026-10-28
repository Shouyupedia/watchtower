package api

import (
	"crypto/sha256"
	"crypto/subtle"
	"fmt"
	"net/http"
	"time"

	log "github.com/sirupsen/logrus"
)

const tokenMissingMsg = "api token is empty or has not been set. exiting"

// API is the http server responsible for serving the HTTP API endpoints
type API struct {
	Token       string
	hasHandlers bool
}

// New is a factory function creating a new API instance
func New(token string) *API {
	return &API{
		Token:       token,
		hasHandlers: false,
	}
}

// RequireToken is wrapper around http.HandleFunc that checks token validity
func (api *API) RequireToken(fn http.HandlerFunc) http.HandlerFunc {
	want := sha256.Sum256([]byte(fmt.Sprintf("Bearer %s", api.Token)))
	return func(w http.ResponseWriter, r *http.Request) {
		auth := r.Header.Get("Authorization")
		got := sha256.Sum256([]byte(auth))
		if api.Token == "" || subtle.ConstantTimeCompare(got[:], want[:]) != 1 {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		log.Debug("Valid token found.")
		fn(w, r)
	}
}

// RegisterFunc is a wrapper around http.HandleFunc that also sets the flag used to determine whether to launch the API
func (api *API) RegisterFunc(path string, fn http.HandlerFunc) {
	api.hasHandlers = true
	http.HandleFunc(path, api.RequireToken(fn))
}

// RegisterHandler is a wrapper around http.Handler that also sets the flag used to determine whether to launch the API
func (api *API) RegisterHandler(path string, handler http.Handler) {
	api.hasHandlers = true
	http.Handle(path, api.RequireToken(handler.ServeHTTP))
}

// Start the API and serve over HTTP. Requires an API Token to be set.
func (api *API) Start(block bool) error {

	if !api.hasHandlers {
		log.Debug("Watchtower HTTP API skipped.")
		return nil
	}

	if api.Token == "" {
		log.Fatal(tokenMissingMsg)
	}

	if block {
		runHTTPServer()
	} else {
		go func() {
			runHTTPServer()
		}()
	}
	return nil
}

func runHTTPServer() {
	server := &http.Server{
		Addr:              ":8080",
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       30 * time.Second,
		IdleTimeout:       60 * time.Second,
		MaxHeaderBytes:    64 * 1024,
	}
	log.Fatal(server.ListenAndServe())
}
