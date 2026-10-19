# Shortening interrupted handoff — 2026-10-18

With a 1-second heartbeat, interrupted writer takeover took **4.75 seconds
median (4.48–5.28 seconds, three runs)**, compared with **57.68 seconds** for
the default 12-second heartbeat in the same environment. A 2-second heartbeat
took **9.72 seconds median (9.72–10.29 seconds, three runs)**.

The delay was mainly session expiry. The shared extent runtime derives the
session lifetime as five heartbeat intervals: about 60 seconds by default,
10 seconds with a 2-second interval, and 5 seconds with a 1-second interval.
Successful ATTACH itself took **28–39 ms** across these seven runs.

These are local experiments with TiDB unistore and RustFS, not a production
latency guarantee. They extend the [original interrupted-handoff demo](../demos/interrupted-handoff/README.md).
The original 57.84-second GIF remains a record of its original configuration.

## Configuration

In a **fresh DuckDB instance**, before accessing any extent file:

```sql
LOAD 'build/tidbfs.duckdb_extension';
SET GLOBAL tidbfs_heartbeat_ms = 1000;
-- Create the filesystem secret, then ATTACH the database.
```

Use explicit `SET GLOBAL`, since database and WAL opens must see the same
instance-wide setting. The default stays 12,000 ms; valid values are
1,000–600,000 ms. Changing the setting after creating the extent client does
not replace its session: subsequent opens with a different value fail and
require a fresh instance. Use `2000` for a less aggressive profile.

Start the filesystem server with its existing maintenance setting:

```sh
DRIVE9_TENANT_MAINTENANCE_INTERVAL=1s /path/to/drive9-server
```

The server's default maintenance throttle is 5 minutes. **The original
57.84-second demo already used 1-second maintenance**, so lowering maintenance
alone would not address that measured delay. All runs below use 1-second
maintenance and a 200 ms sleep between the replacement worker's ATTACH
attempts. That retry loop belongs to the test/application; the extension does
not automatically retry failed writes.

Short heartbeats require the server to advertise `session_guard: true`.
The plugin retains a one-heartbeat safety margin for the 1 s and 2 s profiles,
bounds metadata requests by the remaining safe lifetime, and permanently stops
the client after ambiguity or expiry. A late response cannot revive the old
session. No API, table, or server-specific DuckDB lease was added for this tuning.

The cost is increased renewal and associated metadata traffic: a 1 s interval
has approximately **12× the renewal frequency** of the default. It also
tolerates less network delay or process scheduling disruption. Measure those
conditions before selecting a production setting. Cleanup scheduling, retry
timing and WAL recovery still contribute; a 5 s lifetime is not a hard 5 s
takeover bound.

## Measurements

The performance matrix completed on October 18, 2026, 20:34–20:38 PDT.
Runs were sequential on one Apple M4 Mac, 10 CPU cores, 24 GiB RAM,
macOS 27.0.1 arm64. Workers used DuckDB 1.4.2; metadata used TiDB 8.5.3
unistore; object I/O used RustFS 1.0.3. All traffic was loopback.

| Heartbeat | Approx. session lifetime | Runs | Disconnect → successful ATTACH, median (min–max) |
|---|---:|---:|---:|
| 12 s (default) | 60 s | 1 | 57.685 s |
| 2 s | 10 s | 3 | 9.724 s (9.720–10.290) |
| 1 s | 5 s | 3 | 4.748 s (4.479–5.278) |

The 1 s profile reduced observed takeover time by about **92%** relative to
the new default control. One control run and three runs per tuned profile
are too few to establish tail latency. No timed matrix sample was discarded.
The historical 57.845 s run used a 1 s retry sleep, so it is not substituted
for the new default control, which uses the same 200 ms retry sleep as the
tuned runs.

Timing starts when the controller cuts A's two network routes and ends when
B's successful ATTACH completes, including WAL recovery. Failed attempts,
retry sleeps and fresh worker bootstrap are included. Each attempt's boundaries
are retained in the raw results. ATTACH alone is timed separately; the first
full verification scan follows it and is not included in takeover time.

### Fault and consistency checks

A checkpoints 100,000 rows, commits row 100,000 into a 167-byte WAL, and leaves
row 100,002 uncommitted. Two per-worker socket relays then cut A's filesystem
metadata and S3 connections. A remains running and answers `SELECT 42`.
B uses independent routes; the harness neither edits session/lock rows nor
calls manual cleanup. Once B acquires the lock, it recovers A's acknowledged
WAL, commits row 100,001, and keeps the lock while A's routes are restored.
A then attempts another INSERT and COMMIT.

**All seven matrix runs passed:**

- B recovered all 100,001 acknowledged rows with the expected full-row SHA-256.
- A's attempted stale COMMIT failed in 5.98–7.28 ms after reconnection.
- B and a fresh read-only worker both verified all 100,002 final rows.
- A's uncommitted and attempted stale rows were absent.

Verification hashes every `id,payload\n` row in ID order and compares it with
an independently generated expected stream; it is not only a row-count check.
Payloads are `md5(id::VARCHAR)`. Expected checksums:

| State | SHA-256 |
|---|---|
| A before disconnection; B after recovery | `c9cefafcfd28fe3f120552a4f1e1da5fcf9408ac25b347c2a7f708a6e886fba9` |
| B after its commit; B after stale rejection; fresh reader | `d86a3978b55958cc6671d0af8e56f0a210d8c5993afde7a6e88f7afdab6f5b80` |

The first 1 s and 2 s matrix runs also held a healthy writer for at least
15 s and 25 s before disconnecting it, repeatedly confirming that other
writers could not take its lock. Those checks prove renewal/exclusion; their
same-value UPDATE is not used as evidence of a state-changing write.

### Additional renewal/write validation

Two separate validation runs use the same engine binary with a stronger
healthy-phase check: after several session lifetimes, A commits a changed
payload and then commits the original payload back. Both acknowledged state
changes occur before the network cut. The full-row checksum is checked again,
and the resulting **482-byte WAL** must recover correctly on B.

Both runs passed renewal, exclusion, write, stale-rejection and full-row
recovery checks:

| Heartbeat | Healthy interval | Excluded contenders | Acknowledged state changes | Takeover | Stale COMMIT rejection |
|---|---:|---:|---:|---:|---:|
| 1 s | 16.073 s | 15 | 2 | 5.283 s | 6.790 ms |
| 2 s | 25.691 s | 24 | 2 | 9.791 s | 9.846 ms |

These runs are listed separately because they add writes and change
the WAL workload; they are not silently pooled into the three-sample timing
cohorts above.

## Reproduce and inspect

[Raw results directory](handoff-tuning-2026-10-18/) contains the original JSON
for all seven matrix runs and the two additional validation runs, including
attempt timings, socket events, checksums, session settings and binary hashes.
It omits credentials and endpoints; raw service logs are not published.

Build the extension as described in the repository README. Python requires
`pymysql` and `boto3`. Supply TiDB, RustFS and a compatible guarded server:

```sh
python3 scripts/demo_interrupted_handoff.py \
  --tidb-server /path/to/tidb-server \
  --fs-server /path/to/guarded/drive9-server \
  --fs-source /path/to/guarded/fs-checkout \
  --rustfs-server "$PWD/build/rustfs" \
  --heartbeat-ms 1000 --poll-interval 0.2 \
  --healthy-seconds 15 \
  --log-dir build/handoff-tuned
```

Use `--heartbeat-ms 2000 --healthy-seconds 25` for the other renewal check.
The harness starts disposable services, stops its own child processes, and
removes runtime storage on exit. It never changes the host's network settings.

Measured revisions:

- Extension engine and matrix harness:
  `4a1f685aa42e3123734dd1a639bf27bcaf915f59`.
- Additional validation harness: `96f68076d3940c8038f3857f64ba844237ab4023`
  (test-only change; same engine binary).
- Shared extent client: `67deed055d66c9cac05dbe752eeee6700559fc6c`, pinned via
  `v0.10.1-0.20261019032931-67deed055d66`.
- Server: `1a4967b61ac8ca6105945aa5d4955d3b5265495b`.

An initial preflight found a setting-scope mismatch before database ATTACH
completed. It was fixed by requiring `SET GLOBAL`, before any matrix run.
The source hash stored in each result identifies its exact executed harness.

Related public PRs: [DuckDB plugin #1](https://github.com/siddontang/duckdb-tidbfs/pull/1)
and [shared extent client #1039](https://github.com/mem9-ai/drive9/pull/1039).
Reproducing this exact server build requires access to
[fs #241](https://github.com/tidbcloud/fs/pull/241), which provides the generic
transactional session guard. The independent `/v1/fs` conditional-read PR #238
is not that guard.

Local validation also passed the SDK extent suite and vet, bridge tests and
vet, extension build/LOAD, all 25 analytical fixtures, live bridge conformance
and delayed-publication regression, and native checkpoint/reopen plus
live-setting immutability tests. These checks are distinct from the timed runs.

## Boundaries and public reply

This is a small 100,002-row failure-recovery scenario, separate from the
[3.20 GiB clean-handoff benchmark](handoff-2026-10-18.md). It does not establish
large-WAL recovery time, WAN behavior, silent/asymmetric partitions, clock
skew, production TiKV failover, or behavior under metadata overload.
This reconnect scenario rejects the stale COMMIT in the client; the server's
delayed-publication guard is covered by a separate regression. Existing inode
locks remain advisory and require cooperating clients.

> I made the session heartbeat configurable and repeated the disconnect /
> takeover / reconnect test. With a 1 s heartbeat (~5 s session lifetime),
> takeover was 4.75 s median across three local runs, versus 57.68 s with the
> default. A 2 s heartbeat gave 9.72 s.
>
> Every stale writer was rejected after reconnect, and all 100,002 committed
> rows matched the expected checksum. Shorter sessions trade more metadata
> traffic and less tolerance for stalls for faster takeover. This is still an
> experiment with cooperating clients and advisory locks.
