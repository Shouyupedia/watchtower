package update

import (
	"errors"
	"io"
	"net/http"
	"strings"

	log "github.com/sirupsen/logrus"
)

// New is a factory function creating a new  Handler instance
func New(updateFn func(images []string), updateLock chan bool) *Handler {
	if updateLock == nil {
		updateLock = make(chan bool, 1)
		updateLock <- true
	}

	return &Handler{
		fn:      updateFn,
		Path:    "/v1/update",
		lock:    updateLock,
		pending: make(chan struct{}, 16),
	}
}

// Handler is an API handler used for triggering container update scans
type Handler struct {
	fn      func(images []string)
	Path    string
	lock    chan bool
	pending chan struct{}
}

// Handle is the actual http.Handle function doing all the heavy lifting
func (handle *Handler) Handle(w http.ResponseWriter, r *http.Request) {
	log.Info("Updates triggered by HTTP API request.")

	const maxRequestBodyBytes = 64 * 1024
	r.Body = http.MaxBytesReader(w, r.Body, maxRequestBodyBytes)
	defer r.Body.Close()
	_, err := io.Copy(io.Discard, r.Body)
	if err != nil {
		var tooLarge *http.MaxBytesError
		if errors.As(err, &tooLarge) {
			http.Error(w, "request body is too large", http.StatusRequestEntityTooLarge)
		} else {
			http.Error(w, "invalid request body", http.StatusBadRequest)
		}
		return
	}

	var images []string
	imageQueries, found := r.URL.Query()["image"]
	if found {
		for _, image := range imageQueries {
			images = append(images, strings.Split(image, ",")...)
		}

	} else {
		images = nil
	}

	if len(images) > 0 {
		select {
		case handle.pending <- struct{}{}:
			defer func() { <-handle.pending }()
		default:
			http.Error(w, "too many pending updates", http.StatusTooManyRequests)
			return
		}
		select {
		case <-r.Context().Done():
			return
		case chanValue := <-handle.lock:
			defer func() { handle.lock <- chanValue }()
			if r.Context().Err() == nil {
				handle.fn(images)
			}
		}
	} else {
		select {
		case <-r.Context().Done():
			return
		case chanValue := <-handle.lock:
			defer func() { handle.lock <- chanValue }()
			if r.Context().Err() == nil {
				handle.fn(images)
			}
		default:
			log.Debug("Skipped. Another update already running.")
		}
	}

}
