# Interrupted handoff — measured event replay

The viewer should remember: reconnecting an old DuckDB worker does not restore
its expired write ownership. This film will use measured events from the live
socket-disconnect test, never invented timing or a staged success.

## Three structures

1. Terminal-only recording: strong evidence, but waiting for expiry obscures the
   explanation on a phone.
2. Latency chart first: emphasizes a single timing sample too strongly.
3. Persistent two-worker diagram with actual SQL outcomes and measured time:
   chosen because the viewer can follow the old process through disconnection,
   takeover, reconnection and rejection without losing its identity.

## Direction

Siddon Visual Engineering v2, Dark Tech Keynote. Reference grammar:
lemo-opuscar revision `508848acb2b4930c4041fef4d594120da1525bff`, specifically
AGENTS, DIRECTOR, TECHNIQUE, dark-keynote STYLE and core README. No demo source
or story is copied. Learn continuity from a sequence diagram and readable
evidence from a terminal transcript; take no external film assets or shots.

Silent English social demo, matching the existing English X conversation and
silent architecture GIF. 34 seconds, 1920×1080 at 24 fps, plus a 1280×720 GIF.
No music, voice, or sound effects: the muted presentation is the intended output.
All beats share one timeline; MP4 and GIF show the same evidence. It is a
compressed measured-event replay, explicitly labeled, not a screen recording
or a real-time playback of the outage.

Persistent objects: writer A (left), writer B (right), shared database (below),
network routes and an ownership badge. Blue means active ownership; amber means
disconnection/expired session/rejected action; green only means checksum verified.
Neutral off-white headings and subdued inactive components. The filesystem box
represents shared metadata plus object storage; connecting arrows are client
I/O, not a claim that S3 bytes travel through the metadata HTTP server.

## Timeline and causal beats

| Seconds | Before → event → consequence | Framing and text |
|---|---|---|
| 0–6 | A holds the lock; confirmed commit is in WAL, another row is uncommitted | Wide stable diagram. “What if the old writer comes back?” |
| 6–13 | TCP routes to metadata and S3 are cut; A stays alive; B is locked out until cleanup | Same anchors; sever A's routes. “A loses connectivity. B waits.” |
| 13–20 | Natural session expiry + cleanup permits B's ATTACH; WAL is recovered and B commits | Ownership badge moves to B. Actual measured seconds. “B takes over.” |
| 20–27 | A's network is restored while B still owns the lock; A tries INSERT + COMMIT | A route reconnects, but A remains amber. “A reconnects. COMMIT is rejected.” |
| 27–34 | New reader verifies every ordered row; abandoned and stale rows are absent | Hold final proof and persistent ownership. “Connectivity returns. Ownership doesn't.” |

Signature transformation: the route reconnects, while the ownership badge stays
with B; the old COMMIT becomes a visible rejection. The final hold makes this
distinction legible. The camera stays fixed to preserve causal identity; no
decorative motion, no repeated small-print API lists. Key words are large enough
for a phone. Exact settings and checksums live in the linked report.

## Conditions and validation

One local run on real DuckDB, TiDB unistore and RustFS. A-specific HTTP socket
relays disconnect both storage routes; B uses independent routes. A is neither
paused nor killed to induce the handoff. Session expiry is unmodified; demo
maintenance throttle is 1 second and attach retries are 1 second apart.
Do not label this production partition safety, mandatory fencing, or multiwriter
support. This scenario validates the cooperating plugin's stale-session failure;
it does not independently exercise an in-flight delayed publication race.

Before delivery: verify every metric against JSON, inspect keyframes at full and
phone size, check text bounds/reading time, compare shuffled-time frame hashes,
decode the final media and check durations. Deliver source, build script, poster,
MP4, GIF, captions, raw evidence and reproducible test instructions.
