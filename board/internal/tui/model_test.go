// Derived from cc-fleet internal/tui/model_test.go (Apache-2.0); modified.

package tui

import (
	"errors"
	"strings"
	"testing"

	tea "github.com/charmbracelet/bubbletea"

	"ds-bridge/board/internal/dsjobs"
)

// keyMsg builds a tea.KeyMsg for a key name. Single special keys map to their
// KeyType; anything else is treated as typed runes.
func keyMsg(s string) tea.KeyMsg {
	switch s {
	case "up":
		return tea.KeyMsg{Type: tea.KeyUp}
	case "down":
		return tea.KeyMsg{Type: tea.KeyDown}
	case "left":
		return tea.KeyMsg{Type: tea.KeyLeft}
	case "right":
		return tea.KeyMsg{Type: tea.KeyRight}
	case "enter":
		return tea.KeyMsg{Type: tea.KeyEnter}
	case "esc":
		return tea.KeyMsg{Type: tea.KeyEsc}
	case "tab":
		return tea.KeyMsg{Type: tea.KeyTab}
	case "space":
		return tea.KeyMsg{Type: tea.KeySpace}
	case "ctrl+c":
		return tea.KeyMsg{Type: tea.KeyCtrlC}
	default:
		return tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune(s)}
	}
}

// step applies a message and returns the concrete Model + cmd.
func step(t *testing.T, m Model, msg tea.Msg) (Model, tea.Cmd) {
	t.Helper()
	next, cmd := m.Update(msg)
	nm, ok := next.(Model)
	if !ok {
		t.Fatalf("Update returned %T, want tui.Model", next)
	}
	return nm, cmd
}

// press applies a named key.
func press(t *testing.T, m Model, key string) (Model, tea.Cmd) {
	t.Helper()
	return step(t, m, keyMsg(key))
}

func TestQuitKeys(t *testing.T) {
	m2, cmd := press(t, NewModel(), "q")
	if !m2.quitting || cmd == nil {
		t.Fatalf("q: quitting=%v cmd=%v, want quitting=true + quit cmd", m2.quitting, cmd)
	}
	m3, cmd := press(t, NewModel(), "ctrl+c")
	if !m3.quitting || cmd == nil {
		t.Fatalf("ctrl+c: quitting=%v cmd=%v, want quitting=true + quit cmd", m3.quitting, cmd)
	}
}

// addFormOnDeepseek walks Add row -> pick Templates[0] (DeepSeek) -> form. The
// resulting form has a models_endpoint prefilled, so the model picker is live.
func addFormOnDeepseek(t *testing.T) Model {
	t.Helper()
	m := NewModel()             // no providers -> cursor 0 == Add row
	m, _ = press(t, m, "enter") // +Add -> grouped picker (cursor 0 = DeepSeek)
	m, _ = press(t, m, "enter") // choose DeepSeek -> add form
	return m
}

// boardModel enters the Agents Board with the given teammates + jobs
// already loaded (screen=screenSpawn, boardEpoch=1, loading=false).
func boardModel(t *testing.T, jobs []dsjobs.Result) Model {
	t.Helper()
	m := NewModel() // the board opens directly (epoch 1, loading)
	// stamp the live boardEpoch so the gate accepts the refresh.
	m, _ = step(t, m, boardMsg{jobs: jobs, epoch: m.boardEpoch})
	return m
}

// TestBoardHideShowNoOpOutsideTeammateRows: h/s are no-ops on an empty board and on the
// jobs rail (no teammate to act on).
func TestBoardHideShowNoOpOutsideTeammateRows(t *testing.T) {
	m := boardModel(t, nil)
	mh, cmd := press(t, m, "h")
	if cmd != nil || mh.screen != screenSpawn {
		t.Fatalf("h on an empty board: cmd=%v screen=%d, want nil + screenSpawn", cmd, mh.screen)
	}
	if _, cmd := press(t, m, "s"); cmd != nil {
		t.Fatal("s on an empty board should be a no-op (nil cmd)")
	}
	m = boardModel(t, []dsjobs.Result{{JobID: "j0000000", Status: "done", LeadSessionID: "s", StartedAt: "2026-05-26T01:00:00Z"}})
	m, _ = press(t, m, "enter") // the job row is the only continuum row
	if m.asMode != asModeEntity {
		t.Fatalf("setup: mode=%d, want entity", m.asMode)
	}
	if _, cmd := press(t, m, "h"); cmd != nil {
		t.Fatal("h on a job row should be a no-op")
	}
	if _, cmd := press(t, m, "s"); cmd != nil {
		t.Fatal("s on a job row should be a no-op")
	}
}

