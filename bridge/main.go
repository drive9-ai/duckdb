// The C ABI adapts drive9's existing extent client. It introduces no server API.
package main

/*
#include <stdlib.h>
*/
import "C"

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
	"unsafe"

	"github.com/juicedata/juicefs/pkg/meta"
	"github.com/juicedata/juicefs/pkg/vfs"
	"github.com/mem9-ai/drive9/pkg/client"
	"github.com/mem9-ai/drive9/pkg/extent"
	"github.com/mem9-ai/drive9/pkg/pathutil"
)

type request struct {
	Op        string `json:"op"`
	Endpoint  string `json:"endpoint"`
	Token     string `json:"token"`
	Session   uint64 `json:"session"`
	Handle    uint64 `json:"handle"`
	Path      string `json:"path"`
	Target    string `json:"target"`
	Offset    uint64 `json:"offset"`
	Length    uint64 `json:"length"`
	Data      []byte `json:"data"`
	Write     bool   `json:"write"`
	Create    bool   `json:"create"`
	Exclusive bool   `json:"exclusive"`
	Truncate  bool   `json:"truncate"`
	Lock      string `json:"lock"`
}

type handle struct {
	ino   meta.Ino
	fh    uint64
	write bool
}
type session struct {
	mu       sync.Mutex
	rt       *extent.Runtime
	files    map[uint64]handle
	failed   atomic.Bool
	deadline atomic.Int64
}

var sessions = struct {
	sync.Mutex
	next uint64
	all  map[uint64]*session
}{all: make(map[uint64]*session)}

func (s *session) healthy() error {
	// Fail closed before the existing 60-second session can expire. In
	// particular, a process resumed after SIGSTOP cannot revive an old lock.
	if d := s.deadline.Load(); d != 0 && time.Now().Unix() >= d-5 {
		s.failed.Store(true)
	}
	if s.failed.Load() {
		return errors.New("extent session failed or expired; close and reopen, do not replay writes")
	}
	return nil
}

func newSession(r request) (*session, error) {
	s := &session{files: make(map[uint64]handle)}
	c := client.New(r.Endpoint, r.Token)
	ctx, cancel := context.WithTimeout(context.Background(), extent.CredentialMintTimeout)
	defer cancel()
	cred, err := c.GetDataCredential(ctx)
	if err != nil {
		return nil, fmt.Errorf("extent credential: %w", err)
	}
	storage, err := extent.OpenStorage(cred, c)
	if err != nil {
		return nil, err
	}
	transport := extent.NewTransport(func(ctx context.Context, op string, raw json.RawMessage) (json.RawMessage, int, error) {
		if err := s.healthy(); err != nil {
			return nil, int(syscall.EIO), err
		}
		body, err := c.ExtentMeta(ctx, op, raw)
		if err != nil {
			// JuiceFS may retry an uncertain metadata publication. Once the
			// response is lost, refuse every subsequent RPC in this session.
			s.failed.Store(true)
			return nil, int(syscall.EIO), err
		}
		var result struct {
			Errno *int `json:"errno"`
		}
		if json.Unmarshal(body, &result) != nil || result.Errno == nil {
			s.failed.Store(true)
			return nil, int(syscall.EIO), errors.New("invalid extent metadata acknowledgement")
		}
		if op == "new_session" || op == "refresh_session" {
			var input struct {
				Expire int64 `json:"expire"`
			}
			if *result.Errno == 0 && json.Unmarshal(raw, &input) == nil {
				s.deadline.Store(input.Expire)
			} else {
				s.failed.Store(true)
			}
		}
		return body, 0, nil
	})
	s.rt, err = extent.NewRuntime(extent.RuntimeConfig{Transport: transport, Storage: storage, CacheBytes: 64 << 20})
	if err != nil {
		return nil, err
	}
	return s, nil
}

func fileContext(path string) vfs.LogContext {
	return vfs.NewLogContext(meta.NewContext(uint32(os.Getpid()), 0, []uint32{0}).WithValue(meta.Drive9PathKey, path))
}

func canonical(p string) (string, error) {
	if p == "" || !strings.HasPrefix(p, "/") {
		return "", syscall.EINVAL
	}
	p, err := pathutil.Canonicalize(p)
	if err != nil {
		return "", err
	}
	if p != "/" {
		p = strings.TrimSuffix(p, "/")
	}
	return p, nil
}

