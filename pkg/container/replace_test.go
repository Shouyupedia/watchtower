package container

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	cerrdefs "github.com/containerd/errdefs"
	"github.com/moby/moby/api/types/container"
	sdkClient "github.com/moby/moby/client"
	"github.com/shouyupedia/watchtower/pkg/filters"
	t "github.com/shouyupedia/watchtower/pkg/types"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// replaceAPI is a fake daemon for the operations behind the replace strategy. It records what is done in order.
type replaceAPI struct {
	sdkClient.APIClient

	mu     sync.Mutex
	ops    []string
	states map[string]container.InspectResponse // by id or name
	list   []container.Summary

	// stopsOnKill makes a killed container stop, which is what a real container does
	stopsOnKill bool
	renameErr   error
}

func (r *replaceAPI) record(format string, args ...any) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.ops = append(r.ops, fmt.Sprintf(format, args...))
}

func (r *replaceAPI) operations() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]string(nil), r.ops...)
}

func (r *replaceAPI) ContainerUpdate(_ context.Context, id string, options sdkClient.ContainerUpdateOptions) (sdkClient.ContainerUpdateResult, error) {
	r.record("policy:%s:%s", id, options.RestartPolicy.Name)
	return sdkClient.ContainerUpdateResult{}, nil
}

func (r *replaceAPI) ContainerKill(_ context.Context, id string, options sdkClient.ContainerKillOptions) (sdkClient.ContainerKillResult, error) {
	r.record("kill:%s:%s", id, options.Signal)
	if r.stopsOnKill {
		r.mu.Lock()
		defer r.mu.Unlock()
		if st, ok := r.states[id]; ok && st.State != nil {
			st.State.Running = false
		}
	}
	return sdkClient.ContainerKillResult{}, nil
}

func (r *replaceAPI) ContainerInspect(_ context.Context, id string, _ sdkClient.ContainerInspectOptions) (sdkClient.ContainerInspectResult, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if st, ok := r.states[id]; ok {
		return sdkClient.ContainerInspectResult{Container: st}, nil
	}
	return sdkClient.ContainerInspectResult{}, cerrdefs.ErrNotFound.WithMessage("no such container: " + id)
}

func (r *replaceAPI) ContainerRename(_ context.Context, id string, options sdkClient.ContainerRenameOptions) (sdkClient.ContainerRenameResult, error) {
	r.record("rename:%s:%s", id, options.NewName)
	return sdkClient.ContainerRenameResult{}, r.renameErr
}

func (r *replaceAPI) ContainerStart(_ context.Context, id string, _ sdkClient.ContainerStartOptions) (sdkClient.ContainerStartResult, error) {
	r.record("start:%s", id)
	return sdkClient.ContainerStartResult{}, nil
}

func (r *replaceAPI) ContainerRemove(_ context.Context, id string, opts sdkClient.ContainerRemoveOptions) (sdkClient.ContainerRemoveResult, error) {
	r.record("remove:%s:volumes=%t", id, opts.RemoveVolumes)
	return sdkClient.ContainerRemoveResult{}, nil
}

func (r *replaceAPI) ContainerList(_ context.Context, opts sdkClient.ContainerListOptions) (sdkClient.ContainerListResult, error) {
	r.record("list:all=%t", opts.All)
	return sdkClient.ContainerListResult{Items: r.list}, nil
}

func (r *replaceAPI) ImageInspect(context.Context, string, ...sdkClient.ImageInspectOption) (sdkClient.ImageInspectResult, error) {
	return sdkClient.ImageInspectResult{}, cerrdefs.ErrNotFound
}

func (r *replaceAPI) running(id string, running bool) {
	r.withState(id, running, nil)
}

func (r *replaceAPI) withState(id string, running bool, labels map[string]string) {
	if r.states == nil {
		r.states = map[string]container.InspectResponse{}
	}
	r.states[id] = container.InspectResponse{
		ID: id, Name: "/" + id, State: &container.State{Running: running}, HostConfig: &container.HostConfig{},
		Config: &container.Config{Labels: labels},
	}
}

