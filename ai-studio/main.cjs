const { app, BrowserWindow, dialog, ipcMain, shell } = require("electron");
const path = require("path");
const fs = require("fs");
const { spawn, spawnSync, execFileSync } = require("child_process");

let win = null;
let activeChildren = new Set();
let cancelled = false;

function sendProgress(stage, percent, message, extra = {}) {
  if (win && !win.isDestroyed()) win.webContents.send("progress", {
    stage, percent: Math.max(0, Math.min(100, Math.round(percent))), message, ...extra
  });
}

function findFfmpeg() {
  const candidates = [
    path.join(process.resourcesPath, "runtime", "ffmpeg.exe"),
    path.join(__dirname, "runtime", "ffmpeg.exe"),
    process.env.PAOKRA_FFMPEG ? path.resolve(process.env.PAOKRA_FFMPEG) : ""
  ].filter(Boolean);
  return candidates.find((p) => fs.existsSync(p)) || null;
}

function findRealESRGAN() {
  const candidates = [
    path.join(process.resourcesPath, "runtime", "realesrgan", "realesrgan-ncnn-vulkan.exe"),
    path.join(__dirname, "runtime", "realesrgan", "realesrgan-ncnn-vulkan.exe")
  ];
  return candidates.find((p) => fs.existsSync(p)) || null;
}

function runProcess(exe, args, onLine, options = {}) {
  return new Promise((resolve, reject) => {
    if (cancelled) return reject(new Error("ยกเลิกแล้ว"));
    const child = spawn(exe, args, {
      windowsHide: true,
      cwd: options.cwd || path.dirname(exe),
      stdio: ["ignore", "ignore", "ignore"]
    });
    activeChildren.add(child);

    child.on("error", (err) => {
      activeChildren.delete(child);
      reject(err);
    });
    child.on("close", (code) => {
      activeChildren.delete(child);
      if (cancelled) return reject(new Error("ยกเลิกแล้ว"));
      if (code === 0) return resolve({ code });
      reject(new Error("process failed (exit " + code + ")"));
    });
  });
}

function getVideoInfo(ffmpeg, input) {
  const probe = spawnSync(ffmpeg, ["-hide_banner", "-i", input], {
    windowsHide: true,
    encoding: "utf8",
    stdio: ["ignore", "ignore", "pipe"]
  });
  const stderr = String(probe.stderr || "");
  const durationMatch = stderr.match(/Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)/i);
  const fpsMatch = stderr.match(/(\d+(?:\.\d+)?)\s*(?:fps|tbr)/i);
  const duration = durationMatch
    ? Number(durationMatch[1]) * 3600 + Number(durationMatch[2]) * 60 + Number(durationMatch[3])
    : 0;
  const fps = fpsMatch ? Number(fpsMatch[1]) : 30;
  return { duration, fps: fps > 0 && fps < 240 ? fps : 30 };
}

function countJpg(dir) {
  try { return fs.readdirSync(dir).filter((x) => x.toLowerCase().endsWith(".jpg")).length; }
  catch (_) { return 0; }
}

const os = require("os");

let _vramCache = null;
function detectVramMB() {
  if (_vramCache) return _vramCache;
  let v = 0;
  try {
    const r = spawnSync("nvidia-smi", ["--query-gpu=memory.total", "--format=csv,noheader,nounits"],
      { windowsHide: true, encoding: "utf8", timeout: 4000 });
    if (r.status === 0) {
      const nums = String(r.stdout).split(/\r?\n/).map(Number).filter(Number.isFinite);
      if (nums.length) v = Math.max(...nums);
    }
  } catch (_) {}
  if (!(v > 0)) {
    try {
      const ps = "Get-ItemProperty 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Class\\{4d36e968-e325-11cd-be10-08002be10318}\\0*' -ErrorAction SilentlyContinue | ForEach-Object { $_.'HardwareInformation.qwMemorySize' } | Sort-Object -Descending | Select-Object -First 1";
      const r = spawnSync("powershell", ["-NoProfile", "-Command", ps],
        { windowsHide: true, encoding: "utf8", timeout: 8000 });
      const n = Number(String(r.stdout).trim());
      if (Number.isFinite(n) && n > 0) v = Math.round(n / 1048576);
    } catch (_) {}
  }
  _vramCache = v > 0 ? v : 4096;
  return _vramCache;
}

