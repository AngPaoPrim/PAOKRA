const { app, BrowserWindow, ipcMain, dialog, session, shell, protocol, net, Menu, Notification } = require("electron");
const { spawn } = require("child_process");
const { ensureEnvironment, inspectEnvironment, findBundledFfmpeg } = require("./environment.cjs");

// Native Electron app scheme: ship and load the UI from disk, not from Flask static routes.
// Only Python engine/API/media requests are proxied to the local worker.
protocol.registerSchemesAsPrivileged([{
  scheme: "paokra",
  privileges: { standard: true, secure: true, supportFetchAPI: true, corsEnabled: true, stream: true, codeCache: true }
}]);

// Allow the Scene Builder preview video to autoplay with its real audio track.
app.commandLine.appendSwitch("autoplay-policy", "no-user-gesture-required");

app.setAppUserModelId("com.paokra.studio");

const gotSingleInstanceLock = app.requestSingleInstanceLock();
if (!gotSingleInstanceLock) {
  app.quit();
  return;
}
app.on("second-instance", () => {
  if (win && !win.isDestroyed()) {
    if (win.isMinimized()) win.restore();
    if (process.env.PAOKRA_QA_HEADLESS !== "1") win.show();
    win.focus();
  }
});

const path = require("path");
const http = require("http");
const { Readable } = require("stream");
const fs = require("fs");
const { execFileSync } = require("child_process");

const DEV_ROOT = path.resolve(__dirname, "..");
const LOCAL_APP_DATA = process.env.LOCALAPPDATA || app.getPath("appData");
const USER_DATA_ROOT = path.join(LOCAL_APP_DATA, "PAOKRA Studio");
const DEV_SESSION_ROOT = path.join(DEV_ROOT, ".paokra-runtime");
const SESSION_ROOT = app.isPackaged
  ? path.join(USER_DATA_ROOT, "session")
  : DEV_SESSION_ROOT;
fs.mkdirSync(USER_DATA_ROOT, { recursive: true });
fs.mkdirSync(SESSION_ROOT, { recursive: true });
app.setPath("userData", USER_DATA_ROOT);
app.setPath("sessionData", SESSION_ROOT);
app.setPath("cache", path.join(SESSION_ROOT, "Cache"));

const RUNTIME_ROOT = app.isPackaged
  ? path.join(process.resourcesPath, "paokra")
  : DEV_ROOT;
const DATA_ROOT = process.env.PAOKRA_DATA
  ? path.resolve(process.env.PAOKRA_DATA)
  : (app.isPackaged ? path.join(USER_DATA_ROOT, "data") : path.join(DEV_ROOT, "data"));
fs.mkdirSync(DATA_ROOT, { recursive: true });
const WEB_ROOT = app.isPackaged
  ? path.join(RUNTIME_ROOT, "app", "web")
  : path.join(DEV_ROOT, "app", "web");
const SERVER_EXE = path.join(RUNTIME_ROOT, "paokra-server.exe");
const SERVER_SCRIPT = path.join(DEV_ROOT, "app", "server.py");
let SERVER_TEMP_ROOT = process.env.TEMP || process.env.TMP || app.getPath("temp");
if (process.platform === "win32" && app.isPackaged) {
  const installTemp = process.env.PAOKRA_TEMP
    ? path.resolve(process.env.PAOKRA_TEMP)
    : path.join(path.dirname(process.resourcesPath), ".paokra-runtime-temp");
  try {
    fs.mkdirSync(installTemp, { recursive: true });
    SERVER_TEMP_ROOT = installTemp;
  } catch (_) {}
}
const DEV_PYTHON = path.join(DEV_ROOT, ".venv", "Scripts", "python.exe");
const PAOKRA_PORT = (() => {
  const n = Number.parseInt(process.env.PAOKRA_PORT || "8797", 10);
  return Number.isInteger(n) && n >= 1024 && n <= 65535 ? n : 8797;
})();
const PAOKRA_URL = `http://127.0.0.1:${PAOKRA_PORT}`;
const PAOKRA_APP_URL = "paokra://app";
function traceTailwindWarnings(contents, surface) {
  if (!contents || typeof contents.on !== "function") return;
  contents.on("console-message", (...args) => {
    try {
      // Electron 22+ sends (event, details); older versions sent the fields
      // positionally. Support both so the diagnostic survives Electron upgrades.
      const raw = args[1];
      const d = raw && typeof raw === "object" ? raw : {
        level: args[1], message: args[2], lineNumber: args[3], sourceId: args[4]
      };
      const message = String(d.message || "");
      if (!/tailwindcss(?:\.com)?|cdn\.tailwindcss/i.test(message)) return;
      let url = "";
      try {
        const parsed = new URL(contents.getURL());
        url = parsed.protocol === "paokra:" ? "paokra://" + parsed.hostname + parsed.pathname : parsed.origin + parsed.pathname;
      } catch (_) {}
      const dir = path.join(USER_DATA_ROOT, "logs");
      fs.mkdirSync(dir, { recursive: true });
      const record = {
        time: new Date().toISOString(), surface: String(surface || "unknown"), url,
        sourceId: String(d.sourceId || ""), lineNumber: Number(d.lineNumber || 0),
        level: d.level == null ? null : d.level, message: message.slice(0, 1000)
      };
      fs.appendFileSync(path.join(dir, "tailwind-warning.log"), JSON.stringify(record) + "\n", "utf8");
    } catch (_) {}
  });
}
function protocolMime(file) {
  const ext = path.extname(file).toLowerCase();
  return ({
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".ico": "image/x-icon",
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf"
  })[ext] || "application/octet-stream";
}
// MJPEG/video responses are long-lived and can occupy Electron's shared
// net.fetch connection pool. If that pool fills, ordinary /api/state and control
// POSTs queue behind previews, leaving Start/Stop buttons permanently stale.
// Stream these GET/HEAD routes through Node's own unpooled HTTP sockets instead.
function proxyLongLivedLocalResponse(request, targetPath) {
  return new Promise((resolve, reject) => {
    const headers = {};
    request.headers.forEach((value, key) => {
      const name = String(key).toLowerCase();
      if (!["host", "connection", "content-length", "transfer-encoding",
            "keep-alive", "proxy-authorization", "proxy-authenticate",
            "te", "trailer", "upgrade"].includes(name)) {
        headers[name] = value;
      }
    });
    const upstream = http.request({
      hostname: "127.0.0.1", port: PAOKRA_PORT, path: targetPath,
      method: request.method, headers, agent: false,
    }, incoming => {
      const responseHeaders = new Headers();
      for (const [key, raw] of Object.entries(incoming.headers || {})) {
        const name = String(key).toLowerCase();
        if (raw == null || ["connection", "transfer-encoding", "keep-alive",
            "proxy-authorization", "proxy-authenticate", "te", "trailer",
            "upgrade"].includes(name)) continue;
        responseHeaders.set(name, Array.isArray(raw) ? raw.join(", ") : String(raw));
      }
      if (!responseHeaders.has("cache-control")) responseHeaders.set("Cache-Control", "no-store");
      // Backpressure/cancellation is preserved by the Web<->Node stream adapter.
      resolve(new Response(Readable.toWeb(incoming), {
        status: incoming.statusCode || 200,
        headers: responseHeaders,
      }));
    });
    upstream.once("error", reject);
    if (request.signal) {
      request.signal.addEventListener("abort", () => upstream.destroy(), { once: true });
    }
    upstream.end();
  });
}

