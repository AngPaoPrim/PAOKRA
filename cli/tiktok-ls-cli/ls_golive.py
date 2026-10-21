"""ls_golive v3: press Go LIVE / End LIVE in TikTok LIVE Studio through Chromium's standard remote-debugging
channel (the app is an Electron app). Trusted input events are sent straight into the app's own page, so no OS
mouse/keyboard is used. Only buttons a user could click by hand are pressed; login, eligibility and content rules
stay enforced by LIVE Studio. No private endpoints, IPC messages or credentials are touched.

Modes: ui       window stays visible
       headless window is hidden by the wrapper; shown only while acting, hidden again afterwards
       (the app's own --headless flag does NOT render the studio UI, so it is not used)"""
import base64, json, os, re, socket, struct, time, urllib.request

DEFAULT_UI = {
    "debug_port": 9223,
    "go_live_names": ["เริ่ม LIVE", "Go LIVE", "Go Live", "Start LIVE"],
    "end_live_names": ["สิ้นสุด LIVE", "จบ LIVE", "End LIVE", "End Live"],
    "confirm_names": ["สิ้นสุดตอนนี้", "สิ้นสุด", "ยืนยัน", "End now", "End Now", "End", "Confirm", "OK"],
    "timer_regex": r"^\d{1,2}:\d\d:\d\d$",
}

# ---------------- minimal websocket + CDP (stdlib) ----------------
class _WS:
    def __init__(s, url):
        host, port, path = re.match(r"ws://([^:/]+):(\d+)(/.*)", url).groups()
        s.sock = socket.create_connection((host, int(port)), timeout=15)
        key = base64.b64encode(os.urandom(16)).decode()
        s.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
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

class _CDP:
    def __init__(s, url): s.ws = _WS(url); s.i = 0
    def call(s, method, params=None):
        s.i += 1
        s.ws.send(json.dumps({"id": s.i, "method": method, "params": params or {}}))
        while True:
            m = json.loads(s.ws.recv())
            if m.get("id") == s.i:
                if "error" in m: raise RuntimeError(m["error"])
                return m.get("result", {})
    def eval(s, expr):
        return s.call("Runtime.evaluate", {"expression": expr, "returnByValue": True}).get("result", {}).get("value")
    def click(s, x, y):
        for ty in ("mouseMoved", "mousePressed", "mouseReleased"):
            s.call("Input.dispatchMouseEvent", {"type": ty, "x": x, "y": y, "button": "left", "clickCount": 1})
    def close(s): s.ws.close()

# ---------------- helpers ----------------
def _ui(cfg):
    u = dict(DEFAULT_UI)
    u.update((cfg.get("studio", {}) or {}).get("ui", {}) or {})
    port = (cfg.get("studio", {}) or {}).get("debug_port")
    if port: u["debug_port"] = port
    return u