func (s *session) lookup(p string) (*meta.Entry, syscall.Errno) {
	ctx := fileContext(p)
	entry, eno := s.rt.VFS.GetAttr(ctx, 1, 0)
	if eno != 0 || p == "/" {
		return entry, eno
	}
	for _, name := range strings.Split(strings.TrimPrefix(p, "/"), "/") {
		entry, eno = s.rt.VFS.Lookup(ctx, entry.Inode, name)
		if eno != 0 {
			return nil, eno
		}
	}
	return entry, 0
}

func (s *session) parent(p string) (meta.Ino, string, syscall.Errno) {
	if p == "/" {
		return 0, "", syscall.EINVAL
	}
	i := strings.LastIndexByte(p, '/')
	dir := p[:i]
	if dir == "" {
		dir = "/"
	}
	e, eno := s.lookup(dir)
	if eno != 0 {
		return 0, "", eno
	}
	if e.Attr.Typ != meta.TypeDirectory {
		return 0, "", syscall.ENOTDIR
	}
	return e.Inode, p[i+1:], 0
}

func info(e *meta.Entry) map[string]any {
	return map[string]any{"exists": true, "directory": e.Attr.Typ == meta.TypeDirectory, "size": e.Attr.Length, "inode": e.Inode}
}

func (s *session) call(r request) (map[string]any, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.healthy(); err != nil {
		return nil, err
	}
	p, err := canonical(r.Path)
	if r.Path != "" && err != nil {
		return nil, err
	}
	ctx := fileContext(p)
	v := s.rt.VFS
	check := func(eno syscall.Errno) error {
		if eno != 0 {
			return eno
		}
		return nil
	}
	switch r.Op {
	case "stat":
		e, eno := s.lookup(p)
		if eno == syscall.ENOENT {
			return map[string]any{"exists": false, "directory": false}, nil
		}
		if eno != 0 {
			return nil, eno
		}
		return info(e), nil
	case "open":
		parent, name, eno := s.parent(p)
		if eno != 0 {
			return nil, eno
		}
		e, eno := s.lookup(p)
		flags := uint32(syscall.O_RDONLY)
		if r.Write {
			flags = syscall.O_RDWR
		}
		var fh uint64
		if eno == syscall.ENOENT && r.Create {
			e, fh, eno = v.Create(ctx, parent, name, 0600, 0, flags|syscall.O_CREAT|syscall.O_EXCL)
			if eno == syscall.EEXIST && !r.Exclusive {
				e, eno = s.lookup(p)
				if eno == 0 {
					e, fh, eno = v.Open(ctx, e.Inode, flags)
				}
			}
		} else if eno == 0 {
			if r.Exclusive {
				return nil, syscall.EEXIST
			}
			if e.Attr.Typ != meta.TypeFile {
				return nil, syscall.EISDIR
			}
			e, fh, eno = v.Open(ctx, e.Inode, flags)
		}
		if eno != 0 {
			return nil, eno
		}
		ok := false
		defer func() {
			if !ok {
				v.Release(ctx, e.Inode, fh)
			}
		}()
		if r.Lock != "" {
			typ := uint32(syscall.F_RDLCK)
			if r.Lock == "write" {
				typ = syscall.F_WRLCK
			}
			// Whole-file POSIX record lock, identical to DuckDB's local backend.
			if eno = v.Setlk(ctx, e.Inode, fh, fh, 0, math.MaxInt64, typ, uint32(os.Getpid()), false); eno != 0 {
				return nil, fmt.Errorf("file is locked: %w", eno)
			}
		}
		if r.Truncate {
			if !r.Write {
				return nil, syscall.EACCES
			}
			if _, eno = v.SetAttr(ctx, e.Inode, int(meta.SetAttrSize), fh, 0, 0, 0, 0, 0, 0, 0, 0); eno != 0 {
				return nil, eno
			}
			e.Attr.Length = 0
		}
		s.files[fh] = handle{e.Inode, fh, r.Write}
		ok = true
		out := info(e)
		out["handle"] = fh
		return out, nil
	case "mkdir", "remove":
		parent, name, eno := s.parent(p)
		if eno != 0 {
			return nil, eno
		}
		if r.Op == "mkdir" {
			_, eno = v.Mkdir(ctx, parent, name, 0700, 0)
			return map[string]any{}, check(eno)
		}
		e, eno := s.lookup(p)
		if eno != 0 {
			return nil, eno
		}
		if e.Attr.Typ == meta.TypeDirectory {
			eno = v.Rmdir(ctx, parent, name)
		} else {
			eno = v.Unlink(ctx, parent, name)
		}
		return map[string]any{}, check(eno)
	case "rename":
		target, err := canonical(r.Target)
		if err != nil {
			return nil, err
		}
		parent, name, eno := s.parent(p)
		if eno != 0 {
			return nil, eno
		}
		tparent, tname, eno := s.parent(target)
		if eno != 0 {
			return nil, eno
		}
		return map[string]any{}, check(v.Rename(fileContext(target), parent, name, tparent, tname, 0))
	case "list":
		e, eno := s.lookup(p)
		if eno != 0 {
			return nil, eno
		}
		fh, eno := v.Opendir(ctx, e.Inode, 0)
		if eno != 0 {
			return nil, eno
		}
		defer v.Releasedir(ctx, e.Inode, fh)
		entries := []map[string]any{}
		for off := 0; ; {
			batch, _, eno := v.Readdir(ctx, e.Inode, 4096, off, fh, true)
			if eno != 0 {
				return nil, eno
			}
			if len(batch) == 0 {
				break
			}
			for _, child := range batch {
				name := string(child.Name)
				if name != "." && name != ".." {
					entries = append(entries, map[string]any{"name": name, "directory": child.Attr.Typ == meta.TypeDirectory})
				}
			}
			off += len(batch)
			v.UpdateReaddirOffset(ctx, e.Inode, fh, off)
		}
		return map[string]any{"entries": entries}, nil
	}
	h, ok := s.files[r.Handle]
	if !ok {
		return nil, syscall.EBADF
	}
	if r.Offset > math.MaxInt64 || r.Length > 1<<20 || len(r.Data) > 1<<20 {
		return nil, syscall.EINVAL
	}
	switch r.Op {
	case "read":
		data := make([]byte, r.Length)
		n, eno := v.Read(ctx, h.ino, data, r.Offset, h.fh)
		return map[string]any{"data": data[:n]}, check(eno)
	case "write":
		if !h.write {
			return nil, syscall.EACCES
		}
		if eno := v.Write(ctx, h.ino, r.Data, r.Offset, h.fh); eno != 0 {
			s.failed.Store(true)
			return nil, eno
		}
		// Conservative initial policy: acknowledge only after existing VFS has
		// uploaded blocks and published slice metadata. No dirty close errors.
		if eno := v.Fsync(ctx, h.ino, 0, h.fh); eno != 0 {
			s.failed.Store(true)
			return nil, eno
		}
	case "sync":
		if eno := v.Fsync(ctx, h.ino, 0, h.fh); eno != 0 {
			s.failed.Store(true)
			return nil, eno
		}
	case "truncate":
		if !h.write {
			return nil, syscall.EACCES
		}
		_, eno := v.SetAttr(ctx, h.ino, int(meta.SetAttrSize), h.fh, 0, 0, 0, 0, 0, 0, 0, r.Offset)
		if eno != 0 {
			s.failed.Store(true)
			return nil, eno
		}
	case "close":
		if eno := v.Flush(ctx, h.ino, h.fh, h.fh); eno != 0 {
			s.failed.Store(true)
			return nil, eno
		}
		v.Release(ctx, h.ino, h.fh)
		delete(s.files, r.Handle)
	default:
		return nil, syscall.ENOSYS
	}
	return map[string]any{}, s.healthy()
}

