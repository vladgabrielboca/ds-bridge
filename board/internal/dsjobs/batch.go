package dsjobs

import (
	"bufio"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

// This file reads ds batches (jobs/b<id>/batch.json, SPEC 6) as cc-fleet workflow runs:
// batch = run, group = phase, the group's job = agent. The integration check is a last
// pseudo-phase CheckPhase whose agent is the fix job, when one ran.

// CheckPhase is the title of the integration check's pseudo-phase.
const CheckPhase = "integration check"

// batchStaleAfter: a batch with no end, no running child and no write for this long is taken as
// killed (ds records no batch pid). Group transitions rewrite batch.json at once, so a live batch
// with nothing running is only ever seconds old.
const batchStaleAfter = 30 * time.Second

type batchGroup struct {
	Label  string   `json:"label"`
	Steps  []string `json:"steps"`
	Needs  []int    `json:"needs"`
	Job    *string  `json:"job"`
	Status string   `json:"status"`
}

type batchFile struct {
	ID          string       `json:"id"`
	Title       string       `json:"title"`
	Plan        string       `json:"plan"`
	Project     string       `json:"project"`
	Owner       string       `json:"owner"`
	Started     float64      `json:"started"`
	Groups      []batchGroup `json:"groups"`
	Check       *string      `json:"check"`
	Ended       *float64     `json:"ended"`
	Integration string       `json:"integration"`
	FixJob      string       `json:"fix_job"`

	mtime  time.Time
	titles []string // phase title per group
}

// readBatches reads every jobs/b*/batch.json, keyed by batch id.
func readBatches() map[string]*batchFile {
	out := map[string]*batchFile{}
	ents, err := os.ReadDir(jobsDir())
	if err != nil {
		return out
	}
	for _, e := range ents {
		if !e.IsDir() || !strings.HasPrefix(e.Name(), "b") {
			continue
		}
		p := filepath.Join(jobsDir(), e.Name(), "batch.json")
		var b batchFile
		if !readJSON(p, &b) {
			continue
		}
		if b.ID == "" {
			b.ID = e.Name()
		}
		if st, err := os.Stat(p); err == nil {
			b.mtime = st.ModTime()
		}
		steps := planStepTitles(b.Plan)
		for i, g := range b.Groups {
			// The board's rail already shows the row number; repeat the label only when it differs.
			t := g.Label
			if len(g.Steps) > 0 && steps[g.Steps[0]] != "" {
				if g.Label == strconv.Itoa(i+1) {
					t = steps[g.Steps[0]]
				} else {
					t += " " + steps[g.Steps[0]]
				}
			}
			b.titles = append(b.titles, t)
		}
		out[b.ID] = &b
	}
	return out
}

// planStepTitles reads "## <id> <title>" lines of the plan file (dslib/batch.py parse_plan; a
// "step"/"step-"/"s" prefix before a digit is dropped from the id). Best effort: a moved plan gives none.
func planStepTitles(path string) map[string]string {
	out := map[string]string{}
	f, err := os.Open(path)
	if err != nil {
		return out
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := sc.Text()
		if !strings.HasPrefix(line, "##") || strings.HasPrefix(line, "###") {
			continue
		}
		rest := strings.TrimLeft(line[2:], " \t")
		if rest == "" || rest == line[2:] {
			continue
		}
		id, title, _ := strings.Cut(rest, " ")
		for _, pre := range []string{"step-", "step ", "step", "s"} {
			if p, ok := strings.CutPrefix(id, pre); ok && p != "" && p[0] >= '0' && p[0] <= '9' {
				id = p
				break
			}
		}
		if id == "step" || id == "Step" {
			// "## step 1 title"
			id, title, _ = strings.Cut(strings.TrimSpace(title), " ")
		}
		out[id] = strings.TrimSpace(title)
	}
	return out
}

// phaseOf names the phase a batch child belongs to: its group's title, the check phase for the
// fix job, else the group whose job it is.
func (b *batchFile) phaseOf(jobID string, group *int) string {
	if group != nil && *group >= 0 && *group < len(b.titles) {
		return b.titles[*group]
	}
	if b.FixJob == jobID {
		return CheckPhase
	}
	for i, g := range b.Groups {
		if g.Job != nil && *g.Job == jobID {
			return b.titles[i]
		}
	}
	if group == nil {
		// no group: ds only starts the integration fix job that way
		return CheckPhase
	}
	return "?"
}

// ListRuns returns every ds batch as a run, newest first. jobs is the ListJobs result of the same
// refresh (the batch children's status decides the run's).
func ListRuns(jobs []Result) ([]WorkflowRun, error) {
	batches := readBatches()
	kids := map[string][]Result{}
	for _, j := range jobs {
		if j.RunID != "" {
			kids[j.RunID] = append(kids[j.RunID], j)
		}
	}
	var out []WorkflowRun
	for _, b := range batches {
		out = append(out, b.run(kids[b.ID]))
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].StartedAt > out[j].StartedAt })
	return out, nil
}

