package lifecycle

import (
	"errors"
	"testing"

	"github.com/containrrr/watchtower/pkg/container"
	"github.com/containrrr/watchtower/pkg/types"
)

type failedInspectClient struct {
	container.Client
}

func (failedInspectClient) GetContainer(types.ContainerID) (types.Container, error) {
	return nil, errors.New("container unavailable")
}

func TestPostUpdateHandlesInspectionError(t *testing.T) {
	// The old implementation dereferenced the nil container before checking err.
	ExecutePostUpdateCommand(failedInspectClient{}, types.ContainerID("dummy-container"))
}
