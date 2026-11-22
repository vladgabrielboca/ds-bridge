// Derived from cc-fleet internal/tui/model.go (Apache-2.0); modified.

// Package tui is the ds-bridge jobs board: cc-fleet's Agents Board with the provider hub,
// Agent Teams and setup screens removed. It reads ds jobs and batches through dsjobs.
package tui

import (
	"fmt"
	"sort"
	"strings"
	"sync/atomic"
	"time"
	"unicode"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"

	"ds-bridge/board/internal/dsjobs"
	"ds-bridge/board/internal/pinned"
	"ds-bridge/board/internal/sessiontitle"
)

// screen enumerates the TUI's views; one Model dispatches Update/View on the
// active screen.
type screen int

const (
	screenSpawn screen = iota // the board (ds-bridge has no provider hub)
)

// asMode is the active level of the master-detail Agents Board — all WITHIN
// screenSpawn, so the refresh/tick msg ownership stays on one screen.
// →/enter descend a level; ← ascends but CLAMPS at the board's top level; esc ascends too
// and only leaves for the Model Providers hub at the top — so the entity-level detail card
// always RETURNS on esc, never exits the board. Single-choice levels collapse: one project skips L0, one
// session skips L1 too.
type asMode int

const (
	asModeProjects  asMode = iota // L0: project rail | the cursored project's sessions (>1 project)
	asModeSessions                // L1: session rail | the cursored session's overview (>1 session)
	asModeBoxes                   // L2: the focused session's stacked boxes — Dynamic Workflows + Subagents
	asModeEntity                  // L3: entity list | the focused entity's inline detail card (j/k scroll)
	asModeRunPhases               // run drill: the focused run's Phases | the selected phase's agents
	asModeRunAgent                // run drill: agent list | the selected agent's inline detail (j/k scroll)
	asModeBrowser                 // flat, type-to-filter session browser; Enter opens an entity's existing detail
)

// Model is the root bubbletea model.
type Model struct {
	screen screen
	width  int
	height int

	// Agents Board (screenSpawn): live teammates, workflow runs, and async subagent
	// jobs in one master-detail board. asMode re-roots the levels (projects → sessions →
	// the session's Dynamic Workflows + Subagents boxes → entity detail, and
	// the run drill below the boxes) WITHIN this one screen; focusedProject/focusedSessionID
	// root the deeper levels. boardEpoch
	// tags each auto-refresh tick chain so re-entering the board supersedes a stale
	// chain instead of stacking a second one. sessionMeta carries each session's
	// resolved /rename title + recorded working directory (the project grouping key).
	spawnErr    error
	jobs        []dsjobs.Result
	sessionMeta map[string]sessiontitle.Meta
	// confirm, when non-nil, is the centered confirm modal overlaying the board: every destructive
	// action (delete a run / ended team / job, clear a session's finished) opens one (see confirm.go).
	// pins is the pin-registry snapshot loaded each refresh — the board reads it to mark ★ rows and
	// to exclude pinned records from clear-finished.
	confirm    *confirmModal
	pins       pinned.Set
	boardEpoch int
	// lastRefreshSeq is the highest boardRefreshSeq whose JOB/workflow rows were applied this epoch (from
	// EITHER chain); a refresh that read disk earlier but arrived later (the 3s/500ms chains race) is
	// dropped so it can't resurrect a terminal row. lastBoardFullSeq is the same for the full-refresh-only
	// state (teammates/meta/pins), bumped only by boardMsg — so a boardMsg whose jobs lost to a faster
	// wfRefreshMsg still updates teammates, while a stale boardMsg can't roll that state back over a newer
	// boardMsg. Both reset on board entry so the visit's first refresh always applies.
	lastRefreshSeq   int64
	lastBoardFullSeq int64
	// boardSeen marks that a boardMsg has ever been accepted; a revisit then skips the
	// "discovering…" loading frame and keeps the previous frame until the fresh load
	// lands. boardEntryRoute defers the entry reset (cursors + focus park) to that
	// first accepted refresh, so the stale frame can still render.
	boardSeen        bool
	boardEntryRoute  bool
	asMode           asMode
	focusedProject   string
	focusedSessionID string
	asProjectCursor  int       // L0 rail row
	asSessionCursor  int       // L1 rail row
	asBoxCursor      int       // L2 continuum row: the session's runs, then its jobs
	asEntitySrc      asRailRef // the collection L3 lists (set on descend from L2)
	asEntityCursor   int       // L3 entity row
	asCardScroll     int       // L3 detail-card scroll offset (j/k); reset on entity change
	// Focused-job inline detail (the L3 jobs collection): the job's prompt/answer io +
	// activity snapshot, read off the Update goroutine (the wf board's wfDetail* pattern).
	// asDetailJobID records WHICH job the loaded io belongs to, so a render only shows it
	// when it matches the focused job; asDetailNonce drops a slow read for a previously-
	// focused job; asPromptExpanded mirrors wfPromptExpanded.
	asDetailJobID    string
	asDetailPrompt   string
	asDetailAnswer   string
	asDetailIO       bool
	asDetailSnap     activitySnapshot
	asDetailNonce    int
	asDetailTerminal bool // the focused job was terminal when its io load was issued
	asPromptExpanded bool
	// boardJobsErr is the last refresh's jobs-scan failure, rendered on its OWN dim line under the
	// board — a persistent data-availability warning, not a one-shot outcome (those pop as modals).
	boardJobsErr error
	// wfLiveOn marks the 500ms workflow-only refresh chain as scheduled, so refreshes that
	// keep seeing running leaves don't stack a second chain.
	wfLiveOn bool

	// Run drill (screenSpawn, asModeRunPhases/asModeRunAgent): the master-detail levels below
	// a Dynamic Workflows box row. focusedRunID is the run being drilled; wfPhaseCursor and
	// wfAgentCursor index the focused run's phases and the focused phase's agents. wfActivity
	// holds each leaf's activity snapshot (read off the refresh goroutine, keyed by job id): a
	// running leaf's tokens climb live, and every leaf's tool count persists.
	workflowJobs  []dsjobs.Result
	workflowRuns  []dsjobs.WorkflowRun
	focusedRunID  string
	wfPhaseCursor int
	wfAgentCursor int
	wfActivity    map[string]activitySnapshot
	// wfRestarting is the per-run in-flight guard: a run id is added when a stop/restart/delete is
	// dispatched and removed when its workflowCtlMsg lands, so a second x/r/d on the same run is a
	// no-op until the first completes (and a restart shows a transient "restarting …" status meanwhile).
	wfRestarting map[string]bool

	// Focused-agent inline detail (asModeRunAgent right pane): the focused leaf's prompt/answer
	// read from its io files (PersistIO-gated), rendered scrollable in the right pane.
	// wfDetailJob records WHICH leaf the loaded io belongs to, so a render only shows the
	// prompt/output when it matches the focused leaf. wfDetailIO records whether the io files
	// were present. wfCardScroll is the right-pane scroll offset (lines), preserved across the
	// auto-refresh and reset when the focused leaf changes. wfDetailNonce is bumped on each
	// focused-leaf change so a slow read for a prior leaf is dropped, never shown on the wrong one.
	wfDetailJob    dsjobs.Result
	wfDetailPrompt string
	wfDetailAnswer string
	wfDetailIO     bool
	wfCardScroll   int
	wfDetailNonce  int
	// wfPromptExpanded toggles the inline detail's prompt between a collapsed "N lines · ⏎ expand"
	// summary (default) and the full text; reset to collapsed when the focused leaf changes.
	wfPromptExpanded bool

	// Session browser (asModeBrowser): a flat, newest-first list of past standalone subagent
	// jobs + workflow runs + team sessions, type-to-filter over title/provider/lane. browserFilter
	// is the live query (cursor resets to 0 on every edit); browserCursor indexes the FILTERED
	// rows. browserAnchor is the highlighted row's {kind,id}, re-found after each refresh so an
	// inserted/removed row above the cursor can't shift Enter onto a different entity. browserOrigin
	// marks a detail view entered FROM the browser, so esc returns to it (left still does normal
	// board ascent — preserving the ended-team delete path). browserReturnMode is the board mode
	// ctrl+f was opened from, restored on esc so the browser closes back to the board, not the hub.
	browserFilter     string
	browserCursor     int
	browserAnchor     browserRef
	browserOrigin     bool
	browserReturnMode asMode

	loading  bool
	quitting bool
}

// NewModel returns the initial model: the board, loading its first refresh (ds-bridge has no
// provider hub, so the board's former entry from the hub's tab key happens here).
func NewModel() Model {
	m := Model{screen: screenSpawn, loading: true}
	m.wfRestarting = map[string]bool{}
	// The entry reset (cursor zeroing + the unfocused park) is deferred to the first refresh.
	m.boardEntryRoute = true
	m.boardEpoch = 1
	return m
}

// Init satisfies tea.Model: start the first board load and the refresh tick chain.
func (m Model) Init() tea.Cmd {
	return tea.Batch(loadBoard(m.boardEpoch), boardTick(m.boardEpoch))
}

// ---------------------------------------------------------------------------
// messages + commands
// ---------------------------------------------------------------------------

// boardMsg carries one Agents Board refresh: the ds jobs and batches. It opts into
// screenOwnedAsyncMsg (owningScreen → screenSpawn) AND carries the boardEpoch
// that scheduled it, so a stale refresh from a prior board visit is dropped
// when the user re-enters (epoch++) or leaves the board.
type boardMsg struct {
	jobs        []dsjobs.Result
	jobsErr     error
	runs        []dsjobs.WorkflowRun
	activity    map[string]activitySnapshot
	seq         int64
	sessionMeta map[string]sessiontitle.Meta
	pins        pinned.Set
	epoch       int
	// fullSeq orders the full-refresh-only state (stamped at the start of the read); seq orders the job rows
	// (stamped at ListJobs). Two stamps because boardMsg reads those at different times.
	fullSeq int64
}

func (boardMsg) owningScreen() screen { return screenSpawn }

// boardTickMsg drives the board's auto-refresh. epoch identifies the tick chain
// that scheduled it; a tick whose epoch != Model.boardEpoch is stale (the user
// left and re-entered the board) and is dropped instead of rescheduling.
type boardTickMsg struct{ epoch int }

// boardRefreshInterval is the auto-refresh cadence while the board is open.
const boardRefreshInterval = 3 * time.Second

// workflowsLiveInterval is the tighter cadence the board's workflow refresh ticks at while a leaf is running,
// so its live token/tool counters climb smoothly instead of in coarse 3s steps; it falls back to
// boardRefreshInterval once nothing is running.
const workflowsLiveInterval = 500 * time.Millisecond

