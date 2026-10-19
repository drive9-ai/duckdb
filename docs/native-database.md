# Native DuckDB over existing extent storage

The extension implements DuckDB's C++ FileSystem interface. Its extent adapter
links `mem9-ai/drive9` at `d39a6d1b3ba1d3e23fc75b8d530259c861855702`
through `bridge/main.go`. The bridge is in-process, not a new filesystem server
or a separately deployed service. Its module pins the same JuiceFS HTTP engine
fork used by drive9. Build-time Go dependencies are recorded in `bridge/go.sum`.

## Files and requests

`tidbfs+db:///warehouse.duckdb` maps to `/warehouse.duckdb`, with the usual
`.wal` and `.tmp/` sidecars. Arbitrary base filenames are accepted. All of these
are normal extent files, visible through `/v1/fs` metadata and listing. The
server has no DuckDB filename, WAL, transaction or checkpoint logic.

```mermaid
flowchart LR
  SQL[DuckDB SQL / storage engine] --> CPP[C++ filesystem adapter]
  CPP --> HTTP[Existing file HTTP client]
  HTTP --> FS["/v1/fs: conditions, ranges, revision CAS"]
  CPP --> Runtime[Existing drive9 extent runtime]
  Runtime --> Meta["/v1/extent/meta: inode, slices, locks, sessions"]
  Runtime --> Cred["/v1/data-credential"]
  Runtime --> Blocks[Existing object blocks]
```

`tidbfs:///` keeps the conditional-read and CAS-upload implementation for ordinary
analytical files. Existing extent-layout files automatically use the same extent
adapter. `tidbfs+db:///` explicitly selects extent creation and database locks.
It does not select a separate storage namespace.

No server flag or schema migration is needed for native mode. It needs a server
with the existing extent API and a supported data-credential backend. The
current runtime supports local file storage for tests and S3/GCS object storage.
Subtree-scoped server tokens are rejected by the existing inode API; use an
unscoped filesystem token. DuckDB secret scopes only control client routing.

## Locking, durability and failures

DuckDB's lock flags map to existing whole-file POSIX `setlk` locks with a stable
handle owner. Multiple readers can hold shared locks; a writer excludes them.
All clients writing a live database must honor these advisory locks. An ordinary
PUT or rename is not fenced by a database-specific lease. This is the existing
filesystem contract, not mandatory enforcement against arbitrary other writers.

The existing VFS performs random I/O, zero-filled holes, truncate, rename,
unlink, block uploads and slice publication. Its writeback staging is disabled.
The first adapter version syncs each write through VFS.Fsync before returning,
and maps DuckDB FileSync to that same operation. This avoids whole-database
uploads but adds synchronous flush overhead; write batching needs separate
performance work and recovery validation. The C ABI currently marshals bounded
1 MiB chunks through JSON/base64, adding copies inside the process. Object
transfers retain the runtime's existing binary protocol.

Metadata transport errors permanently fail that client session. Repeated
metadata publication attempts from the runtime are blocked after an ambiguous
response. Immutable object uploads may still use the runtime's normal retries.
The adapter also refuses I/O and renewal five seconds before its last confirmed
session deadline if no refresh succeeded. A monotonic deadline prevents a
backward wall-clock adjustment from extending an already confirmed session.
Metadata requests have a 30-second upper bound, shortened to the remaining
session lifetime. Responses are checked again before acknowledgement: a late
successful refresh cannot revive an expired session. A process paused past
expiry cannot resume using an old lock.

The shared drive9 extent transport now adds its existing session ID as
`caller_sid` to metadata requests. On servers advertising `session_guard: true`
at registration, mutation transactions lock and validate that same session row.
Cleanup cannot release locks ahead of an admitted write; requests delivered after
expiry or cleanup are rejected before publishing slices. A collected session
cannot be revived by a late renewal. This uses the ordinary extent API and
session table, including for FUSE and other clients; there is no DuckDB lease.

