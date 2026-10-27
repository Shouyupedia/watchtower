package container

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	cerrdefs "github.com/containerd/errdefs"
	"io"
	"strings"
	"time"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/filters"
	"github.com/docker/docker/api/types/image"
	"github.com/docker/docker/api/types/network"
	sdkClient "github.com/docker/docker/client"
	log "github.com/sirupsen/logrus"

	"github.com/shouyupedia/watchtower/pkg/registry"
	"github.com/shouyupedia/watchtower/pkg/registry/digest"
	t "github.com/shouyupedia/watchtower/pkg/types"
)

const defaultStopSignal = "SIGTERM"

// apiCallTimeout bounds a single, non-streaming call to the Docker daemon
const apiCallTimeout = 2 * time.Minute

// pollInterval is how often the state of a container is checked while waiting for it to stop
const pollInterval = time.Second

// maxExecOutputBytes caps how much output of a lifecycle hook command is kept
const maxExecOutputBytes = 1 << 20

// A Client is the interface through which watchtower interacts with the
// Docker API.
type Client interface {
	ListContainers(context.Context, t.Filter) ([]t.Container, error)
	GetContainer(ctx context.Context, containerID t.ContainerID) (t.Container, error)
	StopContainer(context.Context, t.Container, time.Duration) error
	StartContainer(context.Context, t.Container) (t.ContainerID, error)
	RenameContainer(context.Context, t.Container, string) error
	IsContainerStale(context.Context, t.Container, t.UpdateParams) (stale bool, latestImage t.ImageID, err error)
	ExecuteCommand(ctx context.Context, containerID t.ContainerID, command string, timeout int) (SkipUpdate bool, err error)
	RemoveImageByID(context.Context, t.ImageID) error
	WarnOnHeadPullFailed(container t.Container) bool
}

// NewClient returns a new Client instance which can be used to interact with
// the Docker API.
// The client reads its configuration from the following environment variables:
//   - DOCKER_HOST			the docker-engine host to send api requests to
//   - DOCKER_TLS_VERIFY		whether to verify tls certificates
//   - DOCKER_API_VERSION	pins the docker api version. If unset, the version is negotiated with the daemon
func NewClient(opts ClientOptions) Client {
	cli, err := sdkClient.NewClientWithOpts(sdkClient.FromEnv, sdkClient.WithAPIVersionNegotiation())

	if err != nil {
		log.Fatalf("Error instantiating Docker client: %s", err)
	}

	return dockerClient{
		api:           cli,
		ClientOptions: opts,
	}
}

// ClientOptions contains the options for how the docker client wrapper should behave
type ClientOptions struct {
	RemoveVolumes     bool
	IncludeStopped    bool
	ReviveStopped     bool
	IncludeRestarting bool
	WarnOnHeadFailed  WarningStrategy
}

// WarningStrategy is a value determining when to show warnings
type WarningStrategy string

const (
	// WarnAlways warns whenever the problem occurs
	WarnAlways WarningStrategy = "always"
	// WarnNever never warns when the problem occurs
	WarnNever WarningStrategy = "never"
	// WarnAuto skips warning when the problem was expected
	WarnAuto WarningStrategy = "auto"
)

type dockerClient struct {
	api sdkClient.APIClient
	ClientOptions
}

func (client dockerClient) WarnOnHeadPullFailed(container t.Container) bool {
	if client.WarnOnHeadFailed == WarnAlways {
		return true
	}
	if client.WarnOnHeadFailed == WarnNever {
		return false
	}

	return registry.WarnOnAPIConsumption(container)
}

// callCtx derives a context that bounds a single call to the Docker daemon
func callCtx(ctx context.Context) (context.Context, context.CancelFunc) {
	return context.WithTimeout(ctx, apiCallTimeout)
}

