# DuckDB compatibility

SQL, catalog, WAL, recovery and transaction execution remain inside unmodified
DuckDB 1.4.2. This is an experimental filesystem integration, not a complete
DuckDB or POSIX compatibility claim.

| Workflow | `tidbfs:///` | `tidbfs+db:///` |
| --- | --- | --- |
| Parquet/CSV/JSON, Hive/globs, partition COPY | Supported | Extent I/O; use analytical URI for globs |
| New analytical output | Revision-checked PUT/multipart | Existing extent creation |
| Existing extent analytical files | Automatically routed to extent runtime | Extent runtime |
| Writable native ATTACH, catalog and DML | Use explicit database URI | Existing extent files |
| COMMIT/ROLLBACK, checkpoint and reopen | Local engine or attached database | Native WAL and pages on extent storage |
| One writer or shared readers | CAS publication for ordinary output | Existing advisory POSIX locks |
| Remote spill files | Whole-generation ordinary output | Extent random I/O and DuckDB cleanup |
| Metadata/listing through `/v1/fs` | Yes | Yes, same paths and namespace |
| Additional server tables/endpoints | None | None |

## Verification

`make test` builds and loads the extension and runs 24 analytical CLI fixtures.
`scripts/test_native_live.py` runs bridge conformance, native DuckDB tests and
client/server restart checks against disposable TiDB and filesystem services.
See [the native adapter](native-database.md) for the exact failure scenarios.
Server-side range validators, binary projections, shared-schema isolation and
existing extent regressions run with `--fs-source`.
Public CI runs six native compatibility cases on the public upstream server;
the complete recovery suite runs against current `tidbcloud/fs`, whose checkout
is private. The harness defaults to the complete suite, including failure tests.

## Boundaries

- Native mode is single writer or shared read-only processes. It does not add
  simultaneous database writer processes or distributed query execution.
- Existing locks are advisory. Other writers must cooperate; ordinary API
  clients must not replace a live database or its WAL.
- The client fails closed after transport ambiguity or missed session renewal.
  This is not transactional server-side fencing of every write. Delayed RPCs,
  clock skew, TiKV failover and production object stores need more validation.
- fsync uses the existing extent durability path. The first version syncs every
  write, trading performance for simple acknowledgement semantics.
- The C++ extension statically includes a Go runtime and cloud dependencies,
  increasing build time and binary size. JSON/base64 inside its C ABI adds
  copies. It requires no helper daemon, FUSE mount, or Go installation at runtime.
- Native mode inherits existing extent backend/permission limits, including
  rejection of subtree-scoped server tokens. It adds no 1 GiB database cap.
- Named spill files can remain after a crash. Normal DuckDB cleanup removes
  them; there is no new server-side temporary-file reclamation policy.
- Native URI globs and automatic conversion of inline/object files into extent
  databases are unsupported. Use analytical globs and explicit export/import.
- Full SQLLogicTest coverage, checkpoint-boundary fault sweeps, S3/GCS failure
  testing and broader platform/version coverage remain open.

Analytical publication uses existing revision CAS. Individual file publication
is conditional; a multi-file export is not atomic and is not rolled back with
SQL. Existing backend APIs also have inode-aware conditions, but the ordinary
PUT/multipart contract used here only exposes revision. Query-local read pinning
is not a transaction-wide or dataset-wide snapshot.