// boardRefreshSeq is a process-wide monotonic counter stamped on each board/wf refresh as it finishes
// reading from disk. Within one board epoch the 3s and 500ms chains race and their tea.Cmds can deliver
// out of order; the handlers apply a refresh only when its seq is the newest seen, so a later-delivered
// EARLIER read can't overwrite a fresh one (which would resurrect a just-terminal row, with its stale
// live snapshot). Higher seq == read disk later == fresher.
var boardRefreshSeq atomic.Int64

// loadBoard returns a tea.Cmd that assembles a board refresh tagged with the
// caller's epoch: list the ds jobs and batches. A jobs error degrades to no jobs and
// surfaces on its own line — the board never crashes on a data-source failure. The epoch carries through to boardMsg so
// Update can drop a stale refresh from a prior visit.
func loadBoard(epoch int) tea.Cmd {
	return func() tea.Msg {
		// Stamp the full-state freshness first: the full-refresh-only state (meta/pins) is gated on
		// its own track, so its seq must order by when that read began, not by the later ListJobs stamp.
		fullSeq := boardRefreshSeq.Add(1)
		jobs, jobsErr := dsjobs.ListJobs()
		// Stamp the freshness seq the instant job STATUS is read — before loadWfData's activity/run reads
		// and the meta/pins work below, any of which can stall. The resurrection concern is a
		// done row flashing back to running, so seq must order by when a row's status was observed; a slow
		// older read then can't out-rank a fast newer one in the handler.
		seq := boardRefreshSeq.Add(1)
		runs, activity, wfErr := loadWfData(jobs)
		if jobsErr == nil {
			jobsErr = wfErr
		}
		ids := leadSessionIDs(jobs, runs)
		meta := sessiontitle.ResolveMeta(ids)
		pins, _ := pinned.Snapshot() // best-effort: a read glitch just renders no ★ this tick
		return boardMsg{
			jobs:        jobs,
			jobsErr:     jobsErr,
			runs:        runs,
			activity:    activity,
			sessionMeta: meta,
			pins:        pins,
			epoch:       epoch,
			seq:         seq,
			fullSeq:     fullSeq,
		}
	}
}

// loadWfData assembles the live-token half of a refresh from an already-listed job set: the run
// manifests plus each job's activity snapshot keyed by JobID — a workflow leaf or a sync subagent
// from its <jobID>.activity sidecar, a detached background job synthesized from its poll-time Usage
// (it writes no sidecar). This feeds both the workflow run headers and the standalone subagent rows,
// so every running agent's token count climbs on the 500ms light tick.
func loadWfData(jobs []dsjobs.Result) ([]dsjobs.WorkflowRun, map[string]activitySnapshot, error) {
	activity := map[string]activitySnapshot{}
	for _, j := range jobs {
		if j.JobID == "" {
			continue
		}
		// Only a RUNNING job's sidecar/Usage describes live activity. A queued/held job — notably a leaf
		// requeued for its next attempt, whose old .activity can survive a best-effort remove — gets a bare
		// attempt-stamped tombstone, never a sidecar read: reading a stale sidecar here would seed the
		// current attempt's floor (snap.attempt is stamped from j) while queued, and floorActivity would
		// carry that into the running phase as the same attempt.
		if j.Status == "running" {
			if snap, ok := readLeafActivity(j.JobID); ok {
				snap.attempt = j.Attempt
				activity[j.JobID] = snap
				continue
			}
			// No sidecar (a detached background job): surface its running Usage — filled live by
			// StatusFor's stream-json scan — so its row climbs like a sync leaf's.
			if j.Usage != nil && (j.Usage.InputTokens > 0 || j.Usage.OutputTokens > 0) {
				activity[j.JobID] = activitySnapshot{inTok: j.Usage.InputTokens, outTok: j.Usage.OutputTokens, hasUsage: true, attempt: j.Attempt}
				continue
			}
		}
		// A live job with no usage to show yet — the restart gap, or a queued/held leaf. A zero,
		// hasUsage:false tombstone keeps the job present so floorActivity retains its per-attempt floor
		// across out-of-order refreshes; liveTokens is gated on hasUsage, so a tombstone never changes the
		// rendered tokens — it only preserves attempt order.
		if j.Status == "running" || j.Status == "queued" || j.Status == "held" {
			activity[j.JobID] = activitySnapshot{attempt: j.Attempt}
		}
	}
	runs, err := dsjobs.ListRuns(jobs)
	return runs, activity, err
}

// floorActivity keeps each job's live token snapshot monotonic across refreshes. The 3s full chain and
// the 500ms light chain both scan the same jobs and their tea.Cmds can complete out of order, so a
// later-delivered OLDER snapshot must not lower a job's already-shown input/output — the board would
// tick down for a frame. The floor respects attempt order: a HIGHER attempt (a restart reusing the job
// id with a fresh low count) resets the floor to the new attempt; an EQUAL attempt holds the monotonic
// max; a LOWER attempt is a stale pre-restart refresh that finished late and is discarded for the newer
// attempt already shown. A finished job leaves the live map (rendering from its terminal Result).
// Mutates and returns next.
func floorActivity(prev, next map[string]activitySnapshot) map[string]activitySnapshot {
	for id, ns := range next {
		ps, ok := prev[id]
		if !ok {
			continue
		}
		switch {
		case ns.attempt > ps.attempt:
			// A restart: take the fresh low count as-is so the floor resets to the new attempt.
		case ns.attempt < ps.attempt:
			// A pre-restart refresh landing late: keep the newer attempt, never the stale total.
			next[id] = ps
		default:
			if ps.inTok > ns.inTok {
				ns.inTok = ps.inTok
			}
			if ps.outTok > ns.outTok {
				ns.outTok = ps.outTok
			}
			// Tool sigs accumulate within an attempt, so the longer list is the newer read; keep it rather
			// than a shorter one carried by an out-of-order refresh (seq orders by ListJobs, not the sidecar
			// read), so the tool count + Activity feed never tick backward while in/out are floored.
			if len(ps.sigs) > len(ns.sigs) {
				ns.sigs = ps.sigs
			}
			ns.hasUsage = ns.hasUsage || ps.hasUsage
			next[id] = ns
		}
	}
	return next
}

// wfTagged filters a job list down to the RunID-tagged workflow leaves.
func wfTagged(jobs []dsjobs.Result) []dsjobs.Result {
	var out []dsjobs.Result
	for _, j := range jobs {
		if j.RunID != "" {
			out = append(out, j)
		}
	}
	return out
}

// wfRefreshMsg carries a LIGHT refresh (the full subagent job list + run manifests + activity sidecars —
// never teammate discovery or pane capture), driven by the 500ms live chain so a running run's counters
// AND a standalone background subagent's row climb smoothly between the 3s full refreshes. jobs is the
// full ListJobs result (mirror boardMsg) so a standalone row's status/usage refresh at 500ms too, not
// only on the 3s tick. Owned by screenSpawn, boardEpoch-gated like boardMsg.
type wfRefreshMsg struct {
	jobs     []dsjobs.Result
	jobsErr  error
	runs     []dsjobs.WorkflowRun
	activity map[string]activitySnapshot
	epoch    int
	seq      int64
}

func (wfRefreshMsg) owningScreen() screen { return screenSpawn }

// wfLiveTickMsg drives the light chain; a stale epoch stops it (mirror boardTickMsg).
type wfLiveTickMsg struct{ epoch int }

// loadWfLight reads only the workflow data (no teammate discovery, no pane capture).
func loadWfLight(epoch int) tea.Cmd {
	return func() tea.Msg {
		all, jobsErr := dsjobs.ListJobs()
		seq := boardRefreshSeq.Add(1) // stamp at the job-status read, before loadWfData (see loadBoard)
		runs, activity, wfErr := loadWfData(all)
		if jobsErr == nil {
			jobsErr = wfErr
		}
		return wfRefreshMsg{jobs: all, jobsErr: jobsErr, runs: runs, activity: activity, epoch: epoch, seq: seq}
	}
}

// wfLiveTick schedules the next light refresh.
func wfLiveTick(epoch int) tea.Cmd {
	return tea.Tick(workflowsLiveInterval, func(time.Time) tea.Msg {
		return wfLiveTickMsg{epoch: epoch}
	})
}

// startWfLive starts the 500ms light chain when a refresh sees a running leaf and no chain
// is already live; the chain stops itself (clearing the flag) once nothing runs.
func (m *Model) startWfLive() tea.Cmd {
	if m.wfLiveOn || !m.anyAgentRunning() {
		return nil
	}
	m.wfLiveOn = true
	return wfLiveTick(m.boardEpoch)
}

func leadSessionIDs(jobs []dsjobs.Result, runs []dsjobs.WorkflowRun) []string {
	ids := make([]string, 0, len(jobs)+len(runs))
	for _, j := range jobs {
		if j.LeadSessionID != "" {
			ids = append(ids, j.LeadSessionID)
		}
	}
	for _, r := range runs {
		if r.SessionID != "" {
			ids = append(ids, r.SessionID)
		}
	}
	return ids
}

// asSession is one Claude session's slice of the Agents Board: its batches (runs) and its
// standalone (RunID == "") ds jobs — the single tree every level indexes. earliest (the first job
// StartedAt) is the "created" display; latest is the newest-first sort key.
type asSession struct {
	sessionID string
	runs      []runGroup
	jobs      []dsjobs.Result
	earliest  time.Time // first activity — the "created" display
	latest    time.Time // most recent activity — the newest-first sort key
	hasTime   bool
}

// asRailRef identifies an entity collection by TYPE: the session's ds jobs (the only
// collection left once Agent Teams are gone).
type asRailRef struct {
	jobs bool
}

// asProject is one project directory's slice of the board: the sessions whose transcripts
// record that working directory ("" = unresolvable, the "(no project)" bucket).
type asProject struct {
	dir      string
	sessions []asSession
}

// sessionProjectDir is a session's project bucket key: the lead session's recorded cwd,
// else (for a runs-only session) the run manifest's launch cwd, else the first job's project.
func sessionProjectDir(s asSession, meta map[string]sessiontitle.Meta) string {
	if dir := meta[s.sessionID].Cwd; dir != "" {
		return dir
	}
	if len(s.runs) > 0 {
		return s.runs[0].cwd
	}
	if len(s.jobs) > 0 {
		return s.jobs[0].Project
	}
	return ""
}

// groupProjects buckets the (already newest-first) sessions by their recorded working
// directory. First-seen project order therefore follows each project's most recently
// active session; the unknown bucket sorts last.
func groupProjects(sessions []asSession, meta map[string]sessiontitle.Meta) []asProject {
	order := []string{}
	byDir := map[string]*asProject{}
	for _, s := range sessions {
		dir := sessionProjectDir(s, meta)
		p, ok := byDir[dir]
		if !ok {
			p = &asProject{dir: dir}
			byDir[dir] = p
			order = append(order, dir)
		}
		p.sessions = append(p.sessions, s)
	}
	out := make([]asProject, 0, len(order))
	for _, dir := range order {
		out = append(out, *byDir[dir])
	}
	sort.SliceStable(out, func(i, j int) bool {
		if (out[i].dir == "") != (out[j].dir == "") {
			return out[j].dir == ""
		}
		return false
	})
	return out
}

