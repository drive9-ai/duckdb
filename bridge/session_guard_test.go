package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"sync"
	"testing"
	"time"

	jfsmeta "github.com/juicedata/juicefs/pkg/meta"

	"github.com/mem9-ai/drive9/pkg/client"
	"github.com/mem9-ai/drive9/pkg/extent"
)

// Exercise the shared SDK and real HTTP endpoint. No runtime heartbeat is
// started: this test controls the existing session lifecycle explicitly.
func TestLiveExtentDelayedPublicationAfterSessionCleanup(t *testing.T) {
	endpoint, token := os.Getenv("DRIVE9_BASE"), os.Getenv("DRIVE9_API_KEY")
	if endpoint == "" || token == "" {
		t.Skip("requires disposable live filesystem")
	}
	c := client.New(endpoint, token)
	call := func(tr *extent.Transport, op string, req any) map[string]json.RawMessage {
		t.Helper()
		var out map[string]json.RawMessage
		if st := tr.Call(jfsmeta.Background(), op, req, &out); st != 0 {
			t.Fatalf("%s: %v", op, st)
		}
		return out
	}
	errno := func(out map[string]json.RawMessage) int {
		t.Helper()
		var n int
		if err := json.Unmarshal(out["errno"], &n); err != nil {
			t.Fatal(err)
		}
		return n
	}
	ok := func(tr *extent.Transport, op string, req any) map[string]json.RawMessage {
		t.Helper()
		out := call(tr, op, req)
		if n := errno(out); n != 0 {
			t.Fatalf("%s errno=%d", op, n)
		}
		return out
	}
	admin := extent.NewHTTPTransport(c)
	counter := func(name string) uint64 {
		t.Helper()
		out := ok(admin, "incr_counter", map[string]any{"name": name, "value": 1})
		var value uint64
		if err := json.Unmarshal(out["value"], &value); err != nil {
			t.Fatal(err)
		}
		return value
	}
	oldSID, newSID := counter("nextSession"), counter("nextSession")
	old := extent.NewHTTPTransport(c)
	ok(old, "new_session", map[string]any{"sid": oldSID, "expire": time.Now().Unix() + 60})
	if !old.SessionGuardSupported() {
		if os.Getenv("TIDBFS_REQUIRE_SESSION_GUARD") == "1" {
			t.Fatal("server did not advertise session_guard")
		}
		t.Skip("legacy server does not advertise transactional session guard")
	}
	ino := counter("nextInode")
	name := fmt.Sprintf("session-fence-%d", time.Now().UnixNano())
	ok(old, "mknod", map[string]any{"inode": ino, "parent": 1, "name": name, "type": 1, "mode": 0600,
		"proj_path": "/" + name, "attr": map[string]any{"Typ": 1, "Mode": 0600, "Nlink": 1, "Parent": 1, "Full": true}})
	t.Cleanup(func() {
		_, _ = c.ExtentMeta(context.Background(), "unlink", map[string]any{"parent": 1, "name": name, "proj_path": "/" + name})
	})
	ok(old, "flock", map[string]any{"sid": oldSID, "inode": ino, "owner": 1, "ltype": uint32('W')})

	captured, release := make(chan struct{}), make(chan struct{})
	var once sync.Once
	unblock := func() { once.Do(func() { close(release) }) }
	t.Cleanup(unblock)
	original := old.CallFn
	old.CallFn = func(ctx context.Context, op string, raw json.RawMessage) (json.RawMessage, int, error) {
		if op == "write" {
			close(captured)
			<-release
		}
		return original(ctx, op, raw)
	}
	type result struct {
		status uint32
		body   map[string]json.RawMessage
	}
	done := make(chan result, 1)
	go func() {
		var out map[string]json.RawMessage
		st := old.Call(jfsmeta.Background(), "write", map[string]any{"inode": ino, "slice": map[string]any{"Id": 0, "Size": 4, "Len": 4}}, &out)
		done <- result{uint32(st), out}
	}()
	select {
	case <-captured:
	case <-time.After(10 * time.Second):
		t.Fatal("write did not reach transport")
	}
	ok(admin, "refresh_session", map[string]any{"sid": oldSID, "expire": time.Now().Unix() - 1})
	ok(admin, "clean_stale_session", map[string]any{"sid": oldSID})
	current := extent.NewHTTPTransport(c)
	ok(current, "new_session", map[string]any{"sid": newSID, "expire": time.Now().Unix() + 60})
	ok(current, "flock", map[string]any{"sid": newSID, "inode": ino, "owner": 2, "ltype": uint32('W')})
	ok(current, "write", map[string]any{"inode": ino, "slice": map[string]any{"Id": 0, "Size": 8, "Len": 8}})
	before := ok(current, "read", map[string]any{"inode": ino})
	unblock()
	select {
	case got := <-done:
		if got.status != 0 || errno(got.body) == 0 {
			t.Fatalf("stale publication: status=%d body=%s", got.status, got.body["errno"])
		}
	case <-time.After(10 * time.Second):
		t.Fatal("delayed write did not complete")
	}
	after := ok(current, "read", map[string]any{"inode": ino})
	if string(before["slices"]) != string(after["slices"]) {
		t.Fatal("stale writer changed slice mapping")
	}
	if n := errno(call(old, "refresh_session", map[string]any{"sid": oldSID, "expire": time.Now().Unix() + 60})); n == 0 {
		t.Fatal("collected session revived")
	}
	ok(current, "flock", map[string]any{"sid": newSID, "inode": ino, "owner": 2, "ltype": uint32('U')})
}
