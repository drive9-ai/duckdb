# One native writer and independent snapshot readers

## Semantics

One write-capable DuckDB process owns the existing native database and WAL.
Independent DuckDB processes read published native database checkpoints. They
execute their own SQL; no query is forwarded to the writer.

Publication is explicit: `CALL tidbfs_publish('warehouse')` checkpoints the
attached writable database while holding DuckDB's checkpoint exclusion, then
atomically publishes a metadata clone. It returns the reader URI and generation
(inode identity). It fails rather than publishing uncommitted or mixed pages.
A normal COMMIT remains durable through the existing WAL path, but does not
publish a new reader generation.

A reader attaches `tidbfs+snapshot:///warehouse.duckdb` with READ_ONLY. The
logical source is `tidbfs+db:///warehouse.duckdb`. The reader pins one published
inode for the entire attachment, including catalog, indexes and all pages.
Queries and transactions never auto-refresh that attachment. DETACH followed by
ATTACH explicitly selects the latest published generation; DuckDB's usual
transaction/active-query restrictions apply. Refresh does not promise the latest
writer COMMIT: it sees the latest successfully published checkpoint.

No generation exists before the first successful publish. Readers fail clearly
rather than falling back to the mutable live database. They never replay the
writer's mutable WAL: the published checkpoint is self-contained.

## Existing capabilities assessed

- `jfs_session2` and session-guarded mutations fence expired clients. Reuse them.
- Existing inode POSIX locks enforce the one-writer rule. Keep it unchanged.
- Immutable extent objects and `jfs_chunk_ref` retain shared blocks. Reuse them.
- `jfs_sustained`, open_ref and unlink/rename preserve an open old inode and
  reclaim it only after the final reader closes or its session is cleaned up.
- Revision CAS conditions detect changed ordinary files; they do not retain a
  historical native extent mapping. Extent inode identity is the immutable
  publication generation, avoiding a second revision protocol or manifest table.
- Existing filesystem copy/hardlink does not create an independent extent map.
  Add one generic atomic extent clone operation, not a DuckDB-specific endpoint.

## Generic extent addition

Clone a locked source inode to a destination dentry, optionally replacing an
expected destination inode. Session and source lock ownership are validated in
the same transaction. Copy the chunk mapping and increment existing slice refs;
create a new inode and update both namespace views atomically. Existing readers
retain the replaced inode through their open references. Payload bytes stay in
existing immutable object storage. Use the existing per-filesystem SQL scope,
quota admission, deleted-file drain and block GC; no new tables or lock protocol.

Capability negotiation must reject older servers clearly. Ambiguous metadata
responses fail the existing client session; never replay publication blindly.

## Failure model

Before the atomic clone commits, readers see the previous published version.
After it commits, fresh readers see the new one; already attached readers keep
their old version. Lost acknowledgement means the publisher cannot claim success,
although the new version may be visible. A fresh session can inspect/reopen it.
Writer crash or takeover does not invalidate published snapshots. Session expiry
invalidates that client's handles rather than silently refreshing their pages.
Existing advisory-lock limitations remain: direct clients must cooperate and must
not mutate published snapshot sidecars or replace a live database/WAL.

## Retention and limits

The latest checkpoint is a normal extent inode at the reserved sibling
`<source>.tidbfs-snapshot`. Its path is an atomic publication pointer, not a copy
of the entire object payload. The inode identity returned by `tidbfs_publish`
is the generation; no extra manifest or revision table is needed. The live file
and WAL keep their existing names. Names ending in `.wal` are reserved and cannot
be published, so snapshot opens cannot accidentally replay a writer WAL.

Each publication allocates a new inode and replaces the previous sidecar using
an expected-inode CAS. Readers hold the old inode with existing open references.
Closing the final reader, or cleanup of its expired session, permits the existing
deleted-file drain and reference-counted GC to reclaim its unshared blocks.
Keeping readers open intentionally retains historical storage. The latest
published generation remains until replaced or explicitly removed while unused.
Deleting a live database does not automatically remove its published checkpoint.

Cloning is bounded to 65536 chunk rows and 8 MiB of mapping metadata per atomic
transaction. Highly fragmented mappings can exceed this limit independently of
database byte size; compact before retrying. Logical file/storage quotas apply
to the cloned inode even though payload bytes are shared. All publishers and
direct FS clients must honor locks and leave the reserved sidecar immutable.

The plugin holds DuckDB 2.0 checkpoint exclusion through clone. Active update or
catalog transactions cause a retryable SQL error. Native concurrent checkpoint
semantics allow append/delete transactions while including only committed data;
publication does not promise commits that finish after its checkpoint boundary.
It uses matching DuckDB C++ internals, so rebuild/revalidate for a new DuckDB ABI.

## Validation

Generic clone: source/destination isolation, shared-schema collisions, lock and
session ownership, CAS rejection, atomic rollback, sparse/truncated mappings,
quota limits, slice reference retention under compaction/deletion, and release
of replaced-but-open versions.

DuckDB: independent processes with writer active; catalog/index/DML consistency;
old reader pinned across multiple publications; new reader freshness; explicit
refresh; no uncommitted rows; concurrent publish/write exclusion; publication
failure/lost acknowledgement; writer crash/reopen/takeover; reader session loss;
RustFS object failures and retention of old versions after source changes.

`scripts/snapshot_smoke.py` covers the independent-process cases with real TiDB
and RustFS. `test/snapshot_concurrency.cpp` uses the matching DuckDB shared
library to test concurrent connections in the writer: update exclusion,
uncommitted append/delete visibility, and cross-table generation consistency.
The private FS integration workflow runs the positive capability tests; public
plugin CI builds and tests the adapter against its pinned legacy server too.
These are bounded fault tests, not exhaustive production object-store or TiKV
failover certification. Native database support remains experimental.