// TestBoardEnterNoOpWhenEmpty: enter with no rows stays on the (empty) boxes level.
func TestBoardEnterNoOpWhenEmpty(t *testing.T) {
	m := boardModel(t, nil)
	m, _ = press(t, m, "enter")
	if m.screen != screenSpawn || m.asMode != asModeBoxes {
		t.Fatalf("enter with no rows: screen=%d mode=%d, want screenSpawn + boxes (no-op)", m.screen, m.asMode)
	}
}

// TestJobElapsedNeverAgesTerminalJobs: a terminal job whose record carries no duration
// renders no elapsed at all — never a live, growing since-StartedAt figure.
func TestJobElapsedNeverAgesTerminalJobs(t *testing.T) {
	if got := jobElapsed(dsjobs.Result{Status: "done", StartedAt: "2026-05-01T00:00:00Z"}); got != "" {
		t.Fatalf("terminal durationless job elapsed = %q, want empty", got)
	}
	if got := jobElapsed(dsjobs.Result{Status: "running", StartedAt: "2026-05-01T00:00:00Z"}); got == "" {
		t.Fatal("a running job should show a live elapsed")
	}
	if got := jobElapsed(dsjobs.Result{Status: "done", DurationMs: 7000}); got != "7s" {
		t.Fatalf("terminal job with a duration = %q, want 7s", got)
	}
}

// TestLoadWfData_SynthesizesBackgroundLiveSnapshot: a detached background job writes no .activity
// sidecar, so loadWfData synthesizes a live snapshot from its poll-time Usage — making its standalone
// row climb. A terminal job gets none (only a running row overrides with the live figure).
func TestLoadWfData_SynthesizesBackgroundLiveSnapshot(t *testing.T) {
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	running := dsjobs.Result{JobID: "bg11111111111111", Status: "running",
		Usage: &dsjobs.Usage{InputTokens: 5000, OutputTokens: 320}}
	_, activity, _ := loadWfData([]dsjobs.Result{running})
	if snap := activity["bg11111111111111"]; !snap.hasUsage || snap.inTok != 5000 || snap.outTok != 320 {
		t.Fatalf("a running background job must get a synthesized live snapshot, got %+v", snap)
	}
	done := dsjobs.Result{JobID: "bg22222222222222", Status: "done",
		Usage: &dsjobs.Usage{InputTokens: 5000, OutputTokens: 320}}
	if _, activity, _ := loadWfData([]dsjobs.Result{done}); len(activity) != 0 {
		t.Fatalf("a terminal job must not get a synthesized live snapshot: %+v", activity)
	}
}

