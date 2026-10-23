# PAOKRA Studio v1.4.8 Seam Smoothness Beta

## System Update / version picker
- Fix the System Update FIX version selector when GitHub Releases API returns an error such as HTTP 403: fall back to the public GitHub Releases Atom feed and read each release's latest.yml.
- Only list releases whose latest.yml identifies the expected PAOKRA-Studio-Setup.exe and includes both installer byte size and SHA-512 digest.
- Verify both byte size and SHA-512 after download before finalizing the installer; keep HTTPS host and redirect allowlists in place.
- Show when the picker used the fallback feed rather than silently reporting an ordinary API result.

## Clip Flow / AI Seam handoff
- Pre-open and decode the first frame of the exact cached directed Seam bridge while the current OPEN/Normal clip is still playing.
- At the same time, pre-open the destination B capture at the exact saved resume timestamp, so the app does not have to initialize two decoders at the handoff boundary.
- Reuse the already-open capture and prepared first frame when switching sources; retain the existing fallback path if preloading is unavailable.
- Use a short 35-ms pre-roll for successfully preloaded Clip Flow sources rather than holding the outgoing frame for the generic 300-ms cold-start lead.
- Bound the preload cache, expire unused captures, invalidate in-flight preload work when a Flow ends/restarts, and key captures by file version so replaced media cannot reuse an obsolete decoder.
- Show explicit runtime evidence in Clip Flow: cache lookup reason, whether the seam's preloaded first frame was used, prepared bridge-frame count, and Virtual Camera output ticks during the seam.
- Fix diagnostic frame-count baselines so a starting counter value of zero is measured correctly.

## QA
- Clip Flow UI/timeline regressions: 38 PASS.
- Seam preflight workflow: 8 PASS.
- Seam preload/capture lifecycle tests: 5 PASS.
- System Update API-403 fallback and fail-closed SHA-512 metadata tests: 2 PASS.
- Make Frames batch-lock stability, Open-derived FPS profile, LIVE Stop wait budget, Python/JavaScript syntax, and diff checks pass.
- A physical long-running Virtual Camera/LIVE run, and the second PC's media/cache/driver combination, still require verification on those actual machines; deterministic tests do not substitute for that test.
