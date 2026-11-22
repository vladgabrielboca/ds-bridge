// Derived from cc-fleet internal/tui/view.go (Apache-2.0); modified.

package tui

import (
	"fmt"
	"os"
	"sort"
	"strings"
	"time"

	"github.com/charmbracelet/lipgloss"
	"github.com/charmbracelet/x/ansi"

	"ds-bridge/board/internal/dsjobs"
	"ds-bridge/board/internal/pinned"
	"ds-bridge/board/internal/sessiontitle"
)

// errColor and okColor are shared beyond their styles: the confirm-modal and
// codex-auth borders re-tint on the outcome.
var (
	errColor = lipgloss.AdaptiveColor{Light: "160", Dark: "203"}
	okColor  = lipgloss.AdaptiveColor{Light: "28", Dark: "78"}
)

// Shared lipgloss styles. Colors are AdaptiveColor pairs of ANSI 256 indices —
// Dark is the dark-background palette, Light a darker counterpart that stays
// legible on white terminals — so they degrade gracefully on limited terminals.
var (
	titleStyle      = lipgloss.NewStyle().Bold(true).Foreground(lipgloss.AdaptiveColor{Light: "32", Dark: "39"})
	cursorStyle     = lipgloss.NewStyle().Foreground(lipgloss.AdaptiveColor{Light: "198", Dark: "212"})
	selectedStyle   = lipgloss.NewStyle().Bold(true).Foreground(lipgloss.AdaptiveColor{Light: "198", Dark: "212"})
	faintStyle      = lipgloss.NewStyle().Foreground(lipgloss.AdaptiveColor{Light: "247", Dark: "241"})
	contentStyle    = lipgloss.NewStyle().Foreground(lipgloss.AdaptiveColor{Light: "242", Dark: "245"}) // board body text — softer than the full-contrast tone, above faint
	liveStyle       = lipgloss.NewStyle().Foreground(lipgloss.AdaptiveColor{Light: "237", Dark: "252"}) // active (done/running) labels + the answer body — strong, below the frame
	modalBodyStyle  = lipgloss.NewStyle().Foreground(lipgloss.AdaptiveColor{Light: "233", Dark: "255"}) // centered-modal body text — full contrast; a modal floats over the board and must outshine it
	borderStyle     = lipgloss.NewStyle().Foreground(lipgloss.AdaptiveColor{Light: "233", Dark: "255"}) // master-detail box frame — the strongest line (full contrast, like native)
	sessionHdrStyle = lipgloss.NewStyle().Bold(true).Foreground(lipgloss.AdaptiveColor{Light: "32", Dark: "39"})
	codexHdrStyle   = lipgloss.NewStyle().Bold(true).Foreground(lipgloss.AdaptiveColor{Light: "92", Dark: "141"}) // violet — codex launcher marker (the ◇, vs the claude blue ◆)
	errStyle        = lipgloss.NewStyle().Foreground(errColor)
	okStyle         = lipgloss.NewStyle().Foreground(okColor)
	noteStyle       = lipgloss.NewStyle().Foreground(lipgloss.AdaptiveColor{Light: "94", Dark: "214"})  // contextual Note hints — a warm tone above the gray body
	pinStyle        = lipgloss.NewStyle().Foreground(lipgloss.AdaptiveColor{Light: "136", Dark: "220"}) // the kept/pinned ★ — gold, trailing the row
)

// footer renders a dim key-hint line.
func footer(s string) string { return faintStyle.Render(s) }

// View satisfies tea.Model.
func (m Model) View() string {
	if m.quitting {
		return ""
	}
	s := m.viewSpawn()
	if m.confirm != nil {
		s = overlayCenter(s, m.renderConfirmBox(), m.width)
	}
	return s
}

// railSeparator is the divider above "+ Add provider…": dashes spanning the rail
// to one column shy of each edge, so it reads a touch longer than the widest
// header/label with a small margin on each side.
func railSeparator(leftW int) string {
	n := leftW - 2
	if n < 4 {
		n = 4
	}
	return " " + faintStyle.Render(strings.Repeat("─", n))
}

// labeledDivider is a dim rail separator "── <label> ──" filled to width, marking a
// launcher-group boundary in a session list. It mirrors mateOutputSection's stamp rule (the
// fill is measured before styling so faintStyle's ANSI does not skew it).
func labeledDivider(label string, width int) string {
	stamp := "── " + label + " "
	if fill := width - 1 - lipgloss.Width(stamp); fill > 0 {
		stamp += strings.Repeat("─", fill)
	}
	return " " + faintStyle.Render(stamp)
}

func codexDivider(width int) string     { return labeledDivider("codex", width) }
func noSessionDivider(width int) string { return labeledDivider("no session", width) }

// launcherDividerRows splices the codex / no-session dividers into the rendered session rows at
// the launcher-group boundaries (sessions are rank-sorted claude → codex → "(no session)") and
// returns the render rows + the window cursor shifted past any divider inserted at or before the
// data cursor. Dividers are render-only — the data cursor still indexes sessions — and appear
// only when codex is present, so a codex-free project renders byte-identically.
func launcherDividerRows(rows []string, sessions []asSession, cursor, width int) ([]string, int) {
	if firstCodexSession(sessions) < 0 {
		return rows, cursor
	}
	dividers := map[int]string{}
	if ci := firstCodexSession(sessions); ci > 0 {
		dividers[ci] = codexDivider(width)
	}
	if ni := firstNoSessionIndex(sessions); ni > 0 {
		dividers[ni] = noSessionDivider(width)
	}
	if len(dividers) == 0 {
		return rows, cursor
	}
	out := make([]string, 0, len(rows)+len(dividers))
	renderCursor := cursor
	for i := range rows {
		if d, ok := dividers[i]; ok {
			out = append(out, d)
			if cursor >= i {
				renderCursor++
			}
		}
		out = append(out, rows[i])
	}
	return out, renderCursor
}

// viewSpawn renders the Agents Board: a project-first master-detail. asMode re-roots
// the levels under one shared header+rule chrome — projects → sessions → the session's Dynamic
// Workflows + Agent Teams + Subagents boxes → entity detail, plus the run drill below the
// boxes. Rows and cards show only field-source-safe data — canonical `ps --check`
// health vocabulary, CleanTitle-scrubbed names/models/ids, integer token counts — NEVER a
// job's answer text (Result.Result), an ErrorMsg body, or raw pane capture; the detail
// card's Output reads the focused job's .answer side file alone.
func (m Model) viewSpawn() string {
	var b strings.Builder
	switch {
	case m.loading:
		b.WriteString(m.boardFallback("discovering…"))
	case m.spawnErr != nil:
		b.WriteString(m.boardFallback(errStyle.Render("error: " + m.spawnErr.Error())))
	case m.asMode == asModeProjects:
		b.WriteString(m.viewAsProjects())
	case m.asMode == asModeSessions:
		b.WriteString(m.viewAsSessions())
	case m.asMode == asModeEntity:
		b.WriteString(m.viewAsEntity())
	case m.asMode == asModeRunPhases:
		b.WriteString(m.viewWfPhases())
	case m.asMode == asModeRunAgent:
		b.WriteString(m.viewWfAgent())
	case m.asMode == asModeBrowser:
		b.WriteString(m.viewBrowser())
	default:
		if _, ok := m.focusedSession(); !ok {
			b.WriteString(m.boardFallback(
				faintStyle.Render("(no live agents — none spawned, and no subagent jobs)")))
		} else {
			b.WriteString(m.viewAsBoxes())
		}
	}
	pad := strings.Repeat(" ", boardMargin) // the jobs warning + footer align with the box border
	// The jobs-scan failure renders on its OWN dim line — a persistent data-availability warning,
	// not a one-shot outcome (those pop as centered modals). Every transient prompt/outcome (control
	// results, hide/show, the save-name input) is a modal now; only this warning and the key-legend
	// footer live at the board's foot.
	if m.boardJobsErr != nil {
		b.WriteString("\n" + pad + faintStyle.Render("jobs unavailable: "+
			sessiontitle.CleanTitle(m.boardJobsErr.Error())))
	}
	b.WriteString("\n" + pad + m.renderAsFooter())
	return b.String()
}

// spawnTitle is the Agents Board app title + tab hint, used in the loading / empty / error
// fallbacks and as the first chrome line every board level renders above its header.
func (m Model) spawnTitle() string {
	return titleStyle.Render("ds · Agents Board") + faintStyle.Render("    tab/q quit")
}

// boardFallback renders a bare board level (loading / error / empty) as the app title over a
// single message, inset by boardMargin so the title lines up with every populated level and the
// Model Providers hub — and never shifts horizontally when records arrive.
func (m Model) boardFallback(body string) string {
	return indentBox(m.spawnTitle()+"\n\n"+body, boardMargin)
}

// renderAsFooter is the contextual footer per asMode; the boxes level swaps in the card
// keys while the cursor sits on a job row (its card is inline there).
func (m Model) renderAsFooter() string {
	var hint string
	switch m.asMode {
	case asModeProjects:
		hint = "↑/↓ project · →/⏎ open · ctrl+f find · r refresh · esc/tab/q quit"
	case asModeSessions:
		hint = "↑/↓ session · →/⏎ open · d delete · ← back · ctrl+f find · r refresh · esc/tab/q quit"
	case asModeEntity:
		// A job IS a record, so it is deletable here.
		hint = "↑/↓ row · j/k scroll · ⏎ expand · p pin · d delete · c clear · ←/esc back · r refresh · q quit"
	case asModeRunPhases:
		hint = "↑/↓ phase · → agents · x stop · ←/esc back · r refresh · q quit"
	case asModeRunAgent:
		hint = "↑/↓ agent · j/k scroll · ⏎ prompt · x stop · ←/esc back · r refresh · q quit"
	case asModeBrowser:
		// q is search text in the browser, so quit is ctrl+c here; esc returns to the board.
		hint = "type to search · ↑/↓ move · ⏎ open · esc back · ctrl+c quit"
	default:
		_, onJob := m.boxJob()
		if onJob {
			hint = "↑/↓ row · j/k scroll · ⏎ expand · p pin · d delete · c clear · ←/esc back · r refresh · q quit"
		} else { // a run row
			hint = "↑/↓ row · →/⏎ detail · p pin · d delete · c clear · ← back · r refresh · esc/tab/q quit"
		}
	}
	return footer(hint)
}

// statusDot maps a leaf/run/phase status to a colored glyph: done ✔ (green), running ● (accent),
// failed ● (err), held ▶ (amber — parked by a leaf stop, waiting for its restart), stopped ■
// (faint — a stop is neutral, not a failure), cached ○ (faint), queued/unknown ◌ (faint hollow).
func statusDot(status string) string {
	switch status {
	case "done":
		return okStyle.Render("✔")
	case "running":
		return cursorStyle.Render("●")
	case "failed":
		return errStyle.Render("●")
	case "held":
		return noteStyle.Render("▶")
	case "stopped":
		return faintStyle.Render("■")
	case "cached":
		return faintStyle.Render("○")
	default: // "" / queued / not-yet-started
		return faintStyle.Render("◌")
	}
}

