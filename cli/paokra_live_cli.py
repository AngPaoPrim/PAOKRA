import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def ffmpeg_path():
    roots = [ROOT]
    if getattr(sys, "frozen", False):
        roots += [Path(sys.executable).resolve().parents[2], Path(sys.executable).resolve().parent]
    candidates = []
    for root in roots:
        candidates += [
            root / "build" / "runtime" / "ms-playwright" / "ffmpeg-1011" / "ffmpeg-win64.exe",
            root / "dist" / "win-unpacked" / "resources" / "paokra" / "ms-playwright" / "ffmpeg-1011" / "ffmpeg-win64.exe",
        ]
    for p in candidates:
        if p.exists():
            return p
    return Path("ffmpeg.exe")

def build_push_args(src, url, key, width, height, fps, bitrate, audio, loop):
    target = url.rstrip("/") + "/" + key.lstrip("/") if key else url
    args = [str(ffmpeg_path()), "-hide_banner", "-loglevel", "info"]
    if loop:
        args += ["-stream_loop", "-1"]
    args += ["-re", "-i", str(src)]
    vf = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,format=yuv420p"
    args += ["-vf", vf, "-r", str(fps)]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-b:v", bitrate, "-maxrate", bitrate, "-bufsize", bitrate]
    args += ["-g", str(fps * 2), "-keyint_min", str(fps * 2), "-sc_threshold", "0"]
    if audio:
        args += ["-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2"]
    else:
        args += ["-an"]
    args += ["-f", "flv", target]
    return args

def main():
    ap = argparse.ArgumentParser(prog="paokra-live", description="PAOKRA low-overhead live push CLI")
    ap.add_argument("--source", required=True, help="video/media source")
    ap.add_argument("--url", required=True, help="RTMP/RTMPS publish server URL")
    ap.add_argument("--key", default="", help="publish key/token supplied by the authorized live service")
    ap.add_argument("--width", type=int, default=720)
    ap.add_argument("--height", type=int, default=1280)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--bitrate", default="2500k")
    ap.add_argument("--no-audio", action="store_true")
    ap.add_argument("--no-loop", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src = Path(args.source).expanduser().resolve()
    if not src.exists():
        print(f"ERROR: source not found: {src}", file=sys.stderr)
        return 2
    cmd = build_push_args(src, args.url, args.key, args.width, args.height, args.fps, args.bitrate, not args.no_audio, not args.no_loop)
    print("PAOKRA LIVE CLI")
    print(f"source : {src}")
    print(f"output : {args.width}x{args.height}@{args.fps}")
    print(f"bitrate: {args.bitrate}")
    print(f"ffmpeg : {cmd[0]}")
    if args.dry_run:
        print("DRY RUN")
        print(subprocess.list2cmdline(cmd))
        return 0
    print("Starting publish...")
    try:
        p = subprocess.Popen(cmd, cwd=str(ROOT))
    except FileNotFoundError:
        print("ERROR: ffmpeg.exe not found", file=sys.stderr)
        return 3
    try:
        rc = p.wait()
    except KeyboardInterrupt:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
        rc = 130
    print(f"publisher exited: {rc}")
    return rc

if __name__ == "__main__":
    raise SystemExit(main())
