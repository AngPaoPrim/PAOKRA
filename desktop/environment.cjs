const fs = require("fs");
const path = require("path");
const os = require("os");
const https = require("https");
const { spawnSync } = require("child_process");

const OBS_VERSION = "32.2.2";
const OBS_URL = `https://github.com/obsproject/obs-studio/releases/download/${OBS_VERSION}/OBS-Studio-${OBS_VERSION}-Windows-x64-Installer.exe`;
const VBCABLE_URL = "https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack45.zip";
// Fallback only when the packaged FFmpeg runtime is missing/corrupt.
// This is an app-local runtime; it does not require Node.js, Python, winget, or a system PATH change.
const FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip";

function isWindows() {
  return process.platform === "win32";
}

function runPowerShell(script, timeoutMs = 20000) {
  const r = spawnSync("powershell.exe", [
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-Command", script
  ], {
    windowsHide: true,
    encoding: "utf8",
    timeout: timeoutMs,
    maxBuffer: 1024 * 1024
  });
  return {
    ok: r.status === 0,
    code: r.status,
    stdout: String(r.stdout || ""),
    stderr: String(r.stderr || ""),
    error: r.error ? String(r.error.message || r.error) : ""
  };
}

function hasObsVirtualCamera() {
  if (!isWindows()) return false;
  const r = spawnSync("reg.exe", [
    "query",
    "HKLM\\SOFTWARE\\Classes\\CLSID\\{A3FCE0F5-3493-419F-958A-ABA1250EC20B}",
    "/reg:64"
  ], {
    windowsHide: true,
    encoding: "utf8",
    timeout: 5000,
    maxBuffer: 128 * 1024
  });
  return r.status === 0 && /OBS Virtual Camera/i.test(String(r.stdout || ""));
}

function hasVirtualAudio() {
  if (!isWindows()) return false;
  const r = runPowerShell(
    "$d = Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue; " +
    "$in = $d | Where-Object { $_.FriendlyName -match '^CABLE (Input|In 16 Ch).*VB-Audio Virtual Cable' }; " +
    "$out = $d | Where-Object { $_.FriendlyName -match '^CABLE Output.*VB-Audio Virtual Cable' }; " +
    "if($in -and $out){'READY'}",
    8000
  );
  return r.ok && /READY/i.test(r.stdout);
}

function findBundledFfmpeg(runtimeRoot, userDataRoot = "") {
  const roots = [
    path.join(runtimeRoot, "ffmpeg"),
    // Development tree stores the shared FFmpeg binary under ai-studio/runtime.
    // Packaged builds place it under runtimeRoot/ffmpeg, handled by the first entry.
    path.join(runtimeRoot, "ai-studio", "runtime"),
    userDataRoot ? path.join(userDataRoot, "environment-runtime", "ffmpeg") : "",
    path.join(runtimeRoot, "ms-playwright")
  ].filter(Boolean);
  for (const root of roots) {
    try {
      if (!fs.existsSync(root)) continue;
      const stack = [root];
      while (stack.length) {
        const dir = stack.pop();
        for (const name of fs.readdirSync(dir)) {
          const full = path.join(dir, name);
          let st;
          try { st = fs.statSync(full); } catch (_) { continue; }
          if (st.isDirectory()) stack.push(full);
          else if (/^ffmpeg(?:-win64)?\.exe$/i.test(name)) return full;
        }
      }
    } catch (_) {}
  }
  return null;
}

function ensureDir(p) {
  fs.mkdirSync(p, { recursive: true });
  return p;
}

