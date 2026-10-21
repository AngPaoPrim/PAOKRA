"""tt_core: auth (real-browser login via CDP, DPAPI-encrypted session) + live (ffmpeg RTMP push).
stdlib only. The password is typed by the user into the real Chrome window; this tool never sees it."""
import base64, ctypes, ctypes.wintypes as wt, json, os, re, shutil, socket, struct, subprocess, sys, time, urllib.request
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TikTokLSCLI"
SESSION = CONFIG_DIR / "session.bin"
PROFILE = CONFIG_DIR / "chrome-profile"
STATE = CONFIG_DIR / "live.json"
FFLOG = CONFIG_DIR / "ffmpeg-live.log"
PROGRESS = CONFIG_DIR / "ffmpeg-progress.txt"
PORT = 9333
LOGIN_URL = "https://www.tiktok.com/login"
PRODUCER_URL = "https://livecenter.tiktok.com/producer"

# ---------------- DPAPI ----------------
class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

def dpapi(data: bytes, protect=True) -> bytes:
    buf = ctypes.create_string_buffer(data, len(data))
    inb = _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _BLOB()
    f = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not f(ctypes.byref(inb), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI failed")
    addr = ctypes.cast(out.pbData, ctypes.c_void_p).value
    res = ctypes.string_at(addr, out.cbData)
    ctypes.windll.kernel32.LocalFree(ctypes.c_void_p(addr))
    return res

def save_session(cookies):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SESSION.write_bytes(dpapi(json.dumps({"saved_at": int(time.time()), "cookies": cookies}).encode()))

def load_session():
    try:
        return json.loads(dpapi(SESSION.read_bytes(), False).decode())
    except Exception:
        return None

# ---------------- minimal websocket + CDP ----------------
class WS:
    def __init__(s, url):
        host, port, path = re.match(r"ws://([^:/]+):(\d+)(/.*)", url).groups()
        s.sock = socket.create_connection((host, int(port)), timeout=15)
        key = base64.b64encode(os.urandom(16)).decode()
        s.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            c = s.sock.recv(4096)
            if not c: raise EOFError("handshake")
            buf += c
        head, s.rest = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]: raise RuntimeError("ws handshake failed")
    def _read(s, n):
        while len(s.rest) < n:
            c = s.sock.recv(65536)
            if not c: raise EOFError
            s.rest += c
        d, s.rest = s.rest[:n], s.rest[n:]
        return d
    def send(s, text):
        p = text.encode(); L = len(p); h = bytearray([0x81])
        if L < 126: h.append(0x80 | L)
        elif L < 65536: h.append(0x80 | 126); h += struct.pack(">H", L)
        else: h.append(0x80 | 127); h += struct.pack(">Q", L)
        m = os.urandom(4); h += m
        s.sock.sendall(bytes(h) + bytes(b ^ m[i % 4] for i, b in enumerate(p)))
    def recv(s):
        data = b""
        while True:
            b1, b2 = s._read(2); op = b1 & 15; L = b2 & 127
            if L == 126: L = struct.unpack(">H", s._read(2))[0]
            elif L == 127: L = struct.unpack(">Q", s._read(8))[0]
            pl = s._read(L)
            if op == 8: raise EOFError
            if op in (9, 10): continue
            data += pl
            if b1 & 0x80: return data.decode("utf-8", "replace")
    def close(s):
        try: s.sock.close()
        except Exception: pass

class CDP:
    def __init__(s, url): s.ws = WS(url); s.i = 0
    def call(s, method, params=None):
        s.i += 1
        s.ws.send(json.dumps({"id": s.i, "method": method, "params": params or {}}))
        while True:
            m = json.loads(s.ws.recv())
            if m.get("id") == s.i:
                if "error" in m: raise RuntimeError(m["error"])
                return m.get("result", {})
    def close(s): s.ws.close()

def _http(path, method="GET", timeout=2):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def cdp_alive():
    try: _http("/json/version", timeout=1); return True
    except Exception: return False

