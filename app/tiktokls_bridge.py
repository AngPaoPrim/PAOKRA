from __future__ import annotations
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# tiktokls.exe lives inside the paokra project (cli/) so the installer bundles it.
# Override with env var TIKTOKLS_CLI if needed.
import sys

def _find_cli() -> Path:
    cands = []
    env = os.environ.get("TIKTOKLS_CLI")
    if env:
        cands.append(Path(env))
    if getattr(sys, "frozen", False):
        # Installed build: paokra-server.exe sits next to the bundled cli/ folder.
        exe_dir = Path(sys.executable).resolve().parent
        cands += [exe_dir / "cli" / "tiktokls.exe", exe_dir.parent / "cli" / "tiktokls.exe"]
        mei = getattr(sys, "_MEIPASS", None)
        if mei:
            cands.append(Path(mei) / "cli" / "tiktokls.exe")
    cands.append(ROOT / "cli" / "tiktokls.exe")
    for c in cands:
        if c.is_file():
            return c
    return cands[0] if cands else ROOT / "cli" / "tiktokls.exe"

CLI_EXE = _find_cli()

import threading, time, collections

TRACE = collections.deque(maxlen=3000)
_TRACE_LOCK = threading.Lock()
_TRACE_SEQ = [0]

def trace(src: str, level: str, msg) -> int:
    """Append one line to the in-memory verbose log (shown in the UI: Logs > Server + CLI)."""
    with _TRACE_LOCK:
        _TRACE_SEQ[0] += 1
        TRACE.append({"n": _TRACE_SEQ[0], "t": time.time(), "src": src, "lv": level, "m": str(msg)})
        return _TRACE_SEQ[0]

def trace_since(since: int = 0, limit: int = 600):
    with _TRACE_LOCK:
        items = [x for x in TRACE if x["n"] > since]
        last = _TRACE_SEQ[0]
    return items[-limit:], last

def trace_clear():
    with _TRACE_LOCK:
        TRACE.clear()

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)

def _run(*args: str, timeout: int = 20) -> dict:
    if not CLI_EXE.is_file():
        trace("bridge", "ERROR", f"tiktokls.exe not found: {CLI_EXE}")
        return {"ok": False, "error": "tiktokls.exe not found", "path": str(CLI_EXE)}
    trace("bridge", "INFO", "$ tiktokls " + " ".join(args) + f"   (timeout {timeout}s)")
    trace("bridge", "DEBUG", f"exe={CLI_EXE} cwd={CLI_EXE.parent}")
    t0 = time.time()
    env = dict(os.environ); env["PYTHONUNBUFFERED"] = "1"; env["PYTHONIOENCODING"] = "utf-8"
    try:
        p = subprocess.Popen([str(CLI_EXE), *args], cwd=str(CLI_EXE.parent),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             encoding="utf-8", errors="replace", env=env, creationflags=_NOWIN)
    except Exception as e:
        trace("bridge", "ERROR", f"spawn failed: {type(e).__name__}: {e}")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    trace("bridge", "INFO", f"spawned pid={p.pid}")
    out_l, err_l = [], []
    def pump(stream, bucket, lv):
        try:
            for line in iter(stream.readline, ""):
                line = line.rstrip("\r\n"); bucket.append(line)
                if line.strip(): trace("cli", lv, line)
        except Exception:
            pass
    ths = [threading.Thread(target=pump, args=(p.stdout, out_l, "OUT"), daemon=True),
           threading.Thread(target=pump, args=(p.stderr, err_l, "ERR"), daemon=True)]
    for th in ths: th.start()
    timed_out = False; hb = t0
    while True:
        try:
            p.wait(timeout=1); break
        except subprocess.TimeoutExpired:
            el = time.time() - t0
            if el >= timeout:
                timed_out = True
                trace("bridge", "WARN", f"timeout {timeout}s -> killing process tree pid={p.pid}")
                try: subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, creationflags=_NOWIN)
                except Exception: pass
                try: p.kill()
                except Exception: pass
                break
            if time.time() - hb >= 5:
                trace("bridge", "DEBUG", f"... CLI still running {int(el)}s/{timeout}s (pid={p.pid})"); hb = time.time()
    for th in ths: th.join(timeout=2)
    el = time.time() - t0
    raw = "\n".join(out_l).strip(); err = "\n".join(err_l).strip()
    if timed_out:
        trace("bridge", "ERROR", f"TIMEOUT after {el:.1f}s")
        return {"ok": False, "error": f"tiktokls timed out after {timeout}s", "args": list(args),
                "stdout": raw[-600:], "stderr": err[-600:]}
    trace("bridge", "INFO" if p.returncode == 0 else "ERROR", f"exit rc={p.returncode} in {el:.1f}s")
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {"stdout": raw}
    if not isinstance(data, dict):
        data = {"result": data}
    data.setdefault("returncode", p.returncode)
    if p.returncode != 0:
        data.setdefault("ok", False)
        data.setdefault("stderr", err)
    return data

