// Package logging is the logger of watchtower. It writes through log/slog, and additionally lets hooks (such as
// the notifier) receive the entries that are logged.
//
// The API follows what watchtower's code has always used for logging: printf style messages with optional fields.
package logging

import (
	"context"
	"fmt"
	"io"
	"log"
	"log/slog"
	"os"
	"sort"
	"strings"
	"sync"
	"time"
)

// Fields are the key-value pairs that can be attached to an entry
type Fields map[string]any

// Entry is a log entry as seen by hooks, and by notification templates
type Entry struct {
	Time    time.Time
	Level   Level
	Message string
	Data    Fields
}

// Hook is called for the entries logged at the levels it asks for
type Hook interface {
	Levels() []Level
	Fire(*Entry) error
}

// core holds the state shared by a logger and the loggers derived from it
type core struct {
	mu      sync.RWMutex
	level   Level
	out     io.Writer
	opts    Options
	handler slog.Handler
	hooks   map[Level][]Hook
	exit    func(int)
}

// Logger logs entries. The loggers returned by WithField, WithFields and WithError add fields to all they log.
type Logger struct {
	c      *core
	fields Fields
}

var std = newLogger()

func newLogger() *Logger {
	c := &core{
		level: InfoLevel,
		out:   os.Stderr,
		hooks: map[Level][]Hook{},
		exit:  os.Exit,
	}
	c.handler = c.buildHandler()
	return &Logger{c: c}
}

// Default returns the logger used by the package level functions
func Default() *Logger { return std }

// SetLevel sets the least severe level that is logged
func SetLevel(level Level) {
	std.c.mu.Lock()
	defer std.c.mu.Unlock()
	std.c.level = level
}

// GetLevel returns the least severe level that is logged
func GetLevel() Level {
	std.c.mu.RLock()
	defer std.c.mu.RUnlock()
	return std.c.level
}

// IsLevelEnabled returns whether entries at the level are logged
func IsLevelEnabled(level Level) bool { return std.IsLevelEnabled(level) }

// SetOutput sets where entries are written, standard error by default
func SetOutput(w io.Writer) {
	std.c.mu.Lock()
	defer std.c.mu.Unlock()
	std.c.out = w
	std.c.handler = std.c.buildHandler()
}

// Output returns where entries are written
func Output() io.Writer {
	std.c.mu.RLock()
	defer std.c.mu.RUnlock()
	return std.c.out
}

// CurrentOptions returns the options the output was configured with
func CurrentOptions() Options {
	std.c.mu.RLock()
	defer std.c.mu.RUnlock()
	return std.c.opts
}

// Configure sets the format of the output
func Configure(opts Options) {
	std.c.mu.Lock()
	defer std.c.mu.Unlock()
	std.c.opts = opts
	std.c.handler = std.c.buildHandler()
}

// SetExitFunc replaces the function called after a fatal entry (os.Exit by default), returning one that restores it.
// It is meant for tests.
func SetExitFunc(exit func(int)) (restore func()) {
	std.c.mu.Lock()
	defer std.c.mu.Unlock()
	previous := std.c.exit
	std.c.exit = exit
	return func() {
		std.c.mu.Lock()
		defer std.c.mu.Unlock()
		std.c.exit = previous
	}
}

// AddHook registers a hook for the levels it asks for
func AddHook(hook Hook) {
	std.c.mu.Lock()
	defer std.c.mu.Unlock()
	for _, level := range hook.Levels() {
		// Copy on write, so that loggers firing hooks never see the slice change underneath them
		hooks := append(append([]Hook(nil), std.c.hooks[level]...), hook)
		std.c.hooks[level] = hooks
	}
}

// HookCount returns the number of hooks registered for the level
func HookCount(level Level) int {
	std.c.mu.RLock()
	defer std.c.mu.RUnlock()
	return len(std.c.hooks[level])
}

// IsLevelEnabled returns whether entries at the level are logged
func (l *Logger) IsLevelEnabled(level Level) bool {
	l.c.mu.RLock()
	defer l.c.mu.RUnlock()
	return level <= l.c.level
}

// WithField returns a logger that adds the field to its entries
func (l *Logger) WithField(key string, value any) *Logger {
	return l.WithFields(Fields{key: value})
}

// WithFields returns a logger that adds the fields to its entries
func (l *Logger) WithFields(fields Fields) *Logger {
	merged := make(Fields, len(l.fields)+len(fields))
	for k, v := range l.fields {
		merged[k] = v
	}
	for k, v := range fields {
		merged[k] = v
	}
	return &Logger{c: l.c, fields: merged}
}

// WithError returns a logger that adds the error as the field "error"
func (l *Logger) WithError(err error) *Logger {
	return l.WithField("error", err)
}