// TestFloorActivity_KeepsTokensMonotonic: the 3s and 500ms board chains can deliver refreshes out of
// order, so a later, lower snapshot for a running job must not lower its already-shown tokens; a higher
// one raises them; a new job passes through; a job absent from the new map is dropped.
func TestFloorActivity_KeepsTokensMonotonic(t *testing.T) {
	prev := map[string]activitySnapshot{"j1": {inTok: 5000, outTok: 800, hasUsage: true}}
	// A stale, lower snapshot lands later — the floor holds the higher tokens.
	got := floorActivity(prev, map[string]activitySnapshot{"j1": {inTok: 4000, outTok: 300, hasUsage: true}})
	if got["j1"].inTok != 5000 || got["j1"].outTok != 800 {
		t.Fatalf("floor must hold the higher tokens, got %+v", got["j1"])
	}
	// A genuinely higher snapshot raises the figure.
	if up := floorActivity(prev, map[string]activitySnapshot{"j1": {inTok: 5000, outTok: 950, hasUsage: true}}); up["j1"].outTok != 950 {
		t.Fatalf("a higher snapshot must raise the figure, got %+v", up["j1"])
	}
	// A new job passes through; a job absent from the new map is not carried.
	fresh := floorActivity(prev, map[string]activitySnapshot{"j2": {outTok: 10, hasUsage: true}})
	if fresh["j2"].outTok != 10 {
		t.Fatalf("a new job must pass through, got %+v", fresh)
	}
	if _, ok := fresh["j1"]; ok {
		t.Fatal("a job absent from the new map must not be carried")
	}
	// A same-attempt out-of-order refresh carrying FEWER tool sigs must not shrink the count: sigs
	// accumulate within an attempt, so the longer list is the newer read.
	prevSigs := map[string]activitySnapshot{"j1": {inTok: 100, outTok: 50, hasUsage: true, attempt: 1, sigs: []string{"A", "B", "C"}}}
	short := floorActivity(prevSigs, map[string]activitySnapshot{"j1": {inTok: 120, outTok: 60, hasUsage: true, attempt: 1, sigs: []string{"A"}}})
	if len(short["j1"].sigs) != 3 {
		t.Fatalf("a same-attempt refresh with fewer sigs must not shrink the tool list, got %d", len(short["j1"].sigs))
	}
	// A restart reuses the job id with a higher Attempt and a fresh low count — the floor must NOT pin it
	// to the prior attempt's total; the new low snapshot wins.
	prevA1 := map[string]activitySnapshot{"j1": {inTok: 5000, outTok: 800, hasUsage: true, attempt: 1}}
	restart := floorActivity(prevA1, map[string]activitySnapshot{"j1": {inTok: 200, outTok: 5, hasUsage: true, attempt: 2}})
	if restart["j1"].inTok != 200 || restart["j1"].outTok != 5 {
		t.Fatalf("a bumped attempt must reset the floor, got %+v", restart["j1"])
	}
	// A stale pre-restart refresh (attempt 1) can land AFTER the restart's attempt-2 snapshot; it must be
	// discarded, never put the board back on attempt 1's high total.
	prevA2 := map[string]activitySnapshot{"j1": {inTok: 200, outTok: 5, hasUsage: true, attempt: 2}}
	stale := floorActivity(prevA2, map[string]activitySnapshot{"j1": {inTok: 5000, outTok: 800, hasUsage: true, attempt: 1}})
	if stale["j1"].inTok != 200 || stale["j1"].outTok != 5 {
		t.Fatalf("a stale lower-attempt snapshot must be discarded, got %+v", stale["j1"])
	}
	// The attempt-2 leaf may briefly emit no activity (the restart gap): loadWfData carries it as a zero
	// attempt-2 tombstone. That refresh must KEEP the attempt-2 floor (not drop the job), so a later stale
	// attempt-1 refresh still loses to it instead of being accepted as a brand-new entry.
	held := floorActivity(prevA2, map[string]activitySnapshot{"j1": {attempt: 2}})
	if held["j1"].inTok != 200 || held["j1"].outTok != 5 || held["j1"].attempt != 2 {
		t.Fatalf("an empty same-attempt tombstone must hold the floor, got %+v", held["j1"])
	}
	if after := floorActivity(held, map[string]activitySnapshot{"j1": {inTok: 5000, outTok: 800, hasUsage: true, attempt: 1}}); after["j1"].inTok != 200 {
		t.Fatalf("a stale attempt-1 refresh after a tombstone must still be discarded, got %+v", after["j1"])
	}
}

// TestBoardJobCardLeafParity: the standalone-job detail card carries the wf agent card's
// sections — collapsed Prompt (⏎ expands), the Activity last-3 feed, the Output body — fed
// by the nonce-gated io load issued when the board lands on the jobs detail view.
func TestBoardJobCardLeafParity(t *testing.T) {
	job := dsjobs.Result{JobID: "j0000000", Status: "done", LeadSessionID: "s",
		StartedAt: "2026-05-26T01:00:00Z", NumTurns: 2}
	m := boardModel(t, []dsjobs.Result{job}) // jobs-only → straight to the detail view
	if m.asMode != asModeEntity || m.asDetailNonce == 0 {
		t.Fatalf("setup: mode=%d nonce=%d, want entity + an issued io load", m.asMode, m.asDetailNonce)
	}
	m, _ = step(t, m, asDetailMsg{
		nonce: m.asDetailNonce, epoch: m.boardEpoch, jobID: "j0000000", present: true,
		prompt: "line one\nline two\nline three\nline four\nline five\nline six",
		answer: "THE-ANSWER-BODY",
		snap:   activitySnapshot{sigs: []string{"A(1)", "B(2)", "C(3)", "D(4)"}},
	})
	// Assert on the full card line set — the box viewport scrolls, so sections can sit
	// below the visible fold.
	card := strings.Join(m.entityDetailLines(m.asEntityRightWidth()), "\n")
	for _, want := range []string{
		"Prompt · 6 lines · ⏎ expand",
		"Activity · last 3 of 4 tool calls",
		"D(4)", // the last three sigs show…
		"Output", "THE-ANSWER-BODY",
	} {
		if !strings.Contains(card, want) {
			t.Errorf("job card missing %q:\n%s", want, card)
		}
	}
	if strings.Contains(card, "A(1)") {
		t.Errorf("Activity should show only the LAST 3 signatures:\n%s", card)
	}
	if strings.Contains(card, "line six") {
		t.Errorf("the folded prompt must hide its tail:\n%s", card)
	}
	m, _ = press(t, m, "enter") // expand the prompt
	card = strings.Join(m.entityDetailLines(m.asEntityRightWidth()), "\n")
	if !strings.Contains(card, "line six") {
		t.Fatalf("⏎ should expand the full prompt:\n%s", card)
	}
}

