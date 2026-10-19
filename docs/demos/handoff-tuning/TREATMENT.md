# Faster interrupted handoff

The viewer should remember: tuning the existing heartbeat shortened local
writer takeover from about 58 seconds to about 5 seconds, while the tested
old writer still failed closed after reconnecting.

Question: can the earlier 57.84-second wait be shortened without letting an
expired writer resume publishing?

Three structures considered: a latency chart alone (clear timing, weak causal
proof); a terminal replay (too much reading); a persistent two-worker diagram
under a before/after metric (chosen: connects timing with observed behavior).

Siddon Visual Engineering v2, Dark Tech Keynote with one metric comparison,
not a plotted performance distribution. Silent English GIF for an English X
reply. 26 seconds, 1920×1080/24 fps source MP4 and 1280×720/12 fps GIF.
Style reference: lemo-opuscar 508848acb2b4930c4041fef4d594120da1525bff,
AGENTS, DIRECTOR, TECHNIQUE, dark-keynote STYLE, core README. Our previous
interrupted-handoff diagram supplies the implementation infrastructure and
continuity benchmark; no external demo story or assets are used.

Keep one near-black stage, neutral panels, off-white text and blue for current
ownership. Amber means disconnected or rejected; green appears only with
verified contents. Persistent identities are writer A, worker B, shared
database, and the write-lock badge. Lines mean logical client access to shared
storage; they do not claim that S3 traffic is proxied by the metadata server.

| Seconds | Before → event → consequence | Display and continuity |
|---|---|---|
| 0–7 | A owns the lock; B waits | Before/after result visible from frame one. Default 57.68 s is one control; tuned 4.75 s is median of three. |
| 7–13 | A's metadata and S3 sockets are cut; A stays running | Sever A's existing route, keep its worker visible, and show B waiting for expiry/cleanup. |
| 13–19 | Expiry and cleanup release A's lock; B opens and recovers WAL | Lock badge moves continuously from A to B. Shared database stays at the same anchor. |
| 19–26 | B commits; A reconnects and attempts COMMIT; client rejects it | Restore A's route without returning the lock badge. Hold rejected COMMIT and verified 100,002 rows. |

Signature transformation: connectivity returns but the lock stays with B.
Fixed camera preserves identity; no extra panels, animated counters, APIs, or
chart interpolation. Transitions are compressed event replay, not wall-clock
footage. Seven-second last hold leaves time to read the result.

All metrics load from the committed raw benchmark JSON. The representative
event run is heartbeat-1000-trial-1-v2, which is also the three-sample median.
One 12 s-heartbeat control is compared with three 1 s-heartbeat trials, all
with 1 s cleanup and 200 ms retries. Advisory locks require cooperating clients;
this is a local experiment, not a production bound or mandatory fencing claim.
The measured stale COMMIT fails in the client.

Sound/voice: intentionally none (GIF request). The four event titles drive
the SRT and picture from one timeline. Validate bounds, reading times,
shuffled-time determinism, source numbers, full media decode, keyframe contact
sheets and a phone-size frame. Reuse bundled OFL fonts with attribution.
