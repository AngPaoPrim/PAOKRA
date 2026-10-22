# PAOKRA Studio v1.4.5 Beta 1

**Pre-release / QA build. Do not treat as stable.**

## Make Frames / AI Seam
- Reserve Engine, Virtual Camera, Scene/Output settings, Source selection, seeking, and Playlist edits across the entire Auto Make Frames batch, including the gap between generated transition pairs.
- Verify generated transition cache entries after each job with bounded retries, and prevent stale asynchronous seam-list responses from replacing newer results.
- Retry transient AI Seam job-status reads instead of abandoning a long batch immediately after one polling failure.
- Publish a generation job as `complete` only after the generation lock is released, avoiding the race where the next pair fails with a false “another job is running” response.
- Keep the last valid composited frame during Engine start while the decoder fills its first frame buffer, reducing a black flash during pre-roll.

## QA included
- New backend integration test for batch reservation acquire/heartbeat/release/expiry and rejection of conflicting Start/Seek/Source/Scene/AI Seam mutations.
- Extended Clip Flow / Scene Builder UI regression checks for batch protection, stale cache polling, generation completion ordering, and Engine hold-frame behavior.
- Existing directed playlist matrix, AI Seam/RIFE inference, cache integrity, Scene Builder, media upload and packaged Engine/Virtual Camera regression suites are run by the installer build script.

## Known beta caveats
- The current local regression checks exercise synthetic concurrent requests and existing media fixtures; they cannot guarantee behavior for every codec, variable-frame-rate clip, GPU driver or long-running LIVE session.
- Full manual GUI verification on a separate 8 GiB VRAM computer is not available in this build environment. Keep v1.4.4 stable installed and use this beta only for focused testing until more real-world logs are reviewed.