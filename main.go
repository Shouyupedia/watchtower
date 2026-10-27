package main

import (
	"github.com/shouyupedia/watchtower/cmd"
	log "github.com/shouyupedia/watchtower/internal/logging"
)

func init() {
	log.SetLevel(log.InfoLevel)
}

func main() {
	cmd.Execute()
}
