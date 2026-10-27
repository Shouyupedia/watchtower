package sorter_test

import (
	"sort"
	"testing"
	"time"

	"github.com/shouyupedia/watchtower/internal/actions/mocks"
	"github.com/shouyupedia/watchtower/pkg/sorter"
	"github.com/shouyupedia/watchtower/pkg/types"
	"github.com/stretchr/testify/assert"
)

func TestByCreatedOrdersOldestFirst(t *testing.T) {
	now := time.Now()
	containers := []types.Container{
		mocks.CreateMockContainer("new", "/new", "img", now),
		mocks.CreateMockContainer("old", "/old", "img", now.Add(-48*time.Hour)),
		mocks.CreateMockContainer("mid", "/mid", "img", now.Add(-24*time.Hour)),
	}

	sort.Sort(sorter.ByCreated(containers))

	assert.Equal(t, []string{"/old", "/mid", "/new"}, []string{containers[0].Name(), containers[1].Name(), containers[2].Name()})
}
