package notifications

import (
	"testing"

	log "github.com/shouyupedia/watchtower/internal/logging"
	"github.com/stretchr/testify/assert"
)

func TestCheckRemovedSkipVerify(t *testing.T) {
	exited := false
	defer log.SetExitFunc(func(int) { exited = true })()

	for value, wantExit := range map[string]bool{
		"":      false,
		"false": false,
		"0":     false,
		"true":  true,
		"1":     true,
		"maybe": true,
	} {
		exited = false
		checkRemovedSkipVerify(value)
		assert.Equal(t, wantExit, exited, "value %q", value)
	}
}
