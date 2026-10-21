import argparse, hashlib, json, os, re, subprocess, sys, time
from pathlib import Path

# ── Force UTF-8 stdout/stderr (ป้องกัน garbled Thai บน Windows cp874) ────────
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

try:
    import ls_backend
except Exception:
    ls_backend = None
try:
    import ls_golive
except Exception:
    ls_golive = None
try:
    import ls_stream
except Exception:
    ls_stream = None

APP = "TikTok-LS-CLI"
VERSION = "0.4.0-cdp-stream"
CONFIG_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TikTokLSCLI"
CONFIG = CONFIG_DIR / "config.json"
LOG = CONFIG_DIR / "cli.log"

def exe(name):
    if getattr(sys, "frozen", False):
        p = Path(sys.executable).resolve().parent / name
        if p.exists(): return str(p)
    return name

def run(cmd, timeout=15):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout + "\n" + p.stderr).strip()
    except Exception as e:
        return 999, str(e)

def ensure_config():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG.exists():
        save_config({"version": 1, "camera": None, "microphone": None,
                     "video": {"width":720,"height":1280,"fps":30},
                     "audio":{"sample_rate":48000,"channels":2},
                     "encoder":{"name":"auto","bitrate":"2500k"}})

def load_config():
    ensure_config()
    try: return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception: return {}

def save_config(c):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(c, indent=2, ensure_ascii=False), encoding="utf-8")

def log(msg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f: f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")

def device_id(kind, alt):
    return kind + "-" + hashlib.sha256(alt.encode("utf-8", "ignore")).hexdigest()[:16]

def parse_dshow_devices(text):
    out=[]; current=None
    for line in text.splitlines():
        m=re.search(r'"([^"]+)" \((audio)\)', line)
        v=re.search(r'"([^"]+)" \((none)\)', line)
        if m or v:
            name=(m or v).group(1); kind="microphone" if m else "camera"
            current={"id":None,"type":kind,"name":name,"backend":"dshow","alternative_name":None}
            out.append(current)
        elif "Alternative name" in line and current:
            x=re.search(r'"([^"]+)"', line)
            if x:
                current["alternative_name"]=x.group(1)
                current["id"]=device_id(current["type"], x.group(1))
    for d in out:
        if not d["id"]: d["id"]=device_id(d["type"], d["name"])
    return out

def list_devices(kind=None):
    rc,out=run([exe("ffmpeg"),"-hide_banner","-list_devices","true","-f","dshow","-i","dummy"],10)
    ds=parse_dshow_devices(out)
    return [d for d in ds if kind is None or d["type"]==kind]

def print_data(data, as_json=False):
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2)); return
    if isinstance(data, list):
        for d in data: print(f'{d["id"]} | {d["type"]:<10} | {d["name"]}')
    elif isinstance(data, dict):
        print(json.dumps(data, ensure_ascii=False, indent=2))

def resolve_device(kind, ident):
    ds=list_devices(kind)
    for d in ds:
        if ident in (d["id"], d["name"], d.get("alternative_name")): return d
    return None

def capabilities(d):
    target=f'video={d["name"]}' if d["type"]=="camera" else f'audio={d["name"]}'
    rc,out=run([exe("ffmpeg"),"-hide_banner","-list_options","true","-f","dshow","-i",target],10)
    modes=[]
    for line in out.splitlines():
        m=re.search(r'(?:vcodec|pixel_format).*?(?:min s|s)=(\d+)x(\d+).*?(?:max s|s)=(\d+)x(\d+)',line)
        if m: modes.append({"min":[int(m.group(1)),int(m.group(2))],"max":[int(m.group(3)),int(m.group(4))],"raw":line.strip()})
        elif "s=" in line and ("fps=" in line or "fps" in line):
            modes.append({"raw":line.strip()})
    return {"device":d,"probe_returncode":rc,"modes":modes,"raw":out[-12000:]}

def select_device(kind, ident):
    d=resolve_device(kind, ident)
    if not d: return {"ok":False,"error":"device_not_found","available":list_devices(kind)}
    c=load_config(); c["camera" if kind=="camera" else "microphone"]=d; save_config(c)
    log(f"selected {kind} {d['id']} {d['name']}")
    return {"ok":True,"selected":d}

