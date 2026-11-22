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
	single := 0
	for _, j := range jobs {
		if j.RunID == "" {
			single++
		}
	}
	if single != len(cases) {
		t.Errorf("got %d single jobs, want %d", single, len(cases))
	}
}

func TestListJobsFields(t *testing.T) {
	fixture(t)
	jobs := byID(t)
	js, _ := ListJobs()
	if js[0].JobID != "1122-102200" || js[len(js)-1].JobID != "1122-100000" {
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

func runByID(t *testing.T) (map[string]WorkflowRun, map[string]Result) {
	t.Helper()
	jobs, err := ListJobs()
	if err != nil {
		t.Fatal(err)
	}
	runs, err := ListRuns(jobs)
	if err != nil {
		t.Fatal(err)
	}
	m := map[string]WorkflowRun{}
	for _, r := range runs {
		m[r.RunID] = r
	}
	j := map[string]Result{}
	for _, x := range jobs {
		j[x.JobID] = x
	}
	return m, j
}

func TestBatchChildren(t *testing.T) {
	fixture(t)
	_, jobs := runByID(t)
	want := map[string]string{"1122-101000": "Fix add", "1122-101100": "Fix mul",
		"1122-102100": "Fix add", "1122-102200": CheckPhase}
	for id, phase := range want {
		j := jobs[id]
		if j.Phase != phase || j.RunID == "" || j.Label != id {
			t.Errorf("%s: run %q phase %q label %q, want phase %q", id, j.RunID, j.Phase, j.Label, phase)
		}
	}
}

func TestBatchRuns(t *testing.T) {
	dir := fixture(t)
	old := time.Now().Add(-time.Hour)
	os.Chtimes(filepath.Join(dir, "jobs/b1122-103000/batch.json"), old, old)
	runs, _ := runByID(t)
	if len(runs) != 3 {
		t.Fatalf("got %d runs", len(runs))
	}
	b := runs["b1122-100950"]
	if b.Status != "failed" || b.Name != "Split calc" || b.SessionID != "sess-b" || b.Cwd != "/home/u/proj" || b.UpdatedAt == "" {
		t.Errorf("batch 1: %+v", b)
	}
	type ph struct{ title, status, detail string }
	got := []ph{}
	for _, p := range b.Phases {
		got = append(got, ph{p.Title, p.Status, p.Detail})
	}
	want := []ph{{"Fix add", "done", ""}, {"Fix mul", "failed", "FAIL"},
		{"Use both", "stopped", "skipped: needs step 2 (FAIL)"},
		{CheckPhase, "stopped", "not run (not every step landed)"}}
	if len(got) != len(want) {
		t.Fatalf("phases %+v", got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("phase %d: %+v, want %+v", i, got[i], want[i])
		}
	}
	f := runs["b1122-102000"]
	last := f.Phases[len(f.Phases)-1]
	if f.Status != "done" || last.Title != CheckPhase || last.Status != "done" ||
		last.Detail != "pass (after fix job) · `python3 test_calc.py`" {
		t.Errorf("batch 2: %+v", f)
	}
	k := runs["b1122-103000"]
	if k.Status != "failed" || k.Description != "interrupted" {
		t.Errorf("killed batch: %+v", k)
	}
	// The same batch, freshly written (fixture mtime is now), still counts as running.
	os.Chtimes(filepath.Join(dir, "jobs/b1122-103000/batch.json"), time.Now(), time.Now())
	if runs, _ = runByID(t); runs["b1122-103000"].Status != "running" {
		t.Errorf("fresh batch: %+v", runs["b1122-103000"])
	}
}

func TestPlanStepTitles(t *testing.T) {
	got := planStepTitles("testdata/state/plan.md")
	if got["1"] != "Fix add" || got["2"] != "Fix mul" || got["3"] != "Use both" {
		t.Errorf("%v", got)
	}
}

// TestListJobsMalformed: a job.json or batch.json that exists but doesn't parse is skipped and
// named in the error (the board's dim jobs line); the other jobs still load. A job dir with no
// job.json yet (being created) is skipped silently. A missing state dir is no error.
func TestListJobsMalformed(t *testing.T) {
	dst := fixture(t)
	want, _ := ListJobs()
	for name, body := range map[string]string{
		"1122-999990/job.json":    "{not json",
		"1122-999991/job.json":    `{"id": "1122-999991", "round": "two"}`,
		"b1122-999992/batch.json": "[]x",
	} {
		p := filepath.Join(dst, "jobs", name)
		os.MkdirAll(filepath.Dir(p), 0o755)
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	os.MkdirAll(filepath.Join(dst, "jobs", "1122-999993"), 0o755)
	js, err := ListJobs()
	if len(js) != len(want) {
		t.Errorf("got %d jobs, want %d (bad ones skipped, rest kept)", len(js), len(want))
	}
	wantErr := "skipped unreadable 1122-999990/job.json, 1122-999991/job.json, b1122-999992/batch.json"
	if err == nil || err.Error() != wantErr {
		t.Errorf("err = %v, want %q", err, wantErr)
	}

	StateDir = filepath.Join(dst, "missing")
	if js, err := ListJobs(); err != nil || len(js) != 0 {
		t.Errorf("missing state dir: %d jobs, err %v; want none, nil", len(js), err)
	}
	if rs, err := ListRuns(nil); err != nil || len(rs) != 0 {
		t.Errorf("missing state dir: %d runs, err %v; want none, nil", len(rs), err)
	}
}
