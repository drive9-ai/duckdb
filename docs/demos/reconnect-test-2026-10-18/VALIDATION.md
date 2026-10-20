# Delivery validation

- Live rerun completed successfully; original results copied byte-for-byte.
- Executed harness and relay hashes matched the recorded Git revision.
- Engine source and extension binary matched the previous validated build.
- All five ordered-row checksum checks matched independent expected data.
- Both disconnected routes failed independent probes; both restored routes
  passed before the stale write attempt. A remained alive until that attempt.
- Fresh reader found zero forbidden rows. No post-reconnect extent publication
  appeared in A's recorded proxy trace. Temporary runtime storage was removed.
- GIF: 1280×720, 12 fps, 336 frames, 28 seconds; 724,417 bytes.
- MP4: 1920×1080, 24 fps, 28 seconds; 481,295 bytes; intentionally silent.
- Both final media files decoded fully without errors.
- Seventeen selected timestamps rendered identically in reverse order.
- All on-screen text passed bounds and minimum reading-time checks.
- Inspected implementation keyframes, final GIF contact sheet and 480×270
  phone-size final frame. No clipped text or incorrect ownership reversal.
- Interactive browser playback was not performed: the browser rejected local
  `file:` URLs. No alternate browser route was attempted. Visual inspection
  used locally decoded frames, so full-speed interactive playback is not claimed.