func (client dockerClient) ListContainers(ctx context.Context, fn t.Filter) ([]t.Container, error) {
	cs := []t.Container{}

	if client.IncludeStopped && client.IncludeRestarting {
		log.Debug("Retrieving running, stopped, restarting and exited containers")
	} else if client.IncludeStopped {
		log.Debug("Retrieving running, stopped and exited containers")
	} else if client.IncludeRestarting {
		log.Debug("Retrieving running and restarting containers")
	} else {
		log.Debug("Retrieving running containers")
	}

	filter := client.createListFilter()
	listCtx, cancel := callCtx(ctx)
	defer cancel()
	containers, err := client.api.ContainerList(
		listCtx,
		container.ListOptions{
			Filters: filter,
		})

	if err != nil {
		return nil, err
	}

	for _, runningContainer := range containers {

		c, err := client.GetContainer(ctx, t.ContainerID(runningContainer.ID))
		if err != nil {
			return nil, err
		}

		if fn(c) {
			cs = append(cs, c)
		}
	}

	return cs, nil
}

func (client dockerClient) createListFilter() filters.Args {
	filterArgs := filters.NewArgs()
	filterArgs.Add("status", "running")

	if client.IncludeStopped {
		filterArgs.Add("status", "created")
		filterArgs.Add("status", "exited")
	}

	if client.IncludeRestarting {
		filterArgs.Add("status", "restarting")
	}

	return filterArgs
}

func (client dockerClient) GetContainer(ctx context.Context, containerID t.ContainerID) (t.Container, error) {
	ctx, cancel := callCtx(ctx)
	defer cancel()

	containerInfo, err := client.api.ContainerInspect(ctx, string(containerID))
	if err != nil {
		return &Container{}, err
	}

	netType, netContainerId, found := strings.Cut(string(containerInfo.HostConfig.NetworkMode), ":")
	if found && netType == "container" {
		parentContainer, err := client.api.ContainerInspect(ctx, netContainerId)
		if err != nil {
			log.WithFields(map[string]interface{}{
				"container":         containerInfo.Name,
				"error":             err,
				"network-container": netContainerId,
			}).Warnf("Unable to resolve network container: %v", err)

		} else {
			// Replace the container ID with a container name to allow it to reference the re-created network container
			containerInfo.HostConfig.NetworkMode = container.NetworkMode(fmt.Sprintf("container:%s", parentContainer.Name))
		}
	}

	imageInfo, err := client.api.ImageInspect(ctx, containerInfo.Image)
	if err != nil {
		log.Warnf("Failed to retrieve container image info: %v", err)
		return &Container{containerInfo: &containerInfo, imageInfo: nil}, nil
	}

	return &Container{containerInfo: &containerInfo, imageInfo: &imageInfo}, nil
}

func (client dockerClient) StopContainer(ctx context.Context, c t.Container, timeout time.Duration) error {
	signal := c.StopSignal()
	if signal == "" {
		signal = defaultStopSignal
	}

	idStr := string(c.ID())
	shortID := c.ID().ShortID()

	if c.IsRunning() {
		log.Infof("Stopping %s (%s) with %s", c.Name(), shortID, signal)
		killCtx, cancel := callCtx(ctx)
		err := client.api.ContainerKill(killCtx, idStr, signal)
		cancel()
		if err != nil {
			return err
		}
	}

	// TODO: This should probably be checked.
	_ = client.waitForStopOrTimeout(ctx, c, timeout)

	if c.ContainerInfo().HostConfig.AutoRemove {
		log.Debugf("AutoRemove container %s, skipping ContainerRemove call.", shortID)
	} else {
		log.Debugf("Removing container %s", shortID)

		removeCtx, cancel := callCtx(ctx)
		err := client.api.ContainerRemove(removeCtx, idStr, container.RemoveOptions{Force: true, RemoveVolumes: client.RemoveVolumes})
		cancel()
		if err != nil {
			if cerrdefs.IsNotFound(err) {
				log.Debugf("Container %s not found, skipping removal.", shortID)
				return nil
			}
			return err
		}
	}

	// Wait for container to be removed. In this case an error is a good thing
	if err := client.waitForStopOrTimeout(ctx, c, timeout); err == nil {
		return fmt.Errorf("container %s (%s) could not be removed", c.Name(), shortID)
	}

	return nil
}

