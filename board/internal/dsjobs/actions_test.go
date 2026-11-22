package dsjobs

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"ds-bridge/board/internal/pinned"
)

// fakeProc makes a /proc with one process: pid, argv, started at epoch start (btime 1795000000).
func fakeProc(t *testing.T, pid int, argv []string, start float64) {
	t.Helper()
	root := t.TempDir()
	d := filepath.Join(root, fmt.Sprint(pid))
	os.MkdirAll(d, 0o755)
	os.WriteFile(filepath.Join(d, "cmdline"), []byte(strings.Join(argv, "\x00")+"\x00"), 0o644)
	ticks := int((start - 1795000000) * 100)
	stat := fmt.Sprintf("%d (py) thon) S%s %d 0 0\n", pid, strings.Repeat(" 0", 18), ticks)
	os.WriteFile(filepath.Join(d, "stat"), []byte(stat), 0o644)
	os.WriteFile(filepath.Join(root, "stat"), []byte("cpu 1 2 3\nbtime 1795000000\n"), 0o644)
	old := procRoot
	procRoot = root
	t.Cleanup(func() { procRoot = old })
}

// catchTerm replaces the SIGTERM sender; the returned slice collects the pids it got.
func catchTerm(t *testing.T) *[]int {
	t.Helper()
	got := &[]int{}
	old := sigterm
	sigterm = func(pid int) error { *got = append(*got, pid); return nil }
	t.Cleanup(func() { sigterm = old })
	return got
}

func TestIsDSProcess(t *testing.T) {
	fakeProc(t, 4242, []string{"/usr/bin/python3", "/home/u/.local/bin/ds", "run", "-C", "/p", "-"}, 1795300000)
	if !isDSProcess(4242, 1795300010) {
		t.Error("a ds run process started before its files should match")
	}
	if isDSProcess(4242, 1795299000) {
		t.Error("a process started after the files were written is a recycled pid")
	}
	if isDSProcess(4243, 0) {
		t.Error("a missing pid matched")
	}
	fakeProc(t, 4242, []string{"/usr/bin/python3", "/home/u/.local/bin/ds", "approve", "1"}, 1795300000)
	if isDSProcess(4242, 0) {
		t.Error("ds approve is not a stoppable process")
	}
	fakeProc(t, 4242, []string{"bash", "-c", "ds run x"}, 1795300000)
	if isDSProcess(4242, 0) {
		t.Error("a shell is not ds")
	}
}

func TestStopJob(t *testing.T) {
	dir := fixture(t)
	fakeProc(t, 4242, []string{"python3", "/x/ds", "run", "-"}, 1795300000)
	got := catchTerm(t)
	b := `{"state":"running","pid":4242,"started":1795300000,"updated":1795300010}`
	os.WriteFile(filepath.Join(dir, "progress", "1122-100300.json"), []byte(b), 0o644)
	if err := StopJob("1122-100300"); err != nil || len(*got) != 1 || (*got)[0] != 4242 {
		t.Fatalf("stop: err=%v sent=%v", err, *got)
	}
	if err := StopJob("1122-100200"); err == nil || !strings.Contains(err.Error(), "not running") {
		t.Errorf("a landed job: %v", err)
	}
	// the progress file names a pid that now belongs to another program
	fakeProc(t, 4242, []string{"/usr/bin/vim"}, 1795300000)
	if err := StopJob("1122-100300"); err == nil || len(*got) != 1 {
		t.Errorf("a recycled pid got SIGTERM: err=%v sent=%v", err, *got)
	}
}