// asBoxRef identifies an L2 continuum row by typed identity — a run by id, a job by id —
// so the cursor can re-find its row after a refresh shifts the indices.
type asBoxRef struct {
	jobID string
	runID string
}

// groupSessions builds the board's session tree: RunID-tagged jobs feed the runs (each
// session's workflow runs, via groupByRun) instead of the Subagents box. Sessions sort
// NEWEST-FIRST by latest activity — defined for any mix of jobs/runs; a session with
// no parseable timestamp sorts after timed ones, and "" (no session) always last.
func groupSessions(jobs []dsjobs.Result, runs []runGroup) []asSession {
	order := []string{}
	byID := map[string]*asSession{}
	ensure := func(id string) *asSession {
		if s, ok := byID[id]; ok {
			return s
		}
		s := &asSession{sessionID: id}
		byID[id] = s
		order = append(order, id)
		return s
	}
	noteTime := func(s *asSession, t time.Time) {
		if !s.hasTime || t.Before(s.earliest) {
			s.earliest = t
		}
		if !s.hasTime || t.After(s.latest) {
			s.latest = t
		}
		s.hasTime = true
	}
	for _, j := range jobs {
		if j.RunID != "" {
			continue
		}
		s := ensure(j.LeadSessionID)
		s.jobs = append(s.jobs, j)
		if ts, err := time.Parse(time.RFC3339, j.StartedAt); err == nil {
			noteTime(s, ts)
		}
	}
	for _, g := range runs {
		s := ensure(g.sessionID)
		s.runs = append(s.runs, g)
		if ts, err := time.Parse(time.RFC3339, g.startedAt); err == nil {
			noteTime(s, ts)
		}
	}
	out := make([]asSession, 0, len(order))
	for _, id := range order {
		out = append(out, *byID[id])
	}
	sort.SliceStable(out, func(i, j int) bool {
		a, b := out[i], out[j]
		if (a.sessionID == "") != (b.sessionID == "") {
			return b.sessionID == "" // "(no session)" always last
		}
		if a.hasTime != b.hasTime {
			return a.hasTime
		}
		if a.hasTime && !a.latest.Equal(b.latest) {
			return a.latest.After(b.latest)
		}
		return false // stable: encounter order as the tiebreaker
	})
	return out
}

// sessionRank orders a session by launcher for the board's codex grouping: real
// non-codex first (0), codex-launched (1), then "(no session)" (2). The shared key
// behind sinkCodexSessions and the browser's row grouping.
func sessionRank(id string) int {
	switch {
	case id == "":
		return 2
	case strings.HasPrefix(id, "codex:"):
		return 1
	default:
		return 0
	}
}

// sinkCodexSessions groups codex-launched sessions below the non-codex ones (and above
// "(no session)", which stays last), keeping the newest-first order within each group. A
// list with no codex session is returned unchanged, so a codex-free board renders identically.
func sinkCodexSessions(sessions []asSession) []asSession {
	out := make([]asSession, len(sessions))
	copy(out, sessions)
	if firstCodexSession(out) < 0 {
		return out // no codex launcher → leave the order exactly as given
	}
	sort.SliceStable(out, func(i, j int) bool {
		return sessionRank(out[i].sessionID) < sessionRank(out[j].sessionID)
	})
	return out
}

// firstCodexSession returns the index of the first codex-launched session in a slice, or -1
// when there are none. In a codex-sunk slice that index is the divider boundary; sinkCodexSessions
// also calls it on an unsorted slice just to detect whether any codex session is present.
func firstCodexSession(sessions []asSession) int {
	for i, s := range sessions {
		if sessionRank(s.sessionID) == 1 {
			return i
		}
	}
	return -1
}

// firstNoSessionIndex returns the index of the launcher-unknown "(no session)" row in a
// codex-sunk slice (it sorts last), or -1 when there is none.
func firstNoSessionIndex(sessions []asSession) int {
	for i, s := range sessions {
		if s.sessionID == "" {
			return i
		}
	}
	return -1
}

// boardTick schedules the next auto-refresh tick for the given epoch.
func boardTick(epoch int) tea.Cmd {
	return tea.Tick(boardRefreshInterval, func(time.Time) tea.Msg {
		return boardTickMsg{epoch: epoch}
	})
}

// stopRunCmd reaps + stops the focused run and reports the outcome via its workflowCtlMsg. It is the
// board's only run-state mutation besides restart. The epoch stamps the originating board visit so a
// result landing after the user left + re-entered (epoch++) is dropped (mirror boardMsg's gate).
func stopRunCmd(runID string, epoch int) tea.Cmd {
	return func() tea.Msg {
		err := dsjobs.WithRunLock(runID, func() error {
			_, e := dsjobs.StopRun(runID)
			return e
		})
		return workflowCtlMsg{verb: "stop", runID: runID, err: err, epoch: epoch}
	}
}

// leafCtlCmd sends a LIVE leaf directive (stop-leaf / restart-leaf) over the run's
// control plane and reports via workflowCtlMsg — the engine's poller applies it and the
// effect surfaces through the board poll (held ▶ / a fresh attempt).
func leafCtlCmd(verb, runID, leafID string, epoch int) tea.Cmd {
	op := "stop"
	if verb == "restart-leaf" {
		op = "restart"
	}
	return func() tea.Msg {
		err := dsjobs.SendLeafCommand(runID, op, leafID)
		return workflowCtlMsg{verb: verb, runID: runID, err: err, epoch: epoch}
	}
}

// phaseCtlCmd sends a LIVE phase directive over the run's control plane.
func phaseCtlCmd(verb, runID, phase string, epoch int) tea.Cmd {
	op := "stop"
	if verb == "restart-phase" {
		op = "restart"
	}
	return func() tea.Msg {
		err := dsjobs.SendPhaseCommand(runID, op, phase)
		return workflowCtlMsg{verb: verb, runID: runID, err: err, epoch: epoch}
	}
}

// deleteRunCmd removes a run + all its jobs from the board (the board never auto-clears, so runs
// accumulate until deleted). Mirrors stopRunCmd's epoch-gated workflowCtlMsg outcome.
func deleteRunCmd(runID string, epoch int) tea.Cmd {
	return func() tea.Msg {
		var line string
		err := dsjobs.WithRunLock(runID, func() error {
			var e error
			line, e = dsjobs.PurgeRun(runID)
			return e
		})
		return workflowCtlMsg{verb: "delete", runID: runID, err: err, line: line, epoch: epoch, kept: keptNotices(runID, err)}
	}
}

// keptNotices reads, after a successful delete / restart, which of runID's isolation worktrees were
// left in place because they hold unsaved work. Called off the Update goroutine.
func keptNotices(runID string, err error) []string {
	if err != nil {
		return nil
	}
	return dsjobs.KeptWorktreeNotices(runID)
}

// keptWorktreeNote is the suffix a cleanup's status message carries when worktrees were kept: their
// count and the first one's path. Empty when nothing was kept.
func keptWorktreeNote(notices []string) string {
	if len(notices) == 0 {
		return ""
	}
	// Each notice reads "keeping <wt>: <reason> (...)".
	first, _, _ := strings.Cut(strings.TrimPrefix(notices[0], "keeping "), ": ")
	return fmt.Sprintf(" · kept %d worktree(s) with unsaved work: %s", len(notices), first)
}

// workflowCtlMsg carries the outcome of a run control (stop/restart from the drill, delete/save from
// the run row). Its handler resolves the running modal (stop/restart) or pops an info modal
// (delete/save), then reloads. Owned by screenSpawn; epoch gates a stale prior-visit result.
type workflowCtlMsg struct {
	verb  string // "stop" | "restart" | "delete" | "save"
	runID string
	err   error
	epoch int
	kept  []string // KeptWorktreeNotices after a successful delete / restart
	line  string   // ds forget's line for a delete
}

func (workflowCtlMsg) owningScreen() screen { return screenSpawn }

// ctlOutcome formats a run-control result line and whether it failed — shared by the modal-resolve
// path (a stop confirmed in the modal) and the info-popup path (a delete outcome).
func ctlOutcome(verb, runID string, err error) (string, bool) {
	if err != nil {
		return fmt.Sprintf("%s %s failed: %s", verb, runID, sessiontitle.CleanTitle(err.Error())), true
	}
	switch verb {
	case "stop-leaf":
		return "job stop sent", false
	case "stop-phase":
		return "group stop sent", false
	case "stop", "stop-job":
		return fmt.Sprintf("stop sent to %s: it shows failed · interrupted once ds has stopped its OpenCode session", runID), false
	}
	return fmt.Sprintf("%s %s: ok", verb, runID), false
}

// runningVerb is the workflowCtlMsg verb a running modal of the given kind awaits, so a shared-channel
// outcome for a different op on the same run (e.g. a save) can't resolve it.
func runningVerb(kind string) string {
	switch kind {
	case confirmStop:
		return "stop"
	case confirmStopLeaf:
		return "stop-leaf"
	case confirmStopPhase:
		return "stop-phase"
	case confirmStopJob:
		return "stop-job"
	}
	return ""
}

// wfDetailMsg carries the focused leaf's io read for the inline agent-detail pane: the prompt +
// answer (already read off the Update goroutine) and whether either io file was present. Owned by
// screenSpawn (the agent detail is inline); nonce is the focused-leaf request it answers, so a
// slow read for a previously-focused leaf is dropped rather than shown on the wrong agent.
type wfDetailMsg struct {
	nonce   int
	epoch   int
	job     dsjobs.Result
	prompt  string
	answer  string
	present bool
}

func (wfDetailMsg) owningScreen() screen { return screenSpawn }

// loadLeafIOCmd reads the selected leaf's prompt/answer side files
// (<ConfigDir>/subagent-jobs/<jobID>.prompt / .answer; 0600, present only when
// persist-io was on). A read failure (absent files) degrades to empty + present
// false so the inline detail shows the not-persisted note. The answer text reaches ONLY the
// focused agent's inline detail pane — never the board's agent rows. nonce tags the request so a
// stale read can't populate a later leaf's detail.
func loadLeafIOCmd(job dsjobs.Result, nonce, epoch int) tea.Cmd {
	return func() tea.Msg {
		prompt, answer, present := readLeafIO(job.JobID)
		return wfDetailMsg{nonce: nonce, epoch: epoch, job: job, prompt: prompt, answer: answer, present: present}
	}
}

