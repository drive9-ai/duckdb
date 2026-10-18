# Experimental native database storage

This is a native C++ filesystem implementation under DuckDB's own database
engine. There is no server-side SQL execution and no close-time database upload.

## Run

Build the extension and the matching fs server changes. Use a disposable TiDB
instance or database; the file-volume tables are outside ordinary fs backup/metering.

```sh
export DRIVE9_TENANT_PROVIDER=local
export DRIVE9_EXPERIMENTAL_FILE_VOLUMES=1
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

## Client policy over a generic API

The server exposes `POST /v1/file-volumes`, not a DuckDB endpoint. The protocol
provides a caller-selected volume, ordinary absolute file paths, shared/exclusive
leases, random reads/writes, truncate, sync, atomic file rename, unlink, listing
and explicitly ephemeral creation. No distinguished database file, extension,
WAL format or checkpoint sequence exists in the server contract.

The plugin implements the mapping:

| DuckDB URI | Generic volume | Path inside volume | Creation lifetime |
| --- | --- | --- | --- |
| `tidbfs+db:///warehouse.duckdb` | `/warehouse.duckdb` | `/data` | Persistent |
| `tidbfs+db:///warehouse.duckdb.wal` | `/warehouse.duckdb` | `/data.wal` | Persistent |
| `tidbfs+db:///warehouse.duckdb.tmp/part` | `/warehouse.duckdb` | `/data.tmp/part` | Inherited from ephemeral `/data.tmp/` |

The first `.duckdb` marker, sidecar mapping, main-handle lifetime and selection
of temporary directories are **plugin policy** in `src/native_database.hpp`.
Directories created through native mode are spill workspaces and explicitly use
`ephemeral=true`; analytical directories continue to use ordinary `/v1/fs`.
The generic HTTP and lease client lives separately in `src/file_volume.hpp`.
A plain file-volume client can instead use `/search-index` with `/segments/1.bin`
and `/scratch/`, without importing or installing DuckDB.

Each lease covers all files in its volume. This plugin uses one volume per
native database, one exclusive writer or multiple read-only processes, and a
30-second lease renewed every 5 seconds. The server validates the lease and
commits mutations in the same transaction. Stable file IDs survive rename and
unlink until the ownership epoch ends. Explicit temporary entries and detached
inodes are reclaimed when no lease remains; filenames alone never trigger GC.

Writes are synchronous to TiDB. DuckDB itself orders WAL writes, sync, checkpoint
headers and WAL removal through the plugin's file operations. Its own engine
performs recovery after a crash. The server stores opaque bytes and enforces
ownership; TiDB deployment settings determine physical durability. A multi-call
write is not atomic as a whole and there is no multi-file server transaction API.

Any RPC failure poisons the client session. Mutations are never blindly replayed:
a lost acknowledgement can mean a committed operation. Reopen and reconcile the
application transaction; a crashed owner's lease can delay reopening by 30 seconds.

Protocol v1 limits remain 1 MiB binary payload per call, 1 GiB total logical bytes
per volume and 100,000 retained entries. Directories use a trailing slash.
Errors are 400 invalid/limit, 403 read-only/scoped, 404 missing, 409 locked/existing,
410 expired lease and 501 disabled. See the complete [generic server contract](
https://github.com/tidbcloud/fs/blob/codex/duckdb-range-reads/docs/design/file-volumes.md).

This is a breaking revision of the unreleased prototype. The old specialized
endpoint, feature flag and tables are no longer used. No automatic table
migration or deletion runs. Export needed data with the previous client/server
pair before upgrading, then import with the new pair.

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
The harness also runs `scripts/file_volume_smoke.py`, a standard-library-only
HTTP client that exercises generic binary files, sparse writes, truncation,
rename identity, directory listing, shared reads and explicit temporary cleanup.
It has no DuckDB dependency and can also run alone with `DRIVE9_BASE` and
`DRIVE9_API_KEY` against a disposable filesystem.

The native smoke alone uses `DRIVE9_BASE` and `DRIVE9_API_KEY` and creates unique
`conformance-*.duckdb` databases; use a disposable filesystem because those
databases remain after the run. The current public CI covers the analytical
fixtures and native extension build; live native tests require the updated fs
server and are run separately.
