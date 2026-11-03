# Native database attach attribution

`tidbfs+db:///` attaches a native DuckDB database. This benchmark does not measure
Studio preview, Studio scheduling, or Studio's complete request latency.

## Reproduce

Build an unchanged baseline and the candidate with the same DuckDB release and
pinned C++ SDK. Keep each extension named `tidbfs.duckdb_extension` in separate
directories (DuckDB derives its entrypoint from that name). The benchmark accepts
`--baseline-extension /absolute/path/baseline/tidbfs.duckdb_extension`.

```
python3 scripts/test_native_live.py \
  --tidb-server /path/to/tidb-server --fs-server /path/to/drive9-server \
  --rustfs-server /path/to/rustfs --attach-benchmark \
  --baseline-extension /path/to/baseline/tidbfs.duckdb_extension \
  --bench-samples 10 --bench-delay-ms 20 --log-dir build/attach-benchmark
```

This creates disposable TiDB databases, filesystem credentials, RustFS storage,
and a tiny checkpointed native database, then tears down the services and data.
It uses no cloud credentials. Reuse the installed RustFS executable. CI runs
three samples per scenario with these local fixtures, checks query results and
startup/session counts, and uploads only `attach-*.json`. A baseline comparison
also requires the candidate to reduce cold attach request count; timings are
reported without a noisy wall-clock threshold.

An explicitly opted-in remote run uses `DRIVE9_BASE` and `DRIVE9_API_KEY` from
the environment, never command-line credentials:

```
python3 scripts/benchmark_attach.py --allow-remote --samples 20 \
  --region your-region --output build/remote-attach.json
```

Use a disposable filesystem. Each invocation creates a unique database and deletes
only that database and its WAL in `finally`. It does not list or delete unrelated
files. Credentials are neither persisted nor printed; SQL is passed over stdin.
Failures print only fixed diagnostic categories. Never upload service logs from
an actual remote run: service-side logs have their own privacy policy.

## What is measured

A persistent CLI controller records process readiness (spawn through first SQL
response), extension load, secret setup, ATTACH, catalog read, first data query,
DETACH, and fresh-process close. A random stdout marker delimits each stage.
Catalog results are consumed without being recorded; the data query must return
42. Controller/pipe overhead is included in stage durations.

The same checkpointed one-table database is read repeatedly in three lifetimes:

- A fresh process and DuckDB instance for every attach.
- A retained instance, detaching and reattaching the database. Its first attach
  is reported separately as `same_instance_cold`; the remaining samples are warm.
- A retained process with `.open :memory:` replacing its DuckDB instance before
  each reattach. Instance disposal is measured separately.

A loopback HTTP proxy records only allowlisted operation names, status, duration,
and anonymous socket sequence numbers. It adds 0 or 20 ms per request, not per
TCP packet. It preserves keep-alive on both sides and never retries a request,
follows a redirect, records a body, or emits a path, token, session/inode ID,
signed URL, hostname, or file contents. Unrecognized operations become `other`.
Per-stage request counts are observed metadata HTTP round trips. Startup calls
(`init`, `load`, counter allocation, `new_session`) and data-credential acquisition
are attributed individually inside ATTACH. File resolution, locking, open refs,
and reads likewise appear by operation. Close includes unlock/ref/session calls.

The report includes median, nearest-rank p95, extrema, sample count, request
histograms, per-operation distributions, distinct downstream sockets, upstream
TCP/TLS connection count and connect duration, DuckDB version, SDK and plugin
base revisions, extension digest, service binary digests, platform, region and
cache conditions. Binary digests distinguish uncommitted candidate builds from
the baseline even when their plugin base SHA is identical. For an unpublished
build, record the source diff alongside its digest. Ten samples give only a coarse
p95 (the maximum), not a production tail-latency estimate.

Server/OS caches are warm after creation. Fresh processes have cold client caches;
retained instances keep SDK caches. Direct object-store traffic bypasses this
metadata proxy. It does not separate SDK-side DNS/TCP/TLS handshakes or object
transport from other work inside ATTACH. Upstream connection timing belongs to
the measurement proxy, not the SDK. An HTTPS remote run changes the TLS boundary
by terminating the SDK connection on loopback and originating TLS at the proxy;
use it to attribute metadata calls, not to claim direct WAN transport performance.

## Optimization and correctness