def test_camera(d, seconds=2):
    cmd=[exe("ffmpeg"),"-hide_banner","-loglevel","error","-f","dshow","-video_size","640x480","-i",f'video={d["name"]}',"-t",str(seconds),"-f","null","-"]
    rc,out=run(cmd,seconds+8)
    return {"ok":rc==0,"returncode":rc,"device":d,"message":out or ("camera test passed" if rc==0 else "camera test failed")}

def test_audio(d, seconds=2):
    cmd=[exe("ffmpeg"),"-hide_banner","-loglevel","error","-f","dshow","-i",f'audio={d["name"]}',"-t",str(seconds),"-f","null","-"]
    rc,out=run(cmd,seconds+8)
    return {"ok":rc==0,"returncode":rc,"device":d,"message":out or ("audio test passed" if rc==0 else "audio test failed")}

def main():
    json_anywhere = "--json" in sys.argv[1:]
    if json_anywhere:
        sys.argv = [x for x in sys.argv if x != "--json"]
    ap=argparse.ArgumentParser(prog="tiktokls", description="Lightweight TikTok LIVE Studio control-plane CLI")
    ap.add_argument("--json", action="store_true", dest="json_out")
    ap.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sp=ap.add_subparsers(dest="cmd")

    # ── auth ──────────────────────────────────────────────────────────────────
    auth=sp.add_parser("auth"); authsp=auth.add_subparsers(dest="sub")
    authsp.add_parser("login"); authsp.add_parser("status"); authsp.add_parser("logout")

    # ── devices ───────────────────────────────────────────────────────────────
    dev=sp.add_parser("devices"); dssp=dev.add_subparsers(dest="sub")
    for k in ("camera","microphone"):
        p=dssp.add_parser(k); s=p.add_subparsers(dest="action"); s.add_parser("list")
        x=s.add_parser("info"); x.add_argument("device_id")
        x=s.add_parser("capabilities"); x.add_argument("device_id")
        s.add_parser("status")

    # ── camera / audio ────────────────────────────────────────────────────────
    cam=sp.add_parser("camera"); cs=cam.add_subparsers(dest="action")
    x=cs.add_parser("select"); x.add_argument("device_id")
    x=cs.add_parser("test"); x.add_argument("device_id"); x.add_argument("--seconds",type=int,default=2)

    aud=sp.add_parser("audio"); ass=aud.add_subparsers(dest="action")
    x=ass.add_parser("select"); x.add_argument("device_id")
    x=ass.add_parser("test"); x.add_argument("device_id"); x.add_argument("--seconds",type=int,default=2)

    # ── config ────────────────────────────────────────────────────────────────
    cfg=sp.add_parser("config"); cfgsp=cfg.add_subparsers(dest="sub")
    cfgsp.add_parser("show")
    v=cfgsp.add_parser("video"); v.add_argument("--width",type=int); v.add_argument("--height",type=int); v.add_argument("--fps",type=int)

    # ── diagnostics ───────────────────────────────────────────────────────────
    di=sp.add_parser("diagnostics"); dis=di.add_subparsers(dest="sub")
    dis.add_parser("version"); dis.add_parser("capabilities"); dis.add_parser("logs")

    # ── setup ─────────────────────────────────────────────────────────────────
    setup=sp.add_parser("setup", help="Configure LIVE Studio backend launch mode")
    setup.add_argument("--ui",       action="store_true", help="Show LIVE Studio window (default)")
    setup.add_argument("--headless", action="store_true", help="CDP mode: window hidden, shown only while acting")

    # ── studio ────────────────────────────────────────────────────────────────
    studio=sp.add_parser("studio", help="TikTok LIVE Studio backend bridge")
    ss=studio.add_subparsers(dest="sub")
    ss.add_parser("status"); ss.add_parser("start"); ss.add_parser("stop"); ss.add_parser("capabilities")
    ss.add_parser("ui-dump", help="List all CDP-visible buttons on each LIVE Studio page")
    ss.add_parser("show",    help="Show the LIVE Studio window")
    ss.add_parser("hide",    help="Hide the LIVE Studio window")

    # ── live ──────────────────────────────────────────────────────────────────
    live=sp.add_parser("live", help="Go LIVE / End LIVE via CDP")
    ls=live.add_subparsers(dest="sub")
    ls.add_parser("status"); ls.add_parser("health")
    lst=ls.add_parser("start", help="Press Go LIVE in LIVE Studio")
    lst.add_argument("--confirm",  action="store_true", help="Required — this starts a real public broadcast")
    lst.add_argument("--timeout",  type=int, default=40)
    lst.add_argument("--mode",     choices=["ui","headless","hidden"])
    lst.add_argument("--restart",  action="store_true", help="Restart Studio to open the CDP channel (never while live)")
    lsp=ls.add_parser("stop", help="Press End LIVE in LIVE Studio")
    lsp.add_argument("--mode", choices=["ui","headless","hidden"])

    # ── stream ────────────────────────────────────────────────────────────────
    stream=sp.add_parser("stream", help="Set LIVE title/topic and go live via CDP")
    strsp=stream.add_subparsers(dest="sub")

    # stream info — show current title/topic
    strsp.add_parser("info", help="Show current LIVE title and topic from LIVE Studio")

    # stream set-title — set title (and optionally topic) without going live
    sst=strsp.add_parser("set-title", help="Set LIVE title and topic in LIVE Studio (before going live)")
    sst.add_argument("title",           help="LIVE title text")
    sst.add_argument("--topic",         help="Game/topic name (search and select in LIVE Studio)", default=None)

    # stream go — set title + go live in one command
    sgo=strsp.add_parser("go", help="Set title + go LIVE in one command (CDP, no OS mouse/keyboard)")
    sgo.add_argument("title",           help="LIVE title text")
    sgo.add_argument("--topic",         help="Game/topic name", default=None)
    sgo.add_argument("--confirm",       action="store_true", help="Required — starts a real public broadcast")
    sgo.add_argument("--timeout",       type=int, default=40)
    sgo.add_argument("--mode",          choices=["ui","headless","hidden"])
    sgo.add_argument("--restart",       action="store_true")

    args=ap.parse_args(); ensure_config()
    args.json_out = args.json_out or json_anywhere

    # ── setup ─────────────────────────────────────────────────────────────────
    if args.cmd=="setup":
        c=load_config(); c.setdefault("studio", {})
        if args.ui and args.headless:
            return print_data({"ok":False,"error":"choose_one_of_ui_or_headless"}, args.json_out)
        if args.headless:
            c["studio"].update({"launch_mode":"headless","args":[]})
            note="Headless mode: Studio window hidden. CDP drives everything; window shown briefly only when clicking."
        else:
            c["studio"].update({"launch_mode":"ui","args":[]})
            note="UI mode: Studio window is visible."
        save_config(c)
        return print_data({"ok":True,"studio":c["studio"],"note":note}, args.json_out)

    # ── devices ───────────────────────────────────────────────────────────────
    if args.cmd=="devices":
        kind="microphone" if args.sub=="microphone" else "camera"
        if args.action=="list": return print_data(list_devices(kind), args.json_out)
        if args.action in ("info","capabilities"):
            d=resolve_device(kind,args.device_id); data={"ok":bool(d),"device":d}
            if d and args.action=="capabilities": data=capabilities(d)
            return print_data(data, args.json_out)
        if args.action=="status":
            return print_data({"selected":load_config().get("camera" if kind=="camera" else "microphone"),"available":list_devices(kind)}, args.json_out)

    if args.cmd in ("camera","audio"):
        kind="camera" if args.cmd=="camera" else "microphone"
        if args.action=="select": return print_data(select_device(kind,args.device_id), args.json_out)
        d=resolve_device(kind,args.device_id)
        return print_data({"ok":False,"error":"device_not_found"} if not d else
                          (test_camera(d,args.seconds) if kind=="camera" else test_audio(d,args.seconds)), args.json_out)

    # ── config ────────────────────────────────────────────────────────────────
    if args.cmd=="config":
        c=load_config()
        if args.sub=="show": return print_data(c, args.json_out)
        if args.sub=="video":
            for k in ("width","height","fps"):
                val=getattr(args,k)
                if val is not None: c["video"][k]=val
            save_config(c); return print_data(c, args.json_out)

    # ── diagnostics ───────────────────────────────────────────────────────────
    if args.cmd=="diagnostics":
        if args.sub=="version":
            return print_data({"cli":APP,"version":VERSION,"python":sys.version.split()[0],
                                "ffmpeg":run([exe("ffmpeg"),"-version"],5)[1].splitlines()[0]}, args.json_out)
        if args.sub=="capabilities":
            return print_data({"camera_count":len(list_devices("camera")),
                                "microphone_count":len(list_devices("microphone")),
                                "ffmpeg":exe("ffmpeg"),"json":True}, args.json_out)
        if args.sub=="logs":
            return print_data({"path":str(LOG),"exists":LOG.exists()}, args.json_out)

    # ── auth ──────────────────────────────────────────────────────────────────
    if args.cmd=="auth":
        if args.sub=="login":
            if ls_backend is None: return print_data({"ok":False,"error":"ls_backend_unavailable"}, args.json_out)
            r=ls_backend.start(); r["note"]="Complete login in the official LIVE Studio window."
            return print_data(r, args.json_out)
        if args.sub=="status":
            return print_data({"mode":"LIVE Studio-owned","logged_in":"not_exposed"}, args.json_out)
        if args.sub=="logout":
            return print_data({"ok":False,"error":"logout_owned_by_live_studio",
                                "note":"Sign out in the official LIVE Studio application."}, args.json_out)

    # ── studio ────────────────────────────────────────────────────────────────
    if args.cmd=="studio":
        if ls_backend is None: return print_data({"ok":False,"error":"ls_backend_unavailable"}, args.json_out)
        if args.sub=="status":       return print_data(ls_backend.status(), args.json_out)
        if args.sub=="start":        return print_data(ls_backend.start(), args.json_out)
        if args.sub=="stop":         return print_data(ls_backend.stop(), args.json_out)
        if args.sub=="capabilities": return print_data(ls_backend.capability(), args.json_out)
        if args.sub=="show":         return print_data(ls_backend.show_window(), args.json_out)
        if args.sub=="hide":         return print_data(ls_backend.hide_window(), args.json_out)
        if args.sub=="ui-dump":
            if ls_golive is None: return print_data({"ok":False,"error":"ls_golive_unavailable"}, args.json_out)
            return print_data(ls_golive.ui_dump(load_config()), args.json_out)

    # ── live ──────────────────────────────────────────────────────────────────
    if args.cmd=="live":
        if ls_backend is None or ls_golive is None:
            return print_data({"ok":False,"error":"backend_unavailable"}, args.json_out)
        c=load_config()
        if args.sub=="status":
            st=ls_backend.status()
            st["live_state"]=ls_golive.live_state(c) if st.get("running") else "studio_not_running"
            return print_data(st, args.json_out)
        if args.sub=="health":
            st=ls_backend.status()
            ls_state=ls_golive.live_state(c) if st.get("running") else "studio_not_running"
            return print_data({"ok":ls_state in ("live","unknown"),
                                "live_state":ls_state,
                                "studio_running":st.get("running",False),
                                "launch_mode":st.get("launch_mode","ui"),
                                "pids":st.get("pids")}, args.json_out)
        if args.sub=="start":
            r=ls_golive.go_live(c, ls_backend, confirm=args.confirm,
                                timeout=args.timeout, mode=args.mode, restart=args.restart)
            log(f"live start -> {r.get('ok')} {r.get('state') or r.get('error')}")
            return print_data(r, args.json_out)
        if args.sub=="stop":
            r=ls_golive.end_live(c, ls_backend, mode=args.mode)
            log(f"live stop -> {r.get('ok')} {r.get('state') or r.get('error')}")
            return print_data(r, args.json_out)

    # ── stream ────────────────────────────────────────────────────────────────
    if args.cmd=="stream":
        if ls_stream is None:
            return print_data({"ok":False,"error":"ls_stream_unavailable",
                                "hint":"ls_stream.py missing or failed to import."}, args.json_out)
        if ls_backend is None:
            return print_data({"ok":False,"error":"ls_backend_unavailable"}, args.json_out)
        c=load_config()

        if args.sub=="info":
            return print_data(ls_stream.get_live_info(c), args.json_out)

        if args.sub=="set-title":
            r=ls_stream.set_live_title(c, args.title, topic=args.topic)
            log(f"stream set-title '{args.title}' -> {r.get('ok')} {r.get('error','')}")
            return print_data(r, args.json_out)

        if args.sub=="go":
            r=ls_stream.go_live_with_title(c, ls_backend, args.title,
                                            topic=args.topic,
                                            confirm=args.confirm,
                                            timeout=args.timeout,
                                            mode=args.mode,
                                            restart=args.restart)
            log(f"stream go '{args.title}' -> {r.get('ok')} {r.get('state') or r.get('error','')}")
            return print_data(r, args.json_out)

        # default: show help for stream subcommand
        stream.print_help()
        return

    ap.print_help()

if __name__=="__main__": main()
