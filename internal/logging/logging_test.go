package logging

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"strings"
	"sync"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// capture sends the output of the default logger to a buffer for the duration of a test
func capture(t *testing.T, opts Options, level Level) *bytes.Buffer {
	t.Helper()
	buf := &bytes.Buffer{}
	SetOutput(buf)
	Configure(opts)
	SetLevel(level)
	t.Cleanup(func() {
		SetOutput(os.Stderr)
		Configure(Options{})
		SetLevel(InfoLevel)
	})
	return buf
}

func TestParseLevel(t *testing.T) {
	for name, want := range map[string]Level{
		"panic": PanicLevel, "FATAL": FatalLevel, "Error": ErrorLevel, "warn": WarnLevel,
		"warning": WarnLevel, "info": InfoLevel, "debug": DebugLevel, "trace": TraceLevel,
	} {
		got, err := ParseLevel(name)
		require.NoError(t, err, name)
		assert.Equal(t, want, got, name)
	}
	_, err := ParseLevel("loud")
	assert.Error(t, err)
}

func TestLevelNamesAndSlogMapping(t *testing.T) {
	for _, level := range AllLevels {
		parsed, err := ParseLevel(level.String())
		require.NoError(t, err)
		assert.Equal(t, level, parsed)
		assert.Equal(t, level, levelFromSlog(level.slog()), "round trip through slog")
	}
	assert.Equal(t, "warning", WarnLevel.String(), "the name used in logs and notification templates")

	text, err := InfoLevel.MarshalText()
	require.NoError(t, err)
	assert.Equal(t, "info", string(text))
}

func TestLevelsAreOrderedBySeverity(t *testing.T) {
	for i := 1; i < len(AllLevels); i++ {
		assert.Greater(t, AllLevels[i-1].slog(), AllLevels[i].slog(), "more severe levels have higher slog values")
	}
}

func TestLevelFiltering(t *testing.T) {
	buf := capture(t, Options{Format: FormatLogfmt}, WarnLevel)

	Debug("hidden")
	Info("hidden too")
	Warn("shown")
	Error("shown as well")

	out := buf.String()
	assert.NotContains(t, out, "hidden")
	assert.Contains(t, out, "msg=shown")
	assert.Equal(t, 2, strings.Count(out, "\n"))
	assert.True(t, IsLevelEnabled(ErrorLevel))
	assert.False(t, IsLevelEnabled(InfoLevel))
}

func TestLogfmtOutput(t *testing.T) {
	buf := capture(t, Options{Format: FormatLogfmt}, TraceLevel)

	WithFields(Fields{"container": "web", "count": 3}).WithError(errors.New("boom")).Warnf("Update of %s failed", "web")

	line := buf.String()
	assert.Contains(t, line, "level=warning")
	assert.Contains(t, line, `msg="Update of web failed"`)
	assert.Contains(t, line, "container=web")
	assert.Contains(t, line, "count=3")
	assert.Contains(t, line, "error=boom")
	assert.Contains(t, line, "time=")
}

func TestJSONOutput(t *testing.T) {
	buf := capture(t, Options{Format: FormatJSON}, TraceLevel)

	WithField("container", "web").Info("hello ", "world")

	var entry map[string]any
	require.NoError(t, json.Unmarshal(buf.Bytes(), &entry), buf.String())
	assert.Equal(t, "info", entry["level"])
	assert.Equal(t, "hello world", entry["msg"])
	assert.Equal(t, "web", entry["container"])
	assert.NotEmpty(t, entry["time"])
}

func TestPrettyOutputIsReadable(t *testing.T) {
	buf := capture(t, Options{Format: FormatPretty, NoColor: true}, TraceLevel)

	WithFields(Fields{"image": "nginx:latest", "note": "has spaces"}).Info("Found new image")
	Trace("fine detail")

	lines := strings.Split(strings.TrimSpace(buf.String()), "\n")
	require.Len(t, lines, 2)
	assert.Regexp(t, `^\d{2}:\d{2}:\d{2} INFO  Found new image  image=nginx:latest note="has spaces"$`, lines[0])
	assert.Regexp(t, `^\d{2}:\d{2}:\d{2} TRACE fine detail$`, lines[1])
	assert.NotContains(t, buf.String(), "\x1b[", "no colors were asked for")
}

func TestPrettyOutputColors(t *testing.T) {
	t.Setenv("NO_COLOR", "")
	t.Setenv("CLICOLOR", "")
	t.Setenv("CLICOLOR_FORCE", "")

	buf := capture(t, Options{Format: FormatPretty}, TraceLevel)
	Warn("colorful")
	assert.Contains(t, buf.String(), "\x1b[33mWARN \x1b[0m")

	t.Setenv("NO_COLOR", "1")
	buf = capture(t, Options{Format: FormatPretty}, TraceLevel)
	Warn("plain")
	assert.NotContains(t, buf.String(), "\x1b[")
}

