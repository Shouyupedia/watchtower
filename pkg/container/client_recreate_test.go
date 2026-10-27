package container

import (
	"context"
	"errors"
	"sync"
	"testing"

	dockercontainer "github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/network"
	sdkClient "github.com/docker/docker/client"
	ocispec "github.com/opencontainers/image-spec/specs-go/v1"
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

func (r *recordingAPI) ContainerCreate(_ context.Context, config *dockercontainer.Config, _ *dockercontainer.HostConfig, _ *network.NetworkingConfig, _ *ocispec.Platform, name string) (dockercontainer.CreateResponse, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.createdWith = append(r.createdWith, config.Image)
	return dockercontainer.CreateResponse{ID: "new-container-id"}, nil
}

func (r *recordingAPI) NetworkDisconnect(context.Context, string, string, bool) error { return nil }

func (r *recordingAPI) NetworkConnect(context.Context, string, string, *network.EndpointSettings) error {
	return nil
}

func (r *recordingAPI) ContainerStart(context.Context, string, dockercontainer.StartOptions) error {
	return r.startErr
}

func (r *recordingAPI) ContainerRemove(_ context.Context, id string, _ dockercontainer.RemoveOptions) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.removed = append(r.removed, id)
	return nil
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
