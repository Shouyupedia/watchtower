package update

import (
	"errors"
	"io"
	"net/http"
	"strings"
	"sync/atomic"

	log "github.com/sirupsen/logrus"
)

const (
	// Path is the URL path the update handler is registered at
	Path = "/v1/update"

	maxBodyBytes     = 1 << 20
	maxImages        = 100
	maxImageNameLen  = 255
	maxPendingImages = 10
)

// New is a factory function creating a new  Handler instance
func New(updateFn func(images []string), updateLock chan bool) *Handler {
	if updateLock == nil {
		updateLock = make(chan bool, 1)
		updateLock <- true
	}

	return &Handler{
		fn:   updateFn,
		Path: Path,
		lock: updateLock,
	}
}

// Handler is an API handler used for triggering container update scans
type Handler struct {
	fn      func(images []string)
	Path    string
	lock    chan bool
	pending atomic.Int32
}

// Handle is the actual http.Handle function doing all the heavy lifting
//
// POST triggers an update. GET is still accepted for backwards compatibility but is deprecated, as it is neither
// safe nor idempotent. A request without an image filter is skipped (202) if another update is already running,
// while a request for specific images waits for its turn.
func (handle *Handler) Handle(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodPost:
	case http.MethodGet:
		log.Warn("Triggering updates with GET is deprecated and will be removed in a future release. Use POST instead.")
		w.Header().Set("Deprecation", "true")
	default:
		w.Header().Set("Allow", "POST")
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	// The body is not used, but has to be consumed (within limits) for the connection to be reusable
	if _, err := io.Copy(io.Discard, http.MaxBytesReader(w, r.Body, maxBodyBytes)); err != nil {
		var tooLarge *http.MaxBytesError
		if errors.As(err, &tooLarge) {
			http.Error(w, "request body too large", http.StatusRequestEntityTooLarge)
		} else {
			http.Error(w, "failed to read request body", http.StatusBadRequest)
		}
		return
	}

	images, err := parseImages(r)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}

	log.Info("Updates triggered by HTTP API request.")

	if len(images) > 0 {
		if handle.pending.Add(1) > maxPendingImages {
			handle.pending.Add(-1)
			w.Header().Set("Retry-After", "60")
			http.Error(w, "too many pending update requests", http.StatusTooManyRequests)
			return
		}
		defer handle.pending.Add(-1)

		select {
		case chanValue := <-handle.lock:
			defer func() { handle.lock <- chanValue }()
			handle.fn(images)
		case <-r.Context().Done():
			log.Debug("Update request was cancelled while waiting for a running update to finish.")
			http.Error(w, "request cancelled", http.StatusServiceUnavailable)
		}
		return
	}

	select {
	case chanValue := <-handle.lock:
		defer func() { handle.lock <- chanValue }()
		handle.fn(nil)
	default:
		log.Debug("Skipped. Another update already running.")
		w.WriteHeader(http.StatusAccepted)
	}
}

func parseImages(r *http.Request) ([]string, error) {
	var images []string
	for _, query := range r.URL.Query()["image"] {
		for _, image := range strings.Split(query, ",") {
			image = strings.TrimSpace(image)
			if image == "" {
				continue
			}
			if len(image) > maxImageNameLen {
				return nil, errors.New("image name too long")
			}
			images = append(images, image)
		}
	}
	if len(images) > maxImages {
		return nil, errors.New("too many images specified")
	}
	return images, nil
}
