package logging

import (
	"context"
	"io"
	"log/slog"
	"sort"
	"strconv"
	"strings"
	"sync"
)

// prettyHandler writes entries in a format that is easy to read for humans:
//
//	15:04:05 INFO  Found new image  image=nginx:latest
//
// It is not meant to be parsed, use the logfmt or JSON formats for that.
type prettyHandler struct {
	mu    *sync.Mutex
	out   io.Writer
	color bool
}

func newPrettyHandler(out io.Writer, color bool) *prettyHandler {
	return &prettyHandler{mu: &sync.Mutex{}, out: out, color: color}
}

func (h *prettyHandler) Enabled(context.Context, slog.Level) bool { return true }

// WithAttrs and WithGroup are not needed, as the logger passes all the attributes with each record
func (h *prettyHandler) WithAttrs([]slog.Attr) slog.Handler { return h }
func (h *prettyHandler) WithGroup(string) slog.Handler      { return h }

const (
	ansiReset = "\x1b[0m"
	ansiGray  = "\x1b[90m"
)

func levelColor(l Level) string {
	switch l {
	case TraceLevel, DebugLevel:
		return "\x1b[37m" // light gray
	case InfoLevel:
		return "\x1b[36m" // cyan
	case WarnLevel:
		return "\x1b[33m" // yellow
	}
	return "\x1b[31m" // red
}

func levelLabel(l Level) string {
	switch l {
	case TraceLevel:
		return "TRACE"
	case DebugLevel:
		return "DEBUG"
	case InfoLevel:
		return "INFO "
	case WarnLevel:
		return "WARN "
	case ErrorLevel:
		return "ERROR"
	case FatalLevel:
		return "FATAL"
	}
	return "PANIC"
}

func (h *prettyHandler) Handle(_ context.Context, r slog.Record) error {
	level := levelFromSlog(r.Level)

	var sb strings.Builder
	timestamp := r.Time.Format("15:04:05")
	label := levelLabel(level)
	if h.color {
		sb.WriteString(ansiGray + timestamp + ansiReset + " " + levelColor(level) + label + ansiReset)
	} else {
		sb.WriteString(timestamp + " " + label)
	}
	sb.WriteString(" ")
	sb.WriteString(r.Message)

	attrs := make([]slog.Attr, 0, r.NumAttrs())
	r.Attrs(func(a slog.Attr) bool {
		attrs = append(attrs, a)
		return true
	})
	sort.Slice(attrs, func(i, j int) bool { return attrs[i].Key < attrs[j].Key })
	for i, a := range attrs {
		if i == 0 {
			sb.WriteString("  ")
		} else {
			sb.WriteString(" ")
		}
		key := a.Key
		if h.color {
			key = levelColor(level) + key + ansiReset
		}
		sb.WriteString(key + "=" + formatValue(a.Value.String()))
	}
	sb.WriteString("\n")

	h.mu.Lock()
	defer h.mu.Unlock()
	_, err := io.WriteString(h.out, sb.String())
	return err
}

// formatValue quotes values that would be ambiguous or span lines
func formatValue(v string) string {
	if v == "" || strings.ContainsAny(v, " \t\r\n\"=") {
		return strconv.Quote(v)
	}
	return v
}
