package dsjobs

import (
	"bytes"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"

	"ds-bridge/board/internal/pinned"
)

// The board's actions (SPEC 8). Stops send SIGTERM to a ds process after checking it is one;
// ds's own handler then interrupts the OpenCode session and marks the job interrupted. Deletes
// run `ds forget`, so the Python side owns every removal of job dirs, refs and worktrees.

// dsCommands are the ds subcommands whose process may be stopped.
var dsCommands = map[string]bool{"run": true, "revise": true, "reply": true, "research": true, "batch": true, "init": true}

// procRoot is /proc (a test seam).
var procRoot = "/proc"

// isDSProcess is cc-fleet's pid-reuse guard for ds: pid is alive, its argv is
// `python… <path>/ds <command> …`, and it started no later than notAfter (epoch seconds, +2 s
// slack) — a recycled pid belongs to a process started after the ds one wrote its files.
func isDSProcess(pid int, notAfter float64) bool {
	if pid <= 0 {
		return false
	}
	data, err := os.ReadFile(filepath.Join(procRoot, strconv.Itoa(pid), "cmdline"))
	if err != nil {
		return false
	}
	argv := strings.Split(strings.TrimRight(string(data), "\x00"), "\x00")
	if len(argv) < 3 || !strings.HasPrefix(filepath.Base(argv[0]), "python") ||
		filepath.Base(argv[1]) != "ds" || !dsCommands[argv[2]] {
		return false
	}
	if notAfter > 0 {
		if start, ok := procStart(pid); ok && start > notAfter+2 {
			return false
		}
	}
	return true
}

// procStart is the process start time (epoch seconds) from /proc/<pid>/stat and /proc/stat btime.
func procStart(pid int) (float64, bool) {
	stat, err := os.ReadFile(filepath.Join(procRoot, strconv.Itoa(pid), "stat"))
	if err != nil {
		return 0, false
	}
	// comm may hold spaces and parens: the fields start after the last ')'.
	i := bytes.LastIndexByte(stat, ')')
	if i < 0 {
		return 0, false
	}
	f := strings.Fields(string(stat[i+1:]))
	if len(f) < 20 {
		return 0, false
	}
	ticks, err := strconv.ParseFloat(f[19], 64) // field 22, starttime
	if err != nil {
		return 0, false
	}
	all, err := os.ReadFile(filepath.Join(procRoot, "stat"))
	if err != nil {
		return 0, false
	}
	for _, l := range strings.Split(string(all), "\n") {
		if strings.HasPrefix(l, "btime ") {
			bt, err := strconv.ParseFloat(strings.TrimSpace(l[6:]), 64)
			if err != nil {
				return 0, false
			}
			return bt + ticks/100, true // USER_HZ is 100 on Linux
		}
	}
	return 0, false
}

// sigterm sends SIGTERM (a test seam).
var sigterm = func(pid int) error { return syscall.Kill(pid, syscall.SIGTERM) }

// jobPID is the pid of a running job and the time its files were last written by it: the
// running progress file, else the running marker (written before the progress file exists).
func jobPID(jobID string) (int, float64) {
	var p progressFile
	if readJSON(filepath.Join(StateDir, "progress", jobID+".json"), &p) && p.State == "running" && p.PID > 0 {
		return p.PID, p.Updated
	}
	path := filepath.Join(StateDir, "running", jobID+".json")
	var m struct {
		PID int `json:"pid"`
	}
	if readJSON(path, &m) && m.PID > 0 {
		if st, err := os.Stat(path); err == nil {
			return m.PID, float64(st.ModTime().Unix())
		}
	}
	return 0, 0
}

// StopJob sends SIGTERM to a running job's ds process.
func StopJob(jobID string) error {
	pid, when := jobPID(jobID)
	if pid == 0 {
		return fmt.Errorf("job %s is not running", jobID)
	}
	if !isDSProcess(pid, when) {
		return fmt.Errorf("pid %d is not job %s's ds process any more", pid, jobID)
	}
	return sigterm(pid)
}

