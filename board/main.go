// ds-board: the ds-bridge jobs board, a port of cc-fleet's Agents Board.
package main

import (
	"flag"
	"fmt"
	"os"
	"path/filepath"

	"ds-bridge/board/internal/dsjobs"
	"ds-bridge/board/internal/pinned"
	"ds-bridge/board/internal/tui"
)

const version = "0.1.0"

func main() {
	home, _ := os.UserHomeDir()
	state := flag.String("state", filepath.Join(home, ".local/share/ds-bridge"), "ds state dir")
	showVersion := flag.Bool("version", false, "print the version and exit")
	flag.Parse()
	if *showVersion {
		fmt.Println("ds-board " + version)
		return
	}
	dsjobs.StateDir = *state
	pinned.StateDir = *state
	if err := tui.Run(); err != nil {
		fmt.Fprintln(os.Stderr, "ds-board:", err)
		os.Exit(1)
	}
}
