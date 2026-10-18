# DuckDB compatibility

The extension has two storage modes. SQL execution, catalog management, WAL
recovery and transaction semantics remain inside unmodified DuckDB 1.4.2.

| Workflow | `tidbfs:///` analytical files | `tidbfs+db:///name.duckdb` database files |
| --- | --- | --- |
| Parquet/CSV/JSON, Hive/globs, partition COPY | Supported | Use analytical mode |
| EXPORT/IMPORT DATABASE | Supported | Operates on the attached native database through DuckDB |
| Writable native ATTACH, catalog and DML | Deliberately rejected | Experimental; real TiDB/CLI tests |
| COMMIT, ROLLBACK, checkpoint and reopen | Local/in-memory DuckDB engine | Native DuckDB WAL and pages persist remotely |
| One writer process, or multiple read-only processes | No database lifetime lock | Server-clock leases, renewal and transactional fencing |
| Remote temporary files / external sorting | Whole-file publication | Positional page I/O and temporary-directory cleanup |
| Filesystem mutations roll back with SQL | No | DuckDB's native database transactions do; arbitrary file operations do not |
| Production service integration | Requires range API changes | Disabled by default; local-provider opt-in only |

## Native mode: implemented guarantees

Every acknowledged write commits its bytes and file size in a TiDB transaction.
The database and WAL share a lease. `FileSync` checks that lease after preceding
writes; it never merely flushes a local staging file. A 256 KiB database block
updates four 64 KiB pages, rather than uploading the complete database. Requests
are bounded to 1 MiB of binary data, encoded as base64 in this initial protocol.

A database row serializes all operations. Lease checks use current reads after
locking that row: a transaction snapshot taken before a lock wait cannot admit
a second owner. Expired tokens cannot renew or mutate; token hashes, rather
than usable lease tokens, are stored in the database. Random file IDs preserve
open-handle identity through file replacement and unlink. Unlinked inodes remain
until the final lease ends. Abandoned `.tmp` files are reclaimed when no active
lease remains. Ordinary `/v1/fs` PUT/multipart/rename cannot reach this namespace.

A lost response permanently invalidates the client session. Mutations are never
automatically replayed. After an ambiguous COMMIT, reopen and reconcile the
application transaction: the result can legitimately be committed or aborted.
This implementation does not yet provide mutation-ID outcome lookup.

The writer lock excludes every other process; multiple read-only leases coexist.
It does not add multiple concurrent writer processes to DuckDB's native format.
A crashed owner can delay reopening for up to 30 seconds. Live owners renew every
5 seconds. Spill directories retain the lease through DuckDB's shutdown cleanup.

## Verification

`make test` runs the analytical protocol fixtures against the real DuckDB CLI.
`python3 scripts/native_smoke.py` runs native tests against an updated live fs
server and TiDB, covering:

- Catalog/schema/table/view/index/sequence/macro creation, ALTER, constraints,
  INSERT/UPDATE/DELETE, COMMIT/ROLLBACK, checkpoint and fresh-process reopen.
- Client SIGKILL after a committed transaction and before an uncommitted
  transaction finishes; WAL recovery preserves only the committed rows.
- Writer contention, shared readers, and renewal across a full lease interval.
- External sorting with remote spill files; out-of-memory cleanup.
- A proxy drops an already-committed write acknowledgement; no write is retried
  and the database can be recovered.

`scripts/test_native_live.py` owns disposable TiDB/fs processes, can also run
server Go regressions, and additionally kills/restarts fs after an acknowledged
transaction, then checks WAL recovery. Go regressions cover cross-page binary
writes, holes, truncation, inode identity, atomic file rename, lease expiry,
concurrent acquisition, tenant isolation, API gates, and schema generation.
TiDB's local unistore is a functional test backend, not a power-loss durability
or production TiKV failover certification.

## Remaining boundaries

Native mode is an **experimental compatibility implementation**, not a claim
of 100% DuckDB, POSIX, extension or binary-version compatibility.

- Server requires `DRIVE9_TENANT_PROVIDER=local` and
  `DRIVE9_EXPERIMENTAL_DUCKDB=1`. Only unscoped filesystem tokens are accepted.
- Database paths must end in `.duckdb`; that marker is reserved in ancestor
  components. Sidecars and temporary files are managed within that database.
- Aggregate logical file size is capped at 1 GiB per database, including WAL,
  spill and unlinked-but-open files. This is a development limit.
- Pages currently live in TiDB. Native object-block storage, compression,
  prefetch/batching and performance tuning are future work; no throughput or
  cost advantage over local disk/S3 is claimed.
- Ordinary fs listing, backup/export, tenant accounting/deletion and permission
  management do not include the isolated database tables. Back up all four
  `duckdb_*` tables consistently while stopped; use DuckDB EXPORT for portable
  logical backups. Production enablement is intentionally blocked.
- Cross-database file moves, directory rename, native-mode globs and POSIX
  private-file permissions are rejected. Use analytical mode for data files.
- Exhaustive DuckDB SQLLogicTest coverage, kill-at-every-checkpoint-boundary
  testing, network partitions with server failover, TiKV outages/power loss,
  and wider platform/version coverage remain before a full compatibility claim.

## Analytical mode

Analytical output handles stage into private local temporary files and publish
complete generations with revision CAS. They do not participate in SQL rollback.
Partitioned exports can leave completed files after later failure. Use a fresh
output directory and publish an application pointer after successful export.
Revision CAS does not fence delete/recreate ABA, so it is deliberately never used
as the native database locking mechanism. See [the analytical server contract](server-contract.md)
and [the native database protocol](native-database.md).

Reference: DuckDB's version-pinned [filesystem contract](https://github.com/duckdb/duckdb/blob/v1.4.2/src/include/duckdb/common/file_system.hpp)
and [block manager](https://github.com/duckdb/duckdb/blob/v1.4.2/src/storage/single_file_block_manager.cpp).
