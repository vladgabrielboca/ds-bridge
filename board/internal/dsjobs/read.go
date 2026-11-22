package dsjobs

import (
	"bufio"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"syscall"
	"time"
)

// This file reads ds jobs from the state dir (SPEC 5 and 6). It never writes there.

// jobFile is the part of jobs/<id>/job.json the board reads.
type jobFile struct {
	ID      string  `json:"id"`
	Dir     string  `json:"dir"`
	Project string  `json:"project"`
	Model   string  `json:"model"`
	Agent   string  `json:"agent"`
	Round   int     `json:"round"`
	Status  string  `json:"status"`
	Task    string  `json:"task"`
	Owner   string  `json:"owner"`
	Started float64 `json:"started"`
	Batch   string  `json:"batch"`
	Group   any     `json:"batch_group"`
	Steps   int     `json:"steps"`
	Tools   int     `json:"tools"`
	Cost    float64 `json:"cost"`
	Tokens  *struct {
		In    int `json:"in"`
		Out   int `json:"out"`
		Cache int `json:"cache"`
	} `json:"tokens"`
	Result string  `json:"result"`
	Ended  float64 `json:"ended"`
}

// progressFile is the part of progress/<id>.json the board reads.
type progressFile struct {
	State     string  `json:"state"`
	Round     int     `json:"round"`
	Started   float64 `json:"started"`
	Updated   float64 `json:"updated"`
	Steps     int     `json:"steps"`
	PID       int     `json:"pid"`
	OutTokens int     `json:"out_tokens"`
	Last      string  `json:"last"`
}

// failWords are the result words that make a job failed (dslib/results.py job_status, finish_research).
var failWords = map[string]bool{"FAIL": true, "BLOCKED": true, "CONFLICT": true, "LIMIT": true,
	"PARTIAL": true, "PROVIDER": true, "ERROR": true}

func jobsDir() string { return filepath.Join(StateDir, "jobs") }

func readJSON(path string, v any) bool {
	b, err := os.ReadFile(path)
	if err != nil {
		return false
	}
	return json.Unmarshal(b, v) == nil
}

// pidAlive is ds's pid_alive: /proc/<pid> exists (signal 0 also covers a pid we may not inspect).
func pidAlive(pid int) bool {
	if pid <= 0 {
		return false
	}
	if _, err := os.Stat(fmt.Sprintf("/proc/%d", pid)); err == nil {
		return true
	}
	return syscall.Kill(pid, 0) == nil
}

func epochRFC3339(sec float64) string {
	if sec <= 0 {
		return ""
	}
	s, frac := math.Modf(sec)
	return time.Unix(int64(s), int64(frac*1e9)).Local().Format(time.RFC3339)
}

// shortModel drops the provider prefix ("opencode-go/deepseek-v4.1-flash" → "deepseek-v4.1-flash").
func shortModel(m string) string {
	if i := strings.LastIndex(m, "/"); i >= 0 {
		return m[i+1:]
	}
	return m
}

// lastTxt returns the lines of jobs/<id>/last.txt (nil when absent).
func lastTxt(id string) []string {
	b, err := os.ReadFile(filepath.Join(jobsDir(), id, "last.txt"))
	if err != nil {
		return nil
	}
	return strings.Split(strings.TrimRight(string(b), "\n"), "\n")
}

func exists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}

// ListJobs returns every ds job, newest first. A batch child carries RunID (the batch id) and
// Phase (its group's phase title).
func ListJobs() ([]Result, error) {
	ents, err := os.ReadDir(jobsDir())
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	batches := readBatches()
	var out []Result
	for _, e := range ents {
		if !e.IsDir() || strings.HasPrefix(e.Name(), "b") {
			continue
		}
		if r, ok := readJob(e.Name(), batches); ok {
			out = append(out, r)
		}
	}
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].StartedAt != out[j].StartedAt {
			return out[i].StartedAt > out[j].StartedAt
		}
		return out[i].JobID > out[j].JobID
	})
	return out, nil
}