func (l *Logger) log(level Level, msg string) {
	c := l.c
	c.mu.RLock()
	enabled := level <= c.level
	handler := c.handler
	hooks := c.hooks[level]
	c.mu.RUnlock()
	if !enabled {
		return
	}

	now := time.Now()
	record := slog.NewRecord(now, level.slog(), msg, 0)
	keys := make([]string, 0, len(l.fields))
	for k := range l.fields {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		record.AddAttrs(slog.Any(k, l.fields[k]))
	}
	_ = handler.Handle(context.Background(), record)

	if len(hooks) > 0 {
		data := make(Fields, len(l.fields))
		for k, v := range l.fields {
			data[k] = v
		}
		entry := &Entry{Time: now, Level: level, Message: msg, Data: data}
		for _, hook := range hooks {
			if err := hook.Fire(entry); err != nil {
				fmt.Fprintf(os.Stderr, "Failed to fire log hook: %v\n", err)
			}
		}
	}
}

func (l *Logger) exitNow() {
	l.c.mu.RLock()
	exit := l.c.exit
	l.c.mu.RUnlock()
	exit(1)
}

// Log logs the arguments, formatted like fmt.Sprint does, at the level
func (l *Logger) Log(level Level, args ...any) { l.log(level, fmt.Sprint(args...)) }

// Logf logs a formatted message at the level
func (l *Logger) Logf(level Level, format string, args ...any) {
	if l.IsLevelEnabled(level) {
		l.log(level, fmt.Sprintf(format, args...))
	}
}

// Trace logs at the trace level
func (l *Logger) Trace(args ...any) { l.Log(TraceLevel, args...) }

// Debug logs at the debug level
func (l *Logger) Debug(args ...any) { l.Log(DebugLevel, args...) }

// Info logs at the info level
func (l *Logger) Info(args ...any) { l.Log(InfoLevel, args...) }

// Warn logs at the warning level
func (l *Logger) Warn(args ...any) { l.Log(WarnLevel, args...) }

// Error logs at the error level
func (l *Logger) Error(args ...any) { l.Log(ErrorLevel, args...) }

// Tracef logs a formatted message at the trace level
func (l *Logger) Tracef(format string, args ...any) { l.Logf(TraceLevel, format, args...) }

// Debugf logs a formatted message at the debug level
func (l *Logger) Debugf(format string, args ...any) { l.Logf(DebugLevel, format, args...) }

// Infof logs a formatted message at the info level
func (l *Logger) Infof(format string, args ...any) { l.Logf(InfoLevel, format, args...) }

// Warnf logs a formatted message at the warning level
func (l *Logger) Warnf(format string, args ...any) { l.Logf(WarnLevel, format, args...) }

// Errorf logs a formatted message at the error level
func (l *Logger) Errorf(format string, args ...any) { l.Logf(ErrorLevel, format, args...) }

// Fatal logs at the fatal level, and exits
func (l *Logger) Fatal(args ...any) {
	l.log(FatalLevel, fmt.Sprint(args...))
	l.exitNow()
}

// Fatalf logs a formatted message at the fatal level, and exits
func (l *Logger) Fatalf(format string, args ...any) {
	l.log(FatalLevel, fmt.Sprintf(format, args...))
	l.exitNow()
}

// StdLogger returns a standard library logger that logs each line written to it at the level, for libraries
// that want one. The prefix is added to the lines.
func (l *Logger) StdLogger(prefix string, level Level) *log.Logger {
	return log.New(&lineWriter{logger: l, level: level}, prefix, 0)
}

type lineWriter struct {
	logger *Logger
	level  Level
}

func (w *lineWriter) Write(p []byte) (int, error) {
	for _, line := range strings.Split(strings.TrimRight(string(p), "\r\n"), "\n") {
		w.logger.log(w.level, strings.TrimRight(line, "\r"))
	}
	return len(p), nil
}

// The package level functions log with the default logger

// WithField returns a logger that adds the field to its entries
func WithField(key string, value any) *Logger { return std.WithField(key, value) }

// WithFields returns a logger that adds the fields to its entries
func WithFields(fields Fields) *Logger { return std.WithFields(fields) }

// WithError returns a logger that adds the error as the field "error"
func WithError(err error) *Logger { return std.WithError(err) }

// Trace logs at the trace level
func Trace(args ...any) { std.Trace(args...) }

// Debug logs at the debug level
func Debug(args ...any) { std.Debug(args...) }

// Info logs at the info level
func Info(args ...any) { std.Info(args...) }

// Warn logs at the warning level
func Warn(args ...any) { std.Warn(args...) }

// Error logs at the error level
func Error(args ...any) { std.Error(args...) }

// Fatal logs at the fatal level, and exits
func Fatal(args ...any) { std.Fatal(args...) }

// Tracef logs a formatted message at the trace level
func Tracef(format string, args ...any) { std.Tracef(format, args...) }

// Debugf logs a formatted message at the debug level
func Debugf(format string, args ...any) { std.Debugf(format, args...) }

// Infof logs a formatted message at the info level
func Infof(format string, args ...any) { std.Infof(format, args...) }

// Warnf logs a formatted message at the warning level
func Warnf(format string, args ...any) { std.Warnf(format, args...) }

// Errorf logs a formatted message at the error level
func Errorf(format string, args ...any) { std.Errorf(format, args...) }

// Fatalf logs a formatted message at the fatal level, and exits
func Fatalf(format string, args ...any) { std.Fatalf(format, args...) }
