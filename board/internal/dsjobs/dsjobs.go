// Package dsjobs is the board's data layer: ds jobs and batches read from the ds state dir
// (SPEC section 6), in the shapes the ported cc-fleet board reads (subagent.Result,
// subagent.WorkflowRun). Jobs are read in read.go; batches come in T4.
package dsjobs

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
	// Outcome is the ds outcome text for the Outcome line ("OK · landed", "rejected",
	// "interrupted", the result word); Hint is a second line (BLOCKED's question and reply hint).
	Outcome string
	Hint    string
	// Kind is run or research.
	Kind string
	// Tools is job.json tools (summed over rounds): the tool count of a finished job.
	Tools int
	// Closed: not running and nothing left for Claude to approve or reject (job.json status
	// landed, approved or rejected, or open with its private copy gone). ds forget decides for
	// real; the board uses this for the clear count.
	Closed bool
}

// RunPhase is one phase (ds: batch group) of a run.
type RunPhase struct {
	Title  string `json:"title"`
	Detail string `json:"detail,omitempty"`
	// Status is the phase's own status when the batch knows it (ds: a SKIPPED group is "stopped",
	// a failed one "failed"); "" or "queued" lets the board derive it from the agents.
	Status string `json:"status,omitempty"`
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
	// Closed: the batch is not running and every job of it is Closed.
	Closed bool
}

// ErrCodeStopped is the ErrorCode of a job finalized by a stop (a stop, not a failure).
const ErrCodeStopped = "SUBAGENT_STOPPED"

// WithRunLock runs fn (ds batches need no lock on the board side).
func WithRunLock(runID string, fn func() error) error { return fn() }

// KeptWorktreeNotices lists worktrees a delete kept (none for ds: the Python side owns them).
func KeptWorktreeNotices(runID string) []string { return nil }
