# Experimental native database storage

This is a native C++ filesystem implementation under DuckDB's own database
engine. There is no server-side SQL execution and no close-time database upload.

## Run

Build the extension and the matching fs server changes. Use a disposable TiDB
instance or database; the native tables are outside ordinary fs backup/metering.

```sh
export DRIVE9_TENANT_PROVIDER=local
export DRIVE9_EXPERIMENTAL_DUCKDB=1
export DRIVE9_LOCAL_DSN='root@tcp(127.0.0.1:4000)/drive9_local?parseTime=true'
# Start drive9-server and provision a token through POST /v1/provision.
```

In DuckDB 1.4.2:

```sql
LOAD 'build/tidbfs.duckdb_extension';
CREATE TEMPORARY SECRET dbfiles (
    TYPE tidbfs,
    ENDPOINT 'http://127.0.0.1:9009',
    TOKEN 'YOUR_PROVISIONED_TOKEN',
    SCOPE 'tidbfs+db:///'
);
ATTACH 'tidbfs+db:///warehouse.duckdb' AS warehouse;
USE warehouse;
CREATE TABLE events(id BIGINT PRIMARY KEY, value VARCHAR);
BEGIN;
INSERT INTO events VALUES (1, 'durable');
COMMIT;
CHECKPOINT;
```

A later process can `ATTACH` that same URI, including `(READ_ONLY)`. Existing
`tidbfs:///` analytics paths remain separate. Default secrets without an explicit
SCOPE cover both schemes; explicitly scoped secrets must include the new prefix.

## Protocol version 1

All calls are authenticated `POST /v1/duckdb` with JSON. Success is HTTP 200 and
includes `version: 1`. Every operation carries `database`, the canonical absolute
path without the URI scheme. Names are suffixes: `""` for the database, `".wal"`
for its WAL and `".tmp/..."` for spill files. File IDs are opaque UUIDs.

| Operation | Additional fields | Result |
| --- | --- | --- |
| `acquire` | `create`, `read_only` | `token`, `lease_ms: 30000` |
| `renew`, `release`, `sync` | `token` | Lease checked / renewed / released |
| `probe` | `name` | Existence/size only, without a lease |
| `stat`, `list` | `token`, `name` | File metadata / directory entries |
| `open` | `token`, `name`, `create`, `exclusive`, `truncate`, `directory` | `file_id`, `size` |
| `read` | `token`, `file_id`, `offset`, `length` | base64 `data`, current `size` |
| `write` | `token`, `file_id`, `offset`, base64 `data` | committed `size` |
| `truncate` | `token`, `file_id`, `offset` (new length) | committed `size` |
| `remove`, `rename` | `token`, `name`, `target` (rename) | Atomic file namespace change |

Errors: 400 invalid request/size limit, 403 read-only or scoped credentials,
404 missing file, 409 ownership/create conflict, 410 expired/invalid lease,
501 feature disabled. Other errors can mean an unknown commit outcome. The C++
client poisons the session on RPC failure and does not replay mutations.

The server locks the `duckdb_databases` row, expires leases using the database
clock, and validates ownership in the same transaction as each page or namespace
change. All dependent queries use current reads (`FOR UPDATE`), including after
a lock wait. Shared schemas include `fs_id` in every key and predicate, although
this endpoint is currently gated to local-provider deployments.

`duckdb_files` owns stable file IDs; `duckdb_pages` stores sparse 64 KiB pages.
Page writes and file-size updates commit together. Replacement/unlink detach the
name without retargeting open handles. Unlinked pages and abandoned `.tmp` files
are collected only with no active database lease. Lease rows store SHA-256 token
hashes so metadata reads do not expose reusable ownership capabilities.

All writes are synchronous to TiDB. A WAL sync must complete before DuckDB
publishes checkpoint headers or deletes the WAL. The single serialization row
preserves that order across handles and server processes. TiDB deployment
settings determine underlying replication and physical durability.

## Reproduce live tests

```sh
python3 -m pip install pymysql
make build
python3 scripts/test_native_live.py \
  --tidb-server /path/to/tidb-server \
  --fs-server /path/to/fs/bin/drive9-server \
  --fs-source /path/to/fs
```

The harness launches and terminates only its own processes, creates temporary
TiDB data, clears inherited `DRIVE9_*` configuration, and uses random local
signing keys. `--fs-source` is optional. Logs go under ignored `build/native-live/`.
The native smoke alone uses `DRIVE9_BASE` and `DRIVE9_API_KEY` and creates unique
`conformance-*.duckdb` databases; use a disposable filesystem because those
databases remain after the run. The current public CI covers the analytical
fixtures and native extension build; live native tests require the updated fs
server and are run separately.
