package update_test

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/shouyupedia/watchtower/pkg/api/update"
	"github.com/stretchr/testify/assert"
)

func newHandler(fn func(images []string)) (*update.Handler, chan bool) {
	lock := make(chan bool, 1)
	lock <- true
	return update.New(fn, lock), lock
}

func do(h *update.Handler, method, target string, body string) *httptest.ResponseRecorder {
	rec := httptest.NewRecorder()
	h.Handle(rec, httptest.NewRequest(method, target, strings.NewReader(body)))
	return rec
}

func TestPostTriggersUpdateWithoutFilter(t *testing.T) {
	var calls [][]string
	h, _ := newHandler(func(images []string) { calls = append(calls, images) })

	rec := do(h, http.MethodPost, "/v1/update", "")

	assert.Equal(t, http.StatusOK, rec.Code)
	assert.Len(t, calls, 1)
	assert.Nil(t, calls[0], "no image filter must be passed as nil, an empty filter would match nothing")
}

func TestImagesAreSplitAndTrimmed(t *testing.T) {
	var got []string
	h, _ := newHandler(func(images []string) { got = images })

	rec := do(h, http.MethodPost, "/v1/update?image=foo/bar,baz&image=%20qux%20,", "")

	assert.Equal(t, http.StatusOK, rec.Code)
	assert.Equal(t, []string{"foo/bar", "baz", "qux"}, got)
}

func TestGetIsStillAcceptedButMarkedDeprecated(t *testing.T) {
	called := false
	h, _ := newHandler(func([]string) { called = true })

	rec := do(h, http.MethodGet, "/v1/update", "")

	assert.Equal(t, http.StatusOK, rec.Code)
	assert.True(t, called)
	assert.Equal(t, "true", rec.Header().Get("Deprecation"))
}

func TestOtherMethodsAreRejected(t *testing.T) {
	called := false
	h, _ := newHandler(func([]string) { called = true })

	for _, m := range []string{http.MethodPut, http.MethodDelete, http.MethodPatch} {
		rec := do(h, m, "/v1/update", "")
		assert.Equal(t, http.StatusMethodNotAllowed, rec.Code, m)
		assert.Equal(t, "POST", rec.Header().Get("Allow"))
	}
	assert.False(t, called)
}

func TestOversizedBodyIsRejected(t *testing.T) {
	called := false
	h, _ := newHandler(func([]string) { called = true })

	rec := do(h, http.MethodPost, "/v1/update", strings.Repeat("a", (1<<20)+1))

	assert.Equal(t, http.StatusRequestEntityTooLarge, rec.Code)
	assert.False(t, called)
}

func TestImageLimits(t *testing.T) {
	h, _ := newHandler(func([]string) { t.Fatal("update must not run") })

	rec := do(h, http.MethodPost, "/v1/update?image="+strings.Repeat("a", 256), "")
	assert.Equal(t, http.StatusBadRequest, rec.Code)

	rec = do(h, http.MethodPost, "/v1/update?image="+strings.Repeat("a,", 101), "")
	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestRequestWithoutFilterIsSkippedWhileUpdateRuns(t *testing.T) {
	var runs atomic.Int32
	h, lock := newHandler(func([]string) { runs.Add(1) })
	<-lock // simulate a running update

	rec := do(h, http.MethodPost, "/v1/update", "")

	assert.Equal(t, http.StatusAccepted, rec.Code)
	assert.Zero(t, runs.Load())
}

func TestRequestWithFilterWaitsForRunningUpdate(t *testing.T) {
	var runs atomic.Int32
	h, lock := newHandler(func([]string) { runs.Add(1) })
	<-lock

	done := make(chan *httptest.ResponseRecorder)
	go func() { done <- do(h, http.MethodPost, "/v1/update?image=foo", "") }()

	select {
	case <-done:
		t.Fatal("request must wait for the running update")
	case <-time.After(50 * time.Millisecond):
	}

	lock <- true
	rec := <-done
	assert.Equal(t, http.StatusOK, rec.Code)
	assert.EqualValues(t, 1, runs.Load())
}

func TestWaitingRequestIsReleasedWhenCancelled(t *testing.T) {
	h, lock := newHandler(func([]string) { t.Fatal("update must not run") })
	<-lock

	req := httptest.NewRequest(http.MethodPost, "/v1/update?image=foo", nil)
	ctx, cancel := contextWithCancel(req)
	rec := httptest.NewRecorder()

	var wg sync.WaitGroup
	wg.Add(1)
	go func() { defer wg.Done(); h.Handle(rec, ctx) }()
	cancel()
	wg.Wait()

	assert.Equal(t, http.StatusServiceUnavailable, rec.Code)
}

func TestTooManyWaitingRequestsAreRejected(t *testing.T) {
	h, lock := newHandler(func([]string) {})
	<-lock

	var wg sync.WaitGroup
	for i := 0; i < 10; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); do(h, http.MethodPost, "/v1/update?image=foo", "") }()
	}
	time.Sleep(100 * time.Millisecond)

	rec := do(h, http.MethodPost, "/v1/update?image=foo", "")
	assert.Equal(t, http.StatusTooManyRequests, rec.Code)
	assert.NotEmpty(t, rec.Header().Get("Retry-After"))

	lock <- true
	wg.Wait()
}
