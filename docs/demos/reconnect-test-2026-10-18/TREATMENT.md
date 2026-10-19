# Disconnect, take over, reconnect — fresh measured run

The viewer should remember: this live test cuts the old writer's connections
without stopping its process; after another worker takes over, restoring the
old connections does not restore write ownership.

Three structures: a terminal recording (credible but too dense on a phone),
the previous latency comparison (answers tuning, not this fresh run), or one
persistent two-worker diagram with the actual event order and measured result.
Choose the last. Preserve the improved legibility of our handoff-tuning GIF.

Style: Siddon Visual Engineering v2 / Dark Tech Keynote, English silent social
GIF. Reference snapshot lemo-opuscar 508848acb2b4930c4041fef4d594120da1525bff;
AGENTS, DIRECTOR, TECHNIQUE, dark-keynote STYLE and core README were consulted
in this conversation and the unchanged snapshot was verified for this rerun.
Use the existing deterministic renderer and bundled OFL fonts. No external
demo story, footage, assets, voice, or music. No audio is appropriate for GIF.

Persistent objects: A on the left, B on the right, database below, and one
write-lock badge. Blue = current ownership, amber = disconnected/expired/rejected,
green = independently verified rows. Lines show logical client access to shared
metadata and object storage; they do not imply an S3 proxy in the FS HTTP server.

| Seconds | Initial state → event → consequence | Text and continuity |
|---|---|---|
| 0–6 | A has acknowledged WAL commits; B cannot write | A owns the same visible badge. Title: “One database. One writer.” |
| 6–13 | A's metadata and S3 sockets are cut while its process lives | Break A's existing line; keep A visible. B waits for expiry and cleanup. |
| 13–20 | B acquires the lock and recovers committed WAL | Move the badge to B once. Show actual takeover time from raw JSON. |
| 20–28 | B commits, A's network returns, A attempts COMMIT and is rejected | Restore A's line without moving the lock back. Hold verified rows and zero stale rows. |

Fixed camera, large status labels, no API list or tiny SQL transcript.
The signature shot is a working A network route under a rejected COMMIT,
while the ownership badge remains on B. All motion explains these changes.
One timeline drives titles and captions. The closing eight-second hold is
deliberate reading time. MP4 is 1920×1080/24 fps, GIF is 1280×720/12 fps.

This is a compressed replay of a new real run, not a live recording or a new
timing distribution. Every number is read from results.json, never invented.
The chosen configuration is 1 s heartbeat, ~5 s lifetime, 1 s maintenance and
200 ms retries on disposable local TiDB unistore + RustFS. The 100,002-row
correctness workload is distinct from the previous multi-GiB scan benchmark.
The observed rejection is client-side; advisory locks require cooperation.

Validation: raw/source/binary hashes, complete checksums, event order, actual
network probes and old-process liveness; keyframes at phone size, text bounds
and duration, shuffled-time determinism, final media decode and transitions.