// labelStyle colors a board row label by progress: bright (liveStyle) for a reached row
// (done/running/failed/stopped), faint for a queued/not-started/cached one. The cursored row overrides
// this with selectedStyle (focus precedence), so labelStyle applies to non-cursored rows only.
func labelStyle(status string) lipgloss.Style {
	switch status {
	case "done", "running", "failed", "stopped", "held":
		return liveStyle
	default: // "" (queued/not-started) / "cached"
		return faintStyle
	}
}

// phaseStatus derives a phase's progress from its agent counts: done when all finished, running when
// some have started, "" (queued) when none have — so a phase row colors like a leaf row.
func phaseStatus(done, total int) string {
	switch {
	case total > 0 && done >= total:
		return "done"
	case total > 0:
		return "running"
	default:
		return ""
	}
}

// statusLabel is the detail-pane status token (glyph + word in one color): done renders an all-green
// "✔ Done", failed a red dot + word, stopped a faint dot + word (neutral), running/other an accent dot
// + a bright word.
func statusLabel(status string) string {
	switch status {
	case "done":
		return okStyle.Render("✔ " + humanStatus(status))
	case "failed":
		return statusDot(status) + " " + errStyle.Render(humanStatus(status))
	case "held":
		return statusDot(status) + " " + noteStyle.Render(humanStatus(status))
	case "stopped":
		return statusDot(status) + " " + faintStyle.Render(humanStatus(status))
	default:
		return statusDot(status) + " " + liveStyle.Render(humanStatus(status))
	}
}

// humanStatus title-cases a status word for the detail card ("running" → "Running"); empty → "Running".
func humanStatus(status string) string {
	if status == "" {
		return "Running"
	}
	return strings.ToUpper(status[:1]) + status[1:]
}

// boardWidth is the usable board width — m.width, or a default when no WindowSizeMsg has arrived
// (every board unit test renders at width 0, so the panes must still size positively).
func (m Model) boardWidth() int {
	if m.width > 40 {
		return m.width
	}
	return 100
}

// boardMargin is the horizontal inset of the header + box; the full-width header rule overhangs the
// inset box by this much on each side.
const boardMargin = 2

// boardInner is the content width of the inset header + box (boardWidth minus the two-side margin).
func (m Model) boardInner() int {
	return m.boardWidth() - 2*boardMargin
}

// headerRule is the full-width divider drawn between the run header and the inset box, so it overhangs
// the box top border by boardMargin on each side.
func (m Model) headerRule() string {
	return borderStyle.Render(strings.Repeat("─", m.boardWidth()))
}

// indentBox left-pads every line of s by n columns — the board content inset that lets the full-width
// header rule overhang the box.
func indentBox(s string, n int) string {
	if n <= 0 {
		return s
	}
	pad := strings.Repeat(" ", n)
	return pad + strings.ReplaceAll(s, "\n", "\n"+pad)
}

// isTerminalLeaf reports whether a leaf status is finished. cached counts as done (it completed in a
// prior run and is being replayed); queued / running / "" (not-yet-started) are all in-progress — so a
// queued placeholder never inflates the done counter into showing a phase complete before any work runs.
func isTerminalLeaf(status string) bool {
	switch status {
	case "done", "failed", "stopped", "cached":
		return true
	}
	return false
}

// phaseAgentCounts / runAgentCounts return (done, total) where done counts only TERMINAL leaves.
func phaseAgentCounts(p runPhaseGroup) (done, total int) {
	for _, j := range p.jobs {
		total++
		if isTerminalLeaf(j.Status) {
			done++
		}
	}
	return
}

func runAgentCounts(g runGroup) (done, total int) {
	for _, p := range g.phases {
		d, t := phaseAgentCounts(p)
		done += d
		total += t
	}
	return
}

// runTokens sums each leaf's cumulative OUTPUT tokens across the run — the truly additive
// figure (generated text accumulates across leaves). Live snapshot while running, the final
// Result once done. Cache-read is excluded throughout.
func (m Model) runTokens(g runGroup) int {
	total := 0
	for _, p := range g.phases {
		for _, j := range p.jobs {
			_, out, _ := m.leafCounts(j)
			total += out
		}
	}
	return total
}

// renderRunHeader is the run drill's header — the board's unified chrome: line 1 = the
// fixed app title (run cwd right-aligned), a blank spacer, line 3 = the run label beside
// the CURSORED thing's stats (the phase's agents+tokens at the Phases level, the leaf's
// tokens/tools/duration at the Agent level; the run rollup as the fallback).
func (m Model) renderRunHeader(g runGroup) string {
	bw := m.boardInner()
	right := m.runStatsLine(g)
	switch m.asMode {
	case asModeRunPhases:
		if p, ok := m.focusedPhase(); ok {
			right = m.phaseStatsLine(p)
		}
	case asModeRunAgent:
		if j, ok := m.selectedLeaf(); ok {
			right = m.leafStatsLine(j)
		}
	}
	return m.appTitleLine(g.cwd) + "\n\n" + headerSummaryLine(m.runLabel(g), right, bw)
}

// boardBodyHeight is the inner row budget for the master-detail box (drives right-pane scroll). It
// derives from the terminal height, with a default for tests / pre-WindowSizeMsg renders.
func (m Model) boardBodyHeight() int {
	h := m.height
	if h < 12 {
		h = 24
	}
	avail := h - 10 // header (3 lines: name / blank / desc) + rule + box top/bottom + status + footer + margin
	if avail < 5 {
		avail = 5
	}
	return avail
}

// boxCell pads (or ANSI-aware-truncates) a possibly-styled line to EXACTLY w visible columns. After a
// truncation it re-pads: ansi.Truncate refuses to split a double-width (CJK) glyph, so cutting on a
// wide-char boundary returns w-1 columns — without the re-pad the right border would shift left by one.
func boxCell(s string, w int) string {
	if w <= 0 {
		return ""
	}
	if ansi.StringWidth(s) > w {
		s = ansi.Truncate(s, w, "")
	}
	if pad := w - ansi.StringWidth(s); pad > 0 {
		s += strings.Repeat(" ", pad)
	}
	return s
}

// boxBorder builds one rounded box border row — a top "╭ <lt> ─┬ <rt> ─╮" or a bottom "╰─┴─ <extra>╯".
// leftW/rightW are the inner cell widths; each title segment spans its cell width + 4 (the cell's two
// surrounding spaces on each side). rightExtra is appended at the right segment's end (the bottom-border scroll
// indicator). The whole line renders in the box frame color so titles read as part of the frame.
func boxBorder(open, join, clos, leftTitle, rightTitle, rightExtra string, leftW, rightW int) string {
	seg := func(title, extra string, width int) string {
		if title == "" {
			fill := width - ansi.StringWidth(extra)
			if fill < 0 {
				fill = 0
			}
			return strings.Repeat("─", fill) + extra
		}
		head := "  " + title + " "
		fill := width - ansi.StringWidth(head) - ansi.StringWidth(extra)
		if fill < 0 {
			return boxCell(head, width-ansi.StringWidth(extra)) + extra
		}
		return head + strings.Repeat("─", fill) + extra
	}
	return borderStyle.Render(open + seg(leftTitle, "", leftW+4) + join + seg(rightTitle, rightExtra, rightW+4) + clos)
}

// renderBoard draws the native single enclosing box: two title segments over an internal divider
// (┬/┴-joined), the left pane's rows beside a scroll-window of the right pane's rows, and a bottom-
// right "↑ a–b of T ↓" when the right pane overflows bodyH. Both panes' lines are pre-styled; cells
// are ANSI-aware padded/truncated to the column widths.
func renderBoard(leftTitle string, leftLines []string, rightTitle string, rightLines []string, leftW, rightW, bodyH, scroll int) string {
	if scroll < 0 {
		scroll = 0
	}
	rightExtra := ""
	if len(rightLines) > bodyH {
		last := scroll + bodyH
		if last > len(rightLines) {
			last = len(rightLines)
		}
		rightExtra = fmt.Sprintf(" ↑ %d–%d of %d ↓ ", scroll+1, last, len(rightLines))
	}
	var b strings.Builder
	b.WriteString(boxBorder("╭", "┬", "╮", leftTitle, rightTitle, "", leftW, rightW) + "\n")
	bar := borderStyle.Render("│")
	for i := 0; i < bodyH; i++ {
		l, r := "", ""
		if i < len(leftLines) {
			l = leftLines[i]
		}
		if ri := i + scroll; ri < len(rightLines) {
			r = rightLines[ri]
		}
		b.WriteString(bar + "  " + boxCell(l, leftW) + "  " + bar + "  " + boxCell(r, rightW) + "  " + bar + "\n")
	}
	b.WriteString(boxBorder("╰", "┴", "╯", "", "", rightExtra, leftW, rightW))
	return b.String()
}

// windowLines keeps the cursor visible for a list longer than the box: it returns up to height lines
// centered on cursor. A list that already fits is returned unchanged.
func windowLines(lines []string, cursor, height int) []string {
	if height < 1 || len(lines) <= height {
		return lines
	}
	start := cursor - height/2
	if start < 0 {
		start = 0
	}
	if start+height > len(lines) {
		start = len(lines) - height
	}
	if start < 0 {
		start = 0
	}
	return lines[start : start+height]
}

// leftWidth sizes the master list (left rail) to its content — the wider of its title and its widest
// row — rather than a fixed fraction, so a short phase list doesn't hog the frame (the native board's
// left rail hugs its labels). Clamped to [14, boardWidth/2]. The right pane gets the rest.
func leftWidth(title string, lines []string, boardW int) int {
	w := ansi.StringWidth(title)
	for _, l := range lines {
		if sw := ansi.StringWidth(l); sw > w {
			w = sw
		}
	}
	w += 2 // breathing room past the widest label
	if w < 14 {
		w = 14
	}
	if cap := boardW / 2; w > cap {
		w = cap
	}
	return w
}

// paneWidths derives the right pane from a content-sized left: left + right + 11 == boardInner (the 11
// non-content columns are the two outer borders, the divider, and TWO spaces on each side of each cell).
// leftW is CAPPED to always leave ≥20 columns for the detail pane, so the box never overflows its inset
// width (the floor only bites on a sub-45-column terminal, where the box must wrap regardless).
func (m Model) paneWidths(leftW int) (left, right int) {
	avail := m.boardInner() - 11
	if avail < 30 {
		avail = 30
	}
	if leftW > avail-20 {
		leftW = avail - 20
	}
	if leftW < 10 {
		leftW = 10
	}
	return leftW, avail - leftW
}

