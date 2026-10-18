# DuckDB compatibility: current scope and native database requirements

The goal is to support DuckDB workflows through its real filesystem interface,
without a SQL proxy or a restricted SQL dialect. This release implements
read/write analytics files. It does **not** claim that a remote `.duckdb` file is
a safe replacement for local database storage.

## What works now

DuckDB executes queries, joins, DDL, DML and transactions in its ordinary local
or in-memory engine. `tidbfs` supplies input files and receives output files.
The real CLI tests cover Parquet/CSV/JSON roundtrips, partitioned COPY with
APPEND/OVERWRITE, temporary output followed by rename, empty files, and
EXPORT/IMPORT of tables and views. The live TiDB smoke includes an explicit
transaction with INSERT, UPDATE and DELETE before export and restoration.

The filesystem implements positional/sequential writes, seek, local staging
truncate, sync/close publication, directory existence/creation/listing/removal,
file deletion and same-filesystem rename. Writes publish complete file
generations with revision compare-and-swap; they are not distributed locks.
Read-only operations retain the conditional range-read optimization.

External file writes and namespace changes do not participate in DuckDB SQL
transaction rollback. A partitioned export may leave completed files after a
later failure. Use a new output directory for each export and publish a separate
application-level pointer only after the entire operation succeeds.

## Why native database ATTACH remains blocked

DuckDB database handles require read/write access and a lifetime file lock.
Analytic COPY writers request write-only locks too; complete-generation CAS
provides conflict detection for those output handles. Silently treating a
database lock as successful would allow independent writers to corrupt the
database and WAL. Database handle requests therefore fail before creating data.

Uploading a local database file only on close is insufficient: an acknowledged
COMMIT must survive a client crash before close. A lease without fencing is also
insufficient: a paused or partitioned former holder can resume after expiry.
Per-file revision CAS cannot coordinate a database file, its WAL and checkpoint
renames, and does not protect against delete/recreate with a repeated revision.

## Required server protocol for native database storage

These are design requirements, not implemented capabilities:

1. **Open by inode and acquire ownership atomically.** Return a durable handle,
   inode generation and monotonically increasing fencing token. All mutation
   routes, including ordinary PUT/delete/rename and multipart finalization, must
   enforce that ownership or reject operations on leased database files.
2. **Lease renewal and fail-closed handles.** A lost/expired lease permanently
   invalidates the client handle. Every write, truncate, sync and namespace
   mutation checks the fence in the same metadata transaction as publication.
   Shared read locks and the exclusive database writer must follow DuckDB's
   concurrency model; a distributed filesystem does not add multi-process writers.
3. **Durable offset I/O and barriers.** Provide random read/write and truncation
   against the opened inode. `FileSync` acknowledges durable bytes and metadata,
   ordered across WAL writes, data pages and checkpoint publication. Small WAL
   appends must not re-upload the entire database on each commit.
4. **Atomic namespace operations.** Replacement rename, create-exclusive,
   unlink-while-open and delete/recreate must preserve inode identity. Database
   and WAL ownership must remain coherent through checkpoint and recovery.
5. **Idempotent recovery.** Mutation IDs resolve lost responses without replaying
   a write against a new generation. Crashed upload/session garbage collection
   must preserve data still reachable from a committed database/WAL generation.

TiDB transactions can host ownership, generations and metadata. Immutable
object blocks can hold larger data ranges. The write fence and manifest update
must be committed together; object upload success alone is not database commit.

## Acceptance criteria before enabling writable ATTACH

Run the same tests against local DuckDB and tidbfs, then compare durable state:

* CREATE/ALTER/DROP, INSERT/UPDATE/DELETE, indexes, sequences, views, transactions,
  rollback, CHECKPOINT, DETACH and reopen in a new process.
* Kill the client immediately after acknowledged COMMIT, during checkpoint and
  during WAL cleanup; reopen and verify every acknowledged transaction.
* Kill/restart the server; inject lost responses, duplicate requests, short
  writes, full storage and object-store failures at durability boundaries.
* Race competing processes, expire/renew leases, pause the old owner until a
  new owner acquires the file, then resume it and verify every stale write fails.
* Exercise alias paths, rename, unlink, delete/recreate and scoped credentials
  against every route that can mutate the database or its WAL.

Only after those checks pass should the compatibility table mark writable
native databases as supported. General filesystem compatibility is distinct
from compatibility with every DuckDB extension or every DuckDB binary version.

References: DuckDB's [filesystem contract](https://github.com/duckdb/duckdb/blob/v1.4.2/src/include/duckdb/common/file_system.hpp),
[export implementation](https://github.com/duckdb/duckdb/blob/v1.4.2/src/execution/operator/persistent/physical_export.cpp),
and [concurrency model](https://duckdb.org/docs/current/connect/concurrency).
