package logging

import (
	"io"
	"log/slog"
	"os"
	"strings"
)

// Format is how entries are written
type Format int

const (
	// FormatAuto uses FormatPretty if the output is a terminal, and FormatLogfmt otherwise
	FormatAuto Format = iota
	// FormatPretty is meant to be read by people
	FormatPretty
	// FormatLogfmt writes key=value pairs, one entry per line
	FormatLogfmt
	// FormatJSON writes a JSON object per line
	FormatJSON
)

// ParseFormat converts the name of a format, ignoring case
func ParseFormat(name string) (Format, bool) {
	switch strings.ToLower(name) {
	case "auto":
		return FormatAuto, true
	case "pretty":
		return FormatPretty, true
	case "logfmt":
		return FormatLogfmt, true
	case "json":
		return FormatJSON, true
	}
	return 0, false
}

// Options configures the output
type Options struct {
	Format Format
	// NoColor turns colors off, in the formats that have them
	NoColor bool
}

// buildHandler creates the handler for the configured format. The levels are filtered by the logger already,
// so the handlers log everything they are given.
func (c *core) buildHandler() slog.Handler {
	format := c.opts.Format
	isTerminal := isTerminal(c.out)

	if format == FormatAuto {
		if isTerminal {
			format = FormatPretty
		} else {
			format = FormatLogfmt
		}
	}

	switch format {
	case FormatJSON:
		return slog.NewJSONHandler(c.out, &slog.HandlerOptions{Level: slogTrace, ReplaceAttr: renameLevel})
	case FormatLogfmt:
		return slog.NewTextHandler(c.out, &slog.HandlerOptions{Level: slogTrace, ReplaceAttr: renameLevel})
	}

	// FormatPretty is colored unless that is turned off or the environment says otherwise. The environment
	// follows https://bixense.com/clicolors/ and https://no-color.org/ and gets to override a terminal
	// being detected, but not an explicit request for no colors.
	color := !c.opts.NoColor
	if color && c.opts.Format == FormatAuto {
		color = isTerminal
	}
	if color {
		color = envAllowsColor()
	}
	return newPrettyHandler(c.out, color)
}

// renameLevel makes the level show up under the same names as it always has, instead of slog's upper case ones
func renameLevel(_ []string, a slog.Attr) slog.Attr {
	if a.Key == slog.LevelKey {
		if level, ok := a.Value.Any().(slog.Level); ok {
			return slog.String(slog.LevelKey, levelFromSlog(level).String())
		}
	}
	return a
}

func envAllowsColor() bool {
	if os.Getenv("NO_COLOR") != "" {
		return false
	}
	if force := os.Getenv("CLICOLOR_FORCE"); force != "" && force != "0" {
		return true
	}
	return os.Getenv("CLICOLOR") != "0"
}

func isTerminal(w io.Writer) bool {
	f, ok := w.(*os.File)
	if !ok {
		return false
	}
	info, err := f.Stat()
	return err == nil && info.Mode()&os.ModeCharDevice != 0
}