func (client dockerClient) GetNetworkConfig(c t.Container) *network.NetworkingConfig {
	config := &network.NetworkingConfig{
		EndpointsConfig: c.ContainerInfo().NetworkSettings.Networks,
	}

	for _, ep := range config.EndpointsConfig {
		aliases := make([]string, 0, len(ep.Aliases))
		cidAlias := c.ID().ShortID()

		// Remove the old container ID alias from the network aliases, as it would accumulate across updates otherwise
		for _, alias := range ep.Aliases {
			if alias == cidAlias {
				continue
			}
			aliases = append(aliases, alias)
		}

		ep.Aliases = aliases
	}
	return config
}

func (client dockerClient) StartContainer(ctx context.Context, c t.Container) (t.ContainerID, error) {
	config := c.GetCreateConfig()
	hostConfig := c.GetCreateHostConfig()
	networkConfig := client.GetNetworkConfig(c)

	// simpleNetworkConfig is a networkConfig with only 1 network.
	// see: https://github.com/docker/docker/issues/29265
	simpleNetworkConfig := func() *network.NetworkingConfig {
		oneEndpoint := make(map[string]*network.EndpointSettings)
		for k, v := range networkConfig.EndpointsConfig {
			oneEndpoint[k] = v
			// we only need 1
			break
		}
		return &network.NetworkingConfig{EndpointsConfig: oneEndpoint}
	}()

	name := c.Name()

	log.Infof("Creating %s", name)

	opCtx, cancel := callCtx(ctx)
	defer cancel()

	createdContainer, err := client.api.ContainerCreate(opCtx, config, hostConfig, simpleNetworkConfig, nil, name)
	if err != nil {
		return "", err
	}

	if !(hostConfig.NetworkMode.IsHost()) {

		for k := range simpleNetworkConfig.EndpointsConfig {
			err = client.api.NetworkDisconnect(opCtx, k, createdContainer.ID, true)
			if err != nil {
				return "", err
			}
		}

		for k, v := range networkConfig.EndpointsConfig {
			err = client.api.NetworkConnect(opCtx, k, createdContainer.ID, v)
			if err != nil {
				return "", err
			}
		}

	}

	createdContainerID := t.ContainerID(createdContainer.ID)
	if !c.IsRunning() && !client.ReviveStopped {
		return createdContainerID, nil
	}

	return createdContainerID, client.doStartContainer(opCtx, c, createdContainer)

}

func (client dockerClient) doStartContainer(ctx context.Context, c t.Container, creation container.CreateResponse) error {
	name := c.Name()

	log.Debugf("Starting container %s (%s)", name, t.ContainerID(creation.ID).ShortID())
	err := client.api.ContainerStart(ctx, creation.ID, container.StartOptions{})
	if err != nil {
		return err
	}
	return nil
}

func (client dockerClient) RenameContainer(ctx context.Context, c t.Container, newName string) error {
	ctx, cancel := callCtx(ctx)
	defer cancel()
	log.Debugf("Renaming container %s (%s) to %s", c.Name(), c.ID().ShortID(), newName)
	return client.api.ContainerRename(ctx, string(c.ID()), newName)
}

func (client dockerClient) IsContainerStale(ctx context.Context, container t.Container, params t.UpdateParams) (stale bool, latestImage t.ImageID, err error) {
	if container.IsNoPull(params) {
		log.Debugf("Skipping image pull.")
	} else if err := client.PullImage(ctx, container); err != nil {
		return false, container.SafeImageID(), err
	}

	return client.HasNewImage(ctx, container)
}

