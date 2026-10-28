package update

import (
	"context"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestHandleParsesImagesAndReleasesLock(t *testing.T) {
	var got []string
	handler := New(func(images []string) { got = images }, nil)
	handler.Handle(httptest.NewRecorder(), httptest.NewRequest("GET", "/v1/update?image=one,two&image=three", nil))
	if !reflect.DeepEqual(got, []string{"one", "two", "three"}) {
		t.Fatalf("unexpected images: %v", got)
	}
	select {
	case <-handler.lock:
	default:
		t.Fatal("update lock was not released")
	}
}

func TestHandleRejectsOversizedBody(t *testing.T) {
	called := false
	handler := New(func([]string) { called = true }, nil)
	rec := httptest.NewRecorder()
	req := httptest.NewRequest("POST", "/v1/update", strings.NewReader(strings.Repeat("x", 64*1024+1)))
	handler.Handle(rec, req)
	if rec.Code != http.StatusRequestEntityTooLarge || called {
		t.Fatalf("oversized request accepted: %d, called=%v", rec.Code, called)
	}
}

func TestWaitingUpdateHonorsCancellation(t *testing.T) {
	lock := make(chan bool, 1)
	called := make(chan struct{}, 1)
	handler := New(func([]string) { called <- struct{}{} }, lock)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	req := httptest.NewRequest("GET", "/v1/update?image=one", nil).WithContext(ctx)
	done := make(chan struct{})
	go func() {
		defer close(done)
		handler.Handle(httptest.NewRecorder(), req)
	}()
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("cancelled request remained queued")
	}
	select {
	case <-called:
		t.Fatal("cancelled request triggered an update")
	default:
	}
}

func TestHandlersKeepTheirOwnLocks(t *testing.T) {
	firstLock := make(chan bool, 1)
	firstLock <- true
	secondLock := make(chan bool, 1)
	called := false
	first := New(func([]string) { called = true }, firstLock)
	New(func([]string) { t.Fatal("wrong handler called") }, secondLock)
	first.Handle(httptest.NewRecorder(), httptest.NewRequest("GET", "/v1/update", nil))
	if !called {
		t.Fatal("creating another handler replaced the original lock")
	}
}

func TestUnfilteredUpdateSkipsBusyLock(t *testing.T) {
	handler := New(func([]string) { t.Fatal("busy update was not skipped") }, make(chan bool, 1))
	handler.Handle(httptest.NewRecorder(), httptest.NewRequest("GET", "/v1/update", nil))
}

func TestFilteredUpdateQueueIsBounded(t *testing.T) {
	handler := New(func([]string) { t.Fatal("full queue must reject the request") }, make(chan bool, 1))
	for i := 0; i < cap(handler.pending); i++ {
		handler.pending <- struct{}{}
	}
	rec := httptest.NewRecorder()
	handler.Handle(rec, httptest.NewRequest("GET", "/v1/update?image=one", nil))
	if rec.Code != http.StatusTooManyRequests {
		t.Fatalf("expected 429 for full update queue, got %d", rec.Code)
	}
}