// renderRunRow is one run row: "<dot> <name (short id)>" left; "<done>/<total> agents ·
// <elapsed>[ · <started MM-DD HH:MM>]" right-aligned. selected marks the cursored row.
func (m Model) renderRunRow(g runGroup, width int, selected bool) string {
	marker := ""
	name := trunc(m.runLabel(g), 42)
	switch {
	case selected:
		marker = cursorStyle.Render("❯ ")
		name = selectedStyle.Render(name)
	default:
		name = labelStyle(g.status).Render(name)
	}
	done, total := runAgentCounts(g)
	left := marker + statusDot(g.status) + " " + name
	metrics := fmt.Sprintf("%d/%d agents · %s", done, total, g.elapsed())
	if t, err := time.Parse(time.RFC3339, g.startedAt); err == nil {
		metrics += " · " + t.Format("01-02 15:04")
	}
	return joinRowEnds(left, faintStyle.Render(metrics)+m.pinSuffix(pinned.Run, g.runID), width)
}

// viewWfPhases is the run drill's Phases level: the run header above one box — "Phases | the selected
// phase's agents". The box is a FIXED height (fills the screen) so the bottom border stays put; the
// left rail is content-sized.
func (m Model) viewWfPhases() string {
	g, ok := m.focusedGroup()
	if !ok {
		return faintStyle.Render("(no workflow runs)")
	}
	var leftLines []string
	for i, p := range g.phases {
		marker := "  "
		if i == m.wfPhaseCursor {
			marker = cursorStyle.Render("❯ ")
		}
		leftLines = append(leftLines, marker+phaseRow(p, i, i == m.wfPhaseCursor))
	}
	leftW, rightW := m.paneWidths(leftWidth("Phases", leftLines, m.boardInner()))
	rightTitle, rightLines := m.phaseAgentLines(rightW)
	bodyH := m.boardBodyHeight()
	leftLines = windowLines(leftLines, m.wfPhaseCursor, bodyH)
	return indentBox(m.renderRunHeader(g), boardMargin) + "\n" + m.headerRule() + "\n" +
		indentBox(renderBoard("Phases", leftLines, rightTitle, rightLines, leftW, rightW, bodyH, 0), boardMargin)
}

// phaseRow is one phase line — a green ✔ (done) or the 1-based index, the title, and the
// done/total agent counts; selected swaps the title to the selection tone. Shared by the
// Phases drill rail and the Dynamic Workflows box preview.
func phaseRow(p runPhaseGroup, i int, selected bool) string {
	done, total := phaseAgentCounts(p)
	st := phaseStatus(done, total)
	title := trunc(sessiontitle.CleanTitle(p.title), 28)
	glyph := fmt.Sprintf("%d", i+1)
	if st == "done" {
		glyph = statusDot("done")
	}
	if selected {
		title = selectedStyle.Render(title)
	} else {
		title = labelStyle(st).Render(title)
	}
	counts := ""
	if total > 0 {
		counts = "  " + faintStyle.Render(fmt.Sprintf("%d/%d", done, total))
	}
	return fmt.Sprintf("%s %s%s", glyph, title, counts)
}

// phaseAgentLines returns the title + full agent rows (right pane) for the focused phase ("Not started
// yet" when empty). width is the right pane width — the metrics right-align to it.
func (m Model) phaseAgentLines(width int) (title string, lines []string) {
	p, ok := m.focusedPhase()
	if !ok {
		return "agents", []string{faintStyle.Render("Not started yet")}
	}
	title = fmt.Sprintf("%s · %d agents", trunc(sessiontitle.CleanTitle(p.title), 20), len(p.jobs))
	if len(p.jobs) == 0 {
		return title, []string{faintStyle.Render("Not started yet")}
	}
	for _, j := range p.jobs {
		lines = append(lines, m.renderAgentRowFull(j, width))
	}
	return title, lines
}

// agentLeftLines builds the COMPACT agent list shown in the run drill's agent left rail (status +
// label only; the metrics live in the detail pane), plus its title. Shared by viewWfAgent and
// wfAgentRightWidth so the scroll clamp and the render agree on the right pane width.
func (m Model) agentLeftLines() (title string, lines []string) {
	p, ok := m.focusedPhase()
	if !ok {
		return "agents", nil
	}
	title = fmt.Sprintf("%s · %d agents", trunc(sessiontitle.CleanTitle(p.title), 20), len(p.jobs))
	for i, j := range p.jobs {
		lines = append(lines, m.renderAgentRowCompact(j, i == m.wfAgentCursor))
	}
	return title, lines
}

// wfAgentRightWidth is the run drill's agent right pane width (mirrors viewWfAgent's content-sized
// split) so the scroll clamp wraps the detail to the same column budget the render uses.
func (m Model) wfAgentRightWidth() int {
	title, lines := m.agentLeftLines()
	_, rightW := m.paneWidths(leftWidth(title, lines, m.boardInner()))
	return rightW
}

// viewWfAgent is the run drill's Agent level: the run header above one box — "agent list | the focused
// agent's inline detail" (the right pane scrolls with j/k via wfCardScroll). Fixed-height box;
// content-sized left rail.
func (m Model) viewWfAgent() string {
	g, ok := m.focusedGroup()
	if !ok {
		return faintStyle.Render("(no workflow runs)")
	}
	listTitle, leftLines := m.agentLeftLines()
	leftW, rightW := m.paneWidths(leftWidth(listTitle, leftLines, m.boardInner()))
	cardTitle := "agent"
	if j, jok := m.selectedLeaf(); jok {
		if t := trunc(sessiontitle.CleanTitle(j.Label), rightW-6); t != "" {
			cardTitle = t
		}
	}
	rightLines := m.agentDetailLines(rightW)
	bodyH := m.boardBodyHeight()
	leftLines = windowLines(leftLines, m.wfAgentCursor, bodyH)
	return indentBox(m.renderRunHeader(g), boardMargin) + "\n" + m.headerRule() + "\n" +
		indentBox(renderBoard(listTitle, leftLines, cardTitle, rightLines, leftW, rightW, bodyH, m.clampCardScroll(m.wfCardScroll)), boardMargin)
}

// renderAgentRowFull is one agent row for a phase's agent list (right pane): "<dot> <label>  <model>"
// left, "↓ <out> out · <N> tools · <dur>" RIGHT-ALIGNED to width — output tokens only, so the rows sum
// to the header total (input is the leaf's peak context, shown per-leaf in the detail, never summed).
// Live for a RUNNING leaf from its activity snapshot; a done leaf uses its final Result. No answer text.
func (m Model) renderAgentRowFull(j dsjobs.Result, width int) string {
	_, out, tools := m.leafCounts(j)
	label := sessiontitle.CleanTitle(j.Label)
	model := sessiontitle.CleanTitle(j.Model)
	left := statusDot(j.Status) + " "
	switch {
	case label != "":
		left += labelStyle(j.Status).Render(label)
		if model != "" {
			left += "  " + faintStyle.Render(trunc(model, 22))
		}
	case model != "":
		left += faintStyle.Render(trunc(model, 28)) // unlabeled leaf → the model is its identifier
	default:
		left += faintStyle.Render("agent")
	}
	metrics := fmt.Sprintf("↓ %s out · %d tools", humanTokens(out), tools)
	if d := leafDuration(j); d != "" {
		metrics += " · " + d
	}
	return joinRowEnds(left, faintStyle.Render(metrics), width)
}

// joinRowEnds right-aligns the metrics beside the label within width: a tight row shrinks
// the label to fit, a pathologically narrow one keeps the metrics alone, truncated.
func joinRowEnds(left, right string, width int) string {
	rw := ansi.StringWidth(right)
	gap := width - ansi.StringWidth(left) - rw
	if gap < 1 {
		if avail := width - rw - 1; avail >= 1 {
			left = boxCell(left, avail)
			gap = 1
		} else {
			return boxCell(right, width)
		}
	}
	return left + strings.Repeat(" ", gap) + right
}

// renderAgentRowCompact is one agent row for the run drill's agent left rail: marker + status + label only (narrow).
func (m Model) renderAgentRowCompact(j dsjobs.Result, selected bool) string {
	marker := "  "
	label := sessiontitle.CleanTitle(j.Label)
	if label == "" {
		if model := sessiontitle.CleanTitle(j.Model); model != "" {
			label = model // unlabeled leaf → the model is its identifier
		} else {
			label = "agent"
		}
	}
	if selected {
		marker = cursorStyle.Render("❯ ")
		label = selectedStyle.Render(label)
	} else {
		label = labelStyle(j.Status).Render(label)
	}
	return marker + statusDot(j.Status) + " " + label
}

// leafDuration formats a done leaf's wall-clock (DurationMs) as "30s" / "2m 3s"; "" while running.
func leafDuration(j dsjobs.Result) string {
	if j.DurationMs <= 0 {
		return ""
	}
	d := time.Duration(j.DurationMs) * time.Millisecond
	if d < time.Minute {
		return fmt.Sprintf("%ds", int(d.Seconds()))
	}
	return fmt.Sprintf("%dm %ds", int(d/time.Minute), int(d.Seconds())%60)
}

// snapForJob returns the live activity snapshot that may drive j's board display, or the zero snapshot
// when none applies. The snapshot describes j only while j is RUNNING at the SAME attempt it was scanned
// at: floorActivity smooths m.wfActivity across out-of-order refreshes, so the map can momentarily hold a
// snapshot carried from a different attempt (a restart reuses the job id), which must never paint j's
// tokens or tool count. A non-running job renders from its Result, never the snapshot. Single render-side
// gate — every wfActivity consumer reads through it.
func (m Model) snapForJob(j dsjobs.Result) activitySnapshot {
	if snap := m.wfActivity[j.JobID]; j.Status == "running" && snap.attempt == j.Attempt {
		return snap
	}
	return activitySnapshot{}
}

// leafCounts returns the leaf's (input, output) tokens + tool-call count. input is the PEAK context
// window (max over turns, non-cumulative — so it is shown per-leaf, never summed across leaves); output
// is CUMULATIVE generated tokens (additive, what the run header sums); cache-read is excluded. Live from
// the activity snapshot while running, the accurate final from the Result once done; the tool count comes
// from the snapshot (the final Result doesn't carry it), so it shows only while j is the running current
// attempt.
func (m Model) leafCounts(j dsjobs.Result) (in, out, tools int) {
	in, out = m.liveTokens(j)
	return in, out, m.snapForJob(j).toolCount()
}

// liveTokens resolves a job's (peak-input, cumulative-output) token pair: the final Result usage,
// overridden by the live activity snapshot while the job runs. Shared by leafCounts and jobTokens so
// every surface (run header, workflow rows, subagent rows) reads the same 500ms-refreshed source.
func (m Model) liveTokens(j dsjobs.Result) (in, out int) {
	if j.Usage != nil {
		in, out = j.Usage.InputTokens, j.Usage.OutputTokens
	}
	if snap := m.snapForJob(j); snap.hasUsage {
		in, out = snap.inTok, snap.outTok
	}
	return in, out
}