// anyLeafRunning reports whether any workflow activity is live (drives the live tick
// cadence). A run whose every leaf is held has no running LEAF but is still live —
// its manifest says running — so the run status is consulted too.
func (m Model) anyLeafRunning() bool {
	for _, j := range m.workflowJobs {
		if j.Status == "running" {
			return true
		}
	}
	for _, r := range m.workflowRuns {
		if r.Status == "running" {
			return true
		}
	}
	return false
}

// anyJobRunning reports whether any STANDALONE subagent job is live — so the 500ms light chain also
// ticks for a background subagent's climbing token count, not just for workflow runs. Workflow leaves
// (RunID set) are covered by anyLeafRunning via m.workflowJobs; this scans only RunID=="" rows so a
// finished leaf can't keep the chain alive through a standalone scan.
func (m Model) anyJobRunning() bool {
	for _, j := range m.jobs {
		if j.RunID == "" && j.Status == "running" {
			return true
		}
	}
	return false
}

// anyAgentRunning is the live-tick predicate: a workflow leaf/run OR a standalone job is running.
// Both the start gate and the keep-alive gate use it, so the chain can't start then die after a tick.
func (m Model) anyAgentRunning() bool { return m.anyLeafRunning() || m.anyJobRunning() }

// asDetailMsg carries the focused standalone job's io + activity read for the entity detail
// card (mirror wfDetailMsg). Owned by screenSpawn; nonce gates out a stale read so a slow
// job-A read landing after the user moved to job-B is dropped.
type asDetailMsg struct {
	nonce   int
	epoch   int
	jobID   string
	prompt  string
	answer  string
	present bool
	snap    activitySnapshot
}

func (asDetailMsg) owningScreen() screen { return screenSpawn }

// loadJobIOCmd reads the job's prompt/answer side files + activity sidecar off the Update
// goroutine (mirror loadLeafIOCmd). The answer text reaches ONLY the focused job's inline
// detail card — never a board row.
func loadJobIOCmd(jobID string, nonce, epoch int) tea.Cmd {
	return func() tea.Msg {
		prompt, answer, present := readLeafIO(jobID)
		snap, _ := readLeafActivity(jobID)
		return asDetailMsg{nonce: nonce, epoch: epoch, jobID: jobID, prompt: prompt, answer: answer, present: present, snap: snap}
	}
}

// ---------------------------------------------------------------------------
// update
// ---------------------------------------------------------------------------

// Update is the single tea.Model entry point. Async results (providersMsg etc.)
// are handled regardless of screen unless they implement screenOwnedAsyncMsg —
// in that case Update drops the message when the user has navigated off the
// owning screen, so e.g. a slow models-fetch result can't reach the provider list
// after the user esc'd back. Key handling dispatches on the active screen.
// ctrl+c always quits.
func (m Model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	if owned, ok := msg.(screenOwnedAsyncMsg); ok {
		if owned.owningScreen() != m.screen {
			return m, nil
		}
	}
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width = msg.Width
		m.height = msg.Height
		return m, nil

	case boardMsg:
		// Even when the owner-check accepts the message (screen == screenSpawn),
		// a stale refresh from a PRIOR board visit must still be dropped: the
		// epoch is bumped on each board entry, so a discover scheduled before
		// re-entry has msg.epoch < m.boardEpoch and must NOT clobber the fresh
		// visit's loading=true / jobs list.
		if msg.epoch != m.boardEpoch {
			return m, nil
		}
		// A board entry routes from THIS fresh load: zero the cursors and park the
		// focus now (deferred from the tab handler so the stale frame could render).
		if m.boardEntryRoute {
			m.boardEntryRoute = false
			m.asProjectCursor, m.asSessionCursor, m.asBoxCursor, m.asEntityCursor, m.asCardScroll = 0, 0, 0, 0, 0
			m.focusedProject, m.focusedSessionID, m.asMode = asNoFocus, asNoFocus, asModeBoxes
			m.browserOrigin = false // a fresh board entry has no browser back-link
		}
		// Capture the L2 cursor row's TYPED identity before the data changes, so the
		// cursor can re-find it (a run or a job by id) after the refresh.
		prevBox, hadBox := m.boxRowRef()
		prevEntity, hadEntity := m.entityRowRef()
		m.loading = false
		m.boardSeen = true
		// Session-meta / pin state is full-refresh-only (the 500ms light chain never carries it),
		// gated on fullSeq, on a boardMsg-only track. So a boardMsg whose JOB rows lost the shared-seq
		// race to a faster wfRefreshMsg still updates it (the light chain never bumps lastBoardFullSeq),
		// but a stale boardMsg can't roll it back over a newer boardMsg.
		if msg.fullSeq >= m.lastBoardFullSeq {
			m.lastBoardFullSeq = msg.fullSeq
			m.sessionMeta = msg.sessionMeta
			m.pins = msg.pins
		}
		// The job/workflow rows are seq-gated against BOTH chains: a refresh that read disk earlier but
		// arrived after a newer one (the 3s/500ms chains race within an epoch) would otherwise resurrect a
		// just-terminal job as running, with its stale live snapshot.
		if msg.seq >= m.lastRefreshSeq {
			m.lastRefreshSeq = msg.seq
			m.jobs = msg.jobs
			m.boardJobsErr = msg.jobsErr
			m.workflowJobs = wfTagged(msg.jobs)
			m.workflowRuns = msg.runs
			m.wfActivity = floorActivity(m.wfActivity, msg.activity)
		}
		// Preserve the drill state: re-route if the focus chain broke, re-find the L2 row
		// by identity, index-clamp the entity cursor, re-clamp the card scroll.
		m.rerootSpawn()
		if hadBox {
			m.reanchorBox(prevBox)
		}
		m.clampAsCursors()
		if hadEntity && !m.reanchorEntity(prevEntity) {
			m.browserOrigin = false // the browser-entered entity is gone; the cursor fell to a sibling
		}
		m.asCardScroll = m.clampAsCardScroll(m.asCardScroll)
		// Load (or live-refresh) the focused entity's detail payload — a reroot can land
		// the board straight on a detail view. Jobs: a non-terminal focused job re-reads
		// each refresh so its Activity feed climbs, and the load that FIRST sees the job
		// terminal still runs once, so the final .answer lands after the running→done
		// flip.
		var detail tea.Cmd
		if m.asMode == asModeEntity {
			if m.asEntitySrc.jobs {
				if j, ok := m.selectedJob(); ok && m.jobCardStale(j) {
					m.asDetailNonce++
					m.asDetailTerminal = isTerminalLeaf(j.Status)
					detail = loadJobIOCmd(j.JobID, m.asDetailNonce, m.boardEpoch)
				}
			}
		} else if m.asMode == asModeBoxes {
			// The boxes level always previews a job card (the cursored job, else the
			// first) — keep it fresh on the same terms as the entity-level card.
			if j, ok := m.boxPreviewJob(); ok && m.jobCardStale(j) {
				m.asDetailNonce++
				m.asDetailTerminal = isTerminalLeaf(j.Status)
				detail = loadJobIOCmd(j.JobID, m.asDetailNonce, m.boardEpoch)
			}
		}
		return m, tea.Batch(detail, m.startWfLive())

	case boardTickMsg:
		// Only the current tick chain, and only while the board is open, keeps
		// refreshing — a stale or off-board tick stops the chain.
		if m.screen == screenSpawn && msg.epoch == m.boardEpoch {
			return m, tea.Batch(loadBoard(msg.epoch), boardTick(msg.epoch))
		}
		return m, nil

	case wfRefreshMsg:
		// The light chain's payload: only the workflow halves move; the cursor re-anchors
		// exactly like a full refresh (a finishing run reshapes the continuum).
		if msg.epoch != m.boardEpoch {
			return m, nil
		}
		// Same out-of-order drop as boardMsg: an earlier-read refresh arriving late must not roll the
		// workflow rows + live snapshots back over a fresher one.
		if msg.seq < m.lastRefreshSeq {
			return m, nil
		}
		m.lastRefreshSeq = msg.seq
		prevBox, hadBox := m.boxRowRef()
		prevEntity, hadEntity := m.entityRowRef()
		// The light chain carries the full job list too, so a standalone subagent row's status/usage
		// refresh at 500ms — otherwise it lags 3s behind its own live snapshot and falls back to a stale,
		// lower usage (a visible down-jump) and a stuck "running" when the job finishes between full ticks.
		m.jobs = msg.jobs
		m.boardJobsErr = msg.jobsErr
		m.workflowJobs = wfTagged(msg.jobs)
		m.workflowRuns = msg.runs
		m.wfActivity = floorActivity(m.wfActivity, msg.activity)
		m.rerootSpawn()
		if hadBox {
			m.reanchorBox(prevBox)
		}
		m.clampAsCursors()
		if hadEntity && !m.reanchorEntity(prevEntity) {
			m.browserOrigin = false // the browser-entered entity is gone; the cursor fell to a sibling
		}
		m.asCardScroll = m.clampAsCardScroll(m.asCardScroll)
		return m, m.startWfLive()

	case wfLiveTickMsg:
		if m.screen == screenSpawn && msg.epoch == m.boardEpoch {
			if m.anyAgentRunning() {
				return m, tea.Batch(loadWfLight(msg.epoch), wfLiveTick(msg.epoch))
			}
			m.wfLiveOn = false // nothing runs — the chain ends; a later refresh restarts it
		}
		return m, nil

	case workflowCtlMsg:
		// A stale result from a prior board visit must not mutate a fresh one.
		if msg.epoch != m.boardEpoch {
			return m, nil
		}
		if msg.verb != "save" {
			delete(m.wfRestarting, msg.runID) // stop/restart/delete completed — clear the in-flight guard (save never set it)
		}
		// The run id + error are opaque/operator-supplied text, so scrub them before display.
		runID := shortRunID(sessiontitle.CleanTitle(msg.runID))
		line, isErr := ctlOutcome(msg.verb, runID, msg.err)
		if msg.err == nil && msg.line != "" {
			line = sessiontitle.CleanTitle(msg.line)
		}
		line += keptWorktreeNote(msg.kept)
		// A stop/restart confirmed via the modal waits in modalRunning for exactly this id AND verb —
		// resolve it in place. Otherwise (a delete/save dispatched on close, or a same-id result for a
		// different verb) pop a fresh info modal; withInfo no-ops if a modal is up so it can't clobber.
		if m.confirm != nil && m.confirm.phase == modalRunning && m.confirm.id == msg.runID && msg.verb == runningVerb(m.confirm.kind) {
			c := *m.confirm
			c.phase, c.result, c.resultErr = modalResult, line, isErr
			m.confirm = &c
		} else {
			m = m.withInfo(line, isErr)
		}
		return m, loadWfLight(m.boardEpoch)

	case jobCtlMsg:
		if msg.epoch != m.boardEpoch {
			return m, nil
		}
		if msg.err != nil {
			m = m.withInfo(sessiontitle.CleanTitle(msg.err.Error()), true)
		} else {
			m = m.withInfo(sessiontitle.CleanTitle(msg.line), false)
		}
		return m, loadBoard(m.boardEpoch)

	case asDetailMsg:
		// Drop a read that answers a prior focused job or a prior board visit (the
		// wfDetailMsg nonce gate + the board's epoch gate).
		if msg.nonce != m.asDetailNonce || msg.epoch != m.boardEpoch {
			return m, nil
		}
		m.asDetailJobID = msg.jobID
		m.asDetailPrompt = msg.prompt
		m.asDetailAnswer = msg.answer
		m.asDetailIO = msg.present
		m.asDetailSnap = msg.snap
		return m, nil

	case wfDetailMsg:
		// Drop a read that answers a prior focused leaf or a prior board visit (nonce +
		// epoch gates) so the inline detail never shows the wrong leaf's answer.
		if msg.nonce != m.wfDetailNonce || msg.epoch != m.boardEpoch {
			return m, nil
		}
		m.wfDetailJob = msg.job
		m.wfDetailPrompt = msg.prompt
		m.wfDetailAnswer = msg.answer
		m.wfDetailIO = msg.present
		return m, nil

	case tea.KeyMsg:
		if msg.Type == tea.KeyCtrlC {
			m.quitting = true
			return m, tea.Quit
		}
		return m.updateSpawn(msg)
	}
	return m, nil
}