# ── setup mode ────────────────────────────────────────────────────────────────

def setup_headless() -> dict:
    """Configure tiktokls to run LIVE Studio hidden (CDP headless).
    Call this once before starting broadcasts. Safe to call every time.
    """
    return _run("setup", "--headless", "--json")

def setup_ui() -> dict:
    """Configure tiktokls to show the LIVE Studio window (UI mode)."""
    return _run("setup", "--ui", "--json")

def get_setup() -> dict:
    """Return current tiktokls config (includes studio.launch_mode)."""
    return _run("config", "show", "--json")

# ── studio ────────────────────────────────────────────────────────────────────

def version() -> dict:
    return _run("--version", "--json")

def _normalize_mode(mode=None) -> str:
    value=str(mode or os.environ.get("PAOKRA_TIKTOK_STUDIO_MODE","headless") or "headless").strip().lower()
    return "ui" if value in ("ui","visible","normal") else "headless"

def studio_start(mode=None) -> dict:
    chosen=_normalize_mode(mode)
    configured=_run("setup","--ui" if chosen=="ui" else "--headless","--json")
    if configured.get("ok") is False or configured.get("returncode",0) not in (0,None):
        return {"ok":False,"mode":chosen,"error":"could not configure LIVE Studio mode","setup":configured}
    result=_run("studio","start","--json",timeout=90);result.setdefault("mode",chosen);return result

def studio_stop() -> dict:
    return _run("studio", "stop", "--json", timeout=40)

def studio_status() -> dict:
    return _run("studio", "status", "--json")

def studio_capabilities() -> dict:
    return _run("studio", "capabilities", "--json")

def studio_show() -> dict:
    """Temporarily show the LIVE Studio window (for settings / manual action)."""
    return _run("studio", "show", "--json")

def studio_hide() -> dict:
    """Hide the LIVE Studio window again after manual action."""
    return _run("studio", "hide", "--json")

# ── live ──────────────────────────────────────────────────────────────────────

def live_status() -> dict:
    return _run("live", "status", "--json", timeout=30)

def live_health() -> dict:
    return _run("live", "health", "--json")

def live_start(mode=None) -> dict:
    # Calling this starts a real public broadcast; the caller must request it explicitly.
    return _run("live","start","--confirm","--mode",_normalize_mode(mode),"--json",timeout=220)

def live_stop(mode=None) -> dict:
    return _run("live","stop","--mode",_normalize_mode(mode),"--json",timeout=90)

# ── stream (title / topic) ────────────────────────────────────────────────────

def stream_info() -> dict:
    return _run("stream", "info", "--json")

def stream_set_title(title: str, topic: str | None = None) -> dict:
    args = ["stream", "set-title", title, "--json"]
    if topic:
        args += ["--topic", topic]
    return _run(*args)

def stream_go(title: str, topic: str | None = None, timeout: int = 40) -> dict:
    """Set title + go LIVE in one call (headless CDP, no mouse/keyboard)."""
    args = ["stream", "go", title, "--confirm", "--mode", "headless",
            "--timeout", str(timeout), "--json"]
    if topic:
        args += ["--topic", topic]
    return _run(*args, timeout=timeout + 10)

# ── high-level helpers used by server.py ──────────────────────────────────────

def setup(mode=None) -> dict:
    """Configure the requested UI/headless mode and inspect Studio without broadcasting."""
    chosen = _normalize_mode(mode)
    # Each CLI call is a ~1-6 s PyInstaller start-up; run the independent
    # read-only ones in parallel instead of one after another.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as ex:
        f_cfg = ex.submit(setup_ui if chosen == "ui" else setup_headless)
        f_status = ex.submit(studio_status)
        f_health = ex.submit(live_health)
        f_ver = ex.submit(version)
        cfg, status, health, ver = f_cfg.result(), f_status.result(), f_health.result(), f_ver.result()
    return {
        "ok": bool(cfg.get("ok", False) and status.get("installed", False)),
        "cli": str(CLI_EXE),
        "version": ver,
        "headless_config": cfg,
        "studio_start": None,
        "studio_status": status,
        "setup_message": f"{chosen} configuration applied; Studio start is deferred until explicitly needed.",
        "live_health": health,
        "mode": chosen,
        "note": "LIVE Studio window follows the selected mode.",
    }

def prepare_live() -> dict:
    status = studio_status()
    health = live_health()
    return {
        "ok": bool(status.get("installed") and status.get("running")),
        "studio_status": status,
        "live_health": health,
        "ready_for_manual_start": bool(status.get("running")),
        "mode": "headless",
        "note": "Studio and local control path are prepared; broadcast starts only on explicit live_start().",
    }