// agentDetailLines is the focused agent's inline detail (the run drill's agent right pane, scrollable): status/model
// (with an "attempt N" marker when a legacy record carries one) and ↑ ctx · ↓ out · tool-calls, then a fixed
// Prompt → Activity → Output → Outcome order — the Prompt, the Activity
// feed (last 3 tool signatures), the Output (when the io files are loaded for THIS leaf via the PersistIO
// opt-in), and the Outcome. The Output reads from the leaf's .answer side file (focused-single-agent
// surface, CleanTitle-scrubbed), NEVER Result.Result on a row.
func (m Model) agentDetailLines(rightW int) []string {
	j, ok := m.selectedLeaf()
	if !ok {
		return []string{faintStyle.Render("(no agent)")}
	}
	in, out, tools := m.leafCounts(j)
	snap := m.snapForJob(j)
	status := statusLabel(j.Status) + faintStyle.Render(" · "+trunc(sessiontitle.CleanTitle(j.Model), 28))
	if j.Attempt > 1 {
		// >1 occurs only in records from engines that retried schema mismatches; surface it.
		status += faintStyle.Render(fmt.Sprintf(" · attempt %d", j.Attempt))
	}
	// ↑ peak input context · ↓ cumulative output (the header sums only output across leaves) · cache-write
	// tokens when the leaf wrote the prompt cache · tool calls.
	tokLine := fmt.Sprintf("↑ %s ctx · ↓ %s out", humanTokens(in), humanTokens(out))
	if j.Usage != nil && j.Usage.CacheCreationInputTokens > 0 {
		tokLine += fmt.Sprintf(" · ⊕ %s cache-w", humanTokens(j.Usage.CacheCreationInputTokens))
	}
	tokLine += fmt.Sprintf(" · %d tool calls", tools)
	lines := []string{
		status,
		faintStyle.Render(tokLine),
	}
	// Prompt first.
	switch {
	case m.wfDetailJob.JobID != j.JobID:
		lines = append(lines, "", faintStyle.Render("(loading…)"))
	case !m.wfDetailIO:
		lines = append(lines, "", faintStyle.Render("(prompt/output not persisted — run with default persist-io)"))
	default:
		lines = append(lines, "")
		lines = append(lines, promptSection(m.wfDetailPrompt, m.wfPromptExpanded, rightW)...)
	}
	// Then the Activity feed.
	lines = append(lines, "")
	lines = append(lines, activityLines(snap, rightW)...)
	// Then the Output, when this leaf's io files are loaded.
	if m.wfDetailJob.JobID == j.JobID && m.wfDetailIO {
		lines = append(lines, "", faintStyle.Render("Output"))
		lines = append(lines, ioLines(m.wfDetailAnswer, rightW, liveStyle)...)
	}
	// Outcome last.
	lines = append(lines, "", faintStyle.Render("Outcome"), " "+m.renderOutcome(j))
	return lines
}

// ioLines renders an io block (prompt or answer) preserving its source LOGICAL lines: each newline-
// delimited line is CleanTitle-scrubbed (scrub per line — CleanTitle collapses whitespace, so scrubbing
// the whole block first would lose the line breaks), then hard-wrapped to width-2 and indented ONE
// column within the cell. With the cell's own 2-column pane padding the body sits 3 columns from the
// left box border and a matching margin from the right (boxCell pads the spare column) — one step
// deeper than the section headers, so the hierarchy reads. An empty block shows a dim placeholder.
// style colors the body — the gray contentStyle for the prompt, the bright liveStyle for the answer.
func ioLines(s string, width int, style lipgloss.Style) []string {
	if strings.TrimSpace(s) == "" {
		return []string{faintStyle.Render(" (empty)")}
	}
	var out []string
	for _, ln := range strings.Split(s, "\n") {
		clean := sessiontitle.CleanTitle(ln)
		if clean == "" {
			out = append(out, "") // preserve a blank line between paragraphs
			continue
		}
		for _, w := range wrapTo(clean, width-2) {
			out = append(out, " "+style.Render(w))
		}
	}
	return out
}

// promptPreviewLines is how many DISPLAY lines a collapsed fold section shows before "… N more".
const promptPreviewLines = 4

// promptSection renders the detail card's Prompt block: the full text when expanded, else a
// collapsed "Prompt · N lines · ⏎ expand" header + the leading preview. The fold counts
// DISPLAY lines (post-wrap), so a single long paragraph folds too; a prompt that already
// fits the preview shows in full with no expand hint. Shared by the Workflows agent card
// and the Agents Board job card so the two read identically.
func promptSection(prompt string, expanded bool, rightW int) []string {
	full := ioLines(prompt, rightW, contentStyle)
	if expanded || len(full) <= promptPreviewLines {
		return append([]string{faintStyle.Render("Prompt")}, full...)
	}
	lines := []string{faintStyle.Render(fmt.Sprintf("Prompt · %d lines · ⏎ expand", len(full)))}
	prev := full[:promptPreviewLines]
	for len(prev) > 0 && strings.TrimSpace(prev[len(prev)-1]) == "" {
		prev = prev[:len(prev)-1] // drop a trailing blank preview row so "… N more lines" doesn't gap
	}
	lines = append(lines, prev...)
	lines = append(lines, " "+faintStyle.Render(fmt.Sprintf("… %d more lines", len(full)-len(prev)))) // body-aligned (indent 1)
	return lines
}

// activityLines renders a detail card's Activity section: the tool-call header + the last
// 3 signatures ("(no tool calls)" when none). Shared by all three detail cards.
func activityLines(snap activitySnapshot, rightW int) []string {
	lines := []string{faintStyle.Render(fmt.Sprintf("Activity · last 3 of %d tool calls", snap.toolCount()))}
	sigs := snap.lastSigs(3)
	if len(sigs) == 0 {
		lines = append(lines, faintStyle.Render(" (no tool calls)"))
	}
	for _, s := range sigs {
		lines = append(lines, " "+contentStyle.Render(truncCols(sessiontitle.CleanTitle(s), rightW-2)))
	}
	return lines
}

// truncCols truncates a plain (un-styled) string to w DISPLAY columns with an "…" tail (CJK-aware), so
// a tool signature is bounded by columns, not runes — leaving the pane's right margin intact.
func truncCols(s string, w int) string {
	if w < 1 {
		w = 1
	}
	if ansi.StringWidth(s) <= w {
		return s
	}
	return ansi.Truncate(s, w, "…")
}

// prettyDir abbreviates $HOME to ~ for a compact project-dir display.
func prettyDir(dir string) string {
	if home, err := os.UserHomeDir(); err == nil && home != "" {
		if dir == home {
			return "~"
		}
		if strings.HasPrefix(dir, home+string(os.PathSeparator)) {
			return "~" + dir[len(home):]
		}
	}
	return dir
}

// leftTruncCols keeps the TRAILING w display columns of s (a path's tail is the useful part), prefixing
// "…" when it cut. CJK-aware via ansi.StringWidth.
func leftTruncCols(s string, w int) string {
	if w < 1 {
		w = 1
	}
	if ansi.StringWidth(s) <= w {
		return s
	}
	r := []rune(s)
	for len(r) > 0 && ansi.StringWidth(string(r)) > w-1 {
		r = r[1:]
	}
	return "…" + string(r)
}

// wrapTo hard-wraps a plain (un-styled) string to w DISPLAY columns — CJK-aware (a wide glyph counts
// as 2), so a double-width line doesn't overflow the pane and get truncated off-screen.
func wrapTo(s string, w int) []string {
	if w < 1 {
		w = 1
	}
	if ansi.StringWidth(s) <= w {
		return []string{s}
	}
	var out []string
	var cur strings.Builder
	curW := 0
	flush := func() {
		if cur.Len() > 0 {
			out = append(out, cur.String())
			cur.Reset()
			curW = 0
		}
	}
	for _, word := range strings.Fields(s) {
		// A word too wide for any line (a long token, or a space-less script like
		// CJK) is character-wrapped; otherwise words pack greedily, breaking only on
		// spaces so a word is never split mid-token.
		if ansi.StringWidth(word) > w {
			flush()
			for _, r := range word {
				rw := ansi.StringWidth(string(r))
				if curW+rw > w && curW > 0 {
					flush()
				}
				cur.WriteRune(r)
				curW += rw
			}
			continue
		}
		sep := 0
		if curW > 0 {
			sep = 1
		}
		if curW+sep+ansi.StringWidth(word) > w {
			flush()
			sep = 0
		}
		if sep == 1 {
			cur.WriteByte(' ')
			curW++
		}
		cur.WriteString(word)
		curW += ansi.StringWidth(word)
	}
	flush()
	return out
}

// renderOutcome is the key-safe outcome line: status + a canonical summary, NEVER Result.Result.
// Done shows turns, stopped is neutral, a failure shows its error class, queued/running stay in progress.
func (m Model) renderOutcome(j dsjobs.Result) string {
	switch {
	case j.Status == "held":
		// MUST precede the OK branch: a held StatusFor result carries OK=true.
		return noteStyle.Render("held — restart this agent to resume")
	case j.Status == "running" || j.Status == "queued" || j.Status == "":
		return faintStyle.Render("Still running…") // queued has OK=true but isn't done — keep it in-progress
	case j.OK || j.Status == "done":
		return faintStyle.Render(fmt.Sprintf("done · %d turns", j.NumTurns))
	case j.Status == "stopped":
		return faintStyle.Render("stopped") // a `workflow stop` reap — neutral, not a failure
	default:
		cls := j.ErrorCode
		if cls == "" {
			cls = "failed"
		}
		return errStyle.Render(sessiontitle.CleanTitle(cls))
	}
}

// humanTokens compacts a token count: <1000 verbatim, else N.Nk (e.g. 50.7k), else
// N.NM for millions.
func humanTokens(n int) string {
	switch {
	case n >= 1_000_000:
		return fmt.Sprintf("%.1fM", float64(n)/1_000_000)
	case n >= 1000:
		return fmt.Sprintf("%.1fk", float64(n)/1000)
	default:
		return fmt.Sprintf("%d", n)
	}
}

// runLabel renders a run header label: its sanitized name plus the short run id,
// or just the short id when the run has no name (manifest GC'd or never created).
func (m Model) runLabel(g runGroup) string {
	// The run id is opaque operator metadata: ids.ValidateJobID lets a
	// non-whitespace control rune (e.g. an ANSI escape) through — it only blocks
	// path-unsafe chars — so the id gets the same render-time CleanTitle scrub as
	// the name/phase/label before it reaches the terminal.
	short := shortRunID(sessiontitle.CleanTitle(g.runID))
	if name := sessiontitle.CleanTitle(g.name); name != "" {
		return trunc(name, 48) + " (" + short + ")"
	}
	return short
}

// shortRunID trims a run id to its first 8 chars for the run header.
func shortRunID(id string) string {
	if len(id) > 8 {
		return id[:8]
	}
	return id
}

// headerSummaryLine is the chrome's third line: the CURSORED child's label on the left (the
// live preview context, the slot the wf run header gives its description) + the container
// rollup right-aligned. The left side truncates so the line never wraps and shifts the
// fixed-height boxes down.
func headerSummaryLine(left, right string, bw int) string {
	r := liveStyle.Render(right)
	if left == "" {
		gap := bw - lipgloss.Width(r)
		if gap < 0 {
			gap = 0
		}
		return strings.Repeat(" ", gap) + r
	}
	l := liveStyle.Render(trunc(left, 80))
	gap := bw - lipgloss.Width(l) - lipgloss.Width(r)
	if gap < 1 {
		l = boxCell(l, bw-lipgloss.Width(r)-1)
		gap = 1
	}
	return l + strings.Repeat(" ", gap) + r
}