func preservableContainer(policy container.RestartPolicy, running bool) *Container {
	c := MockContainer(WithContainerState(container.State{Running: running}))
	c.containerInfo.Name = "/web"
	c.containerInfo.HostConfig.RestartPolicy = policy
	return c
}

func TestBackupNamesRoundTrip(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	for _, policy := range []container.RestartPolicy{
		{Name: container.RestartPolicyDisabled},
		{Name: container.RestartPolicyAlways},
		{Name: container.RestartPolicyUnlessStopped},
		{Name: container.RestartPolicyOnFailure, MaximumRetryCount: 5},
	} {
		for _, running := range []bool{true, false} {
			name := backupName("/web-1", now, policy, running)
			match := backupNamePattern.FindStringSubmatch(name)
			require.NotNil(t, match, name)
			assert.Equal(t, "web-1", match[1])
			assert.Equal(t, "1700000000", match[2])
			assert.Equal(t, policy, restartPolicyFromCode(match[3], match[4]), name)
			assert.Equal(t, running, match[5] == "r", name)
		}
	}
	assert.Nil(t, backupNamePattern.FindStringSubmatch("web_wt-prev-notatime"))
	assert.Nil(t, backupNamePattern.FindStringSubmatch("web"))
}

func TestStopAndPreserveDisablesTheRestartPolicyBeforeStopping(t *testing.T) {
	api := &replaceAPI{stopsOnKill: true}
	c := preservableContainer(container.RestartPolicy{Name: container.RestartPolicyAlways}, true)
	api.running(string(c.ID()), true)

	backup, err := dockerClient{api: api}.StopAndPreserve(context.Background(), c, time.Second)

	require.NoError(t, err)
	ops := api.operations()
	require.GreaterOrEqual(t, len(ops), 3)
	assert.Equal(t, "policy:container_id:no", ops[0], "otherwise the daemon restarts the container right after it is killed")
	assert.Equal(t, "kill:container_id:SIGTERM", ops[1])
	assert.True(t, strings.HasPrefix(ops[len(ops)-1], "rename:container_id:web_wt-prev-"), ops)
	assert.Equal(t, "/web", backup.OriginalName)
	assert.True(t, backup.WasRunning)
	assert.Equal(t, container.RestartPolicyAlways, backup.RestartPolicy.Name)
	assert.True(t, strings.HasSuffix(ops[len(ops)-1], "-ar"), "policy and state are kept in the name so a crash cannot lose them: %v", ops)
}

func TestStopAndPreserveRefusesContainersTheDaemonRemoves(t *testing.T) {
	api := &replaceAPI{}
	c := preservableContainer(container.RestartPolicy{}, true)
	c.containerInfo.HostConfig.AutoRemove = true

	_, err := dockerClient{api: api}.StopAndPreserve(context.Background(), c, time.Second)

	assert.ErrorIs(t, err, ErrNotPreservable)
	assert.Empty(t, api.operations())
}

func shortKillWait(t *testing.T) {
	previous := killWait
	killWait = 50 * time.Millisecond
	t.Cleanup(func() { killWait = previous })
}

func TestStopAndPreserveLeavesARunningContainerAlone(t *testing.T) {
	shortKillWait(t)
	api := &replaceAPI{stopsOnKill: false} // the container cannot be stopped at all
	c := preservableContainer(container.RestartPolicy{Name: container.RestartPolicyUnlessStopped}, true)
	api.running(string(c.ID()), true)

	_, err := dockerClient{api: api}.StopAndPreserve(context.Background(), c, 50*time.Millisecond)

	require.ErrorContains(t, err, "could not be stopped")
	ops := api.operations()
	assert.Contains(t, ops, "kill:container_id:SIGKILL", "a container ignoring its stop signal is killed, like docker stop does")
	assert.Equal(t, "policy:container_id:unless-stopped", ops[len(ops)-1], "the restart policy has to be put back")
	for _, op := range ops {
		assert.False(t, strings.HasPrefix(op, "rename:"), "a container that is still running must keep its name")
	}
}

