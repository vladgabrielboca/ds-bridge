package dsjobs

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// fixture copies testdata/state into a temp dir, writes the two running progress files (one with
// this test's own pid, one with a pid that has exited) and points StateDir at it.
func fixture(t *testing.T) string {
	t.Helper()
	dst := t.TempDir()
	if err := os.CopyFS(dst, os.DirFS("testdata/state")); err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command("true")
	if err := cmd.Run(); err != nil {
		t.Fatal(err)
	}
	dead := cmd.Process.Pid
	write := func(id string, pid int) {
		b, _ := json.Marshal(map[string]any{"state": "running", "job": id, "round": 1, "pid": pid,
			"started": 1795300000, "updated": 1795300010, "steps": 2, "tools": 2, "last": "edit calc.py"})
		if err := os.WriteFile(filepath.Join(dst, "progress", id+".json"), b, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	write("1122-100000", os.Getpid())
	write("1122-100100", dead)
	old := StateDir
	StateDir = dst
	t.Cleanup(func() { StateDir = old })
	return dst
}

func byID(t *testing.T) map[string]Result {
	t.Helper()
	js, err := ListJobs()
	if err != nil {
		t.Fatal(err)
	}
	m := map[string]Result{}
	for _, j := range js {
		m[j.JobID] = j
	}
	return m
}

func TestListJobsStatus(t *testing.T) {
	fixture(t)
	jobs := byID(t)
	cases := []struct {
		id, status, errCode, outcome string
	}{
		{"1122-100000", "running", "", ""},
		{"1122-100100", "failed", "interrupted", "interrupted"},
		{"1122-100200", "done", "", "OK · landed"},
		{"1122-100300", "done", "", "OK · not landed"},
		{"1122-100400", "failed", "BLOCKED", "BLOCKED"},
		{"1122-100500", "done", "", "OK · approved"},
		{"1122-100600", "done", "", "OK · landed"},
		{"1122-100700", "stopped", "", "rejected"},
		{"1122-100800", "queued", "", ""},
	}
	for _, c := range cases {
		j, ok := jobs[c.id]
		if !ok {
			t.Errorf("%s missing", c.id)
			continue
		}
		if j.Status != c.status || j.ErrorCode != c.errCode || j.Outcome != c.outcome {
			t.Errorf("%s: got status=%q code=%q outcome=%q, want %q %q %q",
				c.id, j.Status, j.ErrorCode, j.Outcome, c.status, c.errCode, c.outcome)
		}
	}
	if len(jobs) != len(cases) {
		t.Errorf("got %d jobs, want %d (batch child and b* dir left out)", len(jobs), len(cases))
	}
}

func TestListJobsFields(t *testing.T) {
	fixture(t)
	jobs := byID(t)
	js, _ := ListJobs()
	if js[0].JobID != "1122-100800" || js[len(js)-1].JobID != "1122-100000" {
		t.Errorf("not newest first: %s … %s", js[0].JobID, js[len(js)-1].JobID)
	}
	l := jobs["1122-100200"]
	if l.Model != "deepseek-v4.1-flash" || l.PromptProfile != "worker" || l.Kind != "run" ||
		l.LeadSessionID != "sess-a" || l.Project != "/home/u/proj" || l.NumTurns != 4 ||
		l.StopReason != "OK" || l.CostUSD != 0.0123 || l.DurationMs != 15000 || l.Attempt != 1 ||
		!sameTime(l.StartedAt, "2026-11-21T22:30:00Z") {
		t.Errorf("landed job fields: %+v", l)
	}
	if l.Usage == nil || l.Usage.InputTokens != 11495 || l.Usage.OutputTokens != 235 || l.Usage.CacheReadInputTokens != 33152 {
		t.Errorf("landed job tokens: %+v", l.Usage)
	}
	if r := jobs["1122-100000"]; r.PID != os.Getpid() || r.DurationMs != 0 {
		t.Errorf("running job: pid %d dur %d", r.PID, r.DurationMs)
	}
	if jobs["1122-100300"].Attempt != 2 {
		t.Errorf("round 2 job: attempt %d", jobs["1122-100300"].Attempt)
	}
	if jobs["1122-100500"].Kind != "research" || jobs["1122-100500"].PromptProfile != "researcher" {
		t.Errorf("research job: %+v", jobs["1122-100500"])
	}
	if p := jobs["1122-100600"]; p.LeadSessionID != "" || p.StopReason != "OK" || p.Usage != nil {
		t.Errorf("pre-T1 job: %+v", p)
	}
	b := jobs["1122-100400"]
	if !strings.Contains(b.Hint, "A) keep the API or B) break it?") || !strings.Contains(b.Hint, "ds reply 1122-100400") {
		t.Errorf("BLOCKED hint: %q", b.Hint)
	}
}

func TestReadIO(t *testing.T) {
	fixture(t)
	p, a, ok := ReadIO("1122-100200")
	if !ok || p != "TASK: task of 1122-100200\nCHECK: true" || a != "STATUS: DONE\nSUMMARY: fixed add()" {
		t.Errorf("landed io: %v %q %q", ok, p, a)
	}
	_, a, _ = ReadIO("1122-100500")
	if a != "ANSWER: dslib/core.py cfg()" {
		t.Errorf("research answer should come from answer.txt: %q", a)
	}
	p, a, ok = ReadIO("1122-100000")
	if !ok || a != "" || !strings.HasPrefix(p, "TASK:") {
		t.Errorf("running job: the old round's report must not show: %v %q %q", ok, p, a)
	}
	if _, _, ok := ReadIO("../etc"); ok {
		t.Error("a path id must not read")
	}
}

func TestReadActivity(t *testing.T) {
	fixture(t)
	a, ok := ReadActivity("1122-100000")
	want := []string{"shell(ls -la)", "edit(calc.py)", "todowrite"}
	if !ok || strings.Join(a.Sigs, "|") != strings.Join(want, "|") {
		t.Errorf("sigs %v ok %v", a.Sigs, ok)
	}
	if !a.HasUsage || a.InTok != 12000 || a.OutTok != 180 {
		t.Errorf("latest usage: %+v", a)
	}
	if _, ok := ReadActivity("1122-100600"); ok {
		t.Error("a pre-T1 job has no activity file")
	}
}

// The board never writes the state dir.
func TestReadOnly(t *testing.T) {
	dir := fixture(t)
	before := snapshot(t, dir)
	ListJobs()
	for _, id := range []string{"1122-100000", "1122-100200", "1122-100600"} {
		ReadIO(id)
		ReadActivity(id)
	}
	if after := snapshot(t, dir); after != before {
		t.Errorf("state dir changed:\n%s\n---\n%s", before, after)
	}
}

func snapshot(t *testing.T, dir string) string {
	var b strings.Builder
	filepath.Walk(dir, func(p string, info os.FileInfo, err error) error {
		if err == nil {
			b.WriteString(p + " " + info.ModTime().String() + "\n")
		}
		return nil
	})
	return b.String()
}

func sameTime(a, b string) bool {
	ta, e1 := time.Parse(time.RFC3339, a)
	tb, e2 := time.Parse(time.RFC3339, b)
	return e1 == nil && e2 == nil && ta.Equal(tb)
}