def _targets(u):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{u['debug_port']}/json/list", timeout=2) as r:
            return [t for t in json.loads(r.read().decode()) if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    except Exception:
        return None

def _page(u, key):
    for t in _targets(u) or []:
        if key in t.get("url", ""): return t
    return None

_JS_BTNS = """((names,rx)=>{const R=rx?new RegExp(rx):null;
return JSON.stringify([...document.querySelectorAll('button,[role=button],[class*=utton],[class*=btn]')]
 .filter(e=>(e.offsetWidth||e.offsetHeight)&&!e.disabled&&e.getAttribute('aria-disabled')!=='true')
 .map(e=>{const t=(e.innerText||e.getAttribute('aria-label')||'').trim().replace(/\\s+/g,' ');const b=e.getBoundingClientRect();
   return {t:t.slice(0,40),x:b.x+b.width/2,y:b.y+b.height/2,w:b.width,h:b.height}})
 .filter(o=>o.t&&o.w>0&&o.h>0&&(names===null||names.includes(o.t)||(R&&R.test(o.t)))))})(%s,%s)"""

def _buttons(t, names=None, rx=None):
    c = _CDP(t["webSocketDebuggerUrl"])
    try:
        v = c.eval(_JS_BTNS % (json.dumps(names, ensure_ascii=False), json.dumps(rx)))
        return json.loads(v) if v else []
    finally:
        c.close()

def _click_best(t, btns):
    if not btns: return None
    b = max(btns, key=lambda o: o["w"] * o["h"])
    c = _CDP(t["webSocketDebuggerUrl"])
    try: c.click(b["x"], b["y"])
    finally: c.close()
    return b["t"]

def _all_pages(u): return _targets(u) or []

# ---------------- state ----------------
def live_state(cfg):
    u = _ui(cfg)
    if _targets(u) is None: return "unknown_no_debug_port"
    main = _page(u, "main/index")
    if not main: return "unknown"
    try:
        if _buttons(main, [], u["timer_regex"]): return "live"
        if _buttons(main, u["go_live_names"]): return "idle"
    except Exception:
        return "unknown"
    return "unknown"

def ui_dump(cfg):
    u = _ui(cfg)
    ts = _targets(u)
    if ts is None:
        return {"ok": False, "error": "no_debug_port", "hint": f"Start LIVE Studio via `studio start` (it opens debug port {u['debug_port']})."}
    pages = []
    for t in ts:
        try:
            pages.append({"page": t["url"].split("html/")[-1][:40], "buttons": [b["t"] for b in _buttons(t, None)][:40]})
        except Exception as e:
            pages.append({"page": t["url"][-40:], "error": str(e)[:80]})
    return {"ok": True, "debug_port": u["debug_port"], "pages": pages}

# ---------------- lifecycle ----------------
def _wait_ready(u, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        main = _page(u, "main/index")
        if main:
            try:
                if len(_buttons(main, None)) >= 3: return True
            except Exception: pass
        time.sleep(2)
    return False

def _ensure(cfg, backend, mode, restart=False):
    u = _ui(cfg)
    if _targets(u) is None:
        if backend.status().get("running"):
            if not restart:
                return {"ok": False, "error": "restart_required",
                        "hint": "LIVE Studio is running without the debug port. Re-run with --restart (this closes the app; do not use while live)."}
            backend.stop(); time.sleep(3)
        r = backend.start()
        if not r.get("ok"): return r
    if not _wait_ready(u):
        return {"ok": False, "error": "studio_not_ready", "hint": "Main page did not finish loading. Make sure you are logged in inside LIVE Studio."}
    if mode == "headless" and hasattr(backend, "show_window"): backend.show_window(); time.sleep(0.8)
    return {"ok": True}

def _finish(backend, mode):
    if mode == "headless" and hasattr(backend, "hide_window"):
        try: backend.hide_window()
        except Exception: pass

def resolve_mode(cfg, mode=None):
    if mode in ("ui", "hidden", "headless"): return "headless" if mode in ("hidden", "headless") else "ui"
    return "headless" if (cfg.get("studio", {}) or {}).get("launch_mode") == "headless" else "ui"

# ---------------- actions ----------------
def go_live(cfg, backend, confirm=False, timeout=40, mode=None, restart=False):
    if not confirm:
        return {"ok": False, "error": "confirm_required",
                "note": "Go LIVE starts a real public broadcast on the logged-in account. Re-run with --confirm."}
    mode = resolve_mode(cfg, mode); u = _ui(cfg)
    try:
        e = _ensure(cfg, backend, mode, restart)
        if not e.get("ok"): return {**e, "mode": mode}
        if live_state(cfg) == "live": return {"ok": True, "state": "live", "action": "already_live", "mode": mode}
        end = time.time() + timeout; clicks = []
        while time.time() < end and len(clicks) < 5:
            main = _page(u, "main/index")
            b = _buttons(main, u["go_live_names"]) if main else []
            if not b:
                time.sleep(1.5); continue
            clicks.append(_click_best(main, b))
            for _ in range(6):  # wait up to ~8s for this click to take effect
                time.sleep(1.4)
                if live_state(cfg) == "live":
                    return {"ok": True, "state": "live", "clicks": len(clicks), "mode": mode}
        return {"ok": False, "state": live_state(cfg), "clicks": len(clicks), "mode": mode,
                "hint": "Go LIVE not confirmed (eligibility, login, or a dialog in LIVE Studio?). Run `studio ui-dump` and check names in config studio.ui."}
    finally:
        _finish(backend, mode)

def end_live(cfg, backend, timeout=20, mode=None, restart=False):
    mode = resolve_mode(cfg, mode); u = _ui(cfg)
    st = live_state(cfg)
    if st == "unknown_no_debug_port": return {"ok": False, "error": "no_debug_port", "mode": mode}
    if st != "live": return {"ok": True, "state": st, "action": "not_live", "mode": mode}
    try:
        if mode == "headless" and hasattr(backend, "show_window"): backend.show_window(); time.sleep(0.8)
        main = _page(u, "main/index")
        b = _buttons(main, u["end_live_names"], u["timer_regex"])
        clicked = _click_best(main, b)
        end = time.time() + timeout; seen = []
        while time.time() < end:
            time.sleep(1.5)
            if live_state(cfg) != "live": return {"ok": True, "state": live_state(cfg), "clicked": clicked, "mode": mode}
            for t in _all_pages(u):  # confirmation dialog may be in any page
                try:
                    cb = _buttons(t, u["confirm_names"])
                    if cb and t is not None:
                        _click_best(t, cb); seen.append(cb[0]["t"]); break
                except Exception: pass
        dump = ui_dump(cfg)
        return {"ok": False, "state": live_state(cfg), "clicked": clicked, "confirm_clicked": seen, "mode": mode,
                "hint": "End not confirmed; set studio.ui.end_live_names/confirm_names from this dump.", "ui": dump.get("pages")}
    finally:
        _finish(backend, mode)