async function installFfmpeg(userDataRoot, report) {
  const cacheDir = ensureDir(path.join(userDataRoot, "environment-cache"));
  const zip = path.join(cacheDir, "ffmpeg-release-essentials.zip");
  const extractDir = path.join(cacheDir, "ffmpeg-release-essentials");
  const installDir = ensureDir(path.join(userDataRoot, "environment-runtime", "ffmpeg"));
  const installed = path.join(installDir, "ffmpeg.exe");

  report("download", "กำลังดาวน์โหลด FFmpeg Runtime", 8);
  if (!fs.existsSync(zip) || fs.statSync(zip).size < 5000000) {
    await download(FFMPEG_URL, zip, (loaded, total) => {
      const pct = total ? 8 + (loaded / total) * 32 : 8;
      report("download", "กำลังดาวน์โหลด FFmpeg Runtime", pct);
    });
  }
  if (!fs.existsSync(extractDir)) {
    ensureDir(extractDir);
    report("install", "กำลังแตกไฟล์ FFmpeg Runtime", 43);
    const er = runPowerShell(
      "Expand-Archive -LiteralPath '" + zip.replace(/'/g, "''") + "' -DestinationPath '" + extractDir.replace(/'/g, "''") + "' -Force",
      60000
    );
    if (!er.ok) throw new Error("แตกไฟล์ FFmpeg ไม่สำเร็จ: " + (er.stderr || er.error));
  }
  const sr = runPowerShell(
    "$p = Get-ChildItem '" + extractDir.replace(/'/g, "''") + "' -Filter 'ffmpeg.exe' -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName; if($p){$p}",
    15000
  );
  const source = String(sr.stdout || "").trim().split(/\r?\n/)[0];
  if (!source || !fs.existsSync(source)) throw new Error("ไม่พบ ffmpeg.exe ในแพ็กเกจที่ดาวน์โหลด");
  report("install", "กำลังติดตั้ง FFmpeg Runtime ลงใน PAOKRA", 58);
  fs.copyFileSync(source, installed);
  report("verify", "กำลังตรวจสอบ FFmpeg Runtime", 65);
  if (!fs.existsSync(installed) || fs.statSync(installed).size < 1000000) {
    throw new Error("ติดตั้ง FFmpeg Runtime ไม่สมบูรณ์");
  }
  return installed;
}

function download(url, dest, onProgress) {
  return new Promise((resolve, reject) => {
    ensureDir(path.dirname(dest));
    const request = (target) => {
      const req = https.get(target, {
        headers: {
          "User-Agent": "PAOKRA-Studio/1.3.4",
          "Accept": "*/*"
        }
      }, res => {
        if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
          res.resume();
          return request(new URL(res.headers.location, target).toString());
        }
        if (res.statusCode !== 200) {
          res.resume();
          return reject(new Error("ดาวน์โหลดไม่สำเร็จ HTTP " + res.statusCode));
        }
        const total = Number(res.headers["content-length"] || 0);
        let loaded = 0;
        const out = fs.createWriteStream(dest);
        res.on("data", chunk => {
          loaded += chunk.length;
          out.write(chunk);
          if (onProgress) onProgress(loaded, total);
        });
        res.on("end", () => {
          out.end(() => resolve(dest));
        });
        res.on("error", err => {
          try { out.destroy(); } catch (_) {}
          reject(err);
        });
        out.on("error", reject);
      });
      req.setTimeout(30000, () => req.destroy(new Error("การดาวน์โหลดหมดเวลา")));
      req.on("error", reject);
    };
    request(url);
  });
}

