package metrics

import (
	"sync"
	"testing"
)

func TestDefaultIsSafeForConcurrentUse(t *testing.T) {
	var wg sync.WaitGroup
	results := make([]*Metrics, 16)
	for i := range results {
		wg.Add(1)
		go func() {
			defer wg.Done()
			results[i] = Default()
		}()
	}
	wg.Wait()

	for _, m := range results {
		if m == nil || m != results[0] {
			t.Fatal("Default() must return the same, fully initialised instance to every caller")
		}
	}
}
