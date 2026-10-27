#!/bin/bash

cd $(git rev-parse --show-toplevel)

# Go 1.24 moved wasm_exec.js from misc/wasm to lib/wasm
GOROOT="$(go env GOROOT)"
WASM_EXEC="$GOROOT/lib/wasm/wasm_exec.js"
[ -f "$WASM_EXEC" ] || WASM_EXEC="$GOROOT/misc/wasm/wasm_exec.js"
cp "$WASM_EXEC" ./docs/assets/

GOARCH=wasm GOOS=js go build -o ./docs/assets/tplprev.wasm ./tplprev