// projectCounts is one project's "N sessions[ · K workflows][ · J subagents]" rollup.
func projectCounts(p asProject) string {
	jobs, runs := 0, 0
	for _, sess := range p.sessions {
		jobs += len(sess.jobs)
		runs += len(sess.runs)
	}
	out := fmt.Sprintf("%d sessions", len(p.sessions))
	if runs > 0 {
		out += fmt.Sprintf(" · %d workflows", runs)
	}
	if jobs > 0 {
		out += fmt.Sprintf(" · %d subagents", jobs)
	}
	return out
}

// renderAppHeader is the L0 header — the same THREE-line + rule chrome every deeper level
// uses, so changing levels never reshapes the screen: the app title + tab hint on line 1, a
// blank spacer, then the cursored project beside ITS rollup (the project total is the
// rail's own row count).
func (m Model) renderAppHeader() string {
	bw := m.boardInner()
	projects := m.asProjects()
	left, right := "", ""
	if m.asProjectCursor < len(projects) {
		p := projects[m.asProjectCursor]
		right = projectCounts(p)
		// L0 has no dir on the title line, so the label carries the project's FULL
		// path, front-abbreviated when it overflows the slot.
		label := sessiontitle.CleanTitle(p.dir)
		if label == "" {
			label = "(no project)"
		}
		limit := bw - ansi.StringWidth(right) - 1
		if limit > 80 {
			limit = 80
		}
		left = leftTruncCols(label, limit)
	}
	return m.spawnTitle() + "\n\n" + headerSummaryLine(left, right, bw)
}

// viewAsProjects is L0 (>1 project): the app header above one box — the project rail | the
// cursored project's session rows (title, created time).
func (m Model) viewAsProjects() string {
	projects := m.asProjects()
	var leftLines []string
	for i, p := range projects {
		marker := "  "
		label := leftTruncCols(sessiontitle.CleanTitle(projectName(p.dir)), 28)
		if i == m.asProjectCursor {
			marker = cursorStyle.Render("❯ ")
			label = selectedStyle.Render(label)
		} else {
			label = liveStyle.Render(label)
		}
		leftLines = append(leftLines, fmt.Sprintf("%s%s  %s", marker, label,
			faintStyle.Render(fmt.Sprintf("%d", len(p.sessions)))))
	}
	leftW, rightW := m.paneWidths(leftWidth("Projects", leftLines, m.boardInner()))
	rightTitle := "sessions"
	rightLines := []string{faintStyle.Render("(none)")}
	if m.asProjectCursor < len(projects) {
		p := projects[m.asProjectCursor]
		rightTitle = fmt.Sprintf("%s · %d sessions", leftTruncCols(sessiontitle.CleanTitle(projectLabel(p.dir)), 24), len(p.sessions))
		rightLines = nil
		for _, s := range p.sessions {
			rightLines = append(rightLines, m.renderSessionRow(s, rightW))
		}
		// Same codex / no-session dividers the L1 Sessions pane shows — this preview has no cursor.
		rightLines, _ = launcherDividerRows(rightLines, p.sessions, 0, rightW)
	}
	bodyH := m.boardBodyHeight()
	leftLines = windowLines(leftLines, m.asProjectCursor, bodyH)
	return indentBox(m.renderAppHeader(), boardMargin) + "\n" + m.headerRule() + "\n" +
		indentBox(renderBoard("Projects", leftLines, rightTitle, rightLines, leftW, rightW, bodyH, 0), boardMargin)
}

// appTitleLine is the chrome's FIRST line at every Agents Board level: the fixed app title
// + tab hint, with the level's directory (when it has one) right-aligned in faint — so the
// top anchor never changes as the user drills.
func (m Model) appTitleLine(dir string) string {
	left := m.spawnTitle()
	if dir == "" {
		return left
	}
	bw := m.boardInner()
	d := faintStyle.Render(leftTruncCols(sessiontitle.CleanTitle(prettyDir(dir)), bw/2))
	gap := bw - lipgloss.Width(left) - lipgloss.Width(d)
	if gap < 1 {
		gap = 1
	}
	return left + strings.Repeat(" ", gap) + d
}

// renderProjectHeader is the L1 header — the fixed app title line over the CONTAINER's
// label + rollup. The cursored session names only the right pane (the preview).
func (m Model) renderProjectHeader(p asProject) string {
	bw := m.boardInner()
	line1 := m.appTitleLine(p.dir)
	// The left slot stays FIXED on the container label (the cursored session already
	// shows in the rail highlight + the right pane title; a moving header reads jumpy).
	return line1 + "\n\n" + headerSummaryLine(sessiontitle.CleanTitle(projectLabel(p.dir)), projectCounts(p), bw)
}

// viewAsSessions is L1 (>1 session in the focused project): the PROJECT's header (the
// container the user descended into) above one box — the session rail | the cursored
// session's row preview.
func (m Model) viewAsSessions() string {
	p, ok := m.focusedProjectGroup()
	if !ok {
		return m.boardFallback(faintStyle.Render("(no live agents)"))
	}
	var leftLines []string
	for i, s := range p.sessions {
		marker := "  "
		label := trunc(m.sessionLabel(s.sessionID), 40)
		if i == m.asSessionCursor {
			marker = cursorStyle.Render("❯ ")
			label = selectedStyle.Render(label)
		} else {
			label = liveStyle.Render(label)
		}
		leftLines = append(leftLines, marker+sessionMarker(s.sessionID)+label)
	}
	leftW, rightW := m.paneWidths(leftWidth("Sessions", leftLines, m.boardInner()))
	rightTitle := "overview"
	if m.asSessionCursor < len(p.sessions) {
		rightTitle = trunc(m.sessionTitleWithLauncher(p.sessions[m.asSessionCursor].sessionID), rightW-6)
	}
	rightLines := m.sessionOverviewLines(p, rightW)
	bodyH := m.boardBodyHeight()
	// Splice the codex / no-session launcher dividers in (render-only; the cursor still indexes
	// p.sessions) and window to the shifted render cursor.
	renderLines, renderCursor := launcherDividerRows(leftLines, p.sessions, m.asSessionCursor, leftW)
	renderLines = windowLines(renderLines, renderCursor, bodyH)
	return indentBox(m.renderProjectHeader(p), boardMargin) + "\n" + m.headerRule() + "\n" +
		indentBox(renderBoard("Sessions", renderLines, rightTitle, rightLines, leftW, rightW, bodyH, 0), boardMargin)
}

// sessionOverviewLines is the L1 right pane: the cursored session's ACTUAL rows under
// separate Dynamic Workflows / Agent Teams / Subagents sections — what ⏎ will open, previewed
// in place (renderBoard clips overflow to the box height; created/project live in the header).
func (m Model) sessionOverviewLines(p asProject, rightW int) []string {
	if m.asSessionCursor >= len(p.sessions) {
		return []string{faintStyle.Render("(none)")}
	}
	s := p.sessions[m.asSessionCursor]
	var lines []string
	if len(s.runs) > 0 {
		lines = append(lines, "", faintStyle.Render(fmt.Sprintf("Dynamic Workflows · %d", len(s.runs))))
		for _, g := range s.runs {
			lines = append(lines, " "+m.renderRunRow(g, rightW-1, false))
		}
	}
	if len(s.jobs) > 0 {
		lines = append(lines, "", faintStyle.Render(fmt.Sprintf("Subagents · %d", len(s.jobs))))
		for _, j := range s.jobs {
			lines = append(lines, " "+m.renderJobRowFull(j, rightW-1))
		}
	}
	if len(lines) > 0 && lines[0] == "" {
		lines = lines[1:] // the first section needs no leading spacer
	}
	return lines
}

// renderSessionRow is one session row in the L0 right pane: the launcher marker (◆ claude /
// ◇ codex) + "<title (short id)>" left, the created time right-aligned — the header already
// carries the cursored project's counts, so the row keeps its width for the title.
func (m Model) renderSessionRow(s asSession, width int) string {
	left := sessionMarker(s.sessionID) + liveStyle.Render(trunc(m.sessionLabel(s.sessionID), 44))
	return joinRowEnds(left, faintStyle.Render(asCreated(s)), width)
}

// asSessionCounts is a session's rollup: "K workflows · M subagents"; "empty" when it has neither.
func asSessionCounts(s asSession) string {
	var parts []string
	if len(s.runs) > 0 {
		parts = append(parts, fmt.Sprintf("%d workflows", len(s.runs)))
	}
	if len(s.jobs) > 0 {
		parts = append(parts, fmt.Sprintf("%d subagents", len(s.jobs)))
	}
	if len(parts) == 0 {
		return "empty"
	}
	return strings.Join(parts, " · ")
}

// asCreated is the session's first-activity timestamp ("" when unrecorded).
func asCreated(s asSession) string {
	if !s.hasTime {
		return ""
	}
	return s.earliest.Format("01-02 15:04")
}

// splitPathSegs breaks a project dir into its path segments, splitting on either
// separator so a Windows backslash cwd (C:\Users\me\proj) yields real segments
// instead of one. Empty segments (leading / doubled separators) are dropped.
func splitPathSegs(dir string) []string {
	return strings.FieldsFunc(dir, func(r rune) bool { return r == '/' || r == '\\' })
}

// projectName is the project rail's short form — the path's LAST segment only (the L0
// header and the right pane title carry the longer forms); "(no project)" when unresolved.
func projectName(dir string) string {
	if dir == "" {
		return "(no project)"
	}
	segs := splitPathSegs(dir)
	if len(segs) == 0 {
		return ""
	}
	return segs[len(segs)-1]
}

// projectLabel renders a project dir as its LAST TWO path segments (a deep absolute path
// doesn't fit a rail and the tail is the identifying part); "(no project)" when unresolved.
func projectLabel(dir string) string {
	if dir == "" {
		return "(no project)"
	}
	segs := splitPathSegs(dir)
	if n := len(segs); n > 2 {
		segs = segs[n-2:]
	}
	return strings.Join(segs, "/")
}

// renderSessionHeader is the persistent header above the L2/L3 boxes, THREE lines: the
// FIXED app title (the session's dir right-aligned); a blank spacer; then the session title
// beside the right slot — the CURSORED agent's own stats when the cursor sits on a single
// job/teammate (its tokens/elapsed live where the eye already is), else the session's
// counts + created rollup. Width-bounded so a long title can't wrap and shift the
// fixed-height boxes down.
func (m Model) renderSessionHeader(s asSession) string {
	bw := m.boardInner()
	line1 := m.appTitleLine(m.sessionMeta[s.sessionID].Cwd)
	right := m.headerEntityStats()
	if right == "" {
		right = asSessionCounts(s)
		if c := asCreated(s); c != "" {
			right += " · created " + c
		}
	}
	return line1 + "\n\n" + headerSummaryLine(m.sessionTitleWithLauncher(s.sessionID), right, bw)
}