// TestBoardJobCardStaleNonceDropped: an io read answering a previously-focused job is
// dropped, never shown on the wrong card.
func TestBoardJobCardStaleNonceDropped(t *testing.T) {
	job := dsjobs.Result{JobID: "j0000000", Status: "done", LeadSessionID: "s",
		StartedAt: "2026-05-26T01:00:00Z"}
	m := boardModel(t, []dsjobs.Result{job})
	stale := asDetailMsg{nonce: m.asDetailNonce - 1, epoch: m.boardEpoch, jobID: "j0000000", present: true, answer: "STALE-ANSWER"}
	m, _ = step(t, m, stale)
	if m.asDetailIO || strings.Contains(m.View(), "STALE-ANSWER") {
		t.Fatal("a stale-nonce io read must be dropped")
	}
	prior := asDetailMsg{nonce: m.asDetailNonce, epoch: m.boardEpoch - 1, jobID: "j0000000", present: true, answer: "PRIOR-VISIT"}
	m, _ = step(t, m, prior)
	if m.asDetailIO || strings.Contains(m.View(), "PRIOR-VISIT") {
		t.Fatal("a prior-visit (stale-epoch) io read must be dropped")
	}
}

// TestBoardJobCardReloadsOnTerminalFlip: the refresh that first sees the focused job
// terminal still issues one io load (the final .answer lands), and the NEXT refresh of the
// stable terminal job issues none.
func TestBoardJobCardReloadsOnTerminalFlip(t *testing.T) {
	running := dsjobs.Result{JobID: "j0000000", Status: "running", LeadSessionID: "s",
		StartedAt: "2026-05-26T01:00:00Z"}
	done := running
	done.Status = "done"
	m := boardModel(t, []dsjobs.Result{running})
	mm, cmd := step(t, m, boardMsg{jobs: []dsjobs.Result{done}, epoch: m.boardEpoch})
	if cmd == nil {
		t.Fatal("the running→done refresh must reload the focused job's io")
	}
	// Land the issued read (production delivers it between refreshes), then a stable
	// terminal focused job must stop reloading.
	mm, _ = step(t, mm, asDetailMsg{nonce: mm.asDetailNonce, epoch: mm.boardEpoch, jobID: "j0000000", present: true, answer: "final"})
	if _, cmd := step(t, mm, boardMsg{jobs: []dsjobs.Result{done}, epoch: mm.boardEpoch}); cmd != nil {
		t.Fatal("a stable terminal focused job must not reload io every refresh")
	}
}

// TestBoardJobCardNotPersistedNote: a job without io side files shows the not-persisted
// note instead of empty sections.
func TestBoardJobCardNotPersistedNote(t *testing.T) {
	job := dsjobs.Result{JobID: "j0000000", Status: "done", LeadSessionID: "s",
		StartedAt: "2026-05-26T01:00:00Z"}
	m := boardModel(t, []dsjobs.Result{job})
	m, _ = step(t, m, asDetailMsg{nonce: m.asDetailNonce, epoch: m.boardEpoch, jobID: "j0000000", present: false})
	if out := m.View(); !strings.Contains(out, "not persisted") {
		t.Fatalf("an io-less job should show the not-persisted note:\n%s", out)
	}
}

