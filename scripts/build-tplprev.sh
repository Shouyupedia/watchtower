#!/bin/bash
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

cp "$(go env GOROOT)/lib/wasm/wasm_exec.js" ./docs/assets/

GOARCH=wasm GOOS=js go build -o ./docs/assets/tplprev.wasm ./tplprev