// headerEntityStats is the cursored row's own stat summary for the session header: a job's
// tokens + elapsed + started, an L2 run row's run stats. "" when nothing single is cursored (an empty
// collection) — the header falls back to the session rollup.
func (m Model) headerEntityStats() string {
	switch m.asMode {
	case asModeEntity:
		if j, ok := m.selectedJob(); ok {
			return m.jobStats(j)
		}
	case asModeBoxes:
		if g, ok := m.boxRun(); ok {
			return m.runStatsLine(g)
		}
		if j, ok := m.boxJob(); ok {
			return m.jobStats(j)
		}
	}
	return ""
}

// jobStats is one job's header summary: "↑ <ctx> ctx · ↓ <out> out · <elapsed> · <started>"
// with the token segment omitted until usage exists (a running unfocused job reports none).
func (m Model) jobStats(j dsjobs.Result) string {
	var parts []string
	if in, out := m.jobTokens(j); in > 0 || out > 0 {
		parts = append(parts, fmt.Sprintf("↑ %s ctx · ↓ %s out", humanTokens(in), humanTokens(out)))
	}
	if el := jobElapsed(j); el != "" {
		parts = append(parts, el)
	}
	if ts, err := time.Parse(time.RFC3339, j.StartedAt); err == nil {
		parts = append(parts, ts.Format("01-02 15:04"))
	}
	return strings.Join(parts, " · ")
}

// runStatsLine is a run's header summary: agents done/total, summed leaf input + output
// tokens, elapsed, started.
func (m Model) runStatsLine(g runGroup) string {
	done, total := runAgentCounts(g)
	in := 0
	for _, p := range g.phases {
		for _, j := range p.jobs {
			i, _, _ := m.leafCounts(j)
			in += i
		}
	}
	out := fmt.Sprintf("%d/%d agents · ↑ %s · ↓ %s · %s", done, total, humanTokens(in), humanTokens(m.runTokens(g)), g.elapsed())
	if ts, err := time.Parse(time.RFC3339, g.startedAt); err == nil {
		out += " · " + ts.Format("01-02 15:04")
	}
	return out
}

// phaseStatsLine is a phase's header summary: its agents done/total + summed tokens.
func (m Model) phaseStatsLine(p runPhaseGroup) string {
	done, total := phaseAgentCounts(p)
	in, out := 0, 0
	for _, j := range p.jobs {
		i, o, _ := m.leafCounts(j)
		in += i
		out += o
	}
	return fmt.Sprintf("%d/%d agents · ↑ %s · ↓ %s", done, total, humanTokens(in), humanTokens(out))
}

// leafStatsLine is one leaf's header summary: its tokens, tool calls, and duration.
func (m Model) leafStatsLine(j dsjobs.Result) string {
	in, out, tools := m.leafCounts(j)
	line := fmt.Sprintf("↑ %s ctx · ↓ %s out · %d tools", humanTokens(in), humanTokens(out), tools)
	if d := leafDuration(j); d != "" {
		line += " · " + d
	}
	return line
}

// jobTokens is the subagent row/card token pair — the shared live resolution, so any row (not just
// the focused one) climbs live.
func (m Model) jobTokens(j dsjobs.Result) (in, out int) { return m.liveTokens(j) }

// viewAsBoxes is L2: the session header above the stacked boxes — Dynamic Workflows
// (master-detail: run rail | the previewed run's phase rows), Agent Teams (master-detail:
// team rail | the cursored team's member rows), and
// Subagents (master-detail: job rail | the previewed job's inline card — the cursored job,
// else the first; no descend needed). One ↑/↓ cursor walks all three (see updateAsBoxes);
// with the cursor on a JOB row the Subagents box takes the height. A kind the session has
// none of renders as a slim placeholder box below the content boxes.
func (m Model) viewAsBoxes() string {
	s, ok := m.focusedSession()
	if !ok {
		return m.boardFallback(
			faintStyle.Render("(no live agents — none spawned, and no subagent jobs)"))
	}
	header := indentBox(m.renderSessionHeader(s), boardMargin) + "\n" + m.headerRule() + "\n"
	runsBody, jobsBody := m.splitBoxHeights(s)
	innerW := m.boardInner() - 6
	// Content boxes keep the canonical order; the empty placeholders sink below them
	// (an empty kind never carries cursor rows, so the continuum order is unaffected).
	var parts, empties []string
	if len(s.runs) > 0 {
		parts = append(parts, indentBox(m.renderRunsBox(s, runsBody), boardMargin))
	} else {
		empties = append(empties, indentBox(emptyKindBox("Dynamic Workflows", innerW), boardMargin))
	}
	if len(s.jobs) > 0 {
		parts = append(parts, indentBox(m.renderJobsBoxDetail(s, jobsBody), boardMargin))
	} else {
		empties = append(empties, indentBox(emptyKindBox("Subagents · 0", innerW), boardMargin))
	}
	parts = append(parts, empties...)
	return header + strings.Join(parts, "\n")
}

// emptyKindBox is the slim placeholder a session view shows for a kind it has none of, so
// the three-box silhouette never reshapes.
func emptyKindBox(title string, innerW int) string {
	return renderBox(title, []string{faintStyle.Render("(none in this session)")}, innerW, 1)
}

// splitBoxHeights divides the body budget across the session's two boxes (an empty kind
// shows a slim one-row placeholder): the Dynamic Workflows box fits its run rail and the
// previewed run's phases up to a quarter; the Subagents box takes the rest. The second box
// costs its two border rows.
func (m Model) splitBoxHeights(s asSession) (runs, jobs int) {
	avail := m.boardBodyHeight() - 2 // two boxes always render
	if avail < 6 {
		avail = 6
	}
	runs = 1
	if len(s.runs) > 0 {
		need := len(s.runs)
		if ri := m.boxRunIdx(s); ri >= 0 && len(s.runs[ri].phases) > need {
			need = len(s.runs[ri].phases)
		}
		runs = need
		if cap := avail / 4; runs > cap {
			runs = cap
		}
		if runs < 1 {
			runs = 1
		}
	}
	avail -= runs
	jobs = 1
	if len(s.jobs) > 0 {
		jobs = avail
	}
	return runs, jobs
}

// boxRunIdx is the run the Dynamic Workflows box previews: the cursored run while the L2
// cursor sits in the runs range, else the last run (the cursor moved below the box).
func (m Model) boxRunIdx(s asSession) int {
	if len(s.runs) == 0 {
		return -1
	}
	if m.asBoxCursor < len(s.runs) {
		return m.asBoxCursor
	}
	return len(s.runs) - 1
}

// renderRunsBox is the Dynamic Workflows master-detail box: run rail (newest first) | the
// previewed run's phase rows; ⏎ opens the cursored run's Phases level.
func (m Model) renderRunsBox(s asSession, bodyH int) string {
	listTitle := fmt.Sprintf("Dynamic Workflows · %d", len(s.runs))
	var leftLines []string
	for i, g := range s.runs {
		marker := "  "
		name := trunc(m.runLabel(g), 32)
		if i == m.asBoxCursor {
			marker = cursorStyle.Render("❯ ")
			name = selectedStyle.Render(name)
		} else {
			name = labelStyle(g.status).Render(name)
		}
		done, total := runAgentCounts(g)
		leftLines = append(leftLines, fmt.Sprintf("%s%s %s  %s%s", marker, statusDot(g.status), name,
			faintStyle.Render(fmt.Sprintf("%d/%d", done, total)), m.pinSuffix(pinned.Run, g.runID)))
	}
	leftW, rightW := m.paneWidths(leftWidth(listTitle, leftLines, m.boardInner()))
	rightTitle := "phases"
	var rightLines []string
	if ri := m.boxRunIdx(s); ri >= 0 {
		g := s.runs[ri]
		done, total := runAgentCounts(g)
		rightTitle = fmt.Sprintf("%s · %d/%d agents", trunc(m.runLabel(g), 24), done, total)
		for i, p := range g.phases {
			rightLines = append(rightLines, phaseRow(p, i, false))
		}
	}
	if len(rightLines) == 0 {
		rightLines = []string{faintStyle.Render("(no phases)")}
	}
	leftLines = windowLines(leftLines, m.boxRunIdx(s), bodyH)
	return renderBoard(listTitle, leftLines, rightTitle, rightLines, leftW, rightW, bodyH, 0)
}

// jobsBoxLeftLines is the Subagents box's compact job rail (dot + label-or-short-id), cursor-marked
// when the L2 continuum sits in the jobs range — shared by renderJobsBoxDetail and
// jobsBoxRightWidth so the scroll clamp and the render agree on the card width.
func (m Model) jobsBoxLeftLines(s asSession) []string {
	cursor := m.asBoxCursor - len(s.runs)
	var lines []string
	for i, j := range s.jobs {
		marker := "  "
		label := jobRowLabel(j)
		if i == cursor {
			marker = cursorStyle.Render("❯ ")
			label = selectedStyle.Render(label)
		} else {
			label = labelStyle(j.Status).Render(label)
		}
		lines = append(lines, marker+statusDot(j.Status)+" "+label+m.pinSuffix(pinned.Job, j.JobID))
	}
	return lines
}

// jobsBoxRightWidth is the inline job card's column budget in the Subagents box.
func (m Model) jobsBoxRightWidth(s asSession) int {
	title := fmt.Sprintf("Subagents · %d", len(s.jobs))
	_, rightW := m.paneWidths(leftWidth(title, m.jobsBoxLeftLines(s), m.boardInner()))
	return rightW
}

// renderJobsBoxDetail is the Subagents box: a master-detail of the job rail | the
// PREVIEWED job's inline card (the cursored job, else the first — a job's detail is always
// visible, never behind a descend). j/k scrolling applies once the cursor sits in the
// jobs range.
func (m Model) renderJobsBoxDetail(s asSession, bodyH int) string {
	listTitle := fmt.Sprintf("Subagents · %d", len(s.jobs))
	leftLines := m.jobsBoxLeftLines(s)
	leftW, rightW := m.paneWidths(leftWidth(listTitle, leftLines, m.boardInner()))
	cardTitle := "job"
	var rightLines []string
	if j, ok := m.boxPreviewJob(); ok {
		cardTitle = jobRowLabel(j)
		rightLines = m.jobDetailLines(j, rightW)
	}
	scroll := 0
	cursor := m.asBoxCursor - len(s.runs)
	if cursor >= 0 {
		scroll = m.clampAsCardScroll(m.asCardScroll)
	} else {
		cursor = 0 // window the rail on the previewed first job
	}
	leftLines = windowLines(leftLines, cursor, bodyH)
	return renderBoard(listTitle, leftLines, cardTitle, rightLines, leftW, rightW, bodyH, scroll)
}

