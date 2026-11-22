// Derived from cc-fleet internal/tui/workflow_events.go (Apache-2.0); modified.

package tui

import "ds-bridge/board/internal/dsjobs"

// This file holds the board's per-leaf data projections: the live tool-call + token activity read
// from the leaf's activity file, and the prompt/answer io for the focused agent's inline
// detail. The board renders per-leaf activity + the final Result metrics, not the raw stream.

// activitySnapshot is one leaf's live activity, read from its <jobID>.activity sidecar: the ordered
// tool-call signatures ("Tool(arg)") and the latest running token snapshot. It is what makes the
// board's per-agent token + tool counts climb WHILE a sync leaf runs (before its final Result caches).
type activitySnapshot struct {
	sigs          []string // tool-call signatures in arrival order
	inTok, outTok int
	hasUsage      bool
	attempt       int  // the leaf's exec ordinal this snapshot belongs to; a restart bumps it and resets the floor
	missing       bool // ds: the job has no activity.jsonl (a job from before the board)
}

// toolCount is the number of tool calls the leaf has made so far.
func (s activitySnapshot) toolCount() int { return len(s.sigs) }

// lastSigs returns the most-recent n signatures (fewer if the leaf made fewer).
func (s activitySnapshot) lastSigs(n int) []string {
	if len(s.sigs) <= n {
		return s.sigs
	}
	return s.sigs[len(s.sigs)-n:]
}

// readLeafActivity reads a leaf's activity (ds: JOBS/<id>/activity.jsonl) into a snapshot via
// dsjobs. A missing file degrades to (zero, false). The tool args are model content already
// masked at write; the board CleanTitle-scrubs them at render — never the answer, never the key.
func readLeafActivity(jobID string) (activitySnapshot, bool) {
	a, ok := dsjobs.ReadActivity(jobID)
	if !ok {
		return activitySnapshot{missing: true}, false
	}
	return activitySnapshot{sigs: a.Sigs, inTok: a.InTok, outTok: a.OutTok, hasUsage: a.HasUsage}, true
}

// readLeafIO reads a leaf's prompt and final output via dsjobs (ds: prompt.txt and report.txt),
// plus whether either was present. The answer text returned here reaches only the focused
// agent's inline detail pane.
func readLeafIO(jobID string) (prompt, answer string, present bool) {
	return dsjobs.ReadIO(jobID)
}
