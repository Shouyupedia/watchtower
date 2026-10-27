package container

import (
	"context"
	"fmt"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	dockercontainer "github.com/moby/moby/api/types/container"
	dockerimage "github.com/moby/moby/api/types/image"
	sdkClient "github.com/moby/moby/client"
	"github.com/shouyupedia/watchtower/pkg/filters"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// latencyAPI simulates a daemon where every call takes a while. Calls that are not overridden panic.
type latencyAPI struct {
	sdkClient.APIClient
	latency      time.Duration
	count        int
	sharedImages int

	inspects   atomic.Int32
	imageCalls atomic.Int32
	inFlight   atomic.Int32
	maxFlight  atomic.Int32
	failOn     string
	mu         sync.Mutex
}

func (l *latencyAPI) ContainerList(context.Context, sdkClient.ContainerListOptions) (sdkClient.ContainerListResult, error) {
	list := make([]dockercontainer.Summary, l.count)
	for i := range list {
		list[i] = dockercontainer.Summary{ID: fmt.Sprintf("c%03d", i)}
	}
	return sdkClient.ContainerListResult{Items: list}, nil
}

func (l *latencyAPI) ContainerInspect(ctx context.Context, id string, _ sdkClient.ContainerInspectOptions) (sdkClient.ContainerInspectResult, error) {
	l.inspects.Add(1)
	cur := l.inFlight.Add(1)
	defer l.inFlight.Add(-1)
	l.mu.Lock()
	if cur > l.maxFlight.Load() {
		l.maxFlight.Store(cur)
	}
	l.mu.Unlock()

	select {
	case <-time.After(l.latency):
	case <-ctx.Done():
		return sdkClient.ContainerInspectResult{}, ctx.Err()
	}
	if id == l.failOn {
		return sdkClient.ContainerInspectResult{}, fmt.Errorf("inspect %s failed", id)
	}

	var n int
	_, _ = fmt.Sscanf(id, "c%03d", &n)
	return sdkClient.ContainerInspectResult{Container: dockercontainer.InspectResponse{
		ID:         id,
		Name:       "/" + id,
		Image:      fmt.Sprintf("sha256:image%d", n%l.sharedImages),
		HostConfig: &dockercontainer.HostConfig{},

		Config: &dockercontainer.Config{Labels: map[string]string{}},
	}}, nil
}

func (l *latencyAPI) ImageInspect(_ context.Context, id string, _ ...sdkClient.ImageInspectOption) (sdkClient.ImageInspectResult, error) {
	l.imageCalls.Add(1)
	time.Sleep(l.latency)
	return sdkClient.ImageInspectResult{InspectResponse: dockerimage.InspectResponse{ID: id}}, nil
}

func TestListContainersInspectsInParallelAndKeepsOrder(t *testing.T) {
	api := &latencyAPI{latency: 20 * time.Millisecond, count: 32, sharedImages: 4}
	client := dockerClient{api: api}

	start := time.Now()
	containers, err := client.ListContainers(context.Background(), filters.NoFilter)
	elapsed := time.Since(start)

	t.Logf("listing 32 containers with 20ms calls took %v (about 1.28s one by one)", elapsed)
	require.NoError(t, err)
	require.Len(t, containers, 32)
	for i, c := range containers {
		assert.EqualValues(t, fmt.Sprintf("c%03d", i), c.ID(), "order must follow the daemon's list")
	}

	// Sequentially this takes 32 * (20ms + 20ms) = 1.28s; with 8 workers it is a fraction of that
	assert.Less(t, elapsed, 600*time.Millisecond)
	assert.Greater(t, api.maxFlight.Load(), int32(1), "inspects must overlap")
	assert.LessOrEqual(t, api.maxFlight.Load(), int32(inspectConcurrency), "parallelism must be bounded")
}

func TestListContainersLooksUpSharedImagesOnce(t *testing.T) {
	// Run the container inspects one at a time so that cache hits are deterministic
	api := &latencyAPI{latency: time.Millisecond, count: 20, sharedImages: 4}
	client := dockerClient{api: api}

	containers, err := client.ListContainers(context.Background(), filters.NoFilter)
	require.NoError(t, err)
	require.Len(t, containers, 20)

	// 4 distinct images; concurrent first lookups of the same image may overlap, but never anywhere near 20
	assert.LessOrEqual(t, api.imageCalls.Load(), int32(4+inspectConcurrency))
	for _, c := range containers {
		assert.NotNil(t, c.ImageInfo())
	}
}

func TestListContainersReturnsInspectErrors(t *testing.T) {
	api := &latencyAPI{latency: time.Millisecond, count: 10, sharedImages: 2, failOn: "c004"}
	client := dockerClient{api: api}

	_, err := client.ListContainers(context.Background(), filters.NoFilter)

	assert.ErrorContains(t, err, "c004")
}

func TestListContainersStopsWhenCancelled(t *testing.T) {
	api := &latencyAPI{latency: time.Second, count: 50, sharedImages: 2}
	client := dockerClient{api: api}
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()

	start := time.Now()
	_, err := client.ListContainers(ctx, filters.NoFilter)

	assert.Error(t, err)
	assert.Less(t, time.Since(start), 500*time.Millisecond)
}
