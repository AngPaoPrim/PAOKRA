# PAOKRA Studio v1.4.5 Beta 1 — Directed Make Frames / Open → Normal Fix

**Pre-release / QA build. Do not treat as stable.**

## Auto Make Frames / AI Seam
- Generate and verify every required directed transition from the configured Playlist before LIVE: Open → each Normal, Open → Stop, every distinct Normal → Normal pair in both directions, and every Normal → Stop.
- Fix the pre-LIVE Source-FPS profile: when output FPS is set to Source, generated seams and preflight validation use the configured Open clip's cadence—the same cadence Clip Flow uses to start the Virtual Camera—instead of a previously selected Scene Builder preview clip's FPS.
- Keep exact directed-pair lookup (A → B is never treated as B → A), verify source signatures and output compatibility, and only enable seam playback after the whole matrix is ready.
- Keep frame generation protected as one batch, including gaps between pair jobs; verify saved cache entries with bounded retries and prevent overlapping source, seek, settings and playlist mutations.
- Publish a generation job as complete only after the compute lock is released. Hold the last valid compositor frame during Engine pre-roll.

## Clip Flow / LIVE order
- Normal playback remains shuffled and does not repeat within a cycle.
- A matching generated bridge is played at the handoff; the destination clip resumes at the correct bridge endpoint.
- Stop waits for the active clip to finish, then plays Stop to completion, before the existing Protocol shutdown sequence proceeds.

## Version selector / updates
- Make **Settings → About → Choose version (Beta / Stable)** directly open the release selector.
- Show available GitHub Releases and retain HTTPS, installer-size and SHA-256 checks before allowing installation.

## QA included
- Clip Flow timeline / updater UI regression suite: 28 checks.
- Directed playlist matrix: 4 checks, including the full 13-edge case for Open + three Normal clips + Stop.
- Auto Make Frames reservation / conflict checks and Protocol Stop wait-budget checks.
- Source-FPS regression for a 24 FPS preview versus a 59.94 FPS Open clip, plus fixed-FPS preservation.
- AI Seam/RIFE source QA: actual Vulkan GPU and CPU inference, bridge MP4 integrity, exact/reverse directed-cache checks, and untouched source files.
- The installer script also runs Post-build tests against the packaged application, including Clip Flow/Virtual Camera transition insertion.

## Beta caveats
- Tests use deterministic local fixtures; they cannot guarantee behavior for every codec, variable-frame-rate file, GPU driver or prolonged real-world LIVE session.
- Keep the stable release installed until the new beta has been verified with your own playlist and full LIVE/Protocol flow.
