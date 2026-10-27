package api

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"crypto/tls"
	"errors"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"

	log "github.com/sirupsen/logrus"
)

const tokenMissingMsg = //nolint:gosec // an error message, not a credential
"api token is empty or has not been set. exiting"

// DefaultAddr is the address the API listens on unless configured otherwise
const DefaultAddr = ":8080"

const (
	readHeaderTimeout = 10 * time.Second
	readTimeout       = 30 * time.Second
	writeTimeout      = 0 // update requests are synchronous and may run for a long time
	idleTimeout       = 120 * time.Second
	maxHeaderBytes    = 16 << 10
	shutdownTimeout   = 10 * time.Second
)

// API is the http server responsible for serving the HTTP API endpoints
type API struct {
	Token       string
	Addr        string
	TLSCertFile string
	TLSKeyFile  string

	hasHandlers bool
	mux         *http.ServeMux

	mu       sync.Mutex // guards server and shutdown
	server   *http.Server
	shutdown bool
}

// New is a factory function creating a new API instance
func New(token string) *API {
	return &API{
		Token:       token,
		Addr:        DefaultAddr,
		hasHandlers: false,
		mux:         http.NewServeMux(),
	}
}

// RequireToken is wrapper around http.HandleFunc that checks token validity
func (api *API) RequireToken(fn http.HandlerFunc) http.HandlerFunc {
	// Compare fixed-size digests so neither the content nor the length of the token leaks through timing
	want := sha256.Sum256([]byte("Bearer " + api.Token))
	return func(w http.ResponseWriter, r *http.Request) {
		got := sha256.Sum256([]byte(r.Header.Get("Authorization")))
		if subtle.ConstantTimeCompare(got[:], want[:]) != 1 {
			w.Header().Set("WWW-Authenticate", "Bearer")
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
	api.mux.HandleFunc(path, api.RequireToken(fn))
}

// RegisterHandler is a wrapper around http.Handler that also sets the flag used to determine whether to launch the API
func (api *API) RegisterHandler(path string, handler http.Handler) {
	api.hasHandlers = true
	api.mux.Handle(path, api.RequireToken(handler.ServeHTTP))
}

// Start the API and serve over HTTP(S). Requires an API Token to be set.
// If block is false, the server is started in the background once the listener is bound.
func (api *API) Start(block bool) error {
	if !api.hasHandlers {
		log.Debug("Watchtower HTTP API skipped.")
		return nil
	}

	if api.Token == "" {
		return errors.New(tokenMissingMsg)
	}

	if (api.TLSCertFile == "") != (api.TLSKeyFile == "") {
		return errors.New("both the http api tls certificate and key must be set to enable TLS")
	}

	addr := api.Addr
	if addr == "" {
		addr = DefaultAddr
	}

	server := &http.Server{
		Addr:              addr,
		Handler:           api.mux,
		ReadHeaderTimeout: readHeaderTimeout,
		ReadTimeout:       readTimeout,
		WriteTimeout:      writeTimeout,
		IdleTimeout:       idleTimeout,
		MaxHeaderBytes:    maxHeaderBytes,
		TLSConfig:         &tls.Config{MinVersion: tls.VersionTLS12},
	}

	listener, err := (&net.ListenConfig{}).Listen(context.Background(), "tcp", addr)
	if err != nil {
		return err
	}

	api.mu.Lock()
	if api.shutdown {
		api.mu.Unlock()
		_ = listener.Close()
		return nil
	}
	api.server = server
	api.mu.Unlock()

	if api.TLSCertFile == "" {
		log.Warn("The HTTP API is served over plain HTTP. The API token is sent unencrypted, consider enabling TLS or placing it behind a TLS terminating proxy.")
	}

	serve := func() error {
		var err error
		if api.TLSCertFile != "" {
			err = server.ServeTLS(listener, api.TLSCertFile, api.TLSKeyFile)
		} else {
			err = server.Serve(listener)
		}
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return err
	}

	if block {
		return serve()
	}

	go func() {
		if err := serve(); err != nil {
			log.Fatalf("HTTP API stopped unexpectedly: %v", err)
		}
	}()
	return nil
}

// Shutdown gracefully stops the API server, waiting for in-flight requests to finish.
// It is safe to call before Start, in which case the server will not start.
func (api *API) Shutdown() {
	api.mu.Lock()
	api.shutdown = true
	server := api.server
	api.mu.Unlock()

	if server == nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), shutdownTimeout)
	defer cancel()
	if err := server.Shutdown(ctx); err != nil {
		log.WithError(err).Warn("HTTP API did not shut down cleanly")
	}
}

// IsTLS returns whether the API is configured to serve over TLS
func (api *API) IsTLS() bool {
	return strings.TrimSpace(api.TLSCertFile) != ""
}