// readJob builds one job's Result from job.json, progress/<id>.json, last.txt and the marker files.
func readJob(id string, batches map[string]*batchFile) (Result, bool) {
	var j jobFile
	if !readJSON(filepath.Join(jobsDir(), id, "job.json"), &j) {
		return Result{}, false
	}
	if j.ID == "" {
		j.ID = id
	}
	var p progressFile
	hasProg := readJSON(filepath.Join(StateDir, "progress", id+".json"), &p)
	last := lastTxt(id)
	word := j.Result
	if word == "" && len(last) > 0 {
		if f := strings.Fields(last[0]); len(f) > 0 {
			word = f[0]
		}
	}
	project := j.Project
	if project == "" {
		project = j.Dir
	}
	kind := "run"
	if exists(filepath.Join(jobsDir(), id, "answer.txt")) || j.Agent == "researcher" {
		kind = "research"
	}
	round := j.Round
	if round < 1 {
		round = 1
	}
	r := Result{
		JobID:         j.ID,
		Model:         shortModel(j.Model),
		PromptProfile: j.Agent,
		Kind:          kind,
		LeadSessionID: j.Owner,
		Project:       project,
		StartedAt:     epochRFC3339(j.Started),
		Attempt:       round,
		NumTurns:      j.Steps,
		Tools:         j.Tools,
		CostUSD:       j.Cost,
		StopReason:    word,
	}
	if j.Tokens != nil {
		r.Usage = &Usage{InputTokens: j.Tokens.In, OutputTokens: j.Tokens.Out, CacheReadInputTokens: j.Tokens.Cache}
	}
	// A live round's own start and counters come from the progress file.
	if hasProg && p.State == "running" {
		r.PID = p.PID
		if p.Started > 0 {
			r.StartedAt = epochRFC3339(p.Started)
		}
	}
	if j.Batch != "" {
		// The id is the agent row's label: every child runs the same model.
		r.RunID, r.Phase, r.Label = j.Batch, "?", r.JobID
		if b, ok := batches[j.Batch]; ok {
			r.Phase = b.phaseOf(r.JobID, groupIndex(j.Group))
		}
	}
	setStatus(&r, j, p, hasProg, word, last)
	r.Closed = closedJob(r.Status, j)
	// Wall-clock of a finished job: job ended (else the progress file's last update) minus started.
	if r.Status != "running" && r.Status != "queued" {
		end := j.Ended
		if end <= 0 && hasProg {
			end = p.Updated
		}
		if end > j.Started && j.Started > 0 {
			r.DurationMs = int64((end - j.Started) * 1000)
		}
	}
	return r, true
}

// setStatus applies SPEC 5, in its order.
func setStatus(r *Result, j jobFile, p progressFile, hasProg bool, word string, last []string) {
	switch {
	case hasProg && p.State == "running" && pidAlive(p.PID):
		r.Status = "running"
		if p.Round > 0 {
			r.Attempt = p.Round
		}
		return
	case hasProg && p.State == "running":
		r.Status, r.ErrorCode, r.Outcome = "failed", "interrupted", "interrupted"
		return
	case hasProg && p.State == "error" && (word == "" || (p.Last == "interrupted" && p.Updated > j.Ended+1)):
		// The newest round died before it wrote its result (mark_interrupted writes
		// last=interrupted). A round that ended with an error has its word in job.json instead.
		msg := "error"
		if p.Last == "interrupted" {
			msg = "interrupted"
		}
		r.Status, r.ErrorCode, r.Outcome = "failed", msg, msg
		return
	}
	switch j.Status {
	case "landed", "approved":
		r.Status, r.OK = "done", true
		r.Outcome = strings.TrimSpace(word + " · " + j.Status)
		r.Outcome = strings.TrimPrefix(r.Outcome, "· ")
		return
	case "rejected":
		r.Status, r.Outcome = "stopped", "rejected"
		return
	}
	switch {
	case word == "OK":
		r.Status, r.OK = "done", true
		r.Outcome = "OK · not landed"
		if r.Kind == "research" {
			r.Outcome = "OK"
		}
	case failWords[word]:
		r.Status, r.ErrorCode, r.Outcome = "failed", word, word
		if word == "BLOCKED" {
			q := ""
			for _, l := range last {
				if strings.HasPrefix(l, "question: ") {
					q = strings.TrimPrefix(l, "question: ")
					break
				}
			}
			r.Hint = strings.TrimSpace(q + "\nds reply " + r.JobID + " …")
		}
	case word != "":
		r.Status, r.ErrorCode, r.Outcome = "failed", word, word
	case exists(filepath.Join(jobsDir(), r.JobID, "last.txt.pending")):
		r.Status = "queued"
	case markerAlive(r.JobID):
		// claimed, the progress file not written yet
		r.Status = "running"
	default:
		r.Status, r.Outcome = "stopped", "no result"
	}
}