function shellRunElevated(filePath, args, cwd = undefined) {
  const argArray = Array.isArray(args) ? args : [];
  const argText = argArray.map(a => "'" + String(a).replace(/'/g, "''") + "'").join(", ");
  const script =
    "$p = Start-Process -FilePath '" + String(filePath).replace(/'/g, "''") +
    "' -ArgumentList @(" + argText + ") -Verb RunAs -Wait -PassThru" +
    (cwd ? " -WorkingDirectory '" + String(cwd).replace(/'/g, "''") + "'" : "") +
    "; exit $p.ExitCode";
  return runPowerShell(script, 180000);
}

async function installObs(userDataRoot, report) {
  const cacheDir = ensureDir(path.join(userDataRoot, "environment-cache"));
  const installer = path.join(cacheDir, `OBS-Studio-${OBS_VERSION}-Windows-x64-Installer.exe`);
  report("download", "กำลังดาวน์โหลด OBS Studio Virtual Camera", 0);
  if (!fs.existsSync(installer) || fs.statSync(installer).size < 1000000) {
    await download(OBS_URL, installer, (loaded, total) => {
      const pct = total ? loaded / total * 70 : 0;
      report("download", "กำลังดาวน์โหลด OBS Studio Virtual Camera", pct);
    });
  }
  report("install", "กำลังติดตั้ง OBS Studio เพื่อเพิ่ม Virtual Camera", 72);
  const r = shellRunElevated(installer, ["/S"]);
  if (!r.ok && r.code !== 0) throw new Error("ติดตั้ง OBS ไม่สำเร็จ: " + (r.stderr || r.error || r.code));
  await new Promise(resolve => setTimeout(resolve, 1600));

  const candidates = [
    process.env.ProgramFiles ? path.join(process.env.ProgramFiles, "obs-studio", "data", "obs-plugins", "win-dshow", "virtualcam-install.bat") : "",
    "C:\\Program Files\\obs-studio\\data\\obs-plugins\\win-dshow\\virtualcam-install.bat"
  ].filter(Boolean);
  let bat = candidates.find(p => fs.existsSync(p));
  if (!bat) {
    const scan = runPowerShell(
      "$p = Get-ChildItem 'C:\\Program Files','C:\\Program Files (x86)' -Filter 'virtualcam-install.bat' -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName; if($p){$p}",
      30000
    );
    bat = String(scan.stdout || "").trim().split(/\r?\n/)[0];
  }
  if (!bat || !fs.existsSync(bat)) throw new Error("ติดตั้ง OBS แล้วแต่ไม่พบตัวติดตั้ง Virtual Camera");
  report("install", "กำลังลงทะเบียน OBS Virtual Camera กับ Windows", 86);
  const br = shellRunElevated("cmd.exe", ["/c", bat], path.dirname(bat));
  if (!br.ok && br.code !== 0) throw new Error("ลงทะเบียน Virtual Camera ไม่สำเร็จ: " + (br.stderr || br.error || br.code));
  await new Promise(resolve => setTimeout(resolve, 1400));
  return hasObsVirtualCamera();
}

async function installVbCable(userDataRoot, report) {
  const cacheDir = ensureDir(path.join(userDataRoot, "environment-cache"));
  const zip = path.join(cacheDir, "VBCABLE_Driver_Pack45.zip");
  const extractDir = path.join(cacheDir, "VBCABLE_Driver_Pack45");
  report("download", "กำลังดาวน์โหลด VB-CABLE Virtual Audio", 0);
  if (!fs.existsSync(zip) || fs.statSync(zip).size < 300000) {
    await download(VBCABLE_URL, zip, (loaded, total) => {
      const pct = total ? loaded / total * 42 : 0;
      report("download", "กำลังดาวน์โหลด VB-CABLE Virtual Audio", pct);
    });
  }
  if (!fs.existsSync(extractDir)) {
    ensureDir(extractDir);
    const er = runPowerShell(
      "Expand-Archive -LiteralPath '" + zip.replace(/'/g, "''") + "' -DestinationPath '" + extractDir.replace(/'/g, "''") + "' -Force",
      30000
    );
    if (!er.ok) throw new Error("แตกไฟล์ VB-CABLE ไม่สำเร็จ: " + (er.stderr || er.error));
  }
  const sr = runPowerShell(
    "$p = Get-ChildItem '" + extractDir.replace(/'/g, "''") + "' -Filter 'VBCABLE_Setup_x64.exe' -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName; if($p){$p}",
    10000
  );
  const setup = String(sr.stdout || "").trim().split(/\r?\n/)[0];
  if (!setup || !fs.existsSync(setup)) throw new Error("ไม่พบ VBCABLE_Setup_x64.exe");
  report("install", "กำลังติดตั้ง VB-CABLE Virtual Audio Driver", 55);
  const ir = shellRunElevated(setup, ["-i", "-h"], path.dirname(setup));
  if (!ir.ok && ir.code !== 0) {
    throw new Error("ติดตั้ง VB-CABLE ไม่สำเร็จ: " + (ir.stderr || ir.error || ir.code));
  }
  report("install", "กำลังรอ Windows ลงทะเบียน Virtual Audio", 78);
  for (let i = 0; i < 12; i++) {
    if (hasVirtualAudio()) return true;
    await new Promise(resolve => setTimeout(resolve, 900));
  }
  return hasVirtualAudio();
}

function inspectEnvironment(runtimeRoot, userDataRoot = "") {
  const ffmpeg = findBundledFfmpeg(runtimeRoot, userDataRoot);
  const camera = hasObsVirtualCamera();
  const audio = hasVirtualAudio();
  return {
    ok: !!ffmpeg && camera && audio,
    ffmpeg: { ready: !!ffmpeg, path: ffmpeg || "" },
    virtualCamera: { ready: camera, provider: camera ? "OBS Virtual Camera" : "" },
    virtualAudio: { ready: audio, provider: audio ? "VB-CABLE / AudioRelay" : "" },
    platform: process.platform,
    arch: process.arch,
    windows: isWindows()
  };
}

async function ensureEnvironment({ runtimeRoot, userDataRoot, onProgress }) {
  const report = (phase, message, percent) => {
    if (onProgress) onProgress({ phase, message, percent: Math.max(0, Math.min(100, Number(percent) || 0)) });
  };
  if (!isWindows()) {
    const s = inspectEnvironment(runtimeRoot, userDataRoot);
    return { ready: false, state: s, error: "PAOKRA Environment Setup รองรับ Windows ในแพ็กเกจนี้" };
  }

  report("scan", "กำลังตรวจสอบสภาพแวดล้อมของเครื่อง", 2);
  let state = inspectEnvironment(runtimeRoot, userDataRoot);
  if (state.ok) {
    report("ready", "สภาพแวดล้อมพร้อมใช้งาน", 100);
    return { ready: true, state };
  }

  report("scan", "พบส่วนประกอบที่ยังไม่พร้อม กำลังเตรียมการติดตั้ง", 8);

  try {
    if (!state.ffmpeg.ready) {
      await installFfmpeg(userDataRoot, report);
      state = inspectEnvironment(runtimeRoot, userDataRoot);
      if (!state.ffmpeg.ready) throw new Error("FFmpeg Runtime ยังไม่พร้อมหลังติดตั้ง");
    }

    if (!state.virtualCamera.ready) {
      const ok = await installObs(userDataRoot, report);
      if (!ok) throw new Error("Virtual Camera ยังไม่พร้อมหลังติดตั้ง OBS");
      state = inspectEnvironment(runtimeRoot, userDataRoot);
    }

    if (!state.virtualAudio.ready) {
      const ok = await installVbCable(userDataRoot, report);
      if (!ok) throw new Error("Virtual Audio ยังไม่พร้อมหลังติดตั้ง VB-CABLE — อาจต้องรีสตาร์ท Windows");
      state = inspectEnvironment(runtimeRoot, userDataRoot);
    }

    if (!state.ok) throw new Error("ยังมีส่วนประกอบของระบบที่ไม่พร้อม");
    report("ready", "ติดตั้งและตรวจสอบสภาพแวดล้อมเสร็จแล้ว", 100);
    const marker = path.join(userDataRoot, "environment-ready-v2.json");
    try {
      fs.writeFileSync(marker, JSON.stringify({
        version: 2,
        checkedAt: new Date().toISOString(),
        machine: os.hostname(),
        state
      }, null, 2), "utf8");
    } catch (_) {}
    return { ready: true, state };
  } catch (error) {
    state = inspectEnvironment(runtimeRoot, userDataRoot);
    report("error", error.message || String(error), 100);
    return { ready: false, state, error: error.message || String(error) };
  }
}

module.exports = {
  OBS_VERSION,
  OBS_URL,
  VBCABLE_URL,
  findBundledFfmpeg,
  inspectEnvironment,
  ensureEnvironment
};