// StopRun sends SIGTERM to a running batch's ds process; it stops its jobs and starts no more.
func StopRun(runID string) (bool, error) {
	b, ok := readBatches()[runID]
	switch {
	case !ok:
		return false, fmt.Errorf("no batch %s", runID)
	case b.Ended != nil:
		return false, fmt.Errorf("batch %s has ended", runID)
	case b.PID == 0:
		return false, fmt.Errorf("batch %s recorded no pid (started before the board could stop batches)", runID)
	case !isDSProcess(b.PID, b.Started):
		return false, fmt.Errorf("batch %s is not running (pid %d is gone)", runID, b.PID)
	}
	return true, sigterm(b.PID)
}

// SendLeafCommand stops one job of a batch (op "stop"); the batch goes on with the others.
func SendLeafCommand(runID, op, leafID string) error {
	if op != "stop" {
		return fmt.Errorf("%s is not a ds action", op)
	}
	return StopJob(leafID)
}

// SendPhaseCommand stops the job of one batch group, found by its phase title.
func SendPhaseCommand(runID, op, phase string) error {
	if op != "stop" {
		return fmt.Errorf("%s is not a ds action", op)
	}
	b, ok := readBatches()[runID]
	if !ok {
		return fmt.Errorf("no batch %s", runID)
	}
	for i, t := range b.titles {
		if t == phase && i < len(b.Groups) && b.Groups[i].Job != nil {
			return StopJob(*b.Groups[i].Job)
		}
	}
	return fmt.Errorf("phase %q has no job running", phase)
}

// dsBin is the ds that started the board (`ds board` sets DS_BIN), else ds on PATH.
func dsBin() string {
	if b := os.Getenv("DS_BIN"); b != "" {
		return b
	}
	return "ds"
}

// forget runs `ds forget IDS…` and returns its output lines; err is set when ds kept any id or
// could not run.
func forget(ids ...string) ([]string, error) {
	out, err := exec.Command(dsBin(), append([]string{"forget"}, ids...)...).CombinedOutput()
	lines := strings.Split(strings.TrimSpace(string(out)), "\n")
	if len(lines) == 1 && lines[0] == "" {
		lines = nil
	}
	var ee *exec.ExitError
	if err != nil && !errors.As(err, &ee) {
		return nil, fmt.Errorf("ds forget: %v", err)
	}
	if err != nil {
		kept := []string{}
		for _, l := range lines {
			if !strings.HasPrefix(l, "forgot ") {
				kept = append(kept, l)
			}
		}
		if len(kept) == 0 {
			kept = []string{err.Error()}
		}
		return lines, errors.New(strings.Join(kept, "; "))
	}
	return lines, nil
}

// DeleteJob forgets one closed job; the line is ds forget's.
func DeleteJob(jobID string) (string, error) {
	lines, err := forget(jobID)
	if err != nil {
		return "", err
	}
	return strings.Join(lines, "; "), nil
}

// PurgeRun forgets a closed batch and its jobs.
func PurgeRun(runID string) (string, error) { return DeleteJob(runID) }

// ClearIDs is what `c` clears in a session: its closed, unpinned standalone jobs and batches.
func ClearIDs(jobs []Result, runs []WorkflowRun, sessionID string, pins pinned.Set) []string {
	var ids []string
	for _, r := range runs {
		if r.SessionID == sessionID && r.Closed && !pins.Has(pinned.Run, r.RunID) {
			ids = append(ids, r.RunID)
		}
	}
	for _, j := range jobs {
		if j.RunID == "" && j.LeadSessionID == sessionID && j.Closed && !pins.Has(pinned.Job, j.JobID) {
			ids = append(ids, j.JobID)
		}
	}
	return ids
}

// ClearFinished forgets the session's closed, unpinned jobs and batches, read fresh, in one ds
// forget call; returns how many went.
func ClearFinished(sessionID string, pins pinned.Set) (int, error) {
	jobs, err := ListJobs()
	if err != nil {
		return 0, err
	}
	runs, err := ListRuns(jobs)
	if err != nil {
		return 0, err
	}
	ids := ClearIDs(jobs, runs, sessionID, pins)
	if len(ids) == 0 {
		return 0, nil
	}
	lines, err := forget(ids...)
	n := 0
	for _, l := range lines {
		if strings.HasPrefix(l, "forgot ") {
			n++
		}
	}
	return n, err
}