// updateSpawn drives the master-detail Agents Board, branching on asMode (projects,
// sessions, the session's boxes, an entity's inline detail, or a run drill). r, R and ctrl+r
// reload (ds-bridge has no restart); tab and q quit (there is no provider hub). The per-mode
// handlers own ↑/↓, →/⏎ (descend) and ←/esc (ascend — esc additionally quits at the board's top
// level). The auto-refresh tick chain runs independently (see boardTickMsg).
func (m Model) updateSpawn(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	if m.confirm != nil {
		return m.updateConfirm(msg) // a confirm modal traps focus: ←/→ select, enter runs the choice (no esc)
	}
	// The flat browser is a text-entry mode: every printable rune (incl. r/q) is filter
	// input, so it routes BEFORE the board-global shortcuts below. Only ctrl+c (handled in
	// Update) stays global.
	if m.asMode == asModeBrowser {
		return m.updateBrowser(msg)
	}
	switch msg.String() {
	case "ctrl+f":
		return m.openBrowser()
	case "r", "R", "ctrl+r":
		m.loading = true
		return m, loadBoard(m.boardEpoch)
	case "tab", "q":
		m.quitting = true
		return m, tea.Quit
	}
	switch m.asMode {
	case asModeProjects:
		return m.updateAsProjects(msg)
	case asModeSessions:
		return m.updateAsSessions(msg)
	case asModeEntity:
		return m.updateAsEntity(msg)
	case asModeRunPhases:
		return m.updateWfPhases(msg)
	case asModeRunAgent:
		return m.updateWfAgent(msg)
	default:
		return m.updateAsBoxes(msg)
	}
}

// updateAsProjects (L0, >1 project): ↑/↓ choose a project (the right pane previews its
// sessions), →/⏎ descend; esc → Providers (← clamps at this top level).
func (m Model) updateAsProjects(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	projects := m.asProjects()
	switch msg.String() {
	case "up":
		m.asProjectCursor = clampIndex(m.asProjectCursor-1, len(projects))
	case "down":
		m.asProjectCursor = clampIndex(m.asProjectCursor+1, len(projects))
	case "right", "enter":
		return m.asDescend()
	case "esc":
		return m.asAscend(true)
	case "left":
		return m.asAscend(false)
	}
	return m, nil
}

// updateAsSessions (L1, >1 session in the focused project): ↑/↓ choose a session (the right
// pane shows its overview), →/⏎ descend into its boxes; ←/esc ascend.
func (m Model) updateAsSessions(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	p, ok := m.focusedProjectGroup()
	switch msg.String() {
	case "up":
		if ok {
			m.asSessionCursor = clampIndex(m.asSessionCursor-1, len(p.sessions))
		}
	case "down":
		if ok {
			m.asSessionCursor = clampIndex(m.asSessionCursor+1, len(p.sessions))
		}
	case "right", "enter":
		return m.asDescend()
	case "c":
		// Clear the cursored session's closed, unpinned jobs and batches. The "(no session)"
		// bucket has no id to target.
		if ok && m.asSessionCursor >= 0 && m.asSessionCursor < len(p.sessions) {
			if id := p.sessions[m.asSessionCursor].sessionID; id != "" {
				return m.openClear(id)
			}
		}
	case "esc":
		return m.asAscend(true)
	case "left":
		return m.asAscend(false)
	}
	return m, nil
}

// updateAsBoxes (L2): a single ↑/↓ cursor walks the continuum — the session's run rows
// (the Dynamic Workflows box rail), then its job rows (the Subagents box). A job row shows its card INLINE in the Subagents box, so it
// gets the card keys right here (j/k scroll, ⏎ fold) and never needs a descend; →/⏎ descend a
// RUN row into its phases; d opens a delete confirm for a run / job row. ←/esc ascend.
func (m Model) updateAsBoxes(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	s, ok := m.focusedSession()
	n := 0
	if ok {
		n = len(s.runs) + len(s.jobs)
	}
	switch msg.String() {
	case "up":
		m.asBoxCursor = clampIndex(m.asBoxCursor-1, n)
		return m.focusBoxJobIO()
	case "down":
		m.asBoxCursor = clampIndex(m.asBoxCursor+1, n)
		return m.focusBoxJobIO()
	case "d":
		// A run row confirms a batch delete; a job row its delete. Each opens a modal.
		if g, onRun := m.boxRun(); onRun {
			return m.openConfirm(confirmRun, g.runID, "Delete batch "+g.runID+" and its jobs? ds forget removes them for good.")
		}
		if j, onJob := m.boxJob(); onJob {
			return m.openDeleteJob(j)
		}
	case "x":
		if g, onRun := m.boxRun(); onRun {
			return m.openStopRun(g.runID, g.status)
		}
		if j, onJob := m.boxJob(); onJob {
			return m.openStopJob(j)
		}
	case "p":
		// Toggle the keep/pin on the cursored row (run / job — any state). A pinned
		// record survives GC and the clear-finished bulk action until it is unpinned or deleted.
		if kind, id, ok := m.boxPinTarget(); ok {
			return m.togglePin(kind, id)
		}
	case "c":
		// Clear this session's finished (done/failed/stopped) jobs+runs, excluding pinned. Needs a real (non-"(no session)") session.
		if s, ok := m.focusedSession(); ok && s.sessionID != "" {
			return m.openClear(s.sessionID)
		}
	case "right", "enter":
		if _, onJob := m.boxJob(); onJob {
			if msg.String() == "right" {
				return m, nil // the card is already inline — nothing deeper
			}
			m.asPromptExpanded = !m.asPromptExpanded
			m.asCardScroll = m.clampAsCardScroll(m.asCardScroll)
			return m, nil
		}
		return m.asDescend()
	case "j":
		if _, onJob := m.boxJob(); onJob {
			m.asCardScroll = m.clampAsCardScroll(m.asCardScroll + 1)
		}
	case "k":
		if _, onJob := m.boxJob(); onJob {
			m.asCardScroll = m.clampAsCardScroll(m.asCardScroll - 1)
		}
	case "esc":
		return m.asAscend(true)
	case "left":
		return m.asAscend(false)
	}
	return m, nil
}

// focusBoxJobIO keeps the Subagents box's previewed card loaded as the L2 cursor moves
// (focusJobIO's boxes-level sibling, nonce-gated): scroll/fold reset when the cursor sits
// on a job row, and a stale preview (different job, or one still running) reloads.
func (m Model) focusBoxJobIO() (tea.Model, tea.Cmd) {
	j, ok := m.boxPreviewJob()
	if !ok {
		return m, nil
	}
	if _, onJob := m.boxJob(); onJob {
		m.asCardScroll = 0
		m.asPromptExpanded = false
	}
	if !m.jobCardStale(j) {
		return m, nil
	}
	m.asDetailNonce++
	m.asDetailTerminal = isTerminalLeaf(j.Status)
	return m, loadJobIOCmd(j.JobID, m.asDetailNonce, m.boardEpoch)
}

// updateAsEntity (L3): ↑/↓ walk the collection's rows (resetting the card scroll), j/k
// scroll the inline detail card; ←/esc ascend to the boxes — esc RETURNS from the detail card, it never exits the board
// from here.
func (m Model) updateAsEntity(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	jobs := m.railEntities()
	n := len(jobs)
	switch msg.String() {
	case "up":
		m.asEntityCursor = clampIndex(m.asEntityCursor-1, n)
		return m.focusEntityIO()
	case "down":
		m.asEntityCursor = clampIndex(m.asEntityCursor+1, n)
		return m.focusEntityIO()
	case "enter":
		// Fold/unfold the focused card's collapsible block (the job's Prompt, collapsed by
		// default); re-clamp the scroll since
		// the card height changes.
		m.asPromptExpanded = !m.asPromptExpanded
		m.asCardScroll = m.clampAsCardScroll(m.asCardScroll)
		return m, nil
	case "j":
		m.asCardScroll = m.clampAsCardScroll(m.asCardScroll + 1)
	case "k":
		m.asCardScroll = m.clampAsCardScroll(m.asCardScroll - 1)
	case "p":
		// Pin the focused detail row: a job by id.
		if j, ok := m.selectedJob(); ok {
			return m.togglePin(pinned.Job, j.JobID)
		}
	case "d":
		if j, ok := m.selectedJob(); ok {
			return m.openDeleteJob(j)
		}
	case "x":
		if j, ok := m.selectedJob(); ok {
			return m.openStopJob(j)
		}
	case "c":
		// Clear this session's closed records — also reachable here because a single-kind
		// session auto-skips the boxes level and lands straight on this entity view.
		if s, ok := m.focusedSession(); ok && s.sessionID != "" {
			return m.openClear(s.sessionID)
		}
	case "esc":
		if m.browserOrigin {
			return m.returnToBrowser() // esc from a browser-entered detail returns to the browser
		}
		return m.asAscend(true)
	case "left":
		// ← always does normal board ascent (so an ended team reaches its boxes-level delete);
		// exiting a browser-entered detail this way drops the browser-origin link.
		m.browserOrigin = false
		return m.asAscend(false)
	}
	return m, nil
}