func TestStopAndPreserveStartsTheContainerAgainIfItCannotBeRenamed(t *testing.T) {
	api := &replaceAPI{stopsOnKill: true, renameErr: errors.New("conflict")}
	c := preservableContainer(container.RestartPolicy{Name: container.RestartPolicyAlways}, true)
	api.running(string(c.ID()), true)

	_, err := dockerClient{api: api}.StopAndPreserve(context.Background(), c, time.Second)

	require.ErrorContains(t, err, "conflict")
	ops := api.operations()
	assert.Equal(t, []string{"policy:container_id:always", "start:container_id"}, ops[len(ops)-2:])
}

func TestStopAndPreserveOnlyRenamesAStoppedContainer(t *testing.T) {
	api := &replaceAPI{}
	c := preservableContainer(container.RestartPolicy{}, false)

	backup, err := dockerClient{api: api}.StopAndPreserve(context.Background(), c, time.Second)

	require.NoError(t, err)
	assert.False(t, backup.WasRunning)
	ops := api.operations()
	require.Len(t, ops, 1)
	assert.True(t, strings.HasPrefix(ops[0], "rename:"))
}

func TestRestoreContainer(t *testing.T) {
	api := &replaceAPI{}
	backup := backupFor("/web", true, container.RestartPolicy{Name: container.RestartPolicyOnFailure, MaximumRetryCount: 3})

	require.NoError(t, dockerClient{api: api}.RestoreContainer(context.Background(), backup))

	assert.Equal(t, []string{"rename:abc:web", "policy:abc:on-failure", "start:abc"}, api.operations())
}

func TestRestoreContainerDoesNotStartAContainerThatWasStopped(t *testing.T) {
	api := &replaceAPI{}

	require.NoError(t, dockerClient{api: api}.RestoreContainer(context.Background(), backupFor("/web", false, container.RestartPolicy{})))

	assert.Equal(t, []string{"rename:abc:web"}, api.operations())
}

func TestRemoveBackupHonoursRemoveVolumes(t *testing.T) {
	api := &replaceAPI{}
	b := backupFor("/web", true, container.RestartPolicy{})

	require.NoError(t, dockerClient{api: api, ClientOptions: ClientOptions{RemoveVolumes: true}}.RemoveBackup(context.Background(), b))
	assert.Equal(t, []string{"remove:abc:volumes=true"}, api.operations())
}

func backupFor(name string, running bool, policy container.RestartPolicy) t.ContainerBackup {
	return t.ContainerBackup{ID: "abc", OriginalName: name, BackupName: strings.TrimPrefix(name, "/") + "_wt-prev-1", WasRunning: running, RestartPolicy: policy}
}

func TestWaitUntilHealthy(t *testing.T) {
	state := func(s container.State, restarts int) container.InspectResponse {
		return container.InspectResponse{State: &s, RestartCount: restarts}
	}
	cases := []struct {
		name    string
		state   container.InspectResponse
		timeout time.Duration
		wantErr string
	}{
		{"no timeout means no wait", state(container.State{}, 0), 0, ""},
		{"healthy", state(container.State{Running: true, Health: &container.Health{Status: "healthy"}}, 0), time.Minute, ""},
		{"unhealthy", state(container.State{Running: true, Health: &container.Health{Status: "unhealthy"}}, 0), time.Minute, "unhealthy"},
		{"exited", state(container.State{Running: false, ExitCode: 3}, 0), time.Minute, "exited with code 3"},
		{"restarting", state(container.State{Running: true, Restarting: true}, 0), time.Minute, "keeps restarting"},
		{"restarted by the daemon", state(container.State{Running: true}, 2), time.Minute, "keeps restarting"},
		{"still running without a health check", state(container.State{Running: true}, 0), 30 * time.Millisecond, ""},
		{"never became healthy", state(container.State{Running: true, Health: &container.Health{Status: "starting"}}, 0), 30 * time.Millisecond, "still \"starting\""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			api := &replaceAPI{states: map[string]container.InspectResponse{"new": tc.state}}

			err := dockerClient{api: api}.WaitUntilHealthy(context.Background(), "new", tc.timeout)

			if tc.wantErr == "" {
				assert.NoError(t, err)
			} else {
				assert.ErrorContains(t, err, tc.wantErr)
			}
		})
	}
}

