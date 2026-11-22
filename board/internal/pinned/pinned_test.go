// Derived from cc-fleet internal/pinned/pinned_test.go (Apache-2.0); modified.

package pinned

import (
	"testing"
)

func setup(t *testing.T) {
	t.Helper()
	StateDir = t.TempDir()
}

// has is the tests' point query: production reads pins via Snapshot()+Set.Has, so the tests do too.
func has(k Kind, id string) bool {
	s, _ := Snapshot()
	return s.Has(k, id)
}

func TestPinUnpinCycle(t *testing.T) {
	setup(t)
	if has(Run, "r1") {
		t.Fatal("fresh registry: nothing pinned")
	}
	if err := Pin(Run, "r1"); err != nil {
		t.Fatalf("Pin: %v", err)
	}
	if !has(Run, "r1") {
		t.Fatal("after Pin, the run should read pinned")
	}
	// Idempotent re-pin.
	if err := Pin(Run, "r1"); err != nil {
		t.Fatalf("re-Pin: %v", err)
	}
	if err := Unpin(Run, "r1"); err != nil {
		t.Fatalf("Unpin: %v", err)
	}
	if has(Run, "r1") {
		t.Fatal("after Unpin, the run should read unpinned")
	}
	// Unpin of an absent marker is not an error.
	if err := Unpin(Run, "r1"); err != nil {
		t.Fatalf("Unpin absent: %v", err)
	}
}

func TestSnapshotHas(t *testing.T) {
	setup(t)
	for _, p := range []struct {
		k  Kind
		id string
	}{{Job, "j1"}, {Run, "r1"}, {Run, "r2"}, {Job, "alpha"}} {
		if err := Pin(p.k, p.id); err != nil {
			t.Fatalf("Pin %s/%s: %v", p.k, p.id, err)
		}
	}
	set, err := Snapshot()
	if err != nil {
		t.Fatalf("Snapshot: %v", err)
	}
	for _, want := range []struct {
		k  Kind
		id string
	}{{Job, "j1"}, {Run, "r1"}, {Run, "r2"}, {Job, "alpha"}} {
		if !set.Has(want.k, want.id) {
			t.Errorf("Set.Has(%s,%s) = false, want true", want.k, want.id)
		}
	}
	if set.Has(Job, "nope") {
		t.Error("Set.Has must be false for unpinned / wrong-kind ids")
	}
}

func TestZeroSetHasIsFalse(t *testing.T) {
	var s Set // zero value — no map
	if s.Has(Run, "anything") {
		t.Fatal("zero Set.Has must be false (degrades to nothing-pinned)")
	}
}

func TestSnapshotMissingRegistry(t *testing.T) {
	setup(t)
	set, err := Snapshot()
	if err != nil {
		t.Fatalf("Snapshot on empty: %v", err)
	}
	if set.Has(Run, "r1") {
		t.Fatal("empty registry snapshot has no pins")
	}
}

func TestInvalidIDRejected(t *testing.T) {
	setup(t)
	for _, bad := range []string{"", ".", "..", "a/b", `a\b`, "../x", "%pane", "a b"} {
		if err := Pin(Run, bad); err == nil {
			t.Errorf("Pin(Run, %q) should reject an unsafe id", bad)
		}
	}
	// A leading-dot name is still a valid id.
	if err := Pin(Job, ".foo"); err != nil {
		t.Errorf("Pin(Job, \".foo\") should be accepted: %v", err)
	}
	if !has(Job, ".foo") {
		t.Error(".foo should be pinned")
	}
}