// returnToBrowser re-opens the flat browser from a detail view entered via it, re-finding the
// row that was opened so the cursor lands back on it.
func (m Model) returnToBrowser() (tea.Model, tea.Cmd) {
	m.browserOrigin = false
	m.asMode = asModeBrowser
	m.reanchorBrowser()
	return m, nil
}

// singleKindSrc reports the one entity collection of a session whose boxes level would
// disambiguate nothing — only jobs — so navigation skips
// straight to the detail view in both directions.
func singleKindSrc(s asSession) (asRailRef, bool) {
	if len(s.runs) > 0 {
		return asRailRef{}, false // the Dynamic Workflows box must stay reachable
	}
	if len(s.jobs) > 0 {
		return asRailRef{jobs: true}, true
	}
	return asRailRef{}, false
}

// enterSession focuses a session and lands on its boxes — or straight on the entity view
// for a single-kind session (see singleKindSrc).
func (m *Model) enterSession(s asSession) {
	m.focusedSessionID = s.sessionID
	m.asBoxCursor, m.asEntityCursor, m.asCardScroll = 0, 0, 0
	if src, ok := singleKindSrc(s); ok {
		m.asEntitySrc = src
		m.asMode = asModeEntity
		return
	}
	m.asMode = asModeBoxes
}

// asDescend drops one level: projects → sessions (or straight into a sole session),
// sessions → the session's boxes (or its detail view when single-kind), boxes → the
// cursored row's entity detail. The entity level is the deepest.
func (m Model) asDescend() (tea.Model, tea.Cmd) {
	switch m.asMode {
	case asModeProjects:
		projects := m.asProjects()
		if m.asProjectCursor >= len(projects) {
			return m, nil
		}
		p := projects[m.asProjectCursor]
		m.focusedProject = p.dir
		m.asSessionCursor = 0
		if len(p.sessions) == 1 {
			m.enterSession(p.sessions[0])
		} else {
			m.asMode = asModeSessions
		}
	case asModeSessions:
		p, ok := m.focusedProjectGroup()
		if !ok || m.asSessionCursor >= len(p.sessions) {
			return m, nil
		}
		m.enterSession(p.sessions[m.asSessionCursor])
	case asModeBoxes:
		s, ok := m.focusedSession()
		if !ok {
			return m, nil
		}
		switch {
		case m.asBoxCursor < len(s.runs):
			// A run row opens the run's Phases level.
			m.focusedRunID = s.runs[m.asBoxCursor].runID
			m.wfPhaseCursor, m.wfAgentCursor = 0, 0
			m.asMode = asModeRunPhases
			return m, nil
		case m.asBoxCursor-len(s.runs) < len(s.jobs):
			// Descending from a job row keeps THAT job focused in the entity list.
			m.asEntitySrc = asRailRef{jobs: true}
			m.asEntityCursor = m.asBoxCursor - len(s.runs)
		default:
			return m, nil
		}
		m.asMode = asModeEntity
		m.asCardScroll = 0
	}
	if m.asMode == asModeEntity {
		return m.focusEntityIO()
	}
	return m, nil
}

// jobCardStale reports whether the visible job card needs a fresh io load: a different
// job, a still-running one (its Activity feed climbs), or the first refresh that sees it
// terminal (the final .answer lands after the running→done flip).
func (m Model) jobCardStale(j dsjobs.Result) bool {
	return j.JobID != m.asDetailJobID || !isTerminalLeaf(j.Status) || !m.asDetailTerminal
}

// focusEntityIO loads the focused entity's detail payload (job io). Called whenever the
// entity cursor lands on a row.
func (m Model) focusEntityIO() (tea.Model, tea.Cmd) {
	return m.focusJobIO()
}

// focusJobIO resets the card scroll/expand state and loads the focused job's io + activity
// into the inline detail, nonce-gated (mirror focusLeafIO). Called whenever the entity
// cursor lands on a job.
func (m Model) focusJobIO() (tea.Model, tea.Cmd) {
	m.asCardScroll = 0
	m.asPromptExpanded = false
	j, ok := m.selectedJob()
	if !ok {
		m.asDetailJobID, m.asDetailPrompt, m.asDetailAnswer, m.asDetailIO = "", "", "", false
		m.asDetailSnap = activitySnapshot{}
		return m, nil
	}
	m.asDetailNonce++
	m.asDetailTerminal = isTerminalLeaf(j.Status)
	return m, loadJobIOCmd(j.JobID, m.asDetailNonce, m.boardEpoch)
}

// asAscend climbs one level: entity → boxes, boxes → sessions (multi-session project) or
// projects (multi-project), sessions → projects. At the board's TOP level there's nowhere
// to climb: exitAtTop quits (esc) vs stays put (←) — so repeated ← can't fall
// out of the board (mirror wfAscend).
func (m Model) asAscend(exitAtTop bool) (tea.Model, tea.Cmd) {
	projects := m.asProjects()
	switch m.asMode {
	case asModeEntity:
		// A single-kind session skipped the boxes level on the way down; skip it on the
		// way up too.
		if s, ok := m.focusedSession(); ok {
			if _, single := singleKindSrc(s); !single {
				m.asMode = asModeBoxes
				return m, nil
			}
		} else {
			m.asMode = asModeBoxes
			return m, nil
		}
		fallthrough
	case asModeBoxes:
		if p, ok := m.focusedProjectGroup(); ok && len(p.sessions) > 1 {
			m.asMode = asModeSessions
			return m, nil
		}
		if len(projects) > 1 {
			m.asMode = asModeProjects
			return m, nil
		}
	case asModeSessions:
		if len(projects) > 1 {
			m.asMode = asModeProjects
			return m, nil
		}
	}
	if exitAtTop {
		m.quitting = true // ds-bridge: no provider hub above the board
		return m, tea.Quit
	}
	return m, nil
}

// asSessions is the board's session tree — recomputed per call off the loaded slices.
func (m Model) asSessions() []asSession {
	return groupSessions(m.jobs, m.wfGroups())
}

// asProjects is the session tree bucketed by recorded working directory, with each
// project's sessions codex-sunk (codex-launched sessions grouped below the non-codex
// ones) — applied AFTER groupProjects so the sink never reorders the projects themselves.
func (m Model) asProjects() []asProject {
	projects := groupProjects(m.asSessions(), m.sessionMeta)
	for i := range projects {
		projects[i].sessions = sinkCodexSessions(projects[i].sessions)
	}
	return projects
}

// focusedProjectGroup returns the project the sessions/boxes levels are rooted on.
func (m Model) focusedProjectGroup() (asProject, bool) {
	if m.focusedProject == asNoFocus {
		return asProject{}, false
	}
	for _, p := range m.asProjects() {
		if p.dir == m.focusedProject {
			return p, true
		}
	}
	return asProject{}, false
}

// focusedSession returns the session the boxes/entity levels are rooted on; ok=false when
// nothing is focused yet or its session vanished.
func (m Model) focusedSession() (asSession, bool) {
	if m.focusedSessionID == asNoFocus {
		return asSession{}, false
	}
	for _, s := range m.asSessions() {
		if s.sessionID == m.focusedSessionID {
			return s, true
		}
	}
	return asSession{}, false
}

// railEntities returns the L3 collection's rows per asEntitySrc: the session's standalone jobs.
func (m Model) railEntities() (jobs []dsjobs.Result) {
	s, ok := m.focusedSession()
	if !ok {
		return nil
	}
	if m.asEntitySrc.jobs {
		return s.jobs
	}
	return nil
}

// selectedJob returns the job under asEntityCursor (ok=false on a team rail).
func (m Model) selectedJob() (dsjobs.Result, bool) {
	jobs := m.railEntities()
	if m.asEntityCursor < 0 || m.asEntityCursor >= len(jobs) {
		return dsjobs.Result{}, false
	}
	return jobs[m.asEntityCursor], true
}

// asNoFocus marks "no project/session focused" — "" can't serve as the sentinel because it
// is the real id of the "(no session)" / "(no project)" buckets. The control byte keeps it
// impossible as a real value; it never renders.
const asNoFocus = "\x00unfocused"

// rerootSpawn re-derives the board's focus after a refresh lands (mirror rerootWorkflows):
// a still-valid focus chain keeps its drill state; a broken one (a fresh entry parks on
// asNoFocus so the first load routes, or the focused project/session vanished) re-routes by
// the loaded counts — 0 sessions → empty boxes, 1 session → enterSession (its boxes, or its
// detail view when single-kind), 1 project → its session list, >1 project → the project
// list.
func (m *Model) rerootSpawn() {
	sessions := m.asSessions()
	projects := groupProjects(sessions, m.sessionMeta)

	switch m.asMode {
	case asModeBrowser:
		// The flat browser is a first-class mode: rebuild its rows from the refreshed slices
		// and re-find the cursor by identity, WITHOUT routing to boxes/projects (the default
		// path below would eject it back to the board).
		m.reanchorBrowser()
		return
	case asModeRunPhases, asModeRunAgent:
		// The run drill keeps its place while the run and its session both survive; a
		// vanished run demotes to the session's boxes, a vanished session re-routes.
		if _, ok := m.focusedGroup(); ok {
			if s, ok := m.focusedSession(); ok {
				m.focusedProject = sessionProjectDir(s, m.sessionMeta)
				m.clampRunCursors()
				m.clampAsCursors()
				return
			}
		}
		if s, ok := m.focusedSession(); ok {
			m.asMode = asModeBoxes
			m.focusedRunID = ""
			m.browserOrigin = false // the browser-entered run is gone — drop its esc-return link
			m.focusedProject = sessionProjectDir(s, m.sessionMeta)
			m.clampAsCursors()
			return
		}
	case asModeEntity:
		if s, ok := m.focusedSession(); ok {
			// The session's recorded cwd can resolve late; keep the project link true.
			m.focusedProject = sessionProjectDir(s, m.sessionMeta)
			m.clampAsCursors()
			// The focused collection can vanish mid-watch (clamp demotes to boxes); a
			// now-single-kind session skips that level, as descend would.
			if m.asMode == asModeBoxes {
				if _, single := singleKindSrc(s); single {
					m.enterSession(s)
					m.clampAsCursors()
				}
			}
			return
		}
	case asModeBoxes:
		if s, ok := m.focusedSession(); ok {
			m.focusedProject = sessionProjectDir(s, m.sessionMeta)
			// The boxes level can turn degenerate mid-watch (the session lost its last
			// team or its last job): skip to the detail view, as descend would.
			if _, single := singleKindSrc(s); single {
				m.enterSession(s)
			}
			m.clampAsCursors()
			return
		}
	case asModeSessions:
		if p, ok := m.focusedProjectGroup(); ok && len(p.sessions) > 1 {
			m.clampAsCursors()
			return
		}
	case asModeProjects:
		if len(projects) > 1 {
			m.clampAsCursors()
			return
		}
	}

	// The focus chain broke and we re-route below (possibly straight into another session's
	// detail) — none of it was browser-entered, so drop any stale esc-return link.
	m.browserOrigin = false
	m.asProjectCursor, m.asSessionCursor, m.asBoxCursor, m.asEntityCursor, m.asCardScroll = 0, 0, 0, 0, 0
	m.focusedProject, m.focusedSessionID = asNoFocus, asNoFocus
	switch {
	case len(sessions) == 0:
		m.asMode = asModeBoxes // renders the empty state
	case len(sessions) == 1:
		m.focusedProject = projects[0].dir
		m.enterSession(sessions[0])
	case len(projects) == 1:
		m.focusedProject = projects[0].dir
		m.asMode = asModeSessions
	default:
		m.asMode = asModeProjects
	}
	m.clampAsCursors()
}

