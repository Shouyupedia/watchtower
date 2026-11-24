//go:build ignore

// Extract every `uses:` scalar value from one or more GitHub Actions YAML files
// (handling both block and flow-style mappings) and print tab-separated records:
//
//	<file>\t<line>\t<ref>\t<comment>
//
// Invoked by scripts/verify-action-pins.sh using tools/go.mod's pinned yaml.v3.
package main

import (
	"fmt"
	"os"
	"strings"

	"gopkg.in/yaml.v3"
)

func main() {
	for _, path := range os.Args[1:] {
		raw, err := os.ReadFile(path)
		if err != nil {
			fmt.Fprintf(os.Stderr, "extract_action_uses: %s: %v\n", path, err)
			os.Exit(1)
		}
		var doc yaml.Node
		if err := yaml.Unmarshal(raw, &doc); err != nil {
			fmt.Fprintf(os.Stderr, "extract_action_uses: %s: invalid YAML: %v\n", path, err)
			os.Exit(1)
		}
		lines := strings.Split(strings.ReplaceAll(string(raw), "\r\n", "\n"), "\n")
		walk(path, &doc, lines)
	}
}

func walk(path string, node *yaml.Node, lines []string) {
	if node == nil {
		return
	}
	if node.Kind == yaml.MappingNode {
		for i := 0; i+1 < len(node.Content); i += 2 {
			k := node.Content[i]
			v := node.Content[i+1]
			if k.Kind == yaml.ScalarNode && k.Value == "uses" {
				if v.Kind != yaml.ScalarNode {
					fmt.Fprintf(os.Stderr, "extract_action_uses: %s:%d: non-scalar uses value\n", path, v.Line)
					os.Exit(1)
				}
				comment := strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(v.LineComment), "#"))
				if comment == "" && v.Line >= 1 && v.Line <= len(lines) {
					comment = trailingComment(lines[v.Line-1])
				}
				fmt.Printf("%s\t%d\t%s\t%s\n", path, v.Line, strings.TrimSpace(v.Value), comment)
				continue
			}
			walk(path, v, lines)
		}
		return
	}
	for _, child := range node.Content {
		walk(path, child, lines)
	}
}

func trailingComment(line string) string {
	var quote byte
	for i := 0; i < len(line); i++ {
		ch := line[i]
		if quote != 0 {
			if ch == '\\' && quote == '"' && i+1 < len(line) {
				i++
				continue
			}
			if ch == quote {
				quote = 0
			}
			continue
		}
		if ch == '"' || ch == '\'' {
			quote = ch
		} else if ch == '#' && (i == 0 || line[i-1] == ' ' || line[i-1] == '\t') {
			return strings.TrimSpace(line[i+1:])
		}
	}
	return ""
}
