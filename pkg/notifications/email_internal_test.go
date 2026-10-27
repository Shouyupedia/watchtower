package notifications

import (
	"testing"

	"github.com/sirupsen/logrus"
	"github.com/stretchr/testify/assert"
)

func TestCheckRemovedSkipVerify(t *testing.T) {
	exited := false
	oldExit := logrus.StandardLogger().ExitFunc
	logrus.StandardLogger().ExitFunc = func(int) { exited = true }
	defer func() { logrus.StandardLogger().ExitFunc = oldExit }()

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