let _encoderCache = null;
function pickEncoder(ffmpeg) {
  if (_encoderCache) return _encoderCache;
  const cands = [
    { name: "h264_nvenc", args: ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "20", "-b:v", "0"] },
    { name: "h264_amf", args: ["-c:v", "h264_amf", "-quality", "speed", "-rc", "cqp", "-qp_i", "20", "-qp_p", "22"] },
    { name: "h264_qsv", args: ["-c:v", "h264_qsv", "-global_quality", "21"] }
  ];
  for (const c of cands) {
    try {
      const r = spawnSync(ffmpeg, ["-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
        "color=c=black:s=256x256:d=0.4:r=25", "-pix_fmt", "yuv420p", ...c.args, "-f", "null", "NUL"],
        { windowsHide: true, timeout: 12000 });
      if (r.status === 0) { _encoderCache = c; return c; }
    } catch (_) {}
  }
  _encoderCache = { name: "libx264", args: ["-c:v", "libx264", "-preset", "veryfast", "-crf", "19"] };
  return _encoderCache;
}

function fpsArg(fps) {
  const known = [[23.976, "24000/1001"], [29.97, "30000/1001"], [59.94, "60000/1001"]];
  for (const [v, r] of known) if (Math.abs(fps - v) < 0.03) return r;
  return String(Math.round(fps * 1000) / 1000);
}

function tuneFor(options, vramMB, resolvedModel) {
  const fast = resolvedModel === "realesr-animevideov3";
  const mode = ["low", "auto", "max"].includes(options.vram) ? options.vram : "auto";
  let tile = Number(options.tile) || 0;
  if (!tile) {
    const t = fast
      ? (vramMB >= 8000 ? 0 : vramMB >= 5000 ? 512 : vramMB >= 3500 ? 400 : vramMB >= 2000 ? 256 : 128)
      : (vramMB >= 8000 ? 400 : vramMB >= 5000 ? 256 : vramMB >= 3500 ? 200 : vramMB >= 2000 ? 128 : 64);
    tile = t;
  }
  if (mode === "low") tile = Math.max(32, Math.round((tile || 256) / 2));
  if (mode === "max") tile = fast ? 0 : Math.round((tile || 400) * 1.5);
  const cores = Math.max(1, os.cpus().length);
  let threads = (cores >= 6 ? 2 : 1) + ":" + (vramMB >= 4000 ? 2 : 1) + ":2";
  if (mode === "low") threads = "1:1:1";
  if (mode === "max") threads = "2:4:2";
  const chunk = mode === "low" || vramMB < 3000 ? 90 : 180;
  return { tile, threads, chunk };
}