// closedJob is ds forget's rule (dslib/jobs.py _why_kept): not running, and landed, approved,
// rejected, or open with its private copy gone.
func closedJob(status string, j jobFile) bool {
	if status == "running" || status == "queued" {
		return false
	}
	switch j.Status {
	case "landed", "approved", "rejected":
		return true
	case "open":
		if j.Project == "" {
			return false
		}
		st, err := os.Stat(j.Dir)
		return j.Dir != "" && (err != nil || !st.IsDir())
	}
	return false
}

// markerAlive reports whether running/<id>.json names a live pid.
func markerAlive(id string) bool {
	var m struct {
		PID int `json:"pid"`
	}
	return readJSON(filepath.Join(StateDir, "running", id+".json"), &m) && pidAlive(m.PID)
}

// stripPreamble drops what ds puts before the task: the worker rules (message mode) and the
// WORKSPACE note, the same cut `ds list` makes.
func stripPreamble(task string) string {
	if i := strings.Index(task, "\n\n=====\n\n"); i >= 0 {
		task = task[i+len("\n\n=====\n\n"):]
	}
	if strings.HasPrefix(task, "WORKSPACE:") {
		if i := strings.Index(task, "\n\n"); i >= 0 {
			task = task[i+2:]
		}
	}
	return strings.TrimSpace(task)
}

// ReadIO returns a job's prompt (job task) and final output (report.txt, answer.txt for research),
// and whether either was found. While a round runs the output is empty: report.txt holds the
// previous round's text then.
func ReadIO(jobID string) (prompt, answer string, present bool) {
	if strings.ContainsAny(jobID, "/\\") || jobID == "" || jobID == "." || jobID == ".." {
		return "", "", false
	}
	var j jobFile
	if readJSON(filepath.Join(jobsDir(), jobID, "job.json"), &j) {
		prompt = stripPreamble(j.Task)
	}
	var p progressFile
	running := readJSON(filepath.Join(StateDir, "progress", jobID+".json"), &p) && p.State == "running" && pidAlive(p.PID)
	if !running {
		for _, name := range []string{"answer.txt", "report.txt"} {
			if b, err := os.ReadFile(filepath.Join(jobsDir(), jobID, name)); err == nil {
				answer = strings.TrimSpace(string(b))
				break
			}
		}
	}
	return prompt, answer, prompt != "" || answer != ""
}

// Activity is one job's activity.jsonl read into tool signatures and the latest usage.
type Activity struct {
	Sigs          []string
	InTok, OutTok int
	HasUsage      bool
}

type activityLine struct {
	Kind string `json:"kind"`
	Tool string `json:"tool"`
	Arg  string `json:"arg"`
	In   int    `json:"in"`
	Out  int    `json:"out"`
}

// ReadActivity reads jobs/<id>/activity.jsonl: every tool record as "tool(arg)" (or "tool"),
// and the latest usage. ok is false when the file is absent (a job from before T1).
// A torn last line (a write in progress) is skipped.
func ReadActivity(jobID string) (Activity, bool) {
	if strings.ContainsAny(jobID, "/\\") || jobID == "" || jobID == "." || jobID == ".." {
		return Activity{}, false
	}
	f, err := os.Open(filepath.Join(jobsDir(), jobID, "activity.jsonl"))
	if err != nil {
		return Activity{}, false
	}
	defer f.Close()
	var a Activity
	// Paths inside the job's own worktree show relative to it.
	work := filepath.Join(StateDir, "work", jobID) + "/"
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 64*1024), 1024*1024)
	for sc.Scan() {
		var l activityLine
		if json.Unmarshal(sc.Bytes(), &l) != nil {
			continue
		}
		switch l.Kind {
		case "tool":
			l.Arg = strings.ReplaceAll(l.Arg, work, "")
			if l.Arg != "" {
				a.Sigs = append(a.Sigs, l.Tool+"("+l.Arg+")")
			} else {
				a.Sigs = append(a.Sigs, l.Tool)
			}
		case "usage":
			a.InTok, a.OutTok, a.HasUsage = l.In, l.Out, true
		}
	}
	return a, true
}