def wait_cdp(timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        if cdp_alive(): return True
        time.sleep(0.5)
    return False

def find_chrome():
    pf, pf86, la = os.environ.get("PROGRAMFILES", ""), os.environ.get("PROGRAMFILES(X86)", ""), os.environ.get("LOCALAPPDATA", "")
    for p in (pf + r"\Google\Chrome\Application\chrome.exe", pf86 + r"\Google\Chrome\Application\chrome.exe",
              la + r"\Google\Chrome\Application\chrome.exe", pf86 + r"\Microsoft\Edge\Application\msedge.exe",
              pf + r"\Microsoft\Edge\Application\msedge.exe"):
        if p and os.path.exists(p): return p
    return None

def launch_browser(url):
    chrome = find_chrome()
    if not chrome: raise RuntimeError("Chrome/Edge not found")
    PROFILE.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen([chrome, f"--remote-debugging-port={PORT}", "--remote-allow-origins=*",
                             f"--user-data-dir={PROFILE}", "--no-first-run", "--no-default-browser-check", url])

def browser_cdp():
    return CDP(_http("/json/version")["webSocketDebuggerUrl"])

def tiktok_cookies():
    b = browser_cdp()
    try: cs = b.call("Storage.getCookies").get("cookies", [])
    finally: b.close()
    return [c for c in cs if "tiktok" in c.get("domain", "")]

def close_browser():
    try:
        b = browser_cdp(); b.call("Browser.close")
    except Exception:
        pass

# ---------------- auth ----------------
def _sid(cookies):
    for c in cookies:
        if c["name"] in ("sessionid", "sessionid_ss"): return c
    return None

def auth_login(timeout=300, keep_open=False):
    if not cdp_alive(): launch_browser(LOGIN_URL)
    else: _http("/json/new?" + LOGIN_URL, "PUT")
    if not wait_cdp(): return {"ok": False, "error": "browser_cdp_unavailable"}
    print("Chrome opened. Log in to TikTok in that window (captcha/2FA included). Waiting...", file=sys.stderr)
    end = time.time() + timeout
    while time.time() < end:
        try:
            cs = tiktok_cookies()
            if _sid(cs):
                save_session(cs)
                if not keep_open: close_browser()
                return auth_status()
        except Exception:
            pass
        time.sleep(2)
    return {"ok": False, "error": "login_timeout"}

def auth_status():
    s = load_session()
    if not s: return {"ok": True, "logged_in": False, "note": "no saved session"}
    c = _sid(s["cookies"])
    if not c: return {"ok": True, "logged_in": False, "note": "session has no sessionid"}
    exp = c.get("expires", -1)
    expired = exp not in (-1, None) and exp < time.time()
    return {"ok": True, "logged_in": not expired, "saved_at": s["saved_at"],
            "expires": None if exp in (-1, None) else int(exp), "expired": expired,
            "cookie_count": len(s["cookies"]), "storage": "DPAPI-encrypted (current Windows user only)"}

def auth_logout(wipe_profile=False):
    close_browser(); time.sleep(1)
    SESSION.unlink(missing_ok=True)
    if wipe_profile: shutil.rmtree(PROFILE, ignore_errors=True)
    return {"ok": True, "session_removed": True, "profile_wiped": wipe_profile}

# ---------------- stream key (best effort, logged-in account only) ----------------
_JS = ("(()=>{const t=document.body?document.body.innerText:'';"
       "const v=[...document.querySelectorAll('input,textarea')].map(e=>e.value).filter(Boolean);"
       "return JSON.stringify({u:location.href,t:t.slice(0,30000),v})})()")

def get_stream_info(page_url=None, wait=120):
    """Reads the RTMP URL + key shown on the LIVE Producer page of the logged-in account.
    Only works if TikTok itself grants this account LIVE access. No eligibility bypass."""
    if not load_session(): return {"ok": False, "error": "not_logged_in"}
    page_url = page_url or PRODUCER_URL
    if not cdp_alive(): launch_browser(page_url)
    else: _http("/json/new?" + page_url, "PUT")
    if not wait_cdp(): return {"ok": False, "error": "browser_cdp_unavailable"}
    end = time.time() + wait; last = None
    while time.time() < end:
        try:
            for p in _http("/json"):
                if p.get("type") != "page" or "tiktok" not in p.get("url", ""): continue
                c = CDP(p["webSocketDebuggerUrl"])
                try: r = c.call("Runtime.evaluate", {"expression": _JS, "returnByValue": True})
                finally: c.close()
                d = json.loads(r["result"]["value"]); last = d["u"]
                blob = d["t"] + "\n" + "\n".join(d["v"])
                m = re.search(r"rtmps?://[^\s\"']+", blob)
                if not m: continue
                url = m.group(0)
                key = None
                for v in d["v"]:
                    if " " not in v and len(v) >= 16 and not v.startswith(("rtmp", "http")): key = v; break
                if not key:
                    k = re.search(r"(?i)stream[ _]?key\s*[:：]?\s*([A-Za-z0-9_\-?=&.]{16,})", blob)
                    key = k.group(1) if k else None
                if key: return {"ok": True, "url": url, "key": key}
        except Exception:
            pass
        time.sleep(3)
    return {"ok": False, "error": "stream_info_not_found", "last_page": last,
            "hint": "Account may lack LIVE access, or the page needs a manual step. Use --url/--key instead."}

# ---------------- ffmpeg live ----------------
def _run(cmd, timeout=15):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout + "\n" + p.stderr)
    except Exception as e:
        return 999, str(e)

def pick_encoder(ffmpeg, want="auto"):
    if want not in ("auto", None): return want
    _, out = _run([ffmpeg, "-hide_banner", "-encoders"], 10)
    for e in ("h264_nvenc", "h264_qsv", "h264_amf"):
        if e in out: return e
    return "libx264"

