package logging

import (
	"fmt"
	"log/slog"
	"strings"
)

// Level is the severity of a log entry. Lower values are more severe, so a logger set to a level logs that
// level and everything more severe.
type Level uint32

// The levels, from most to least severe
const (
	PanicLevel Level = iota
	FatalLevel
	ErrorLevel
	WarnLevel
	InfoLevel
	DebugLevel
	TraceLevel
)

// AllLevels lists all levels, from most to least severe
var AllLevels = []Level{PanicLevel, FatalLevel, ErrorLevel, WarnLevel, InfoLevel, DebugLevel, TraceLevel}

// String returns the name of the level, which is also what is shown in logs and notifications
func (l Level) String() string {
	switch l {
	case PanicLevel:
		return "panic"
	case FatalLevel:
		return "fatal"
	case ErrorLevel:
		return "error"
	case WarnLevel:
		return "warning"
	case InfoLevel:
		return "info"
	case DebugLevel:
		return "debug"
	case TraceLevel:
		return "trace"
	}
	return "unknown"
}

// MarshalText makes levels appear by name in JSON
func (l Level) MarshalText() ([]byte, error) {
	return []byte(l.String()), nil
}

// ParseLevel converts the name of a level, ignoring case. Both "warn" and "warning" are accepted.
func ParseLevel(name string) (Level, error) {
	switch strings.ToLower(name) {
	case "panic":
		return PanicLevel, nil
	case "fatal":
		return FatalLevel, nil
	case "error":
		return ErrorLevel, nil
	case "warn", "warning":
		return WarnLevel, nil
	case "info":
		return InfoLevel, nil
	case "debug":
		return DebugLevel, nil
	case "trace":
		return TraceLevel, nil
	}
	return 0, fmt.Errorf("not a valid log level: %q", name)
}

// slog has no levels below debug or above error, so the extra ones are placed in the gaps it leaves
const (
	slogTrace = slog.Level(-8)
	slogFatal = slog.Level(12)
	slogPanic = slog.Level(16)
)

func (l Level) slog() slog.Level {
	switch l {
	case PanicLevel:
		return slogPanic
	case FatalLevel:
		return slogFatal
	case ErrorLevel:
		return slog.LevelError
	case WarnLevel:
		return slog.LevelWarn
	case InfoLevel:
		return slog.LevelInfo
	case DebugLevel:
		return slog.LevelDebug
	}
	return slogTrace
}

func levelFromSlog(l slog.Level) Level {
	switch {
	case l >= slogPanic:
		return PanicLevel
	case l >= slogFatal:
		return FatalLevel
	case l >= slog.LevelError:
		return ErrorLevel
	case l >= slog.LevelWarn:
		return WarnLevel
	case l >= slog.LevelInfo:
		return InfoLevel
	case l >= slog.LevelDebug:
		return DebugLevel
	}
	return TraceLevel
}
