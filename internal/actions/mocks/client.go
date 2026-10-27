package mocks

import (
	"context"
	"errors"
	"fmt"
	"time"

	t "github.com/shouyupedia/watchtower/pkg/types"
)

// MockClient is a mock that passes as a watchtower Client
type MockClient struct {
	TestData      *TestData
	pullImages    bool
	removeVolumes bool
}

// TestData is the data used to perform the test
type TestData struct {
	TriedToRemoveImageCount int
	NameOfContainerToKeep   string
	Containers              []t.Container
	Staleness               map[string]bool
	// FailStart makes StartContainer fail for the containers with these names
	FailStart map[string]bool
	// FailRollback makes RollbackContainer fail for the containers with these names
	FailRollback map[string]bool
	// RolledBack collects the names of the containers that were rolled back
	RolledBack []string
}

// TriedToRemoveImage is a test helper function to check whether RemoveImageByID has been called
func (testdata *TestData) TriedToRemoveImage() bool {
	return testdata.TriedToRemoveImageCount > 0
}

// CreateMockClient creates a mock watchtower Client for usage in tests
func CreateMockClient(data *TestData, pullImages bool, removeVolumes bool) MockClient {
	return MockClient{
		data,
		pullImages,
		removeVolumes,
	}
}

// ListContainers is a mock method returning the provided container testdata
func (client MockClient) ListContainers(_ context.Context, _ t.Filter) ([]t.Container, error) {
	return client.TestData.Containers, nil
}

// StopContainer is a mock method
func (client MockClient) StopContainer(_ context.Context, c t.Container, _ time.Duration) error {
	if c.Name() == client.TestData.NameOfContainerToKeep {
		return errors.New("tried to stop the instance we want to keep")
	}
	return nil
}

// StartContainer is a mock method
func (client MockClient) StartContainer(_ context.Context, c t.Container) (t.ContainerID, error) {
	if client.TestData.FailStart[c.Name()] {
		return "", errors.New("failed to start the container")
	}
	return "", nil
}

// RollbackContainer is a mock method that records the rollback
func (client MockClient) RollbackContainer(_ context.Context, c t.Container) (t.ContainerID, error) {
	if client.TestData.FailRollback[c.Name()] {
		return "", errors.New("failed to recreate the container")
	}
	client.TestData.RolledBack = append(client.TestData.RolledBack, c.Name())
	return "", nil
}

// RenameContainer is a mock method
func (client MockClient) RenameContainer(_ context.Context, _ t.Container, _ string) error {
	return nil
}

// RemoveImageByID increments the TriedToRemoveImageCount on being called
func (client MockClient) RemoveImageByID(_ context.Context, _ t.ImageID) error {
	client.TestData.TriedToRemoveImageCount++
	return nil
}

// GetContainer is a mock method
func (client MockClient) GetContainer(_ context.Context, _ t.ContainerID) (t.Container, error) {
	return client.TestData.Containers[0], nil
}

// ExecuteCommand is a mock method
func (client MockClient) ExecuteCommand(_ context.Context, _ t.ContainerID, command string, _ int) (SkipUpdate bool, err error) {
	switch command {
	case "/PreUpdateReturn0.sh":
		return false, nil
	case "/PreUpdateReturn1.sh":
		return false, fmt.Errorf("command exited with code 1")
	case "/PreUpdateReturn75.sh":
		return true, nil
	default:
		return false, nil
	}
}

// IsContainerStale is true if not explicitly stated in TestData for the mock client
func (client MockClient) IsContainerStale(_ context.Context, cont t.Container, params t.UpdateParams) (bool, t.ImageID, error) {
	stale, found := client.TestData.Staleness[cont.Name()]
	if !found {
		stale = true
	}
	return stale, "", nil
}

// WarnOnHeadPullFailed is always true for the mock client
func (client MockClient) WarnOnHeadPullFailed(_ t.Container) bool {
	return true
}

// Ping is a mock method, the mock daemon is always reachable
func (client MockClient) Ping(_ context.Context) error {
	return nil
}
