// Package dsjobs is the board's data layer: ds jobs and batches read from the ds state dir
// (SPEC section 6), in the shapes the ported cc-fleet board reads (subagent.Result,
// subagent.WorkflowRun). T2: every reader returns fixed sample data; T3 reads the real files.
package dsjobs

import (
	"errors"
	"time"

	"ds-bridge/board/internal/pinned"
)

// StateDir is the ds state dir (~/.local/share/ds-bridge, or --state). Set by main.
var StateDir string

// Usage mirrors cc-fleet's subagent.Usage: the token counts the board shows.
type Usage struct {
	InputTokens              int `json:"input_tokens,omitempty"`
	OutputTokens             int `json:"output_tokens,omitempty"`
	CacheReadInputTokens     int `json:"cache_read_input_tokens,omitempty"`
	CacheCreationInputTokens int `json:"cache_creation_input_tokens,omitempty"`
}

// Result holds the fields the board reads from cc-fleet's subagent.Result, filled from a ds job.
type Result struct {
	OK            bool
	Result        string // the final answer text (ds: report.txt)
	Provider      string
	Model         string
	DurationMs    int64
	NumTurns      int    // ds: steps
	StopReason    string // ds: the result word (OK, FAIL, BLOCKED, …)
	Usage         *Usage
	CostUSD       float64
	SessionID     string
	LeadSessionID string // ds: job owner (CLAUDE_CODE_SESSION_ID)
	RunID         string // ds: batch id
	Phase         string // ds: batch group label
	Label         string
	JournalKey    string
	Attempt       int    // ds: round
	PromptProfile string // ds: agent
	SlimDowngrade string
	JobID         string
	Status        string // queued | running | held | done | failed | stopped
	PID           int
	StartedAt     string // RFC3339
	ErrorCode     string
	ErrorMsg      string
	Suggestion    string

	// Project is the job's real project dir (job "project", else "dir"); ds-bridge only. The board
	// groups sessions by it when the session's own cwd is unknown.
	Project string
}

// RunPhase is one phase (ds: batch group) of a run.
type RunPhase struct {
	Title  string `json:"title"`
	Detail string `json:"detail,omitempty"`
}

// WorkflowRun holds the fields the board reads from cc-fleet's subagent.WorkflowRun (ds: a batch).
type WorkflowRun struct {
	RunID       string
	Name        string
	Description string
	StartedAt   string
	UpdatedAt   string
	Phases      []RunPhase
	Status      string
	SessionID   string
	Cwd         string
}

// ErrCodeStopped is the ErrorCode of a job finalized by a stop (a stop, not a failure).
const ErrCodeStopped = "SUBAGENT_STOPPED"

// errNotYet is what the T5 actions return until they are built.
var errNotYet = errors.New("not available yet (ticket T5)")

func ago(d time.Duration) string { return time.Now().Add(-d).UTC().Format(time.RFC3339) }

// ListJobs returns every ds job, newest first. T2: fixed sample data.
func ListJobs() ([]Result, error) {
	return []Result{
		{JobID: "1122-1630", Status: "running", Model: "deepseek-v4-flash", PromptProfile: "worker",
			LeadSessionID: "sample-session-a1", StartedAt: ago(90 * time.Second), PID: 1,
			Usage: &Usage{InputTokens: 11241, OutputTokens: 188}, Attempt: 1, Project: "/home/sample/proj-alpha"},
		{JobID: "1122-1610", Status: "done", OK: true, Model: "deepseek-v4-flash", PromptProfile: "worker",
			LeadSessionID: "sample-session-a1", StartedAt: ago(20 * time.Minute), DurationMs: 15000,
			NumTurns: 4, StopReason: "OK", Usage: &Usage{InputTokens: 11495, OutputTokens: 235, CacheReadInputTokens: 33152},
			Attempt: 2, Result: "STATUS: DONE\nSUMMARY: Changed add() in calc.py to return a + b.\nREVIEW: calc.py:1-2",
			Project: "/home/sample/proj-alpha"},
		{JobID: "1122-1500", Status: "failed", Model: "muse-spark", PromptProfile: "worker",
			LeadSessionID: "sample-session-a2", StartedAt: ago(90 * time.Minute), DurationMs: 300000,
			NumTurns: 12, StopReason: "FAIL", ErrorCode: "FAIL", ErrorMsg: "check failed: pytest -q (exit 1)",
			Usage: &Usage{InputTokens: 40210, OutputTokens: 3120}, Attempt: 1, Project: "/home/sample/proj-alpha"},
		{JobID: "1122-1400", Status: "done", OK: true, Model: "deepseek-v4-flash", PromptProfile: "researcher",
			LeadSessionID: "sample-session-b1", StartedAt: ago(3 * time.Hour), DurationMs: 42000,
			NumTurns: 6, StopReason: "OK", Usage: &Usage{InputTokens: 20500, OutputTokens: 900}, Attempt: 1,
			Result: "ANSWER: the config is read in dslib/core.py cfg().", Project: "/home/sample/proj-beta"},
		{JobID: "1122-1300", Status: "done", OK: true, Model: "deepseek-v4-flash", PromptProfile: "worker",
			LeadSessionID: "sample-session-b1", RunID: "b1122-1300", Phase: "1", Label: "step 1",
			StartedAt: ago(4 * time.Hour), DurationMs: 60000, NumTurns: 5, StopReason: "OK",
			Usage: &Usage{InputTokens: 15000, OutputTokens: 600}, Attempt: 1, Result: "STATUS: DONE", Project: "/home/sample/proj-beta"},
		{JobID: "1122-1301", Status: "done", OK: true, Model: "deepseek-v4-flash", PromptProfile: "worker",
			LeadSessionID: "sample-session-b1", RunID: "b1122-1300", Phase: "2", Label: "step 2",
			StartedAt: ago(4 * time.Hour), DurationMs: 80000, NumTurns: 7, StopReason: "OK",
			Usage: &Usage{InputTokens: 18000, OutputTokens: 700}, Attempt: 1, Result: "STATUS: DONE", Project: "/home/sample/proj-beta"},
	}, nil
}

