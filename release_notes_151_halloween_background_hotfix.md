# PAOKRA Studio v1.5.1 — Halloween Background Hotfix

## Fixed
- Applied the orange pumpkin and violet animated gradient to the main application background stylesheet, not just the guide overlay.
- Made the main background visible through translucent app surfaces and added a subtle floating pumpkin accent.
- Changed the default appearance to Halloween and added a one-time UI migration for existing installs so the prior saved gray/static background does not override the new theme on first launch.
- Subsequent user theme choices continue to be saved normally; the migration does not force Halloween on every launch.
- Reduced-motion preferences and the app's existing animation-off setting remain respected.

## QA
- Focused background, default-selection/migration, guide, packaged-asset and updater-metadata tests are included.
