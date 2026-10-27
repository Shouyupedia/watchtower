package actions_test

import (
	"context"
	"fmt"
	"sync/atomic"
	"testing"
	"time"

	"github.com/shouyupedia/watchtower/internal/actions"
	"github.com/shouyupedia/watchtower/internal/actions/mocks"
	"github.com/shouyupedia/watchtower/pkg/types"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// slowClient makes the staleness check take a while, like a registry round trip would
type slowClient struct {
	mocks.MockClient
	delay     time.Duration
	inFlight  atomic.Int32
	maxFlight atomic.Int32
}

func (c *slowClient) IsContainerStale(ctx context.Context, cont types.Container, params types.UpdateParams) (bool, types.ImageID, error) {
	cur := c.inFlight.Add(1)
	defer c.inFlight.Add(-1)
	for {
		prev := c.maxFlight.Load()
		if cur <= prev || c.maxFlight.CompareAndSwap(prev, cur) {
			break
		}
	}
	time.Sleep(c.delay)
	return c.MockClient.IsContainerStale(ctx, cont, params)
}

func newSlowClient(n int, delay time.Duration) *slowClient {
	var containers []types.Container
	for i := 0; i < n; i++ {
		name := fmt.Sprintf("container-%02d", i)
		containers = append(containers, mocks.CreateMockContainer(name, "/"+name, "image:latest", time.Now()))
	}
	return &slowClient{
		MockClient: mocks.CreateMockClient(&mocks.TestData{Containers: containers, Staleness: map[string]bool{}}, false, false),
		delay:      delay,
	}
}

func TestUpdateChecksContainersConcurrently(t *testing.T) {
	client := newSlowClient(12, 50*time.Millisecond)

	start := time.Now()
	report, err := actions.Update(context.Background(), client, types.UpdateParams{
		Filter:           func(types.FilterableContainer) bool { return true },
		MonitorOnly:      true,
		CheckConcurrency: 4,
	})
	elapsed := time.Since(start)

	t.Logf("checking 12 containers with 50ms checks, 4 at a time, took %v (about 600ms one by one)", elapsed)
	require.NoError(t, err)
	assert.Len(t, report.Scanned(), 12)
	assert.EqualValues(t, 4, client.maxFlight.Load())
	// 12 checks of 50ms take 600ms one by one, but about 150ms four at a time
	assert.Less(t, elapsed, 400*time.Millisecond)
}

func TestUpdateChecksOneByOneByDefault(t *testing.T) {
	client := newSlowClient(4, 5*time.Millisecond)

	_, err := actions.Update(context.Background(), client, types.UpdateParams{
		Filter:      func(types.FilterableContainer) bool { return true },
		MonitorOnly: true,
	})

	require.NoError(t, err)
	assert.EqualValues(t, 1, client.maxFlight.Load())
}
