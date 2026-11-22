package tui

import (
	"fmt"
	"math"
	"strings"
	"testing"

	"ds-bridge/board/internal/dsjobs"
)

func manyLines(prefix string, n int) string {
	var b strings.Builder
	for i := 0; i < n; i++ {
		fmt.Fprintf(&b, "%s %d\n", prefix, i)
	}
	return strings.TrimSpace(b.String())
}

// T7: a running job's card shows "Output · live", follows the bottom as the live text grows,
// stops following after k, follows again at the bottom, reloads on the 500 ms chain, and
// switches to the final report when the job ends.
func TestJobCardLiveOutput(t *testing.T) {
	running := dsjobs.Result{JobID: "j0000000", Status: "running", LeadSessionID: "s",
		StartedAt: "2026-05-26T01:00:00Z"}
	m := boardModel(t, []dsjobs.Result{running})
	if m.asMode != asModeEntity {
		t.Fatalf("setup: mode=%d", m.asMode)
	}
	load := func(m Model, answer string) Model {
		m, _ = step(t, m, asDetailMsg{nonce: m.asDetailNonce, epoch: m.boardEpoch, jobID: "j0000000", present: true, answer: answer})
		return m
	}
	bottom := func(m Model) int { return m.clampAsCardScroll(math.MaxInt32) }

	m = load(m, manyLines("live", 80))
	card := strings.Join(m.entityDetailLines(m.asEntityRightWidth()), "\n")
	if !strings.Contains(card, "Output · live") || !strings.Contains(card, "live 79") {
		t.Fatalf("running card should show the live output:\n%s", card)
	}
	if m.asCardScroll == 0 || m.asCardScroll != bottom(m) {
		t.Fatalf("the live card should sit at the bottom: scroll %d, bottom %d", m.asCardScroll, bottom(m))
	}
	if !strings.Contains(m.View(), "live 79") {
		t.Fatalf("the newest live line should be on screen:\n%s", m.View())
	}
	m = load(m, manyLines("live", 120))
	if m.asCardScroll != bottom(m) {
		t.Fatalf("growing text keeps the card at the bottom: %d vs %d", m.asCardScroll, bottom(m))
	}

	// k holds the scroll; more text does not move it.
	m, _ = press(t, m, "k")
	m, _ = press(t, m, "k")
	held := m.asCardScroll
	m = load(m, manyLines("live", 160))
	if m.asCardScroll != held || m.asCardHeld != "j0000000" {
		t.Fatalf("after k the card must stay put: scroll %d, was %d, held %q", m.asCardScroll, held, m.asCardHeld)
	}
	// j back to the bottom follows again.
	for i := 0; i < 200 && m.asCardScroll < bottom(m); i++ {
		m, _ = press(t, m, "j")
	}
	if m.asCardHeld != "" {
		t.Fatal("j to the bottom should release the hold")
	}
	m = load(m, manyLines("live", 200))
	if m.asCardScroll != bottom(m) {
		t.Fatalf("released card follows again: %d vs %d", m.asCardScroll, bottom(m))
	}

	// The 500 ms chain reloads a running card.
	if _, cmd := step(t, m, wfRefreshMsg{jobs: []dsjobs.Result{running}, epoch: m.boardEpoch, seq: 1}); cmd == nil {
		t.Fatal("the light refresh must reload a running job's card")
	}

	// The job ends: the final report replaces the live text.
	done := running
	done.Status = "done"
	m, cmd := step(t, m, boardMsg{jobs: []dsjobs.Result{done}, epoch: m.boardEpoch, seq: 2})
	if cmd == nil {
		t.Fatal("the running→done refresh must reload the card")
	}
	m = load(m, manyLines("final", 60))
	card = strings.Join(m.entityDetailLines(m.asEntityRightWidth()), "\n")
	if strings.Contains(card, "Output · live") || strings.Contains(card, "live 199") || !strings.Contains(card, "final 59") {
		t.Fatalf("done card should show the report under Output:\n%s", card)
	}
	if m.asCardScroll != bottom(m) {
		t.Fatalf("a followed card lands the final report at the bottom (Outcome): %d vs %d", m.asCardScroll, bottom(m))
	}
	// A done job focused fresh starts at the top.
	mm, _ := m.focusJobIO()
	m = load(mm.(Model), manyLines("final", 60))
	if m.asCardScroll != 0 {
		t.Fatalf("a done card opened fresh starts at the top, got %d", m.asCardScroll)
	}
}

// T7: the run drill's agent card reloads while its leaf runs (it loaded only on focus before)
// and follows the bottom.
func TestWfAgentCardLiveOutput(t *testing.T) {
	jobs, runs := oneRun()
	m := drillRun(t, runsModel(t, jobs, runs, nil))
	m, _ = press(t, m, "down")
	m, _ = press(t, m, "enter")
	if m.asMode != asModeRunAgent {
		t.Fatalf("setup: mode=%d", m.asMode)
	}
	leaf, _ := m.selectedLeaf()
	m, _ = step(t, m, wfDetailMsg{nonce: m.wfDetailNonce, epoch: m.boardEpoch, job: leaf, present: true, answer: manyLines("leaf", 80)})
	if m.wfCardScroll == 0 || m.wfCardScroll != m.clampCardScroll(math.MaxInt32) {
		t.Fatalf("the live leaf card should sit at the bottom: %d", m.wfCardScroll)
	}
	card := strings.Join(m.agentDetailLines(m.wfAgentRightWidth()), "\n")
	if !strings.Contains(card, "Output · live") || !strings.Contains(m.View(), "leaf 79") {
		t.Fatalf("live leaf output not on screen:\n%s", m.View())
	}
	if _, cmd := step(t, m, wfRefreshMsg{jobs: jobs, runs: runs, epoch: m.boardEpoch, seq: 5}); cmd == nil {
		t.Fatal("the light refresh must reload a running leaf's card")
	}
}