The adapter previously called `Stat` before every `Open`. The pinned SDK already
resolves the path in `Open`, validates the node, establishes its reference and
lock, and refreshes attributes after lock acquisition. Non-creating nullable
opens can return null on the SDK's ENOENT result (code 2) without a preliminary
stat. Other errors still propagate. The SDK unwinds references and locks before
an open failure is returned. Required session/heartbeat operations and lock-time
attribute refreshes are unchanged.

Creating/overwriting opens with NULL_IF_NOT_EXISTS retain the preflight, because
calling Open would otherwise create the missing file. NULL_IF_EXISTS retains its
preflight as well. Exclusive create remains enforced by the SDK/server. The
adapter caches no existence result or authorization decision and replays no
writes. Existing clients remain scoped to a DuckDB instance and keyed by endpoint
and effective token, with heartbeat configuration fixed for that instance.

Regressions cover catalog transactions, checkpoint/reopen, multiple databases,
reader/writer exclusion, credential replacement, shared session ownership across
URI routes, heartbeat configuration, actual missed-heartbeat expiry, dropped
write acknowledgements, metadata timeout, object download failures/truncation,
baseline interoperability, and client/server restart with WAL recovery. The
proxy unit test checks connection reuse, redaction and no replay after a dropped
write acknowledgement. Token revocation and multi-tenant authorization remain
server responsibilities; this optimization adds no authentication cache.

## Measured result (2026-11-03)

[Summary distributions and binary identities](benchmarks/native-attach-2026-11-03.json)
record macOS arm64, DuckDB 2.0.0, TiDB 8.5.3 and RustFS 1.0.4, with SDK and
public server both at `42a316a20a71d3d7b789053ed020b45ba633aedc`. The unchanged
plugin baseline is `da3e1ac22bfeda4147b452eac2260a893eadf20f`; the candidate adds
this PR's adapter change. No SDK source or revision changes are needed.

With 20 ms added to every metadata request:

| Lifetime | Samples | Requests baseline → candidate | Median ms baseline → candidate | p95 ms baseline → candidate |
| --- | ---: | ---: | ---: | ---: |
| Fresh process | 10 each | 28 → 25 | 724.46 → 631.91 | 750.75 → 676.40 |
| New instance, same process | 10 each | 28 → 25 | 743.81 → 653.09 | 765.47 → 683.48 |
| Same instance, warm | 9 each | 23 → 20 | 606.65 → 526.48 | 628.76 → 567.05 |

Cold ATTACH's median improved by 12.8% in this controlled run. The directly
attributed improvement is one fewer `getattr` and two fewer `lookup` calls per
attach, while open refs, locks, reads and startup calls retain their counts.
Three removed requests eliminate 60 ms of deliberately injected delay; the rest
of the observed difference includes service and scheduler variation.

Each fresh process/new instance established one observed metadata connection;
the nine warm attaches reused their instance's existing connection with zero new
upstream connections. Each new instance acquired a credential and performed all
four required extent startup operations. Warm attaches performed none of those
startup operations. No new session pool is necessary in the plugin.

The zero-delay observations are also archived, but were collected alongside
other local conformance processes and show substantial order/load sensitivity.
Do not use their large apparent speedup as evidence of low-RTT performance.
The reliable attribution is the request-count reduction and controlled-delay
comparison; these results are neither a WAN measurement nor a Studio SLA.

Verification used the exact pinned public server for the standalone SDK live
suite, catalog persistence, and credential replacement, and server source
`f21d5acde9b1a3cb97935e3447671a31227f22af` for the existing native crash, lease,
failure, lock and interop regressions. The public server does not provide the
current server's transactional stale-session cleanup. Older cached test server
binaries were rejected by the SDK directory conformance suite, so they were not
used to claim SDK compatibility. CI retains its existing pinned-public-server
scope, including its explicit restart/expiry exclusions.

## Process boundary and follow-up

In-process reuse cannot survive process exit. Warm instance measurements retain
one existing credential and extent session; `.open` and process replacement must
register new sessions. These startup calls are dependent and are not removed or
parallelized. A preliminary candidate that skipped only ordinary opens did not
reduce native ATTACH requests: DuckDB uses nullable opens on this path. The final
change handles that contract explicitly.

Studio can separately evaluate a bounded worker lifetime that retains a DuckDB
instance between tasks. That work must own per-credential isolation, invalidation,
limits, cancellation and crash cleanup. These plugin measurements do not establish
how much of Studio's previously observed 6.1–6.4 seconds belongs to process setup,
network, or other application work. No daemon, SDK pin change, public protocol,
storage-format change, or deployment is part of this patch.