function registerPaokraProtocol() {
  protocol.handle("paokra", async request => {
    try {
      const u = new URL(request.url);
      const pathname = decodeURIComponent(u.pathname || "/");
      if (u.hostname !== "app") return new Response("Not found", { status: 404 });

      // Serve packaged HTML/CSS/JS/media assets directly from the application directory.
      // /static/* is a legacy route alias; its files live in WEB_ROOT.
      let candidate = null;
      if (pathname === "/" || pathname === "/index.html") {
        candidate = path.join(WEB_ROOT, "index.html");
      } else if (pathname.startsWith("/static/")) {
        candidate = path.join(WEB_ROOT, path.basename(pathname));
      } else {
        candidate = path.resolve(WEB_ROOT, "." + pathname);
      }
      const rootPrefix = path.resolve(WEB_ROOT) + path.sep;
      const resolved = path.resolve(candidate);
      if ((resolved === path.resolve(WEB_ROOT, "index.html") ||
           resolved.startsWith(rootPrefix)) && fs.existsSync(resolved) &&
          fs.statSync(resolved).isFile()) {
        const content = fs.readFileSync(resolved);
        const headers = new Headers({ "Content-Type": protocolMime(resolved) });
        if (/\.(html|js|mjs|css)$/i.test(resolved)) headers.set("Cache-Control", "no-cache");
        return new Response(content, { status: 200, headers });
      }
      if (pathname === "/favicon.ico") return new Response(null, { status: 204 });

      // Long-running preview/media responses must not share Electron net.fetch's
      // connection pool with the JSON/control API. Otherwise MJPEG streams can
      // exhaust pooled sockets and make Engine Start/Stop look unclickable.
      if ((request.method === "GET" || request.method === "HEAD") &&
          (/^\/(?:preview|source|web)\.mjpg$/i.test(pathname) ||
           /^\/source\.mp4$/i.test(pathname))) {
        return await proxyLongLivedLocalResponse(request, pathname + u.search);
      }

      // The Python process remains a local engine/API worker; the desktop UI itself is not HTTP-served.
      const target = PAOKRA_URL + pathname + u.search;
      const init = { method: request.method, headers: new Headers(request.headers) };
      init.headers.delete("host");
      init.headers.delete("connection");
      if (request.method !== "GET" && request.method !== "HEAD") {
        init.body = request.body;
        init.duplex = "half";
      }
      return await net.fetch(target, init);
    } catch (err) {
      return new Response("PAOKRA local bridge error: " + String(err?.message || err), {
        status: 502, headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" }
      });
    }
  });
}
let win = null, serverProc = null;

// A packaged PyInstaller one-file backend creates a worker child process. On
// Windows, ChildProcess.kill() alone can leave that child running after Electron
// closes, which leaves OBS Virtual Camera/FFmpeg handles open. Stop only the
// exact backend process tree that THIS Electron instance spawned.
function stopBackendProcessTree() {
  const child = serverProc;
  serverProc = null;
  if (!child || !Number.isInteger(Number(child.pid)) || Number(child.pid) <= 0) return;
  if (child.exitCode !== null || child.signalCode !== null) return;

  if (process.platform === "win32") {
    const targetBackend = app.isPackaged ? SERVER_EXE : path.resolve(SERVER_SCRIPT);
    const targetLiteral = "'" + String(targetBackend).replace(/'/g, "''") + "'";
    const targetMatch = app.isPackaged
      ? "[string]::Equals($p.ExecutablePath, $target, [StringComparison]::OrdinalIgnoreCase)"
      : "($p.CommandLine -and $p.CommandLine.ToLowerInvariant().Contains($target.ToLowerInvariant()))";
    const cleanup = "$target = " + targetLiteral + "; " +
      "$p = Get-CimInstance Win32_Process -Filter 'ProcessId=" + Number(child.pid) + "' -ErrorAction SilentlyContinue; " +
      "if ($p -and " + targetMatch + ") { & taskkill.exe /PID " + Number(child.pid) + " /T /F | Out-Null }";
    try {
      execFileSync("powershell.exe", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cleanup], {
        windowsHide: true, stdio: "ignore", timeout: 10000
      });
      return;
    } catch (_) {
      // Fall through to killing the exact ChildProcess handle, never by image name.
    }
  }
  try { if (child.exitCode === null && child.signalCode === null) child.kill(); } catch (_) {}
}

let dashboardRetryTimer = null;
let dashboardLoadFn = null;
let tiktokDashboardWin = null;
const TIKTOK_DASHBOARD_URL = "https://shop.tiktok.com/streamer/live/product/dashboard";
const TIKTOK_SESSION_PARTITION = "persist:paokra-tiktok-dashboard";
let webSourceWin = null;
let webSourceUrl = "";
let webSourceSize = "";
let webPosting = false;
let webCapturing = false;
let webPollTimer = null;
let webPaintTimer = null;