// run builds the run from the batch file and its child jobs (SPEC 5, batches).
func (b *batchFile) run(kids []Result) WorkflowRun {
	byID := map[string]Result{}
	anyRunning := false
	for _, k := range kids {
		byID[k.JobID] = k
		if k.Status == "running" || k.Status == "queued" {
			anyRunning = true
		}
	}
	r := WorkflowRun{RunID: b.ID, Name: b.Title, StartedAt: epochRFC3339(b.Started),
		SessionID: b.Owner, Cwd: b.Project}
	bad := false
	for i, g := range b.Groups {
		p := RunPhase{Title: b.titles[i], Status: "queued"}
		job := ""
		if g.Job != nil {
			job = *g.Job
		}
		switch {
		case g.Status == "SKIPPED":
			p.Status, p.Detail = "stopped", "skipped: "+b.skipReason(i, byID)
			bad = true
		case g.Status == "running":
			p.Status = "running"
		case g.Status == "OK":
			p.Status = "done"
		case g.Status != "queued" && g.Status != "":
			p.Status, p.Detail = "failed", g.Status
			bad = true
		}
		if k, ok := byID[job]; ok && k.Status == "failed" {
			p.Status = "failed"
			bad = true
		}
		if len(g.Needs) > 0 {
			var ls []string
			for _, n := range g.Needs {
				if n >= 0 && n < len(b.Groups) {
					ls = append(ls, b.Groups[n].Label)
				}
			}
			if p.Detail == "" && len(ls) > 0 {
				p.Detail = "needs step " + strings.Join(ls, ", ")
			}
		}
		r.Phases = append(r.Phases, p)
	}
	if b.Check != nil && *b.Check != "" {
		p := RunPhase{Title: CheckPhase, Status: "queued", Detail: "`" + *b.Check + "`"}
		switch {
		case b.Integration == "pass" || b.Integration == "pass (after fix job)":
			p.Status, p.Detail = "done", b.Integration+" · `"+*b.Check+"`"
		case b.Integration == "fail":
			p.Status, p.Detail = "failed", "fail · `"+*b.Check+"`"
			bad = true
		case b.Integration == "none" && b.Ended != nil:
			p.Status, p.Detail = "stopped", "not run (not every step landed)"
		}
		r.Phases = append(r.Phases, p)
	}
	switch {
	case b.Ended != nil:
		r.UpdatedAt = epochRFC3339(*b.Ended)
		r.Status = "done"
		if bad {
			r.Status = "failed"
		}
	case anyRunning || time.Since(b.mtime) < batchStaleAfter:
		r.Status = "running"
	default:
		// No end written and nothing running: the batch process was killed.
		r.Status = "failed"
		r.Description = "interrupted"
		r.UpdatedAt = b.mtime.Local().Format(time.RFC3339)
	}
	return r
}

// skipReason is dslib/batch.py's SKIPPED line: the first needed group that did not pass and land.
func (b *batchFile) skipReason(i int, kids map[string]Result) string {
	for _, n := range b.Groups[i].Needs {
		if n < 0 || n >= len(b.Groups) {
			continue
		}
		dep := b.Groups[n]
		why := dep.Status
		if why == "OK" {
			if dep.Job != nil {
				if k, ok := kids[*dep.Job]; ok && k.Status == "done" && k.Outcome != "OK · not landed" {
					continue
				}
			}
			why = "passed but did not land"
		}
		return "needs step " + dep.Label + " (" + why + ")"
	}
	return "a needed step did not land"
}

// groupIndex parses job.json batch_group.
func groupIndex(v any) *int {
	switch x := v.(type) {
	case float64:
		i := int(x)
		return &i
	case string:
		if i, err := strconv.Atoi(x); err == nil {
			return &i
		}
	}
	return nil
}
