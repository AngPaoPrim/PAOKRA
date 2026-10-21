# PAOKRA
Local real-time media studio prototype.

## Run
สำหรับ Desktop ให้รันไฟล์เดียว:

PAOKRA.ps1

มันจะเปิด Electron Desktop + Flask backend และทำความสะอาดเฉพาะ process ของ PAOKRA ที่ค้างจากรอบก่อน

สำหรับ browser-only:
py -3 app\server.py
Open http://127.0.0.1:8797

## Current pipeline
Local file path / Upload / URL -> media timing probe -> OpenCV decode -> compositor -> MJPEG realtime preview.

ไฟล์วิดีโอขนาดใหญ่สามารถเลือกเป็น Local Source ได้โดยตรงจากดิสก์ ไม่ต้องคัดลอกเข้า data ก่อนใช้งาน


## Planned adapters
Virtual Camera -> Windows camera device.
Control -> local supported desktop-app integration.
Scheduler -> local start/stop timers.
Accounts -> profile names only; credentials are not stored.

## Updates (Windows desktop)
Starting with desktop version 1.3.8, the packaged Electron app checks GitHub Releases for stable updates. When a newer release is published it downloads in the background, prompts the user when ready, and restarts only after the user chooses “Restart and install”. Use the app menu **Help → Check for updates** to check manually.

The release feed is configured for https://github.com/AngPaoPrim/PAOKRA. To publish a new version:
1. Bump both `desktop/package.json` and `desktop/package-lock.json` together.
2. Build and QA a Windows x64 NSIS installer; never publish from the QA build command.
3. Create a GitHub Release tag matching the app version (for example `v1.3.8`) and upload **all three** update assets using the exact names referenced by `latest.yml`: `PAOKRA-Studio-Setup.exe`, `PAOKRA-Studio-Setup.exe.blockmap`, and `latest.yml`. The NSIS build output is locally named `PAOKRA Studio Setup.exe`; make a renamed copy for the Release assets so the URL in `latest.yml` matches exactly.
4. The first release containing this updater must be installed manually by existing 1.3.7 users; older builds do not have update-check code.

## Build notes
- Windows x64, Node.js/npm, and Python with dependencies from `requirements.txt` are required for a local development build.
- The installer build expects locally provisioned runtime assets (the Python backend build, FFmpeg, the RIFE model/runtime, and the TikTok Live Studio helper). These binaries/models and video fixtures are intentionally excluded from source control; provision them before running the packaging script.
- Local generated packages, data, logs, QA reports, cached media, secrets/cookies, and backups must not be committed.

## Safety
The prototype does not automate TikTok password login or extract tokens.