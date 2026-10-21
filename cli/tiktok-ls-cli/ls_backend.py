import json, os, subprocess, time
from pathlib import Path

def _resolve_studio_exe():
    """Find TikTok LIVE Studio instead of trusting one hard-coded version path.
    Order: env override -> remembered path -> running process -> every fixed drive
    (Program Files / versioned dirs, newest first) -> registry -> old default."""
    import glob, string
    name = "TikTok LIVE Studio.exe"
    remembered = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TikTokLSCLI" / "studio_exe.txt"
    cands = []
    for var in ("PAOKRA_TIKTOK_STUDIO_EXE", "TIKTOKLS_STUDIO_EXE"):
        v = os.environ.get(var, "").strip().strip('"')
        if v:
            cands.append(Path(v))
    try:
        v = remembered.read_text(encoding="utf-8").strip()
        if v:
            cands.append(Path(v))
    except Exception:
        pass
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
            "(Get-Process -Name 'TikTok LIVE Studio' -ErrorAction SilentlyContinue | Select-Object -First 1).Path"],
            capture_output=True, text=True, timeout=6, creationflags=0x08000000)
        p = r.stdout.strip()
        if p:
            cands.append(Path(p))
    except Exception:
        pass
    def newest(base):
        out = glob.glob(str(Path(base) / "*" / name))
        out.sort(key=lambda x: [int(t) if t.isdigit() else 0 for t in Path(x).parent.name.replace("-", ".").split(".")], reverse=True)
        return [Path(x) for x in out]
    roots = []
    for drv in string.ascii_uppercase:
        if os.path.exists(drv + ":\\"):
            roots += [drv + ":\\Program Files\\TikTok LIVE Studio", drv + ":\\Program Files (x86)\\TikTok LIVE Studio",
                      drv + ":\\TikTok LIVE Studio"]
    for env in ("LOCALAPPDATA", "APPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        if os.environ.get(env):
            roots.append(os.path.join(os.environ[env], "TikTok LIVE Studio"))
            roots.append(os.path.join(os.environ[env], "Programs", "TikTok LIVE Studio"))
    for b in roots:
        cands.append(Path(b) / name)
        cands += newest(b)
    try:
        import winreg
        for hive, key in ((winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall")):
            try:
                with winreg.OpenKey(hive, key) as k:
                    for i in range(winreg.QueryInfoKey(k)[0]):
                        try:
                            with winreg.OpenKey(k, winreg.EnumKey(k, i)) as sk:
                                if "tiktok live studio" not in str(winreg.QueryValueEx(sk, "DisplayName")[0]).lower():
                                    continue
                                for vn in ("DisplayIcon", "InstallLocation"):
                                    try:
                                        raw = str(winreg.QueryValueEx(sk, vn)[0]).split(",")[0].strip().strip('"')
                                    except OSError:
                                        continue
                                    p = Path(raw)
                                    cands.append(p if p.suffix.lower() == ".exe" else p / name)
                        except OSError:
                            continue
            except OSError:
                continue
    except Exception:
        pass
    for c in cands:
        try:
            if c.is_file() and c.name.lower() == name.lower():
                try:
                    remembered.parent.mkdir(parents=True, exist_ok=True)
                    remembered.write_text(str(c), encoding="utf-8")
                except Exception:
                    pass
                return c
        except Exception:
            pass
    return Path(r"C:\Program Files\TikTok LIVE Studio\1.36.6\TikTok LIVE Studio.exe")

STUDIO_EXE = _resolve_studio_exe()
STATE_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TikTokLSCLI"
STATE_FILE = STATE_DIR / "studio_backend.json"
CONFIG_FILE = STATE_DIR / "config.json"

# ── PowerShell: show/hide the LIVE Studio window by pid list ─────────────────
_PS_SHOW_HIDE = r"""
$Action=$env:TTLS_ACTION   # 'show' or 'hide'
Add-Type @'
using System;using System.Runtime.InteropServices;
public class WinVis{
  [DllImport("user32.dll")]public static extern bool ShowWindow(IntPtr h,int n);
  [DllImport("user32.dll")]public static extern bool IsWindowVisible(IntPtr h);
}
'@
$sw = if($Action -eq 'show'){5}else{0}   # SW_SHOW=5, SW_HIDE=0
$pids = @($env:TTLS_PIDS -split ',') | ForEach-Object { [int]$_ }
$changed=0
foreach($procId in $pids){
  try{
    $proc=[System.Diagnostics.Process]::GetProcessById($procId)
    if($proc.MainWindowHandle -ne [IntPtr]::Zero){
      [WinVis]::ShowWindow($proc.MainWindowHandle,$sw) | Out-Null; $changed++ }
  }catch{}
}
Write-Output "{""ok"":true,""action"":""$Action"",""changed"":$changed}"
"""

def _load_launch_config():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"studio": {"launch_mode": "ui", "args": []}}

def _save(data):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

def _pids():
    try:
        p = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq TikTok LIVE Studio.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5, encoding="utf-8", errors="replace")
        out = []
        for line in p.stdout.splitlines():
            if "TikTok LIVE Studio.exe" in line:
                parts = [x.strip('"') for x in line.split('","')]
                if len(parts) >= 2:
                    try: out.append(int(parts[1]))
                    except ValueError: pass
        return out
    except Exception:
        return []

def _run_show_hide(action, pids):
    """Show or hide LIVE Studio windows. action = 'show' | 'hide'."""
    import base64
    enc = base64.b64encode(_PS_SHOW_HIDE.encode("utf-16-le")).decode()
    env = dict(os.environ, TTLS_PIDS=",".join(str(p) for p in pids), TTLS_ACTION=action)
    try:
        p = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc],
            capture_output=True, text=True, timeout=10,
            env=env, encoding="utf-8", errors="replace")
        lines = (p.stdout or "").strip().splitlines()
        return json.loads(lines[-1])
    except Exception as e:
        return {"ok": False, "error": str(e)}