func TestWindowBounds(t *testing.T) {
	cases := []struct {
		name               string
		cursor, n, max     int
		wantStart, wantEnd int
	}{
		{"short list, no window", 0, 5, 12, 0, 5},
		{"long list, cursor top", 0, 50, 12, 0, 12},
		{"long list, cursor middle", 25, 50, 12, 19, 31},
		{"long list, cursor end", 49, 50, 12, 38, 50},
	}
	for _, c := range cases {
		s, e := windowBounds(c.cursor, c.n, c.max)
		if s != c.wantStart || e != c.wantEnd {
			t.Errorf("%s: windowBounds(%d,%d,%d) = [%d,%d), want [%d,%d)",
				c.name, c.cursor, c.n, c.max, s, e, c.wantStart, c.wantEnd)
		}
	}
}

// ---------------------------------------------------------------------------
// key manager (screenKeys)
// ---------------------------------------------------------------------------

// modalKind reads the open modal's kind without dumping the struct (arg may hold a key).
func modalKind(m Model) string {
	if m.confirm == nil {
		return "<nil>"
	}
	return m.confirm.kind
}

// TestHandlersRegistry_AllScreensRegistered is the enumeration guard: every
// screen constant must have an entry in the package-level `handlers` map.
// Adding a new screen without registering it here would silently produce a TUI
// with an unmapped state.
func TestHandlersRegistry_AllScreensRegistered(t *testing.T) {
	for _, s := range allScreens() {
		h, ok := handlers[s]
		if !ok {
			t.Errorf("screen %d missing from handlers registry", s)
			continue
		}
		if h.update == nil {
			t.Errorf("screen %d: update is nil", s)
		}
		if h.view == nil {
			t.Errorf("screen %d: view is nil", s)
		}
	}
	// And no extras.
	have := map[screen]struct{}{}
	for _, s := range allScreens() {
		have[s] = struct{}{}
	}
	for s := range handlers {
		if _, ok := have[s]; !ok {
			t.Errorf("handlers has screen %d that is not in allScreens()", s)
		}
	}
}

// ---------------------------------------------------------------------------
// Dynamic Workflows (run boxes folded into the Agents Board, screenSpawn)
// ---------------------------------------------------------------------------

// TestGroupByRun_PhaseOrderAndRunOrder: phases render in manifest order first,
// then any phase observed on a job but absent from the manifest appended in
// first-seen order; runs render newest-first by StartedAt.
func TestGroupByRun_PhaseOrderAndRunOrder(t *testing.T) {
	runs := []dsjobs.WorkflowRun{
		{RunID: "run-old", Name: "old", StartedAt: "2026-05-01T00:00:00Z",
			Phases: []dsjobs.RunPhase{{Title: "plan"}, {Title: "build"}}},
		{RunID: "run-new", Name: "new", StartedAt: "2026-05-10T00:00:00Z",
			Phases: []dsjobs.RunPhase{{Title: "design"}}},
	}
	jobs := []dsjobs.Result{
		// run-old: a job in a declared phase + a job in a manifest-absent extra.
		{RunID: "run-old", Phase: "build", Label: "b1", StartedAt: "2026-05-01T01:00:00Z"},
		{RunID: "run-old", Phase: "ship", Label: "s1", StartedAt: "2026-05-01T02:00:00Z"},
		{RunID: "run-new", Phase: "design", Label: "d1", StartedAt: "2026-05-10T01:00:00Z"},
	}
	groups := groupByRun(jobs, runs)
	if len(groups) != 2 {
		t.Fatalf("groups = %d, want 2", len(groups))
	}
	// Newest-first: run-new before run-old.
	if groups[0].runID != "run-new" || groups[1].runID != "run-old" {
		t.Fatalf("run order = [%s, %s], want [run-new, run-old]", groups[0].runID, groups[1].runID)
	}
	// run-old phases: manifest order (plan, build) then observed extra (ship).
	old := groups[1]
	gotPhases := []string{}
	for _, p := range old.phases {
		gotPhases = append(gotPhases, p.title)
	}
	want := []string{"plan", "build", "ship"}
	if strings.Join(gotPhases, ",") != strings.Join(want, ",") {
		t.Fatalf("run-old phase order = %v, want %v", gotPhases, want)
	}
	// "plan" was declared but has no job; "build" has one; "ship" (extra) has one.
	if len(old.phases[0].jobs) != 0 {
		t.Fatalf("declared-but-jobless phase 'plan' should have 0 jobs, got %d", len(old.phases[0].jobs))
	}
	if len(old.phases[1].jobs) != 1 || len(old.phases[2].jobs) != 1 {
		t.Fatalf("phases build/ship should each have 1 job, got %d/%d",
			len(old.phases[1].jobs), len(old.phases[2].jobs))
	}
}