// clampAsCursors bounds every level's cursor to the live tree and drops out of entity mode
// when its collection emptied, so render can never index past the end.
func (m *Model) clampAsCursors() {
	m.asProjectCursor = clampIndex(m.asProjectCursor, len(m.asProjects()))
	if p, ok := m.focusedProjectGroup(); ok {
		m.asSessionCursor = clampIndex(m.asSessionCursor, len(p.sessions))
	} else {
		m.asSessionCursor = 0
	}
	s, ok := m.focusedSession()
	if !ok {
		m.asBoxCursor, m.asEntityCursor = 0, 0
		if m.asMode == asModeEntity {
			m.asMode = asModeBoxes
		}
		return
	}
	m.asBoxCursor = clampIndex(m.asBoxCursor, len(s.runs)+len(s.jobs))
	n := len(m.railEntities())
	m.asEntityCursor = clampIndex(m.asEntityCursor, n)
	if m.asMode == asModeEntity && n == 0 {
		m.asMode = asModeBoxes
	}
}

// The L2 continuum walks the session's runs, then its jobs.

// boxRun returns the run under the L2 continuum cursor (ok=false off the runs range).
func (m Model) boxRun() (runGroup, bool) {
	s, ok := m.focusedSession()
	if !ok {
		return runGroup{}, false
	}
	if i := m.asBoxCursor; i >= 0 && i < len(s.runs) {
		return s.runs[i], true
	}
	return runGroup{}, false
}

// boxPreviewJob is the job whose card the Subagents box shows: the cursored job while the
// continuum sits in the jobs range, else the FIRST job — the box never shows a flat list.
func (m Model) boxPreviewJob() (dsjobs.Result, bool) {
	if j, ok := m.boxJob(); ok {
		return j, true
	}
	s, ok := m.focusedSession()
	if !ok || len(s.jobs) == 0 {
		return dsjobs.Result{}, false
	}
	return s.jobs[0], true
}

// boxJob returns the job under the L2 continuum cursor (ok=false off the jobs range).
func (m Model) boxJob() (dsjobs.Result, bool) {
	s, ok := m.focusedSession()
	if !ok {
		return dsjobs.Result{}, false
	}
	if i := m.asBoxCursor - len(s.runs); i >= 0 && i < len(s.jobs) {
		return s.jobs[i], true
	}
	return dsjobs.Result{}, false
}

// boxRowRef returns the typed identity of the L2 row under asBoxCursor.
func (m Model) boxRowRef() (asBoxRef, bool) {
	s, ok := m.focusedSession()
	if !ok {
		return asBoxRef{}, false
	}
	switch {
	case m.asBoxCursor < len(s.runs):
		return asBoxRef{runID: s.runs[m.asBoxCursor].runID}, true
	case m.asBoxCursor-len(s.runs) < len(s.jobs):
		return asBoxRef{jobID: s.jobs[m.asBoxCursor-len(s.runs)].JobID}, true
	}
	return asBoxRef{}, false
}

// reanchorBox re-finds the previously cursored L2 row by its typed identity after a
// refresh; a vanished row leaves the index-clamped cursor in place.
func (m *Model) reanchorBox(prev asBoxRef) {
	s, ok := m.focusedSession()
	if !ok {
		return
	}
	if prev.runID != "" {
		for i, g := range s.runs {
			if g.runID == prev.runID {
				m.asBoxCursor = i
				return
			}
		}
		return
	}
	for i, j := range s.jobs {
		if prev.jobID != "" && j.JobID == prev.jobID {
			m.asBoxCursor = len(s.runs) + i
			return
		}
	}
}

// entityRowRef returns the L3 entity under asEntityCursor as a typed identity — a job by id — so the cursor can re-find its row after a refresh shifts the indices. It
// reports nothing outside asModeEntity, so a stale asEntitySrc left by an earlier visit can't be
// read as a live entity focus while the board is in a run drill (which would wrongly drop the run's
// browser back-link on the next refresh).
func (m Model) entityRowRef() (browserRef, bool) {
	if m.asMode != asModeEntity {
		return browserRef{}, false
	}
	if j, ok := m.selectedJob(); ok {
		return browserRef{kind: browserJob, id: j.JobID}, true
	}
	return browserRef{}, false
}

// reanchorEntity re-finds the previously focused L3 entity by its typed identity after a refresh
// (mirror reanchorBox), reporting whether it was found. A vanished entity leaves the clamped cursor
// on a sibling — the caller drops browserOrigin in that case, so esc no longer returns to the
// browser from a row the user never opened from it.
func (m *Model) reanchorEntity(prev browserRef) bool {
	if m.asMode != asModeEntity {
		return false
	}
	jobs := m.railEntities()
	if prev.kind == browserJob {
		for i, j := range jobs {
			if j.JobID == prev.id {
				m.asEntityCursor = i
				return true
			}
		}
	}
	return false
}

// clampAsCardScroll bounds the visible card's scroll to [0, lines-viewport] so j/k never
// scroll past the content (mirror clampCardScroll). The card lives in the entity view OR
// inline in the L2 Subagents box — each with its own geometry.
func (m Model) clampAsCardScroll(v int) int {
	var max int
	if m.asMode == asModeBoxes {
		if j, ok := m.boxJob(); ok {
			s, _ := m.focusedSession()
			_, jobsH := m.splitBoxHeights(s)
			max = len(m.jobDetailLines(j, m.jobsBoxRightWidth(s))) - jobsH
		}
	} else {
		max = len(m.entityDetailLines(m.asEntityRightWidth())) - m.asEntityBodyHeight()
	}
	if max < 0 {
		max = 0
	}
	switch {
	case v < 0:
		return 0
	case v > max:
		return max
	default:
		return v
	}
}

// wfGroups is the board's run→phase→agent tree (newest-first) — the single source the Dynamic
// Workflows box rail, phases, agents, and their cursors index.
func (m Model) wfGroups() []runGroup { return groupByRun(m.workflowJobs, m.workflowRuns) }

// focusedGroup returns the run the board is rooted on, ok=false when focusedRunID is unset or GC'd.
func (m Model) focusedGroup() (runGroup, bool) {
	for _, g := range m.wfGroups() {
		if g.runID == m.focusedRunID {
			return g, true
		}
	}
	return runGroup{}, false
}

// focusedPhase returns the phase under wfPhaseCursor in the focused run.
func (m Model) focusedPhase() (runPhaseGroup, bool) {
	g, ok := m.focusedGroup()
	if !ok || m.wfPhaseCursor < 0 || m.wfPhaseCursor >= len(g.phases) {
		return runPhaseGroup{}, false
	}
	return g.phases[m.wfPhaseCursor], true
}

// selectedLeaf returns the agent (job) under wfAgentCursor in the focused phase.
func (m Model) selectedLeaf() (dsjobs.Result, bool) {
	p, ok := m.focusedPhase()
	if !ok || m.wfAgentCursor < 0 || m.wfAgentCursor >= len(p.jobs) {
		return dsjobs.Result{}, false
	}
	return p.jobs[m.wfAgentCursor], true
}

// selectedRunID returns the focused run id (x/r act on the whole run, even when the focused phase
// has no agents).
func (m Model) selectedRunID() (string, bool) {
	if _, ok := m.focusedGroup(); ok {
		return m.focusedRunID, true
	}
	return "", false
}

// clampRunCursors bounds the run drill's phase/agent cursors to the live data and drops out
// of the agent level when the focused phase has no agents, so Enter/render can never index
// past the end.
func (m *Model) clampRunCursors() {
	g, ok := m.focusedGroup()
	if !ok {
		m.wfPhaseCursor, m.wfAgentCursor = 0, 0
		return
	}
	m.wfPhaseCursor = clampIndex(m.wfPhaseCursor, len(g.phases))
	agents := 0
	if m.wfPhaseCursor < len(g.phases) {
		agents = len(g.phases[m.wfPhaseCursor].jobs)
	}
	m.wfAgentCursor = clampIndex(m.wfAgentCursor, agents)
	if m.asMode == asModeRunAgent && agents == 0 {
		m.asMode = asModeRunPhases
	}
}

// clampIndex keeps i in [0, n-1]; returns 0 when n==0 (an empty list parks the cursor at 0).
func clampIndex(i, n int) int {
	switch {
	case n <= 0:
		return 0
	case i >= n:
		return n - 1
	case i < 0:
		return 0
	default:
		return i
	}
}

// updateWfPhases (run drill): ↑/↓ walk phases, → descend into a non-empty phase's agents, ←/esc
// ascend to the session's boxes; x/r control the focused run.
func (m Model) updateWfPhases(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	g, ok := m.focusedGroup()
	switch msg.String() {
	case "up":
		if ok {
			m.wfPhaseCursor = clampIndex(m.wfPhaseCursor-1, len(g.phases))
			m.wfAgentCursor = 0
		}
	case "down":
		if ok {
			m.wfPhaseCursor = clampIndex(m.wfPhaseCursor+1, len(g.phases))
			m.wfAgentCursor = 0
		}
	case "right", "enter":
		return m.wfDescend()
	case "esc":
		if m.browserOrigin {
			return m.returnToBrowser() // esc from a browser-entered run drill returns to the browser
		}
		return m.wfAscend()
	case "left":
		m.browserOrigin = false // ← does normal ascent and drops the browser-origin link
		return m.wfAscend()
	case "x":
		return m.stopFocusedPhase()
	default:
		return m.wfControl(msg)
	}
	return m, nil
}

