# duckdb-tidbfs

[![CI](https://github.com/siddontang/duckdb-tidbfs/actions/workflows/ci.yml/badge.svg)](https://github.com/siddontang/duckdb-tidbfs/actions/workflows/ci.yml)

`tidbfs` is a C++ DuckDB filesystem extension for reading and writing analytics files over the
TiDB Filesystem HTTP data plane, with experimental native `.duckdb` storage. It works in the DuckDB CLI and does not require
Python, fsspec, FUSE, or downloading whole Parquet files. Python is used only by
the build and test scripts.

## Build and query

The locally verified combination is DuckDB **v1.4.2, macOS arm64**. C++ extensions
must be built against the exact DuckDB version and platform that loads them.
Install the DuckDB C++ headers, a C++17 compiler, and libcurl headers/library.
For Homebrew DuckDB on Apple Silicon:

```sh
git clone https://github.com/siddontang/duckdb-tidbfs.git
cd duckdb-tidbfs
make build
duckdb -unsigned
```

For another matching SDK/CLI, set `DUCKDB_INCLUDE` and `DUCKDB`; `CXX` and
`CXXFLAGS` are also supported. The script downloads nlohmann/json 3.11.3 with a
pinned SHA-256, creates the DuckDB extension metadata, and verifies `LOAD`.
On Linux, install CMake and set `DUCKDB_SOURCE` to a matching DuckDB source
checkout. The script uses DuckDB's CMake build to statically link the extension
and its dependencies; the official CLI does not export every C++ filesystem
symbol, and the release SDK's static archive omits its third-party libraries.
The first build takes several minutes. `BUILD_JOBS` controls parallelism (default
2), and `CMAKE_ARGS` accepts additional CMake settings. The
[CI workflow](.github/workflows/ci.yml) shows the complete Linux setup with a
checksummed DuckDB CLI. macOS uses the faster installed-SDK build.

```sql
LOAD 'build/tidbfs.duckdb_extension';

CREATE TEMPORARY SECRET fs (
    TYPE tidbfs,
    ENDPOINT 'https://YOUR_FILESYSTEM_ENDPOINT',
    TOKEN 'YOUR_FILESYSTEM_API_KEY',
    SCOPE 'tidbfs:///'
);

SELECT region, sum(amount_cents)
FROM read_parquet('tidbfs:///warehouse/day=*/*.parquet', hive_partitioning=true)
WHERE day = '2026-10-17'
GROUP BY region;

COPY (SELECT * FROM range(100) t(id))
TO 'tidbfs:///results/output.parquet' (FORMAT PARQUET);

COPY (SELECT i AS id, i % 4 AS bucket FROM range(100) t(i))
TO 'tidbfs:///results/partitioned' (FORMAT PARQUET, PARTITION_BY(bucket));

EXPORT DATABASE 'tidbfs:///backups/run-001' (FORMAT PARQUET);
-- In a fresh local/in-memory database with the extension and secret loaded:
IMPORT DATABASE 'tidbfs:///backups/run-001';
```

`ENDPOINT` is the filesystem data-plane base URL; `/v1/fs` is appended by the
extension. The URI contains an absolute filesystem path, not a hostname.
Use separate scoped secrets to map different path prefixes to different
filesystems. Tokens are redacted by `duckdb_secrets()`. In automation, send SQL
through stdin rather than placing credentials in process arguments.

The build is unsigned and is not published to DuckDB's extension repository.
`INSTALL tidbfs` from a public repository is not available yet.

## Write support and compatibility

| Operation | Support |
| --- | --- |
| Read Parquet, CSV, JSON; glob/Hive pruning | Supported |
| `COPY TO` Parquet, CSV, JSON; overwrite an output file | Supported |
| Partitioned `COPY`, `APPEND`, `OVERWRITE` | Supported; APPEND adds dataset files |
| `COPY ... USE_TMP_FILE true` | Supported through server rename |
| `EXPORT DATABASE` / `IMPORT DATABASE` | Supported; data and SQL scripts are remote |
| SQL DDL, DML and transactions in a local/in-memory database | Normal DuckDB behavior; import/export connects it to remote files |
| Writable `ATTACH 'tidbfs+db:///database.duckdb'` | Experimental: native database, WAL, checkpoint and recovery over TiDB pages |
| Writable native database on ordinary `tidbfs:///` | Rejected; use the isolated database namespace |
| Multi-file atomic export, rollback of external files | Not provided |

Writable handles stage bytes in a private, unlinked local temporary file. This
supports seek, offset writes and truncation without retaining the entire output
in memory, but requires local scratch space proportional to open output files.
On explicit close/sync, the extension publishes a whole generation. Small files
use a conditional PUT; large files use V2 multipart uploads directly to object
storage, without forwarding filesystem credentials. Expired part URLs get one
refresh; failed multipart uploads are aborted on a best-effort basis.

DuckDB's EXPORT script writers destroy handles without calling close. Their
pending output is published at successful query end, with publication errors
returned to the caller. Failed queries discard unpublished staging; files
already published by a multi-file operation remain, as do directory changes.
An ambiguous commit response is reported as failure and is not blindly retried.

Writes carry the destination's expected revision (`0` for creation); conflicting
revisions fail with HTTP 409. This is optimistic publication, not a lifetime file
lock. The current write API compares revisions, not inode identity: a concurrent
delete/recreate with the same revision is not fenced. Do not use concurrent
namespace replacement as a database locking protocol. See the
[compatibility matrix](docs/compatibility.md) for the implemented native mode and remaining gaps.

## Native database mode (experimental)

`tidbfs+db:///warehouse.duckdb` runs DuckDB's own storage engine over remote
the general `/v1/file-volumes` API, backed by 64 KiB TiDB pages. The plugin maps
database/WAL/spill files to ordinary paths and explicitly marks spill directories
ephemeral; the server has no DuckDB format or filename rules. The server checks a renewable lease inside every storage
transaction. Database and WAL writes are durable on acknowledgement; no whole
file upload is required at COMMIT. Single-writer exclusion, shared readers,
checkpoint/reopen, client-crash recovery and remote spill have live tests.

Enable `DRIVE9_EXPERIMENTAL_FILE_VOLUMES=1` on a **local-provider** fs server and use a
secret scoped to `tidbfs+db:///`. The initial implementation has a 1 GiB limit
per database and is not integrated with production fs backup, billing or tenant
deletion. This is progress toward compatibility, not a 100% compatibility claim.
See [setup, protocol and live tests](docs/native-database.md).

The value is twofold: analytical files let DuckDB's pruning reach remote byte
ranges, while native mode lets a short-lived process leave durable SQL state
that another process can recover. Queries still run locally in DuckDB. The
project does not turn DuckDB into a multi-writer distributed SQL engine, and
native-mode performance/cost still needs measurement and optimization.

## Read-path optimizations

* One metadata lookup per path/endpoint/credential **per query**, shared across
  DuckDB's row-group readers. Query end discards these entries; later queries
  revalidate. The selected file version is pinned across those readers.
* Reuse the selected object's presigned URL across readers. Direct object
  requests carry neither the filesystem bearer token nor its ETag. A backing
  object ETag pins subsequent requests; expired URLs get one conditional
  refresh through fs.
* Keep-alive HTTP connections per handle, with independent handles able to read
  concurrently. A handle retains at most 1 MiB of range data; HTTP read buffers
  are capped at 8 MiB. Directory response bodies are capped at 16 MiB.
* A 64 KiB tail/read-ahead window can combine Parquet trailer and footer reads.
  `SET tidbfs_read_ahead = 0` disables prefetch; allowed values are 0–1048576.
  Cached bytes remain handle-local and are released on close.
* Prefix-directed `*`, `?`, `[]`, and `**` globbing supplies ordinary file paths
  to DuckDB, preserving its Hive partition, column, and row-group pruning.

The matching **fs server changes are required**: strong inode/revision/size
ETags on HEAD and GET, conditional GET (`If-Match`), exact inline HTTP ranges,
and SQL `SUBSTRING`/`RIGHT` projections. Inline ranges retrieve only selected
blob bytes from TiDB while size and revision come from the same SQL statement.
The SQL projection reduces TiDB-to-server transfer and Go allocation; it does
not imply fewer TiKV storage-page reads. Large single-object content continues
to use object-store range GETs. SQL predicates still execute in DuckDB. See the
[server compatibility contract](docs/server-contract.md) before connecting an
existing deployment; this repository contains the client extension only.

## Verify

```sh
make test
```

The tests execute actual DuckDB CLI queries against an instrumented HTTP fixture:
column/row-group pruning, Hive and recursive globs, encoded paths, query cache
expiry, signed URL reuse/refresh, credential stripping, invalid ranges, short
responses, concurrent reads, changed revisions, and database-lock rejection.
The write tests cover format roundtrips, partition append/overwrite, temporary
rename, empty files, multipart upload/expiry/abort, concurrent publication,
deferred EXPORT failures, and remote export/import of tables and views.
The real TiDB smoke also verifies native COPY uploads, partitioned output,
temporary-file replacement and export/import following INSERT/UPDATE/DELETE.
The wide-footer fixture verifies that prefetch reduces two range requests to one.

For the real TiDB-backed inline path, start the updated fs server in local mode
with `DRIVE9_INLINE_THRESHOLD=16777216`, provision a disposable filesystem, then:

```sh
export DRIVE9_BASE=http://127.0.0.1:19009
# Supply the provisioned token via DRIVE9_API_KEY in the environment.
python3 scripts/smoke.py
```

This creates and deletes one unique `/duckdb-smoke/<uuid>/` directory. A local
counting proxy records actual HTTP body bytes, excluding headers/TLS. Each query
uses a fresh DuckDB process and verifies its SQL result. A 2026-10-17 local run
with 100,000 synthetic rows and a 4,211,473-byte inline Parquet file measured:

| Query | HTTP body bytes | HEAD | Range GET |
| --- | ---: | ---: | ---: |
| `sum(id) WHERE id < 2048` | 131,072 | 1 | 2 |
| `sum(length(payload))` | 3,859,261 | 1 | 11 |

These queries intentionally read different columns/row groups, demonstrating
that DuckDB pruning reaches the remote byte path. They are not a latency
benchmark or evidence of speedup over S3. The smoke's 16 MiB inline threshold
is an explicit test setting; the server's default remains unchanged.

## Boundaries

The analytical `tidbfs:///` path supports **read/write analytics files** and
rejects native database locks and extent-layout files. Experimental native
storage uses the separate `tidbfs+db:///` protocol above. No dataset-wide
transaction/snapshot is implied for analytical files. Inline changes
fail with HTTP 412; single-object reads can finish from their pinned immutable
generation, or fail if that generation is garbage-collected. Query-level
caching does not provide a transaction-wide snapshot across SQL statements.

Backing object stores must return a strong object ETag and exact 206 ranges.
The current local S3 mock lacks object ETags, so the real local smoke explicitly
uses TiDB-inline content; signed-object behavior is covered by the HTTP fixture.
Filesystem and object validators are different: clients must not automatically
forward filesystem conditional headers to a redirect target. HTTPS is required
except for same-origin loopback HTTP in local development. Transport errors
exclude tokens, signed URLs, and response bodies.

Reference: DuckDB's [filesystem interface](https://github.com/duckdb/duckdb/blob/v1.4.2/src/include/duckdb/common/file_system.hpp)
and [C++ extension entry point](https://github.com/duckdb/duckdb/blob/v1.4.2/src/include/duckdb/main/extension/extension_loader.hpp).

## License

[MIT](LICENSE). The build downloads nlohmann/json (MIT); DuckDB is MIT-licensed
and libcurl uses the curl license. Their respective licenses remain applicable.