func dispatch(r request) (map[string]any, error) {
	if r.Op == "connect" {
		s, err := newSession(r)
		if err != nil {
			return nil, err
		}
		sessions.Lock()
		defer sessions.Unlock()
		sessions.next++
		sessions.all[sessions.next] = s
		return map[string]any{"session": sessions.next}, nil
	}
	sessions.Lock()
	s := sessions.all[r.Session]
	if r.Op == "disconnect" {
		delete(sessions.all, r.Session)
	}
	sessions.Unlock()
	if s == nil {
		return nil, syscall.EBADF
	}
	if r.Op == "disconnect" {
		s.mu.Lock()
		defer s.mu.Unlock()
		for _, h := range s.files {
			s.rt.VFS.Release(fileContext(""), h.ino, h.fh)
		}
		return map[string]any{}, extent.CloseRuntime(s.rt)
	}
	return s.call(r)
}

//export tidbfs_extent_call
func tidbfs_extent_call(raw *C.char) (out *C.char) {
	defer func() {
		if recover() != nil {
			out = C.CString(`{"error":"extent bridge panic"}`)
		}
	}()
	var r request
	err := json.Unmarshal([]byte(C.GoString(raw)), &r)
	var result map[string]any
	if err == nil {
		result, err = dispatch(r)
	}
	if err != nil {
		result = map[string]any{"error": err.Error()}
	}
	data, _ := json.Marshal(result)
	return C.CString(string(data))
}

//export tidbfs_extent_free
func tidbfs_extent_free(p *C.char) { C.free(unsafe.Pointer(p)) }

func main() {}