func TestAutoFormatIsLogfmtWhenNotWritingToATerminal(t *testing.T) {
	buf := capture(t, Options{Format: FormatAuto}, InfoLevel)

	Info("machine readable")

	assert.Contains(t, buf.String(), "level=info")
}

func TestParseFormat(t *testing.T) {
	for name, want := range map[string]Format{"auto": FormatAuto, "PRETTY": FormatPretty, "logfmt": FormatLogfmt, "Json": FormatJSON} {
		got, ok := ParseFormat(name)
		assert.True(t, ok, name)
		assert.Equal(t, want, got, name)
	}
	_, ok := ParseFormat("xml")
	assert.False(t, ok)
}

type recordingHook struct {
	mu      sync.Mutex
	levels  []Level
	entries []*Entry
}

func (h *recordingHook) Levels() []Level { return h.levels }
func (h *recordingHook) Fire(e *Entry) error {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.entries = append(h.entries, e)
	return nil
}

func TestHooksReceiveEntriesAtTheirLevels(t *testing.T) {
	capture(t, Options{Format: FormatLogfmt}, DebugLevel)
	hook := &recordingHook{levels: AllLevels[:InfoLevel+1]}
	before := HookCount(InfoLevel)
	AddHook(hook)
	assert.Equal(t, before+1, HookCount(InfoLevel))

	WithField("notify", "no").Info("with a field")
	Debug("below the level of the hook")
	Error("an error")

	require.Len(t, hook.entries, 2)
	assert.Equal(t, InfoLevel, hook.entries[0].Level)
	assert.Equal(t, "with a field", hook.entries[0].Message)
	assert.Equal(t, "no", hook.entries[0].Data["notify"])
	assert.False(t, hook.entries[0].Time.IsZero())
	assert.Equal(t, ErrorLevel, hook.entries[1].Level)
}

func TestHooksDoNotSeeEntriesThatAreNotLogged(t *testing.T) {
	capture(t, Options{Format: FormatLogfmt}, ErrorLevel)
	hook := &recordingHook{levels: AllLevels}
	AddHook(hook)

	Info("not logged, so not passed on")

	assert.Empty(t, hook.entries)
}

func TestFatalExits(t *testing.T) {
	buf := capture(t, Options{Format: FormatLogfmt}, InfoLevel)
	code := -1
	restore := SetExitFunc(func(c int) { code = c })
	defer restore()

	Fatalf("cannot continue: %s", "reason")

	assert.Equal(t, 1, code)
	assert.Contains(t, buf.String(), "level=fatal")
	assert.Contains(t, buf.String(), "cannot continue: reason")
}

func TestFatalExitsEvenIfNothingIsLogged(t *testing.T) {
	capture(t, Options{Format: FormatLogfmt}, PanicLevel)
	code := -1
	restore := SetExitFunc(func(c int) { code = c })
	defer restore()

	Fatal("quiet")

	assert.Equal(t, 1, code)
}

func TestStdLoggerLogsEachLine(t *testing.T) {
	buf := capture(t, Options{Format: FormatLogfmt}, TraceLevel)

	Default().StdLogger("Shoutrrr: ", TraceLevel).Print("first\nsecond")

	out := buf.String()
	assert.Equal(t, 2, strings.Count(out, "level=trace"))
	assert.Contains(t, out, `msg="Shoutrrr: first"`)
	assert.Contains(t, out, "msg=second")
}

func TestDerivedLoggersDoNotShareFields(t *testing.T) {
	buf := capture(t, Options{Format: FormatLogfmt}, InfoLevel)
	base := WithField("a", 1)
	derived := base.WithField("b", 2)

	base.Info("base")
	derived.Info("derived")

	lines := strings.Split(strings.TrimSpace(buf.String()), "\n")
	require.Len(t, lines, 2)
	assert.NotContains(t, lines[0], "b=2")
	assert.Contains(t, lines[1], "a=1")
	assert.Contains(t, lines[1], "b=2")
}

func TestConcurrentUse(t *testing.T) {
	capture(t, Options{Format: FormatPretty, NoColor: true}, InfoLevel)
	hook := &recordingHook{levels: AllLevels}
	AddHook(hook)

	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 100; j++ {
				WithField("n", j).Info("message")
				SetLevel(InfoLevel)
				_ = IsLevelEnabled(DebugLevel)
			}
		}()
	}
	wg.Wait()

	assert.Len(t, hook.entries, 800)
}