func TestWaitUntilHealthyStopsWhenCancelled(t *testing.T) {
	api := &replaceAPI{states: map[string]container.InspectResponse{
		"new": {State: &container.State{Running: true, Health: &container.Health{Status: "starting"}}},
	}}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()

	start := time.Now()
	err := dockerClient{api: api}.WaitUntilHealthy(ctx, "new", time.Hour)

	assert.ErrorIs(t, err, context.DeadlineExceeded)
	assert.Less(t, time.Since(start), 5*time.Second)
}

func summaryFor(id, name string, age time.Duration, policyCode string, running bool) container.Summary {
	state := "s"
	if running {
		state = "r"
	}
	return container.Summary{ID: id, Names: []string{fmt.Sprintf("/%s%s%d-%s%s", name, backupMarker, time.Now().Add(-age).Unix(), policyCode, state)}}
}

func TestRecoverBackups(t *testing.T) {
	old := time.Hour

	t.Run("restores a container nothing replaced", func(t *testing.T) {
		api := &replaceAPI{list: []container.Summary{summaryFor("b1", "web", old, "a", true)}}
		api.withState("b1", false, nil)

		require.NoError(t, dockerClient{api: api}.RecoverBackups(context.Background(), filters.NoFilter))

		assert.Equal(t, []string{"list:all=true", "rename:b1:web", "policy:b1:always", "start:b1"}, api.operations())
	})

	t.Run("removes the backup once the replacement runs", func(t *testing.T) {
		api := &replaceAPI{list: []container.Summary{summaryFor("b1", "web", old, "n", true)}}
		api.withState("b1", false, nil)
		api.running("web", true)

		require.NoError(t, dockerClient{api: api}.RecoverBackups(context.Background(), filters.NoFilter))

		assert.Equal(t, []string{"list:all=true", "remove:b1:volumes=false"}, api.operations())
	})

	t.Run("removes volumes of the backup like a successful update would, when asked to", func(t *testing.T) {
		api := &replaceAPI{list: []container.Summary{summaryFor("b1", "web", old, "n", true)}}
		api.withState("b1", false, nil)
		api.running("web", true)
		client := dockerClient{api: api, ClientOptions: ClientOptions{RemoveVolumes: true}}

		require.NoError(t, client.RecoverBackups(context.Background(), filters.NoFilter))

		assert.Equal(t, []string{"list:all=true", "remove:b1:volumes=true"}, api.operations())
	})

	t.Run("only touches containers the filter selects", func(t *testing.T) {
		api := &replaceAPI{list: []container.Summary{
			summaryFor("b1", "web", old, "n", true),
			summaryFor("b2", "db", old, "n", true),
		}}
		api.withState("b1", false, map[string]string{"com.centurylinklabs.watchtower.scope": "other"})
		api.withState("b2", false, map[string]string{"com.centurylinklabs.watchtower.scope": "mine"})

		filter, _ := filters.BuildFilter(nil, nil, false, "mine")
		require.NoError(t, dockerClient{api: api}.RecoverBackups(context.Background(), filter))

		assert.Equal(t, []string{"list:all=true", "rename:b2:db", "start:b2"}, api.operations(),
			"the backup of another instance's container must be left alone")
	})

	t.Run("judges a backup by its original name", func(t *testing.T) {
		api := &replaceAPI{list: []container.Summary{summaryFor("b1", "web", old, "n", true)}}
		api.withState("b1", false, nil)

		byName := filters.FilterByNames([]string{"web"}, filters.NoFilter)
		require.NoError(t, dockerClient{api: api}.RecoverBackups(context.Background(), byName))
		assert.Equal(t, []string{"list:all=true", "rename:b1:web", "start:b1"}, api.operations())

		api = &replaceAPI{list: []container.Summary{summaryFor("b1", "web", old, "n", true)}}
		api.withState("b1", false, nil)
		other := filters.FilterByNames([]string{"other"}, filters.NoFilter)
		require.NoError(t, dockerClient{api: api}.RecoverBackups(context.Background(), other))
		assert.Equal(t, []string{"list:all=true"}, api.operations())
	})

	t.Run("leaves both alone if the replacement is not running", func(t *testing.T) {
		api := &replaceAPI{list: []container.Summary{summaryFor("b1", "web", old, "n", true)}}
		api.withState("b1", false, nil)
		api.running("web", false)

		require.NoError(t, dockerClient{api: api}.RecoverBackups(context.Background(), filters.NoFilter))

		assert.Equal(t, []string{"list:all=true"}, api.operations())
	})

	t.Run("ignores backups of updates that may still be running", func(t *testing.T) {
		api := &replaceAPI{list: []container.Summary{summaryFor("b1", "web", time.Minute, "n", true)}}

		require.NoError(t, dockerClient{api: api}.RecoverBackups(context.Background(), filters.NoFilter))

		assert.Equal(t, []string{"list:all=true"}, api.operations())
	})

	t.Run("ignores containers that only look similar", func(t *testing.T) {
		api := &replaceAPI{list: []container.Summary{
			{ID: "x1", Names: []string{"/web_wt-prev-notatime"}},
			{ID: "x2", Names: []string{"/other"}},
			{ID: "x3"},
		}}

		require.NoError(t, dockerClient{api: api}.RecoverBackups(context.Background(), filters.NoFilter))

		assert.Equal(t, []string{"list:all=true"}, api.operations())
	})
}