// updateWfAgent (run drill): ↑/↓ walk agents (reloading the inline detail), j/k scroll that detail,
// ←/esc ascend to Phases; x stops ONLY the focused live agent.
func (m Model) updateWfAgent(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	p, ok := m.focusedPhase()
	switch msg.String() {
	case "up":
		if ok {
			m.wfAgentCursor = clampIndex(m.wfAgentCursor-1, len(p.jobs))
			return m.focusLeafIO()
		}
	case "down":
		if ok {
			m.wfAgentCursor = clampIndex(m.wfAgentCursor+1, len(p.jobs))
			return m.focusLeafIO()
		}
	case "j":
		m.wfCardScroll = m.clampCardScroll(m.wfCardScroll + 1)
	case "k":
		m.wfCardScroll = m.clampCardScroll(m.wfCardScroll - 1)
	case "left", "esc":
		// Agent sits below the run's entry level (Phases), so both back keys retrace one level
		// to Phases, which owns the browser-origin return; the origin survives the retrace.
		return m.wfAscend()
	case "right":
		return m, nil // already at the deepest level
	case "enter":
		// Fold/unfold the focused agent's prompt (collapsed by default); re-clamp the scroll
		// since the detail height changes.
		m.wfPromptExpanded = !m.wfPromptExpanded
		m.wfCardScroll = m.clampCardScroll(m.wfCardScroll)
		return m, nil
	case "x":
		return m.stopFocusedLeaf()
	default:
		return m.wfControl(msg)
	}
	return m, nil
}

// wfDescend drops Phases → agent detail (when the focused phase has agents, loading the
// first agent's io). The agent level is the deepest.
func (m Model) wfDescend() (tea.Model, tea.Cmd) {
	if p, ok := m.focusedPhase(); ok && len(p.jobs) > 0 {
		m.asMode = asModeRunAgent
		m.wfAgentCursor = 0
		return m.focusLeafIO()
	}
	return m, nil
}

// wfAscend climbs out of the run drill: agent → Phases, Phases → the session's boxes
// (which always exist for a session with runs — the skip rule guarantees it).
func (m Model) wfAscend() (tea.Model, tea.Cmd) {
	if m.asMode == asModeRunAgent {
		m.asMode = asModeRunPhases
		return m, nil
	}
	m.asMode = asModeBoxes
	m.focusedRunID = ""
	return m, nil
}

// focusLeafIO resets the detail scroll and loads the focused leaf's prompt/answer into the inline
// detail pane (nonce-gated so a slow read for a prior leaf is dropped). Called whenever the agent
// cursor lands on a new leaf.
func (m Model) focusLeafIO() (tea.Model, tea.Cmd) {
	m.wfCardScroll = 0
	m.wfPromptExpanded = false // each newly focused leaf starts with its prompt collapsed
	job, ok := m.selectedLeaf()
	if !ok {
		m.wfDetailJob, m.wfDetailPrompt, m.wfDetailAnswer, m.wfDetailIO = dsjobs.Result{}, "", "", false
		return m, nil
	}
	m.wfDetailNonce++ // drop a slow read for a previously-focused leaf
	return m, loadLeafIOCmd(job, m.wfDetailNonce, m.boardEpoch)
}

// clampCardScroll bounds the inline-detail scroll offset to [0, lines-viewport] so j/k never scroll
// past the content (and a stale offset from a longer leaf snaps back on a shorter one).
func (m Model) clampCardScroll(v int) int {
	max := len(m.agentDetailLines(m.wfAgentRightWidth())) - m.boardBodyHeight()
	if max < 0 {
		max = 0
	}
	switch {
	case v < 0:
		return 0
	case v > max:
		return max
	default:
		return v
	}
}

// wfBusy reports whether a stop/restart/delete is already in flight for runID — the in-flight guard
// that makes a second x/r/d on the same run a no-op until its workflowCtlMsg lands.
func (m Model) wfBusy(runID string) bool { return m.wfRestarting[runID] }

// markBusy adds runID to the in-flight guard (lazily creating the map).
func (m Model) markBusy(runID string) Model {
	if m.wfRestarting == nil {
		m.wfRestarting = map[string]bool{}
	}
	m.wfRestarting[runID] = true
	return m
}

// stopFocusedPhase is the Phases pane's x — scoped to the focused phase title on a
// LIVE run: every live member holds and future members of the title park (a reused
// title is ONE merged target). A finished run has nothing to stop.
func (m Model) stopFocusedPhase() (tea.Model, tea.Cmd) {
	p, ok := m.focusedPhase()
	if !ok {
		return m, nil
	}
	g, gok := m.focusedGroup()
	if !gok {
		return m, nil
	}
	if m.wfBusy(g.runID) {
		return m, nil
	}
	if g.status != "running" {
		return m.withInfo("the run is not live — nothing to stop", false), nil
	}
	if p.status != "running" {
		return m.withInfo("this step is not running — nothing to stop", false), nil
	}
	m.confirm = &confirmModal{kind: confirmStopPhase, id: g.runID, arg: p.title,
		prompt: fmt.Sprintf("Stop step %q? Its job is interrupted and steps that need it are skipped; the other steps keep running.", p.title)}
	return m, nil
}

// phaseWidening names the OTHER phase titles a keyed phase restart would re-run too:
// identical agent() calls share one content key, and the restart drops whole keys.
func (m Model) phaseWidening(g runGroup, phase string) []string {
	keys := map[string]bool{}
	for _, p := range g.phases {
		if p.title != phase {
			continue
		}
		for _, j := range p.jobs {
			if j.JournalKey != "" {
				keys[j.JournalKey] = true
			}
		}
	}
	widened := map[string]bool{}
	for _, p := range g.phases {
		if p.title == phase {
			continue
		}
		for _, j := range p.jobs {
			if keys[j.JournalKey] && j.JournalKey != "" {
				widened[p.title] = true
			}
		}
	}
	out := make([]string, 0, len(widened))
	for t := range widened {
		out = append(out, t)
	}
	sort.Strings(out)
	return out
}

// stopFocusedLeaf is the agent pane's x — scoped to exactly the focused leaf. A live
// (running/queued) leaf gets the kill-and-HOLD confirm; held is already stopped; a
// terminal leaf has nothing left to stop (its value is consumed).
func (m Model) stopFocusedLeaf() (tea.Model, tea.Cmd) {
	job, ok := m.selectedLeaf()
	if !ok {
		return m, nil
	}
	g, gok := m.focusedGroup()
	if !gok {
		return m, nil
	}
	if m.wfBusy(g.runID) {
		return m, nil
	}
	if g.status != "running" {
		return m.withInfo("the run is not live — nothing to stop", false), nil
	}
	switch job.Status {
	case "held":
		return m.withInfo("agent is already held", false), nil
	case "running", "queued", "":
		m.confirm = &confirmModal{kind: confirmStopLeaf, id: g.runID, arg: job.JobID,
			prompt: "Stop job " + job.JobID + "? It is interrupted and steps that need it are skipped; the other steps keep running."}
		return m, nil
	default:
		return m.withInfo("agent already finished", false), nil
	}
}

// wfControl runs the run-level control (x stop) shared by the Phases pane and the Agent
// pane's non-r keys — both target the FOCUSED run, so they work even when the focused phase has no
// agents. (Save lives at the outermost run row, not in the drill — see updateAsBoxes.)
func (m Model) wfControl(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	runID, ok := m.selectedRunID()
	if !ok {
		return m, nil
	}
	switch msg.String() {
	case "x":
		if m.wfBusy(runID) {
			return m, nil
		}
		g, _ := m.focusedGroup()
		return m.openStopRun(runID, g.status)
	}
	return m, nil
}

// printableRunes drops everything that cannot sit on one line of the box: a bracketed
// paste arrives as one key message and may carry newlines and control bytes, which
// would render straight through the pane border.
func printableRunes(rs []rune) string {
	var b strings.Builder
	for _, r := range rs {
		if unicode.IsPrint(r) {
			b.WriteRune(r)
		}
	}
	return b.String()
}

// Run starts the bubbletea program against stdin/stdout. The caller is
// responsible for ensuring those are a terminal (see cmd/cc-fleet/tui.go).
// verbose routes the session's step trace to a per-pid 0600 log file.
func Run() error {
	// Resolve the terminal background (light vs dark) before bubbletea owns
	// stdin: the adaptive palette's first render would otherwise race the
	// terminal's color-query reply against the program's input reader.
	lipgloss.HasDarkBackground()
	_, err := tea.NewProgram(NewModel()).Run()
	return err
}

// openStopJob is x on a job row: a running job gets the stop confirm (SIGTERM to its ds process,
// which interrupts the OpenCode session); anything else says why there is nothing to stop.
func (m Model) openStopJob(j dsjobs.Result) (tea.Model, tea.Cmd) {
	if j.Status != "running" {
		return m.withInfo("job "+j.JobID+" is not running — nothing to stop", false), nil
	}
	if m.wfBusy(j.JobID) {
		return m, nil
	}
	m.confirm = &confirmModal{kind: confirmStopJob, id: j.JobID,
		prompt: "Stop job " + j.JobID + "? ds interrupts its OpenCode session; Claude can still revise or reject it."}
	return m, nil
}

// openStopRun is x on a batch: only a running one; its running jobs are interrupted and no
// further step starts.
func (m Model) openStopRun(runID, status string) (tea.Model, tea.Cmd) {
	if status != "running" {
		return m.withInfo("batch "+runID+" is not running — nothing to stop", false), nil
	}
	if m.wfBusy(runID) {
		return m, nil
	}
	return m.openConfirm(confirmStop, runID, "Stop batch "+runID+"? Its running jobs are interrupted and no further step starts.")
}

// openDeleteJob is d on a job row: a closed job gets the delete confirm; an open or running one
// still goes through ds forget's refusal, so the modal names the reason.
func (m Model) openDeleteJob(j dsjobs.Result) (tea.Model, tea.Cmd) {
	if j.Status == "running" || j.Status == "queued" {
		return m.withInfo("job "+j.JobID+" is running — stop it first (x)", true), nil
	}
	return m.openConfirm(confirmJob, j.JobID, "Delete job "+j.JobID+"? ds forget removes it for good.")
}

// openClear is c: the count of the session's closed, unpinned jobs and batches, then the confirm.
func (m Model) openClear(sessionID string) (tea.Model, tea.Cmd) {
	n := len(dsjobs.ClearIDs(m.jobs, m.workflowRuns, sessionID, m.pins))
	if n == 0 {
		return m.withInfo("nothing to clear: no closed, unpinned jobs in this session", false), nil
	}
	what := fmt.Sprintf("%d closed, unpinned job(s) or batch(es)", n)
	return m.openConfirmDanger(confirmClear, sessionID, "Clear "+what+" of this session? ds forget removes them for good.")
}

// stopJobCmd sends the stop off the Update goroutine; the outcome resolves the running modal.
func stopJobCmd(jobID string, epoch int) tea.Cmd {
	return func() tea.Msg {
		return workflowCtlMsg{verb: "stop-job", runID: jobID, err: dsjobs.StopJob(jobID), epoch: epoch}
	}
}
