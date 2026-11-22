// Derived from cc-fleet internal/pinned/pinned.go (Apache-2.0); modified.

// Package pinned is an out-of-band registry of the board records a user has chosen
// to keep. A pin is a zero-byte marker file at STATE/pins/<id> (ds-bridge: one flat
// dir for jobs and batches; batch ids start with "b", so they never collide); it
// carries no content. The registry is deliberately separate from the record stores
// (subagent-jobs, teams-history) so the record writers — a board refresh's
// teamhist.Upsert, the engine's manifest SaveRun, a job's writeMeta — never touch a
// pin and so can never clobber it. GC and the board consult the registry to skip /
// mark pinned records; an explicit per-record delete (board `d`) Unpins, and
// uninstall Purges the whole dir.
package pinned

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// pinnedDirName is the registry dir under the ds state dir (ds's gc_landed reads the same dir).
const pinnedDirName = "pins"

// StateDir is the ds state dir (~/.local/share/ds-bridge, or --state). Set by main.
var StateDir string

// Kind namespaces a pin by record type so a job, a run, and a team can share an id
// without colliding (each lives under its own subdir).
type Kind string

const (
	Job Kind = "job"
	Run Kind = "run"
)

func validKind(k Kind) bool { return k == Job || k == Run }

// validID keeps a marker inside the pins dir (the path-component rules of cc-fleet's ids.validateID).
func validID(_ Kind, id string) error {
	switch {
	case id == "", id == ".", id == "..":
		return errors.New("empty or dot")
	case len(id) > 128:
		return errors.New("too long")
	case strings.ContainsAny(id, "/\\ \t\r\n\v\f\x00"):
		return errors.New("must not contain separators, whitespace or NUL")
	case strings.IndexFunc(id, func(r rune) bool {
		return !(r == '.' || r == '-' || r == '_' || r >= '0' && r <= '9' || r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z')
	}) >= 0:
		return errors.New("only letters, digits, '.', '-' and '_'")
	case filepath.Clean(id) != id:
		return errors.New("must equal filepath.Clean(id)")
	}
	return nil
}

// kindDir is the same flat dir for every kind (ds-bridge).
func kindDir(_ Kind) (string, error) {
	if StateDir == "" {
		return "", errors.New("pinned: state dir not set")
	}
	return filepath.Join(StateDir, pinnedDirName), nil
}

// atomicWrite writes data to path via a temp file + rename (stands in for cc-fleet's fileutil).
func atomicWrite(path string, data []byte, mode os.FileMode) error {
	tmp, err := os.CreateTemp(filepath.Dir(path), ".pin-*")
	if err != nil {
		return err
	}
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		os.Remove(tmp.Name())
		return err
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmp.Name())
		return err
	}
	if err := os.Chmod(tmp.Name(), mode); err != nil {
		os.Remove(tmp.Name())
		return err
	}
	return os.Rename(tmp.Name(), path)
}

func markerPath(k Kind, id string) (string, error) {
	if !validKind(k) {
		return "", fmt.Errorf("pinned: invalid kind %q", k)
	}
	if err := validID(k, id); err != nil {
		return "", fmt.Errorf("pinned: invalid id: %w", err)
	}
	dir, err := kindDir(k)
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, id), nil
}

// Pin marks (kind,id) as user-kept. Idempotent (re-pinning rewrites the empty marker).
func Pin(kind Kind, id string) error {
	path, err := markerPath(kind, id)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	return atomicWrite(path, nil, 0o600)
}

// Unpin clears (kind,id)'s mark. A missing marker is not an error.
func Unpin(kind Kind, id string) error {
	path, err := markerPath(kind, id)
	if err != nil {
		return err
	}
	if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}

// Set is an in-memory snapshot of the registry — a map lookup so a GC sweep or a board
// refresh can test many records without one stat per record. The zero Set has no pins
// (Has always false), so a caller that fails to snapshot degrades safely to "nothing
// pinned" only where that is the intended fallback.
type Set struct {
	m map[Kind]map[string]bool
}

// Has reports whether (kind,id) was pinned when the snapshot was taken.
func (s Set) Has(kind Kind, id string) bool {
	return s.m[kind][id]
}

// With returns a copy of the set with (kind,id) pinned (add=true) or unpinned (add=false). The
// board applies it optimistically when it dispatches a pin toggle, so two quick presses before the
// registry write + reload land still alternate instead of both pinning off the same stale snapshot.
func (s Set) With(kind Kind, id string, add bool) Set {
	out := Set{m: map[Kind]map[string]bool{}}
	for k, ids := range s.m {
		cp := make(map[string]bool, len(ids))
		for i := range ids {
			cp[i] = true
		}
		out.m[k] = cp
	}
	if out.m[kind] == nil {
		out.m[kind] = map[string]bool{}
	}
	if add {
		out.m[kind][id] = true
	} else {
		delete(out.m[kind], id)
	}
	return out
}

// Snapshot reads the whole registry into a Set. A missing registry (or a missing kind
// subdir) yields an empty set for that kind — everything reads as unpinned.
func Snapshot() (Set, error) {
	s := Set{m: map[Kind]map[string]bool{}}
	for _, k := range []Kind{Job, Run} {
		ids := map[string]bool{}
		s.m[k] = ids
		dir, err := kindDir(k)
		if err != nil {
			return s, err
		}
		entries, err := os.ReadDir(dir)
		if err != nil {
			if errors.Is(err, os.ErrNotExist) {
				continue
			}
			return s, fmt.Errorf("pinned: read %s dir: %w", k, err)
		}
		for _, e := range entries {
			if !e.IsDir() {
				ids[e.Name()] = true
			}
		}
	}
	return s, nil
}
