package main

import (
	"bytes"
	"errors"
	"fmt"
	"math"
	"os"
	"sync"
	"syscall"
	"testing"
	"time"
	"unsafe"
)

func TestReadBufferRejectsInvalidRangesBeforeTouchingMemory(t *testing.T) {
	for _, r := range []struct{ length, offset uint64 }{
		{1, 0}, // nonempty nil destination
		{maxRead + 1, 0}, {math.MaxUint64, 0}, {0, math.MaxUint64},
		{1, math.MaxInt64}, {maxRead, math.MaxInt64 - maxRead + 1},
	} {
		if _, err := readBuffer(0, 0, nil, r.length, r.offset); !errors.Is(err, syscall.EINVAL) {
			t.Fatalf("length=%d offset=%d: %v", r.length, r.offset, err)
		}
	}
	if _, err := readBuffer(0, 0, nil, 0, math.MaxInt64); !errors.Is(err, syscall.EBADF) {
		t.Fatalf("valid empty range bypassed session validation: %v", err)
	}
}

func TestReadGuardAllowsOverlapAndExcludesHandleMutation(t *testing.T) {
	s := &session{files: map[uint64]handle{1: {fh: 1}}}
	entered := make(chan struct{}, 2)
	release := make(chan struct{})
	var once sync.Once
	unblock := func() { once.Do(func() { close(release) }) }
	t.Cleanup(unblock)
	done := make(chan error, 2)
	for i := 0; i < 2; i++ {
		go func() {
			_, err := s.withReadHandle(1, func(h handle) (int, error) {
				entered <- struct{}{}
				<-release
				return 1, nil
			})
			done <- err
		}()
	}
	for i := 0; i < 2; i++ {
		select {
		case <-entered:
		case <-time.After(5 * time.Second):
			t.Fatal("reads of the same handle did not overlap")
		}
	}
	// The exclusive guard used by close/write/truncate/disconnect must not
	// enter while either reader is using the VFS handle or caller buffer.
	if s.mu.TryLock() {
		s.mu.Unlock()
		t.Fatal("handle mutation entered during active reads")
	}
	unblock()
	for i := 0; i < 2; i++ {
		if err := <-done; err != nil {
			t.Fatal(err)
		}
	}
	s.mu.Lock()
	delete(s.files, 1)
	s.mu.Unlock()
	called := false
	if _, err := s.withReadHandle(1, func(handle) (int, error) { called = true; return 0, nil }); !errors.Is(err, syscall.EBADF) || called {
		t.Fatalf("read used a released handle: called=%v err=%v", called, err)
	}
}

func TestReadFailsClosedBeforeAndAfterIO(t *testing.T) {
	for _, phase := range []string{"closed", "expired-before", "expired-during"} {
		t.Run(phase, func(t *testing.T) {
			s := &session{files: map[uint64]handle{1: {fh: 1}}}
			s.setDeadline(time.Now().Unix() + 60)
			if phase == "closed" {
				s.closed = true
			} else if phase == "expired-before" {
				s.setDeadline(time.Now().Unix())
			}
			called := false
			n, err := s.withReadHandle(1, func(handle) (int, error) {
				called = true
				s.setDeadline(time.Now().Unix())
				return 10, nil
			})
			if err == nil || n != 0 || called != (phase == "expired-during") {
				t.Fatalf("accepted stale data: n=%d called=%v err=%v", n, called, err)
			}
			if phase != "closed" {
				s.setDeadline(time.Now().Unix() + 60)
				if s.healthy() == nil {
					t.Fatal("read failure was revived")
				}
			}
		})
	}
}

func liveReadSession(t *testing.T) uint64 {
	t.Helper()
	endpoint, token := os.Getenv("DRIVE9_BASE"), os.Getenv("DRIVE9_API_KEY")
	if endpoint == "" || token == "" {
		t.Skip("requires disposable live filesystem")
	}
	out, err := dispatch(request{Op: "connect", Endpoint: endpoint, Token: token})
	if err != nil {
		t.Fatal(err)
	}
	sid := out["session"].(uint64)
	t.Cleanup(func() { _, _ = dispatch(request{Op: "disconnect", Session: sid}) })
	return sid
}

func TestLiveBinaryReadRangesAndConcurrentClose(t *testing.T) {
	sid := liveReadSession(t)
	call := func(r request) map[string]any {
		t.Helper()
		r.Session = sid
		out, err := dispatch(r)
		if err != nil {
			t.Fatalf("%s: %v", r.Op, err)
		}
		return out
	}
	h := call(request{Op: "open", Path: fmt.Sprintf("/binary-read-%d", time.Now().UnixNano()), Create: true, Write: true})["handle"].(uint64)
	data := make([]byte, 2*maxRead+37)
	for i := range data {
		data[i] = byte(i*37 + i/251)
	}
	for off := 0; off < len(data); off += maxRead {
		call(request{Op: "write", Handle: h, Offset: uint64(off), Data: data[off:min(off+maxRead, len(data))]})
	}
	for _, span := range [][2]int{{0, 0}, {0, 1}, {3, 257}, {maxRead - 13, 101}, {maxRead, maxRead},
		{len(data) - 7, 64}, {len(data), 23}, {len(data) + 1, 23}} {
		off, length := span[0], span[1]
		buffer := bytes.Repeat([]byte{0xcc}, length+2)
		n, err := readBuffer(sid, h, unsafe.Pointer(&buffer[1]), uint64(length), uint64(off))
		want := data[min(off, len(data)):min(off+length, len(data))]
		if err != nil || n != len(want) || !bytes.Equal(buffer[1:1+n], want) ||
			!bytes.Equal(buffer[1+n:], bytes.Repeat([]byte{0xcc}, length+1-n)) || buffer[0] != 0xcc {
			t.Fatalf("offset=%d length=%d: n=%d err=%v (data or destination bounds mismatch)", off, length, n, err)
		}
	}
	// Read unrelated offsets on the same VFS handle and compare every byte.
	start := make(chan struct{})
	var readers sync.WaitGroup
	for i := 0; i < 8; i++ {
		readers.Add(1)
		go func(index int) {
			defer readers.Done()
			<-start
			buffer := make([]byte, 32768)
			for j := 0; j < 16; j++ {
				off := (index*17377 + j*65537) % (len(data) - len(buffer))
				n, err := readBuffer(sid, h, unsafe.Pointer(&buffer[0]), uint64(len(buffer)), uint64(off))
				if errors.Is(err, syscall.EBADF) {
					return // close won; no use-after-release is permitted
				}
				if err != nil || n != len(buffer) || !bytes.Equal(buffer, data[off:off+n]) {
					t.Errorf("concurrent read: n=%d err=%v", n, err)
					return
				}
			}
		}(i)
	}
	close(start)
	readers.Wait()
	// Race another round of reads against an actual VFS close.
	readers.Add(1)
	go func() {
		defer readers.Done()
		buffer := make([]byte, maxRead)
		_, err := readBuffer(sid, h, unsafe.Pointer(&buffer[0]), maxRead, 0)
		if err != nil && !errors.Is(err, syscall.EBADF) {
			t.Errorf("read/close: %v", err)
		}
	}()
	call(request{Op: "close", Handle: h})
	readers.Wait()
	if _, err := readBuffer(sid, h, nil, 0, 0); !errors.Is(err, syscall.EBADF) {
		t.Fatalf("closed handle still readable: %v", err)
	}
}
