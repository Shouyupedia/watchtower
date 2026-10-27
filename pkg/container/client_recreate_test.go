package container

import (
	"context"
	"errors"
	"sync"
	"testing"

	dockercontainer "github.com/moby/moby/api/types/container"
	sdkClient "github.com/moby/moby/client"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// recordingAPI records what is done to the daemon while recreating a container
type recordingAPI struct {
	sdkClient.APIClient

	mu          sync.Mutex
	createdWith []string
	removed     []string
	startErr    error
}

func (r *recordingAPI) ContainerCreate(_ context.Context, options sdkClient.ContainerCreateOptions) (sdkClient.ContainerCreateResult, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.createdWith = append(r.createdWith, options.Config.Image)
	return sdkClient.ContainerCreateResult{ID: "new-container-id"}, nil
}

func (r *recordingAPI) NetworkDisconnect(context.Context, string, sdkClient.NetworkDisconnectOptions) (sdkClient.NetworkDisconnectResult, error) {
	return sdkClient.NetworkDisconnectResult{}, nil
}

func (r *recordingAPI) NetworkConnect(context.Context, string, sdkClient.NetworkConnectOptions) (sdkClient.NetworkConnectResult, error) {
	return sdkClient.NetworkConnectResult{}, nil
}

func (r *recordingAPI) ContainerStart(context.Context, string, sdkClient.ContainerStartOptions) (sdkClient.ContainerStartResult, error) {
	return sdkClient.ContainerStartResult{}, r.startErr
}

func (r *recordingAPI) ContainerRemove(_ context.Context, id string, _ sdkClient.ContainerRemoveOptions) (sdkClient.ContainerRemoveResult, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.removed = append(r.removed, id)
	return sdkClient.ContainerRemoveResult{}, nil
}

func runningContainer() *Container {
	c := MockContainer(
		WithImageName("registry.example/app:latest"),
		WithContainerState(dockercontainer.State{Running: true}),
	)
	c.containerInfo.Image = "sha256:previous"
	c.containerInfo.NetworkSettings = &dockercontainer.NetworkSettings{}
	return c
}

func TestStartContainerRemovesTheIncompleteContainerOnFailure(t *testing.T) {
	api := &recordingAPI{startErr: errors.New("port is already allocated")}
	client := dockerClient{api: api}

	id, err := client.StartContainer(context.Background(), runningContainer())

	require.ErrorContains(t, err, "port is already allocated")
	assert.Empty(t, id)
	assert.Equal(t, []string{"new-container-id"}, api.removed, "a container that never started would keep its name occupied")
}

func TestStartContainerKeepsTheContainerOnSuccess(t *testing.T) {
	api := &recordingAPI{}
	client := dockerClient{api: api}

	id, err := client.StartContainer(context.Background(), runningContainer())

	require.NoError(t, err)
	assert.EqualValues(t, "new-container-id", id)
	assert.Empty(t, api.removed)
	assert.Equal(t, []string{"registry.example/app:latest"}, api.createdWith)
}

func TestRollbackContainerUsesThePreviousImage(t *testing.T) {
	api := &recordingAPI{}
	client := dockerClient{api: api}
	c := runningContainer()

	_, err := client.RollbackContainer(context.Background(), c)

	require.NoError(t, err)
	assert.Equal(t, []string{"sha256:previous"}, api.createdWith, "the image ID, not the name that now points to the new image")
	assert.Equal(t, "registry.example/app:latest", c.GetCreateConfig().Image, "the container itself must not be changed")
}

func TestRollbackContainerNeedsToKnowThePreviousImage(t *testing.T) {
	c := runningContainer()
	c.containerInfo.Image = ""

	_, err := dockerClient{api: &recordingAPI{}}.RollbackContainer(context.Background(), c)

	assert.Error(t, err)
}