async function startFlask() {
  if (process.env.PAOKRA_EXTERNAL_SERVER === "1") return;
  // Stop only a stale backend owned by this exact development tree or packaged install.
  // Never taskkill by image name alone: another PAOKRA copy may be using that executable.
  // An alternate port means this is an isolated instance (e.g. smoke-test).
  // Never terminate another instance's backend when running side-by-side.
  if (process.platform === "win32" && PAOKRA_PORT === 8797) {
    try {
      const targetBackend = app.isPackaged ? SERVER_EXE : path.resolve(SERVER_SCRIPT);
      const targetLiteral = "'" + String(targetBackend).replace(/'/g, "''") + "'";
      const match = app.isPackaged
        ? "($_.Name -eq 'paokra-server.exe' -and [string]::Equals($_.ExecutablePath, $target, [StringComparison]::OrdinalIgnoreCase))"
        : "($_.Name -match '^(python|pythonw|py)(\\.exe)?$' -and $_.CommandLine -and $_.CommandLine.ToLowerInvariant().Contains($target.ToLowerInvariant()))";
      const cleanup = "$target = " + targetLiteral + "; Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { " + match + " } | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {} }";
      execFileSync("powershell", ["-NoProfile", "-Command", cleanup], {
        windowsHide: true, stdio: "ignore", timeout: 10000
      });
    } catch (_) {}
    // Remove only ORPHANED PAOKRA server trees left by a crash/relaunch. Do not
    // kill a server whose process ancestry still contains a live Studio window;
    // this avoids closing another active PAOKRA session while recovering stale
    // OBS camera handles from an abandoned build.
    try {
      const orphanCleanup = `
$processes = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
$byId = @{}
foreach ($item in $processes) { $byId[[int]$item.ProcessId] = $item }
$targets = @($processes | Where-Object { $_.Name -ieq 'paokra-server.exe' })
foreach ($item in $targets) {
  $parent = $item
  $ownedByStudio = $false
  for ($depth = 0; $depth -lt 12; $depth++) {
    $parentId = [int]$parent.ParentProcessId
    if ($parentId -le 0 -or -not $byId.ContainsKey($parentId)) { break }
    $parent = $byId[$parentId]
    if ($parent.Name -ieq 'PAOKRA Studio.exe') { $ownedByStudio = $true; break }
  }
  if (-not $ownedByStudio) { & taskkill.exe /PID ([int]$item.ProcessId) /T /F | Out-Null }
}
`;
      execFileSync("powershell.exe", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", orphanCleanup], {
        windowsHide: true, stdio: "ignore", timeout: 10000
      });
    } catch (_) {}
  }
  const env = {
    ...process.env,
    PAOKRA_HOME: RUNTIME_ROOT,
    PORT: String(PAOKRA_PORT),
    PAOKRA_PORT: String(PAOKRA_PORT),
    PAOKRA_DATA: DATA_ROOT,
    PAOKRA_WEB: WEB_ROOT,
    PAOKRA_COOKIE_FILE: path.join(DATA_ROOT, "shop.tiktok.com_cookies.txt"),
    PLAYWRIGHT_BROWSERS_PATH: path.join(RUNTIME_ROOT, "ms-playwright"),
    TEMP: SERVER_TEMP_ROOT,
    TMP: SERVER_TEMP_ROOT,
    PAOKRA_FFMPEG: findBundledFfmpeg(RUNTIME_ROOT, USER_DATA_ROOT) || ""
  };
  if (app.isPackaged) {
    if (!fs.existsSync(SERVER_EXE)) {
      throw new Error("PAOKRA backend runtime is missing");
    }
    serverProc = spawn(SERVER_EXE, [], {
      cwd: RUNTIME_ROOT, windowsHide: true, stdio: "ignore", env
    });
  } else {
    const useVenv = fs.existsSync(DEV_PYTHON);
    const pythonExe = useVenv ? DEV_PYTHON : "py";
    const pythonArgs = useVenv ? [SERVER_SCRIPT] : ["-3", SERVER_SCRIPT];
    serverProc = spawn(pythonExe, pythonArgs, {
      cwd: DEV_ROOT, windowsHide: true, stdio: "ignore", env
    });
  }
  serverProc.once("error", err => {
    console.error("PAOKRA backend start failed:", err);
  });
}

function postWebFrame(jpeg) {
  if (webPosting || !jpeg?.length) return;
  webPosting = true;
  const req = http.request({
    hostname: "127.0.0.1",
    port: PAOKRA_PORT,
    path: "/api/web/frame",
    method: "POST",
    headers: {
      "Content-Type": "image/jpeg",
      "Content-Length": jpeg.length
    }
  }, res => {
    res.resume();
    res.on("end", () => { webPosting = false; });
  });
  req.on("error", () => { webPosting = false; });
  req.setTimeout(2000, () => { webPosting = false; req.destroy(); });
  req.write(jpeg);
  req.end();
}

async function createWebSource(url, width, height) {
  if (!url) return;
  if (!webSourceWin || webSourceWin.isDestroyed()) {
    webSourceWin = new BrowserWindow({
      show: false,
      width: Math.max(64, Math.min(1920, Number(width) || 720)),
      height: Math.max(64, Math.min(1920, Number(height) || 1280)),
      paintWhenInitiallyHidden: true,
      backgroundColor: "#ffffff",
      webPreferences: {
        offscreen: true, // claude-fix-offscreen: must live in webPreferences or 'paint' never fires
        contextIsolation: true,
        nodeIntegration: false,
        backgroundThrottling: false
      }
    });
    traceTailwindWarnings(webSourceWin.webContents, "offscreen-web-source");
    webSourceWin.webContents.setBackgroundThrottling(false);
    clearInterval(webPaintTimer);
    webPaintTimer = setInterval(async () => {
      if (!webSourceWin || webSourceWin.isDestroyed() || !webSourceUrl || webCapturing) return;
      webCapturing = true;
      try {
        const image = await webSourceWin.webContents.capturePage();
        try { postWebFrame(image.toJPEG(70)); } catch (_) { webPosting = false; }
      } catch (_) {
      } finally {
        webCapturing = false;
      }
    }, 67);
    webSourceWin.on("closed", () => {
      webSourceWin = null;
      webSourceUrl = "";
      webSourceSize = "";
    });
  }
  webSourceUrl = url;
  webSourceSize = String(width) + "x" + String(height);
  try {
    if (webSourceWin.getBounds().width !== Number(width) ||
        webSourceWin.getBounds().height !== Number(height)) {
      webSourceWin.setContentSize(
        Math.max(64, Math.min(1920, Number(width) || 720)),
        Math.max(64, Math.min(1920, Number(height) || 1280))
      );
    }
    await webSourceWin.loadURL(url);
  } catch (_) {}
}

async function syncWebSource() {
  try {
    const data = await new Promise((resolve, reject) => {
      http.get(PAOKRA_URL + "/api/web/config", res => {
        let body = "";
        res.setEncoding("utf8");
        res.on("data", c => body += c);
        res.on("end", () => {
          try { resolve(JSON.parse(body)); } catch (e) { reject(e); }
        });
      }).on("error", reject);
    });
    const url = String(data.url || "");
    if (!url) {
      webSourceUrl = "";
      clearInterval(webPaintTimer);
      webPaintTimer = null;
      if (webSourceWin && !webSourceWin.isDestroyed()) webSourceWin.close();
      return;
    }
    const size = String(data.width) + "x" + String(data.height);
    if (url !== webSourceUrl || size !== webSourceSize) {
      await createWebSource(url, data.width, data.height);
    }
  } catch (_) {}
}

function startWebBridge() {
  clearInterval(webPollTimer);
  syncWebSource();
  webPollTimer = setInterval(syncWebSource, 800);
}

