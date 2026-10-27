package lifecycle_test

import (
	"context"
	"errors"
	"testing"

	"github.com/shouyupedia/watchtower/pkg/container"
	"github.com/shouyupedia/watchtower/pkg/lifecycle"
	t "github.com/shouyupedia/watchtower/pkg/types"
)

// failingClient embeds the interface so that only the methods under test have to be implemented;
// calling anything else panics, which doubles as an assertion that nothing else is used.
type failingClient struct {
	container.Client
}

func (failingClient) GetContainer(context.Context, t.ContainerID) (t.Container, error) {
	// Real clients return a nil container together with the error
	return nil, errors.New("no such container")
}

func TestExecutePostUpdateCommandSurvivesLookupFailure(testing *testing.T) {
	defer func() {
		if r := recover(); r != nil {
			testing.Fatalf("ExecutePostUpdateCommand panicked when the new container could not be inspected: %v", r)
		}
	}()

	lifecycle.ExecutePostUpdateCommand(context.Background(), failingClient{}, "abcdef")
}