// renderBox draws a full-width single-pane rounded box with a title segment — the
// one-column sibling of renderBoard.
func renderBox(title string, lines []string, innerW, bodyH int) string {
	var b strings.Builder
	head := "  " + title + " "
	fill := innerW + 4 - ansi.StringWidth(head)
	if fill < 0 {
		fill = 0
	}
	b.WriteString(borderStyle.Render("╭"+head+strings.Repeat("─", fill)+"╮") + "\n")
	bar := borderStyle.Render("│")
	for i := 0; i < bodyH; i++ {
		l := ""
		if i < len(lines) {
			l = lines[i]
		}
		b.WriteString(bar + "  " + boxCell(l, innerW) + "  " + bar + "\n")
	}
	b.WriteString(borderStyle.Render("╰" + strings.Repeat("─", innerW+4) + "╯"))
	return b.String()
}

// renderJobRowFull is one subagent-job row (right pane): "<dot> <label|jobID>  <model>" left,
// "<status> · <elapsed>" right-aligned — status columns only, NEVER answer text.
func (m Model) renderJobRowFull(j dsjobs.Result, width int) string {
	left := statusDot(j.Status) + " " +
		labelStyle(j.Status).Render(jobRowLabel(j))
	if model := sessiontitle.CleanTitle(j.Model); model != "" {
		left += "  " + faintStyle.Render(trunc(model, 22))
	}
	metrics := j.Status
	if metrics == "" {
		metrics = "unknown"
	}
	if el := jobElapsed(j); el != "" {
		metrics += " · " + el
	}
	return joinRowEnds(left, faintStyle.Render(metrics), width)
}

// jobElapsed is the job's wall-clock: the recorded duration once terminal, a live
// since-StartedAt while in progress; "" when unparseable — and never a live, growing figure
// for a terminal job whose record carries no duration.
func jobElapsed(j dsjobs.Result) string {
	if d := leafDuration(j); d != "" {
		return d
	}
	if isTerminalLeaf(j.Status) {
		return ""
	}
	start, err := time.Parse(time.RFC3339, j.StartedAt)
	if err != nil {
		return ""
	}
	d := time.Since(start)
	if d < 0 {
		d = 0
	}
	if d < time.Minute {
		return fmt.Sprintf("%ds", int(d.Seconds()))
	}
	return fmt.Sprintf("%dm %ds", int(d/time.Minute), int(d.Seconds())%60)
}

// asEntityLeftLines builds the COMPACT entity list for the L3 left rail (dot + name only),
// plus its title — shared by viewAsEntity and asEntityRightWidth so the scroll clamp and the
// render agree on the right pane width (mirror agentLeftLines).
func (m Model) asEntityLeftLines() (title string, lines []string) {
	jobs := m.railEntities()
	{
		title = fmt.Sprintf("Subagents · %d", len(jobs))
		for i, j := range jobs {
			marker := "  "
			label := jobRowLabel(j)
			if i == m.asEntityCursor {
				marker = cursorStyle.Render("❯ ")
				label = selectedStyle.Render(label)
			} else {
				label = labelStyle(j.Status).Render(label)
			}
			lines = append(lines, marker+statusDot(j.Status)+" "+label+m.pinSuffix(pinned.Job, j.JobID))
		}
		return title, lines
	}
}

// asEntityRightWidth is the L3 right pane width (mirror wfAgentRightWidth) so the scroll
// clamp wraps the detail to the same column budget the render uses.
func (m Model) asEntityRightWidth() int {
	title, lines := m.asEntityLeftLines()
	_, rightW := m.paneWidths(leftWidth(title, lines, m.boardInner()))
	return rightW
}

// viewAsEntity is L3: the session header above one box — "entity list | the focused
// entity's inline detail card" (the right pane scrolls with j/k via asCardScroll).
func (m Model) viewAsEntity() string {
	s, ok := m.focusedSession()
	if !ok {
		return m.boardFallback(faintStyle.Render("(no live agents)"))
	}
	listTitle, leftLines := m.asEntityLeftLines()
	leftW, rightW := m.paneWidths(leftWidth(listTitle, leftLines, m.boardInner()))
	cardTitle := "detail"
	if j, jok := m.selectedJob(); jok {
		cardTitle = jobRowLabel(j)
	}
	rightLines := m.entityDetailLines(rightW)
	bodyH := m.asEntityBodyHeight()
	leftLines = windowLines(leftLines, m.asEntityCursor, bodyH)
	board := indentBox(renderBoard(listTitle, leftLines, cardTitle, rightLines, leftW, rightW, bodyH, m.clampAsCardScroll(m.asCardScroll)), boardMargin)
	// A single-kind session lands here directly (the boxes level is skipped): keep the
	// two-box silhouette, with the slim placeholder for the missing runs BELOW the content
	// (a single-kind session has no runs by definition).
	if _, single := singleKindSrc(s); single {
		innerW := m.boardInner() - 6
		board = board + "\n" + indentBox(emptyKindBox("Dynamic Workflows", innerW), boardMargin)
	}
	return indentBox(m.renderSessionHeader(s), boardMargin) + "\n" + m.headerRule() + "\n" + board
}

// asEntityBodyHeight is the entity box's row budget: the full body, minus the slim
// placeholder box a single-kind session shows for its missing runs (two borders + one row).
func (m Model) asEntityBodyHeight() int {
	if s, ok := m.focusedSession(); ok {
		if _, single := singleKindSrc(s); single {
			h := m.boardBodyHeight() - 3
			if h < 5 {
				h = 5
			}
			return h
		}
	}
	return m.boardBodyHeight()
}

// entityDetailLines is the focused entity's inline detail card (the L3 right pane): the job card
// renders from the already-loaded Result — usage/cost/turns appear once terminal, and a
// failure shows the canonical ErrorCode ONLY (ErrorMsg never renders on this board).
func (m Model) entityDetailLines(rightW int) []string {
	if j, ok := m.selectedJob(); ok {
		return m.jobDetailLines(j, rightW)
	}
	return []string{faintStyle.Render("(no row)")}
}

// detailField renders one "key  value" card line set, wrapping a long value to the pane
// (continuation lines indent under the value column) — the card never truncates a field.
func detailField(lines *[]string, k, v string, rightW int) {
	detailFieldStyled(lines, k, v, rightW, contentStyle)
}

// detailFieldStyled is detailField with a caller-chosen value style (e.g. the
// permission-mode color on the run-perm row).
func detailFieldStyled(lines *[]string, k, v string, rightW int, style lipgloss.Style) {
	if v == "" {
		v = "—"
	}
	for fi, w := range wrapTo(v, rightW-12) {
		key := fmt.Sprintf("%-8s", k)
		if fi > 0 {
			key = strings.Repeat(" ", 8)
		}
		*lines = append(*lines, " "+faintStyle.Render(key)+"  "+style.Render(w))
	}
}

// detailKV is one Overview field, paired two-per-line by detailFieldPairs.
type detailKV struct{ k, v string }

// detailFieldPairs renders Overview fields two per line (halving the card's field-block
// height); each value truncates to its half-cell — long free-text fields stay on
// detailField's full-width wrapping lines instead. An odd tail leaves the right cell empty.
func detailFieldPairs(lines *[]string, fields []detailKV, rightW int) {
	half := (rightW - 1) / 2
	cell := func(f detailKV, w int) string {
		if f.k == "" {
			return strings.Repeat(" ", w)
		}
		v := f.v
		if v == "" {
			v = "—"
		}
		return boxCell(" "+faintStyle.Render(fmt.Sprintf("%-8s", f.k))+"  "+contentStyle.Render(truncCols(v, w-12)), w)
	}
	for i := 0; i < len(fields); i += 2 {
		right := detailKV{}
		if i+1 < len(fields) {
			right = fields[i+1]
		}
		*lines = append(*lines, cell(fields[i], half)+" "+cell(right, rightW-half-1))
	}
}

func (m Model) jobDetailLines(j dsjobs.Result, rightW int) []string {
	status := statusLabel(j.Status)
	if model := sessiontitle.CleanTitle(j.Model); model != "" {
		status += faintStyle.Render(" · " + trunc(model, 28))
	}
	if j.Attempt > 1 {
		// >1 occurs only in records from engines that retried schema mismatches; surface it.
		status += faintStyle.Render(fmt.Sprintf(" · attempt %d", j.Attempt))
	}
	lines := []string{status, "", faintStyle.Render("Overview")}
	// Two fields per line (the status line already carries the model). The job id keeps
	// its own full-width wrapping line — it is the one field that must stay findable.
	detailField(&lines, "job", sessiontitle.CleanTitle(j.JobID), rightW)
	var fields []detailKV
	if profile := sessiontitle.CleanTitle(j.PromptProfile); profile != "" {
		if d := sessiontitle.CleanTitle(j.SlimDowngrade); d != "" {
			profile += " (ran full: " + d + ")"
		}
		fields = append(fields, detailKV{"profile", profile})
	}
	started := sessiontitle.CleanTitle(j.StartedAt)
	if ts, err := time.Parse(time.RFC3339, j.StartedAt); err == nil {
		started = ts.Format("01-02 15:04")
	}
	fields = append(fields, detailKV{"started", started})
	if el := jobElapsed(j); el != "" {
		fields = append(fields, detailKV{"elapsed", el})
	}
	if in, out := m.jobTokens(j); in > 0 || out > 0 {
		fields = append(fields, detailKV{"tokens", fmt.Sprintf("↑ %s · ↓ %s", humanTokens(in), humanTokens(out))})
	}
	if isTerminalLeaf(j.Status) {
		if j.CostUSD > 0 {
			fields = append(fields, detailKV{"cost", fmt.Sprintf("~$%.4f est", j.CostUSD)})
		}
		if j.NumTurns > 0 {
			fields = append(fields, detailKV{"turns", fmt.Sprintf("%d", j.NumTurns)})
		}
		if stop := sessiontitle.CleanTitle(j.StopReason); stop != "" {
			fields = append(fields, detailKV{"stop", stop})
		}
	}
	detailFieldPairs(&lines, fields, rightW)
	// Prompt → Activity → Output → Outcome, the wf agent card's order, fed from the
	// nonce-gated io load. The Output reads the .answer side file (focused-single-job
	// surface, CleanTitle-scrubbed) — NEVER Result.Result.
	lines = append(lines, "")
	switch {
	case m.asDetailJobID != j.JobID:
		lines = append(lines, faintStyle.Render("(loading…)"))
	case !m.asDetailIO:
		lines = append(lines, faintStyle.Render("(prompt/output not persisted — run with default persist-io)"))
	default:
		lines = append(lines, promptSection(m.asDetailPrompt, m.asPromptExpanded, rightW)...)
	}
	if m.asDetailJobID == j.JobID {
		lines = append(lines, "")
		lines = append(lines, activityLines(m.asDetailSnap, rightW)...)
		if m.asDetailIO {
			lines = append(lines, "", faintStyle.Render("Output"))
			lines = append(lines, ioLines(m.asDetailAnswer, rightW, liveStyle)...)
		}
	}
	lines = append(lines, "", faintStyle.Render("Outcome"), " "+m.renderOutcome(j))
	return lines
}