// TestGroupByRun_ManifestOnlyRunYieldsPhaseSkeleton: a run with a manifest but
// zero jobs still produces its declared phase groups (the phase plan), so a
// freshly-created run shows its skeleton.
func TestGroupByRun_ManifestOnlyRunYieldsPhaseSkeleton(t *testing.T) {
	runs := []dsjobs.WorkflowRun{
		{RunID: "run-x", Name: "x", StartedAt: "2026-05-05T00:00:00Z",
			Phases: []dsjobs.RunPhase{{Title: "alpha"}, {Title: "beta"}}},
	}
	groups := groupByRun(nil, runs)
	if len(groups) != 1 {
		t.Fatalf("groups = %d, want 1", len(groups))
	}
	g := groups[0]
	if len(g.phases) != 2 || g.phases[0].title != "alpha" || g.phases[1].title != "beta" {
		t.Fatalf("manifest-only run phases = %+v, want [alpha beta] skeleton", g.phases)
	}
	for _, p := range g.phases {
		if len(p.jobs) != 0 {
			t.Fatalf("manifest-only run phase %q should have 0 jobs, got %d", p.title, len(p.jobs))
		}
	}
}

// TestGroupByRun_NoManifestRunOrdersByEarliestJob: a run whose jobs have no
// manifest (GC'd / never created) gets an empty name, phases in first-seen order,
// and sorts by its earliest job StartedAt.
func TestGroupByRun_NoManifestRunOrdersByEarliestJob(t *testing.T) {
	jobs := []dsjobs.Result{
		{RunID: "orphan", Phase: "second", Label: "j2", StartedAt: "2026-05-02T00:00:00Z"},
		{RunID: "orphan", Phase: "first", Label: "j1", StartedAt: "2026-05-01T00:00:00Z"},
	}
	runs := []dsjobs.WorkflowRun{
		{RunID: "manifested", Name: "m", StartedAt: "2026-05-03T00:00:00Z",
			Phases: []dsjobs.RunPhase{{Title: "p"}}},
	}
	groups := groupByRun(jobs, runs)
	if len(groups) != 2 {
		t.Fatalf("groups = %d, want 2", len(groups))
	}
	// manifested (2026-05-03) is newest → first; orphan (earliest job 2026-05-01) → last.
	if groups[0].runID != "manifested" || groups[1].runID != "orphan" {
		t.Fatalf("run order = [%s, %s], want [manifested, orphan]", groups[0].runID, groups[1].runID)
	}
	orphan := groups[1]
	if orphan.name != "" {
		t.Fatalf("no-manifest run should have empty name, got %q", orphan.name)
	}
	// Phases in first-seen order: "second" then "first".
	if len(orphan.phases) != 2 || orphan.phases[0].title != "second" || orphan.phases[1].title != "first" {
		t.Fatalf("orphan phase order = %+v, want [second first] (first-seen)", orphan.phases)
	}
}