function listFrames(dir) {
  try { return fs.readdirSync(dir).filter((x) => x.toLowerCase().endsWith(".jpg")).sort(); }
  catch (_) { return []; }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function enhanceVideo(options) {
  cancelled = false;
  const ffmpeg = findFfmpeg();
  const realesrgan = findRealESRGAN();
  if (!ffmpeg) throw new Error("ไม่พบ FFmpeg Runtime");
  if (!realesrgan) throw new Error("ไม่พบ Real-ESRGAN Runtime");

  const input = path.resolve(options.input || "");
  if (!fs.existsSync(input)) throw new Error("ไม่พบไฟล์วิดีโอ");

  // "fast" = realesr-animevideov3 (small network, native 2x/3x/4x: ~5-10x faster
  // than x4plus). "quality" = realesrgan-x4plus (heavy, best detail on photos).
  const modelsDir = path.join(path.dirname(realesrgan), "models");
  const hasFast = fs.existsSync(path.join(modelsDir, "realesr-animevideov3-x2.param"));
  const wantQuality = options.model === "quality" || !hasFast;
  const model = wantQuality ? "realesrgan-x4plus" : "realesr-animevideov3";
  let scale = Number(options.scale) || 2;
  if (wantQuality) scale = scale >= 3 ? 4 : 2; else scale = Math.max(2, Math.min(4, Math.round(scale)));

  const vramMB = detectVramMB();
  const tune = tuneFor(options, vramMB, model);
  const encoder = pickEncoder(ffmpeg);

  const workRoot = path.join(app.getPath("temp"), "paokra-ai-studio");
  fs.mkdirSync(workRoot, { recursive: true });
  const job = path.join(workRoot, Date.now() + "-" + Math.random().toString(16).slice(2));
  const frames = path.join(job, "frames");
  const segs = path.join(job, "segs");
  fs.mkdirSync(frames, { recursive: true });
  fs.mkdirSync(segs, { recursive: true });

  try {
    sendProgress("analyze", 3, "กำลังอ่านข้อมูลวิดีโอ…");
    const info = getVideoInfo(ffmpeg, input);
    if (!(info.duration > 0) || !(info.fps > 0)) {
      throw new Error("FFmpeg อ่านไฟล์วิดีโอไม่ได้ — ตรวจสอบว่าไฟล์เป็น MP4/MOV ที่สมบูรณ์");
    }
    sendProgress("analyze", 5, "กำลังตรวจสอบเฟรมแรก…");
    await runProcess(ffmpeg, ["-hide_banner", "-loglevel", "error", "-i", input,
      "-map", "0:v:0", "-frames:v", "1", "-f", "null", "NUL"], null);

    // Stage 1: extraction runs in the background while AI starts on the first chunk.
    let extractDone = false, extractErr = null;
    const extraction = runProcess(ffmpeg, [
      "-y", "-hide_banner", "-loglevel", "error", "-i", input, "-map", "0:v:0",
      "-fps_mode", "passthrough", "-q:v", "2", path.join(frames, "frame_%08d.jpg")
    ], null).then(() => { extractDone = true; }).catch((e) => { extractErr = e; extractDone = true; });

    let totalFrames = Math.max(1, Math.round(info.duration * info.fps));
    let doneFrames = 0, chunkNo = 0;
    const segFiles = [];
    let encodeChain = Promise.resolve();
    const fps = fpsArg(info.fps);

    sendProgress("ai", 8, "Real-ESRGAN (" + model + ") " + scale + "× · VRAM ~" + vramMB + "MB · tile " +
      (tune.tile || "auto") + " · encoder " + encoder.name, { totalFrames });

    for (;;) {
      if (cancelled) throw new Error("ยกเลิกแล้ว");
      let list = listFrames(frames);
      const safe = extractDone ? list.length : list.length - 2;   // last file may be mid-write
      if (safe < tune.chunk && !extractDone) { await sleep(150); continue; }
      if (safe <= 0) break;
      const take = list.slice(0, Math.min(tune.chunk, safe));
      const first = parseInt(take[0].match(/(\d+)/)[1], 10);
      if (extractDone) totalFrames = doneFrames + list.length;

      const cin = path.join(job, "cin_" + chunkNo), cout = path.join(job, "cout_" + chunkNo);
      fs.mkdirSync(cin); fs.mkdirSync(cout);
      for (const f of take) fs.renameSync(path.join(frames, f), path.join(cin, f));

      const poll = setInterval(() => {
        const d = Math.min(take.length, listFrames(cout).length);
        sendProgress("ai", 8 + ((doneFrames + d) / totalFrames) * 82,
          "AI กำลังประมวลผล " + (doneFrames + d) + "/" + totalFrames + " เฟรม…",
          { doneFrames: doneFrames + d, totalFrames });
      }, 500);
      try {
        const args = ["-i", cin, "-o", cout, "-n", model, "-s", String(scale),
          "-t", String(tune.tile), "-f", "jpg", "-j", tune.threads];
        if (options.gpu !== undefined && options.gpu !== "" && Number.isFinite(Number(options.gpu))) args.push("-g", String(Number(options.gpu)));
        await runProcess(realesrgan, args, null, { cwd: path.dirname(realesrgan) });
      } finally {
        clearInterval(poll);
      }
      if (listFrames(cout).length < take.length) {
        throw new Error("AI ประมวลผลไม่ครบเฟรม (" + listFrames(cout).length + "/" + take.length + ")");
      }
      fs.rmSync(cin, { recursive: true, force: true });
      doneFrames += take.length;

      // Stage 3: encode this chunk while the next one is being upscaled.
      const segPath = path.join(segs, "seg_" + String(chunkNo).padStart(5, "0") + ".mp4");
      segFiles.push(segPath);
      const prev = encodeChain;
      encodeChain = prev.then(async () => {
        await runProcess(ffmpeg, ["-y", "-hide_banner", "-loglevel", "error",
          "-framerate", fps, "-start_number", String(first),
          "-i", path.join(cout, "frame_%08d.jpg"),
          ...encoder.args, "-pix_fmt", "yuv420p", segPath], null);
        fs.rmSync(cout, { recursive: true, force: true });
      });
      encodeChain.catch(() => {});
      chunkNo++;
    }
    await extraction;
    if (extractErr && !doneFrames) throw extractErr;
    await encodeChain;
    if (!segFiles.length) throw new Error("แยกเฟรมไม่สำเร็จ");

    const defaultOut = path.join(app.getPath("videos"),
      path.basename(input, path.extname(input)) + "_AI.mp4");
    const output = path.resolve(options.output || defaultOut);
    fs.mkdirSync(path.dirname(output), { recursive: true });

    sendProgress("encode", 92, "กำลังรวมวิดีโอและใส่เสียงกลับ…");
    const listFile = path.join(job, "list.txt");
    fs.writeFileSync(listFile, segFiles.map((f) => "file '" + f.replace(/\\/g, "/").replace(/'/g, "'\\''") + "'").join("\n"));
    await runProcess(ffmpeg, ["-y", "-hide_banner", "-loglevel", "error",
      "-f", "concat", "-safe", "0", "-i", listFile, "-i", input,
      "-map", "0:v:0", "-map", "1:a?", "-c:v", "copy",
      "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", output], null);

    sendProgress("done", 100, "เสร็จแล้ว", { output });
    return { output, totalFrames: doneFrames, fps: info.fps, scale, model, encoder: encoder.name, tile: tune.tile };
  } finally {
    fs.rmSync(job, { recursive: true, force: true });
  }
}

function cancelAll() {
  cancelled = true;
  const children = [...activeChildren];
  for (const child of children) {
    try { child.kill(); } catch (_) {}
    try {
      execFileSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], {
        windowsHide: true,
        stdio: "ignore",
        timeout: 3000
      });
    } catch (_) {}
  }
  sendProgress("cancelled", 0, "หยุดการประมวลผลแล้ว");
  return children.length;
}

function createWindow() {
  win = new BrowserWindow({
    width: 1180,
    height: 780,
    minWidth: 900,
    minHeight: 650,
    backgroundColor: "#090e1a",
    title: "PAOKRA AI Studio",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false
    }
  });
  win.loadFile(path.join(__dirname, "index.html"));
}

ipcMain.handle("choose-video", async () => {
  const r = await dialog.showOpenDialog(win, {
    title: "เลือกวิดีโอ",
    properties: ["openFile"],
    filters: [{ name: "Video", extensions: ["mp4", "mov", "mkv", "avi", "webm", "m4v"] }]
  });
  return r.canceled ? null : r.filePaths[0];
});

ipcMain.handle("choose-output", async (_e, suggested) => {
  const r = await dialog.showSaveDialog(win, {
    title: "บันทึกวิดีโอที่ปรับแล้ว",
    defaultPath: suggested || "video_AI.mp4",
    filters: [{ name: "MP4", extensions: ["mp4"] }]
  });
  return r.canceled ? null : r.filePath;
});

ipcMain.handle("enhance-video", async (_e, options) => {
  try { return { ok: true, result: await enhanceVideo(options || {}) }; }
  catch (err) { return { ok: false, error: err.message || String(err) }; }
});

ipcMain.handle("cancel-job", async () => { cancelAll(); return { ok: true }; });

ipcMain.handle("open-output", async (_e, filePath) => {
  if (filePath && fs.existsSync(filePath)) {
    await shell.showItemInFolder(filePath);
    return true;
  }
  return false;
});

app.whenReady().then(createWindow);
app.on("window-all-closed", () => {
  cancelAll();
  if (process.platform !== "darwin") app.quit();
});