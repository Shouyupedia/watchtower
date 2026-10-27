package actions_test

import (
	"context"
	"testing"
	"time"

	"github.com/shouyupedia/watchtower/internal/actions"
	"github.com/shouyupedia/watchtower/internal/actions/mocks"
	"github.com/shouyupedia/watchtower/pkg/types"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func rollbackFixture(failStart ...string) (mocks.MockClient, *mocks.TestData) {
	data := &mocks.TestData{
		Containers: []types.Container{
			mocks.CreateMockContainer("c1", "/app", "image:latest", time.Now()),
			mocks.CreateMockContainer("c2", "/other", "image:latest", time.Now()),
		},
		FailStart:    map[string]bool{},
		FailRollback: map[string]bool{},
	}
	for _, name := range failStart {
		data.FailStart[name] = true
	}
	return mocks.CreateMockClient(data, false, false), data
}

var allContainers = func(types.FilterableContainer) bool { return true }

func TestFailedStartIsRolledBackWhenEnabled(t *testing.T) {
	client, data := rollbackFixture("/app")

	report, err := actions.Update(context.Background(), client, types.UpdateParams{Filter: allContainers, RollbackOnFailure: true})

	require.NoError(t, err)
	assert.Equal(t, []string{"/app"}, data.RolledBack)
	require.Len(t, report.Failed(), 1)
	assert.Contains(t, report.Failed()[0].Error(), "restored with the previous image")
	assert.Len(t, report.Updated(), 1, "the other container is not affected")
}

func TestFailedStartIsNotRolledBackByDefault(t *testing.T) {
	client, data := rollbackFixture("/app")

	report, err := actions.Update(context.Background(), client, types.UpdateParams{Filter: allContainers})

	require.NoError(t, err)
	assert.Empty(t, data.RolledBack)
	require.Len(t, report.Failed(), 1)
	assert.NotContains(t, report.Failed()[0].Error(), "previous image")
}

func TestFailedRollbackIsReported(t *testing.T) {
	client, data := rollbackFixture("/app")
	data.FailRollback["/app"] = true

	report, err := actions.Update(context.Background(), client, types.UpdateParams{Filter: allContainers, RollbackOnFailure: true})

	require.NoError(t, err)
	assert.Empty(t, data.RolledBack)
	require.Len(t, report.Failed(), 1)
	assert.Contains(t, report.Failed()[0].Error(), "rollback to the previous image failed")
}

func TestSuccessfulUpdatesAreNotRolledBack(t *testing.T) {
	client, data := rollbackFixture()

	report, err := actions.Update(context.Background(), client, types.UpdateParams{Filter: allContainers, RollbackOnFailure: true})

	require.NoError(t, err)
	assert.Empty(t, data.RolledBack)
	assert.Empty(t, report.Failed())
}