// runGroup is a workflow run with its jobs bucketed by phase, ready to render.
type runGroup struct {
	runID       string
	name        string
	description string
	sessionID   string // launching Claude session (the board grouping key); "" when launched outside one
	cwd         string // launching project dir (shown right-aligned on the run header); "" when unknown
	status      string
	startedAt   string
	updatedAt   string
	phases      []runPhaseGroup
}

// elapsed renders the run's wall-clock from StartedAt. A running run ticks live to now (the board
// re-renders each frame); a terminal run freezes at its last heartbeat (UpdatedAt) so the duration
// stops growing once it ends. A run with no parseable StartedAt renders "—".
func (g runGroup) elapsed() string {
	start, err := time.Parse(time.RFC3339, g.startedAt)
	if err != nil {
		return "—"
	}
	end := time.Now()
	if g.status != "running" && g.updatedAt != "" {
		if u, uerr := time.Parse(time.RFC3339, g.updatedAt); uerr == nil {
			end = u
		}
	}
	d := end.Sub(start)
	if d < 0 {
		d = 0
	}
	return d.Round(time.Second).String()
}

// runPhaseGroup is one phase of a run with the jobs observed in it.
type runPhaseGroup struct {
	title string
	jobs  []dsjobs.Result
}

// groupByRun joins RunID-tagged jobs to their run manifests into a run→phase→job
// tree. A run's manifest supplies its Name/Status/StartedAt and the declared
// phase order; phases observed on a job but absent from the manifest are appended
// in first-seen order. A run with no manifest (GC'd or never created) carries an
// empty name and phases in first-seen order. Runs sort newest-first by StartedAt
// (the manifest's, else the earliest job StartedAt) — an RFC3339 string compare,
// whose lexicographic order matches chronological order for the fixed-width format.
func groupByRun(jobs []dsjobs.Result, runs []dsjobs.WorkflowRun) []runGroup {
	byRunID := map[string]dsjobs.WorkflowRun{}
	for _, r := range runs {
		byRunID[r.RunID] = r
	}

	// Assemble groups in first-seen order (manifest first, then jobs), so a run is
	// created even when it has a manifest but zero jobs yet (phase skeleton).
	order := []string{}
	groups := map[string]*runGroup{}
	phaseIdx := map[string]map[string]int{} // runID → phase title → index into phases

	ensureRun := func(runID string) *runGroup {
		g, ok := groups[runID]
		if ok {
			return g
		}
		g = &runGroup{runID: runID}
		if r, ok := byRunID[runID]; ok {
			g.name = r.Name
			g.description = r.Description
			g.sessionID = r.SessionID
			g.cwd = r.Cwd
			g.status = r.Status
			g.startedAt = r.StartedAt
			g.updatedAt = r.UpdatedAt
		}
		groups[runID] = g
		phaseIdx[runID] = map[string]int{}
		order = append(order, runID)
		return g
	}
	ensurePhase := func(g *runGroup, title string) int {
		idx := phaseIdx[g.runID]
		if i, ok := idx[title]; ok {
			return i
		}
		i := len(g.phases)
		g.phases = append(g.phases, runPhaseGroup{title: title})
		idx[title] = i
		return i
	}

	// Manifest-declared runs first: this both seeds the manifest phase order and
	// renders a freshly-created run's phase skeleton before any job lands.
	for _, r := range runs {
		g := ensureRun(r.RunID)
		for _, p := range r.Phases {
			ensurePhase(g, p.Title)
		}
	}

	// Then the jobs: their run may have no manifest, and their phase may be a
	// manifest-absent extra (appended after the declared phases).
	for _, j := range jobs {
		g := ensureRun(j.RunID)
		i := ensurePhase(g, j.Phase)
		g.phases[i].jobs = append(g.phases[i].jobs, j)
		// For a run with no manifest, derive its sort key from the earliest job
		// StartedAt. A manifested run already carries the manifest's StartedAt.
		if _, hasManifest := byRunID[j.RunID]; !hasManifest && j.StartedAt != "" {
			if g.startedAt == "" || j.StartedAt < g.startedAt {
				g.startedAt = j.StartedAt
			}
		}
	}

	out := make([]runGroup, 0, len(order))
	for _, id := range order {
		g := *groups[id]
		for i := range g.phases {
			g.phases[i].jobs = dedupePhaseJobs(g.phases[i].jobs)
		}
		out = append(out, g)
	}
	// Newest-first by StartedAt; empty StartedAt sorts last, first-seen order as
	// the stable tiebreaker.
	sort.SliceStable(out, func(i, j int) bool {
		a, b := out[i].startedAt, out[j].startedAt
		if a != b {
			if a == "" {
				return false
			}
			if b == "" {
				return true
			}
			return a > b
		}
		return false
	})
	// Then group sessions contiguously (a session ranked by its newest run), preserving the
	// newest-first order within each — so each session's runs stay adjacent in the tree.
	newestPerSession := map[string]string{}
	for _, g := range out {
		if g.startedAt > newestPerSession[g.sessionID] {
			newestPerSession[g.sessionID] = g.startedAt
		}
	}
	sort.SliceStable(out, func(i, j int) bool {
		return newestPerSession[out[i].sessionID] > newestPerSession[out[j].sessionID]
	})
	return out
}

// dedupePhaseJobs collapses re-run leaves: within a phase, jobs sharing a non-empty Label keep only the
// newest by StartedAt (a single-leaf restart mints a fresh jobID; the old job lingers until GC). The
// slot identity is (Phase, Label), NOT the JournalKey, on purpose: a cascaded downstream re-run gets a
// NEW key (its input shifted) but keeps its label, so key-dedup would leave it doubled. The cost is two
// leaves an author gave the SAME non-empty label collapse to one row — acceptable, since a sensible
// board needs unique labels per phase (the native board requires them). Empty-Label leaves have no
// stable identity, so they are kept as-is. Order is preserved.
func dedupePhaseJobs(jobs []dsjobs.Result) []dsjobs.Result {
	out := make([]dsjobs.Result, 0, len(jobs))
	idx := map[string]int{} // non-empty Label → index in out
	for _, j := range jobs {
		if j.Label == "" {
			out = append(out, j)
			continue
		}
		if k, ok := idx[j.Label]; ok {
			if jobNewer(j, out[k]) {
				out[k] = j
			}
			continue
		}
		idx[j.Label] = len(out)
		out = append(out, j)
	}
	return out
}

// jobNewer reports whether a started strictly after b (StartedAt parsed as time, so a precision or
// format difference doesn't mis-rank). Unparseable timestamps sort as the zero time (oldest).
func jobNewer(a, b dsjobs.Result) bool {
	ta, _ := time.Parse(time.RFC3339, a.StartedAt)
	tb, _ := time.Parse(time.RFC3339, b.StartedAt)
	return ta.After(tb)
}

func (m Model) sessionLabel(id string) string {
	if id == "" {
		return "(no session)"
	}
	// A codex-launched job/run carries a "codex:<thread>" launcher id (no Claude session,
	// no /rename title); the label is just the thread id — the launcher shows visually via
	// sessionMarker (a violet ◇) and the header's launcher tag, not a text prefix.
	if thread, ok := strings.CutPrefix(id, "codex:"); ok {
		short := shortSessionID(sessiontitle.CleanTitle(thread))
		// Show the thread's first-prompt title (resolved into sessionMeta, like `codex resume`)
		// as "title (id)" — matching a claude session's shape; bare id when none is resolved.
		if title := sessiontitle.CleanTitle(m.sessionMeta[id].Title); title != "" {
			return trunc(title, 48) + " (" + short + ")"
		}
		return short
	}
	// Scrub both the opaque session id and any /rename title with CleanTitle so the board header
	// strips ANSI/BEL/OSC control bytes (not just whitespace) before display.
	clean := sessiontitle.CleanTitle(id)
	if title := sessiontitle.CleanTitle(m.sessionMeta[id].Title); title != "" {
		return trunc(title, 48) + " (" + shortSessionID(clean) + ")"
	}
	// No recorded title: the full id IS the label — an 8-char stub would waste the slot.
	return clean
}

// sessionMarker is a session row's per-launcher glyph, so a row stays identifiable when the
// divider is off-screen: a hollow violet "◇" for codex, the filled blue "◆" for claude, and a
// dim "◆" for a launcher-unknown "(no session)" row (so it isn't mistaken for the claude blue).
func sessionMarker(id string) string {
	switch {
	case strings.HasPrefix(id, "codex:"):
		return codexHdrStyle.Render("◇ ")
	case id == "":
		return faintStyle.Render("◆ ")
	default:
		return sessionHdrStyle.Render("◆ ")
	}
}

// sessionLauncherTag names a session's launcher for a header that draws no ◆/◇ marker:
// "codex", "claude", or "" for "(no session)" (whose label already says so).
func sessionLauncherTag(id string) string {
	switch {
	case id == "":
		return ""
	case strings.HasPrefix(id, "codex:"):
		return "codex"
	default:
		return "claude"
	}
}

// sessionTitleWithLauncher is a session's label for a box-frame / session header, prefixed
// with its launcher tag (codex / claude) — those headers carry no ◆/◇ marker, so the tag is
// where the launcher is named.
func (m Model) sessionTitleWithLauncher(id string) string {
	if tag := sessionLauncherTag(id); tag != "" {
		return tag + " · " + m.sessionLabel(id)
	}
	return m.sessionLabel(id)
}

func shortSessionID(id string) string {
	if len(id) > 8 {
		return id[:8] + "…"
	}
	return id
}

// jobRowLabel is a job's display name on the board: its --label when one was given,
// else the short job id.
func jobRowLabel(j dsjobs.Result) string {
	if l := trunc(sessiontitle.CleanTitle(j.Label), 24); l != "" {
		return l
	}
	return shortJobID(sessiontitle.CleanTitle(j.JobID))
}

// shortJobID trims a job UUID to its first 8 chars for the board's job rows.
func shortJobID(id string) string {
	if len(id) > 8 {
		return id[:8]
	}
	return id
}

// setupOptions are the three choices on the provider-teammates setup nudge, in
// cursor order (index 0 = "enable it for me", handled specially by updateSetup).
var setupOptions = []string{
	"enable it for me  (writes ~/.claude/settings.json, sets tmux split panes)",
	"I've set it up myself",
	"skip — I'll only use subagent / workflow / run",
}

// windowBounds returns the [start,end) slice of indices to render so the cursor
// stays visible when a list of n items is longer than max.
func windowBounds(cursor, n, max int) (int, int) {
	if n <= max {
		return 0, n
	}
	start := cursor - max/2
	if start < 0 {
		start = 0
	}
	end := start + max
	if end > n {
		end = n
		start = end - max
	}
	return start, end
}

// trunc shortens s to n runes, appending "…" when it had to cut.
func trunc(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	if n <= 1 {
		return string(r[:n])
	}
	return string(r[:n-1]) + "…"
}