function environmentSplashHTML(state) {
  const row = (name, ready, detail) =>
    '<div class="row"><span class="dot ' + (ready ? 'ok' : 'bad') + '"></span><span class="name">' +
    name + '</span><span class="state">' + (ready ? 'พร้อม' : 'ต้องติดตั้ง') + '</span><small>' +
    String(detail || '').replace(/[&<>"]/g, s => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[s])) + '</small></div>';
  return '<!doctype html><html lang="th"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>' +
    '*{box-sizing:border-box}html,body{margin:0;width:100%;height:100%;font-family:Segoe UI,Arial,sans-serif;background:#080b14;color:#eef1fa}' +
    'body{display:grid;place-items:center;overflow:hidden;background:radial-gradient(circle at 18% 12%,#31235f 0,transparent 34%),radial-gradient(circle at 88% 92%,#12394b 0,transparent 34%),#080b14}' +
    '.card{width:min(680px,88vw);padding:34px;border:1px solid rgba(255,255,255,.1);border-radius:24px;background:rgba(18,23,38,.78);box-shadow:0 32px 100px rgba(0,0,0,.55);backdrop-filter:blur(18px)}' +
    '.brand{display:flex;align-items:center;gap:14px;margin-bottom:8px}.logo{width:54px;height:54px;border-radius:16px;display:grid;place-items:center;font-weight:800;background:linear-gradient(135deg,#7c6cff,#22d3ee);box-shadow:0 12px 30px rgba(124,108,255,.35)}' +
    'h1{margin:0;font-size:24px}.sub{margin:5px 0 22px;color:#9da7bc;font-size:13px}.rows{display:grid;gap:8px}.row{display:grid;grid-template-columns:12px 190px 88px 1fr;gap:10px;align-items:center;padding:10px 12px;border:1px solid rgba(255,255,255,.07);border-radius:12px;background:rgba(255,255,255,.035)}' +
    '.dot{width:8px;height:8px;border-radius:50%;background:#ef4444}.dot.ok{background:#34d399;box-shadow:0 0 10px rgba(52,211,153,.4)}.name{font-size:13px;font-weight:600}.state{font-size:11px;color:#aeb8cb}.row small{font-size:11px;color:#737f96;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
    '.track{height:8px;margin-top:18px;border-radius:999px;background:rgba(255,255,255,.08);overflow:hidden}.bar{height:100%;width:0;background:linear-gradient(90deg,#7c6cff,#22d3ee);transition:width .2s ease}' +
    '.status{display:flex;justify-content:space-between;gap:12px;margin-top:10px;color:#aeb8cb;font-size:12px}.pct{font-variant-numeric:tabular-nums;color:#fff;font-weight:700}' +
    '.note{margin-top:16px;padding-top:14px;border-top:1px solid rgba(255,255,255,.08);font-size:11px;line-height:1.6;color:#758097}' +
    '</style></head><body><main class="card"><div class="brand"><div class="logo">PK</div><div><h1>PAOKRA Studio</h1><div class="sub">กำลังเตรียมสภาพแวดล้อมของเครื่องสำหรับ Virtual Camera + Virtual Audio</div></div></div>' +
    '<div class="rows">' +
    row('FFmpeg Runtime', !!state.ffmpeg?.ready, state.ffmpeg?.ready ? 'bundled' : 'missing') +
    row('Virtual Camera', !!state.virtualCamera?.ready, state.virtualCamera?.ready ? state.virtualCamera.provider : 'OBS Virtual Camera') +
    row('Virtual Audio', !!state.virtualAudio?.ready, state.virtualAudio?.ready ? state.virtualAudio.provider : 'VB-CABLE / AudioRelay') +
    '</div><div class="track"><div class="bar" id="bar"></div></div><div class="status"><span id="status">กำลังตรวจสอบ…</span><span class="pct" id="pct">0%</span></div><div class="note">ติดตั้งเฉพาะส่วนที่ขาด และจำสภาพแวดล้อมไว้ในเครื่องนี้ รอบถัดไปจะเปิด PAOKRA ตามปกติเมื่ออุปกรณ์ยังพร้อมใช้งาน</div></main><script>window.pkEnvUpdate=function(p,m){var b=document.getElementById("bar"),s=document.getElementById("status"),x=document.getElementById("pct");if(b)b.style.width=Math.max(0,Math.min(100,Number(p)||0))+"%";if(s)s.textContent=String(m||"กำลังทำงาน…");if(x)x.textContent=Math.round(Math.max(0,Math.min(100,Number(p)||0)))+"%"};</script></body></html>';
}

async function showEnvironmentSplash(state) {
  if (!win || win.isDestroyed()) return;
  const html = environmentSplashHTML(state);
  await win.loadURL("data:text/html;charset=UTF-8," + encodeURIComponent(html));
  if (process.env.PAOKRA_QA_HEADLESS !== "1") win.show();
}

async function updateEnvironmentSplash(progress) {
  if (!win || win.isDestroyed()) return;
  const p = Math.max(0, Math.min(100, Number(progress?.percent) || 0));
  const msg = String(progress?.message || "กำลังเตรียมสภาพแวดล้อม…").replace(/\\/g, "\\\\").replace(/'/g, "\\'");
  try { await win.webContents.executeJavaScript("window.pkEnvUpdate(" + p + ",'" + msg + "')", true); } catch (_) {}
}

async function updateStartupLoading(percent, message) {
  if (!win || win.isDestroyed()) return;
  const p = Math.max(0, Math.min(100, Number(percent) || 0));
  const msg = String(message || "กำลังเตรียมระบบ…").replace(/\\/g, "\\\\").replace(/'/g, "\\'");
  try { await win.webContents.executeJavaScript("window.setLoading(" + p + ",'" + msg + "')", true); } catch (_) {}
}

function checkServerOnce(timeoutMs = 1200) {
  return new Promise(resolve => {
    const req = http.get(PAOKRA_URL + "/api/state", res => {
      const ok = res.statusCode === 200;
      res.resume();
      resolve(ok);
    });
    req.setTimeout(timeoutMs, () => {
      try { req.destroy(); } catch (_) {}
      resolve(false);
    });
    req.on("error", () => resolve(false));
  });
}

async function waitForServer(timeoutMs = 90000) {
  const started = Date.now();
  let lastProgress = -1;
  while (Date.now() - started < timeoutMs) {
    if (!win || win.isDestroyed()) return false;
    if (await checkServerOnce()) return true;
    const elapsed = Date.now() - started;
    const p = Math.min(94, 35 + (elapsed / timeoutMs) * 59);
    const rounded = Math.floor(p / 2) * 2;
    if (rounded !== lastProgress) {
      lastProgress = rounded;
      await updateStartupLoading(rounded, "กำลังเริ่ม Backend ของ PAOKRA…");
    }
    await new Promise(resolve => setTimeout(resolve, 300));
  }
  return false;
}

async function createWindow() {
  win = new BrowserWindow({
    width: 1440, height: 900, minWidth: 1100, minHeight: 700,
    title: `PAOKRA Studio v${app.getVersion()}`,
    show: false,
    backgroundColor: "#080b14",
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      webviewTag: true,
      preload: path.join(__dirname, "preload.cjs")
    }
  });
  traceTailwindWarnings(win.webContents, "main-studio-window");
  // Close auxiliary off-screen windows when the main Studio window is closed.
  // Otherwise Electron's window-all-closed event never fires, before-quit does
  // not run, and the backend/OBS sender may outlive the visible app.
  win.on("closed", () => {
    clearTimeout(dashboardRetryTimer);
    clearInterval(webPollTimer);
    webPollTimer = null;
    clearInterval(webPaintTimer);
    webPaintTimer = null;
    webSourceUrl = "";
    webSourceSize = "";
    if (webSourceWin && !webSourceWin.isDestroyed()) {
      const offscreen = webSourceWin;
      webSourceWin = null;
      try { offscreen.destroy(); } catch (_) { try { offscreen.close(); } catch (_) {} }
    }
    win = null;
  });

  const loading = `<!doctype html><html lang="th"><head><meta charset="utf-8"><style>
*{box-sizing:border-box}html,body{margin:0;width:100%;height:100%;overflow:hidden;font-family:Segoe UI,Inter,Arial,sans-serif}
body{display:flex;align-items:center;justify-content:center;background:radial-gradient(circle at 50% 30%,#242a3f 0,#10131d 48%,#090b11 100%);color:#fff}
.bg{position:absolute;inset:0;overflow:hidden}.orb{position:absolute;border-radius:50%;filter:blur(70px);opacity:.42;animation:float 7s ease-in-out infinite}.a{width:360px;height:360px;background:#7c3aed;left:-80px;top:-120px}.b{width:300px;height:300px;background:#06b6d4;right:-70px;bottom:-100px;animation-delay:-3s}
.card{position:relative;width:520px;padding:48px 52px;border:1px solid rgba(255,255,255,.12);border-radius:28px;background:rgba(20,23,34,.78);box-shadow:0 30px 90px rgba(0,0,0,.48);backdrop-filter:blur(22px);text-align:center}
.logo{width:70px;height:70px;margin:0 auto 22px;border-radius:20px;display:grid;place-items:center;font-size:25px;font-weight:800;letter-spacing:-1px;background:linear-gradient(135deg,#8b5cf6,#06b6d4);box-shadow:0 12px 35px rgba(124,58,237,.35);animation:pulse 2.2s ease-in-out infinite}
h1{margin:0;font-size:28px;letter-spacing:-.7px}.sub{margin:9px 0 30px;color:#aeb5c7;font-size:14px}
.track{height:5px;border-radius:99px;background:rgba(255,255,255,.09);overflow:hidden}.bar{height:100%;width:42%;border-radius:99px;background:linear-gradient(90deg,#8b5cf6,#22d3ee);animation:load 1.65s ease-in-out infinite}
.status{margin-top:16px;color:#8992a8;font-size:12px;min-height:18px}.dots{display:inline-flex;gap:5px;margin-left:6px}.dots i{width:4px;height:4px;border-radius:50%;background:#8b5cf6;animation:dot 1.1s infinite}.dots i:nth-child(2){animation-delay:.15s}.dots i:nth-child(3){animation-delay:.3s}
@keyframes load{0%{transform:translateX(-120%);width:32%}50%{width:58%}100%{transform:translateX(210%);width:32%}}@keyframes pulse{50%{transform:translateY(-3px) scale(1.03)}}@keyframes float{50%{transform:translate3d(25px,18px,0) scale(1.08)}}@keyframes dot{0%,100%{opacity:.25;transform:scale(.7)}50%{opacity:1;transform:scale(1)}}
</style></head><body><div class="bg"><div class="orb a"></div><div class="orb b"></div></div><main class="card"><div class="logo">PK</div><h1>PAOKRA Studio</h1><div class="sub">กำลังเตรียมระบบสำหรับ Live Studio</div><div class="track"><div class="bar"></div></div><div class="status" id="status">กำลังเชื่อมต่อ Backend <span id="pct">0%</span> <span class="dots"><i></i><i></i><i></i></span></div></main><script>window.setLoading=function(p,t){var b=document.querySelector('.bar'),s=document.getElementById('status'),x=document.getElementById('pct');if(b)b.style.width=Math.max(3,Math.min(100,p))+'%';if(x)x.textContent=Math.round(p)+'%';if(s)s.firstChild.textContent=t+' ';}</script></body></html>`;
  // Show the real startup loading screen before any synchronous environment scan.
  // The environment checker uses Windows child processes and can block the Electron
  // main thread for several seconds, so the renderer must get a chance to paint first.
  await win.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(loading));
  if (process.env.PAOKRA_QA_HEADLESS !== "1") win.show();
  await new Promise(resolve => setTimeout(resolve, 60));

  const isDashboardURL = url => String(url || "").startsWith(PAOKRA_APP_URL);
  let dashboardLoaded = false;
  let dashboardLoadInFlight = false;
  let dashboardRetryCount = 0;

  const loadDashboard = async () => {
    if (!win || win.isDestroyed() || dashboardLoaded || dashboardLoadInFlight) return;
    clearTimeout(dashboardRetryTimer);
    dashboardLoadInFlight = true;
    try {
      // Only startup navigation. After the PAOKRA document is loaded,
      // tab switching is hash-only and must never cause a top-level reload.
      await win.loadURL(PAOKRA_APP_URL + "/?boot=" + Date.now());
    } catch (_) {
    } finally {
      dashboardLoadInFlight = false;
    }
  };

  win.webContents.on("did-finish-load", () => {
    // Mark the first successful PAOKRA document load as the end of startup
    // navigation. Never run a DOM verification/reload loop after this point.
    if (isDashboardURL(win.webContents.getURL())) {
      dashboardLoaded = true;
      dashboardLoadInFlight = false;
      dashboardRetryCount = 0;
      clearTimeout(dashboardRetryTimer);
    }
  });

  win.webContents.on("did-fail-load", (_event, _errorCode, _errorDescription, validatedURL) => {
    // This handler is strictly for a failed initial navigation. Hash changes
    // inside PAOKRA do not reach this path.
    if (!isDashboardURL(validatedURL) || !win || win.isDestroyed() || dashboardLoaded) return;
    dashboardLoadInFlight = false;
    if (dashboardRetryCount >= 2) return;
    dashboardRetryCount += 1;
    clearTimeout(dashboardRetryTimer);
    dashboardRetryTimer = setTimeout(loadDashboard, 250);
  });
  dashboardLoadFn = loadDashboard;
}

ipcMain.handle("open-tiktok-dashboard", async () => {
  try {
    if (tiktokDashboardWin && !tiktokDashboardWin.isDestroyed()) {
      tiktokDashboardWin.show();
      tiktokDashboardWin.focus();
      return { ok: true, open: true, reused: true, url: tiktokDashboardWin.webContents.getURL() || TIKTOK_DASHBOARD_URL };
    }
    const tiktokSession = session.fromPartition(TIKTOK_SESSION_PARTITION);
    tiktokDashboardWin = new BrowserWindow({
      width: 1280,
      height: 820,
      minWidth: 980,
      minHeight: 640,
      title: "TikTok Dashboard · PAOKRA",
      backgroundColor: "#ffffff",
      autoHideMenuBar: true,
      webPreferences: {
        session: tiktokSession,
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true
      }
    });
    traceTailwindWarnings(tiktokDashboardWin.webContents, "tiktok-dashboard-window");
    tiktokDashboardWin.on("closed", () => { tiktokDashboardWin = null; });
    await tiktokDashboardWin.loadURL(TIKTOK_DASHBOARD_URL);
    tiktokDashboardWin.show();
    tiktokDashboardWin.focus();
    return { ok: true, open: true, reused: false, url: tiktokDashboardWin.webContents.getURL() || TIKTOK_DASHBOARD_URL };
  } catch (err) {
    tiktokDashboardWin = null;
    return { ok: false, error: "เปิด TikTok Dashboard ไม่สำเร็จ: " + (err?.message || String(err)) };
  }
});

ipcMain.handle("tiktok-dashboard-status", async () => {
  const open = !!(tiktokDashboardWin && !tiktokDashboardWin.isDestroyed());
  return {
    ok: true,
    open,
    url: open ? tiktokDashboardWin.webContents.getURL() : TIKTOK_DASHBOARD_URL,
    title: open ? tiktokDashboardWin.getTitle() : ""
  };
});

ipcMain.handle("open-external", async (_event, url) => {
  const value = String(url || "");
  if (!/^https:\/\/openreel\.video(?:\/|$)/i.test(value)) return { ok: false, error: "ไม่อนุญาต URL นี้" };
  try { await shell.openExternal(value); return { ok: true }; }
  catch (err) { return { ok: false, error: err?.message || String(err) }; }
});

ipcMain.handle("open-ai-studio", async () => {
  const candidates = app.isPackaged
    ? [
        path.join(process.resourcesPath, "paokra-ai", "PAOKRA AI Studio.exe"),
        path.join(path.dirname(process.resourcesPath), "PAOKRA AI Studio", "PAOKRA AI Studio.exe")
      ]
    : [
        path.join(DEV_ROOT, "AI Studio", "PAOKRA AI Studio.exe"),
        path.join(DEV_ROOT, "dist-ai", "win-unpacked", "PAOKRA AI Studio.exe")
      ];
  const exe = candidates.find((p) => fs.existsSync(p));
  if (!exe) return { ok: false, error: "ไม่พบ PAOKRA AI Studio.exe — สร้าง/ติดตั้ง AI Studio ก่อน" };
  try {
    const child = spawn(exe, [], {
      cwd: path.dirname(exe),
      detached: true,
      stdio: "ignore",
      windowsHide: false
    });
    child.unref();
    return { ok: true, path: exe };
  } catch (err) {
    return { ok: false, error: err?.message || String(err) };
  }
});

ipcMain.handle("select-video", async () => {
  const r = await dialog.showOpenDialog({
    title: "เลือกไฟล์วิดีโอ",
    properties: ["openFile"],
    filters: [{ name: "Video", extensions: ["mp4","mov","mkv","webm","m4v","avi"] }]
  });
  return r.canceled || !r.filePaths.length ? null : r.filePaths[0];
});
ipcMain.handle("select-videos", async (_event, options = {}) => {
  const role = ["open", "normal", "stop", "seam"].includes(String(options?.role || ""))
    ? String(options.role) : "normal";
  const titles = {
    open: "เลือกคลิป Open (คลิปเปิด)",
    normal: "เลือกคลิป Normal (เลือกได้หลายไฟล์)",
    stop: "เลือกคลิป Stop (คลิปปิด)",
    seam: "AI Seam Studio — เพิ่มคลิป (เลือกได้หลายไฟล์)"
  };
  const properties = ["normal", "seam"].includes(role)
    ? ["openFile", "multiSelections"]
    : ["openFile"];
  const r = await dialog.showOpenDialog({
    title: titles[role],
    properties,
    filters: [{ name: "Video", extensions: ["mp4", "mov", "mkv", "webm", "m4v", "avi"] }]
  });
  return r.canceled ? [] : r.filePaths;
});


let updaterInstance = null;
let updaterManualCheckRequested = false;
let updaterCheckInFlight = false;
let updateInstallPromptShown = false;
let updateInstallRetryTimer = null;
let updateInstallInProgress = false;
let updateInstallWaitLogged = false;
let updateInstallIdleChecks = 0;

function writeUpdaterLog(level, message) {
  try {
    const folder = path.join(USER_DATA_ROOT, "logs");
    fs.mkdirSync(folder, { recursive: true });
    fs.appendFileSync(path.join(folder, "updater.log"),
      JSON.stringify({ time: new Date().toISOString(), level, message: String(message || "").slice(0, 1400) }) + "\n", "utf8");
  } catch (_) {}
}

function showUpdaterDialog(options) {
  const owner = win && !win.isDestroyed() ? win : null;
  return owner ? dialog.showMessageBox(owner, options) : dialog.showMessageBox(options);
}

async function runUpdateCheck(manual = false) {
  if (!app.isPackaged) {
    if (manual) await showUpdaterDialog({
      type: "info", title: "PAOKRA Studio", message: "โหมดพัฒนาไม่ตรวจ Auto-update",
      detail: "ระบบอัปเดตจะทำงานในแอปที่ติดตั้งแล้วเท่านั้น", buttons: ["ตกลง"]
    });
    return { ok: false, state: "not-packaged", dialog: Boolean(manual), error: "Auto-update requires the installed desktop app" };
  }
  if (process.env.PAOKRA_DISABLE_UPDATES === "1") {
    if (manual) await showUpdaterDialog({
      type: "info", title: "PAOKRA Studio", message: "ปิดการตรวจอัปเดตไว้สำหรับ QA",
      buttons: ["ตกลง"]
    });
    return { ok: false, state: "disabled", dialog: Boolean(manual), error: "Updates are disabled for QA" };
  }
  if (!updaterInstance) {
    if (manual) await showUpdaterDialog({
      type: "warning", title: "PAOKRA Studio", message: "ระบบตรวจอัปเดตยังไม่พร้อม",
      detail: "ลองเปิดเมนูนี้อีกครั้งหลังหน้าหลักโหลดเสร็จ", buttons: ["ตกลง"]
    });
    return { ok: false, state: "not-ready", dialog: Boolean(manual), error: "Updater is not initialized" };
  }
  if (updaterCheckInFlight) {
    if (manual) await showUpdaterDialog({
      type: "info", title: "PAOKRA Studio", message: "กำลังตรวจสอบเวอร์ชันอยู่แล้ว", buttons: ["ตกลง"]
    });
    return { ok: true, state: "already-checking", dialog: Boolean(manual) };
  }
  updaterCheckInFlight = true;
  updaterManualCheckRequested = Boolean(manual);
  writeUpdaterLog("info", "Check started; version=" + app.getVersion() + "; manual=" + manual);
  try {
    const result = await updaterInstance.checkForUpdates();
    return { ok: true, state: "checking", version: result?.updateInfo?.version || null };
  } catch (error) {
    const detail = error?.message || String(error);
    writeUpdaterLog("error", "Update check failed: " + detail);
    if (updaterManualCheckRequested) {
      await showUpdaterDialog({
        type: "warning", title: "PAOKRA Studio — ตรวจอัปเดตไม่สำเร็จ",
        message: "เชื่อมต่อ GitHub Releases ไม่สำเร็จ",
        detail: detail.slice(0, 1000), buttons: ["ลองใหม่ภายหลัง"]
      });
    }
    updaterManualCheckRequested = false;
    return { ok: false, state: "error", dialog: Boolean(manual), error: detail.slice(0, 1000) };
  } finally {
    updaterCheckInFlight = false;
  }
}

function scheduleSilentUpdateInstall(delayMs = 5000) {
  if (updateInstallRetryTimer) clearTimeout(updateInstallRetryTimer);
  updateInstallRetryTimer = setTimeout(() => {
    updateInstallRetryTimer = null;
    void trySilentUpdateInstall();
  }, Math.max(1000, Number(delayMs) || 5000));
}
function queryRuntimeIdleForUpdate() {
  return new Promise(resolve => {
    const request = http.get(PAOKRA_URL + "/api/state", { timeout: 1800 }, response => {
      let data = "";
      response.setEncoding("utf8");
      response.on("data", chunk => {
        data += chunk;
        if (data.length > 256 * 1024) request.destroy(new Error("runtime state response too large"));
      });
      response.on("end", () => {
        try {
          if (response.statusCode !== 200) return resolve({ ok: false, reason: "HTTP " + response.statusCode });
          const state = JSON.parse(data);
          if (typeof state.running !== "boolean" || typeof state.virtualcam !== "boolean") return resolve({ ok: false, reason: "incomplete runtime state" });
          resolve({ ok: true, idle: !state.running && !state.virtualcam, running: state.running, virtualcam: state.virtualcam });
        } catch (error) { resolve({ ok: false, reason: error?.message || "invalid runtime state" }); }
      });
    });
    request.on("timeout", () => request.destroy(new Error("runtime state timeout")));
    request.on("error", error => resolve({ ok: false, reason: error?.message || "runtime not ready" }));
  });
}
async function trySilentUpdateInstall() {
  if (!updaterInstance || updateInstallInProgress || app.isQuitting) return;
  const runtime = await queryRuntimeIdleForUpdate();
  if (!runtime.ok) {
    if (!updateInstallWaitLogged) {
      writeUpdaterLog("info", "Silent install is waiting for runtime status: " + runtime.reason);
      updateInstallWaitLogged = true;
    }
    scheduleSilentUpdateInstall(15000);
    return;
  }
  if (!runtime.idle) {
    updateInstallIdleChecks = 0;
    if (!updateInstallWaitLogged) {
      writeUpdaterLog("info", "Silent install deferred until Engine and Virtual Camera are stopped.");
      updateInstallWaitLogged = true;
    }
    scheduleSilentUpdateInstall(15000);
    return;
  }
  // Never restart PAOKRA in the middle of active UI work. Wait until the
  // main window is in the background/minimized, then recheck idle twice.
  if (win && !win.isDestroyed() && win.isVisible() && win.isFocused() && !win.isMinimized()) {
    updateInstallIdleChecks = 0;
    if (!updateInstallWaitLogged) {
      writeUpdaterLog("info", "Silent install deferred while the PAOKRA window is in active use.");
      updateInstallWaitLogged = true;
    }
    scheduleSilentUpdateInstall(15000);
    return;
  }
  updateInstallIdleChecks += 1;
  if (updateInstallIdleChecks < 2) {
    scheduleSilentUpdateInstall(5000);
    return;
  }
  updateInstallInProgress = true;
  if (updateInstallRetryTimer) { clearTimeout(updateInstallRetryTimer); updateInstallRetryTimer = null; }
  writeUpdaterLog("info", "Runtime idle; installing downloaded update silently and restarting PAOKRA.");
  try {
    updaterInstance.quitAndInstall(true, true);
  } catch (error) {
    updateInstallInProgress = false;
    writeUpdaterLog("error", "Silent install could not start: " + (error?.message || String(error)));
    scheduleSilentUpdateInstall(15000);
  }
}

function setupAutoUpdater() {
  if (!app.isPackaged) {
    writeUpdaterLog("info", "Auto-update is available only in packaged builds.");
    return;
  }
  try {
    const { autoUpdater } = require("electron-updater");
    updaterInstance = autoUpdater;
    // Packaged QA must load the real dependency and validate its runtime without
    // making a network request or attempting to install a release during tests.
    if (process.env.PAOKRA_DISABLE_UPDATES === "1") {
      writeUpdaterLog("info", "Auto-update dependency loaded; network checks disabled for QA.");
      return;
    }
    updaterInstance.autoDownload = true;
    updaterInstance.autoInstallOnAppQuit = true;
    updaterInstance.allowPrerelease = false;
    updaterInstance.logger = {
      info: (...args) => writeUpdaterLog("info", args.map(String).join(" ")),
      warn: (...args) => writeUpdaterLog("warn", args.map(String).join(" ")),
      error: (...args) => writeUpdaterLog("error", args.map(String).join(" ")),
      debug: (...args) => writeUpdaterLog("debug", args.map(String).join(" "))
    };
    updaterInstance.on("checking-for-update", () => writeUpdaterLog("info", "Checking GitHub Releases"));
    updaterInstance.on("update-available", info => {
      const version = String(info?.version || "รุ่นใหม่");
      writeUpdaterLog("info", "Update available: " + version);
      updaterManualCheckRequested = false;
      try {
        if (Notification.isSupported()) {
          new Notification({
            title: "PAOKRA Studio — พบอัปเดต",
            body: "เวอร์ชัน " + version + " กำลังดาวน์โหลดโดยอัตโนมัติ"
          }).show();
        }
      } catch (_) {}
    });
    updaterInstance.on("update-not-available", info => {
      writeUpdaterLog("info", "Already current: " + String(info?.version || app.getVersion()));
      if (updaterManualCheckRequested) {
        showUpdaterDialog({
          type: "info", title: "PAOKRA Studio",
          message: "คุณใช้ PAOKRA Studio เวอร์ชันล่าสุดแล้ว",
          detail: "เวอร์ชันปัจจุบัน: " + app.getVersion(), buttons: ["ตกลง"]
        }).catch(() => {});
      }
      updaterManualCheckRequested = false;
    });
    updaterInstance.on("download-progress", progress => {
      const percent = Math.max(0, Math.min(100, Math.round(Number(progress?.percent) || 0)));
      writeUpdaterLog("info", "Download progress: " + percent + "%");
    });
    updaterInstance.on("update-downloaded", info => {
      if (updateInstallPromptShown) return;
      updateInstallPromptShown = true;
      updateInstallIdleChecks = 0;
      updateInstallWaitLogged = false;
      const version = String(info?.version || "รุ่นใหม่");
      writeUpdaterLog("info", "Update downloaded: " + version + "; silent install scheduled.");
      try {
        if (Notification.isSupported()) {
          new Notification({
            title: "PAOKRA Studio — อัปเดตพร้อมติดตั้ง",
            body: "ดาวน์โหลดเวอร์ชัน " + version + " แล้ว จะติดตั้งและเปิดโปรแกรมใหม่ให้อัตโนมัติเมื่อ Engine และ Virtual Camera หยุดอยู่"
          }).show();
        }
      } catch (_) {}
      scheduleSilentUpdateInstall(5000);
    });
    updaterInstance.on("error", error => {
      const detail = error?.message || String(error);
      if (updateInstallInProgress) updateInstallInProgress = false;
      writeUpdaterLog("error", "Updater error: " + detail);
      if (updateInstallPromptShown && !app.isQuitting) scheduleSilentUpdateInstall(15000);
      if (updaterManualCheckRequested) {
        showUpdaterDialog({
          type: "warning", title: "PAOKRA Studio — ตรวจอัปเดตไม่สำเร็จ",
          message: "ตรวจสอบ GitHub Releases ไม่สำเร็จ",
          detail: detail.slice(0, 1000), buttons: ["ตกลง"]
        }).catch(() => {});
      }
      updaterManualCheckRequested = false;
    });
    writeUpdaterLog("info", "Auto-update ready.");
    setTimeout(() => { void runUpdateCheck(false); }, 2500);
  } catch (error) {
    updaterInstance = null;
    writeUpdaterLog("error", "Could not initialize electron-updater: " + (error?.message || String(error)));
  }
}

ipcMain.handle("check-for-updates", async () => {
  return await runUpdateCheck(true);
});

function installApplicationMenu() {
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    {
      label: "Help",
      submenu: [
        { label: "ตรวจสอบการอัปเดต…", click: () => { void runUpdateCheck(true); } },
        { type: "separator" },
        { label: "PAOKRA Studio v" + app.getVersion(), enabled: false }
      ]
    }
  ]));
}