func TestStopRun(t *testing.T) {
	dir := fixture(t)
	fakeProc(t, 4242, []string{"python3", "/x/ds", "batch", "plan.md"}, 1795300000)
	got := catchTerm(t)
	p := filepath.Join(dir, "jobs/b1122-103000/batch.json")
	b, _ := os.ReadFile(p)
	b = []byte(strings.Replace(string(b), "{", `{"pid": 4242, `, 1))
	os.WriteFile(p, b, 0o644)
	if runs, _ := runByID(t); runs["b1122-103000"].Status != "running" {
		t.Errorf("a batch whose pid is a live ds batch is running: %+v", runs["b1122-103000"])
	}
	if ok, err := StopRun("b1122-103000"); !ok || err != nil || len(*got) != 1 {
		t.Fatalf("stop: %v %v %v", ok, err, *got)
	}
	if _, err := StopRun("b1122-100950"); err == nil || !strings.Contains(err.Error(), "ended") {
		t.Errorf("an ended batch: %v", err)
	}
	// SendPhaseCommand finds the group's job by its phase title.
	*got = nil
	os.WriteFile(filepath.Join(dir, "progress", "1122-101000.json"),
		[]byte(`{"state":"running","pid":4242,"updated":1795300010}`), 0o644)
	runs, _ := runByID(t)
	title := runs["b1122-100950"].Phases[0].Title
	if err := SendPhaseCommand("b1122-100950", "stop", title); err != nil || len(*got) != 1 {
		t.Errorf("phase stop: err=%v sent=%v", err, *got)
	}
}

func TestStoppedBatch(t *testing.T) {
	dir := fixture(t)
	p := filepath.Join(dir, "jobs/b1122-103000/batch.json")
	b, _ := os.ReadFile(p)
	s := strings.Replace(string(b), "{", `{"pid": 1, "stopped": true, `, 1)
	s = strings.ReplaceAll(s, `"ended": null`, `"ended": 1795300100`)
	s = strings.ReplaceAll(s, `"status": "running"`, `"status": "STOPPED"`)
	os.WriteFile(p, []byte(s), 0o644)
	runs, _ := runByID(t)
	r := runs["b1122-103000"]
	if r.Status != "failed" || r.Description != "stopped" {
		t.Errorf("stopped batch: %+v", r)
	}
	found := false
	for _, ph := range r.Phases {
		if ph.Detail == "stopped from the board" && ph.Status == "stopped" {
			found = true
		}
	}
	if !found {
		t.Errorf("no STOPPED phase: %+v (batch.json %s)", r.Phases, s)
	}
}

func TestClosed(t *testing.T) {
	dir := fixture(t)
	jobs := byID(t)
	for id, want := range map[string]bool{
		"1122-100000": false, // running
		"1122-100800": false, // queued
		"1122-100200": true,  // landed
		"1122-100500": true,  // approved
		"1122-100700": true,  // rejected
		"1122-100300": true,  // open, but its copy dir is gone
	} {
		if jobs[id].Closed != want {
			t.Errorf("%s Closed=%v, want %v", id, jobs[id].Closed, want)
		}
	}
	// open with its copy still there: not closed
	p := filepath.Join(dir, "jobs/1122-100300/job.json")
	b, _ := os.ReadFile(p)
	os.WriteFile(p, []byte(strings.Replace(string(b), "/work/1122-100300", dir, 1)), 0o644)
	if byID(t)["1122-100300"].Closed {
		t.Error("an open job whose copy exists counts as closed")
	}
}

func TestClearIDs(t *testing.T) {
	old := pinned.StateDir
	pinned.StateDir = t.TempDir()
	t.Cleanup(func() { pinned.StateDir = old })
	pinned.Pin(pinned.Job, "j3")
	pins, _ := pinned.Snapshot()
	jobs := []Result{
		{JobID: "j1", LeadSessionID: "s", Closed: true},
		{JobID: "j2", LeadSessionID: "s", Closed: false},
		{JobID: "j3", LeadSessionID: "s", Closed: true},             // pinned
		{JobID: "j4", LeadSessionID: "other", Closed: true},         // another session
		{JobID: "j5", LeadSessionID: "s", Closed: true, RunID: "b"}, // goes with its batch
	}
	runs := []WorkflowRun{{RunID: "b", SessionID: "s", Closed: true}, {RunID: "b2", SessionID: "s"}}
	got := strings.Join(ClearIDs(jobs, runs, "s", pins), " ")
	if got != "b j1" {
		t.Errorf("ClearIDs = %q, want \"b j1\"", got)
	}
}
