package actions_test

import (
	"errors"
	"testing"

	"github.com/containrrr/watchtower/internal/actions"
	"github.com/containrrr/watchtower/pkg/container"
	"github.com/containrrr/watchtower/pkg/types"
)

type listFailureClient struct {
	container.Client
}

func (listFailureClient) ListContainers(types.Filter) ([]types.Container, error) {
	return nil, errors.New("Docker unavailable")
}

func TestUpdateReturnsReportOnListFailure(t *testing.T) {
	report, err := actions.Update(listFailureClient{}, types.UpdateParams{})
	if err == nil || report == nil {
		t.Fatal("failed scan must return an error and a non-nil report")
	}
	if len(report.All()) != 0 {
		t.Fatal("failed container listing must not invent container results")
	}
}
