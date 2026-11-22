// Derived from cc-fleet internal/tui/pin.go (Apache-2.0); modified.

package tui

import (
	tea "github.com/charmbracelet/bubbletea"

	"ds-bridge/board/internal/dsjobs"
	"ds-bridge/board/internal/pinned"
	"ds-bridge/board/internal/sessiontitle"
)

// pinSuffix is the fixed-width trailing pin indicator a row appends at its right edge: a gold ★
// when (kind,id) is pinned, else two spaces. The slot is ALWAYS reserved so toggling a pin never
// shifts the row's left content — it only fills or clears the rightmost cell.
func (m Model) pinSuffix(kind pinned.Kind, id string) string {
	if m.pins.Has(kind, id) {
		return " " + pinStyle.Render("★")
	}
	return "  "
}

// boxPinTarget maps the cursored L2 row to its pin (kind,id): a run, a team (live or ended), or
// a job. ok=false off any row.
func (m Model) boxPinTarget() (pinned.Kind, string, bool) {
	ref, ok := m.boxRowRef()
	if !ok {
		return "", "", false
	}
	switch {
	case ref.runID != "":
		return pinned.Run, ref.runID, true
	case ref.jobID != "":
		return pinned.Job, ref.jobID, true
	}
	return "", "", false
}

// togglePin flips (kind,id)'s pin: Pin when currently unpinned, else Unpin. The single marker
// write runs HERE on the Update goroutine — key presses are handled one at a time, so a rapid
// double-press serializes (write then write) and can't race itself. The snapshot is bumped
// optimistically so the ★ flips immediately; the reload re-syncs it from disk. A write failure
// pops an info modal and leaves the snapshot untouched.
func (m Model) togglePin(kind pinned.Kind, id string) (tea.Model, tea.Cmd) {
	want := !m.pins.Has(kind, id)
	var err error
	if want {
		err = pinned.Pin(kind, id)
	} else {
		err = pinned.Unpin(kind, id)
	}
	if err != nil {
		return m.withInfo("pin failed: "+sessiontitle.CleanTitle(err.Error()), true), nil
	}
	m.pins = m.pins.With(kind, id, want)
	return m, loadBoard(m.boardEpoch)
}

// jobCtlMsg carries a job-delete outcome (info modal + reload), epoch-gated; line is ds forget's.
type jobCtlMsg struct {
	err   error
	line  string
	epoch int
}

func (jobCtlMsg) owningScreen() screen { return screenSpawn }

// deleteJobCmd removes a job's files (and its pin) off the Update goroutine.
func deleteJobCmd(jobID string, epoch int) tea.Cmd {
	return func() tea.Msg {
		line, err := dsjobs.DeleteJob(jobID)
		return jobCtlMsg{err: err, line: line, epoch: epoch}
	}
}
