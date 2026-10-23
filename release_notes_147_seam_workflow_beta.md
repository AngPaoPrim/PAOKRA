# PAOKRA Studio v1.4.7 Seam Workflow Beta

## Focused fixes
- Scene Builder can load the configured Clip Flow OPEN clip as a stopped preview when the selected source is empty or points to a missing file. This lets users set Fit/Output before building seam frames, without starting Engine just to reveal the picture.
- Both Start Engine in Playlist mode and the Playlist Start button now preflight the directed seam matrix before playback.
- When AI Seam is disabled or any reachable directed pair is missing/incompatible, Start asks before repairing frames. Confirming runs Auto Make Frames for missing/incompatible pairs, enables AI Seam, and verifies the full matrix before allowing playback.
- If preparation fails or is cancelled, Engine remains stopped instead of silently falling back to hard cuts. A Virtual Camera stopped for generation is restored when preflight fails.
- The first Make Frames workflow now opens Clip Flow OPEN as a stopped Scene Builder preview when there is no valid source, so Fit can be chosen before the first generation pass. This removes the old two-pass loop: generate, start Engine to reveal the picture, change Fit, then regenerate.
- Before deciding whether cached bridges are ready, Playlist Start negotiates the real Virtual Camera output mode. In Source FPS mode it starts from the configured OPEN clip, then respects the FPS the local driver actually accepted.
- The negotiated FPS is retained while the camera is paused for regeneration. The backend re-checks the directed seam matrix against the actual camera profile before playback; if a direct/API start bypasses preflight and the profile changes, the start is blocked instead of silently making hard cuts.
- Runtime diagnostics now record why each directed pair was not used (missing local cache, source signature changed, FPS/Fit/aspect mismatch, old algorithm, or settings mismatch) and show the latest lookup reason in Clip Flow status.
- Device-specific cache/profile differences are handled by rebuilding only unavailable or incompatible pairs on that PC using its current Output/FPS/Fit and available compute environment.

## Validation
- Clip Flow UI/static regression: 35 PASS.
- Seam workflow regression (one-pass preview, output negotiation, automatic matrix repair, camera cleanup, and runtime diagnostics): 8 PASS.
- Directed transition matrix, random playlist matrix, Stop wait-budget, Make Frames reservation/concurrency, Open FPS profile, Python compile, JavaScript syntax, and git diff check: PASS.
- Cross-computer behavior still requires testing the installed package on the second PC; source-level tests cannot verify that machine's drivers, local media paths, RIFE/model availability, or live virtual-camera output.
