# PAOKRA Studio v1.4.6 Fix Bug Beta

## Fixes
- Refresh AI Seam compatibility as soon as Scene Builder changes a setting that affects generated bridge output (Output width/height, FPS mode/value, source transform size, or Fit mode).
- Do not display cached transitions as พร้อมใช้ while a fresh server-side compatibility check is still pending.
- Show the reason a directed seam needs regeneration (Output/FPS/aspect/Fit, manual AI resolution, transition frame count, older algorithm, missing cache/source, or unavailable output profile).
- If the compatibility request fails, show that the old status is unconfirmed instead of presenting stale readiness.

## QA
- Frontend syntax and Clip Flow UI regression QA.
- Make Frames batch-lock / mutation exclusion QA.
- OPEN-derived source-FPS profile QA.
- LIVE Stop wait-budget QA.
- AI Seam Studio/RIFE QA and packaged-app QA are required before publishing the installer.

## Workflow note
Set the final Scene Builder transform and Output/FPS settings before running Auto Make Frames. If those settings change later, regenerate only the directed pairs reported incompatible; stopping Engine or Emergency Stop by itself must not change persisted Output/AI Settings.
