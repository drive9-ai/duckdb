# Pure C++ client: validation and performance

The extension now uses the public [drive9 C++ HTTP SDK](https://github.com/mem9-ai/drive9/pull/1056).
`cpp-sdk-revision.txt` pins its exact source. There is no Go bridge, embedded Go
runtime or companion runtime library. Existing server endpoints, sessions,
locks, open references, object keys and slice layouts are reused unchanged.

The analytical path continues to use `/v1/fs` conditions, Range and revision CAS.
Native and existing extent files use `/v1/extent/meta` and
`/v1/data-credential`, then direct immutable object I/O. Acknowledged writes
still wait for object upload and metadata publication. The SDK coalesces block
downloads, shares immutable cache buffers and copies requested bytes into
DuckDB's destination without a language boundary.

## Validation

The independent SDK tests cover binary and sparse reads, slice overlays,
large session IDs, lost acknowledgements, late publication after session
cleanup, credential binding, concurrent readers and close/read ordering.
Network tests cover bounded requests, pool recovery and redirect rejection.
Cloud credential regressions cover omitted S3/GCS endpoints, authoritative
signing regions, generated AWS partition DNS data, custom/FIPS/VPC endpoints,
TLS-safe dotted-bucket addressing and scoped GCS OAuth. These protocol fixtures
do not claim production AWS/GCS fault certification; live object tests use RustFS.
Linux SDK CI enables address and undefined-behavior sanitizers; macOS runs the
same suite. The integration harness runs direct C++ protocol conformance before
the native database and Quack suites, using isolated TiDB/TiKV and RustFS.

`TIDBFS_INTEROP_EXTENSION=/path/to/prior/tidbfs.duckdb_extension` enables the
prior-client compatibility test. It creates a database with the prior client,
reads and changes it with C++, then reopens it with the prior client. It also
reads C++-created extent Parquet through the prior client's ordinary URI route.
The prior client then writes again while C++ files remain live, checking both
slice-ID and inode-ID reservation boundaries. This regression reproduces an
I/O failure with the initial C++ draft; the SDK now uses the same half-open,
batched ID reservations as the existing clients and supplies explicit inodes.
This requires only a historical test artifact; Go is absent from the current
plugin build and runtime.

## Comparison method

The new comparison uses `scripts/benchmark_quack.py` with three independent
Quack workers: pure C++ tidbfs, the previous optimized Go-backed tidbfs and local
files. DuckDB, Quack, server, RustFS, data, thread counts and durability policy
are held constant. Runs are sequential, backend order rotates, and each
configuration has three trials. Measurements include throughput, p50/p95/p99,
worker/server CPU and RSS, object/metadata operations, integrity and host paging.

Streaming scans exceed the worker's buffer pool. The mixed workload contains
90% reads and 10% writes. Hot reads, single-row commits and 128-row commits
exercise different bottlenecks. A whole comparison triplet is excluded if any
backend exceeds the predefined host paging limits. The complete raw data and
eligibility decisions are retained. These are same-host loopback measurements;
they do not predict WAN or production object-store latency.

The earlier [Go bridge report](read-performance.md) is retained as historical
evidence. New measurements will be recorded here before the migration PR is
ready for review.
