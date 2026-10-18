package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"testing"
	"time"
)

func TestSessionStopsBeforeExpiryAndStaysFailed(t *testing.T) {
	s := &session{}
	s.setDeadline(time.Now().Unix() + 4)
	if s.healthy() == nil {
		t.Fatal("session continued into its expiry margin")
	}
	s.setDeadline(time.Now().Unix() + 60)
	if s.healthy() == nil {
		t.Fatal("failed session was revived")
	}
}

func TestLateMetadataAcknowledgementCannotReviveSession(t *testing.T) {
	for _, op := range []string{"write", "refresh_session"} {
		t.Run(op, func(t *testing.T) {
			s := &session{}
			s.setDeadline(time.Now().Unix() + 60)
			raw, _ := json.Marshal(map[string]int64{"expire": time.Now().Unix() + 120})
			calls := 0
			invoke := func(ctx context.Context, op string, raw json.RawMessage) ([]byte, error) {
				calls++
				// Model a request that began while valid but whose response arrives
				// after expiry. No sleeps or edits to production session rows.
				s.setDeadline(time.Now().Unix())
				return []byte(`{"errno":0}`), nil
			}
			if _, _, err := s.metadata(context.Background(), op, raw, invoke); err == nil {
				t.Fatal("accepted successful response after session expired")
			}
			s.setDeadline(time.Now().Unix() + 60)
			if _, _, err := s.metadata(context.Background(), op, raw, invoke); err == nil || calls != 1 {
				t.Fatalf("replayed ambiguous operation: calls=%d err=%v", calls, err)
			}
		})
	}
}

func TestMetadataDeadlineCancellationFailsSession(t *testing.T) {
	s := &session{}
	s.setDeadline(time.Now().Unix() + 60)
	ctx, cancel := context.WithCancel(context.Background())
	_, _, err := s.metadata(ctx, "write", nil, func(ctx context.Context, _ string, _ json.RawMessage) ([]byte, error) {
		if deadline, ok := ctx.Deadline(); !ok || time.Until(deadline) > 30*time.Second {
			t.Fatal("metadata request has no bounded deadline")
		}
		cancel()
		return []byte(`{"errno":0}`), nil
	})
	if !errors.Is(err, context.Canceled) || s.healthy() == nil {
		t.Fatalf("cancelled request was acknowledged: %v", err)
	}
}

func TestLiveExtentFileOperations(t *testing.T) {
	endpoint, token := os.Getenv("DRIVE9_BASE"), os.Getenv("DRIVE9_API_KEY")
	if endpoint == "" || token == "" {
		t.Skip("requires disposable live filesystem")
	}
	connect := func() uint64 {
		t.Helper()
		out, err := dispatch(request{Op: "connect", Endpoint: endpoint, Token: token})
		if err != nil {
			t.Fatal(err)
		}
		sid := out["session"].(uint64)
		t.Cleanup(func() { _, _ = dispatch(request{Op: "disconnect", Session: sid}) })
		return sid
	}
	a, b := connect(), connect()
	call := func(sid uint64, r request) map[string]any {
		t.Helper()
		r.Session = sid
		out, err := dispatch(r)
		if err != nil {
			t.Fatalf("%s: %v", r.Op, err)
		}
		return out
	}
	root := fmt.Sprintf("/extent-conformance-%d", time.Now().UnixNano())
	call(a, request{Op: "mkdir", Path: root})
	open := func(sid uint64, path string) uint64 {
		return call(sid, request{Op: "open", Path: path, Create: true, Write: true, Lock: "write"})["handle"].(uint64)
	}
	h := open(a, root+"/a")
	if _, err := dispatch(request{Op: "open", Session: b, Path: root + "/a", Write: true, Lock: "write"}); err == nil {
		t.Fatal("second writer acquired locked inode")
	}
	// Pin the existing limitation explicitly: advisory locks do not fence
	// another extent client that never requests a lock. Only disposable files
	// are used here; a live database must never have such a competing writer.
	unlocked := call(b, request{Op: "open", Path: root + "/a", Write: true})["handle"].(uint64)
	call(b, request{Op: "write", Handle: unlocked, Data: []byte("bypass")})
	call(b, request{Op: "close", Handle: unlocked})
	c := connect()
	reader := call(c, request{Op: "open", Path: root + "/a"})["handle"].(uint64)
	if data := call(c, request{Op: "read", Handle: reader, Length: 6})["data"].([]byte); string(data) != "bypass" {
		t.Fatalf("advisory-lock boundary changed: %q", data)
	}
	call(c, request{Op: "close", Handle: reader})
	call(a, request{Op: "truncate", Handle: h, Offset: 0})
	call(a, request{Op: "write", Handle: h, Offset: 8, Data: []byte{0, 255, 3}})
	data := call(a, request{Op: "read", Handle: h, Length: 20})["data"].([]byte)
	if !bytes.Equal(data, append(make([]byte, 8), 0, 255, 3)) {
		t.Fatalf("hole/binary read: %v", data)
	}
	call(a, request{Op: "truncate", Handle: h, Offset: 2})
	call(a, request{Op: "truncate", Handle: h, Offset: 12})
	data = call(a, request{Op: "read", Handle: h, Length: 12})["data"].([]byte)
	if !bytes.Equal(data, make([]byte, 12)) {
		t.Fatalf("truncate resurrected tail: %v", data)
	}
	call(a, request{Op: "write", Handle: h, Data: []byte("old")})
	other := open(b, root+"/b")
	call(b, request{Op: "write", Handle: other, Data: []byte("new")})
	call(b, request{Op: "rename", Path: root + "/b", Target: root + "/a"})
	data = call(a, request{Op: "read", Handle: h, Length: 3})["data"].([]byte)
	if string(data) != "old" {
		t.Fatalf("rename retargeted open inode: %q", data)
	}
	call(b, request{Op: "remove", Path: root + "/a"})
	data = call(b, request{Op: "read", Handle: other, Length: 3})["data"].([]byte)
	if string(data) != "new" {
		t.Fatalf("unlink destroyed open inode: %q", data)
	}
	call(b, request{Op: "write", Handle: other, Data: []byte("NEW")})
	data = call(b, request{Op: "read", Handle: other, Length: 3})["data"].([]byte)
	if string(data) != "NEW" {
		t.Fatalf("unlinked open inode is no longer writable: %q", data)
	}
	for _, x := range []struct{ sid, h uint64 }{{a, h}, {b, other}} {
		call(x.sid, request{Op: "close", Handle: x.h})
	}
	entries := call(a, request{Op: "list", Path: root})["entries"].([]map[string]any)
	if len(entries) != 0 {
		t.Fatalf("directory still contains entries: %v", entries)
	}
	call(a, request{Op: "remove", Path: root})
}