// TestPartition_RunTaggedJobNotOnSpawnBoard: a RunID-tagged job feeds the session's run
// box, never the Subagents box — its session tree carries only RunID == "" jobs as standalone
// subagents. The tagged job's id must not appear among the Subagents rows (it lives under the
// Dynamic Workflows box instead); the plain job's does.
func TestPartition_RunTaggedJobNotOnSpawnBoard(t *testing.T) {
	tagged := dsjobs.Result{JobID: "tagged00", RunID: "run-1", Phase: "build", Label: "b1",
		Provider: "glm", Status: "running", StartedAt: "2026-05-01T00:00:00Z", LeadSessionID: "s"}
	plain := dsjobs.Result{JobID: "plain000", Provider: "kimi", Status: "done",
		StartedAt: "2026-05-01T00:00:00Z", LeadSessionID: "s"}
	runs := []dsjobs.WorkflowRun{{RunID: "run-1", Name: "sweep", SessionID: "s", StartedAt: "2026-05-01T00:00:00Z"}}
	m := boardModel(t, nil)
	m, _ = step(t, m, boardMsg{jobs: []dsjobs.Result{tagged, plain}, runs: runs, epoch: m.boardEpoch})
	out := m.View()
	if strings.Contains(out, "tagged00") {
		t.Errorf("RunID-tagged job leaked into the Subagents box on the Agents Board:\n%s", out)
	}
	if !strings.Contains(out, "plain000") {
		t.Errorf("ungrouped job should appear in the Subagents box:\n%s", out)
	}
	// The standalone (RunID == "") jobs are the only entries in each session's Subagents box;
	// the tagged job must instead live among the session's runs.
	for _, s := range m.asSessions() {
		for _, j := range s.jobs {
			if j.RunID != "" {
				t.Errorf("session tree carried a RunID-tagged job in its Subagents box: %+v", j)
			}
		}
	}
	if got := len(m.workflowJobs); got != 1 {
		t.Errorf("the tagged job should be the only workflow leaf, got %d", got)
	}
}

// TestWfRefresh_StaleEpochDropped: a wfRefreshMsg whose epoch != the model's current
// boardEpoch (a prior visit's in-flight light refresh) is dropped — the fresh visit's
// workflow data survives unchanged.
func TestWfRefresh_StaleEpochDropped(t *testing.T) {
	m := boardModel(t, nil)
	m, _ = step(t, m, boardMsg{
		jobs:  []dsjobs.Result{{RunID: "run-z", Phase: "p", JobID: "ok000000", Label: "agent-a"}},
		runs:  []dsjobs.WorkflowRun{{RunID: "run-z", Name: "z", StartedAt: "2026-05-01T00:00:00Z"}},
		epoch: m.boardEpoch,
	})
	before := len(m.workflowJobs)
	stale := wfRefreshMsg{
		jobs:  nil,
		runs:  nil,
		epoch: m.boardEpoch - 1, // from a prior visit; must be dropped
	}
	m, _ = step(t, m, stale)
	if len(m.workflowJobs) != before {
		t.Fatalf("a stale-epoch wfRefreshMsg must be dropped, jobs went %d → %d", before, len(m.workflowJobs))
	}
	// A matching-epoch refresh DOES land (sanity): it replaces the workflow halves.
	fresh := wfRefreshMsg{
		jobs:  []dsjobs.Result{{RunID: "run-z", Phase: "p", JobID: "ok000000", Label: "agent-b"}},
		runs:  []dsjobs.WorkflowRun{{RunID: "run-z", Name: "z", StartedAt: "2026-05-01T00:00:00Z"}},
		epoch: m.boardEpoch,
	}
	m, _ = step(t, m, fresh)
	if len(m.workflowJobs) != 1 || m.workflowJobs[0].Label != "agent-b" {
		t.Fatalf("matching wfRefreshMsg dropped: jobs=%+v", m.workflowJobs)
	}
}

