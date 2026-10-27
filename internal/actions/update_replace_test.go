package actions_test

import (
	"context"
	"testing"
	"time"

	dockercontainer "github.com/docker/docker/api/types/container"
	"github.com/shouyupedia/watchtower/internal/actions"
	"github.com/shouyupedia/watchtower/internal/actions/mocks"
	"github.com/shouyupedia/watchtower/pkg/types"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func runningContainer(id, name string) types.Container {
	return mocks.CreateMockContainerWithConfig(id, name, "image:latest", true, false, time.Now(),
		&dockercontainer.Config{Image: "image:latest", Labels: map[string]string{}})
}

func replaceFixture() *mocks.TestData {
	return &mocks.TestData{
		Containers: []types.Container{
			runningContainer("c1", "/app"),
			runningContainer("c2", "/other"),
		},
		NotPreservable: map[string]bool{},
		FailPreserve:   map[string]bool{},
		FailHealth:     map[string]bool{},
		FailRestore:    map[string]bool{},
		FailStart:      map[string]bool{},
		FailRollback:   map[string]bool{},
	}
}

func replaceParams() types.UpdateParams {
	return types.UpdateParams{Filter: allContainers, UpdateStrategy: types.StrategyReplace, HealthTimeout: time.Minute}
}

func eventsFor(data *mocks.TestData, name string) []string {
	var events []string
	for _, e := range data.Events {
		if e == "recover:" {
			continue
		}
		if len(e) > len(name) && e[len(e)-len(name):] == name {
			events = append(events, e[:len(e)-len(name)-1])
		}
	}
	return events
}

func TestReplaceKeepsTheOldContainerUntilTheNewOneWorks(t *testing.T) {
	data := replaceFixture()

	report, err := actions.Update(context.Background(), mocks.CreateMockClient(data, false, false), replaceParams())

	require.NoError(t, err)
	assert.Empty(t, report.Failed())
	assert.Len(t, report.Updated(), 2)
	assert.Equal(t, "recover:", data.Events[0], "leftovers of earlier updates are dealt with first")
	assert.Equal(t, []string{"preserve", "start", "health", "remove-backup"}, eventsFor(data, "/app"))
	assert.Equal(t, []string{"preserve", "start", "health", "remove-backup"}, eventsFor(data, "/other"))
}

func TestReplaceRestoresTheOldContainerIfTheNewOneDoesNotStart(t *testing.T) {
	data := replaceFixture()
	data.FailStart["/app"] = true

	report, err := actions.Update(context.Background(), mocks.CreateMockClient(data, false, false), replaceParams())

	require.NoError(t, err)
	assert.Equal(t, []string{"preserve", "start", "restore"}, eventsFor(data, "/app"), "the old container must not be removed")
	require.Len(t, report.Failed(), 1)
	assert.Contains(t, report.Failed()[0].Error(), "the previous version was restored")
	assert.Empty(t, data.RolledBack, "restoring the old container replaces rolling back to the old image")
	assert.Equal(t, []string{"preserve", "start", "health", "remove-backup"}, eventsFor(data, "/other"), "other containers are not affected")
}

func TestReplaceDiscardsAndRestoresIfTheNewContainerIsUnhealthy(t *testing.T) {
	data := replaceFixture()
	data.FailHealth["/app"] = true

	report, err := actions.Update(context.Background(), mocks.CreateMockClient(data, false, false), replaceParams())

	require.NoError(t, err)
	assert.Equal(t, []string{"preserve", "start", "health", "discard", "restore"}, eventsFor(data, "/app"),
		"the new container has to go before the old one can take its name back")
	require.Len(t, report.Failed(), 1)
	assert.Contains(t, report.Failed()[0].Error(), "unhealthy")
	assert.Contains(t, report.Failed()[0].Error(), "the previous version was restored")
}

func TestReplaceReportsAFailedRestore(t *testing.T) {
	data := replaceFixture()
	data.FailStart["/app"] = true
	data.FailRestore["/app"] = true

	report, err := actions.Update(context.Background(), mocks.CreateMockClient(data, false, false), replaceParams())

	require.NoError(t, err)
	require.Len(t, report.Failed(), 1)
	assert.Contains(t, report.Failed()[0].Error(), "restoring the previous version failed too")
}

func TestReplaceSkipsTheHealthWaitWithoutATimeout(t *testing.T) {
	data := replaceFixture()
	params := replaceParams()
	params.HealthTimeout = 0

	_, err := actions.Update(context.Background(), mocks.CreateMockClient(data, false, false), params)

	require.NoError(t, err)
	assert.Equal(t, []string{"preserve", "start", "remove-backup"}, eventsFor(data, "/app"))
}

func TestReplaceFallsBackToRecreateForContainersThatCannotBePreserved(t *testing.T) {
	data := replaceFixture()
	data.NotPreservable["/app"] = true

	report, err := actions.Update(context.Background(), mocks.CreateMockClient(data, false, false), replaceParams())

	require.NoError(t, err)
	assert.Empty(t, report.Failed())
	assert.Equal(t, []string{"start"}, eventsFor(data, "/app"), "no backup, no health wait, no restore")
}

func TestReplaceDoesNotStartAContainerThatCouldNotBeStopped(t *testing.T) {
	data := replaceFixture()
	data.FailPreserve["/app"] = true

	report, err := actions.Update(context.Background(), mocks.CreateMockClient(data, false, false), replaceParams())

	require.NoError(t, err)
	require.Len(t, report.Failed(), 1)
	assert.NotContains(t, eventsFor(data, "/app"), "restore")
	assert.NotContains(t, eventsFor(data, "/app"), "remove-backup")
}

func TestReplaceIsNotUsedWithoutRestarting(t *testing.T) {
	data := replaceFixture()
	params := replaceParams()
	params.NoRestart = true

	_, err := actions.Update(context.Background(), mocks.CreateMockClient(data, false, false), params)

	require.NoError(t, err)
	assert.Empty(t, data.Events, "a container that is set aside but never replaced would be lost")
}

func TestRecreateIsTheDefault(t *testing.T) {
	data := replaceFixture()

	_, err := actions.Update(context.Background(), mocks.CreateMockClient(data, false, false), types.UpdateParams{Filter: allContainers})

	require.NoError(t, err)
	assert.Equal(t, []string{"start:/app", "start:/other"}, data.Events)
}

func TestReplaceWithRollingRestart(t *testing.T) {
	data := replaceFixture()
	data.FailStart["/app"] = true
	params := replaceParams()
	params.RollingRestart = true

	report, err := actions.Update(context.Background(), mocks.CreateMockClient(data, false, false), params)

	require.NoError(t, err)
	require.Len(t, report.Failed(), 1)
	assert.Equal(t, []string{"preserve", "start", "restore"}, eventsFor(data, "/app"))
	assert.Equal(t, []string{"preserve", "start", "health", "remove-backup"}, eventsFor(data, "/other"))
}