func TestStopAndPreserveKillsAContainerThatIgnoresItsStopSignal(t *testing.T) {
	shortKillWait(t)
	api := &stubbornAPI{replaceAPI: replaceAPI{}}
	c := preservableContainer(container.RestartPolicy{}, true)
	api.running(string(c.ID()), true)

	backup, err := dockerClient{api: api}.StopAndPreserve(context.Background(), c, 50*time.Millisecond)

	require.NoError(t, err)
	ops := api.operations()
	assert.Equal(t, "kill:container_id:SIGTERM", ops[0])
	assert.Equal(t, "kill:container_id:SIGKILL", ops[1])
	assert.True(t, strings.HasPrefix(ops[2], "rename:"), ops)
	assert.True(t, backup.WasRunning)
}

// stubbornAPI is a container that ignores SIGTERM but not SIGKILL
type stubbornAPI struct {
	replaceAPI
}

func (s *stubbornAPI) ContainerKill(ctx context.Context, id string, options sdkClient.ContainerKillOptions) (sdkClient.ContainerKillResult, error) {
	s.record("kill:%s:%s", id, options.Signal)
	if options.Signal == "SIGKILL" {
		s.mu.Lock()
		s.states[id].State.Running = false
		s.mu.Unlock()
	}
	return sdkClient.ContainerKillResult{}, nil
}

func TestStopAndPreserveRestoresTheContainerIfItsStateCannotBeChecked(t *testing.T) {
	// The container disappears from the daemon's view after the kill: whatever happened, the restart policy
	// has to be put back and an attempt made to start it again, rather than leaving it stopped and unmanaged
	api := &replaceAPI{}
	c := preservableContainer(container.RestartPolicy{Name: container.RestartPolicyAlways}, true)

	_, err := dockerClient{api: api}.StopAndPreserve(context.Background(), c, 50*time.Millisecond)

	require.Error(t, err)
	ops := api.operations()
	assert.Equal(t, []string{"policy:container_id:always", "start:container_id"}, ops[len(ops)-2:])
	for _, op := range ops {
		assert.False(t, strings.HasPrefix(op, "rename:"), ops)
	}
}