// TestWfLiveChain_StartsReschedulesStops: a boardMsg whose data has a running RunID-tagged
// leaf starts the 500ms light chain (wfLiveOn true + a non-nil cmd); a current-epoch
// wfLiveTickMsg with something still running reschedules; one with nothing running clears
// wfLiveOn and returns no cmd; a stale-epoch tick is dropped.
func TestWfLiveChain_StartsReschedulesStops(t *testing.T) {
	runningLeaf := []dsjobs.Result{{RunID: "run-z", Phase: "p", Label: "a", JobID: "j1", Status: "running",
		StartedAt: "2026-05-01T00:00:00Z"}}
	runs := []dsjobs.WorkflowRun{{RunID: "run-z", Name: "z", Status: "running", StartedAt: "2026-05-01T00:00:00Z"}}

	m := boardModel(t, nil)
	m, cmd := step(t, m, boardMsg{jobs: runningLeaf, runs: runs, epoch: m.boardEpoch})
	if !m.wfLiveOn || cmd == nil {
		t.Fatalf("a running leaf should start the light chain: wfLiveOn=%v cmd=%v", m.wfLiveOn, cmd)
	}
	// A current-epoch tick while a leaf still runs reschedules.
	if _, c := step(t, m, wfLiveTickMsg{epoch: m.boardEpoch}); c == nil {
		t.Fatal("a current-epoch live tick with a running leaf should reschedule (non-nil cmd)")
	}
	// A stale-epoch tick is dropped (no reschedule).
	if _, c := step(t, m, wfLiveTickMsg{epoch: m.boardEpoch - 1}); c != nil {
		t.Fatal("a stale-epoch live tick must not reschedule")
	}
	// An all-held shape — no running LEAF but the run manifest still live — keeps the
	// chain alive (the run is controllable and its rows still move).
	heldLeaf := []dsjobs.Result{{RunID: "run-z", Phase: "p", Label: "a", JobID: "j1", Status: "held",
		StartedAt: "2026-05-01T00:00:00Z"}}
	mh, _ := step(t, m, wfRefreshMsg{jobs: heldLeaf, runs: runs, epoch: m.boardEpoch})
	mh, ch := step(t, mh, wfLiveTickMsg{epoch: mh.boardEpoch})
	if !mh.wfLiveOn || ch == nil {
		t.Fatal("an all-held run under a running manifest must keep the live chain")
	}
	// Once nothing runs — leaf AND manifest terminal — the tick clears the flag and stops the chain.
	doneLeaf := []dsjobs.Result{{RunID: "run-z", Phase: "p", Label: "a", JobID: "j1", Status: "done",
		StartedAt: "2026-05-01T00:00:00Z", NumTurns: 1}}
	doneRuns := []dsjobs.WorkflowRun{{RunID: "run-z", Name: "z", Status: "done", StartedAt: "2026-05-01T00:00:00Z"}}
	m2, _ := step(t, m, wfRefreshMsg{jobs: doneLeaf, runs: doneRuns, epoch: m.boardEpoch})
	m2, c := step(t, m2, wfLiveTickMsg{epoch: m2.boardEpoch})
	if m2.wfLiveOn || c != nil {
		t.Fatalf("with nothing running the chain should stop: wfLiveOn=%v cmd=%v", m2.wfLiveOn, c)
	}
}

// TestRunLabel_SanitizesRunID: ids.ValidateJobID permits non-whitespace control
// runes in a run id, so the board scrubs it through CleanTitle before rendering —
// a run id carrying an ANSI escape must not reach the terminal raw.
func TestRunLabel_SanitizesRunID(t *testing.T) {
	m := boardModel(t, nil)
	jobs := []dsjobs.Result{{RunID: "\x1b[31mevil", Phase: "p", Label: "a", StartedAt: "2026-05-01T00:00:00Z"}}
	groups := groupByRun(jobs, nil)
	if len(groups) != 1 {
		t.Fatalf("groups = %d, want 1", len(groups))
	}
	if label := m.runLabel(groups[0]); strings.ContainsRune(label, '\x1b') {
		t.Fatalf("runLabel leaked a raw ESC byte from the run id: %q", label)
	}
}

// TestRunCleanupOutcomeNamesKeptWorktrees: a delete / restart outcome carrying KeptWorktreeNotices
// says how many worktrees were kept and where the first one is; without notices the line is unchanged.
func TestRunCleanupOutcomeNamesKeptWorktrees(t *testing.T) {
	jobs, runs := oneRun()
	m := runsModel(t, jobs, runs, nil)
	kept := []string{
		"keeping /s/run-1/wt-a: git commit failed (remove the directory yourself once the work is saved)",
		"keeping /s/run-1/wt-b: git commit failed (remove the directory yourself once the work is saved)",
	}
	m1, _ := step(t, m, workflowCtlMsg{verb: "delete", runID: "run-1", epoch: m.boardEpoch, kept: kept})
	if m1.confirm == nil || !strings.HasSuffix(m1.confirm.result, "kept 2 worktree(s) with unsaved work: /s/run-1/wt-a") {
		t.Fatalf("delete outcome = %+v, want the kept-worktree note", m1.confirm)
	}
	m2, _ := step(t, m, workflowCtlMsg{verb: "delete", runID: "run-1", epoch: m.boardEpoch})
	if m2.confirm == nil || strings.Contains(m2.confirm.result, "kept") {
		t.Fatalf("delete outcome = %+v, want no kept note without notices", m2.confirm)
	}
	if got := keptNotices("run-1", errors.New("boom")); got != nil {
		t.Fatalf("keptNotices after a failed op = %q, want nil", got)
	}
}
