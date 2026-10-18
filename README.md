# duckdb-tidbfs

[![CI](https://github.com/siddontang/duckdb-tidbfs/actions/workflows/ci.yml/badge.svg)](https://github.com/siddontang/duckdb-tidbfs/actions/workflows/ci.yml)

`tidbfs` is a C++ DuckDB filesystem extension for read-only analytics over the
TiDB Filesystem HTTP data plane. It works in the DuckDB CLI and does not require
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
`CMakeLists.txt` and `extension_config.cmake` also provide an entry point for a
DuckDB source build; the installed-SDK build is the tested local workflow.

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
```

`ENDPOINT` is the filesystem data-plane base URL; `/v1/fs` is appended by the
extension. The URI contains an absolute filesystem path, not a hostname.
Use separate scoped secrets to map different path prefixes to different
filesystems. Tokens are redacted by `duckdb_secrets()`. In automation, send SQL
through stdin rather than placing credentials in process arguments.

The build is unsigned and is not published to DuckDB's extension repository.
`INSTALL tidbfs` from a public repository is not available yet.

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
responses, concurrent reads, changed revisions, and read-only enforcement.
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

This version supports **read-only Parquet/CSV/JSON and other seekable analytics
files**. It does not implement writable `.duckdb` databases, WAL/locking,
`COPY TO tidbfs`, or extent-layout files. Use the existing client/FUSE for those
workflows. No dataset-wide transaction/snapshot is implied. Inline changes
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