def status():
    pids = _pids()
    cfg = _load_launch_config().get("studio", {}) or {}
    mode = str(cfg.get("launch_mode", "ui")).lower()
    return {
        "backend": "tiktok-live-studio",
        "installed": STUDIO_EXE.exists(),
        "running": bool(pids),
        "pids": pids,
        "launch_mode": mode,
        "state_file": str(STATE_FILE),
        "control_mode": "external-wrapper",
        "ui_mode": "LIVE Studio UI" if mode != "headless" else "headless (window hidden)",
        "auth_owner": "LIVE Studio",
        "note": "Wrapper launches the normal LIVE Studio application. Login/session remain inside the app; no credentials or cookies are read.",
        "live_state": "unknown",
    }

def start():
    if not STUDIO_EXE.exists():
        return {"ok": False, "error": "TikTok LIVE Studio executable not found", "path": str(STUDIO_EXE)}
    pids = _pids()
    if pids:
        return {"ok": True, **status(), "action": "already_running"}
    cfg = _load_launch_config().get("studio", {}) or {}
    mode = str(cfg.get("launch_mode", "ui")).lower()
    mode = "headless" if mode == "headless" else "ui"
    # NOTE: the app's own --headless flag does not render the studio UI, so headless = hidden window.
    port = int(cfg.get("debug_port", 9223) or 0)
    extra_args = (["--remote-debugging-port=%d" % port,
                   "--remote-allow-origins=http://127.0.0.1:%d" % port] if port else [])
    cmd = [str(STUDIO_EXE)] + [str(x) for x in extra_args]
    proc = subprocess.Popen(cmd, cwd=str(STUDIO_EXE.parent),
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    time.sleep(1)
    _save({"pid": proc.pid, "started_at": time.time(),
           "exe": str(STUDIO_EXE), "launch_mode": mode})
    if mode == "headless":  # hide the window as soon as it exists
        for _ in range(20):
            time.sleep(1)
            if _run_show_hide("hide", _pids()).get("changed", 0) > 0: break
    s = status()
    s.update({"ok": True, "action": "started", "pid": proc.pid,
              "launch_mode": mode, "launch_args": extra_args})
    return s

def stop():
    pids = _pids()
    if not pids:
        return {"ok": True, **status(), "action": "not_running"}
    subprocess.run(["taskkill", "/IM", "TikTok LIVE Studio.exe", "/T", "/F"],
                   capture_output=True, text=True, timeout=10)
    return {"ok": True, **status(), "action": "stop_requested"}

def show_window():
    """Temporarily show LIVE Studio window (headless → ui click → hide again)."""
    pids = _pids()
    if not pids:
        return {"ok": False, "error": "studio_not_running"}
    return _run_show_hide("show", pids)

def hide_window():
    """Re-hide LIVE Studio window after headless click."""
    pids = _pids()
    if not pids:
        return {"ok": False, "error": "studio_not_running"}
    return _run_show_hide("hide", pids)

def capability():
    return {
        "backend": "tiktok-live-studio",
        "installed": STUDIO_EXE.exists(),
        "wrapper": ["status", "start", "stop", "show_window", "hide_window"],
        "device_pipeline": ["camera", "microphone", "PAOKRA virtual camera"],
        "auth": "owned_by_LIVE_Studio",
        "golive": "UIAutomation + coordinate click (ui mode) / show-click-hide (headless mode)",
        "ui": "normal LIVE Studio window (or hidden when headless)",
        "security": "No credential/cookie extraction and no executable patching",
    }
