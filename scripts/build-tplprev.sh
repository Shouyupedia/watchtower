#!/bin/bash
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

rm -f ./docs/assets/wasm_exec.js
install -m 0644 "$(go env GOROOT)/lib/wasm/wasm_exec.js" ./docs/assets/wasm_exec.js

GOARCH=wasm GOOS=js go build -o ./docs/assets/tplprev.wasm ./tplprev