func (client dockerClient) HasNewImage(ctx context.Context, container t.Container) (hasNew bool, latestImage t.ImageID, err error) {
	currentImageID := t.ImageID(container.ContainerInfo().Image)
	imageName := container.ImageName()

	inspectCtx, cancel := callCtx(ctx)
	defer cancel()
	newImageInfo, err := client.api.ImageInspect(inspectCtx, imageName)
	if err != nil {
		return false, currentImageID, err
	}

	newImageID := t.ImageID(newImageInfo.ID)
	if newImageID == currentImageID {
		log.Debugf("No new images found for %s", container.Name())
		return false, currentImageID, nil
	}

	log.Infof("Found new %s image (%s)", imageName, newImageID.ShortID())
	return true, newImageID, nil
}

// PullImage pulls the latest image for the supplied container, optionally skipping if it's digest can be confirmed
// to match the one that the registry reports via a HEAD request
func (client dockerClient) PullImage(ctx context.Context, container t.Container) error {
	containerName := container.Name()
	imageName := container.ImageName()

	fields := log.Fields{
		"image":     imageName,
		"container": containerName,
	}

	if strings.HasPrefix(imageName, "sha256:") {
		return fmt.Errorf("container uses a pinned image, and cannot be updated by watchtower")
	}

	log.WithFields(fields).Debugf("Trying to load authentication credentials.")
	opts, err := registry.GetPullOptions(imageName)
	if err != nil {
		log.Debugf("Error loading authentication credentials %s", err)
		return err
	}
	if opts.RegistryAuth != "" {
		log.Debug("Credentials loaded")
	}

	log.WithFields(fields).Debugf("Checking if pull is needed")

	if match, err := digest.CompareDigest(ctx, container, opts.RegistryAuth); err != nil {
		headLevel := log.DebugLevel
		if client.WarnOnHeadPullFailed(container) {
			headLevel = log.WarnLevel
		}
		log.WithFields(fields).Logf(headLevel, "Could not do a head request for %q, falling back to regular pull.", imageName)
		log.WithFields(fields).Log(headLevel, "Reason: ", err)
	} else if match {
		log.Debug("No pull needed. Skipping image.")
		return nil
	} else {
		log.Debug("Digests did not match, doing a pull.")
	}

	log.WithFields(fields).Debugf("Pulling image")

	response, err := client.api.ImagePull(ctx, imageName, opts)
	if err != nil {
		log.Debugf("Error pulling image %s, %s", imageName, err)
		return err
	}

	defer func() { _ = response.Close() }()
	// the pull request will be aborted prematurely unless the response is read,
	// and failures are reported in-band as JSON messages rather than as an HTTP error
	dec := json.NewDecoder(response)
	for {
		var msg struct {
			Error string `json:"error"`
		}
		if err = dec.Decode(&msg); errors.Is(err, io.EOF) {
			break
		} else if err != nil {
			log.Error(err)
			return err
		}
		if msg.Error != "" {
			err = fmt.Errorf("pull of %s failed: %s", imageName, msg.Error)
			log.Error(err)
			return err
		}
	}
	return nil
}

func (client dockerClient) RemoveImageByID(ctx context.Context, id t.ImageID) error {
	log.Infof("Removing image %s", id.ShortID())

	ctx, cancel := callCtx(ctx)
	defer cancel()
	items, err := client.api.ImageRemove(
		ctx,
		string(id),
		image.RemoveOptions{
			Force: true,
		})

	if log.IsLevelEnabled(log.DebugLevel) {
		deleted := strings.Builder{}
		untagged := strings.Builder{}
		for _, item := range items {
			if item.Deleted != "" {
				if deleted.Len() > 0 {
					deleted.WriteString(`, `)
				}
				deleted.WriteString(t.ImageID(item.Deleted).ShortID())
			}
			if item.Untagged != "" {
				if untagged.Len() > 0 {
					untagged.WriteString(`, `)
				}
				untagged.WriteString(t.ImageID(item.Untagged).ShortID())
			}
		}
		fields := log.Fields{`deleted`: deleted.String(), `untagged`: untagged.String()}
		log.WithFields(fields).Debug("Image removal completed")
	}

	return err
}