app.whenReady().then(async () => {
  try {
    registerPaokraProtocol();
    await createWindow();
    await updateStartupLoading(4, "กำลังตรวจสอบสภาพแวดล้อมของเครื่อง…");

    // One-time-per-PC environment preparation. Never require Node.js or Python on
    // the user's PC: the packaged backend is a standalone executable and FFmpeg is
    // bundled with PAOKRA. Missing system drivers are installed from the app itself.
    let env = inspectEnvironment(RUNTIME_ROOT);
    // Isolated smoke-test only: never run driver installers or request UAC just
    // to verify an app package. Normal launches retain the full setup flow.
    const qaSkipEnvironmentSetup = process.env.PAOKRA_QA_SKIP_ENV === "1";
    if (!env.ok && !qaSkipEnvironmentSetup) {
      await showEnvironmentSplash(env);
      let result = await ensureEnvironment({
        runtimeRoot: RUNTIME_ROOT,
        userDataRoot: USER_DATA_ROOT,
        onProgress: updateEnvironmentSplash
      });
      if (!result.ready) {
        const choice = await dialog.showMessageBox(win, {
          type: "warning",
          title: "PAOKRA Environment Setup",
          message: "สภาพแวดล้อมยังไม่พร้อมใช้งาน",
          detail: result.error || "ติดตั้งส่วนประกอบไม่สำเร็จ อาจต้องกดอนุญาต UAC แล้วลองใหม่",
          buttons: ["ลองติดตั้งอีกครั้ง", "เข้าใช้งานต่อ"],
          defaultId: 0,
          cancelId: 1
        });
        if (choice.response === 0) {
          result = await ensureEnvironment({
            runtimeRoot: RUNTIME_ROOT,
            userDataRoot: USER_DATA_ROOT,
            onProgress: updateEnvironmentSplash
          });
        }
      }
      env = result.state || inspectEnvironment(RUNTIME_ROOT);
      if (!env.ok) {
        await updateEnvironmentSplash({ percent: 100, message: "ยังติดตั้งส่วนประกอบไม่ครบ — ไม่สามารถเริ่ม PAOKRA ได้" });
        return;
      }
      // Re-show the normal startup loading screen after the environment installer.
      await win.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(
        environmentSplashHTML(env).replace("กำลังเตรียมสภาพแวดล้อมของเครื่องสำหรับ Virtual Camera + Virtual Audio", "สภาพแวดล้อมพร้อมแล้ว — กำลังเริ่ม PAOKRA Studio")
      ));
      if (process.env.PAOKRA_QA_HEADLESS !== "1") win.show();
    }

    await updateStartupLoading(18, "กำลังเริ่ม Backend ของ PAOKRA…");
    try {
      await startFlask();
    } catch (err) {
      await updateStartupLoading(100, "เริ่ม Backend ไม่สำเร็จ: " + (err?.message || String(err)));
      await dialog.showMessageBox(win, {
        type: "error",
        title: "PAOKRA Studio",
        message: "ไม่สามารถเริ่ม Backend ได้",
        detail: err?.message || String(err),
        buttons: ["ปิด"]
      });
      return;
    }

    // Never navigate to the real web app until the backend has answered HTTP 200.
    // This prevents the chrome-error://chromewebdata blank page that previously
    // required a manual Refresh on slower PCs.
    const ready = await waitForServer(210000);
    if (!ready) {
      await updateStartupLoading(100, "Backend ไม่ตอบสนอง — กำลังหยุดเพื่อป้องกันหน้าเว็บว่าง");
      await dialog.showMessageBox(win, {
        type: "error",
        title: "PAOKRA Studio",
        message: "PAOKRA Backend เริ่มไม่สำเร็จ",
        detail: `โปรดลองเปิด PAOKRA Studio ใหม่ หากยังเกิดซ้ำ อาจมีโปรแกรมอื่นใช้พอร์ต ${PAOKRA_PORT} อยู่`,
        buttons: ["ปิด"]
      });
      return;
    }

    await updateStartupLoading(97, "กำลังเปิด PAOKRA Studio…");
    clearTimeout(dashboardRetryTimer);
    dashboardLoadFn?.();
    startWebBridge();
    installApplicationMenu();
    setupAutoUpdater();
  } catch (err) {
    console.error("PAOKRA startup failed:", err);
    try {
      await updateStartupLoading(100, "เกิดข้อผิดพลาดในการเริ่ม PAOKRA Studio");
      await dialog.showMessageBox(win, {
        type: "error",
        title: "PAOKRA Studio",
        message: "เริ่มโปรแกรมไม่สำเร็จ",
        detail: err?.message || String(err),
        buttons: ["ปิด"]
      });
    } catch (_) {}
  }
});

app.on("before-quit", () => {
  // This also runs when quitting from the app menu, not just when the last
  // BrowserWindow closes. Kill the owned backend process TREE so PyInstaller's
  // worker, virtual camera sender, and FFmpeg child cannot remain orphaned.
  stopBackendProcessTree();
});

app.on("window-all-closed", () => {
  clearTimeout(dashboardRetryTimer);
  clearInterval(webPollTimer);
  clearInterval(webPaintTimer);
  if (webSourceWin && !webSourceWin.isDestroyed()) {
    try { webSourceWin.close(); } catch (_) {}
  }
  app.quit();
});