// ListRuns returns every ds batch as a run. T2: fixed sample data.
func ListRuns() ([]WorkflowRun, error) {
	return []WorkflowRun{{RunID: "b1122-1300", Name: "sample batch", StartedAt: ago(4 * time.Hour),
		UpdatedAt: ago(4 * time.Hour), Phases: []RunPhase{{Title: "1"}, {Title: "2"}}, Status: "done",
		SessionID: "sample-session-b1", Cwd: "/home/sample/proj-beta"}}, nil
}

// ReadIO returns a job's prompt and final output, and whether either was found. T2: sample text.
func ReadIO(jobID string) (prompt, answer string, present bool) {
	for _, j := range mustJobs() {
		if j.JobID == jobID {
			return "Fix add() in calc.py so it returns the sum.\n\nTASK (quick):\n- check: python3 -c 'import calc'\n" +
				"- files: find them yourself; edit only what the task needs\n- report with the usual block", j.Result, true
		}
	}
	return "", "", false
}

// Activity is one job's activity.jsonl read into tool signatures and the latest usage.
type Activity struct {
	Sigs          []string
	InTok, OutTok int
	HasUsage      bool
}

// ReadActivity reads a job's activity. T2: sample tool calls.
func ReadActivity(jobID string) (Activity, bool) {
	for _, j := range mustJobs() {
		if j.JobID == jobID {
			a := Activity{Sigs: []string{"shell(ls -la && cat calc.py)", "read(calc.py)", "edit(calc.py)",
				"shell(python3 -c 'import calc; assert calc.add(2,3)==5')"}}
			if j.Usage != nil {
				a.InTok, a.OutTok, a.HasUsage = j.Usage.InputTokens, j.Usage.OutputTokens, true
			}
			return a, true
		}
	}
	return Activity{}, false
}

func mustJobs() []Result {
	js, _ := ListJobs()
	return js
}

// The actions below come in T5 (stop, delete, clear). Until then they refuse.

// StopRun stops a running batch.
func StopRun(runID string) (bool, error) { return false, errNotYet }

// PurgeRun deletes a closed batch.
func PurgeRun(runID string) error { return errNotYet }

// WithRunLock runs fn (ds batches need no lock on the board side).
func WithRunLock(runID string, fn func() error) error { return fn() }

// DeleteJob deletes a closed job.
func DeleteJob(jobID string) error { return errNotYet }

// StopJob stops a running job.
func StopJob(jobID string) error { return errNotYet }

// DeleteSession deletes every closed, unpinned job of a session; returns how many.
func DeleteSession(sessionID string, pins pinned.Set) (int, error) {
	return 0, errNotYet
}

// ClearFinished deletes every closed, unpinned job of a session; returns how many.
func ClearFinished(sessionID string, pins pinned.Set) (int, error) {
	return 0, errNotYet
}

// KeptWorktreeNotices lists worktrees a delete kept (none for ds: the Python side owns them).
func KeptWorktreeNotices(runID string) []string { return nil }

// SendLeafCommand stops one job of a batch (verb "stop-leaf").
func SendLeafCommand(runID, verb, leafID string) error { return errNotYet }

// SendPhaseCommand stops one group of a batch (verb "stop-phase").
func SendPhaseCommand(runID, verb, phase string) error { return errNotYet }