The generic changes are [drive9 #1039](https://github.com/mem9-ai/drive9/pull/1039)
(client transport) and [fs #241](https://github.com/tidbcloud/fs/pull/241)
(server transaction guard). The latter repository requires access. The separate
[fs #238](https://github.com/tidbcloud/fs/pull/238) contains only conditional-read
and Range improvements and is not the session guard.

Older servers ignore this field and retain their existing behavior, so they do
not provide this guarantee. The live bridge regression reports that distinction
and can require it with `TIDBFS_REQUIRE_SESSION_GUARD=1`. Session validation does
not make advisory locks mandatory or protect against another authorized client
that deliberately bypasses them. Production object-store and TiKV failover
coverage remains incomplete; native database support stays experimental.

Existing sessions refresh approximately every 12 seconds and expire after
approximately 60 seconds. After SIGKILL, the server's existing stale-session
sweep must release locks before another process opens the database. Recovery
latency depends on that sweep; there is no fixed 30-second takeover guarantee.
After an ambiguous COMMIT, reopen and reconcile application state; either the
committed or aborted outcome may be valid. Never blindly replay the transaction.

Temporary files follow ordinary extent lifecycle. DuckDB removes its spill
directory on normal cleanup; a crashed process can leave named temporary files.
No new filename-based server garbage collector is introduced. Ordinary backup,
quota and deletion behavior is inherited from extent storage, with no parallel
set of tables to integrate.

## Build and live validation

Build with a matching DuckDB 1.4.2 CLI/SDK, C++17, Go >=1.25.1 and libcurl:

```sh
make test
python3 -m pip install pymysql
python3 scripts/test_native_live.py \
  --tidb-server /path/to/tidb-server \
  --fs-server /path/to/drive9-server \
  --fs-source /path/to/fs
```

Linux links the Go adapter as adjacent `libtidbfs_extent.so`, with a relative
`$ORIGIN` search path; macOS includes its static archive. A Go archive linked
directly into DuckDB's ELF image makes its TLS allocation exceed glibc's dynamic
loading reserve ([Go issue #48596](https://github.com/golang/go/issues/48596)).
`scripts/install.py` installs the extension plus Linux runtime together and
verifies `LOAD tidbfs`. Both direct build-directory loading and installed loading
are checked in Linux CI. No `LD_PRELOAD` or loader tuning is required.

The harness creates disposable TiDB and filesystem processes, databases, object
storage, keys and paths. It runs bridge conformance, native SQL and recovery
checks. `--fs-source` additionally runs relevant server read/extent/schema tests.
Logs are under ignored `build/native-live/`. The disposable services use UTC
and a one-second maintenance poll to bound cleanup latency; the real 60-second
session expiry is unchanged. No existing services are stopped.

Checks include DDL/DML, COMMIT/ROLLBACK, checkpoint and fresh-process reopen;
writer contention and shared readers; client SIGKILL with committed and
uncommitted WAL; missed-heartbeat resume; a dropped successful extent-write
response; remote spill and out-of-memory cleanup; normal namespace visibility;
analytical reads/writes of extent Parquet; and client/server restart recovery.
Bridge tests exercise binary data, holes, truncate/regrow, rename replacement,
unlink with open handles, cross-session locks and fail-closed deadlines.

The separate [interrupted-handoff demo](demos/interrupted-handoff/README.md)
cuts both metadata and object-storage connections for a running writer, lets
another worker take over through natural session expiry/cleanup, restores the
connections, and checks the old COMMIT fails while all committed rows survive.
The public replay and raw results state the one-second test maintenance setting
and distinguish client-side rejection from server-side delayed-write protection.

Public GitHub CI builds a pinned public drive9 server and downloads a pinned,
checksum-verified RustFS release, then runs bridge
conformance, six native compatibility cases and four transient S3 fault cases.
The full crash, missed-heartbeat and restart suite targets current `tidbcloud/fs`.
That server repository is private; its newer stale-session scheduling is not
present in the public upstream server used by CI. These recovery tests remain
enabled by default in this harness; CI lists its narrower selection explicitly.

## Object-store fault validation

To exercise the S3 protocol with an isolated RustFS service and network fault
proxy, download the checksum-verified RustFS 1.0.3 release and add `--rustfs-server`:

```sh
python3 -m pip install pymysql==1.1.1 boto3==1.42.97
python3 scripts/download_rustfs.py
python3 scripts/test_native_live.py \
  --tidb-server /path/to/tidb-server \
  --fs-server /path/to/drive9-server \
  --rustfs-server "$PWD/build/rustfs"
```

Every service binds to loopback, uses temporary data, and receives fresh test
credentials. Static extent credentials are enabled only for that disposable
server. No production bucket or credential is needed. Without `--rustfs-server`,
the six object fault cases are explicitly skipped; the native cases still run.

| Fault | Required result |
| --- | --- |
| Two S3 PUT 503 responses | Retry immutable objects; committed rows have no duplicates |
| Successful S3 PUT, response lost | Retry the same object key; fresh-process query checks all values |
| Two S3 GET 503 responses | Fresh reader returns the complete expected data |
| Truncated S3 GET body | Retry or error, never return partial successful SQL results; tested path retries successfully |
| Persistent S3 PUT AccessDenied | COMMIT reports failure; reopening preserves only previous committed rows |
| Crash during checkpoint after object upload, before slice publication | Recover the previously acknowledged transaction from WAL |
| Successful metadata write, response delayed beyond 30 seconds | Fail the client, issue no duplicate publication, reopen and reconcile |

The persistent upload test waits for the existing runtime's terminal error
(up to several minutes of retries), rather than counting a test-process timeout
as a rejected COMMIT. Bridge conformance also deliberately demonstrates that a
client opening without a lock can write a file held by a cooperating writer.
This documents the advisory-lock boundary; it is not a supported database usage.

TiDB unistore and local RustFS validate ordering and S3 protocol fault behavior.
They do not establish AWS/GCS IAM or credential rotation, production service
durability, TiKV power-loss durability, arbitrary network-partition safety, or
full DuckDB/POSIX compatibility. Existing ordinary
inline/object files are not automatically converted into native extent databases.
Export/import existing prototype data before using this unreleased replacement;
no file-volume tables are read, migrated or dropped at runtime.
