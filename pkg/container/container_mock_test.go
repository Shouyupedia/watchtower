package container

import (
	dockercontainer "github.com/docker/docker/api/types/container"
	dockerimage "github.com/docker/docker/api/types/image"
	"github.com/docker/go-connections/nat"
	dockerspec "github.com/moby/docker-image-spec/specs-go/v1"
)

type MockContainerUpdate func(*dockercontainer.InspectResponse, *dockerimage.InspectResponse)

func MockContainer(updates ...MockContainerUpdate) *Container {
	containerInfo := dockercontainer.InspectResponse{
		ContainerJSONBase: &dockercontainer.ContainerJSONBase{
			ID:         "container_id",
			Image:      "image",
			Name:       "test-containrrr",
			HostConfig: &dockercontainer.HostConfig{},
		},
		Config: &dockercontainer.Config{
			Labels: map[string]string{},
		},
	}
	image := dockerimage.InspectResponse{
		ID:     "image_id",
		Config: &dockerspec.DockerOCIImageConfig{},
	}

	for _, update := range updates {
		update(&containerInfo, &image)
	}
	return NewContainer(&containerInfo, &image)
}

func WithPortBindings(portBindingSources ...string) MockContainerUpdate {
	return func(c *dockercontainer.InspectResponse, i *dockerimage.InspectResponse) {
		portBindings := nat.PortMap{}
		for _, pbs := range portBindingSources {
			portBindings[nat.Port(pbs)] = []nat.PortBinding{}
		}
		c.HostConfig.PortBindings = portBindings
	}
}

func WithImageName(name string) MockContainerUpdate {
	return func(c *dockercontainer.InspectResponse, i *dockerimage.InspectResponse) {
		c.Config.Image = name
		i.RepoTags = append(i.RepoTags, name)
	}
}

func WithLinks(links []string) MockContainerUpdate {
	return func(c *dockercontainer.InspectResponse, i *dockerimage.InspectResponse) {
		c.HostConfig.Links = links
	}
}

func WithLabels(labels map[string]string) MockContainerUpdate {
	return func(c *dockercontainer.InspectResponse, i *dockerimage.InspectResponse) {
		c.Config.Labels = labels
	}
}

func WithContainerState(state dockercontainer.State) MockContainerUpdate {
	return func(cnt *dockercontainer.InspectResponse, img *dockerimage.InspectResponse) {
		cnt.State = &state
	}
}

func WithHealthcheck(healthConfig dockercontainer.HealthConfig) MockContainerUpdate {
	return func(cnt *dockercontainer.InspectResponse, img *dockerimage.InspectResponse) {
		cnt.Config.Healthcheck = &healthConfig
	}
}

func WithImageHealthcheck(healthConfig dockercontainer.HealthConfig) MockContainerUpdate {
	return func(cnt *dockercontainer.InspectResponse, img *dockerimage.InspectResponse) {
		img.Config.Healthcheck = &dockerspec.HealthcheckConfig{
			Test:          healthConfig.Test,
			Interval:      healthConfig.Interval,
			Timeout:       healthConfig.Timeout,
			StartPeriod:   healthConfig.StartPeriod,
			StartInterval: healthConfig.StartInterval,
			Retries:       healthConfig.Retries,
		}
	}
}