func (client dockerClient) ExecuteCommand(ctx context.Context, containerID t.ContainerID, command string, timeout int) (SkipUpdate bool, err error) {
	clog := log.WithField("containerID", containerID)

	// Create the exec
	execConfig := container.ExecOptions{
		Tty: true,
		Cmd: []string{"sh", "-c", command},
	}

	createCtx, cancel := callCtx(ctx)
	exec, err := client.api.ContainerExecCreate(createCtx, string(containerID), execConfig)
	cancel()
	if err != nil {
		return false, err
	}

	// The command is allowed to run for as long as the timeout says (or forever if it is 0),
	// so the attach and start calls are bound to that instead of the regular API call timeout
	execCtx := ctx
	if timeout > 0 {
		var cancelExec context.CancelFunc
		execCtx, cancelExec = context.WithTimeout(ctx, time.Duration(timeout)*time.Minute)
		defer cancelExec()
	}

	response, attachErr := client.api.ContainerExecAttach(execCtx, exec.ID, container.ExecAttachOptions{
		Tty:    true,
		Detach: false,
	})
	if attachErr != nil {
		clog.Errorf("Failed to extract command exec logs: %v", attachErr)
	}

	// Run the exec
	execStartCheck := container.ExecStartOptions{Detach: false, Tty: true}
	err = client.api.ContainerExecStart(execCtx, exec.ID, execStartCheck)
	if err != nil {
		return false, err
	}

	var output string
	if attachErr == nil {
		defer response.Close()
		var writer bytes.Buffer
		written, err := writer.ReadFrom(io.LimitReader(response.Reader, maxExecOutputBytes))
		if err != nil {
			clog.Error(err)
		} else if written > 0 {
			output = strings.TrimSpace(writer.String())
		}
	}

	// Inspect the exec to get the exit code and print a message if the
	// exit code is not success.
	skipUpdate, err := client.waitForExecOrTimeout(execCtx, exec.ID, output)
	if err != nil {
		return true, err
	}

	return skipUpdate, nil
}

func (client dockerClient) waitForExecOrTimeout(ctx context.Context, ID string, execOutput string) (SkipUpdate bool, err error) {
	const ExTempFail = 75

	for {
		execInspect, err := client.api.ContainerExecInspect(ctx, ID)

		//goland:noinspection GoNilness
		log.WithFields(log.Fields{
			"exit-code":    execInspect.ExitCode,
			"exec-id":      execInspect.ExecID,
			"running":      execInspect.Running,
			"container-id": execInspect.ContainerID,
		}).Debug("Awaiting timeout or completion")

		if err != nil {
			return false, err
		}
		if execInspect.Running {
			if err := sleepCtx(ctx, pollInterval); err != nil {
				return false, err
			}
			continue
		}
		if len(execOutput) > 0 {
			log.Infof("Command output:\n%v", execOutput)
		}

		if execInspect.ExitCode == ExTempFail {
			return true, nil
		}

		if execInspect.ExitCode > 0 {
			return false, fmt.Errorf("command exited with code %v  %s", execInspect.ExitCode, execOutput)
		}
		break
	}
	return false, nil
}

func (client dockerClient) waitForStopOrTimeout(ctx context.Context, c t.Container, waitTime time.Duration) error {
	timeout := time.After(waitTime)

	for {
		select {
		case <-timeout:
			return nil
		case <-ctx.Done():
			return ctx.Err()
		default:
			inspectCtx, cancel := callCtx(ctx)
			ci, err := client.api.ContainerInspect(inspectCtx, string(c.ID()))
			cancel()
			if err != nil {
				return err
			} else if !ci.State.Running {
				return nil
			}
		}
		if err := sleepCtx(ctx, pollInterval); err != nil {
			return err
		}
	}
}

// sleepCtx waits for d to pass, or for ctx to be done, whichever comes first
func sleepCtx(ctx context.Context, d time.Duration) error {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-timer.C:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}
