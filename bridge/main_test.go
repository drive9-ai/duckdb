package main

import (
	"bytes"
	"fmt"
	"os"
	"testing"
	"time"
)

func TestSessionStopsBeforeExpiryAndStaysFailed(t *testing.T) {
	s := &session{}
	s.deadline.Store(time.Now().Unix() + 4)
	if s.healthy() == nil {
		t.Fatal("session continued into its expiry margin")
	}
	s.deadline.Store(time.Now().Unix() + 60)
	if s.healthy() == nil {
		t.Fatal("failed session was revived")
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