def build_ffmpeg(ffmpeg, cfg, target):
    v, a, e = cfg["video"], cfg.get("audio", {}), cfg["encoder"]
    cam, mic = cfg.get("camera"), cfg.get("microphone")
    if not cam: raise ValueError("no camera selected (camera select <id>)")
    w, h, fps = v["width"], v["height"], v["fps"]
    br = str(e.get("bitrate", "2500k")); num = int(re.sub(r"\D", "", br) or 2500)
    enc = pick_encoder(ffmpeg, e.get("name", "auto"))
    cmd = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "warning", "-progress", str(PROGRESS),
           "-f", "dshow", "-video_size", v.get("capture", "1280x720"), "-framerate", str(fps), "-i", f"video={cam['name']}"]
    if mic: cmd += ["-f", "dshow", "-i", f"audio={mic['name']}"]
    cmd += ["-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},format=yuv420p", "-r", str(fps), "-c:v", enc]
    if enc == "h264_nvenc": cmd += ["-preset", "p4", "-tune", "ll", "-rc", "cbr"]
    elif enc == "h264_qsv": cmd += ["-preset", "veryfast"]
    elif enc == "h264_amf": cmd += ["-usage", "lowlatency"]
    else: cmd += ["-preset", "veryfast", "-tune", "zerolatency"]
    cmd += ["-b:v", br, "-maxrate", br, "-bufsize", f"{num*2}k", "-g", str(fps * 2)]
    if mic:
        vol = float(a.get("volume", 1.0))
        cmd += ["-af", f"volume={vol}", "-c:a", "aac", "-b:a", "128k", "-ar", str(a.get("sample_rate", 48000)), "-ac", "2"]
    else:
        cmd += ["-an"]
    cmd += ["-f", "flv", target]
    return cmd, enc

def _alive(pid):
    rc, out = _run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], 5)
    return str(pid) in out

def _mask(cmd): return [("***KEY***" if i == len(cmd) - 1 else c) for i, c in enumerate(cmd)]

def live_start(ffmpeg, cfg, url, key, dry_run=False):
    st = live_state()
    if st.get("running"): return {"ok": False, "error": "already_running", **st}
    target = url.rstrip("/") + "/" + key
    cmd, enc = build_ffmpeg(ffmpeg, cfg, target)
    if dry_run: return {"ok": True, "dry_run": True, "encoder": enc, "command": _mask(cmd)}
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PROGRESS.unlink(missing_ok=True)
    logf = open(FFLOG, "ab")
    flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_PROCESS_GROUP | NO_WINDOW
    p = subprocess.Popen(cmd, stdout=logf, stderr=logf, stdin=subprocess.DEVNULL, creationflags=flags)
    time.sleep(3)
    if p.poll() is not None:
        tail = FFLOG.read_text(errors="replace")[-1500:]
        return {"ok": False, "error": "ffmpeg_exited", "returncode": p.returncode, "log_tail": tail.replace(key, "***KEY***")}
    STATE.write_text(json.dumps({"pid": p.pid, "started": int(time.time()), "encoder": enc, "server": url}))
    return {"ok": True, "pid": p.pid, "encoder": enc, "server": url}

def live_stop():
    st = live_state()
    if not st.get("pid"): return {"ok": True, "note": "not running"}
    _run(["taskkill", "/PID", str(st["pid"]), "/T", "/F"], 10)
    STATE.unlink(missing_ok=True)
    return {"ok": True, "stopped_pid": st["pid"]}

def _progress():
    try: lines = PROGRESS.read_text(errors="replace").splitlines()[-40:]
    except Exception: return {}
    d = {}
    for l in lines:
        if "=" in l:
            k, v = l.split("=", 1); d[k.strip()] = v.strip()
    return d

def live_state():
    try: st = json.loads(STATE.read_text())
    except Exception: return {"running": False}
    st["running"] = _alive(st["pid"])
    if st["running"]: st["uptime_s"] = int(time.time() - st["started"])
    return st

def live_health():
    st = live_state()
    if not st.get("running"): return {"ok": False, "state": "stopped", **st}
    p = _progress()
    sp = float(p.get("speed", "0x").rstrip("x") or 0) if p.get("speed", "N/A") != "N/A" else 0.0
    frames, drop = int(p.get("frame", 0) or 0), int(p.get("drop_frames", 0) or 0)
    drop_pct = round(100 * drop / frames, 2) if frames else 0.0
    status = "good" if sp >= 0.97 and drop_pct < 2 else ("degraded" if sp >= 0.85 else "bad")
    return {"ok": status == "good", "state": status, "uptime_s": st["uptime_s"], "encoder": st["encoder"],
            "fps": p.get("fps"), "bitrate": p.get("bitrate"), "speed": p.get("speed"),
            "dropped_frames": drop, "drop_pct": drop_pct}
