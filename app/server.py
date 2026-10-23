from flask import Flask, request, jsonify, Response, send_from_directory, send_file
import atexit
import sys
from pathlib import Path
from collections import deque
import cv2, json, threading, time, urllib.request, uuid, subprocess, os, json as _json, random, queue, urllib.parse, re
from PIL import Image, ImageDraw, ImageFont
from textsprite import sprite as cached_text_sprite, blend as blend_text_sprite
import numpy as np
import shutil
from virtualcam import VirtualCamera, VirtualAudioRouter
from libobs_bridge import LibObsBridge
import accounts as acc_store
import html as _html
import ctypes
import comment_bot
import tiktokls_bridge
from ai_seam import (create_blueprint as _create_ai_seam_blueprint, find_cached_transition,
                     generation_active as _ai_seam_generation_active,
                     validate_transition_matrix as _ai_seam_validate_transition_matrix)

# MediaPipe is optional and must not block PAOKRA startup. Some Windows builds
# can spend a very long time initializing it, so face-mesh stays disabled until
# it is explicitly made available by the runtime.
mp = None

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("PAOKRA_HOME", str(DEFAULT_ROOT))).expanduser().resolve()
DATA = Path(os.environ.get("PAOKRA_DATA", str(ROOT / "data"))).expanduser().resolve()
WEB = Path(os.environ.get("PAOKRA_WEB", str(ROOT / "app" / "web"))).expanduser().resolve()
DATA.mkdir(parents=True, exist_ok=True)
# Keep large multipart video uploads off the system drive.
import tempfile
UPLOAD_TMP = DATA / ".upload_tmp"
UPLOAD_TMP.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(UPLOAD_TMP)

MAX_MEDIA_UPLOAD_BYTES = 64 * 1024 * 1024 * 1024
app = Flask(__name__, static_folder=str(WEB))
lock = threading.Lock()
V2F_JOBS = {}
V2F_JOB_LOCK = threading.Lock()
state = {"source": None, "source_serial": 0, "running": False, "loop": True,
         "text": "", "text_x": 24, "text_y": 56,
         "image": None, "image_x": 24, "image_y": 24,
         "web_url": "", "web_x": 0, "web_y": 0, "web_width": 720, "web_height": 1280,
         "image_width": 220, "image_height": 220, "image_pan_x": 0, "image_pan_y": 0, "fps": 30, "fps_mode": "fixed",
         "image_fit": "contain", "image_zoom": 1.0, "audio_offset_ms": 0,
         "text_size": 28, "texts": [], "legacy_text_added": False,
         "layer_order": [],
         "web_enabled": False,
         "loop_variation_enabled": False, "loop_variation_strength": 4, "loop_count": 0,
         "source_x": 0, "source_y": 0, "source_width": 720, "source_height": 405, "source_aspect": 720/405,
         "source_fit_mode": "cover", "source_flip_h": False, "source_flip_v": False,
         "width": 720, "height": 1280, "accounts": [],
         # Use a conservative default and reserve CPU for the compositor,
         # decoder workers, Electron, and the receiving app. Settings can still
         # raise this explicitly; saved user choices remain respected below.
         "cpu_threads": max(1, min(12, (os.cpu_count() or 1) - 2 if (os.cpu_count() or 1) > 4 else (os.cpu_count() or 1))),
         "decode_backend": "auto", "render_backend": "auto",
         "source_enabled": True, "text_enabled": True, "image_enabled": True,
         "virtualaudio_enabled": True, "virtualaudio_error": None,
         "virtualcam": False, "virtualcam_error": None, "preview_error": None,
         "libobs_active": False, "libobs_error": None, "libobs_version": None,
         "libobs_latency_ms": 0.0, "libobs_frames_in": 0, "libobs_frames_out": 0,
         "video_fx": {"brightness": 0, "contrast": 1.0, "saturation": 1.0,
                      "warmth": 0.0, "sharpen": 0, "vignette": 0,
                      "skin_smooth": 0, "wrinkle_remove": 0, "skin_bright": 0, "lip_tint": 0,
                      "face_slim": 0, "forehead_width": 0, "forehead_height": 0, "midface_width": 0,
                      "cheekbone": 0, "temple": 0, "face_length": 0, "jaw_refine": 0,
                      "jaw_width": 0, "jaw_curve": 0, "jaw_angle": 0,
                      "chin": 0, "chin_height": 0, "chin_roundness": 0, "chin_tip": 0, "eye_size": 0}}
PERF_FILE = DATA / "performance.json"
try:
    _perf = json.loads(PERF_FILE.read_text(encoding="utf-8")) if PERF_FILE.exists() else {}
    for _k in ("cpu_threads", "decode_backend", "render_backend"):
        if _k in _perf: state[_k] = _perf[_k]
except Exception:
    pass
# Old performance profiles may contain GPU render choices from earlier builds.
# Until a GPU compositor exists, normalize those stale values to the truthful mode.
if state.get("decode_backend") not in ("auto", "hardware", "software"):
    state["decode_backend"] = "auto"
if state.get("render_backend") not in ("auto", "cpu"):
    state["render_backend"] = "auto"
cap = None
# Media decode runs independently from the output renderer. The renderer samples
# the latest decoded frame, so output FPS can never change playback speed and a
# slow decode/JPEG encode cannot inject black frames into the output.
source_fps = 30.0
source_loop_duration = 0.0
source_frame = None
source_frame_serial = 0
source_prepared_frame = None
source_prepared_serial = 0
# Small timestamped frame cache: decoding can run ahead of the compositor so
# short CPU scheduling spikes do not become visible playback stutters.
SOURCE_BUFFER_MAX = 6
# Start both video and audio from one monotonic presentation clock.
# A short pre-roll absorbs decoder/FFmpeg startup latency without changing
# the media timestamps once playback begins.
MEDIA_START_LEAD_SEC = 0.30
source_buffer = deque(maxlen=SOURCE_BUFFER_MAX)  # (sequence_index, prepared_frame)
source_playback_origin = None
source_sequence_index = 0
source_start_offset = 0.0
TEXT_SCROLL_EPOCH = time.monotonic()
source_buffer_lock = threading.Lock()
source_buffer_condition = threading.Condition(source_buffer_lock)
source_frame_lock = threading.Lock()
source_decode_thread = None
# Each decoder receives its own stop token. Replacing the global token instead
# of clearing a shared Event prevents a stale decoder from being unintentionally
# restarted when two source switches happen close together.
source_decode_stop = threading.Event()
source_switch_lock = threading.RLock()
source_decode_generation = 0
source_render_cache = None
source_render_cache_key = None
source_variant = {"brightness": 0.0, "contrast": 1.0, "saturation": 1.0, "hue": 0.0, "angle": 0.0, "scale": 1.0}
FX_DEFAULT = {"brightness": 0, "contrast": 1.0, "saturation": 1.0, "warmth": 0.0,
              "sharpen": 0, "vignette": 0, "skin_smooth": 0, "wrinkle_remove": 0,
              "skin_bright": 0, "lip_tint": 0, "face_slim": 0, "forehead_width": 0,
              "forehead_height": 0, "midface_width": 0, "cheekbone": 0, "temple": 0,
              "face_length": 0, "jaw_refine": 0, "jaw_width": 0, "jaw_curve": 0, "jaw_angle": 0,
              "chin": 0, "chin_height": 0, "chin_roundness": 0, "chin_tip": 0, "eye_size": 0}
_fx_face_detector = None
_fx_eye_detector = None
_fx_yunet = None
_fx_yunet_size = None
_fx_face_mesh = None
_fx_last_detect = 0.0
_fx_face_boxes = []
_fx_eye_boxes = []
_fx_face_landmarks = []
_fx_face_mesh_ready = False
_fx_face_mesh_lock = threading.Lock()
render_frame_count = 0
web_frame = None
web_frame_serial = 0
web_frame_lock = threading.Lock()
web_render_cache = None
web_render_cache_key = None
vcam = VirtualCamera()
vaudio = VirtualAudioRouter()
# Optional, tested libobs compositor bridge. When OBS cannot initialize or falls
# behind, the original direct VirtualCamera path remains the always-on fallback.
libobs_bridge = LibObsBridge(DATA / ".libobs-config")
# Release native device handles on a graceful backend exit. Electron also stops
# this exact backend process tree on window close; these handlers cover Ctrl+C,
# normal interpreter shutdown, and standalone development runs.
atexit.register(vcam.stop)
atexit.register(vaudio.stop)
atexit.register(libobs_bridge.close)
libobs_frame_condition = threading.Condition(threading.Lock())
libobs_latest_frame = None
libobs_latest_frame_serial = 0
libobs_latest_source_serial = 0
libobs_latest_frame_time = 0.0
libobs_input_thread = None
virtual_audio_thread = None

def _start_libobs_bridge(width, height, fps):
    """Use embedded libobs only in an unfrozen runtime; keep packaged output crash-safe.

    The one-file PyInstaller build reproducibly crashed inside VCRUNTIME140.dll
    when the bridge processed a real frame. Until that native ABI/runtime conflict
    is resolved, packaged builds must use the proven direct VirtualCamera path.
    The tested bridge remains available to development/unit-test runs.
    """
    # The experimental bridge has also shown repeated WGL context errors on some
    # Windows/Radeon systems (wglMakeCurrent / device_enter_context), which can
    # flood logs and contend with the actual camera sender. Keep the proven direct
    # VirtualCamera pipeline as the low-resource default; opt in to bridge testing
    # explicitly with PAOKRA_ENABLE_LIBOBS_BRIDGE=1. Packaged builds stay disabled.
    bridge_opt_in = os.environ.get("PAOKRA_ENABLE_LIBOBS_BRIDGE", "").strip().lower() in ("1", "true", "yes")
    if not bridge_opt_in:
        try:
            if libobs_bridge.active:
                libobs_bridge.stop()
        except Exception:
            pass
        with lock:
            state["libobs_active"] = False
            state["libobs_error"] = None
            state["libobs_version"] = None
            state["libobs_latency_ms"] = 0.0
            state["libobs_frames_in"] = 0
            state["libobs_frames_out"] = 0
        return False
    if bool(getattr(sys, "frozen", False)):
        try:
            libobs_bridge.stop()
        except Exception:
            pass
        with lock:
            state["libobs_active"] = False
            state["libobs_error"] = (
                "Packaged libobs path disabled after a native-runtime crash was detected; "
                "using the stable direct Virtual Camera pipeline instead"
            )
            state["libobs_version"] = None
            state["libobs_latency_ms"] = 0.0
            state["libobs_frames_in"] = 0
            state["libobs_frames_out"] = 0
        return False
    try:
        requested_fps = int(fps)
    except (TypeError, ValueError, OverflowError):
        requested_fps = 30
    if requested_fps > 30:
        # The current bridge copies frames across the Python/OpenGL boundary.
        # Full-resolution tests only validate it at <=30 FPS; never risk a 60 FPS
        # stream on an unverified readback path.
        _stop_libobs_bridge()
        with lock:
            state["libobs_error"] = (
                f"LibOBS frame bridge is validated up to 30 FPS; using direct output at {requested_fps} FPS"
            )
        return False
    ok = bool(libobs_bridge.start(width, height, max(1, requested_fps)))
    try:
        info = libobs_bridge.status()
    except Exception as exc:
        info = {"active": False, "version": None, "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                "last_latency_ms": 0.0, "frames_in": 0, "frames_out": 0}
    with lock:
        state["libobs_active"] = bool(ok and info.get("active"))
        state["libobs_error"] = None if state["libobs_active"] else info.get("error")
        state["libobs_version"] = info.get("version")
        state["libobs_latency_ms"] = float(info.get("last_latency_ms") or 0.0)
        state["libobs_frames_in"] = int(info.get("frames_in") or 0)
        state["libobs_frames_out"] = int(info.get("frames_out") or 0)
    return bool(state["libobs_active"])

def _stop_libobs_bridge():
    """Detach libobs from frame IO but retain its reusable graphics context."""
    try:
        libobs_bridge.stop()
        info = libobs_bridge.status()
    except Exception as exc:
        info = {"active": False, "version": None, "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                "last_latency_ms": 0.0, "frames_in": 0, "frames_out": 0}
    with lock:
        state["libobs_active"] = False
        state["libobs_error"] = None
        state["libobs_version"] = info.get("version")
        state["libobs_latency_ms"] = float(info.get("last_latency_ms") or 0.0)
        state["libobs_frames_in"] = int(info.get("frames_in") or 0)
        state["libobs_frames_out"] = int(info.get("frames_out") or 0)

# Open -> shuffled Normal playlist -> Stop clip support. The playlist uses the
# existing decoder/compositor and Virtual Audio router; no upload or transcoding
# is needed for local files.
CLIP_FLOW_FILE = DATA / "clip-flow.json"
CLIP_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
clip_flow_lock = threading.RLock()
clip_flow_eof_event = threading.Event()
clip_flow_done_event = threading.Event()
clip_flow_controller_thread = None
# Serializes source handoff with manual Start/Stop actions to prevent stale source switches.
clip_flow_transition_lock = threading.RLock()
# A reservation spans the entire Auto Make Frames batch, including gaps between
# individual RIFE jobs. The short lease prevents a crashed/closed UI from
# locking Engine controls forever; the UI renews it while the batch is active.
AI_SEAM_BATCH_LEASE_SECONDS = 120.0
ai_seam_batch_guard_lock = threading.RLock()
ai_seam_batch_guard = {"token": "", "lease_until": 0.0, "started_at": 0.0}


def _ai_seam_batch_is_active():
    now = time.monotonic()
    with ai_seam_batch_guard_lock:
        token = str(ai_seam_batch_guard.get("token") or "")
        if token and now >= float(ai_seam_batch_guard.get("lease_until") or 0.0):
            ai_seam_batch_guard.update(token="", lease_until=0.0, started_at=0.0)
            token = ""
        return bool(token)


@app.before_request
def _protect_ai_seam_batch_mutations():
    """Prevent mid-batch edits/cache clearing; allow only token-owned batch calls."""
    if not _ai_seam_batch_is_active():
        return None
    path, method = request.path, request.method.upper()
    if path == "/api/ai-seam/batch-lock":
        return None
    if method in ("GET", "HEAD", "OPTIONS"):
        return None
    if path == "/api/settings" or path == "/api/clip-flow/config" or path == "/api/source":
        return jsonify(ok=False, error="Auto Make Frames is active; Scene/Output/Source changes are temporarily locked until frame verification completes."), 409
    if path.startswith("/api/ai-seam/"):
        body = request.get_json(silent=True)
        body = body if isinstance(body, dict) else {}
        supplied_token = str(body.get("batch_token") or "")
        with ai_seam_batch_guard_lock:
            active_token = str(ai_seam_batch_guard.get("token") or "")
        owns_batch = bool(active_token and supplied_token == active_token)
        if owns_batch and path == "/api/ai-seam/clips" and method == "POST":
            return None
        if owns_batch and path == "/api/ai-seam/generate" and method == "POST":
            return None
        if (owns_batch and path == "/api/ai-seam/settings" and method == "POST"
                and body.get("enabled") is True
                and set(body).issubset({"enabled", "batch_token"})):
            return None
        return jsonify(ok=False, error="Auto Make Frames is verifying transitions. Wait before editing AI Seam clips/settings or clearing the cache."), 409
    return None


# Generation token prevents an old decoder EOF from advancing the next clip.
clip_flow_eof_generation = None

def _load_clip_flow_config():
    default = {"enabled": True, "open": "", "normal": [], "stop": ""}
    try:
        raw = json.loads(CLIP_FLOW_FILE.read_text(encoding="utf-8")) if CLIP_FLOW_FILE.is_file() else {}
        if not isinstance(raw, dict):
            raw = {}
        opened = str(raw.get("open") or "").strip()
        stopped = str(raw.get("stop") or "").strip()
        normals = raw.get("normal") if isinstance(raw.get("normal"), list) else []
        return {
            "enabled": bool(raw.get("enabled", True)),
            "open": opened,
            "normal": [str(x).strip() for x in normals if str(x).strip()][:10000],
            "stop": stopped,
        }
    except Exception:
        return default

clip_flow_config = _load_clip_flow_config()
clip_flow_runtime = {
    "active": False, "phase": "idle", "current_clip": "", "current_role": "",
    "normal_queue": [], "played_in_cycle": [], "previous_normal": "",
    "last_error": "", "reason": "", "started_at": 0.0, "changed_at": 0.0,
    "source_generation": None,
    # Playlist Stop is deferred until the active Open/Normal decoder reaches EOF.
    "stop_requested": False, "pending_stop_path": "",
    "seam_next_path": "", "seam_next_role": "normal", "seam_resume_seconds": 0.0,
    "seam_audio_path": "", "seam_audio_duration": 0.0,
    # Low-cost runtime diagnostics retain evidence of short transitions even when
    # an HTTP poll starts after an 8-frame bridge has already ended.
    "seam_transition_count": 0, "last_seam_transition": None, "last_seam_lookup": None,
}
clip_flow_restore_loop = None

def _apply_cpu_threads(count=None):
    n = int(count or state.get("cpu_threads") or 1)
    n = max(1, min(n, os.cpu_count() or n))
    state["cpu_threads"] = n
    try:
        cv2.setUseOptimized(True)
        cv2.setNumThreads(n)
    except Exception:
        pass
    return n

_apply_cpu_threads()
OUTPUT_PRESETS = {(720,1280),(1080,1920),(1280,720),(1920,1080)}
FPS_PRESETS = {24,30,60}

# Keep the last real camera-negotiated cadence while the camera is stopped for
# AI Seam regeneration. Predicting source FPS alone is not enough on machines
# whose OBS Virtual Camera driver falls back (for example, requested 60 -> 30).
_AI_SEAM_NEGOTIATED_FPS_OVERRIDE = 0


def _effective_output_fps(source_rate_hint=None, prefer_source_hint=False):
    """Resolve fixed FPS or an integer Auto rate, pinning an active camera's clock."""
    # Deliberately do not acquire `lock` (a non-reentrant Lock): callers may
    # already hold it while rendering or serving state.
    mode = str(state.get("fps_mode", "fixed") or "fixed").lower()
    fixed_fps = int(state.get("fps", 30) or 30)
    if mode != "source":
        return fixed_fps if fixed_fps in FPS_PRESETS else 30
    camera = globals().get("vcam")
    if not prefer_source_hint and camera is not None and camera.active:
        try:
            pinned = int(camera.output_fps or 0)
            if 1 <= pinned <= 120:
                return pinned
        except Exception:
            pass
    try:
        rate = float(source_rate_hint if source_rate_hint is not None else source_fps)
    except (TypeError, ValueError, OverflowError):
        rate = float(fixed_fps)
    if not np.isfinite(rate) or rate <= 0:
        rate = float(fixed_fps)
    # pyvirtualcam accepts an integer cadence: 29.97 -> 30, 59.94 -> 60;
    # rates such as 25/50/48 are not forced down to only 24/30/60.
    return max(1, min(120, int(round(rate))))

FONT_CANDIDATES = [Path(r"C:\\Windows\\Fonts\\tahoma.ttf"), Path(r"C:\\Windows\\Fonts\\LeelawUI.ttf"), Path(r"C:\\Windows\\Fonts\\THSarabunNew.ttf")]

def get_font(size):
    for p in FONT_CANDIDATES:
        if p.exists():
            try: return ImageFont.truetype(str(p), max(6, int(size)))
            except Exception: pass
    return ImageFont.load_default()


latest_jpeg = None
latest_jpeg_serial = 0
preview_condition = threading.Condition()
latest_render_frame = None
latest_render_frame_serial = 0
latest_render_frame_lock = threading.Lock()
render_condition = threading.Condition(latest_render_frame_lock)
preview_encode_thread = None
vcam_output_thread = None
render_metrics_lock = threading.Lock()
preview_frame_count = 0
vcam_frame_count = 0
composed_frame_cache = None
composed_frame_cache_key = None
# Cached overlay decode: avoid cv2.imread() on every rendered frame.
overlay_cache_path = None
overlay_cache_mtime = None
overlay_cache = None
overlay_render_cache_key = None
overlay_render_cache = None
render_thread = None
stop_event = threading.Event()
last_preview_encode = 0.0
preview_interval = 1.0 / 30.0
PREVIEW_MAX_WIDTH = 480
_WINMM_TIMER_ACTIVE = False
try:
    if os.name == "nt":
        _WINMM_TIMER_ACTIVE = ctypes.windll.winmm.timeBeginPeriod(1) == 0
except Exception:
    _WINMM_TIMER_ACTIVE = False

def _precise_sleep_until(deadline):
    # High-resolution frame pacing without a busy-spin. Sleeping releases the
    # Python GIL so the render, preview, and camera workers cannot starve each other.
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.0015))

def _resolve_ffmpeg_executable():
    """Prefer the explicit/bundled FFmpeg binary, then fall back to PATH."""
    candidates = [
        os.environ.get("PAOKRA_FFMPEG", ""),
        str(ROOT / "ffmpeg" / "ffmpeg.exe"),
        str(ROOT / "ffmpeg.exe"),
    ]
    for raw in candidates:
        if not raw:
            continue
        try:
            path = Path(raw).expanduser()
            if path.is_file():
                return str(path.resolve())
        except (OSError, ValueError):
            continue
    return shutil.which("ffmpeg.exe") or shutil.which("ffmpeg")


def _resolve_ffprobe_executable():
    """Find ffprobe beside the configured FFmpeg before searching PATH."""
    candidates = []
    ffmpeg = _resolve_ffmpeg_executable()
    if ffmpeg:
        candidates.append(str(Path(ffmpeg).with_name("ffprobe.exe")))
        candidates.append(str(Path(ffmpeg).with_name("ffprobe")))
    candidates.extend((str(ROOT / "ffmpeg" / "ffprobe.exe"), str(ROOT / "ffprobe.exe")))
    for raw in candidates:
        try:
            path = Path(raw)
            if path.is_file():
                return str(path.resolve())
        except (OSError, ValueError):
            continue
    return shutil.which("ffprobe.exe") or shutil.which("ffprobe")


_SOURCE_TIMING_CACHE = {}
_SOURCE_TIMING_CACHE_LOCK = threading.Lock()
_SOURCE_TIMING_CACHE_MAX = 256


def _probe_source_timing(src, fallback_fps):
    """Get timing from ffprobe only when needed; bound work and cache by file version."""
    rate = None
    duration = 0.0
    cache_key = None
    fingerprint = None
    try:
        source_path = Path(src)
        stat = source_path.stat()
        cache_key = os.path.normcase(os.path.abspath(str(source_path)))
        fingerprint = (int(stat.st_size), int(stat.st_mtime_ns))
        with _SOURCE_TIMING_CACHE_LOCK:
            cached = _SOURCE_TIMING_CACHE.get(cache_key)
            if cached and cached[0] == fingerprint:
                return cached[1], cached[2]
    except (OSError, TypeError, ValueError):
        cache_key = None
        fingerprint = None
    try:
        ffprobe = _resolve_ffprobe_executable()
        if ffprobe:
            proc = subprocess.run([
                ffprobe, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=avg_frame_rate,r_frame_rate,duration:format=duration",
                "-of", "json", str(src)
            ], capture_output=True, text=True, timeout=1.25)
            if proc.returncode == 0:
                payload = json.loads(proc.stdout or "{}")
                streams = payload.get("streams") or []
                stream = streams[0] if streams else {}
                raw = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or ""
                if "/" in str(raw):
                    numerator, denominator = str(raw).split("/", 1)
                    denominator = float(denominator)
                    rate = float(numerator) / denominator if denominator else None
                elif raw:
                    rate = float(raw)
                raw_duration = stream.get("duration")
                if raw_duration in (None, "", "N/A"):
                    raw_duration = (payload.get("format") or {}).get("duration")
                if raw_duration not in (None, "", "N/A"):
                    candidate = float(raw_duration)
                    if np.isfinite(candidate) and candidate > 0:
                        duration = candidate
    except Exception:
        pass
    if rate is None or not np.isfinite(rate) or not 1.0 <= rate <= 240.0:
        rate = fallback_fps
    if not np.isfinite(rate) or not 1.0 <= rate <= 240.0:
        rate = 30.0
    result = (float(rate), float(duration))
    if cache_key is not None and fingerprint is not None:
        with _SOURCE_TIMING_CACHE_LOCK:
            if cache_key not in _SOURCE_TIMING_CACHE and len(_SOURCE_TIMING_CACHE) >= _SOURCE_TIMING_CACHE_MAX:
                _SOURCE_TIMING_CACHE.pop(next(iter(_SOURCE_TIMING_CACHE)))
            _SOURCE_TIMING_CACHE[cache_key] = (fingerprint, result[0], result[1])
    return result


def _open_video_capture(src):
    """Open a local media source using the requested decode preference, with safe fallbacks."""
    with lock:
        mode = str(state.get("decode_backend", "auto") or "auto").lower()
    if mode not in ("auto", "hardware", "software"):
        mode = "auto"

    # OpenCV/FFmpeg exposes an acceleration hint, not a guarantee that a GPU
    # decoder exists. Try the requested hint when available, then fall back to
    # FFmpeg's normal backend and finally OpenCV's default backend.
    cap_prop = getattr(cv2, "CAP_PROP_HW_ACCELERATION", None)
    accel_any = getattr(cv2, "VIDEO_ACCELERATION_ANY", None)
    accel_none = getattr(cv2, "VIDEO_ACCELERATION_NONE", None)
    modes = []
    if cap_prop is not None:
        if mode == "software" and accel_none is not None:
            modes.append((cv2.CAP_FFMPEG, [cap_prop, accel_none]))
        elif mode in ("auto", "hardware") and accel_any is not None:
            modes.append((cv2.CAP_FFMPEG, [cap_prop, accel_any]))
    modes.append((cv2.CAP_FFMPEG, None))
    modes.append((0, None))

    for api, params in modes:
        candidate = None
        try:
            if api and params is not None:
                candidate = cv2.VideoCapture(src, api, params)
            elif api:
                candidate = cv2.VideoCapture(src, api)
            else:
                candidate = cv2.VideoCapture(src)
            if candidate is not None and candidate.isOpened():
                return candidate
        except Exception:
            pass
        try:
            if candidate is not None:
                candidate.release()
        except Exception:
            pass
    # Preserve a stable failure contract for callers: return a closed capture.
    return cv2.VideoCapture()


def _seek_capture_to_seconds(capture, seconds, fps):
    """Seek with a frame-index attempt first, then a millisecond fallback.

    OpenCV backend/driver behavior varies; a true return value alone is not a
    guarantee that a position was accepted. The frame getter is used to reject
    grossly wrong results for CFR clips. CAP_PROP_POS_MSEC remains a fallback
    because timestamp getters can be unreliable immediately after a seek.
    """
    try:
        seconds = max(0.0, float(seconds))
        fps = float(fps)
        if not np.isfinite(seconds) or not np.isfinite(fps) or fps <= 0:
            return False
    except (TypeError, ValueError, OverflowError):
        return False
    if seconds <= 0.001:
        return True

    target_frame = max(0, int(round(seconds * fps)))
    try:
        accepted = bool(capture.set(cv2.CAP_PROP_POS_FRAMES, target_frame))
        reported = float(capture.get(cv2.CAP_PROP_POS_FRAMES))
        tolerance = max(1.0, min(3.0, fps * 0.02))
        if accepted and np.isfinite(reported) and abs(reported - target_frame) <= tolerance:
            return True
    except Exception:
        pass
    try:
        return bool(capture.set(cv2.CAP_PROP_POS_MSEC, seconds * 1000.0))
    except Exception:
        return False



# Clip Flow can open and decode the next directed bridge while the current clip
# is still playing. Captures stay off the active output until the exact path and
# destination resume timestamp are selected by the controller.
_source_prime_lock = threading.RLock()
_source_prime_cache = {}
_source_prime_epoch = 0
_SOURCE_PRIME_MAX = 4
_SOURCE_PRIME_TTL_SEC = 45.0
MEDIA_HANDOFF_LEAD_SEC = 0.035

def _source_prime_key(src, start_seconds=0.0):
    try:
        offset = max(0.0, round(float(start_seconds), 4))
    except (TypeError, ValueError, OverflowError):
        offset = 0.0
    try:
        resolved = Path(str(src)).resolve()
        path_key = os.path.normcase(str(resolved)).casefold()
        stat = resolved.stat()
        file_version = (int(stat.st_size), int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9))))
    except Exception:
        path_key = os.path.normcase(str(src)).casefold()
        file_version = (0, 0)
    return path_key, offset, file_version

def _discard_source_prime(item):
    try:
        cap = item.get("cap") if isinstance(item, dict) else None
        if cap is not None:
            cap.release()
    except Exception:
        pass

def _clear_source_prime_cache():
    """Invalidate in-flight prime workers and release every unused media handle."""
    global _source_prime_epoch
    with _source_prime_lock:
        _source_prime_epoch += 1
        stale = list(_source_prime_cache.values())
        _source_prime_cache.clear()
    for item in stale:
        _discard_source_prime(item)
    return len(stale)

def _take_source_prime(src, start_seconds=0.0):
    key = _source_prime_key(src, start_seconds)
    with _source_prime_lock:
        item = _source_prime_cache.pop(key, None)
    if not item:
        return None
    if time.monotonic() - float(item.get("created_at") or 0.0) > _SOURCE_PRIME_TTL_SEC:
        _discard_source_prime(item)
        return None
    cap = item.get("cap")
    try:
        if cap is None or not cap.isOpened():
            _discard_source_prime(item)
            return None
    except Exception:
        _discard_source_prime(item)
        return None
    return item

def _prime_source_capture(src, start_seconds=0.0, expected_epoch=None):
    with _source_prime_lock:
        prime_epoch = _source_prime_epoch if expected_epoch is None else expected_epoch
    try:
        src = str(src or "")
        if not src or not Path(src).is_file():
            return False
        key = _source_prime_key(src, start_seconds)
        now = time.monotonic()
        with _source_prime_lock:
            current = _source_prime_cache.get(key)
            if current and now - float(current.get("created_at") or 0.0) <= _SOURCE_PRIME_TTL_SEC:
                cap = current.get("cap")
                try:
                    if cap is not None and cap.isOpened():
                        return True
                except Exception:
                    pass
                _source_prime_cache.pop(key, None)
        cap = _open_video_capture(src)
        if cap is None or not cap.isOpened():
            try:
                if cap is not None:
                    cap.release()
            except Exception:
                pass
            return False
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        if not np.isfinite(fps) or fps < 1.0 or fps > 240.0:
            fps = 30.0
        offset = max(0.0, float(start_seconds or 0.0))
        if offset > 0.001 and not _seek_capture_to_seconds(cap, offset, fps):
            cap.release()
            return False
        ok, first_frame = cap.read()
        if not ok or first_frame is None:
            cap.release()
            return False
        item = {
            "path": src, "start_seconds": offset, "cap": cap, "first_frame": first_frame,
            "fps": fps, "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1),
            "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 1),
            "frame_count": float(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0),
            "created_at": time.monotonic(),
        }
        evicted = []
        stale_epoch = False
        with _source_prime_lock:
            if prime_epoch != _source_prime_epoch:
                stale_epoch = True
            else:
                # Close stale or duplicate captures before adding another preparation.
                for old_key, old_item in list(_source_prime_cache.items()):
                    if time.monotonic() - float(old_item.get("created_at") or 0.0) > _SOURCE_PRIME_TTL_SEC:
                        evicted.append(_source_prime_cache.pop(old_key))
                duplicate = _source_prime_cache.pop(key, None)
                if duplicate:
                    evicted.append(duplicate)
                while len(_source_prime_cache) >= _SOURCE_PRIME_MAX:
                    oldest_key = min(_source_prime_cache, key=lambda k: _source_prime_cache[k].get("created_at", 0.0))
                    evicted.append(_source_prime_cache.pop(oldest_key))
                _source_prime_cache[key] = item
        if stale_epoch:
            _discard_source_prime(item)
            return False
        for stale in evicted:
            _discard_source_prime(stale)
        return True
    except Exception:
        return False

def _prime_flow_directed_pair(from_path, to_path, target_role="normal"):
    try:
        if not from_path or not to_path or os.path.normcase(str(from_path)) == os.path.normcase(str(to_path)):
            return False
        lookup = {}
        seam = find_cached_transition(DATA, from_path, to_path,
                                      runtime_profile=_ai_seam_runtime_profile(), diagnostics=lookup)
        if not seam:
            return False
        asset = str(seam.get("asset") or "")
        try:
            resume = max(0.0, float(seam.get("resume_seconds") or 0.0))
        except (TypeError, ValueError, OverflowError):
            resume = 0.0
        to_duration = max(0.0, float(seam.get("to_duration") or 0.0))
        if not asset or not Path(asset).is_file() or resume <= 0.0 or to_duration <= resume:
            return False
        # Open the short bridge and B at its exact resume point concurrently;
        # both first frames are decoded before the handoff, not at the boundary.
        with _source_prime_lock:
            expected_epoch = _source_prime_epoch
        tasks = [
            threading.Thread(target=_prime_source_capture, args=(asset, 0.0),
                             kwargs={"expected_epoch": expected_epoch},
                             name="paokra-prime-seam", daemon=True),
            threading.Thread(target=_prime_source_capture, args=(str(to_path), resume),
                             kwargs={"expected_epoch": expected_epoch},
                             name="paokra-prime-destination", daemon=True),
        ]
        for task in tasks:
            task.start()
        deadline = time.monotonic() + 2.1
        for task in tasks:
            task.join(timeout=max(0.0, deadline - time.monotonic()))
        seam_key = _source_prime_key(asset, 0.0)
        destination_key = _source_prime_key(str(to_path), resume)
        with _source_prime_lock:
            return (expected_epoch == _source_prime_epoch
                    and seam_key in _source_prime_cache
                    and destination_key in _source_prime_cache)
    except Exception:
        return False

def _clip_flow_schedule_handoff_prime(current_path, expected_phase=None):
    try:
        with clip_flow_lock:
            phase = str(expected_phase or clip_flow_runtime.get("phase") or "")
            if not clip_flow_runtime.get("active") or phase not in ("open", "normal"):
                return False
            if os.path.normcase(str(clip_flow_runtime.get("current_clip") or "")) != os.path.normcase(str(current_path)):
                return False
            generation = clip_flow_runtime.get("source_generation")
        with source_buffer_lock:
            origin = source_playback_origin
            duration = float(source_loop_duration or 0.0)
        if origin is None or not np.isfinite(duration) or duration <= 0.0:
            return False

        def worker():
            while not stop_event.is_set():
                with clip_flow_lock:
                    still_active = (
                        bool(clip_flow_runtime.get("active"))
                        and clip_flow_runtime.get("phase") == phase
                        and os.path.normcase(str(clip_flow_runtime.get("current_clip") or "")) == os.path.normcase(str(current_path))
                        and clip_flow_runtime.get("source_generation") == generation
                    )
                if not still_active:
                    return
                with source_buffer_lock:
                    now_origin = source_playback_origin
                    now_duration = float(source_loop_duration or 0.0)
                if now_origin is None or now_duration <= 0.0:
                    return
                remaining = now_origin + now_duration - time.monotonic()
                if remaining <= 2.5:
                    break
                time.sleep(min(0.12, max(0.03, remaining - 2.5)))
            with clip_flow_lock:
                if (not clip_flow_runtime.get("active")
                    or clip_flow_runtime.get("phase") != phase
                    or os.path.normcase(str(clip_flow_runtime.get("current_clip") or "")) != os.path.normcase(str(current_path))
                    or clip_flow_runtime.get("source_generation") != generation):
                    return
                if clip_flow_runtime.get("stop_requested") and clip_flow_runtime.get("pending_stop_path"):
                    target = str(clip_flow_runtime.get("pending_stop_path") or "")
                else:
                    target = _clip_flow_peek_next_locked()
            if target and Path(target).is_file():
                _prime_flow_directed_pair(current_path, target)
        threading.Thread(target=worker, name="paokra-clip-flow-prime", daemon=True).start()
        return True
    except Exception:
        return False


def _release_capture_after_thread(thread, capture):
    """Release a VideoCapture after the decoder stops using it.

    Native read() may outlast the short join budget on a slow codec or disk.
    Defer release so the request thread remains responsive without racing the
    old decoder against a native VideoCapture teardown.
    """
    try:
        while thread is not None and thread.is_alive():
            thread.join(timeout=0.5)
    except Exception:
        pass
    try:
        if capture is not None:
            capture.release()
    except Exception:
        pass


def set_source(src, start_seconds=0.0, known_timing=None):
    # Serialize source selection so concurrent UI/playlist/seek actions cannot
    # interleave capture ownership or overwrite decoder-generation tokens.
    with source_switch_lock:
        return _set_source_locked(src, start_seconds=start_seconds, known_timing=known_timing)


def _set_source_locked(src, start_seconds=0.0, known_timing=None):
    global cap, source_fps, source_frame, source_frame_serial
    global source_prepared_frame, source_prepared_serial, source_variant
    global source_playback_origin, source_sequence_index, source_start_offset
    global source_decode_thread, source_decode_stop, source_decode_generation, source_loop_duration
    global source_render_cache, source_render_cache_key
    # Open the candidate first. A bad/corrupt/unsupported source must not destroy
    # the currently selected working source.
    primed_source = _take_source_prime(src, start_seconds)
    new_cap = primed_source.get("cap") if primed_source else _open_video_capture(src)
    first_decoded_frame = primed_source.get("first_frame") if primed_source else None
    ok = bool(new_cap is not None and new_cap.isOpened())
    if not ok:
        try:
            if new_cap is not None:
                new_cap.release()
        except Exception:
            pass
        return False

    # Probe clip metadata before taking the shared state lock. ffprobe can take
    # seconds on some paths/containers; doing it under the lock would stall the
    # renderer and every state request while the clip is being changed.
    new_vw, new_vh = 1, 1
    new_source_fps = float(source_fps or 30.0)
    probed_duration = 0.0
    new_loop_duration = 0.0
    if ok:
        try:
            new_vw = max(1, int(new_cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1))
            new_vh = max(1, int(new_cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 1))
            detected_fps = float(new_cap.get(cv2.CAP_PROP_FPS) or 0.0)
            fps_valid = bool(np.isfinite(detected_fps) and 1.0 <= detected_fps <= 240.0)
            fallback_fps = detected_fps if fps_valid else 30.0
            frame_count = float(new_cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0)
            frame_count_valid = bool(np.isfinite(frame_count) and frame_count > 0.0)
            if known_timing is not None:
                new_source_fps = float(known_timing[0])
                probed_duration = float(known_timing[1])
            elif fps_valid and frame_count_valid:
                # OpenCV already has usable timing metadata for normal local clips.
                # Do not synchronously launch ffprobe on every file switch; that
                # made the UI appear frozen for up to 8 seconds per selected clip.
                new_source_fps = fallback_fps
                probed_duration = 0.0
            else:
                # Unusual containers with missing OpenCV metadata use the bounded,
                # file-version-cached ffprobe fallback (maximum 1.25 s per cold file).
                new_source_fps, probed_duration = _probe_source_timing(src, fallback_fps)
            if np.isfinite(probed_duration) and probed_duration > 0:
                new_loop_duration = float(probed_duration)
            elif np.isfinite(frame_count) and frame_count > 0 and new_source_fps > 0:
                new_loop_duration = frame_count / new_source_fps
        except Exception:
            # Metadata is an optimization; a valid VideoCapture remains usable
            # with safe dimensions/FPS defaults even if the probe fails.
            new_vw, new_vh = max(1, new_vw), max(1, new_vh)
            if not np.isfinite(new_source_fps) or not 1.0 <= new_source_fps <= 240.0:
                new_source_fps = 30.0
            new_loop_duration = 0.0

    try:
        start_seconds = float(start_seconds)
    except (TypeError, ValueError, OverflowError):
        start_seconds = 0.0
    if not np.isfinite(start_seconds):
        start_seconds = 0.0
    start_seconds = max(0.0, start_seconds)
    if new_loop_duration > 0.0:
        end_guard = 1.0 / max(1.0, float(new_source_fps))
        start_seconds = min(start_seconds, max(0.0, new_loop_duration - end_guard))
    else:
        start_seconds = 0.0

    # Validate that the candidate decoder can seek BEFORE stopping/replacing the
    # existing working source. Failed seeking must not desynchronize video from
    # the audio seek or leave the user with a silently frozen/wrong-position clip.
    seek_positioned = bool(primed_source) or _seek_capture_to_seconds(new_cap, start_seconds, new_source_fps)
    if not seek_positioned:
        try:
            new_cap.release()
        except Exception:
            pass
        return False

    with clip_flow_lock:
        flow_source_for_prime = (
            bool(clip_flow_runtime.get("active"))
            and os.path.normcase(str(clip_flow_runtime.get("current_clip") or "")) == os.path.normcase(str(src))
        )
    # Replacing media preserves the Source transform. This matches the useful
    # OBS behavior: changing a file does not unexpectedly move/resize the box.
    with lock:
        preserve = bool(state.get("source"))
        old_transform = (
            int(state.get("source_x", 0)), int(state.get("source_y", 0)),
            max(40, int(state.get("source_width", 720))),
            max(40, int(state.get("source_height", 405)))
        ) if preserve else None

    # Stop and join the old decoder before releasing its VideoCapture. This
    # prevents two source decoders racing when a clip is replaced quickly.
    old_thread = source_decode_thread
    old_stop = source_decode_stop
    old_stop.set()
    if old_thread and old_thread.is_alive() and old_thread is not threading.current_thread():
        old_thread.join(timeout=0.8)

    with lock:
        old = cap
        cap = new_cap if ok else None
        state["source"] = str(src) if ok else None
        state["source_serial"] = int(state.get("source_serial", 0)) + 1
        source_decode_generation += 1
        generation = source_decode_generation
        # Global order for these two locks is source_buffer_lock -> source_frame_lock.
        # _select_source_playback_frame() holds them in that order; reversing it
        # here can deadlock a source switch against the renderer.
        with source_buffer_lock:
            source_buffer.clear()
            source_start_offset = start_seconds
            is_running = bool(state.get("running", False))
            # Prepared Clip Flow handoffs do not need the generic 300-ms cold-start
            # cushion. That cushion otherwise visibly holds the previous clip.
            start_lead = MEDIA_HANDOFF_LEAD_SEC if (flow_source_for_prime and primed_source) else MEDIA_START_LEAD_SEC
            source_playback_origin = (
                time.monotonic() + start_lead - start_seconds
                if is_running else None
            )
            source_sequence_index = int(round(start_seconds * new_source_fps)) if is_running else 0
            with source_frame_lock:
                # Keep the last composed source frame while an active playback
                # switches decoders. The renderer/output workers continue sending
                # that known-good frame until the new decoder has published its
                # first prepared frame, avoiding a black gap between playlist clips.
                # When selecting while paused, clear the prepared frame so the UI
                # preview changes to the newly selected media instead of freezing
                # on the previous clip.
                source_frame = None
                if not (preserve and is_running):
                    source_prepared_frame = None
                source_frame_serial += 1
                source_prepared_serial += 1
        source_render_cache = None
        source_render_cache_key = None
        if ok:
            source_fps = new_source_fps
            source_loop_duration = new_loop_duration
            vaudio.loop_duration = new_loop_duration
            ow, oh = int(state["width"]), int(state["height"])
            # Keep the media aspect ratio. The first source is fit inside the
            # output without distortion; replacing an existing source preserves
            # its current transform/position.
            if old_transform:
                state["source_x"], state["source_y"], state["source_width"], state["source_height"] = old_transform
            else:
                fit = min(1.0, ow / float(new_vw), oh / float(new_vh))
                sw = max(40, int(round(new_vw * fit)))
                sh = max(40, int(round(new_vh * fit)))
                state["source_width"], state["source_height"] = sw, sh
                state["source_x"], state["source_y"] = 0, 0
            state["source_aspect"] = new_vw / float(new_vh)
            source_variant = {"brightness": 0.0, "contrast": 1.0, "saturation": 1.0,
                              "hue": 0.0, "angle": 0.0, "scale": 1.0}
    # Publish the generation expected by Clip Flow before the new decoder starts.
    # Invalidate-on-transition + this token prevents same-file Open/Normal/Stop clips
    # from consuming a stale EOF event from the previous decoder generation.
    with clip_flow_lock:
        if (clip_flow_runtime.get("active") and
                os.path.normcase(str(clip_flow_runtime.get("current_clip") or "")) == os.path.normcase(str(src))):
            clip_flow_runtime["source_generation"] = generation
            if clip_flow_runtime.get("phase") == "seam":
                record = clip_flow_runtime.get("last_seam_transition")
                if isinstance(record, dict) and os.path.normcase(str(record.get("asset") or "")) == os.path.normcase(str(src)):
                    record["preload_hit"] = bool(primed_source)
                    record["preload_first_frame_ready"] = bool(first_decoded_frame is not None)
                    record["preload_created_at"] = float(primed_source.get("created_at") or 0.0) if primed_source else None
    if old is not None and old is not new_cap:
        if old_thread is not None and old_thread.is_alive():
            threading.Thread(
                target=_release_capture_after_thread,
                args=(old_thread, old),
                name="paokra-deferred-capture-release",
                daemon=True,
            ).start()
        else:
            try:
                old.release()
            except Exception:
                pass
    if ok:
        # Give this decoder a fresh, thread-owned stop token. Never clear the
        # previous thread's token: a stale decoder must remain stopped forever.
        worker_stop = threading.Event()
        source_decode_stop = worker_stop
        source_decode_thread = threading.Thread(
            target=source_decode_worker,
            args=(new_cap, str(src), generation, worker_stop,
                  bool(primed_source or (seek_positioned and start_seconds > 0.001)), first_decoded_frame),
            name="paokra-source-decode",
            daemon=True,
        )
        source_decode_thread.start()
    return ok

def _new_loop_variant():
    strength = max(0.0, min(10.0, float(state.get("loop_variation_strength", 4)))) / 10.0
    if not state.get("loop_variation_enabled"):
        return {"brightness": 0.0, "contrast": 1.0, "saturation": 1.0,
                "hue": 0.0, "angle": 0.0, "scale": 1.0}
    return {
        "brightness": random.uniform(-8.0, 8.0) * strength,
        "contrast": 1.0 + random.uniform(-0.035, 0.035) * strength,
        "saturation": 1.0 + random.uniform(-0.08, 0.08) * strength,
        "hue": random.uniform(-2.0, 2.0) * strength,
        "angle": random.uniform(-0.9, 0.9) * strength,
        "scale": 1.0 + random.uniform(-0.025, 0.025) * strength,
    }

def _apply_loop_variant(frame, variant):
    if not variant or (variant.get("brightness", 0) == 0 and variant.get("contrast", 1) == 1
                       and variant.get("saturation", 1) == 1 and variant.get("hue", 0) == 0
                       and variant.get("angle", 0) == 0 and variant.get("scale", 1) == 1):
        return frame
    out = frame
    angle = float(variant.get("angle", 0.0))
    scale = float(variant.get("scale", 1.0))
    if abs(angle) > 0.01 or abs(scale - 1.0) > 0.001:
        h, w = out.shape[:2]
        M = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), angle, scale)
        out = cv2.warpAffine(out, M, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    if abs(variant.get("brightness", 0)) > 0.01 or abs(variant.get("contrast", 1.0) - 1.0) > 0.001:
        out = cv2.convertScaleAbs(out, alpha=float(variant.get("contrast", 1.0)),
                                 beta=float(variant.get("brightness", 0)))
    if abs(variant.get("saturation", 1.0) - 1.0) > 0.001 or abs(variant.get("hue", 0)) > 0.01:
        hsv = cv2.cvtColor(out, cv2.COLOR_BGR2HSV)
        hsv = hsv.astype(np.int16)
        hsv[:, :, 0] = (hsv[:, :, 0] + int(round(variant.get("hue", 0)))) % 180
        s_mul = float(variant.get("saturation", 1.0))
        hsv[:, :, 1] = np.clip(hsv[:, :, 1] * s_mul, 0, 255)
        out = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)
    return out

def _prepare_source_render_frame(decoded, width, height, variant, fit_mode="cover", flip_h=False, flip_v=False, video_fx=None):
    # Prepare at Source-box resolution off the render thread.
    # video_fx is accepted for compatibility, but FX is applied by render_frame()
    # from the latest state so changing a slider cannot get stuck in a stale frame.
    # For cover, crop in the source domain first, then resize. This avoids
    # creating a huge intermediate frame when a landscape MP4 fills portrait output.
    src_h, src_w = decoded.shape[:2]
    width, height = max(1, int(width)), max(1, int(height))
    target_aspect = width / float(height)
    source_aspect = src_w / float(src_h)
    if fit_mode == "cover":
        if source_aspect > target_aspect:
            crop_w = max(1, int(round(src_h * target_aspect)))
            left = max(0, (src_w - crop_w) // 2)
            work = decoded[:, left:left + crop_w]
        elif source_aspect < target_aspect:
            crop_h = max(1, int(round(src_w / target_aspect)))
            top = max(0, (src_h - crop_h) // 2)
            work = decoded[top:top + crop_h, :]
        else:
            work = decoded
        if work.shape[1] != width or work.shape[0] != height:
            interp = cv2.INTER_AREA if work.shape[1] > width or work.shape[0] > height else cv2.INTER_LINEAR
            work = cv2.resize(work, (width, height), interpolation=interp)
    else:
        scale = min(width / float(src_w), height / float(src_h))
        rw = max(1, int(round(src_w * scale)))
        rh = max(1, int(round(src_h * scale)))
        interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
        work = cv2.resize(decoded, (rw, rh), interpolation=interp)
        canvas = np.zeros((height, width, 3), dtype=np.uint8)
        ox = max(0, (width - rw) // 2)
        oy = max(0, (height - rh) // 2)
        canvas[oy:oy + rh, ox:ox + rw] = work
        work = canvas
    if flip_h and flip_v:
        work = cv2.flip(work, -1)
    elif flip_h:
        work = cv2.flip(work, 1)
    elif flip_v:
        work = cv2.flip(work, 0)
    work = _apply_loop_variant(work, variant)
    # FX is applied later in render_frame() from the newest state.
    return np.ascontiguousarray(work, dtype=np.uint8)


def _load_fx_detectors():
    global _fx_face_detector, _fx_eye_detector, _fx_yunet, _fx_face_mesh, _fx_face_mesh_ready
    if _fx_face_mesh_ready:
        return
    try:
        base=Path(cv2.data.haarcascades)
    except Exception:
        base=None
    try:
        _fx_eye_detector=cv2.CascadeClassifier(str(base/"haarcascade_eye.xml")) if base else cv2.CascadeClassifier()
    except Exception:
        _fx_eye_detector=cv2.CascadeClassifier()
    try:
        model=Path(os.environ.get("PAOKRA_YUNET_MODEL",str(ROOT/"app"/"models"/"face_detection_yunet_2023mar.onnx")))
        if model.is_file() and hasattr(cv2,"FaceDetectorYN"):
            _fx_yunet=cv2.FaceDetectorYN.create(str(model),"",(320,320),0.60,0.30,5000)
        else:
            _fx_yunet=None
    except Exception:
        _fx_yunet=None
    try:
        _fx_face_detector=cv2.CascadeClassifier(str(base/"haarcascade_frontalface_default.xml")) if base else cv2.CascadeClassifier()
    except Exception:
        _fx_face_detector=cv2.CascadeClassifier()
    if mp is not None:
        try:
            _fx_face_mesh=mp.solutions.face_mesh.FaceMesh(
                static_image_mode=False,
                max_num_faces=4,
                refine_landmarks=True,
                min_detection_confidence=0.5,
                min_tracking_confidence=0.5,
            )
        except Exception:
            _fx_face_mesh=None
    _fx_face_mesh_ready=True

def _fx_detect_faces(frame):
    global _fx_last_detect, _fx_face_boxes, _fx_eye_boxes, _fx_yunet_size, _fx_face_landmarks
    _load_fx_detectors()
    now=time.monotonic()
    # Mesh tracking is the expensive stage. Refresh often enough to follow movement
    # while avoiding needless duplicate inference during a 30/60 FPS render loop.
    if now-_fx_last_detect < 0.06 and _fx_face_boxes:
        return _fx_face_boxes, _fx_eye_boxes
    _fx_last_detect=now
    faces=[]; eyes=[]; landmarks=[]

    if _fx_face_mesh is not None:
        try:
            rgb=cv2.cvtColor(frame,cv2.COLOR_BGR2RGB)
            with _fx_face_mesh_lock:
                result=_fx_face_mesh.process(rgb)
            if result.multi_face_landmarks:
                h,w=frame.shape[:2]
                eye_groups=(
                    (33,133,159,145,160,144,158,153,157,173),
                    (362,263,386,374,387,373,385,380,384,398),
                )
                for face in result.multi_face_landmarks[:4]:
                    pts=np.array([(float(p.x*w),float(p.y*h),float(p.z*w)) for p in face.landmark],dtype=np.float32)
                    if pts.shape[0] < 200:
                        continue
                    px=np.clip(pts[:,0],0,w-1); py=np.clip(pts[:,1],0,h-1)
                    minx,maxx,miny,maxy=float(px.min()),float(px.max()),float(py.min()),float(py.max())
                    bw=maxx-minx; bh=maxy-miny
                    if bw<48 or bh<48:
                        continue
                    pad_x=bw*0.08; pad_y=bh*0.10
                    x=max(0,int(minx-pad_x)); y=max(0,int(miny-pad_y))
                    x1=min(w,int(maxx+pad_x)); y1=min(h,int(maxy+pad_y))
                    faces.append((x,y,max(1,x1-x),max(1,y1-y)))
                    landmarks.append([(float(p[0]),float(p[1])) for p in pts])
                    for group in eye_groups:
                        ep=pts[list(group),:2]
                        ex0=max(0,int(ep[:,0].min()-max(4,(ep[:,0].max()-ep[:,0].min())*.20)))
                        ey0=max(0,int(ep[:,1].min()-max(3,(ep[:,1].max()-ep[:,1].min())*.35)))
                        ex1=min(w,int(ep[:,0].max()+max(4,(ep[:,0].max()-ep[:,0].min())*.20)))
                        ey1=min(h,int(ep[:,1].max()+max(3,(ep[:,1].max()-ep[:,1].min())*.35)))
                        if ex1-ex0>=10 and ey1-ey0>=8:
                            eyes.append((ex0,ey0,ex1-ex0,ey1-ey0))
        except Exception:
            faces=[]; eyes=[]; landmarks=[]

    # Fallbacks keep old hardware/configuration usable if MediaPipe is unavailable.
    if not faces and _fx_yunet is not None:
        try:
            h,w=frame.shape[:2]
            if _fx_yunet_size!=(w,h):
                _fx_yunet.setInputSize((w,h)); _fx_yunet_size=(w,h)
            _,raw=_fx_yunet.detect(frame)
            if raw is not None:
                for row in raw[:4]:
                    x,y,bw,bh=float(row[0]),float(row[1]),float(row[2]),float(row[3])
                    if bw>=24 and bh>=24:
                        faces.append((max(0,int(x)),max(0,int(y)),max(1,int(bw)),max(1,int(bh))))
                        landmarks.append([(float(row[i]),float(row[i+1])) for i in range(4,14,2)])
        except Exception:
            faces=[]; landmarks=[]
    if not faces and _fx_face_detector is not None and not _fx_face_detector.empty():
        gray=cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY)
        scale=1.0; small=gray
        if gray.shape[1]>480:
            scale=480.0/gray.shape[1]
            small=cv2.resize(gray,(480,max(1,int(gray.shape[0]*scale))),interpolation=cv2.INTER_AREA)
        raw=_fx_face_detector.detectMultiScale(small,1.1,4,minSize=(50,50))
        inv=1.0/scale
        faces=[(int(x*inv),int(y*inv),int(w*inv),int(h*inv)) for x,y,w,h in raw[:4]]
    if faces and not eyes and _fx_eye_detector is not None and not _fx_eye_detector.empty():
        gray=cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY)
        for x,y,w,h in faces[:4]:
            y2=min(gray.shape[0],y+max(1,int(h*0.62)))
            roi=gray[max(0,y):y2,max(0,x):min(gray.shape[1],x+w)]
            if roi.size==0: continue
            raw_eyes=_fx_eye_detector.detectMultiScale(roi,1.12,5,minSize=(18,14))
            for ex,ey,ew,eh in raw_eyes[:4]:
                eyes.append((x+ex,y+ey,ew,eh))
    _fx_face_boxes,_fx_eye_boxes=faces,eyes
    _fx_face_landmarks=landmarks[:len(faces)]
    return faces,eyes

def _fx_skin_mask(frame, x, y, w, h, eyes, amount):
    """Build a face-shaped skin mask with feature exclusions; no giant blur circle."""
    mask=np.zeros(frame.shape[:2],np.uint8)
    x0,y0=max(0,int(x)),max(0,int(y)); x1,y1=min(frame.shape[1],int(x+w)),min(frame.shape[0],int(y+h))
    if x1-x0<32 or y1-y0<32 or amount<=0: return mask
    rw,rh=x1-x0,y1-y0
    # Rounded face silhouette, deliberately narrower than the detector box.
    local=np.zeros((rh,rw),np.uint8)
    pts=np.array([
        [int(rw*.50),int(rh*.03)],[int(rw*.22),int(rh*.12)],[int(rw*.08),int(rh*.36)],
        [int(rw*.12),int(rh*.70)],[int(rw*.28),int(rh*.91)],[int(rw*.50),int(rh*.99)],
        [int(rw*.72),int(rh*.91)],[int(rw*.88),int(rh*.70)],[int(rw*.92),int(rh*.36)],
        [int(rw*.78),int(rh*.12)]
    ],np.int32)
    cv2.fillPoly(local,[pts],255)
    # Exclude eyes and the central mouth/nose zone so texture/detail is retained.
    for ex,ey,ew,eh in eyes:
        if ex+ew<x0 or ex>x1 or ey+eh<y0 or ey>y1: continue
        cv2.ellipse(local,(int(ex+ew/2-x0),int(ey+eh/2-y0)),
                    (max(5,int(ew*.95)),max(4,int(eh*.9))),0,0,360,0,-1)
    mouth_cx=int(x0+rw*.50); mouth_cy=int(y0+rh*.73)
    cv2.ellipse(local,(mouth_cx-x0,mouth_cy-y0),
                (max(10,int(rw*.18)),max(7,int(rh*.09))),0,0,360,0,-1)
    # Feather only the boundary; strength controls blend, not blur radius.
    local=cv2.GaussianBlur(local,(0,0),sigmaX=max(1.5,rw*.012))
    mask[y0:y1,x0:x1]=local
    return mask

def _fx_mesh_warp_face(frame, box, landmarks, amount, region="face"):
    """Dense Face-Mesh geometry warp with independent anatomical regions."""
    amount=max(-1.0,min(1.0,float(amount)))
    if abs(amount)<=0.001 or not landmarks or len(landmarks)<100:
        return
    x,y,w,h=box
    x0,y0=max(0,int(x)),max(0,int(y)); x1,y1=min(frame.shape[1],int(x+w)),min(frame.shape[0],int(y+h))
    rw,rh=x1-x0,y1-y0
    if rw<64 or rh<64:return

    full_lm=np.asarray(landmarks,np.float32)
    if full_lm.ndim!=2 or full_lm.shape[1]<2 or len(full_lm)<100:
        return
    full_lm=full_lm[:,:2].copy()
    full_lm[:,0]-=x0; full_lm[:,1]-=y0
    good=np.isfinite(full_lm).all(axis=1)
    if good.sum()<100:return

    hull=cv2.convexHull(full_lm[good].astype(np.float32))
    mesh_mask=np.zeros((rh,rw),np.uint8)
    cv2.fillConvexPoly(mesh_mask,hull.astype(np.int32),255)
    alpha=cv2.GaussianBlur(mesh_mask,(0,0),sigmaX=max(1.4,rw*.016),sigmaY=max(1.4,rh*.016)).astype(np.float32)/255.0

    cx=float(np.median(full_lm[good,0]))
    half_w=max(20.0,float(np.percentile(full_lm[good,0],92)-np.percentile(full_lm[good,0],8))/2.0)
    top=float(np.percentile(full_lm[good,1],8))
    bottom=float(np.percentile(full_lm[good,1],92))
    yy,xx=np.mgrid[0:rh,0:rw].astype(np.float32)
    relx=np.abs(xx-cx)/half_w
    rely=np.clip((yy-top)/max(1.0,bottom-top),0,1)

    # Each control owns a different vertical/lateral zone.
    configs={
        "face":(.25,.90,.20,.76,.065,.72),
        "forehead":(.25,.84,.03,.32,.050,.90),
        "midface":(.18,.86,.24,.68,.055,.88),
        "cheekbone":(.34,.72,.30,.63,.055,.90),
        "temple":(.55,.98,.05,.42,.045,.95),
        "jaw":(.40,.94,.60,1.02,.085,1.00),
        "chin":(.18,.66,.74,1.04,.055,1.00),
    }
    if region not in configs:
        return
    x0r,x1r,y0r,y1r,max_frac,focus_gain=configs[region]
    side=np.clip((relx-x0r)/max(.01,x1r-x0r),0,1)
    side=side*side*(3.0-2.0*side)
    vertical=np.clip((rely-y0r)/max(.01,y1r-y0r),0,1)
    vertical=vertical*vertical*(3.0-2.0*vertical)
    mid=(y0r+y1r)*.5
    focus=focus_gain*(.70+.30*np.exp(-((rely-mid)/max(.12,(y1r-y0r)*.55))**2))

    # Protect eyes, nose and mouth. Region controls never move the central
    # feature plane, even at 100.
    protected=np.zeros((rh,rw),np.float32)
    protect_groups=(
        ((33,133,159,145,160,144,158,153,157,173),.24),
        ((362,263,386,374,387,373,385,380,384,398),.24),
        ((61,146,91,181,84,17,314,405,321,375,291,308,324,318,402,317,14,87,178,88,95),.38),
        ((1,2,4,5,6,98,195,197,327),.28),
    )
    for ids,scale_ in protect_groups:
        ids=[i for i in ids if i < len(full_lm) and np.isfinite(full_lm[i]).all()]
        if not ids:continue
        p=full_lm[ids]
        cc=p.mean(axis=0)
        sx=max(9.0,float(np.ptp(p[:,0]))*scale_+9.0)
        sy=max(8.0,float(np.ptp(p[:,1]))*scale_+8.0)
        protected=np.maximum(protected,np.exp(-(((xx-cc[0])/sx)**2+((yy-cc[1])/sy)**2)*2.2))
    centre_keep=np.exp(-(((xx-cx)/max(1.0,rw*.30))**2))*np.exp(-((rely-.60)/.30)**2)
    protected=np.clip(np.maximum(protected,centre_keep),0,1)

    displacement=rw*max_frac*amount*side*vertical*focus*alpha*(1.0-protected)
    sign=np.where(xx>=cx,1.0,-1.0)
    map_x=xx+sign*displacement
    original=frame[y0:y1,x0:x1].copy()
    warped=cv2.remap(original,map_x.astype(np.float32),yy,cv2.INTER_LINEAR,borderMode=cv2.BORDER_REFLECT101)
    blend=cv2.GaussianBlur(alpha,(0,0),sigmaX=max(1.2,rw*.007),sigmaY=max(1.2,rh*.007))
    frame[y0:y1,x0:x1]=(original*(1-blend[...,None])+warped*blend[...,None]).astype(np.uint8)

def _fx_box_warp_region(frame,x,y,w,h,amount,region="face"):
    """Fallback geometry warp using only the detected face box when Face Mesh is unavailable."""
    amount=max(-1.0,min(1.0,float(amount)))
    if abs(amount)<=0.001:return
    x0,y0=max(0,int(x)),max(0,int(y)); x1,y1=min(frame.shape[1],int(x+w)),min(frame.shape[0],int(y+h))
    rw,rh=x1-x0,y1-y0
    if rw<64 or rh<64:return
    yy,xx=np.mgrid[0:rh,0:rw].astype(np.float32)
    cx=rw*.50; cy=rh*.50
    rx=np.abs(xx-cx)/max(1.0,rw*.50); ry=yy/max(1.0,rh)
    zones={
        "face": np.exp(-(((rx/.92)**2+((ry-.55)/.58)**2)*2.0)),
        "forehead": np.exp(-(((rx/.78)**2+((ry-.20)/.28)**2)*2.2)),
        "midface": np.exp(-(((rx/.86)**2+((ry-.48)/.27)**2)*2.0)),
        "cheekbone": np.exp(-(((rx/.72)**2+((ry-.53)/.20)**2)*2.2)),
        "temple": np.exp(-(((rx-.70)/.34)**2+((ry-.30)/.28)**2)*2.0),
        "jaw": np.exp(-(((rx/.92)**2+((ry-.73)/.30)**2)*2.0)),
        "chin": np.exp(-(((rx/.55)**2+((ry-.86)/.24)**2)*2.1)),
    }
    mask=zones.get(region)
    if mask is None:return
    mask*=np.clip(1.0-rx*.35,0,1)
    if region in ("face","jaw","chin"):
        # Positive narrows the active outer region; negative expands it.
        scale=1.0-amount*.16*mask
        map_x=cx+(xx-cx)/np.clip(scale,.80,1.20)
        map_y=yy
    elif region in ("forehead","midface","cheekbone","temple"):
        scale=1.0-amount*.12*mask
        map_x=cx+(xx-cx)/np.clip(scale,.82,1.18)
        map_y=yy
    else:
        return
    roi=frame[y0:y1,x0:x1].copy()
    warped=cv2.remap(roi,map_x.astype(np.float32),map_y.astype(np.float32),cv2.INTER_LINEAR,borderMode=cv2.BORDER_REFLECT101)
    blend=cv2.GaussianBlur(np.clip(mask,0,1),(0,0),sigmaX=max(1.4,rw*.012),sigmaY=max(1.4,rh*.012))
    frame[y0:y1,x0:x1]=(roi*(1-blend[...,None])+warped*blend[...,None]).astype(np.uint8)

def _fx_enlarge_eye_box(frame,eyes,amount):
    amount=max(-1.0,min(1.0,float(amount)))
    if abs(amount)<=0.001:return
    for ex,ey,ew,eh in eyes[:2]:
        if ew<12 or eh<8:continue
        pad_x=max(4,int(ew*.45)); pad_y=max(4,int(eh*.70))
        x0=max(0,int(ex-pad_x)); y0=max(0,int(ey-pad_y)); x1=min(frame.shape[1],int(ex+ew+pad_x)); y1=min(frame.shape[0],int(ey+eh+pad_y))
        if x1-x0<20 or y1-y0<16:continue
        roi=frame[y0:y1,x0:x1].copy(); rh,rw=roi.shape[:2]
        cx=(ex+ew*.5)-x0; cy=(ey+eh*.5)-y0
        yy,xx=np.mgrid[0:rh,0:rw].astype(np.float32)
        fall=np.clip(1.0-(((xx-cx)/max(6.0,ew*.9))**2+((yy-cy)/max(5.0,eh*1.4))**2),0,1)
        strength=1.0+amount*.28*fall
        map_x=cx+(xx-cx)/np.clip(strength,.80,1.25); map_y=cy+(yy-cy)/np.clip(strength,.80,1.25)
        warped=cv2.remap(roi,map_x.astype(np.float32),map_y.astype(np.float32),cv2.INTER_LINEAR,borderMode=cv2.BORDER_REFLECT101)
        blend=cv2.GaussianBlur(fall,(0,0),sigmaX=max(1.0,ew*.08),sigmaY=max(1.0,eh*.14))
        frame[y0:y1,x0:x1]=(roi*(1-blend[...,None])+warped*blend[...,None]).astype(np.uint8)

def _fx_remap_region(frame,x,y,w,h,amount,region="face",landmarks=None):
    if landmarks and len(landmarks)>=100:
        _fx_mesh_warp_face(frame,(x,y,w,h),landmarks,amount,region)
    else:
        _fx_box_warp_region(frame,x,y,w,h,amount,region)

def _fx_enlarge_eye_mesh(frame, landmarks, amount):
    """Expand only the actual eye contour; eyebrow/forehead stay outside the mask."""
    amount=max(-1.0,min(1.0,float(amount)))
    if abs(amount)<=0.001 or not landmarks or len(landmarks)<478:
        return
    pts=np.asarray(landmarks,np.float32)
    groups=(
        [33,7,163,144,145,153,154,155,133,173,157,158,159,160,161,246],
        [362,382,381,380,374,373,390,249,263,466,388,387,386,385,384,398],
    )
    for ids in groups:
        p=pts[ids,:2]
        if not np.isfinite(p).all():continue
        minx,maxx=float(p[:,0].min()),float(p[:,0].max())
        miny,maxy=float(p[:,1].min()),float(p[:,1].max())
        ew=maxx-minx; eh=maxy-miny
        if ew<10 or eh<6:continue
        iris_ids=range(468,473) if np.mean(p[:,0]) < frame.shape[1]*.5 else range(473,478)
        iris=pts[list(iris_ids),:2] if len(pts)>=478 else p
        if np.isfinite(iris).all():
            centre=iris.mean(axis=0)
        else:
            centre=p.mean(axis=0)
        pad_x=max(2.0,ew*.16); pad_y=max(2.0,eh*.28)
        rx=(ew*.56)+pad_x; ry=(eh*.78)+pad_y
        x0=max(0,int(centre[0]-rx)); x1=min(frame.shape[1],int(centre[0]+rx))
        y0=max(0,int(centre[1]-ry)); y1=min(frame.shape[0],int(centre[1]+ry))
        if x1-x0<16 or y1-y0<12:continue
        roi=frame[y0:y1,x0:x1].copy()
        rh,rw=roi.shape[:2]
        yy,xx=np.mgrid[0:rh,0:rw].astype(np.float32)
        ccx=float(centre[0]-x0); ccy=float(centre[1]-y0)
        # Tight elliptical falloff. Positive = larger eye, negative = smaller.
        dx=(xx-ccx)/max(1.0,rx); dy=(yy-ccy)/max(1.0,ry)
        fall=np.clip(1.0-(dx*dx+dy*dy),0,1)
        strength=1.0+amount*(.32*fall)
        map_x=ccx+(xx-ccx)/strength
        map_y=ccy+(yy-ccy)/strength
        warped=cv2.remap(roi,map_x.astype(np.float32),map_y.astype(np.float32),cv2.INTER_LINEAR,borderMode=cv2.BORDER_REFLECT101)
        # Use the actual eye polygon as the core mask, then feather only a tiny edge.
        mask=np.zeros((rh,rw),np.uint8)
        poly=np.round(p-np.array([x0,y0],np.float32)).astype(np.int32)
        cv2.fillPoly(mask,[poly],255)
        mask=cv2.GaussianBlur(mask,(0,0),sigmaX=max(1.0,ew*.06),sigmaY=max(1.0,eh*.10)).astype(np.float32)/255.0
        frame[y0:y1,x0:x1]=(roi*(1-mask[...,None])+warped*mask[...,None]).astype(np.uint8)

def _fx_retouch_mask(frame, landmarks, box):
    """Dense skin mask: face mesh minus eyes, brows, nose and lips."""
    x,y,w,h=box
    x0,y0=max(0,int(x)),max(0,int(y)); x1,y1=min(frame.shape[1],int(x+w)),min(frame.shape[0],int(y+h))
    rw,rh=x1-x0,y1-y0
    mask=np.zeros((rh,rw),np.uint8)
    if rw<32 or rh<32 or not landmarks or len(landmarks)<100:
        return mask
    pts=np.asarray(landmarks,np.float32)[:,:2].copy()
    pts[:,0]-=x0; pts[:,1]-=y0
    good=np.isfinite(pts).all(axis=1)
    if good.sum()<100:return mask
    hull=cv2.convexHull(pts[good].astype(np.float32))
    cv2.fillConvexPoly(mask,hull.astype(np.int32),255)
    exclusions=(
        (33,133,159,145,160,144,158,153,157,173,157,158,159,160,161,246),
        (362,263,386,374,387,373,385,380,384,398,382,381,380,374,373,390,249,466),
        (70,63,105,66,107,55,65,52,53,46,300,293,334,296,336,285,295,282,283,276),
        (61,146,91,181,84,17,314,405,321,375,291,308,324,318,402,317,14,87,178,88,95),
        (1,2,4,5,6,98,195,197,327,326,358,429,2),
    )
    for ids in exclusions:
        ids=[i for i in ids if i<len(pts) and np.isfinite(pts[i]).all()]
        if not ids: continue
        poly=np.round(pts[ids]).astype(np.int32)
        cv2.fillConvexPoly(mask,cv2.convexHull(poly),0)
    mask=cv2.GaussianBlur(mask,(0,0),sigmaX=max(1.2,rw*.010),sigmaY=max(1.2,rh*.010))
    return mask

def _fx_lip_mask(frame, landmarks, box):
    x,y,w,h=box
    x0,y0=max(0,int(x)),max(0,int(y)); x1,y1=min(frame.shape[1],int(x+w)),min(frame.shape[0],int(y+h))
    mask=np.zeros((max(1,y1-y0),max(1,x1-x0)),np.uint8)
    if not landmarks or len(landmarks)<200:return mask,(x0,y0,x1,y1)
    pts=np.asarray(landmarks,np.float32)[:,:2].copy()
    ids=[61,146,91,181,84,17,314,405,321,375,291,308,324,318,402,317,14,87,178,88,95]
    ids=[i for i in ids if i<len(pts) and np.isfinite(pts[i]).all()]
    if len(ids)>=6:
        poly=np.round(pts[ids]-np.array([x0,y0],np.float32)).astype(np.int32)
        cv2.fillConvexPoly(mask,poly,255)
        mask=cv2.GaussianBlur(mask,(0,0),sigmaX=max(0.8,(x1-x0)*.012),sigmaY=max(0.8,(y1-y0)*.012))
    return mask,(x0,y0,x1,y1)

def _fx_skin_retouch_box(frame, box, smooth, wrinkle, bright, eyes=()):
    """Beauty fallback for systems without usable Face Mesh landmarks."""
    x,y,w,h=box
    mask=_fx_skin_mask(frame,x,y,w,h,eyes,max(smooth,wrinkle,bright))
    if not np.any(mask):return
    x0,y0=max(0,int(x)),max(0,int(y)); x1,y1=min(frame.shape[1],int(x+w)),min(frame.shape[0],int(y+h))
    roi=frame[y0:y1,x0:x1].copy(); m=mask[y0:y1,x0:x1].astype(np.float32)/255.0
    if smooth>0:
        sm=cv2.bilateralFilter(roi,0,22+smooth*.55,3.0+smooth*.035)
        a=(m*(smooth/100.0)*.62)[...,None]; roi=(roi*(1-a)+sm*a).astype(np.uint8)
    if wrinkle>0:
        gray=cv2.cvtColor(roi,cv2.COLOR_BGR2GRAY); kernel=cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(9,5))
        blackhat=cv2.morphologyEx(gray,cv2.MORPH_BLACKHAT,kernel).astype(np.float32)
        lift=np.clip(cv2.GaussianBlur(blackhat,(0,0),1.0)*(wrinkle/100.0)*.58,0,22)
        a=(m*(wrinkle/100.0))[...,None]; roi=np.clip(roi.astype(np.float32)+lift[...,None]*a,0,255).astype(np.uint8)
    if bright>0:
        lab=cv2.cvtColor(roi,cv2.COLOR_BGR2LAB).astype(np.float32); lab[:,:,0]=np.clip(lab[:,:,0]+(8.0+bright*.07)*m,0,255)
        roi=cv2.cvtColor(lab.astype(np.uint8),cv2.COLOR_LAB2BGR)
    frame[y0:y1,x0:x1]=roi

def _fx_box_vertical_detail(frame,x,y,w,h,amount,region):
    """Fallback vertical geometry for forehead height and overall face length."""
    amount=max(-1.0,min(1.0,float(amount)))
    if abs(amount)<=.001:return
    x0,y0=max(0,int(x)),max(0,int(y)); x1,y1=min(frame.shape[1],int(x+w)),min(frame.shape[0],int(y+h))
    rw,rh=x1-x0,y1-y0
    if rw<64 or rh<64:return
    yy,xx=np.mgrid[0:rh,0:rw].astype(np.float32); cx=rw*.5
    if region=="forehead_height": center=.22; zone=np.exp(-(((xx-cx)/(rw*.42))**2+((yy-rh*center)/(rh*.24))**2)*2.0); strength=1+amount*.11*zone
    elif region=="face_length": center=.55; zone=np.exp(-(((xx-cx)/(rw*.46))**2+((yy-rh*center)/(rh*.48))**2)*1.8); strength=1+amount*.13*zone
    else:return
    cy=rh*center; map_y=cy+(yy-cy)/np.clip(strength,.84,1.18)
    roi=frame[y0:y1,x0:x1].copy(); warped=cv2.remap(roi,xx.astype(np.float32),map_y.astype(np.float32),cv2.INTER_LINEAR,borderMode=cv2.BORDER_REFLECT101)
    blend=cv2.GaussianBlur(np.clip(zone,0,1),(0,0),sigmaX=max(1.4,rw*.014),sigmaY=max(1.4,rh*.014))
    frame[y0:y1,x0:x1]=(roi*(1-blend[...,None])+warped*blend[...,None]).astype(np.uint8)

def _fx_box_lower_detail(frame,x,y,w,h,amount,mode):
    """Fallback lower-face geometry when dense landmarks are unavailable."""
    amount=max(-1.0,min(1.0,float(amount)))
    if abs(amount)<=.001:return
    x0,y0=max(0,int(x)),max(0,int(y)); x1,y1=min(frame.shape[1],int(x+w)),min(frame.shape[0],int(y+h))
    rw,rh=x1-x0,y1-y0; yy,xx=np.mgrid[0:rh,0:rw].astype(np.float32); cx=rw*.5
    lower=np.clip((yy-rh*.58)/(rh*.42),0,1); lower=lower*lower*(3-2*lower)
    rx=np.abs(xx-cx)/max(1.0,rw*.5)
    if mode=="jaw_curve": shift=-amount*rh*.045*lower*np.clip((rx-.25)/.75,0,1); map_y=yy+shift; mask=lower
    elif mode=="jaw_angle": shift=-amount*rh*.040*lower*np.clip((rx-.34)/.66,0,1); map_y=yy+shift; mask=lower*np.clip((rx-.22)/.78,0,1)
    elif mode=="chin_height": cy=rh*.82; fall=np.exp(-(((xx-cx)/(rw*.30))**2+((yy-cy)/(rh*.16))**2)*2.0); scale=1+amount*.13*fall; map_y=cy+(yy-cy)/np.clip(scale,.84,1.18); mask=fall
    elif mode=="chin_roundness": cy=rh*.84; fall=np.exp(-(((xx-cx)/(rw*.34))**2+((yy-cy)/(rh*.15))**2)*2.0); scale=1+amount*.12*fall; map_x=cx+(xx-cx)/np.clip(scale,.84,1.18); map_y=yy; mask=fall
    elif mode=="chin_tip": cy=rh*.87; fall=np.exp(-(((xx-cx)/(rw*.32))**2+((yy-cy)/(rh*.13))**2)*2.0); map_x=xx; map_y=yy+amount*rh*.035*fall; mask=fall
    else:return
    roi=frame[y0:y1,x0:x1].copy(); warped=cv2.remap(roi,(map_x if 'map_x' in locals() else xx).astype(np.float32),map_y.astype(np.float32),cv2.INTER_LINEAR,borderMode=cv2.BORDER_REFLECT101)
    blend=cv2.GaussianBlur(np.clip(mask,0,1),(0,0),sigmaX=max(1.3,rw*.012),sigmaY=max(1.3,rh*.012))
    frame[y0:y1,x0:x1]=(roi*(1-blend[...,None])+warped*blend[...,None]).astype(np.uint8)

def _fx_box_lip_tint(frame,x,y,w,h,amount):
    amount=max(0.0,min(1.0,float(amount)))
    if amount<=0:return
    x0,y0=max(0,int(x)),max(0,int(y)); x1,y1=min(frame.shape[1],int(x+w)),min(frame.shape[0],int(y+h)); rw,rh=x1-x0,y1-y0
    if rw<32 or rh<32:return
    yy,xx=np.mgrid[0:rh,0:rw].astype(np.float32); cx=rw*.5; cy=rh*.74
    mask=np.exp(-(((xx-cx)/(rw*.20))**2+((yy-cy)/(rh*.075))**2)*2.2)
    roi=frame[y0:y1,x0:x1].copy(); a=mask*(amount*.35); tint=np.zeros_like(roi); tint[:]=np.array([105,58,158],dtype=np.uint8)
    frame[y0:y1,x0:x1]=(roi.astype(np.float32)*(1-a[...,None])+tint.astype(np.float32)*a[...,None]).astype(np.uint8)

def _fx_skin_retouch(frame, landmarks, box, smooth, wrinkle, bright):
    if not landmarks:return
    x,y,w,h=box
    mask=_fx_retouch_mask(frame,landmarks,box)
    if not np.any(mask):return
    x0,y0=max(0,int(x)),max(0,int(y)); x1,y1=min(frame.shape[1],int(x+w)),min(frame.shape[0],int(y+h))
    roi=frame[y0:y1,x0:x1].copy()
    m=mask.astype(np.float32)/255.0
    if smooth>0:
        # Bilateral filtering preserves broad facial structure better than a full blur.
        sigma_space=3.0+smooth*.035
        sm=cv2.bilateralFilter(roi,0,22+smooth*.55,sigma_space)
        a=(m*(smooth/100.0)*.62)[...,None]
        roi=(roi*(1-a)+sm*a).astype(np.uint8)
    if wrinkle>0:
        gray=cv2.cvtColor(roi,cv2.COLOR_BGR2GRAY)
        kernel=cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(9,5))
        blackhat=cv2.morphologyEx(gray,cv2.MORPH_BLACKHAT,kernel).astype(np.float32)
        blackhat=cv2.GaussianBlur(blackhat,(0,0),1.0)
        lift=np.clip(blackhat*(wrinkle/100.0)*.58,0,22)
        a=(m*(wrinkle/100.0))[...,None]
        roi=np.clip(roi.astype(np.float32)+lift[...,None]*a,0,255).astype(np.uint8)
    if bright>0:
        lab=cv2.cvtColor(roi,cv2.COLOR_BGR2LAB).astype(np.float32)
        lab[:,:,0]=np.clip(lab[:,:,0]+(8.0+bright*.07)*m,0,255)
        roi=cv2.cvtColor(lab.astype(np.uint8),cv2.COLOR_LAB2BGR)
        hsv=cv2.cvtColor(roi,cv2.COLOR_BGR2HSV).astype(np.float32)
        hsv[:,:,1]=np.clip(hsv[:,:,1]*(1.0+bright*.0018*m),0,255)
        roi=cv2.cvtColor(hsv.astype(np.uint8),cv2.COLOR_HSV2BGR)
    frame[y0:y1,x0:x1]=roi

def _fx_lip_tint(frame, landmarks, box, amount):
    amount=max(0.0,min(1.0,float(amount)))
    if amount<=0:return
    mask,(x0,y0,x1,y1)=_fx_lip_mask(frame,landmarks,box)
    if not np.any(mask):return
    roi=frame[y0:y1,x0:x1].copy()
    a=(mask.astype(np.float32)/255.0)*(amount*.45)
    # Natural rose tint: preserve the original lip luminance/texture instead of
    # replacing hue outright, which can create neon/cyan artifacts on video.
    tint=np.zeros_like(roi)
    tint[:]=np.array([105,58,158],dtype=np.uint8)
    blended=roi.astype(np.float32)*(1-a[...,None])+tint.astype(np.float32)*a[...,None]
    # Recover a little of the original brightness so the lip keeps its shading.
    lab=cv2.cvtColor(blended.astype(np.uint8),cv2.COLOR_BGR2LAB).astype(np.float32)
    original_lab=cv2.cvtColor(roi,cv2.COLOR_BGR2LAB).astype(np.float32)
    lab[:,:,0]=original_lab[:,:,0]*(1-a)+lab[:,:,0]*a
    frame[y0:y1,x0:x1]=cv2.cvtColor(np.clip(lab,0,255).astype(np.uint8),cv2.COLOR_LAB2BGR)

def _fx_mesh_warp_vertical(frame, box, landmarks, amount, region="face_length"):
    """Vertical Face-Mesh geometry adjustment for forehead/face length."""
    amount=max(-1.0,min(1.0,float(amount)))
    if abs(amount)<=.001 or not landmarks or len(landmarks)<100:return
    x,y,w,h=box
    x0,y0=max(0,int(x)),max(0,int(y)); x1,y1=min(frame.shape[1],int(x+w)),min(frame.shape[0],int(y+h))
    rw,rh=x1-x0,y1-y0
    pts=np.asarray(landmarks,np.float32)[:,:2].copy()
    pts[:,0]-=x0; pts[:,1]-=y0
    good=np.isfinite(pts).all(axis=1)
    if good.sum()<100:return
    top=float(np.percentile(pts[good,1],8)); bottom=float(np.percentile(pts[good,1],92))
    yy,xx=np.mgrid[0:rh,0:rw].astype(np.float32)
    rely=np.clip((yy-top)/max(1.0,bottom-top),0,1)
    configs={
        "face_length":(.08,.96,.52,.085),
        "forehead_height":(.02,.39,.20,.075),
    }
    if region not in configs:return
    y0r,y1r,centre,max_frac=configs[region]
    zone=np.clip((rely-y0r)/max(.01,y1r-y0r),0,1)
    zone=zone*(1-zone)
    if region=="forehead_height":
        zone=np.clip(1.0-np.maximum(0,rely-.37)/.20,0,1)
    fall=np.clip(1.0-np.abs(rely-centre)/max(.12,(y1r-y0r)*.72),0,1)
    alpha=np.zeros((rh,rw),np.float32)
    hull=cv2.convexHull(pts[good].astype(np.float32))
    cv2.fillConvexPoly(alpha,hull.astype(np.int32),1)
    alpha=cv2.GaussianBlur(alpha,(0,0),sigmaX=max(1.2,rw*.008),sigmaY=max(1.2,rh*.008))
    protected=np.zeros((rh,rw),np.float32)
    protect_groups=((33,133,159,145,160,144,158,153,157,173),(362,263,386,374,387,373,385,380,384,398),(61,146,91,181,84,17,314,405,321,375,291,308,324,318,402,317,14,87,178,88,95),(1,2,4,5,6,98,195,197,327))
    for ids in protect_groups:
        ids=[i for i in ids if i<len(pts) and np.isfinite(pts[i]).all()]
        if not ids:continue
        p=pts[ids]; cc=p.mean(axis=0); sx=max(8.,float(np.ptp(p[:,0]))*.24+8.); sy=max(8.,float(np.ptp(p[:,1]))*.24+8.)
        protected=np.maximum(protected,np.exp(-(((xx-cc[0])/sx)**2+((yy-cc[1])/sy)**2)*2.2))
    scale=1.0+(max_frac*amount*fall*(1-protected))
    map_y=centre+(yy-centre)/np.clip(scale,.80,1.20)
    map_x=xx
    original=frame[y0:y1,x0:x1].copy()
    warped=cv2.remap(original,map_x.astype(np.float32),map_y.astype(np.float32),cv2.INTER_LINEAR,borderMode=cv2.BORDER_REFLECT101)
    blend=np.clip(alpha*zone,0,1)
    frame[y0:y1,x0:x1]=(original*(1-blend[...,None])+warped*blend[...,None]).astype(np.uint8)

def _fx_jaw_chin_detail(frame, box, landmarks, amount, mode):
    """Fine lower-face geometry. Each mode owns a small anatomical zone."""
    amount=max(-1.0,min(1.0,float(amount)))
    if abs(amount)<=.001 or not landmarks or len(landmarks)<100:return
    x,y,w,h=box
    x0,y0=max(0,int(x)),max(0,int(y)); x1,y1=min(frame.shape[1],int(x+w)),min(frame.shape[0],int(y+h))
    rw,rh=x1-x0,y1-y0
    pts=np.asarray(landmarks,np.float32)[:,:2].copy()
    pts[:,0]-=x0; pts[:,1]-=y0
    good=np.isfinite(pts).all(axis=1)
    if good.sum()<100:return
    top=float(np.percentile(pts[good,1],8)); bottom=float(np.percentile(pts[good,1],96))
    cx=float(np.median(pts[good,0]))
    yy,xx=np.mgrid[0:rh,0:rw].astype(np.float32)
    ry=np.clip((yy-top)/max(1.0,bottom-top),0,1)
    rx=np.abs(xx-cx)/max(20.0,float(np.percentile(pts[good,0],92)-np.percentile(pts[good,0],8))/2.0)
    lower=np.clip((ry-.62)/.38,0,1)
    lower=lower*lower*(3-2*lower)
    centre=np.clip(1-rx/0.72,0,1)
    side=np.clip((rx-.30)/.62,0,1)
    side=side*side*(3-2*side)
    if mode=="jaw_curve":
        # Positive = deeper U curve: sides rise relative to the centre.
        shift=-amount*rh*.055*lower*(side**.8)
        map_y=yy+shift
        mask=lower*np.clip(1-rx/1.05,0,1)
    elif mode=="jaw_angle":
        # Positive = sharper upward jaw corners.
        shift=-amount*rh*.050*lower*side
        map_y=yy+shift
        mask=lower*side
    elif mode=="chin_height":
        cury=top+(bottom-top)*.80
        fall=np.exp(-(((yy-cury)/(max(10,rh*.17)))**2+((xx-cx)/(max(12,rw*.30)))**2)*2.0)
        scale=1+amount*.16*fall
        map_y=cury+(yy-cury)/np.clip(scale,.82,1.18)
        mask=fall*lower
    elif mode=="chin_roundness":
        fall=np.exp(-(((yy-(top+(bottom-top)*.84))/(max(9,rh*.14)))**2+((xx-cx)/(max(14,rw*.34)))**2)*2.0)
        strength=1+amount*.14*fall
        map_x=cx+(xx-cx)/np.clip(strength,.84,1.16)
        map_y=yy
        original=frame[y0:y1,x0:x1].copy()
        warped=cv2.remap(original,map_x.astype(np.float32),map_y.astype(np.float32),cv2.INTER_LINEAR,borderMode=cv2.BORDER_REFLECT101)
        blend=cv2.GaussianBlur(np.clip(fall*lower,0,1),(0,0),max(1.0,rw*.008))
        frame[y0:y1,x0:x1]=(original*(1-blend[...,None])+warped*blend[...,None]).astype(np.uint8)
        return
    elif mode=="chin_tip":
        # Positive = slightly longer/downward lower tip; negative = shorter.
        shift=amount*rh*.045*centre*lower*np.exp(-((ry-.84)/.20)**2)
        map_y=yy+shift
        mask=centre*lower
    else:
        return
    alpha=np.zeros((rh,rw),np.float32)
    hull=cv2.convexHull(pts[good].astype(np.float32))
    cv2.fillConvexPoly(alpha,hull.astype(np.int32),1)
    blend=cv2.GaussianBlur(np.clip(alpha*mask,0,1),(0,0),max(1.0,rw*.008))
    original=frame[y0:y1,x0:x1].copy()
    warped=cv2.remap(original,xx.astype(np.float32),map_y.astype(np.float32),cv2.INTER_LINEAR,borderMode=cv2.BORDER_REFLECT101)
    frame[y0:y1,x0:x1]=(original*(1-blend[...,None])+warped*blend[...,None]).astype(np.uint8)

def _apply_video_fx(frame,fx):
    if not fx: return frame
    f={**FX_DEFAULT,**fx}
    face_amount=max(
        abs(float(f["skin_smooth"])),abs(float(f["wrinkle_remove"])),
        abs(float(f["skin_bright"])),abs(float(f["lip_tint"])),
        abs(float(f["face_slim"])),abs(float(f["forehead_width"])),
        abs(float(f["forehead_height"])),abs(float(f["midface_width"])),
        abs(float(f["cheekbone"])),abs(float(f["temple"])),
        abs(float(f["face_length"])),abs(float(f["jaw_refine"])),abs(float(f["jaw_width"])),
        abs(float(f["jaw_curve"])),abs(float(f["jaw_angle"])),abs(float(f["chin"])),
        abs(float(f["chin_height"])),abs(float(f["chin_roundness"])),abs(float(f["chin_tip"])),abs(float(f["eye_size"]))
    )
    original_h, original_w=frame.shape[:2]
    fx_scale=1.0
    if face_amount>0.5 and original_w>640:
        fx_scale=640.0/float(original_w)
        out=cv2.resize(frame,(640,max(1,int(round(original_h*fx_scale)))),interpolation=cv2.INTER_AREA)
    else:
        out=frame.copy()
    b=float(f["brightness"]); c=float(f["contrast"]); sat=float(f["saturation"]); warm=float(f["warmth"])
    if abs(b)>0.1 or abs(c-1.0)>0.001:
        out=cv2.convertScaleAbs(out,alpha=c,beta=b)
    if abs(sat-1.0)>0.001 or abs(warm)>0.001:
        hsv=cv2.cvtColor(out,cv2.COLOR_BGR2HSV).astype(np.float32)
        hsv[:,:,1]=np.clip(hsv[:,:,1]*sat,0,255)
        out=cv2.cvtColor(hsv.astype(np.uint8),cv2.COLOR_HSV2BGR)
        if abs(warm)>0.001:
            wa=max(-1.0,min(1.0,warm))*18.0
            wb=out.astype(np.int16); wb[:,:,2]=np.clip(wb[:,:,2]+wa,0,255); wb[:,:,0]=np.clip(wb[:,:,0]-wa,0,255)
            out=wb.astype(np.uint8)
    faces,eyes=_fx_detect_faces(out) if face_amount>0.5 else ([],[])
    smooth=max(0.0,min(100.0,float(f["skin_smooth"])))
    wrinkle=max(0.0,min(100.0,float(f["wrinkle_remove"])))
    bright=max(0.0,min(100.0,float(f["skin_bright"])))
    lip=max(0.0,min(100.0,float(f["lip_tint"])))/100.0
    for fi,(x,y,w,h) in enumerate(faces[:4]):
        lm=_fx_face_landmarks[fi] if fi < len(_fx_face_landmarks) else None
        if lm is not None and len(lm)>=100:
            if smooth>0 or wrinkle>0 or bright>0:
                _fx_skin_retouch(out,lm,(x,y,w,h),smooth,wrinkle,bright)
            if lip>0:
                _fx_lip_tint(out,lm,(x,y,w,h),lip)
        elif smooth>0 or wrinkle>0 or bright>0:
            _fx_skin_retouch_box(out,(x,y,w,h),smooth,wrinkle,bright,eyes)
        if lm is None or len(lm)<100:
            if lip>0:
                _fx_box_lip_tint(out,x,y,w,h,lip)
        # Head/face geometry controls remain independent by zone.
        _fx_remap_region(out,x,y,w,h,float(f["face_slim"])/100.0,"face",lm)
        _fx_remap_region(out,x,y,w,h,float(f["forehead_width"])/100.0,"forehead",lm)
        _fx_remap_region(out,x,y,w,h,float(f["midface_width"])/100.0,"midface",lm)
        _fx_remap_region(out,x,y,w,h,float(f["cheekbone"])/100.0,"cheekbone",lm)
        _fx_remap_region(out,x,y,w,h,float(f["temple"])/100.0,"temple",lm)
        _fx_remap_region(out,x,y,w,h,float(f["jaw_refine"])/100.0,"jaw",lm)
        _fx_remap_region(out,x,y,w,h,float(f["jaw_width"])/100.0,"jaw",lm)
        _fx_remap_region(out,x,y,w,h,float(f["chin"])/100.0,"chin",lm)
        if lm is not None and len(lm)>=100:
            _fx_mesh_warp_vertical(out,(x,y,w,h),lm,float(f["forehead_height"])/100.0,"forehead_height")
            _fx_jaw_chin_detail(out,(x,y,w,h),lm,float(f["jaw_curve"])/100.0,"jaw_curve")
            _fx_jaw_chin_detail(out,(x,y,w,h),lm,float(f["jaw_angle"])/100.0,"jaw_angle")
            _fx_jaw_chin_detail(out,(x,y,w,h),lm,float(f["chin_height"])/100.0,"chin_height")
            _fx_jaw_chin_detail(out,(x,y,w,h),lm,float(f["chin_roundness"])/100.0,"chin_roundness")
            _fx_jaw_chin_detail(out,(x,y,w,h),lm,float(f["chin_tip"])/100.0,"chin_tip")
            _fx_mesh_warp_vertical(out,(x,y,w,h),lm,float(f["face_length"])/100.0,"face_length")
        else:
            _fx_box_vertical_detail(out,x,y,w,h,float(f["forehead_height"])/100.0,"forehead_height")
            _fx_box_lower_detail(out,x,y,w,h,float(f["jaw_curve"])/100.0,"jaw_curve")
            _fx_box_lower_detail(out,x,y,w,h,float(f["jaw_angle"])/100.0,"jaw_angle")
            _fx_box_lower_detail(out,x,y,w,h,float(f["chin_height"])/100.0,"chin_height")
            _fx_box_lower_detail(out,x,y,w,h,float(f["chin_roundness"])/100.0,"chin_roundness")
            _fx_box_lower_detail(out,x,y,w,h,float(f["chin_tip"])/100.0,"chin_tip")
            _fx_box_vertical_detail(out,x,y,w,h,float(f["face_length"])/100.0,"face_length")
        eye_amount=float(f["eye_size"])/100.0
        if abs(eye_amount)>0.001:
            if lm is not None and len(lm)>=478:
                _fx_enlarge_eye_mesh(out,lm,eye_amount)
            else:
                _fx_enlarge_eye_box(out,eyes,eye_amount)
    if fx_scale!=1.0:
        out=cv2.resize(out,(original_w,original_h),interpolation=cv2.INTER_LANCZOS4)
    sharp=max(0.0,min(100.0,float(f["sharpen"])))
    if sharp>0:
        blur=cv2.GaussianBlur(out,(0,0),1.15)
        out=cv2.addWeighted(out,1.0+sharp/140.0,blur,-sharp/140.0,0)
    vign=max(0.0,min(100.0,float(f["vignette"])))
    if vign>0:
        h,w=out.shape[:2]; yy,xx=np.mgrid[0:h,0:w].astype(np.float32)
        dx=(xx-w/2)/(w/2); dy=(yy-h/2)/(h/2)
        mask=np.clip(1.0-(dx*dx+dy*dy)*0.55,0.25,1.0)
        mask=1.0-(1.0-mask)*(vign/100.0)*0.8
        out=np.clip(out.astype(np.float32)*mask[:,:,None],0,255).astype(np.uint8)
    return out

def _select_source_playback_frame():
    global source_prepared_frame, source_prepared_serial, source_playback_origin
    # Select by presentation clock, not by whichever frame the decoder happened
    # to finish most recently. This keeps 30 FPS MP4 playback stable while the
    # output may run at 30 or 60 FPS.
    with source_buffer_lock:
        if not source_buffer:
            return
        if source_playback_origin is None:
            source_playback_origin = time.monotonic()
        elapsed = max(0.0, time.monotonic() - source_playback_origin)
        target_index = int(elapsed * max(1.0, source_fps))
        chosen = None
        for seq, prepared in source_buffer:
            if seq <= target_index:
                chosen = prepared
            else:
                break
        if chosen is None:
            chosen = source_buffer[0][1]
        # Drop frames that are definitely behind the presentation point and
        # free buffer space for the decoder to keep reading ahead.
        while len(source_buffer) > 1 and source_buffer[1][0] <= target_index:
            source_buffer.popleft()
        source_buffer_condition.notify_all()
        with source_frame_lock:
            if chosen is not source_prepared_frame:
                source_prepared_frame = chosen
                source_prepared_serial += 1

def _update_loop_duration(frames):
    """Real length of one video loop (frames / fps); audio loops use the same."""
    global source_loop_duration
    try:
        d = float(frames) / max(1.0, float(source_fps))
        if d > 0.2:
            source_loop_duration = d
            vaudio.loop_duration = d
    except Exception:
        pass

def source_decode_worker(local_cap, src, generation, worker_stop=None, initial_positioned=False, initial_decoded=None):
    global source_frame, source_frame_serial, source_prepared_frame, source_prepared_serial, source_variant
    global source_playback_origin, source_sequence_index, source_start_offset, clip_flow_eof_generation
    # Backwards-compatible fallback for internal/manual callers; normal source
    # selection always passes a dedicated Event.
    worker_stop = worker_stop if worker_stop is not None else source_decode_stop
    was_running = False
    positioned_on_open = bool(initial_positioned)
    pass_frames = 0
    while not stop_event.is_set() and not worker_stop.is_set():
        with lock:
            current = state.get("source") == src and source_decode_generation == generation
            loop_enabled = bool(state.get("loop", True))
            running = bool(state.get("running", False))
        with clip_flow_lock:
            flow_owns_source = (
                bool(clip_flow_runtime.get("active"))
                and str(clip_flow_runtime.get("current_clip") or "") == str(src)
                and clip_flow_runtime.get("phase") in ("open", "normal", "seam", "stop")
            )
        if flow_owns_source:
            # Playlist clips must play exactly once. The controller advances only
            # after this decoder reports EOF, keeping video/audio phase transitions
            # tied to actual media completion instead of guessed wall-clock timing.
            loop_enabled = False
        if worker_stop.is_set() or not current:
            break
        if not running:
            with source_frame_lock:
                have_preview = source_frame is not None
            if not have_preview:
                ok, decoded = local_cap.read()
                if worker_stop.is_set():
                    break
                if ok:
                    with source_frame_lock:
                        # Source switches set this Event before clearing shared
                        # frame state. Check it again after acquiring the lock.
                        if worker_stop.is_set():
                            break
                        source_frame = decoded
                        source_frame_serial += 1
            was_running = False
            time.sleep(0.04)
            continue

        if not was_running:
            # Reuse the pre-positioned new capture on a seek; on a later pause /
            # resume, restore the existing engine's established restart behavior.
            with source_buffer_lock:
                start_offset = max(0.0, float(source_start_offset or 0.0))
            if positioned_on_open:
                positioned_on_open = False
            elif start_offset > 0.001:
                if not _seek_capture_to_seconds(local_cap, start_offset, source_fps):
                    with lock:
                        if state.get("source") == src and source_decode_generation == generation:
                            state["virtualcam_error"] = "วิดีโอไม่รองรับการเลื่อนไปยังตำแหน่งที่ขอ"
                    break
            else:
                try:
                    local_cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                except Exception:
                    pass
            with source_buffer_lock:
                source_buffer.clear()
                if source_playback_origin is None:
                    source_playback_origin = time.monotonic() + MEDIA_START_LEAD_SEC - start_offset
                source_sequence_index = int(round(start_offset * max(1.0, float(source_fps))))
            # Do not clear source_prepared_frame here. On a source handoff or
            # pause/resume, keep the most recent valid prepared frame on output
            # until _select_source_playback_frame() can replace it with a frame
            # from this decoder. An empty decode buffer is not a black frame.
            was_running = True
            pass_frames = 0

        with source_buffer_condition:
            buffer_full = len(source_buffer) >= SOURCE_BUFFER_MAX
        if buffer_full:
            _select_source_playback_frame()
            with source_buffer_condition:
                if len(source_buffer) >= SOURCE_BUFFER_MAX:
                    source_buffer_condition.wait(timeout=0.05)
            continue

        # Low-spec catch-up: if decoding fell behind the presentation clock,
        # skip (grab, no color-convert / resize) instead of drifting out of sync
        # with the audio.
        if worker_stop.is_set():
            break
        with source_buffer_lock:
            _origin = source_playback_origin
            _seq_now = source_sequence_index
        if worker_stop.is_set():
            break
        if _origin is not None:
            _target = int(max(0.0, time.monotonic() - _origin) * max(1.0, source_fps))
            _behind = _target - _seq_now
            if _behind > 2:
                _skipped = 0
                while _skipped < min(_behind - 1, 45) and not worker_stop.is_set():
                    if not local_cap.grab():
                        break
                    _skipped += 1
                if worker_stop.is_set():
                    break
                if _skipped:
                    with source_buffer_lock:
                        # A switch sets worker_stop before resetting the shared
                        # sequence index. Never let the retired decoder increment it.
                        if worker_stop.is_set():
                            break
                        source_sequence_index += _skipped
                    pass_frames += _skipped

        if initial_decoded is not None:
            # The primer decoded the first frame while the preceding clip was
            # still on air; publish it immediately instead of stalling at read().
            decoded = initial_decoded
            initial_decoded = None
            ok = True
        else:
            ok, decoded = local_cap.read()
        if worker_stop.is_set():
            break
        if not ok:
            if loop_enabled:
                if pass_frames > 0:
                    _update_loop_duration(pass_frames)
                pass_frames = 0
                try:
                    with lock:
                        source_variant = _new_loop_variant()
                        state["loop_count"] = int(state.get("loop_count", 0)) + 1
                    local_cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ok, decoded = local_cap.read()
                except Exception:
                    ok = False
                if ok:
                    with source_frame_lock:
                        source_frame = decoded
                        source_frame_serial += 1
                if not ok:
                    time.sleep(0.01)
                    continue
            else:
                # EOF events carry the decoder generation, not just a path. This
                # matters when Open/Normal/Stop all reference the same MP4.
                with lock:
                    still_current = (
                        state.get("source") == src
                        and source_decode_generation == generation
                    )
                    running_at_eof = bool(state.get("running", False))
                with clip_flow_lock:
                    flow_owns_source = (
                        still_current and running_at_eof
                        and bool(clip_flow_runtime.get("active"))
                        and str(clip_flow_runtime.get("current_clip") or "") == str(src)
                        and clip_flow_runtime.get("phase") in ("open", "normal", "seam", "stop")
                        and clip_flow_runtime.get("source_generation") == generation
                    )
                    flow_is_switching = bool(clip_flow_runtime.get("active"))
                    if flow_owns_source:
                        clip_flow_eof_generation = generation
                        clip_flow_eof_event.set()
                if not still_current or not running_at_eof:
                    break
                if flow_owns_source:
                    break
                if flow_is_switching:
                    # A different flow clip is being installed. The old decoder
                    # must never change the new Engine state or advance the queue.
                    break
                # Ordinary non-looping source reached EOF.
                with lock:
                    if state.get("source") == src and source_decode_generation == generation:
                        state["running"] = False
                try:
                    vaudio.stop()
                except Exception:
                    pass
                break

        pass_frames += 1
        with lock:
            render_w = max(1, int(state.get("source_width", 720)))
            render_h = max(1, int(state.get("source_height", 405)))
            render_variant = dict(source_variant)
            render_fit = str(state.get("source_fit_mode", "cover"))
            render_flip_h = bool(state.get("source_flip_h", False))
            render_flip_v = bool(state.get("source_flip_v", False))
            still_current = state.get("source") == src and source_decode_generation == generation
        if worker_stop.is_set() or not still_current:
            break

        prepared = _prepare_source_render_frame(
            decoded, render_w, render_h, render_variant,
            render_fit, render_flip_h, render_flip_v, None
        )
        if worker_stop.is_set():
            break
        # Publish frame metadata and its sequence index under one consistent
        # buffer -> frame lock order. If a source switch wins the buffer lock,
        # the old decoder sees its stop token and cannot pollute the new buffer.
        with source_buffer_condition:
            if worker_stop.is_set():
                break
            seq = source_sequence_index
            source_sequence_index += 1
            source_buffer.append((seq, prepared))
            with source_frame_lock:
                if not worker_stop.is_set():
                    source_frame = decoded
                    source_frame_serial += 1
            source_buffer_condition.notify_all()
        if worker_stop.is_set():
            break
        _select_source_playback_frame()

    try:
        if local_cap is cap:
            return
    except Exception:
        pass

def fit_source_to_output(vw=None, vh=None):
    # Caller owns the state lock when this helper is used from settings.
    if not state.get("source"):
        return
    if vw is None or vh is None:
        if cap is None or not cap.isOpened():
            return
        vw = max(1, int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1))
        vh = max(1, int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 1))
    ow, oh = int(state["width"]), int(state["height"])
    # Keep the media aspect ratio whenever output settings change.
    fit = min(1.0, ow / float(vw), oh / float(vh))
    sw, sh = max(40, int(round(vw * fit))), max(40, int(round(vh * fit)))
    state["source_width"], state["source_height"] = sw, sh
    state["source_aspect"] = float(vw) / float(vh)
    state["source_x"], state["source_y"] = 0, 0

def _overlay_compose(ov, fw, fh, fit, zoom, panx, pany):
    """Render an overlay image into a (fw x fh) BGRA frame.
    stretch = fill frame (distorts); contain/cover = keep aspect; free = natural size.
    zoom multiplies the scale; pan moves the image inside the frame (output px).
    Enlarging uses cubic interpolation, shrinking uses area interpolation."""
    if ov.ndim == 2:
        ov = cv2.cvtColor(ov, cv2.COLOR_GRAY2BGRA)
    elif ov.shape[2] == 3:
        ov = cv2.cvtColor(ov, cv2.COLOR_BGR2BGRA)
    ih, iw = ov.shape[:2]
    zoom = max(0.05, min(20.0, float(zoom or 1.0)))
    if fit == "stretch":
        sx, sy = fw / float(iw), fh / float(ih)
    elif fit == "cover":
        sx = sy = max(fw / float(iw), fh / float(ih))
    elif fit == "free":
        sx = sy = 1.0
    else:
        sx = sy = min(fw / float(iw), fh / float(ih))
    sx *= zoom; sy *= zoom
    dw, dh = iw * sx, ih * sy
    if fit == "free":
        tx, ty = float(panx), float(pany)
    else:
        tx, ty = (fw - dw) / 2.0 + panx, (fh - dh) / 2.0 + pany
    src = ov
    if sx < 1.0 or sy < 1.0:
        nw, nh = max(1, int(round(dw))), max(1, int(round(dh)))
        src = cv2.resize(ov, (nw, nh), interpolation=cv2.INTER_AREA)
        M = np.float32([[1, 0, tx], [0, 1, ty]])
    else:
        M = np.float32([[sx, 0, tx], [0, sy, ty]])
    return cv2.warpAffine(src, M, (fw, fh), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))

def render_frame():
    global source_frame, source_render_cache, source_render_cache_key
    global composed_frame_cache, composed_frame_cache_key
    global last_preview_encode, preview_interval
    with lock:
        running = state["running"]
        src = state["source"]
        source_enabled = bool(state["source_enabled"])
        loop = state["loop"]
        text = state["text"]
        text_enabled = bool(state["text_enabled"])
        legacy_text_added = bool(state.get("legacy_text_added", False))
        text_x, text_y = int(state["text_x"]), int(state["text_y"])
        img_path = state["image"]
        image_enabled = bool(state["image_enabled"])
        image_x, image_y = int(state["image_x"]), int(state["image_y"])
        image_width = max(1, int(state["image_width"]))
        image_height = max(1, int(state["image_height"]))
        image_fit = str(state.get("image_fit", "contain"))
        image_zoom = float(state.get("image_zoom", 1.0) or 1.0)
        image_pan_x = int(state.get("image_pan_x", 0) or 0)
        image_pan_y = int(state.get("image_pan_y", 0) or 0)
        w, h = int(state["width"]), int(state["height"])
        source_x, source_y = int(state.get("source_x", 0)), int(state.get("source_y", 0))
        source_width = max(1, int(state.get("source_width", w)))
        source_height = max(1, int(state.get("source_height", h)))
        fps = _effective_output_fps(source_rate_hint=source_fps)
        text_size = max(6, int(state.get("text_size", 28)))
        texts = list(state.get("texts", []))
        order = list(state.get("layer_order", []))
        # Video FX are intentionally NOT part of the live compositor.
        # They are rendered offline by /api/video-filter/export.
        web_enabled = bool(state.get("web_enabled", False)) and bool(str(state.get("web_url") or "").strip()) and ("web" in order)
        web_x, web_y = int(state.get("web_x", 0)), int(state.get("web_y", 0))
        web_width = max(1, int(state.get("web_width", w)))
        web_height = max(1, int(state.get("web_height", h)))
    # Select the frame from the timestamped source buffer. The renderer is
    # independent of decode timing, so a short decode/CPU spike does not directly
    # turn into a visible hitch.
    if running and src and source_enabled:
        _select_source_playback_frame()
    with source_frame_lock:
        render_source_frame = source_prepared_frame
        render_serial = source_prepared_serial

    # Live compositor uses the decoded/prepared frame directly.
    # Beauty/Face/Filter work is offline-only in Video to Filter.

    with web_frame_lock:
        current_web = web_frame
        current_web_serial = web_frame_serial

    marquee_active = bool(text_enabled and "text" in order and any(isinstance(item, dict) and item.get("enabled", True) and item.get("scroll") and str(item.get("text") or "") for item in texts))
    marquee_tick = int((time.monotonic() - TEXT_SCROLL_EPOCH) * max(1, fps)) if marquee_active else 0
    compose_key = (
        render_serial, current_web_serial, w, h, source_x, source_y,
        source_width, source_height, bool(source_enabled), bool(web_enabled),
        web_x, web_y, web_width, web_height, bool(image_enabled),
        image_x, image_y, image_width, image_height,
        image_fit, image_zoom, image_pan_x, image_pan_y,
        text_x, text_y, text_size, bool(text_enabled), bool(legacy_text_added), text,
        tuple(tuple(sorted(t.items())) for t in texts), marquee_tick,
        tuple(order), img_path
    )
    if composed_frame_cache_key == compose_key and composed_frame_cache is not None:
        return composed_frame_cache, fps
    web_cache_key = (current_web_serial, web_width, web_height)
    render_web_frame = None
    if current_web is not None:
        global web_render_cache, web_render_cache_key
        if web_render_cache_key != web_cache_key:
            work_web = current_web
            if work_web.shape[1] != web_width or work_web.shape[0] != web_height:
                work_web = cv2.resize(work_web, (web_width, web_height), interpolation=cv2.INTER_AREA)
            web_render_cache = np.ascontiguousarray(work_web)
            web_render_cache_key = web_cache_key
        render_web_frame = web_render_cache

    if running and src and source_enabled and render_source_frame is None:
        # Decoder is still warming up. Keep a neutral frame only until the first
        # real source frame arrives; subsequent render frames always reuse the
        # last good decoded frame.
        render_source_frame = np.zeros((h, w, 3), np.uint8)
    if render_source_frame is None:
        render_source_frame = np.zeros((h, w, 3), np.uint8)
    # Source transform is cached by decoded-frame serial. At 60 FPS output
    # with a 29.97 FPS source, the same source frame can be reused without another
    # crop/resize operation.
    frame = np.zeros((h, w, 3), np.uint8)
    legacy_text = {"id":"legacy","text":text,"x":text_x,"y":text_y,"size":text_size,"enabled":bool(text_enabled and legacy_text_added),"scroll":False}
    all_texts = ([legacy_text] if legacy_text_added else []) + texts
    def draw_texts(dst, items):
        # Cache each rendered glyph sprite, then blend only the small text ROI.
        # This avoids two full-frame color conversions and PIL image allocation
        # every output frame. Keep the historical coordinates/stroke width: the
        # transparent sprite border is offset back out before blending.
        for item in items:
            value = item.get("text", "")
            if not item.get("enabled", True) or not value:
                continue
            size = max(6, min(400, int(item.get("size", text_size))))
            stroke = max(1, min(20, int(item.get("stroke", 2))))
            y = int(item.get("y", 56))
            sp = cached_text_sprite(str(value), size, color="#ffffff", bold=False, outline=True, stroke=stroke)
            pad = stroke + 2
            if item.get("scroll") and item.get("id") != "legacy":
                try: speed = max(20, min(600, int(float(item.get("scroll_speed", 120)))))
                except (TypeError, ValueError, OverflowError): speed = 120
                travel = max(1, int((time.monotonic() - TEXT_SCROLL_EPOCH) * speed))
                x = w - (travel % (w + sp.shape[1]))
            else:
                x = int(item.get("x", 24))
            blend_text_sprite(dst, sp, x - pad, y - pad)
    def draw_source(dst):
        if not source_enabled or render_source_frame is None: return
        x,y=int(source_x),int(source_y)
        sh,sw=render_source_frame.shape[:2]
        x0,y0=max(0,x),max(0,y); x1,y1=min(w,x+sw),min(h,y+sh)
        if x0>=x1 or y0>=y1:return
        dst[y0:y1,x0:x1]=render_source_frame[y0-y:y1-y,x0-x:x1-x]
    def draw_web(dst):
        if not web_enabled or render_web_frame is None:
            return
        x,y=int(web_x),int(web_y)
        sh,sw=render_web_frame.shape[:2]
        x0,y0=max(0,x),max(0,y); x1,y1=min(w,x+sw),min(h,y+sh)
        if x0>=x1 or y0>=y1:return
        dst[y0:y1,x0:x1]=render_web_frame[y0-y:y1-y,x0-x:x1-x]

    def draw_image(dst):
        global overlay_cache_path, overlay_cache_mtime, overlay_cache
        global overlay_render_cache_key, overlay_render_cache
        if not image_enabled or not img_path: return
        p = Path(img_path)
        if not p.exists(): return
        try: mtime = p.stat().st_mtime_ns
        except OSError: return
        if overlay_cache_path != img_path or overlay_cache_mtime != mtime or overlay_cache is None:
            ov = cv2.imread(img_path, cv2.IMREAD_UNCHANGED)
            if ov is None: return
            overlay_cache_path, overlay_cache_mtime, overlay_cache = img_path, mtime, ov
            overlay_render_cache_key = None
            overlay_render_cache = None
        ow, oh = max(1,image_width), max(1,image_height)
        render_key = (img_path, mtime, ow, oh, image_fit, round(image_zoom, 4), image_pan_x, image_pan_y)
        if overlay_render_cache_key != render_key or overlay_render_cache is None:
            overlay_render_cache = _overlay_compose(overlay_cache, ow, oh, image_fit, image_zoom, image_pan_x, image_pan_y)
            overlay_render_cache_key = render_key
        ov = overlay_render_cache
        x,y = int(image_x),int(image_y)
        x0,y0=max(0,x),max(0,y); x1,y1=min(w,x+ow),min(h,y+oh)
        if x0>=x1 or y0>=y1: return
        crop=ov[y0-y:y1-y,x0-x:x1-x]
        if crop.shape[2]==4:
            a=crop[:,:,3:4].astype(np.float32)/255.0
            dst[y0:y1,x0:x1]=(dst[y0:y1,x0:x1].astype(np.float32)*(1-a)+crop[:,:,:3].astype(np.float32)*a).astype(np.uint8)
        else: dst[y0:y1,x0:x1]=crop[:,:,:3]
    for layer in reversed(order):
        if layer=="source": draw_source(frame)
        elif layer=="web": draw_web(frame)
        elif layer=="image": draw_image(frame)
        elif layer=="text":
            draw_texts(frame, all_texts)
    # Publish the finished frame only. Preview encoding and Virtual Camera
    # output run on separate workers, so neither can block the compositor.
    if not frame.flags.c_contiguous:
        frame = np.ascontiguousarray(frame, dtype=np.uint8)
    composed_frame_cache = frame
    composed_frame_cache_key = compose_key
    return frame, fps

def _publish_render_frame(frame):
    global latest_render_frame, latest_render_frame_serial
    with render_condition:
        latest_render_frame = frame
        latest_render_frame_serial += 1
        render_condition.notify_all()

def render_worker():
    global render_frame_count
    next_deadline = time.monotonic()
    last_running = None
    while not stop_event.is_set():
        try:
            with lock:
                running = bool(state.get("running", False))
                cam_active = bool(state.get("virtualcam", False) and vcam.active)
                out_w = int(state["width"])
                out_h = int(state["height"])
                target_fps = max(1, int(state["fps"]))
            if not running:
                # Do not burn CPU rendering blank frames while idle. When a run
                # stops, publish one black frame so the output workers do not
                # keep sending the previous program frame.
                if last_running is not False and cam_active:
                    _publish_render_frame(np.zeros((out_h, out_w, 3), dtype=np.uint8))
                last_running = False
                time.sleep(0.05)
                next_deadline = time.monotonic()
                continue

            result = render_frame()
            with render_metrics_lock:
                render_frame_count += 1
            frame, fps = result
            _publish_render_frame(frame)
            last_running = True
        except Exception as exc:
            with lock:
                state["virtualcam_error"] = str(exc)
            fps = 10
        interval = 1.0 / max(1, int(fps if 'fps' in locals() else target_fps))
        next_deadline += interval
        delay = next_deadline - time.monotonic()
        if delay > 0:
            _precise_sleep_until(next_deadline)
        else:
            next_deadline = time.monotonic()

preview_clients = 0
preview_clients_lock = threading.Lock()
_PREVIEW_ADAPT = {"w": 480, "idle": 0.0}

def preview_output_worker():
    global latest_jpeg, latest_jpeg_serial, preview_frame_count
    last_frame_obj = None
    last_render_serial = 0
    next_deadline = time.monotonic()
    while not stop_event.is_set():
        # Always retain a valid pacing interval, even if an unexpected error
        # occurs before the current frame's FPS settings can be read.
        interval = 1.0 / 30.0
        try:
            with render_condition:
                while (latest_render_frame_serial == last_render_serial and
                       not stop_event.is_set()):
                    render_condition.wait(timeout=0.5)
                frame = latest_render_frame
                last_render_serial = latest_render_frame_serial
            with lock:
                source_rate = float(source_fps or 30.0)
                camera_mode = bool(state.get("virtualcam", False) and vcam.active)
                # The program output keeps its requested FPS. When a receiving
                # app is using the virtual camera, make the *UI-only* JPEG
                # preview cheaper so it cannot compete with the output path.
                # Keep the Live Console preview at a real 24–30 FPS even while
                # Virtual Camera is active. The old 10–15 FPS cap made a smooth
                # scene look visibly choppy and was incorrectly mistaken for a
                # renderer FPS problem. Reduce preview size/quality instead of
                # reducing cadence; the camera output remains full resolution.
                preview_fps = (
                    min(30, max(24, int(round(source_rate))))
                    if camera_mode else min(60, max(24, int(round(source_rate))))
                )
            interval = 1.0 / preview_fps
            profile_max_width = 400 if camera_mode else PREVIEW_MAX_WIDTH
            preview_quality = 50 if camera_mode else 65
            # A 30 FPS MP4 feeding a 60 FPS output has repeated frames by design.
            # Do not JPEG-encode the same composed object twice; the browser keeps
            # displaying the last JPEG until the next actual frame arrives.
            _now = time.monotonic()
            _want = preview_clients > 0 or (_now - _PREVIEW_ADAPT["idle"]) >= 1.0
            if frame is not None and frame is not last_frame_obj and _want:
                _PREVIEW_ADAPT["idle"] = _now
                _t0 = time.perf_counter()
                preview = frame
                _w = max(320, min(int(_PREVIEW_ADAPT["w"]), profile_max_width))
                if preview.shape[1] > _w:
                    scale = _w / float(preview.shape[1])
                    preview = cv2.resize(
                        preview,
                        (_w, max(1, int(round(preview.shape[0] * scale)))),
                        interpolation=cv2.INTER_LINEAR if scale > 0.5 else cv2.INTER_AREA,
                    )
                ok, enc = cv2.imencode(".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, preview_quality])
                _dt = time.perf_counter() - _t0
                # Self-tuning for low-spec PCs. With a camera consumer active,
                # cap width/quality/FPS for this auxiliary preview only; the
                # Virtual Camera keeps the user's full selected output settings.
                if _dt > interval * 0.55:
                    _PREVIEW_ADAPT["w"] = max(320, int(_w * 0.85))
                elif _dt < interval * 0.25 and _w < profile_max_width:
                    _PREVIEW_ADAPT["w"] = min(profile_max_width, int(_w * 1.1) + 1)
                if ok:
                    with preview_condition:
                        latest_jpeg = enc.tobytes()
                        latest_jpeg_serial += 1
                        with render_metrics_lock:
                            preview_frame_count += 1
                        preview_condition.notify_all()
                    with lock:
                        state["preview_error"] = None
                last_frame_obj = frame
        except Exception as exc:
            # Keep the auxiliary preview worker alive, but surface a useful
            # diagnostic instead of silently swallowing every encode failure.
            with lock:
                state["preview_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
            interval = max(interval, 1.0 / 15.0)
        next_deadline += interval
        delay = next_deadline - time.monotonic()
        if delay > 0:
            _precise_sleep_until(next_deadline)
        else:
            next_deadline = time.monotonic()

def vcam_output_worker():
    # Send only genuinely new compositor frames. Resending the same frame on a
    # timeout can make the output worker run faster than the renderer and spend
    # CPU copying duplicate frames just as another app starts consuming camera.
    # A virtual-camera driver retains its last frame until the next one arrives,
    # so a keep-alive resend is unnecessary when the renderer pauses.
    global vcam_frame_count
    last_serial = 0
    while not stop_event.is_set():
        try:
            with lock:
                active = bool(state.get("virtualcam", False) and vcam.active)
            if not active:
                last_serial = 0
                time.sleep(0.05)
                continue
            with render_condition:
                if latest_render_frame_serial == last_serial:
                    render_condition.wait(timeout=0.5)
                frame = latest_render_frame
                serial = latest_render_frame_serial
            if frame is None or serial == last_serial:
                continue
            last_serial = serial
            output_frame = frame
            if libobs_bridge.active:
                # libobs runs as an optional, headless compositor stage. A single
                # failed/late frame falls back to the native direct path; repeated
                # failures are handled inside the bridge and disable it safely.
                bridged_frame = libobs_bridge.process(frame)
                bridge_status = libobs_bridge.status()
                with lock:
                    state["libobs_active"] = bool(bridge_status["active"])
                    state["libobs_error"] = bridge_status["error"]
                    state["libobs_version"] = bridge_status["version"]
                    state["libobs_latency_ms"] = bridge_status["last_latency_ms"]
                    state["libobs_frames_in"] = bridge_status["frames_in"]
                    state["libobs_frames_out"] = bridge_status["frames_out"]
                if bridged_frame is not None:
                    output_frame = bridged_frame
            if vcam.send(output_frame):
                with render_metrics_lock:
                    vcam_frame_count += 1
                with lock:
                    state["virtualcam_error"] = None
            else:
                # A Stop can win the race after this worker's active check. Do
                # not turn that expected teardown into a stale frame-send error.
                with lock:
                    still_expected = bool(state.get("virtualcam", False) and vcam.active)
                    if still_expected:
                        state["virtualcam_error"] = vcam.error or "Virtual Camera did not accept the frame"
                if still_expected:
                    time.sleep(0.01)
        except Exception as exc:
            with lock:
                state["virtualcam_error"] = str(exc)
            time.sleep(0.05)


def frame_stream(source=False, web=False, quality_mode="balanced"):
    global preview_clients
    live = (not source) and (not web)
    if live:
        with preview_clients_lock:
            preview_clients += 1
    try:
        # Capture request parameters in the route handler and pass plain values
        # into this generator. Flask's request context may end before iteration.
        yield from _frame_stream_inner(source, web, quality_mode)
    finally:
        if live:
            with preview_clients_lock:
                preview_clients = max(0, preview_clients - 1)

def _frame_stream_inner(source=False, web=False, quality_mode="balanced"):
    cached_data = None
    last_serial = -1
    while not stop_event.is_set():
        data = cached_data
        if source:
            with source_frame_lock:
                current = source_prepared_frame if source_prepared_frame is not None else source_frame
                serial = source_prepared_serial if source_prepared_frame is not None else source_frame_serial
            if current is not None and serial != last_serial:
                preview = current
                quality_mode = str(quality_mode or "balanced").lower()
                max_w = {"fast":360, "balanced":480, "sharp":640}.get(quality_mode,480)
                jpg_q = {"fast":55, "balanced":62, "sharp":72}.get(quality_mode,62)
                if preview.shape[1] > max_w:
                    scale = max_w / float(preview.shape[1])
                    preview = cv2.resize(preview, (max_w, max(1, int(preview.shape[0] * scale))), interpolation=cv2.INTER_AREA)
                ok, enc = cv2.imencode(".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, jpg_q])
                if ok:
                    cached_data = enc.tobytes()
                    data = cached_data
                    last_serial = serial
        elif web:
            with web_frame_lock:
                current = web_frame
                serial = web_frame_serial
            if current is not None and serial != last_serial:
                preview = current
                if preview.shape[1] > 720:
                    scale = 720.0 / preview.shape[1]
                    preview = cv2.resize(preview, (720, max(1, int(preview.shape[0] * scale))), interpolation=cv2.INTER_AREA)
                ok, enc = cv2.imencode(".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, 70])
                if ok:
                    cached_data = enc.tobytes()
                    data = cached_data
                    last_serial = serial
        else:
            # Live Console: wait for the encoder to publish a new JPEG instead
            # of polling on a fixed sleep. This removes avoidable delivery jitter.
            with preview_condition:
                current_serial = latest_jpeg_serial
                if current_serial == last_serial:
                    preview_condition.wait(timeout=1.0)
                data = latest_jpeg
                last_serial = latest_jpeg_serial
            if data is None:
                continue
        if data is not None:
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + data + b"\r\n"
        if source or web:
            with lock:
                stream_fps = min(60, max(24, int(state.get("fps", 30))))
            time.sleep(1.0 / stream_fps)

render_thread = threading.Thread(target=render_worker, name="paokra-render", daemon=True)
preview_encode_thread = threading.Thread(target=preview_output_worker, name="paokra-preview", daemon=True)
vcam_output_thread = threading.Thread(target=vcam_output_worker, name="paokra-vcam-output", daemon=True)

def virtual_audio_worker():
    last_source = None
    last_loop_count = 0
    retry_at = 0.0
    flow_audio_key = None
    flow_audio_retries = 0
    flow_audio_reported = False
    while not stop_event.is_set():
        try:
            # Snapshot control state under the transition lock only. Never hold it
            # across audio-device calls or sleeps: continuous reacquisition by this
            # polling worker can starve Source/Scene Builder requests.
            with clip_flow_transition_lock:
                with lock:
                    running = bool(state.get("running", False))
                    audio_enabled = bool(state.get("virtualaudio_enabled", True))
                    source_enabled = bool(state.get("source_enabled", True))
                    src = str(state.get("source") or "")
                    loop_count = int(state.get("loop_count", 0))
                    loop_enabled = bool(state.get("loop", True))
                    audio_offset = int(state.get("audio_offset_ms", 0))
                # Clip Flow owns audio start/seek/EOF timing itself. Snapshot the
                # owner/phase atomically; re-check its token before starting recovery
                # audio, but do not keep the transition lock while the device opens.
                with clip_flow_lock:
                    flow_active = bool(clip_flow_runtime.get("active"))
                    flow_phase = str(clip_flow_runtime.get("phase") or "")
                    flow_clip = str(clip_flow_runtime.get("current_clip") or "")
                    flow_changed_at = float(clip_flow_runtime.get("changed_at") or 0.0)
                    seam_audio_path = str(clip_flow_runtime.get("seam_audio_path") or "") if flow_phase == "seam" else ""
                    seam_audio_duration = float(clip_flow_runtime.get("seam_audio_duration") or 0.0) if flow_phase == "seam" else 0.0
                flow_audio_src = seam_audio_path or src
                flow_owns_audio = flow_active and flow_phase in ("open", "normal", "seam", "stop")
            if flow_owns_audio:
                # The Flow controller owns normal audio transitions and seeking.
                # This worker only recovers a failed audio start, from the current
                # media position, and never reopens a healthy clip from t=0.
                now = time.monotonic()
                flow_key = (flow_phase, flow_clip, flow_changed_at, audio_enabled, source_enabled)
                if flow_key != flow_audio_key:
                    flow_audio_key = flow_key
                    flow_audio_retries = 0
                    flow_audio_reported = False
                    retry_at = now + 0.50  # allow the transition's initial start to settle
                last_source = src
                last_loop_count = loop_count

                if not audio_enabled or not source_enabled or not running or not flow_audio_src or not Path(flow_audio_src).is_file():
                    if vaudio.active:
                        vaudio.stop()
                    retry_at = 0.0
                    time.sleep(0.06)
                    continue

                if vaudio.active:
                    retry_at = 0.0
                    # Keep the retry budget for this clip/seek even if the
                    # audio thread flaps active/inactive repeatedly. A new
                    # clip or user seek creates a new flow_key and resets it.
                else:
                    with vaudio._lock:
                        audio_start_pending = bool(vaudio._thread and vaudio._thread.is_alive())
                    if audio_start_pending:
                        retry_at = max(retry_at, now + 0.50)
                    elif now >= retry_at and flow_audio_retries < 3:
                        with source_buffer_lock:
                            origin = source_playback_origin
                            base_position = float(source_start_offset or 0.0)
                        duration = max(0.0, seam_audio_duration if flow_phase == "seam" and seam_audio_duration > 0 else float(source_loop_duration or 0.0))
                        position = max(base_position, time.monotonic() - origin if origin is not None else base_position)
                        remaining = None
                        if duration > 0.0:
                            end_guard = 1.0 / max(1.0, float(source_fps or 30.0))
                            position = min(max(0.0, position), max(0.0, duration - end_guard))
                            remaining = max(0.0, duration - position)
                        else:
                            position = max(0.0, position)
                        if remaining is not None and remaining <= 0.75:
                            # Audio naturally reaches EOF at the clip end. Let
                            # the generation-tagged video EOF advance the Flow;
                            # reopening a tiny audio tail only creates a gap.
                            retry_at = now + 0.20
                        else:
                            restarted = _clip_start_audio(flow_audio_src, source_offset_seconds=position, duration_override=(seam_audio_duration if flow_phase == "seam" else None))
                            flow_audio_retries += 1
                            retry_at = now + 1.5
                            if not restarted:
                                with lock:
                                    state["virtualaudio_error"] = vaudio.error or "Virtual Audio restart failed"
                    elif now >= retry_at and flow_audio_retries >= 3 and not flow_audio_reported:
                        with lock:
                            state["virtualaudio_error"] = vaudio.error or "Virtual Audio failed to stay active after 3 recovery attempts; check the clip audio track and output device"
                        flow_audio_reported = True
                time.sleep(0.06)
                continue

            want = bool(running and audio_enabled and source_enabled and src and Path(src).is_file())
            if not want:
                if vaudio.active:
                    vaudio.stop()
                last_source = None
                last_loop_count = loop_count
                with lock:
                    state["virtualaudio_error"] = None
                time.sleep(0.08)
                continue

            source_changed = (src != last_source)
            if source_changed and vaudio.active:
                vaudio.stop()

            now = time.monotonic()
            if not vaudio.active and now >= retry_at:
                with source_buffer_lock:
                    start_at = source_playback_origin
                if vaudio.start(src, start_at=start_at, loop=loop_enabled, loop_duration=source_loop_duration, offset_ms=audio_offset):
                    last_source = src
                    last_loop_count = loop_count
                    retry_at = 0.0
                    with lock:
                        state["virtualaudio_error"] = None
                else:
                    last_source = src
                    last_loop_count = loop_count
                    retry_at = now + 1.5
                    with lock:
                        state["virtualaudio_error"] = vaudio.error
            elif vaudio.active and src == last_source:
                last_loop_count = loop_count

            # Keep one continuous audio clock through video loops. The audio
            # worker itself loops the source, so restarting it on every loop
            # would create gaps and lip-sync jumps.
            if not loop_enabled and not vaudio.active and now < retry_at:
                pass
            time.sleep(0.06)
        except Exception as exc:
            with lock:
                state["virtualaudio_error"] = str(exc)
            time.sleep(0.2)
virtual_audio_thread = threading.Thread(target=virtual_audio_worker, name="paokra-virtual-audio", daemon=True)
render_thread.start()
preview_encode_thread.start()
vcam_output_thread.start()
virtual_audio_thread.start()

@app.get("/")
def index(): return send_from_directory(WEB, "index.html")

@app.get("/app.js")
def app_js(): return send_from_directory(WEB, "app.js")

@app.get("/style.css")
def style_css(): return send_from_directory(WEB, "style.css")

@app.get("/v2fFilter.js")
def v2f_filter_js(): return send_from_directory(WEB, "v2fFilter.js")

@app.get("/_filter_addon.css")
def filter_addon_css(): return send_from_directory(WEB, "_filter_addon.css")

# Pro UI assets are stored directly in app/web; serve the /static/ URLs used by index.html.
@app.get("/static/paokra-ui.css")
def paokra_ui_css(): return send_from_directory(WEB, "paokra-ui.css")

@app.get("/static/paokra-ui.js")
def paokra_ui_js(): return send_from_directory(WEB, "paokra-ui.js")

@app.get("/smooth-preview.js")
def smooth_preview_js():
    resp = send_from_directory(WEB, "smooth-preview.js")
    resp.headers["Cache-Control"] = "no-cache"
    return resp

@app.get("/favicon.ico")
def favicon(): return ("", 204)

_SMOOTH_KEYS = (
    "running", "source", "source_enabled", "source_x", "source_y", "source_width",
    "source_height", "source_fit_mode", "source_flip_h", "source_flip_v", "loop",
    "loop_variation_enabled", "loop_count", "layer_order", "image", "image_enabled",
    "image_x", "image_y", "image_width", "image_height", "image_fit", "image_zoom",
    "image_pan_x", "image_pan_y", "text", "text_enabled", "text_x", "text_y",
    "text_size", "texts", "web_enabled", "web_url", "web_x", "web_y", "web_width",
    "web_height", "width", "height", "fps", "source_serial", "virtualcam", "video_fx",
)

@app.get("/api/smooth")
def api_smooth():
    """Small layout + clock snapshot for the browser-side Smooth preview.

    The browser plays the same file in a GPU-decoded <video> and draws the same
    layers (image/text/web) from this state. `origin`/`server_now` are on the
    server's monotonic clock: the browser derives the loop position from them,
    exactly like the audio router does, so picture and preview share one clock.
    """
    with lock:
        s = {k: state.get(k) for k in _SMOOTH_KEYS}
        variant = dict(source_variant)
    with source_buffer_lock:
        origin = source_playback_origin
    s["server_now"] = time.monotonic()
    s["origin"] = None if origin is None else float(origin)
    s["loop_duration"] = float(source_loop_duration or 0.0)
    s["source_fps"] = float(source_fps or 0.0)
    s["variant"] = variant
    img = s.get("image")
    try:
        s["image_mtime"] = Path(img).stat().st_mtime_ns if img else 0
    except OSError:
        s["image_mtime"] = 0
    with web_frame_lock:
        s["web_has_frame"] = web_frame is not None
    resp = jsonify(s)
    resp.headers["Cache-Control"] = "no-store"
    return resp

@app.get("/overlay-media")
def overlay_media():
    with lock:
        img = state.get("image")
    if not img:
        return ("", 404)
    p = Path(img)
    if not p.is_file():
        return ("", 404)
    try:
        return send_file(p, conditional=True, max_age=3600)
    except Exception:
        return ("", 404)

@app.get("/api/libobs/status")
def api_libobs_status():
    """Diagnostic endpoint for the optional embedded libobs frame bridge."""
    try:
        info = libobs_bridge.status()
        # When libobs is intentionally bypassed (for example at 60 FPS), the
        # bridge itself is idle; return the explicit fallback reason from state.
        with lock:
            fallback_reason = state.get("libobs_error")
            camera_active = bool(state.get("virtualcam"))
        if not info.get("active") and camera_active and fallback_reason:
            info["error"] = fallback_reason
        return jsonify(ok=True, **info)
    except Exception as exc:
        return jsonify(ok=False, active=False, error=f"{type(exc).__name__}: {str(exc)[:180]}"), 500


@app.get("/api/state")
def api_state():
    with lock:
        s = dict(state)
    s["accounts"] = acc_store.public_list()
    s["source_fps"] = round(float(source_fps), 6)
    s["effective_fps"] = int(_effective_output_fps())
    # Distinguish the selected output target from the sender-call rate measured
    # by pyvirtualcam. A receiving browser/app may negotiate a lower capture FPS,
    # so the UI must not imply that the configured value is the receiver rate.
    status_reader = getattr(vcam, "status", None)
    if callable(status_reader):
        camera_status = status_reader()
    else:
        camera_status = {
            "target_fps": getattr(vcam, "output_fps", 0),
            "measured_fps": getattr(vcam, "measured_fps", 0.0),
            "width": getattr(vcam, "width", 0),
            "height": getattr(vcam, "height", 0),
        }
    s["virtualcam_output_fps"] = int(camera_status.get("target_fps") or 0)
    s["virtualcam_measured_fps"] = float(camera_status.get("measured_fps") or 0.0)
    s["virtualcam_output_width"] = int(camera_status.get("width") or 0)
    s["virtualcam_output_height"] = int(camera_status.get("height") or 0)
    # Explicit frame counter lets diagnostics verify that the live renderer kept
    # advancing when a short generated seam was selected between two source clips.
    s["render_frame_count"] = int(render_frame_count)
    s["source_duration_seconds"] = round(float(source_loop_duration or 0.0), 6)
    with source_buffer_lock:
        _clock_origin = source_playback_origin
        _base_position = float(source_start_offset or 0.0)
    _duration = max(0.0, float(source_loop_duration or 0.0))
    if s.get("running") and _duration > 0.0:
        _position = max(_base_position, time.monotonic() - _clock_origin if _clock_origin is not None else _base_position)
        _position = max(0.0, _position)
        # A looping source should show its current position within this cycle,
        # not stick at 100% after the first pass. Clip Flow sets loop=False and
        # therefore keeps a normal one-shot progress timeline.
        if bool(s.get("loop", True)):
            _position %= _duration
        s["source_position_seconds"] = round(min(_duration, _position), 6)
    else:
        s["source_position_seconds"] = 0.0
    s["source_serial"] = int(state.get("source_serial", 0))
    with source_frame_lock:
        s["source_frame_serial"] = int(source_frame_serial)
    with render_metrics_lock:
        s["render_frame_count"] = int(render_frame_count)
        s["preview_frame_count"] = int(preview_frame_count)
        s["vcam_frame_count"] = int(vcam_frame_count)
    s["virtualaudio"] = bool(vaudio.active)
    s["virtualaudio_enabled"] = bool(state.get("virtualaudio_enabled", True))
    s["virtualaudio_device"] = vaudio.device_name
    s["virtualaudio_error"] = vaudio.error
    with web_frame_lock:
        s["web_frame_serial"] = int(web_frame_serial)
    return jsonify(s)


def _clip_path(value, allow_blank=True):
    raw = str(value or "").strip().strip('"')
    if not raw and allow_blank:
        return ""
    if not raw:
        raise ValueError("กรุณาเลือกไฟล์วิดีโอ")
    path = Path(raw).expanduser()
    try:
        path = path.resolve()
    except Exception:
        pass
    if path.suffix.lower() not in CLIP_VIDEO_EXTS:
        raise ValueError(f"นามสกุลไฟล์วิดีโอไม่รองรับ: {path.name}")
    if not path.is_file():
        raise ValueError(f"ไม่พบไฟล์: {path}")
    return str(path)


def _clip_config_snapshot():
    with clip_flow_lock:
        return {
            "enabled": bool(clip_flow_config.get("enabled", True)),
            "open": str(clip_flow_config.get("open") or ""),
            "normal": list(clip_flow_config.get("normal") or []),
            "stop": str(clip_flow_config.get("stop") or ""),
        }


def _clip_flow_public_state():
    config = _clip_config_snapshot()
    with clip_flow_lock:
        runtime = {
            "active": bool(clip_flow_runtime.get("active")),
            "phase": str(clip_flow_runtime.get("phase") or "idle"),
            "current_clip": str(clip_flow_runtime.get("current_clip") or ""),
            "current_role": str(clip_flow_runtime.get("current_role") or ""),
            "stop_requested": bool(clip_flow_runtime.get("stop_requested", False)),
            "normal_queue_remaining": len(clip_flow_runtime.get("normal_queue") or []),
            "played_in_cycle": len(clip_flow_runtime.get("played_in_cycle") or []),
            "normal_total": len(config.get("normal") or []),
            "last_error": str(clip_flow_runtime.get("last_error") or ""),
            "reason": str(clip_flow_runtime.get("reason") or ""),
            "changed_at": float(clip_flow_runtime.get("changed_at") or 0.0),
            "seam_transition_count": int(clip_flow_runtime.get("seam_transition_count") or 0),
            "last_seam_transition": dict(clip_flow_runtime.get("last_seam_transition") or {})
                if isinstance(clip_flow_runtime.get("last_seam_transition"), dict) else None,
            "last_seam_lookup": dict(clip_flow_runtime.get("last_seam_lookup") or {})
                if isinstance(clip_flow_runtime.get("last_seam_lookup"), dict) else None,
        }
    with lock:
        actual_source = str(state.get("source") or "")
        running = bool(state.get("running", False))
        output_fps = int(_effective_output_fps())
    with source_buffer_lock:
        origin = source_playback_origin
        base_position = float(source_start_offset or 0.0)
    current_path = runtime["current_clip"]
    matches = bool(actual_source and current_path and os.path.normcase(actual_source) == os.path.normcase(current_path))
    duration = float(source_loop_duration or 0.0) if matches and runtime["active"] and running else 0.0
    position = 0.0
    if runtime["active"] and running and duration > 0.0:
        position = max(base_position, time.monotonic() - origin if origin is not None else base_position)
        position = min(duration, max(0.0, position))
    runtime.update({
        "duration_seconds": round(max(0.0, duration), 3),
        "position_seconds": round(position, 3),
        "remaining_seconds": round(max(0.0, duration - position), 3),
        "source_fps": round(float(source_fps or 0.0), 3),
        "output_fps": output_fps,
    })
    return {"ok": True, "config": config, "runtime": runtime}


def _persist_clip_config(config):
    CLIP_FLOW_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CLIP_FLOW_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CLIP_FLOW_FILE)


def _clip_start_audio(src, source_offset_seconds=None, duration_override=None):
    try:
        vaudio.stop()
    except Exception:
        pass
    with lock:
        enabled = bool(state.get("virtualaudio_enabled", True))
        source_enabled = bool(state.get("source_enabled", True))
        offset = int(state.get("audio_offset_ms", 0) or 0)
    with source_buffer_lock:
        start_at = source_playback_origin
        media_offset = max(0.0, float(source_start_offset if source_offset_seconds is None else source_offset_seconds))
        if start_at is not None:
            start_at += media_offset
    audio_duration = max(0.0, float(duration_override) - media_offset) if duration_override is not None else max(0.0, float(source_loop_duration or 0.0) - media_offset)
    if enabled and source_enabled and src and Path(src).is_file():
        ok = vaudio.start(
            src, start_at=start_at, loop=False,
            loop_duration=audio_duration, offset_ms=offset, source_offset=media_offset
        )
        with lock:
            state["virtualaudio_error"] = vaudio.error
        return bool(ok)
    with lock:
        state["virtualaudio_error"] = None
    return False


def _clip_flow_finish(error="", phase="finished"):
    with clip_flow_transition_lock:
        return _clip_flow_finish_impl(error, phase)


def _clip_flow_finish_impl(error="", phase="finished"):
    global source_playback_origin, source_sequence_index
    global clip_flow_eof_generation, clip_flow_restore_loop
    try:
        vaudio.stop()
    except Exception:
        pass

    # Serialize finish with Start/Stop requests. Restore the user's normal loop
    # preference after the one-shot playlist ends, while preventing an old EOF
    # handler from setting running=False after a newer flow has started.
    with clip_flow_lock:
        restore_loop = clip_flow_restore_loop
        clip_flow_restore_loop = None
        with lock:
            state["running"] = False
            state["virtualaudio_error"] = None
            if restore_loop is not None:
                state["loop"] = bool(restore_loop)
        clip_flow_runtime["active"] = False
        clip_flow_runtime["phase"] = phase
        clip_flow_runtime["source_generation"] = None
        clip_flow_runtime["stop_requested"] = False
        clip_flow_runtime["pending_stop_path"] = ""
        clip_flow_runtime["seam_next_path"] = ""
        clip_flow_runtime["seam_resume_seconds"] = 0.0
        clip_flow_runtime["seam_audio_path"] = ""
        clip_flow_runtime["seam_audio_duration"] = 0.0
        clip_flow_runtime["last_error"] = str(error or "")
        clip_flow_runtime["changed_at"] = time.time()
        clip_flow_eof_generation = None
        clip_flow_eof_event.clear()

    with source_buffer_lock:
        source_buffer.clear()
        source_playback_origin = None
        source_sequence_index = 0
    clip_flow_done_event.set()
    _clear_source_prime_cache()


def _clip_flow_ensure_normal_queue_locked():
    normals = [str(x) for x in (clip_flow_config.get("normal") or []) if str(x) and Path(str(x)).is_file()]
    queue = list(clip_flow_runtime.get("normal_queue") or [])
    if not queue and normals:
        random.shuffle(normals)
        previous = str(clip_flow_runtime.get("previous_normal") or "")
        if len(normals) > 1 and normals[0] == previous:
            normals[0], normals[1] = normals[1], normals[0]
        queue = normals
        clip_flow_runtime["normal_queue"] = queue
        clip_flow_runtime["played_in_cycle"] = []
    return queue

def _clip_flow_peek_next_locked():
    queue = _clip_flow_ensure_normal_queue_locked()
    return str(queue[0]) if queue else ""

def _clip_flow_shuffle_next_locked():
    queue = _clip_flow_ensure_normal_queue_locked()
    if not queue:
        return ""
    chosen = queue.pop(0)
    clip_flow_runtime["normal_queue"] = queue
    clip_flow_runtime.setdefault("played_in_cycle", []).append(chosen)
    clip_flow_runtime["previous_normal"] = chosen
    return chosen


def _ensure_clip_flow_controller():
    global clip_flow_controller_thread
    with clip_flow_lock:
        if clip_flow_controller_thread and clip_flow_controller_thread.is_alive():
            return
        clip_flow_controller_thread = threading.Thread(
            target=_clip_flow_controller, name="paokra-clip-flow", daemon=True
        )
        clip_flow_controller_thread.start()


def _clip_flow_wait_for_presentation_eof(generation, phase, current_path):
    """Wait for the presentation clock to reach clip end after decoder EOF.

    The decoder intentionally reads ahead into a small frame queue. Consequently,
    a native read() EOF can arrive before the viewer has seen those queued frames.
    Playlist transitions must follow the media presentation deadline, not decode
    completion, or Stop can cut the last part of a short/fast-decoded clip.
    """
    with source_buffer_lock:
        origin = source_playback_origin
    duration = float(source_loop_duration or 0.0)
    if origin is None or not np.isfinite(duration) or duration <= 0.0:
        # Compatibility fallback for containers whose duration cannot be probed.
        return True
    presentation_end = float(origin) + duration
    while not stop_event.is_set():
        with clip_flow_lock:
            still_flow = (
                bool(clip_flow_runtime.get("active"))
                and clip_flow_runtime.get("phase") == phase
                and clip_flow_runtime.get("current_clip") == current_path
                and clip_flow_runtime.get("source_generation") == generation
            )
        with lock:
            still_source = (
                bool(state.get("running", False))
                and source_decode_generation == generation
                and os.path.normcase(str(state.get("source") or "")) == os.path.normcase(current_path)
            )
        if not still_flow or not still_source:
            return False
        remaining = presentation_end - time.monotonic()
        if remaining <= 0.0:
            return True
        # Check cancellations/Start overrides frequently, even for long clips.
        time.sleep(min(0.025, max(0.001, remaining)))
    return False


def _clip_flow_controller():
    global clip_flow_eof_generation
    while not stop_event.is_set():
        if not clip_flow_eof_event.wait(0.20):
            continue

        # Consume a generation-tagged EOF under the same lock used by source
        # transitions. This makes the event one-shot and rejects stale decoders.
        with clip_flow_lock:
            eof_generation = clip_flow_eof_generation
            clip_flow_eof_generation = None
            clip_flow_eof_event.clear()
            active = bool(clip_flow_runtime.get("active"))
            phase = str(clip_flow_runtime.get("phase") or "")
            current_path = str(clip_flow_runtime.get("current_clip") or "")
            expected_generation = clip_flow_runtime.get("source_generation")
        if not active or phase not in ("open", "normal", "seam", "stop"):
            continue
        if eof_generation is None or expected_generation != eof_generation:
            continue

        # A path alone is not enough: Open/Normal/Stop may intentionally point to
        # the same file. Validate decoder generation + source before advancing.
        with lock:
            actual_source = str(state.get("source") or "")
            running = bool(state.get("running", False))
            actual_generation = source_decode_generation
        if (actual_generation != eof_generation or
                os.path.normcase(actual_source) != os.path.normcase(current_path) or not running):
            continue

        # EOF means decoding ended, not that the final decoded frames have been
        # presented. Wait for the shared audio/video clock to reach the end time.
        if not _clip_flow_wait_for_presentation_eof(eof_generation, phase, current_path):
            continue

        # Revalidate after the clock wait: a manual Stop or a new Open may have
        # cancelled/replaced this flow while the controller was waiting.
        with clip_flow_lock:
            still_active = (
                bool(clip_flow_runtime.get("active"))
                and clip_flow_runtime.get("phase") == phase
                and clip_flow_runtime.get("current_clip") == current_path
                and clip_flow_runtime.get("source_generation") == eof_generation
            )
        with lock:
            still_source = (
                state.get("source") == current_path
                and source_decode_generation == eof_generation
                and bool(state.get("running", False))
            )
        if not still_active or not still_source:
            continue

        worker = source_decode_thread
        if worker and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=1.0)

        seam_runtime_profile = _ai_seam_runtime_profile() if phase in ("open", "normal") else None
        if phase == "stop":
            # A new Flow may have started while this decoder was joining.
            # Finish only the exact Stop generation that emitted this EOF.
            with clip_flow_lock:
                may_finish = (
                    bool(clip_flow_runtime.get("active"))
                    and clip_flow_runtime.get("phase") == "stop"
                    and clip_flow_runtime.get("current_clip") == current_path
                    and clip_flow_runtime.get("source_generation") == eof_generation
                )
            if may_finish:
                _clip_flow_finish(phase="finished")
            continue

        # Hold the transition lock from queue commit through set_source/audio
        # startup. A Stop click cannot set the Stop state and then be overwritten
        # by a delayed Open->Normal decoder handoff.
        with clip_flow_transition_lock:
            next_path = ""
            target_phase = "normal"
            transition_error = ""
            preserve_seam_audio = False
            seam_resume_seconds = 0.0
            seam_audio_duration = 0.0
            with clip_flow_lock:
                if (not clip_flow_runtime.get("active") or
                        clip_flow_runtime.get("phase") != phase or
                        clip_flow_runtime.get("current_clip") != current_path or
                        clip_flow_runtime.get("source_generation") != eof_generation):
                    continue
                stop_requested = bool(clip_flow_runtime.get("stop_requested"))
                if stop_requested and phase != "seam":
                    # Stop is terminal. If a matching Normal -> Stop bridge was
                    # generated, play it once, then resume the Stop clip and finish.
                    # A stop requested while a bridge is already playing skips a
                    # second bridge and goes directly to Stop after the current EOF.
                    next_path = str(clip_flow_runtime.get("pending_stop_path") or "").strip()
                    if not next_path or not Path(next_path).is_file():
                        transition_error = "คลิป Stop ที่รอเล่นหลังจบคลิปปัจจุบันหายไปหรือเปิดไม่ได้"
                    else:
                        stop_seam = None
                        stop_lookup = {}
                        if phase in ("open", "normal"):
                            stop_seam = find_cached_transition(
                                DATA, current_path, next_path,
                                runtime_profile=seam_runtime_profile, diagnostics=stop_lookup,
                            )
                        seam_asset = str(stop_seam.get("asset") or "") if stop_seam else ""
                        stop_resume = max(0.0, float(stop_seam.get("resume_seconds") or 0.0)) if stop_seam else 0.0
                        stop_duration = max(0.0, float(stop_seam.get("to_duration") or 0.0)) if stop_seam else 0.0
                        with clip_flow_lock:
                            clip_flow_runtime["last_seam_lookup"] = {
                                "from_path": current_path, "to_path": next_path,
                                "found": bool(stop_seam), "target_role": "stop",
                                "reason": str(stop_lookup.get("reason") or ("compatible directed bridge found" if stop_seam else "no compatible seam")),
                                "bridge_fps": int(stop_lookup.get("bridge_fps") or 0),
                                "runtime_fps": int(stop_lookup.get("runtime_fps") or (seam_runtime_profile or {}).get("fps") or 0),
                                "runtime_fit": str(stop_lookup.get("runtime_fit") or (seam_runtime_profile or {}).get("fit") or ""),
                                "checked_at": time.time(),
                                "virtualcam_active": bool(vcam.active),
                                "virtualcam_measured_fps": float(getattr(vcam, "measured_fps", 0.0) or 0.0),
                            }
                        if seam_asset and Path(seam_asset).is_file() and stop_resume > 0 and stop_duration > stop_resume:
                            target_phase = "seam"
                            clip_flow_eof_generation = None
                            clip_flow_runtime["seam_transition_count"] = int(clip_flow_runtime.get("seam_transition_count") or 0) + 1
                            clip_flow_runtime["last_seam_transition"] = {
                                "from_path": current_path, "to_path": next_path, "asset": seam_asset,
                                "target_role": "stop", "selected_at": time.time(),
                                "resume_seconds": stop_resume, "fps": int(stop_seam.get("fps") or 0),
                                "frame_count": int(stop_seam.get("frame_count") or 0),
                                "virtualcam_active": bool(vcam.active),
                                "virtualcam_measured_fps": float(getattr(vcam, "measured_fps", 0.0) or 0.0),
                                "render_frame_count": int(render_frame_count),
                            }
                            clip_flow_runtime.update(
                                phase="seam", current_role="seam", current_clip=seam_asset,
                                source_generation=None, normal_queue=[], stop_requested=False,
                                pending_stop_path="", seam_next_path=next_path, seam_next_role="stop",
                                seam_resume_seconds=stop_resume, seam_audio_path=next_path,
                                seam_audio_duration=stop_duration,
                                reason="ai_seam_before_stop", changed_at=time.time()
                            )
                            next_path = seam_asset
                            seam_resume_seconds, seam_audio_duration = stop_resume, stop_duration
                        else:
                            target_phase = "stop"
                            clip_flow_eof_generation = None
                            clip_flow_runtime.update(
                                phase="stop", current_role="stop", current_clip=next_path,
                                source_generation=None, normal_queue=[], stop_requested=False,
                                pending_stop_path="", seam_next_path="", seam_next_role="normal",
                                seam_resume_seconds=0.0, seam_audio_path="", seam_audio_duration=0.0,
                                reason="stop_after_current_clip", changed_at=time.time()
                            )
                elif phase == "seam":
                    # Persist evidence of the seam's actual decode/output interval before
                    # the runtime record moves on to destination B.
                    record = clip_flow_runtime.get("last_seam_transition")
                    if isinstance(record, dict) and record.get("playback_started_at"):
                        with source_frame_lock:
                            decoded_now = int(source_prepared_serial)
                        with render_metrics_lock:
                            vcam_now = int(vcam_frame_count)
                            render_now = int(render_frame_count)
                        record["playback_finished_at"] = time.time()
                        prepared_before = record.get("source_prepared_serial_before")
                        vcam_before = record.get("vcam_frame_count_before")
                        render_before = record.get("render_frame_count_before")
                        record["prepared_frames_observed"] = max(
                            0, decoded_now - int(decoded_now if prepared_before is None else prepared_before)
                        )
                        record["vcam_frames_during_seam"] = max(
                            0, vcam_now - int(vcam_now if vcam_before is None else vcam_before)
                        )
                        record["render_frames_during_seam"] = max(
                            0, render_now - int(render_now if render_before is None else render_before)
                        )
                    # Resume B at the timestamp used to prepare the bridge endpoint.
                    next_path = str(clip_flow_runtime.get("seam_next_path") or "").strip()
                    try:
                        seam_resume_seconds = max(0.0, float(clip_flow_runtime.get("seam_resume_seconds") or 0.0))
                    except (TypeError, ValueError, OverflowError):
                        seam_resume_seconds = 0.0
                    if not next_path or not Path(next_path).is_file():
                        transition_error = "AI Seam destination clip is missing; refusing a stale transition"
                    else:
                        next_role = str(clip_flow_runtime.get("seam_next_role") or "normal").lower()
                        target_phase = "stop" if next_role == "stop" else "normal"
                        preserve_seam_audio = True
                        clip_flow_eof_generation = None
                        # If Stop was queued while a normal-to-normal bridge was
                        # playing, resume B and keep the request queued until B EOF.
                        keep_stop_pending = bool(clip_flow_runtime.get("stop_requested")) and target_phase == "normal"
                        kept_stop_path = str(clip_flow_runtime.get("pending_stop_path") or "") if keep_stop_pending else ""
                        clip_flow_runtime.update(
                            phase=target_phase, current_role=target_phase, current_clip=next_path,
                            source_generation=None, stop_requested=keep_stop_pending,
                            pending_stop_path=kept_stop_path,
                            seam_next_path="", seam_next_role="normal", seam_resume_seconds=0.0,
                            seam_audio_path="", seam_audio_duration=0.0,
                            reason="ai_seam_destination_resume" if target_phase == "normal" else "ai_seam_stop_destination",
                            changed_at=time.time()
                        )
                else:
                    next_path = _clip_flow_shuffle_next_locked()
                    if next_path:
                        # Only use a verified cache for this exact directed pair.
                        # Without one, keep the existing normal handoff unchanged.
                        seam_lookup = {}
                        seam = find_cached_transition(
                            DATA, current_path, next_path,
                            runtime_profile=seam_runtime_profile, diagnostics=seam_lookup,
                        )
                        with clip_flow_lock:
                            clip_flow_runtime["last_seam_lookup"] = {
                                "from_path": current_path, "to_path": next_path,
                                "found": bool(seam), "target_role": "normal",
                                "reason": str(seam_lookup.get("reason") or ("compatible directed bridge found" if seam else "no compatible seam")),
                                "bridge_fps": int(seam_lookup.get("bridge_fps") or 0),
                                "runtime_fps": int(seam_lookup.get("runtime_fps") or (seam_runtime_profile or {}).get("fps") or 0),
                                "runtime_fit": str(seam_lookup.get("runtime_fit") or (seam_runtime_profile or {}).get("fit") or ""),
                                "checked_at": time.time(),
                                "profile": {
                                    "width": int((seam_runtime_profile or {}).get("width") or 0),
                                    "height": int((seam_runtime_profile or {}).get("height") or 0),
                                    "box_width": int((seam_runtime_profile or {}).get("box_width") or 0),
                                    "box_height": int((seam_runtime_profile or {}).get("box_height") or 0),
                                    "fps": int((seam_runtime_profile or {}).get("fps") or 0),
                                    "fit": str((seam_runtime_profile or {}).get("fit") or ""),
                                },
                                "virtualcam_active": bool(vcam.active),
                                "virtualcam_measured_fps": float(getattr(vcam, "measured_fps", 0.0) or 0.0),
                            }
                        seam_asset = str(seam.get("asset") or "") if seam else ""
                        if seam_asset and Path(seam_asset).is_file():
                            try:
                                seam_resume_seconds = max(0.0, float(seam.get("resume_seconds") or 0.0))
                                seam_audio_duration = max(0.0, float(seam.get("to_duration") or 0.0))
                            except (TypeError, ValueError, OverflowError):
                                seam_resume_seconds, seam_audio_duration = 0.0, 0.0
                            if seam_resume_seconds > 0.0 and seam_audio_duration > seam_resume_seconds:
                                target_phase = "seam"
                                clip_flow_eof_generation = None
                                clip_flow_runtime["seam_transition_count"] = int(clip_flow_runtime.get("seam_transition_count") or 0) + 1
                                clip_flow_runtime["last_seam_transition"] = {
                                    "from_path": current_path, "to_path": next_path, "asset": seam_asset,
                                    "target_role": "normal", "selected_at": time.time(),
                                    "resume_seconds": seam_resume_seconds,
                                    "fps": int(seam.get("fps") or 0),
                                    "frame_count": int(seam.get("frame_count") or 0),
                                    "virtualcam_active": bool(vcam.active),
                                    "virtualcam_measured_fps": float(getattr(vcam, "measured_fps", 0.0) or 0.0),
                                    "render_frame_count": int(render_frame_count),
                                }
                                clip_flow_runtime.update(
                                    phase="seam", current_role="seam", current_clip=seam_asset,
                                    source_generation=None, seam_next_path=next_path, seam_next_role="normal",
                                    seam_resume_seconds=seam_resume_seconds, seam_audio_path=next_path,
                                    seam_audio_duration=seam_audio_duration,
                                    reason="ai_seam_bridge", changed_at=time.time()
                                )
                                next_path = seam_asset
                        if target_phase != "seam":
                            clip_flow_eof_generation = None
                            clip_flow_runtime["source_generation"] = None
                            clip_flow_runtime["phase"] = "normal"
                            clip_flow_runtime["current_role"] = "normal"
                            clip_flow_runtime["current_clip"] = next_path
                            clip_flow_runtime["changed_at"] = time.time()
            if transition_error:
                _clip_flow_finish(error=transition_error, phase="error")
                continue
            if not next_path:
                _clip_flow_finish(phase="finished")
                continue
            try:
                seam_prepared_before = None
                if target_phase == "seam":
                    with source_frame_lock:
                        seam_prepared_before = int(source_prepared_serial)
                with render_metrics_lock:
                    seam_vcam_before = int(vcam_frame_count)
                    seam_render_before = int(render_frame_count)
                if not Path(next_path).is_file() or not set_source(next_path, start_seconds=(seam_resume_seconds if preserve_seam_audio else 0.0)):
                    role = "Stop" if target_phase == "stop" else "AI Seam" if target_phase == "seam" else "Normal"
                    raise RuntimeError(f"{role} source could not be opened: {next_path}")
                if target_phase == "seam":
                    with clip_flow_lock:
                        record = clip_flow_runtime.get("last_seam_transition")
                        if isinstance(record, dict) and os.path.normcase(str(record.get("asset") or "")) == os.path.normcase(str(next_path)):
                            record["playback_started_at"] = time.time()
                            record["source_generation"] = source_decode_generation
                            record["source_prepared_serial_before"] = int(seam_prepared_before or 0)
                            record["vcam_frame_count_before"] = int(seam_vcam_before)
                            record["render_frame_count_before"] = int(seam_render_before)
                    # Audio uses destination B underneath the generated video-only
                    # bridge, and keeps playing through the resumed B source.
                    with clip_flow_lock:
                        bridge_audio = str(clip_flow_runtime.get("seam_audio_path") or "")
                        bridge_duration = float(clip_flow_runtime.get("seam_audio_duration") or 0.0)
                    _clip_start_audio(bridge_audio or next_path, source_offset_seconds=0.0,
                                      duration_override=(bridge_duration if bridge_duration > 0 else None))
                elif preserve_seam_audio:
                    if not vaudio.active:
                        _clip_start_audio(next_path, source_offset_seconds=seam_resume_seconds)
                else:
                    _clip_start_audio(next_path)
                if target_phase == "normal":
                    _clip_flow_schedule_handoff_prime(next_path, "normal")
            except Exception as exc:
                _clip_flow_finish(error=f"{type(exc).__name__}: {str(exc)[:300]}", phase="error")


def _clip_flow_begin_open(reason="manual"):
    with clip_flow_transition_lock:
        return _clip_flow_begin_open_impl(reason)


def _clip_flow_begin_open_impl(reason="manual"):
    global clip_flow_eof_generation, clip_flow_restore_loop, _AI_SEAM_NEGOTIATED_FPS_OVERRIDE
    if _ai_seam_batch_is_active():
        return {"ok": False, "error": "Auto Make Frames is running. Wait for the transition batch to finish before starting Clip Flow."}
    if _ai_seam_generation_active(DATA):
        return {"ok": False, "error": "AI Seam generation is using compute resources. Wait for it to finish before starting Clip Flow."}
    config = _clip_config_snapshot()
    if not config.get("enabled", True):
        return {"ok": False, "error": "Clip Flow ถูกปิดใช้งานอยู่"}
    try:
        open_path = _clip_path(config.get("open"), allow_blank=False)
        normals = [_clip_path(x, allow_blank=False) for x in config.get("normal", [])]
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    if not normals:
        return {"ok": False, "error": "ต้องเพิ่มคลิป Normal อย่างน้อย 1 คลิปก่อนเริ่ม Flow"}

    # If AI Seam is enabled, refuse to start a randomized playlist whose directed
    # transition matrix is incomplete. Otherwise a newly added clip or changed
    # output profile could silently turn a generated seam into a hard cut mid-LIVE.
    stop_path = str(config.get("stop") or "").strip()
    seam_pairs = [(open_path, target) for target in normals]
    seam_pairs.extend((source, target) for source in normals for target in normals
                      if os.path.normcase(source) != os.path.normcase(target))
    if stop_path and Path(stop_path).is_file():
        seam_pairs.append((open_path, stop_path))
        seam_pairs.extend((source, stop_path) for source in normals)
    try:
        matrix = _ai_seam_validate_transition_matrix(DATA, seam_pairs)
    except Exception as exc:
        return {"ok": False, "error": "AI Seam matrix preflight failed; run Auto Make Frames again: " + str(exc)[:240]}
    if matrix.get("enabled") and matrix.get("missing"):
        missing = matrix["missing"]
        examples = "; ".join(
            Path(str(item.get("from_path") or "")).name + " → " +
            Path(str(item.get("to_path") or "")).name
            for item in missing[:4]
        )
        more = " (+" + str(len(missing) - 4) + " more)" if len(missing) > 4 else ""
        return {
            "ok": False,
            "error": "AI Seam is enabled, but Auto Make Frames is incomplete or stale: "
                     + str(matrix.get("ready", 0)) + "/" + str(matrix.get("total", 0))
                     + " directed transitions are compatible. Run Auto Make Frames before starting. Missing: "
                     + examples + more,
            "seam_matrix": matrix,
        }

    # Pin the camera rate for the entire Flow. Auto first requests the opening
    # clip's rounded FPS and retries common driver modes only if that rate fails.
    started_camera_here = False
    with lock:
        out_w = int(state.get("width", 720))
        out_h = int(state.get("height", 1280))
        fps_mode = str(state.get("fps_mode", "fixed") or "fixed")
        user_loop = bool(state.get("loop", True))
    source_rate_hint = None
    if fps_mode == "source":
        try:
            source_rate_hint, _ = _probe_source_timing(open_path, float(source_fps or 30.0))
        except Exception:
            source_rate_hint = float(source_fps or 30.0)
    out_fps = _effective_output_fps(source_rate_hint=source_rate_hint, prefer_source_hint=(fps_mode == "source"))
    camera_was_active = bool(vcam.active)
    if camera_was_active:
        # If preflight has already negotiated a driver-supported cadence, keep it.
        # Re-negotiating here could silently change FPS *after* the seam matrix was
        # verified, which caused bridges to be skipped only on some PCs.
        if fps_mode == "source":
            out_fps = int(vcam.output_fps or out_fps)
            _AI_SEAM_NEGOTIATED_FPS_OVERRIDE = out_fps
        with lock:
            state["virtualcam"] = True
            state["virtualcam_error"] = None
    else:
        camera_ok = bool(vcam.start(out_w, out_h, out_fps, allow_fallback=(fps_mode == "source")))
        started_camera_here = camera_ok
        with lock:
            state["virtualcam"] = bool(vcam.active)
            state["virtualcam_error"] = vcam.error
        if not camera_ok or not vcam.active:
            return {
                "ok": False,
                "error": "เปิด Virtual Camera ไม่สำเร็จ: " + str(vcam.error or "ไม่พบอุปกรณ์ Virtual Camera"),
                "virtualcam": False,
            }
        _AI_SEAM_NEGOTIATED_FPS_OVERRIDE = int(vcam.output_fps or out_fps) if fps_mode == "source" else 0

    # Clip Flow uses the same libobs output stage as manual Virtual Camera start.
    if vcam.active:
        _start_libobs_bridge(out_w, out_h, int(vcam.output_fps or out_fps))

    # The first matrix check ran before camera creation and therefore only knew the
    # predicted FPS. Verify again against the actual device-negotiated profile so
    # direct/API starts cannot silently degrade to hard cuts when this PC falls back.
    try:
        runtime_matrix = _ai_seam_validate_transition_matrix(DATA, seam_pairs)
    except Exception as exc:
        runtime_matrix = {"enabled": False, "missing": [], "error": str(exc)}
        runtime_check_error = True
    else:
        runtime_check_error = False
    if runtime_check_error or (runtime_matrix.get("enabled") and runtime_matrix.get("missing")):
        missing = runtime_matrix.get("missing") or []
        examples = "; ".join(
            Path(str(item.get("from_path") or "")).name + " → " +
            Path(str(item.get("to_path") or "")).name for item in missing[:4]
        )
        if started_camera_here:
            try:
                vcam.stop()
                _stop_libobs_bridge()
            except Exception:
                pass
            with lock:
                state["virtualcam"] = bool(vcam.active)
                state["virtualcam_error"] = vcam.error
        return {
            "ok": False,
            "error": (
                "AI Seam profile changed after Virtual Camera negotiation; playback was blocked to avoid hard cuts. "
                + ("Runtime matrix check failed: " + str(runtime_matrix.get("error") or "unknown error")
                   if runtime_check_error else
                   "Compatible transitions: " + str(runtime_matrix.get("ready", 0)) + "/" +
                   str(runtime_matrix.get("total", 0)) + ". Re-run Start preflight to rebuild for this PC. Missing: " + examples)
            ),
            "seam_matrix": runtime_matrix,
            "virtualcam": bool(vcam.active),
        }
    _ensure_clip_flow_controller()
    with clip_flow_lock:
        if not clip_flow_runtime.get("active"):
            clip_flow_restore_loop = user_loop
        clip_flow_eof_generation = None
        clip_flow_eof_event.clear()
        clip_flow_done_event.clear()
        clip_flow_runtime.update(
            active=True, phase="open", current_clip=open_path, current_role="open",
            normal_queue=[], played_in_cycle=[], previous_normal="",
            source_generation=None, last_error="", reason=str(reason),
            stop_requested=False, pending_stop_path="",
            seam_next_path="", seam_next_role="normal", seam_resume_seconds=0.0, seam_audio_path="", seam_audio_duration=0.0,
            seam_transition_count=0, last_seam_transition=None, last_seam_lookup=None,
            started_at=time.time(), changed_at=time.time()
        )
    with lock:
        state["running"] = True
        state["loop"] = False
        state["loop_count"] = 0
        state["source_enabled"] = True
        order = list(state.get("layer_order") or [])
        if "source" not in order:
            order.append("source")
        state["layer_order"] = order
    try:
        # Release any leftover media handles from an earlier Flow run before
        # warming OPEN; stale asynchronous prime workers are invalidated by epoch.
        _clear_source_prime_cache()
        _prime_source_capture(open_path, 0.0)
        if not set_source(open_path):
            raise RuntimeError(f"เปิดคลิป Open ไม่สำเร็จ: {open_path}")
        _clip_start_audio(open_path)
        _clip_flow_schedule_handoff_prime(open_path, "open")
        return {
            "ok": True, "running": True, "phase": "open", "source": open_path,
            "audio_active": bool(vaudio.active), "virtualcam": bool(vcam.active),
        }
    except Exception as exc:
        _clip_flow_finish(error=f"{type(exc).__name__}: {str(exc)[:300]}", phase="error")
        if started_camera_here:
            try:
                vcam.stop()
            except Exception:
                pass
            with lock:
                state["virtualcam"] = bool(vcam.active)
                state["virtualcam_error"] = vcam.error
        return {"ok": False, "error": str(exc)[:400]}


def _clip_flow_begin_stop(reason="manual_stop"):
    """Queue Stop behind the currently playing playlist clip instead of cutting it off.

    Open/Normal remain the active source until their own EOF event arrives. Repeated
    Stop clicks are idempotent, and Start can explicitly cancel a queued Stop.
    If no playlist is active, this keeps the existing behavior of previewing Stop now.
    """
    global clip_flow_eof_generation, clip_flow_restore_loop
    with clip_flow_transition_lock:
        with clip_flow_lock:
            flow_active = bool(clip_flow_runtime.get("active"))
            phase = str(clip_flow_runtime.get("phase") or "")
            current_path = str(clip_flow_runtime.get("current_clip") or "")
            stop_requested = bool(clip_flow_runtime.get("stop_requested"))
            pending_path = str(clip_flow_runtime.get("pending_stop_path") or "")

        # Clicking Stop again while the closing clip is already playing must not
        # restart it from frame zero or create a second EOF generation.
        if flow_active and phase == "stop":
            runtime = _clip_flow_public_state()["runtime"]
            return {"ok": True, "running": True, "phase": "stop", "source": current_path,
                    "current_role": "stop", "stop_pending": False,
                    "already_playing_stop": True,
                    "duration_seconds": float(runtime.get("remaining_seconds") or 0.0),
                    "current_remaining_seconds": float(runtime.get("remaining_seconds") or 0.0)}

        # If already queued, keep the exact Stop clip selected when the user first
        # requested it—even if playlist settings are edited before EOF.
        if flow_active and phase in ("open", "normal", "seam") and stop_requested and pending_path:
            runtime = _clip_flow_public_state()["runtime"]
            return {"ok": True, "running": True, "phase": phase, "source": current_path,
                    "current_role": str(runtime.get("current_role") or phase),
                    "stop_clip": pending_path, "stop_pending": True,
                    "duration_seconds": float(runtime.get("remaining_seconds") or 0.0),
                    "current_remaining_seconds": float(runtime.get("remaining_seconds") or 0.0),
                    "reason": str(reason)}

        config = _clip_config_snapshot()
        stop_path = str(config.get("stop") or "").strip()
        if not stop_path or not Path(stop_path).is_file():
            return {"ok": False, "error": "ยังไม่ได้ตั้งคลิป Stop หรือไฟล์ Stop ไม่พบ — ไม่ได้ตัดคลิปที่กำลังเล่น"}
        try:
            stop_path = _clip_path(stop_path, allow_blank=False)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        if flow_active and phase in ("open", "normal", "seam"):
            with lock:
                actual_source = str(state.get("source") or "")
                running = bool(state.get("running", False))
            if not running or os.path.normcase(actual_source) != os.path.normcase(current_path):
                return {"ok": False, "error": "คลิปปัจจุบันเปลี่ยนระหว่างรับคำสั่ง Stop กรุณารีเฟรชสถานะแล้วลองใหม่"}
            with clip_flow_lock:
                # Re-check under the flow lock; the transition lock prevents the
                # controller from committing an Open->Normal handoff concurrently.
                if not clip_flow_runtime.get("active") or clip_flow_runtime.get("phase") not in ("open", "normal", "seam"):
                    return {"ok": False, "error": "สถานะ Playlist เปลี่ยนแล้ว กรุณาลอง Stop อีกครั้ง"}
                if not clip_flow_runtime.get("stop_requested"):
                    clip_flow_runtime["stop_requested"] = True
                    clip_flow_runtime["pending_stop_path"] = stop_path
                    clip_flow_runtime["reason"] = str(reason)
                    clip_flow_runtime["changed_at"] = time.time()
            runtime = _clip_flow_public_state()["runtime"]
            remaining = float(runtime.get("remaining_seconds") or 0.0)
            return {"ok": True, "running": True, "phase": phase, "source": current_path,
                    "current_role": str(runtime.get("current_role") or phase),
                    "stop_clip": stop_path, "stop_pending": True,
                    "duration_seconds": remaining, "current_remaining_seconds": remaining,
                    "reason": str(reason)}

        # No playlist owns the active source: preserve the previous Stop-preview
        # behavior for users who click the Stop clip button while the Engine is idle.
        user_loop = None
        with lock:
            user_loop = bool(state.get("loop", True))
        _ensure_clip_flow_controller()
        with clip_flow_lock:
            if not clip_flow_runtime.get("active"):
                clip_flow_restore_loop = user_loop
            clip_flow_eof_generation = None
            clip_flow_eof_event.clear()
            clip_flow_done_event.clear()
            clip_flow_runtime.update(
                active=True, phase="stop", current_clip=stop_path, current_role="stop",
                normal_queue=[], played_in_cycle=[], source_generation=None, last_error="",
                stop_requested=False, pending_stop_path="", seam_next_path="", seam_next_role="normal", seam_resume_seconds=0.0,
                seam_audio_path="", seam_audio_duration=0.0, reason=str(reason), changed_at=time.time()
            )
        with lock:
            state["running"] = True
            state["loop"] = False
            state["source_enabled"] = True
            order = list(state.get("layer_order") or [])
            if "source" not in order:
                order.append("source")
            state["layer_order"] = order
        try:
            if not set_source(stop_path):
                raise RuntimeError(f"เปิดคลิป Stop ไม่สำเร็จ: {stop_path}")
            audio_active = _clip_start_audio(stop_path)
            duration = float(source_loop_duration or 0.0)
            with clip_flow_lock:
                clip_flow_runtime["changed_at"] = time.time()
            return {"ok": True, "running": True, "phase": "stop", "source": stop_path,
                    "current_role": "stop", "stop_pending": False,
                    "audio_active": bool(audio_active), "duration_seconds": duration,
                    "stop_duration_seconds": duration}
        except Exception as exc:
            _clip_flow_finish(error=f"{type(exc).__name__}: {str(exc)[:300]}", phase="error")
            return {"ok": False, "error": str(exc)[:400]}

def _clip_flow_play_stop_and_wait(reason="live_stop"):
    started = _clip_flow_begin_stop(reason=reason)
    if not started.get("ok"):
        return started
    current_remaining = float(started.get("current_remaining_seconds") or
                              (started.get("duration_seconds") if started.get("stop_pending") else 0.0) or 0.0)
    stop_duration = float(started.get("stop_duration_seconds") or 0.0)
    if started.get("stop_pending") and stop_duration <= 0.0:
        try:
            cfg = _clip_config_snapshot()
            _, stop_duration = _probe_source_timing(cfg.get("stop"), float(source_fps or 30.0))
        except Exception:
            stop_duration = 0.0
    if not started.get("stop_pending") and stop_duration <= 0.0:
        stop_duration = float(started.get("duration_seconds") or 0.0)

    # If Stop is requested while an A→B seam is playing, B still has to finish
    # before the closing Stop clip. The bridge's remaining time alone is not enough
    # for the LIVE protocol wait budget; include B's unplayed duration as well.
    destination_remaining = 0.0
    if started.get("stop_pending") and str(started.get("phase") or "").lower() == "seam":
        with clip_flow_lock:
            next_role = str(clip_flow_runtime.get("seam_next_role") or "normal").lower()
            destination_path = str(clip_flow_runtime.get("seam_next_path") or "").strip()
            try:
                destination_duration = max(0.0, float(clip_flow_runtime.get("seam_audio_duration") or 0.0))
                destination_resume = max(0.0, float(clip_flow_runtime.get("seam_resume_seconds") or 0.0))
            except (TypeError, ValueError, OverflowError):
                destination_duration, destination_resume = 0.0, 0.0
        if next_role == "normal":
            if destination_duration <= 0.0 and destination_path:
                try:
                    _, destination_duration = _probe_source_timing(destination_path, float(source_fps or 30.0))
                except Exception:
                    destination_duration = 0.0
            destination_remaining = max(0.0, destination_duration - destination_resume)

    # Do not cap this budget below the media duration: doing so could let the
    # LIVE STOP protocol run before a long queued Normal/Stop sequence completes.
    timeout = max(30.0, current_remaining + destination_remaining + stop_duration + 30.0)
    if not clip_flow_done_event.wait(timeout):
        _clip_flow_finish(error=f"Clip Flow ไม่จบภายใน {timeout:.0f}s", phase="error")
        return {"ok": False, "error": f"Clip Flow ไม่จบภายใน {timeout:.0f} วินาที"}
    with clip_flow_lock:
        error = str(clip_flow_runtime.get("last_error") or "")
    return {"ok": not error, "error": error or None, "completed": not error}


@app.get("/api/clip-flow")
def clip_flow_get():
    return jsonify(**_clip_flow_public_state())


@app.post("/api/clip-flow/config")
def clip_flow_save_config():
    global _AI_SEAM_NEGOTIATED_FPS_OVERRIDE
    if _ai_seam_batch_is_active():
        return jsonify(ok=False, error="Auto Make Frames กำลังทำงานอยู่ จึงล็อกการแก้ Playlist ชั่วคราวเพื่อให้ตรวจเฟรมได้ครบ"), 409
    body = request.get_json(silent=True)
    if body is None:
        body = {}
    if not isinstance(body, dict):
        return jsonify(ok=False, error="clip flow config payload must be an object"), 400
    current = _clip_config_snapshot()
    try:
        if "open" in body:
            current["open"] = _clip_path(body.get("open"), allow_blank=True)
        if "stop" in body:
            current["stop"] = _clip_path(body.get("stop"), allow_blank=True)
        if "normal" in body:
            normals = body.get("normal")
            if not isinstance(normals, list):
                return jsonify(ok=False, error="Normal ต้องเป็นรายการไฟล์วิดีโอ"), 400
            if len(normals) > 10000:
                return jsonify(ok=False, error="Normal playlist exceeds 10000 clips"), 400
            normalized = []
            seen = set()
            for item in normals:
                path = _clip_path(item, allow_blank=False)
                key = os.path.normcase(path)
                if key not in seen:
                    seen.add(key)
                    normalized.append(path)
            current["normal"] = normalized
        if "enabled" in body:
            current["enabled"] = bool(body.get("enabled"))
        if current.get("open") and not Path(current["open"]).is_file():
            return jsonify(ok=False, error="ไม่พบไฟล์ Open ที่บันทึกไว้"), 400
        if current.get("stop") and not Path(current["stop"]).is_file():
            return jsonify(ok=False, error="ไม่พบไฟล์ Stop ที่บันทึกไว้"), 400
        for item in current["normal"]:
            if not Path(item).is_file():
                return jsonify(ok=False, error=f"ไม่พบไฟล์ Normal: {item}"), 400
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)[:400]), 400
    # Commit under the same transition lock used by batch acquisition. The
    # first check above gives fast feedback; this second check closes the race
    # where a playlist edit began before Auto Make acquired its reservation.
    with clip_flow_transition_lock:
        if _ai_seam_batch_is_active():
            return jsonify(ok=False, error="Auto Make Frames เริ่มทำงานระหว่างแก้ Playlist — ไม่มีการบันทึกการเปลี่ยนแปลง"), 409
        with clip_flow_lock:
            clip_flow_config.clear()
            clip_flow_config.update(current)
        try:
            _persist_clip_config(current)
        except Exception as exc:
            return jsonify(ok=False, error=f"บันทึก Clip Flow ไม่สำเร็จ: {exc}"), 500
    # If OPEN changes while the camera is stopped, discard the old Source-FPS
    # hint; the next Start preflight will probe the new OPEN clip.
    if "open" in body and not vcam.active:
        _AI_SEAM_NEGOTIATED_FPS_OVERRIDE = 0
    return jsonify(**_clip_flow_public_state())


@app.post("/api/clip-flow/start")
def clip_flow_start():
    result = _clip_flow_begin_open(reason="settings_start")
    return (jsonify(**result), 200 if result.get("ok") else 400)


@app.post("/api/ai-seam/batch-lock")
def ai_seam_batch_lock_route():
    """Reserve Engine/source controls for the whole Auto Make Frames batch."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(ok=False, error="batch-lock payload must be an object"), 400
    action = str(body.get("action") or "").strip().lower()
    if action not in ("acquire", "heartbeat", "release"):
        return jsonify(ok=False, error="action must be acquire, heartbeat or release"), 400

    if action == "acquire":
        # Use the same transition lock as Engine and Clip Flow starts so two
        # operations cannot pass their preflight checks at the same time.
        with clip_flow_transition_lock:
            with lock:
                engine_running = bool(state.get("running", False))
                camera_running = bool(state.get("virtualcam", False) or vcam.active)
            with clip_flow_lock:
                flow_running = bool(clip_flow_runtime.get("active"))
            if engine_running or camera_running or flow_running:
                return jsonify(ok=False, error="Stop Engine, Virtual Camera and Clip Flow before Auto Make Frames."), 409
            if _ai_seam_generation_active(DATA):
                return jsonify(ok=False, error="An AI Seam generation job is already running."), 409
            now = time.monotonic()
            with ai_seam_batch_guard_lock:
                active_token = str(ai_seam_batch_guard.get("token") or "")
                if active_token and now < float(ai_seam_batch_guard.get("lease_until") or 0.0):
                    return jsonify(ok=False, error="Another Auto Make Frames batch already owns the Engine reservation."), 409
                token = uuid.uuid4().hex
                ai_seam_batch_guard.update(
                    token=token, lease_until=now + AI_SEAM_BATCH_LEASE_SECONDS,
                    started_at=now,
                )
            return jsonify(ok=True, token=token, lease_seconds=AI_SEAM_BATCH_LEASE_SECONDS)

    token = str(body.get("token") or "")
    now = time.monotonic()
    with ai_seam_batch_guard_lock:
        owned_token = str(ai_seam_batch_guard.get("token") or "")
        lease_until = float(ai_seam_batch_guard.get("lease_until") or 0.0)
        if not token or token != owned_token or now >= lease_until:
            if owned_token and now >= lease_until:
                ai_seam_batch_guard.update(token="", lease_until=0.0, started_at=0.0)
            return jsonify(ok=False, error="Auto Make Frames reservation expired; stop and retry the batch."), 409
        if action == "heartbeat":
            ai_seam_batch_guard["lease_until"] = now + AI_SEAM_BATCH_LEASE_SECONDS
            return jsonify(ok=True, lease_seconds=AI_SEAM_BATCH_LEASE_SECONDS)
        ai_seam_batch_guard.update(token="", lease_until=0.0, started_at=0.0)
        return jsonify(ok=True, released=True)


@app.post("/api/source/seek")
def source_seek():
    """Seek the selected source from Scene Builder when Clip Flow is not owning it."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(ok=False, error="seek payload must be an object"), 400
    try:
        position = float(body.get("position_seconds"))
        if not np.isfinite(position):
            raise ValueError("เวลา seek ต้องเป็นตัวเลขจำกัด")
    except (TypeError, ValueError, OverflowError) as exc:
        return jsonify(ok=False, error=f"เวลา seek ไม่ถูกต้อง: {str(exc)[:120]}"), 400

    with clip_flow_transition_lock:
        if _ai_seam_batch_is_active():
            return jsonify(ok=False, error="Auto Make Frames กำลังทำงานอยู่ กรุณารอให้สร้างเสร็จก่อนเลื่อนเวลาใน Scene Builder"), 409
        if _ai_seam_generation_active(DATA):
            return jsonify(ok=False, error="AI Seam กำลังสร้างเฟรมอยู่ กรุณารอให้เสร็จก่อนเลื่อนเวลาใน Scene Builder"), 409
        with clip_flow_lock:
            if bool(clip_flow_runtime.get("active")):
                return jsonify(ok=False, error="Clip Flow กำลังควบคุมคลิปอยู่ กรุณาใช้ Timeline ของ Flow"), 409
        with lock:
            current_path = str(state.get("source") or "")
            running = bool(state.get("running", False))
            audio_enabled = bool(state.get("virtualaudio_enabled", True))
            source_enabled = bool(state.get("source_enabled", True))
            loop_enabled = bool(state.get("loop", True))
            audio_offset = int(state.get("audio_offset_ms", 0))
        if not current_path or not running:
            return jsonify(ok=False, error="เริ่ม Engine และเลือก Source ก่อนจึงจะเลื่อนเวลาได้"), 409
        duration = float(source_loop_duration or 0.0)
        fps = float(source_fps or 30.0)
        if not np.isfinite(duration) or duration <= 0:
            return jsonify(ok=False, error="ยังอ่านความยาวคลิปไม่ได้ จึงยังเลื่อนเวลาไม่ได้"), 409
        position = min(max(0.0, position), max(0.0, duration - 1.0 / max(1.0, fps)))
        if not set_source(current_path, start_seconds=position, known_timing=(fps, duration)):
            return jsonify(ok=False, error="เลื่อนคลิปไม่สำเร็จ ตัวถอดรหัสปฏิเสธตำแหน่งนี้"), 500
        if vaudio.active:
            vaudio.stop()
        if audio_enabled and source_enabled:
            with source_buffer_lock:
                start_at = source_playback_origin
            if not vaudio.start(current_path, start_at=start_at, loop=loop_enabled,
                                loop_duration=duration, offset_ms=audio_offset,
                                source_offset=position):
                with lock:
                    state["virtualaudio_error"] = vaudio.error
        return jsonify(ok=True, position_seconds=position, duration_seconds=duration,
                       source_generation=int(source_decode_generation))


@app.post("/api/clip-flow/seek")
def clip_flow_seek():
    global clip_flow_eof_generation
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(ok=False, error="seek payload must be an object"), 400
    try:
        position = float(body.get("position_seconds"))
        if not np.isfinite(position):
            raise ValueError("เวลา seek ต้องเป็นตัวเลขจำกัด")
    except (TypeError, ValueError, OverflowError) as exc:
        return jsonify(ok=False, error=f"เวลา seek ไม่ถูกต้อง: {str(exc)[:120]}"), 400
    with clip_flow_transition_lock:
        if _ai_seam_batch_is_active():
            return jsonify(ok=False, error="Auto Make Frames กำลังทำงานอยู่ กรุณารอให้สร้างเสร็จก่อนเลื่อนเวลา"), 409
        if _ai_seam_generation_active(DATA):
            return jsonify(ok=False, error="AI Seam กำลังสร้างเฟรมอยู่ กรุณารอให้เสร็จก่อนเลื่อนเวลา"), 409
        with clip_flow_lock:
            active = bool(clip_flow_runtime.get("active"))
            phase = str(clip_flow_runtime.get("phase") or "")
            current_path = str(clip_flow_runtime.get("current_clip") or "")
        if not active or phase not in ("open", "normal", "stop") or not current_path:
            return jsonify(ok=False, error="เริ่ม Clip Flow ก่อนจึงจะเลื่อนเวลาได้"), 409
        with lock:
            actual_source = str(state.get("source") or "")
            running = bool(state.get("running", False))
        if not running or os.path.normcase(actual_source) != os.path.normcase(current_path):
            return jsonify(ok=False, error="คลิปปัจจุบันเปลี่ยนไปแล้ว กรุณารีเฟรชสถานะ"), 409
        duration = float(source_loop_duration or 0.0)
        if not np.isfinite(duration) or duration <= 0:
            return jsonify(ok=False, error="ยังอ่านความยาวคลิปไม่ได้ จึงยังเลื่อนเวลาไม่ได้"), 409
        guard = 1.0 / max(1.0, float(source_fps or 30.0))
        position = min(max(0.0, position), max(0.0, duration - guard))
        with clip_flow_lock:
            clip_flow_eof_generation = None
            clip_flow_eof_event.clear()
        if not set_source(current_path, start_seconds=position, known_timing=(source_fps, duration)):
            return jsonify(ok=False, error="เลื่อนคลิปไม่สำเร็จ ตัวถอดรหัสปฏิเสธตำแหน่งนี้"), 500
        _clip_start_audio(current_path, source_offset_seconds=position)
        with clip_flow_lock:
            clip_flow_runtime["source_generation"] = source_decode_generation
            clip_flow_runtime["reason"] = "seek"
            clip_flow_runtime["last_error"] = ""
            clip_flow_runtime["changed_at"] = time.time()
    return jsonify(**_clip_flow_public_state())


@app.post("/api/clip-flow/play-stop")
def clip_flow_play_stop():
    result = _clip_flow_begin_stop(reason="settings_stop_preview")
    return (jsonify(**result), 200 if result.get("ok") else 400)


@app.post("/api/source")
def api_source():
    # Remote transfer/validation must not hold the transition lock. The helper
    # takes it only when committing a source switch into the live pipeline.
    return _api_source_locked()


def _api_source_locked():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(ok=False, error="source payload must be an object"), 400
    src = str(body.get("source", "")).strip().strip('"')
    if src and not src.startswith(("http://", "https://", "/media/")):
        src = os.path.normpath(src)
    if src.startswith("/media/"):
        src = str(DATA / Path(src.removeprefix("/media/")).name)
    if not src:
        return jsonify(ok=False, error="source required"), 400
    if _ai_seam_batch_is_active() or _ai_seam_generation_active(DATA):
        return jsonify(ok=False, error="AI Seam is generating or verifying transition frames; wait before changing the active Source."), 409
    # Fail fast if Clip Flow already owns the active source; recheck under the
    # transition lock before commit to cover a Flow that starts mid-download.
    with clip_flow_lock:
        if clip_flow_runtime.get("active"):
            return jsonify(ok=False, error="Clip Flow กำลังทำงานอยู่ กรุณาหยุด Flow ก่อนเปลี่ยน Source"), 409
    downloaded_target = None
    if src.startswith("http://") or src.startswith("https://"):
        # HLS is intentionally unsupported. Download to a unique temp file with
        # timeouts/size bounds; never expose a partial transfer as a media file.
        downloaded_target = DATA / ("remote_" + uuid.uuid4().hex + ".mp4")
        temp_target = DATA / (".remote_download_" + uuid.uuid4().hex + ".tmp")
        try:
            total = 0
            with urllib.request.urlopen(src, timeout=20) as response:
                raw_length = response.headers.get("Content-Length")
                if raw_length:
                    try:
                        if int(raw_length) > MAX_MEDIA_UPLOAD_BYTES:
                            raise ValueError("remote media exceeds the configured size limit")
                    except ValueError as exc:
                        if "exceeds" in str(exc):
                            raise
                with temp_target.open("wb") as out:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_MEDIA_UPLOAD_BYTES:
                            raise ValueError("remote media exceeds the configured size limit")
                        out.write(chunk)
            if total <= 0:
                raise ValueError("remote media download is empty")
            os.replace(str(temp_target), str(downloaded_target))
            src = str(downloaded_target)
        except Exception as e:
            try:
                temp_target.unlink(missing_ok=True)
            except Exception:
                pass
            return jsonify(error=str(e)[:300]), 400
    if not Path(src).exists() and not src.startswith(("http://","https://")):
        if downloaded_target is not None:
            downloaded_target.unlink(missing_ok=True)
        return jsonify(error="source not found"), 404

    with clip_flow_transition_lock:
        if _ai_seam_batch_is_active() or _ai_seam_generation_active(DATA):
            if downloaded_target is not None:
                downloaded_target.unlink(missing_ok=True)
            return jsonify(ok=False, error="AI Seam กำลังสร้าง/ตรวจเฟรม จึงยังไม่ได้เปลี่ยน Source"), 409
        with clip_flow_lock:
            if clip_flow_runtime.get("active"):
                if downloaded_target is not None:
                    downloaded_target.unlink(missing_ok=True)
                return jsonify(ok=False, error="Clip Flow เริ่มทำงานระหว่างดาวน์โหลด Source; ไม่ได้เปลี่ยนคลิปปัจจุบัน"), 409
        if not set_source(src):
            if downloaded_target is not None:
                downloaded_target.unlink(missing_ok=True)
            return jsonify(error="cannot open source"), 400
        # Selecting a video is itself an explicit request to add/show Source.
        with lock:
            order = list(state.get("layer_order", []))
            if "source" not in order:
                order.append("source")
            state["layer_order"] = order
            state["source_enabled"] = True
    return jsonify(ok=True, source=src, layer_order=order)

@app.post("/api/media")
def upload_media():
    if request.content_length and request.content_length > MAX_MEDIA_UPLOAD_BYTES:
        return jsonify(ok=False, error="ไฟล์ใหญ่เกิน 64 GB — ถ้าไม่ต้องการทำสำเนา ให้ใช้ Local Source เพื่ออ่านจากดิสก์โดยตรง"), 413
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(ok=False, error="video file required"), 400
    # Media uploads and Source activation are separate actions. Clip Flow's
    # browser fallback uploads multiple queued clips; those files must not replace
    # the currently playing Source as a side effect. Existing callers keep the
    # historical activate-by-default behavior.
    activate_raw = str(request.form.get("activate_source", "true") or "true").strip().lower()
    activate_source = activate_raw not in {"0", "false", "no", "off"}
    if activate_source:
        if _ai_seam_batch_is_active() or _ai_seam_generation_active(DATA):
            return jsonify(ok=False, error="AI Seam is generating or verifying transition frames; wait before activating an uploaded Source."), 409
        with clip_flow_lock:
            if clip_flow_runtime.get("active"):
                return jsonify(ok=False, error="Clip Flow กำลังทำงานอยู่ กรุณาหยุด Flow ก่อนเปลี่ยน Source"), 409
    safe_name = Path(f.filename).name
    target = DATA / safe_name
    # Never overwrite a file that may still be in use by the decoder.
    if target.exists():
        stem, suffix = target.stem, target.suffix
        n = 2
        while True:
            candidate = DATA / f"{stem} ({n}){suffix}"
            if not candidate.exists():
                target = candidate
                break
            n += 1
    temp_target = DATA / (".upload_" + uuid.uuid4().hex + ".tmp")
    try:
        if temp_target.exists():
            temp_target.unlink()
        f.save(temp_target)
        if not temp_target.exists() or temp_target.stat().st_size == 0:
            temp_target.unlink(missing_ok=True)
            return jsonify(ok=False, error="uploaded file is empty"), 400
        # Validate the completed temp file before publishing it to the library.
        probe = cv2.VideoCapture(str(temp_target))
        valid_video = bool(probe.isOpened())
        if valid_video:
            ok_frame, _probe_frame = probe.read()
            valid_video = bool(ok_frame)
        probe.release()
        if not valid_video:
            temp_target.unlink(missing_ok=True)
            return jsonify(ok=False, error="uploaded video cannot be decoded"), 400
        # Never expose a partial large upload as a usable Media item.
        os.replace(str(temp_target), str(target))
    except Exception as e:
        try:
            temp_target.unlink(missing_ok=True)
        except Exception:
            pass
        return jsonify(ok=False, error=f"upload failed: {e}"), 500
    if not activate_source:
        # Upload-only mode is used by Clip Flow's browser file picker. Preserve
        # the current scene/source and allow queue preparation even while a Flow
        # is running; the queue is applied independently by /api/clip-flow/config.
        with lock:
            order = list(state.get("layer_order", []))
        return jsonify(ok=True, uploaded=True, activated=False, source=str(target),
                       name=safe_name, url="/media/" + target.name, layer_order=order)

    with clip_flow_transition_lock:
        if _ai_seam_batch_is_active() or _ai_seam_generation_active(DATA):
            return jsonify(ok=False, uploaded=True,
                           error="อัปโหลดไฟล์แล้ว แต่ AI Seam กำลังสร้าง/ตรวจเฟรม จึงยังไม่ได้เปลี่ยน Source",
                           source=str(target), name=safe_name, url="/media/" + target.name), 409
        with clip_flow_lock:
            if clip_flow_runtime.get("active"):
                # The upload is complete and remains available in the Media library;
                # do not interrupt a Flow that began while the file was uploading.
                return jsonify(ok=False, uploaded=True,
                               error="อัปโหลดไฟล์แล้ว แต่ Clip Flow เริ่มระหว่างอัปโหลด จึงยังไม่ได้เปลี่ยน Source",
                               source=str(target), name=safe_name, url="/media/" + target.name), 409
        if not set_source(str(target)):
            return jsonify(ok=False, uploaded=True, error="uploaded video cannot be decoded",
                           source=str(target), name=safe_name, url="/media/" + target.name), 400
        with lock:
            order = list(state.get("layer_order", []))
            if "source" not in order:
                order.append("source")
            state["layer_order"] = order
            state["source_enabled"] = True
    return jsonify(ok=True, uploaded=True, activated=True, source=str(target), name=safe_name,
                   url="/media/" + target.name, layer_order=order)

@app.get("/media/<path:name>")
def media_file(name):
    return send_from_directory(DATA, name, as_attachment=False)

@app.get("/source-media")
def source_media():
    with lock:
        src = state.get("source")
    if not src:
        return ("", 404)
    p = Path(src)
    if not p.is_file():
        return ("", 404)
    try:
        return send_file(p, conditional=True, max_age=0)
    except Exception:
        return ("", 404)

def _attach_image_layer(path, iw, ih):
    """Using an image always creates/shows the Image layer (no manual '+' first)."""
    scale = min(1.0, 220.0 / max(iw, ih))
    with lock:
        state["image"] = path
        state["image_width"] = max(1, int(iw * scale))
        state["image_height"] = max(1, int(ih * scale))
        state["image_pan_x"] = 0; state["image_pan_y"] = 0
        state["image_zoom"] = 1.0; state["image_fit"] = "contain"
        state["image_enabled"] = True
        order = list(state.get("layer_order", []))
        if "image" not in order:
            order.insert(0, "image")
        state["layer_order"] = order

@app.post("/api/overlay")
def upload_overlay():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(ok=False, error="image file required"), 400
    target = DATA / ("overlay_" + Path(f.filename).name)
    f.save(target)
    if not target.exists() or target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        return jsonify(ok=False, error="uploaded image is empty"), 400
    probe = cv2.imread(str(target), cv2.IMREAD_UNCHANGED)
    if probe is None:
        target.unlink(missing_ok=True)
        return jsonify(ok=False, error="image format could not be decoded"), 400
    ih, iw = probe.shape[:2]
    _attach_image_layer(str(target), iw, ih)
    return jsonify(ok=True, image=str(target), url="/media/" + target.name,
                   width=state["image_width"], height=state["image_height"],
                   layer_order=list(state.get("layer_order", [])))

@app.get("/api/system/hardware")
@app.get("/api/hardware")
def system_hardware():
    """Probe the local PC and return safe, actionable PAOKRA performance recommendations."""
    import platform, shutil
    powershell_exe = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    cpu_count = os.cpu_count() or 1
    cpu_physical = 0
    try:
        p = subprocess.run([powershell_exe, "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfCores -Sum).Sum"], capture_output=True, text=True, timeout=5)
        cpu_physical = int(float(p.stdout.strip() or 0))
    except Exception:
        cpu_physical = max(1, cpu_count // 2)
    ram_gb = 0.0
    try:
        p = subprocess.run([powershell_exe, "-NoProfile", "-Command",
                            "[math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1)"],
                           capture_output=True, text=True, timeout=5)
        ram_gb = float(p.stdout.strip() or 0)
    except Exception:
        pass
    gpu_names = []
    try:
        p = subprocess.run([powershell_exe, "-NoProfile", "-Command",
                            "(Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name) -join '||'"],
                           capture_output=True, text=True, timeout=5)
        gpu_names = [x.strip() for x in p.stdout.strip().split("||") if x.strip()]
    except Exception:
        pass
    if not ram_gb:
        try:
            import psutil
            ram_gb = round(psutil.virtual_memory().total / (1024**3), 1)
        except Exception:
            ram_gb = 0.0
    # The export UI currently launches FFmpeg through PATH, so capability
    # recommendations must probe that same executable, not a different bundled
    # binary that may advertise a different encoder set.
    ffmpeg_exe = shutil.which("ffmpeg.exe") or shutil.which("ffmpeg")
    enc = ""
    if ffmpeg_exe:
        try:
            p = subprocess.run([ffmpeg_exe, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=10)
            enc = (p.stdout + "\n" + p.stderr).lower()
        except Exception:
            pass
    available = {
        "amd_amf": "h264_amf" in enc,
        "nvidia_nvenc": "h264_nvenc" in enc,
        "intel_qsv": "h264_qsv" in enc,
        "cpu_x264": "libx264" in enc,
    }
    ffmpeg_hwaccels = []
    if ffmpeg_exe:
        try:
            p = subprocess.run([ffmpeg_exe, "-hide_banner", "-hwaccels"], capture_output=True, text=True, timeout=10)
            ffmpeg_hwaccels = [line.strip().lower() for line in p.stdout.splitlines() if line.strip() and not line.lower().startswith("hardware acceleration methods")]
        except Exception:
            pass
    vulkan_supported = bool(
        shutil.which("vulkaninfo") or shutil.which("vulkaninfo.exe") or
        Path(r"C:\Windows\System32\vulkan-1.dll").exists() or
        "vulkan" in enc or "vulkan" in ffmpeg_hwaccels
    )
    rocm_supported = bool(
        shutil.which("rocminfo") or shutil.which("rocminfo.exe") or
        shutil.which("hipinfo") or shutil.which("hipinfo.exe") or
        os.environ.get("ROCM_PATH") or os.environ.get("HIP_PATH")
    )
    gpu_text = " ".join(gpu_names).lower()
    if available["amd_amf"] and ("amd" in gpu_text or "radeon" in gpu_text):
        recommended_encoder = "amd"
        reason = "พบ AMD Radeon + H.264 AMF"
    elif available["nvidia_nvenc"] and ("nvidia" in gpu_text or "geforce" in gpu_text or "quadro" in gpu_text):
        recommended_encoder = "cuda"
        reason = "พบ NVIDIA + H.264 NVENC"
    elif available["intel_qsv"] and ("intel" in gpu_text):
        recommended_encoder = "qsv"
        reason = "พบ Intel GPU + H.264 QSV"
    else:
        recommended_encoder = "cpu"
        reason = "ไม่พบ GPU H.264 encoder ที่จับคู่ได้ จึงใช้ CPU x264"
    # OpenCV parallel work competes with decoding, Electron, and the receiving
    # application. Recommend based on physical cores where available, reserving
    # two physical cores on larger CPUs (one on small CPUs), rather than treating
    # every Hyper-Thread as a full core. The slider can still be raised manually.
    if cpu_physical > 0:
        reserve = 2 if cpu_physical >= 8 else (1 if cpu_physical >= 4 else 0)
        recommended_cpu_threads = max(1, min(cpu_count, cpu_physical - reserve))
    elif cpu_count >= 16:
        recommended_cpu_threads = max(1, cpu_count - 4)
    elif cpu_count >= 8:
        recommended_cpu_threads = max(1, cpu_count - 2)
    else:
        recommended_cpu_threads = cpu_count
    preview_quality = "balanced" if cpu_count >= 16 and ram_gb >= 12 else "fast"
    recommended_filter_preview_fps = 4 if cpu_count >= 8 else 2
    recommended_decode_backend = "hardware" if any(x in ffmpeg_hwaccels for x in ("d3d11va","dxva2","vulkan","cuda")) else "software"
    gpu_compute_capability = "Vulkan + ROCm" if vulkan_supported and rocm_supported else ("Vulkan" if vulkan_supported else ("ROCm" if rocm_supported else "CPU"))
    recommended_render_backend = "auto"
    profile_name = "GPU H.264" if recommended_encoder != "cpu" else "CPU Safe"
    return jsonify(ok=True, platform=platform.platform(), cpu_count=cpu_count, cpu_physical=cpu_physical,
                    ram_gb=ram_gb, gpu_names=gpu_names, encoders=available,
                    ffmpeg_hwaccels=ffmpeg_hwaccels, vulkan_supported=vulkan_supported, rocm_supported=rocm_supported,
                    recommended_encoder=recommended_encoder, recommended_cpu_threads=recommended_cpu_threads,
                    cpu_threads_current=int(state.get("cpu_threads") or 1),
                    recommended_filter_preview_fps=recommended_filter_preview_fps,
                    recommended_decode_backend=recommended_decode_backend, recommended_render_backend=recommended_render_backend,
                    preview_quality=preview_quality, reason=reason, profile_name=profile_name,
                    filter_backend="OpenCV CPU", encoder_backend=available)

@app.post("/api/settings")
def settings():
    global _AI_SEAM_NEGOTIATED_FPS_OVERRIDE
    body = request.get_json(silent=True)
    if body is None:
        body = {}
    if not isinstance(body, dict):
        return jsonify(ok=False, error="settings payload must be an object"), 400
    if _ai_seam_batch_is_active():
        return jsonify(ok=False, error="Auto Make Frames กำลังตรวจเฟรมคั่นอยู่ จึงล็อกการเปลี่ยน Scene/Output Settings ชั่วคราว"), 409

    # Reject malformed numeric settings before mutating global render state.
    # A bad UI/API value must not surface as HTTP 500 or poison the render loop.
    integer_settings = (
        "fps", "width", "height", "cpu_threads", "text_x", "text_y", "text_size",
        "image_x", "image_y", "image_width", "image_height", "image_pan_x", "image_pan_y",
        "source_x", "source_y", "source_width", "source_height",
        "web_x", "web_y", "web_width", "web_height", "audio_offset_ms",
    )
    try:
        for key in integer_settings:
            if key in body:
                value = float(body[key])
                if not np.isfinite(value):
                    raise ValueError(f"{key} must be finite")
                body[key] = int(value)
        for key in ("image_zoom", "loop_variation_strength"):
            if key in body:
                value = float(body[key])
                if not np.isfinite(value):
                    raise ValueError(f"{key} must be finite")
                body[key] = value
    except (TypeError, ValueError, OverflowError) as exc:
        return jsonify(ok=False, error=f"invalid numeric setting: {str(exc)[:180]}"), 400

    # Bound render geometry before it can trigger a giant resize/allocation.
    # Output itself is preset-limited; larger hidden layers cost memory but do
    # not improve the visible canvas on this compositor.
    coordinate_keys = (
        "text_x", "text_y", "image_x", "image_y", "image_pan_x", "image_pan_y",
        "source_x", "source_y", "web_x", "web_y",
    )
    for key in coordinate_keys:
        if key in body and abs(body[key]) > 100000:
            return jsonify(ok=False, error=f"{key} is outside the safe range"), 400
    if "text_size" in body:
        body["text_size"] = max(6, min(400, body["text_size"]))
    for key in ("image_width", "image_height"):
        if key in body:
            body[key] = max(1, min(1920, body[key]))
    for key in ("source_width", "source_height"):
        if key in body:
            body[key] = max(40, min(4096, body[key]))
    for key in ("web_width", "web_height"):
        if key in body:
            body[key] = max(64, min(1920, body[key]))

    if "layer_order" in body:
        order = body["layer_order"]
        known_layers = {"source", "image", "text", "web"}
        if (not isinstance(order, list) or any(not isinstance(item, str) for item in order)
                or len(order) != len(set(order)) or not set(order).issubset(known_layers)):
            return jsonify(ok=False, error="layer_order must contain unique layer ids from source,image,text,web"), 400
    if "texts" in body:
        incoming_texts = body["texts"]
        if not isinstance(incoming_texts, list) or any(not isinstance(item, dict) for item in incoming_texts):
            return jsonify(ok=False, error="texts must be a list of text-layer objects"), 400
        if len(incoming_texts) > 128:
            return jsonify(ok=False, error="at most 128 text layers are allowed"), 400
        normalized_texts = []
        for index, incoming in enumerate(incoming_texts):
            item = dict(incoming)
            if "scroll" in item:
                if not isinstance(item["scroll"], bool):
                    if item["scroll"] not in (0, 1, "0", "1", "true", "false"):
                        return jsonify(ok=False, error=f"text layer {index} has invalid scroll"), 400
                    item["scroll"] = str(item["scroll"]).lower() in ("1", "true")
            if "scroll_speed" in item:
                try:
                    speed = float(item["scroll_speed"])
                    if not np.isfinite(speed): raise ValueError("scroll_speed must be finite")
                    item["scroll_speed"] = max(20, min(600, int(speed)))
                except (TypeError, ValueError, OverflowError) as exc:
                    return jsonify(ok=False, error=f"text layer {index} has invalid scroll_speed: {str(exc)[:100]}"), 400
            if "text" in item:
                if not isinstance(item["text"], str):
                    return jsonify(ok=False, error=f"text layer {index} text must be a string"), 400
                item["text"] = item["text"][:4000]
            for key in ("x", "y", "size", "stroke"):
                if key not in item:
                    continue
                try:
                    value = float(item[key])
                    if not np.isfinite(value):
                        raise ValueError(f"{key} must be finite")
                    value = int(value)
                except (TypeError, ValueError, OverflowError) as exc:
                    return jsonify(ok=False, error=f"text layer {index} has invalid {key}: {str(exc)[:100]}"), 400
                if key in ("x", "y"):
                    if abs(value) > 100000:
                        return jsonify(ok=False, error=f"text layer {index} {key} is outside the safe range"), 400
                elif key == "size":
                    value = max(6, min(400, value))
                else:
                    value = max(0, min(20, value))
                item[key] = value
            if "enabled" in item and not isinstance(item["enabled"], bool):
                if item["enabled"] not in (0, 1):
                    return jsonify(ok=False, error=f"text layer {index} enabled must be boolean"), 400
                item["enabled"] = bool(item["enabled"])
            normalized_texts.append(item)
        body["texts"] = normalized_texts
    if "decode_backend" in body:
        decode_backend = str(body.get("decode_backend") or "auto").lower()
        if decode_backend not in ("auto", "hardware", "software"):
            return jsonify(ok=False, error="decode_backend must be auto, hardware, or software"), 400
        body["decode_backend"] = decode_backend
    if "render_backend" in body:
        render_backend = str(body.get("render_backend") or "auto").lower()
        # Current compositor is OpenCV/NumPy CPU. Do not persist GPU selections
        # until a real Vulkan/ROCm render pipeline is implemented and verified.
        if render_backend not in ("auto", "cpu"):
            return jsonify(ok=False, error="GPU render backend is not implemented yet; choose auto or cpu"), 400
        body["render_backend"] = render_backend
    if "fps_mode" in body:
        fps_mode = str(body.get("fps_mode") or "fixed").lower()
        if fps_mode not in ("fixed", "source"):
            return jsonify(ok=False, error="fps_mode must be fixed or source"), 400
        body["fps_mode"] = fps_mode

    allowed = ("text","loop","fps","fps_mode","width","height","text_x","text_y","text_size",
               "image_x","image_y","image_width","image_height","image_pan_x","image_pan_y","image_fit","image_zoom","audio_offset_ms",
               "source_x","source_y","source_width","source_height","source_fit_mode","source_flip_h","source_flip_v",
               "source_enabled","text_enabled","image_enabled","texts","layer_order",
               "web_url","web_x","web_y","web_width","web_height","web_enabled",
               "loop_variation_enabled","loop_variation_strength","source_fit_mode","source_flip_h","source_flip_v",
               "cpu_threads","decode_backend","render_backend","video_fx")
    with clip_flow_transition_lock:
        if _ai_seam_batch_is_active():
            return jsonify(ok=False, error="Auto Make Frames เริ่มทำงานระหว่างแก้ Scene/Output — ไม่มีการเปลี่ยนค่าที่กำลังใช้สร้างเฟรม"), 409
        with lock:
            if state.get("virtualcam") and ("width" in body or "height" in body or "fps" in body or "fps_mode" in body):
                return jsonify(ok=False, error="หยุด Virtual Camera ก่อนเปลี่ยน Output Resolution/FPS"), 409
            if "width" in body or "height" in body:
                nw, nh = int(body.get("width", state["width"])), int(body.get("height", state["height"]))
                if (nw, nh) not in OUTPUT_PRESETS:
                    return jsonify(ok=False, error="unsupported output resolution"), 400
                state["width"], state["height"] = nw, nh
            if "fps" in body:
                nf = int(body["fps"])
                if nf not in FPS_PRESETS: return jsonify(ok=False, error="unsupported FPS"), 400
                state["fps"] = nf
            if "cpu_threads" in body:
                n = max(1, min(int(body["cpu_threads"]), os.cpu_count() or 1))
                state["cpu_threads"] = n
                _apply_cpu_threads(n)
            if "decode_backend" in body:
                state["decode_backend"] = str(body["decode_backend"] or "auto").lower()
            if "render_backend" in body:
                state["render_backend"] = str(body["render_backend"] or "auto").lower()
            for k in allowed:
                if k in body and k not in {"width","height","fps","video_fx"}: state[k] = body[k]
            if state.get("image_fit") not in ("stretch", "cover", "contain", "free"):
                state["image_fit"] = "contain"
            try: state["image_zoom"] = max(0.05, min(20.0, float(state.get("image_zoom", 1.0))))
            except Exception: state["image_zoom"] = 1.0
            try: state["audio_offset_ms"] = max(-500, min(1500, int(float(state.get("audio_offset_ms", 0)))))
            except Exception: state["audio_offset_ms"] = 0
            if "audio_offset_ms" in body:
                try: vaudio.set_offset(state["audio_offset_ms"])
                except Exception: pass
            if "video_fx" in body:
                incoming = body.get("video_fx") if isinstance(body.get("video_fx"), dict) else {}
                fx = {**FX_DEFAULT, **state.get("video_fx", {})}
                for k,v in incoming.items():
                    if k in FX_DEFAULT:
                        fx[k] = float(v) if k in {"contrast","saturation","warmth"} else int(float(v))
                fx["brightness"] = max(-100,min(100,fx["brightness"]))
                fx["contrast"] = max(0.50,min(1.50,float(fx["contrast"])))
                fx["saturation"] = max(0.00,min(2.00,float(fx["saturation"])))
                fx["warmth"] = max(-1.00,min(1.00,float(fx["warmth"])))
                for k in ("sharpen","vignette","skin_smooth","wrinkle_remove","skin_bright","lip_tint"):
                    fx[k] = max(0,min(100,int(fx[k])))
                for k in ("face_slim","forehead_width","forehead_height","midface_width","cheekbone","temple","face_length","jaw_refine","jaw_width","jaw_curve","jaw_angle","chin","chin_height","chin_roundness","chin_tip","eye_size"):
                    fx[k] = max(-100,min(100,int(fx[k])))
                state["video_fx"] = fx
                # Filter work is intentionally asynchronous. Slider requests must
                # return immediately; the source decode worker applies the latest
                # video_fx on the next decoded frame.
            if "width" in body or "height" in body:
                if state.get("source_fit_mode", "cover") == "cover" and state.get("source"):
                    state["source_x"], state["source_y"] = 0, 0
                    state["source_width"], state["source_height"] = state["width"], state["height"]
                else:
                    fit_source_to_output()
            if any(k in body for k in ("cpu_threads","decode_backend","render_backend")):
                try:
                    PERF_FILE.write_text(json.dumps({k:state[k] for k in ("cpu_threads","decode_backend","render_backend")}, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception:
                    pass
    # A settings change may alter the camera modes advertised by the driver;
    # force the next Start preflight to negotiate again instead of reusing stale FPS.
    if any(key in body for key in ("width", "height", "fps", "fps_mode")):
        _AI_SEAM_NEGOTIATED_FPS_OVERRIDE = 0
    return jsonify(ok=True, width=state["width"], height=state["height"], fps=state["fps"] )

@app.post("/api/text")
def text_layer():
    body = request.get_json(silent=True)
    if not isinstance(body, dict): return jsonify(ok=False, error="text payload must be an object"), 400
    action = body.get("action", "add")
    if action not in ("add", "update", "remove"): return jsonify(ok=False, error="unknown text-layer action"), 400
    try:
        for key in ("x", "y", "size", "scroll_speed"):
            if key not in body: continue
            value = float(body[key])
            if not np.isfinite(value): raise ValueError(f"{key} must be finite")
            value = int(value)
            if key in ("x", "y") and abs(value) > 100000: raise ValueError(f"{key} is outside the safe range")
            if key == "size": value = max(6, min(400, value))
            if key == "scroll_speed": value = max(20, min(600, value))
            body[key] = value
        if "text" in body and not isinstance(body["text"], str): return jsonify(ok=False, error="text must be a string"), 400
        for flag in ("enabled", "scroll"):
            if flag in body and not isinstance(body[flag], bool):
                if body[flag] not in (0, 1, "0", "1", "true", "false"): return jsonify(ok=False, error=f"{flag} must be boolean"), 400
                body[flag] = str(body[flag]).lower() in ("1", "true")
    except (TypeError, ValueError, OverflowError) as exc:
        return jsonify(ok=False, error=f"invalid text position/size: {str(exc)[:160]}"), 400
    with lock:
        texts = [dict(item) for item in (state.get("texts") or []) if isinstance(item, dict)]
        order = list(state.get("layer_order") or [])
        if action == "add":
            if len(texts) >= 128: return jsonify(ok=False, error="at most 128 text layers are allowed"), 400
            item = {"id": uuid.uuid4().hex[:8], "text": body.get("text", "")[:4000],
                    "x": int(body.get("x", 24)), "y": int(body.get("y", 56)),
                    "size": max(6, min(400, int(body.get("size", 28)))),
                    "enabled": bool(body.get("enabled", True)), "scroll": bool(body.get("scroll", False)),
                    "scroll_speed": max(20, min(600, int(body.get("scroll_speed", 120))))}
            texts.append(item); state["texts"] = texts
            if "text" not in order: order.append("text")
            state["layer_order"] = order
            return jsonify(ok=True, item=item, texts=texts, layer_order=order)
        tid = str(body.get("id", ""))
        idx = next((i for i, item in enumerate(texts) if item.get("id") == tid), -1)
        if idx < 0: return jsonify(ok=False, error="text not found"), 404
        if action == "update":
            item = texts[idx]
            if "text" in body: item["text"] = body["text"][:4000]
            for key in ("x", "y", "size", "enabled", "scroll", "scroll_speed"):
                if key in body: item[key] = body[key]
            item["size"] = max(6, min(400, int(item.get("size", 28))))
            item["scroll_speed"] = max(20, min(600, int(item.get("scroll_speed", 120))))
            item["scroll"] = bool(item.get("scroll", False)); item["enabled"] = bool(item.get("enabled", True))
        elif action == "remove":
            texts.pop(idx)
            if not texts and not bool(state.get("legacy_text_added", False)):
                order = [layer for layer in order if layer != "text"]
                state["layer_order"] = order
        state["texts"] = texts
    return jsonify(ok=True, texts=texts, layer_order=order)

@app.post("/api/layers/reorder")
def reorder_layers():
    with clip_flow_transition_lock:
        with source_switch_lock:
            return _reorder_layers_locked()


def _reorder_layers_locked():
    global cap, source_decode_thread, source_decode_generation
    global source_frame, source_frame_serial, source_prepared_frame, source_prepared_serial
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="layer order payload must be an object"), 400
    order = body.get("order")
    known = {"source","image","text","web"}
    if (not isinstance(order, list) or any(not isinstance(item, str) for item in order)
            or len(order) != len(set(order)) or not set(order).issubset(known)):
        return jsonify(error="order must contain unique layer ids from source,image,text,web"), 400

    source_removed = "source" not in order
    if source_removed:
        with clip_flow_lock:
            if clip_flow_runtime.get("active"):
                return jsonify(ok=False, error="Stop Clip Flow before removing the Source layer"), 409
    old_cap = None
    old_thread = None
    if source_removed:
        # Removing the Video layer is a real source detach. Stop audio first,
        # then invalidate and join the decoder before releasing its capture.
        vaudio.stop()
        source_decode_stop.set()
        old_thread = source_decode_thread
        if old_thread and old_thread.is_alive() and old_thread is not threading.current_thread():
            old_thread.join(timeout=1.0)
        with lock:
            old_cap = cap
            cap = None
            source_decode_thread = None
            source_decode_generation += 1
            state["running"] = False
            state["source"] = None
            state["source_enabled"] = False
            state["source_serial"] = int(state.get("source_serial", 0)) + 1
            # Keep the buffer/frame lock order consistent with the live renderer.
            with source_buffer_lock:
                source_buffer.clear()
                source_playback_origin = None
                source_sequence_index = 0
                with source_frame_lock:
                    source_frame = None
                    source_prepared_frame = None
                    source_frame_serial += 1
                    source_prepared_serial += 1
    with lock:
        state["layer_order"] = order
        if "source" in order:
            state["source_enabled"] = True
    if old_cap is not None:
        if old_thread is not None and old_thread.is_alive():
            threading.Thread(
                target=_release_capture_after_thread,
                args=(old_thread, old_cap),
                name="paokra-detach-capture-release",
                daemon=True,
            ).start()
        else:
            try:
                old_cap.release()
            except Exception:
                pass
    return jsonify(ok=True, order=order, source_enabled=state["source_enabled"], source=state.get("source"))

# ---------- Offline Video to Filter ----------
@app.post("/api/video-filter/preview")
def video_filter_preview():
    """Process one current-frame preview so Beauty/Face sliders are visible while editing."""
    frame_file = request.files.get("frame")
    if not frame_file:
        return jsonify(ok=False, error="preview frame required"), 400
    try:
        raw = np.frombuffer(frame_file.read(), dtype=np.uint8)
        frame = cv2.imdecode(raw, cv2.IMREAD_COLOR)
        if frame is None:
            return jsonify(ok=False, error="preview frame cannot be decoded"), 400
        fx_in = json.loads(request.form.get("fx", "{}") or "{}")
        fx = {**FX_DEFAULT}
        if isinstance(fx_in, dict):
            for k in FX_DEFAULT:
                if k in fx_in:
                    fx[k] = float(fx_in[k])
        fx["brightness"] = max(-100, min(100, float(fx["brightness"])))
        fx["contrast"] = max(.5, min(1.5, float(fx["contrast"])))
        fx["saturation"] = max(0, min(2, float(fx["saturation"])))
        fx["warmth"] = max(-1, min(1, float(fx["warmth"])))
        for k in ("sharpen","vignette","skin_smooth","wrinkle_remove","skin_bright","lip_tint"):
            fx[k] = max(0, min(100, int(float(fx[k]))))
        for k in ("face_slim","forehead_width","forehead_height","midface_width","cheekbone","temple","face_length","jaw_refine","jaw_width","jaw_curve","jaw_angle","chin","chin_height","chin_roundness","chin_tip","eye_size"):
            fx[k] = max(-100, min(100, int(float(fx[k]))))
        out = _apply_video_fx(frame, fx)
        ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 88])
        if not ok:
            return jsonify(ok=False, error="preview encode failed"), 500
        return Response(buf.tobytes(), mimetype="image/jpeg",
                        headers={"Cache-Control":"no-store"})
    except Exception as e:
        return jsonify(ok=False, error="preview failed: " + type(e).__name__ + ": " + str(e)[:200]), 500

@app.get("/api/video-filter/progress/<job_id>")
def video_filter_progress(job_id):
    with V2F_JOB_LOCK: job = dict(V2F_JOBS.get(job_id, {"phase":"unknown","progress":0,"error":"job not found"}))
    return jsonify(job)

@app.post("/api/video-filter/export")
def video_filter_export():
    """Render a selected clip offline at exactly 60 FPS, then return the finished MP4."""
    import tempfile
    import shutil
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(ok=False, error="video file required"), 400
    raw_fx = request.form.get("fx", "{}")
    encoder_requested = str(request.form.get("encoder", "auto") or "auto").strip().lower()
    try:
        fx_in = json.loads(raw_fx) if raw_fx else {}
        fx = {**FX_DEFAULT}
        if isinstance(fx_in, dict):
            for k in FX_DEFAULT:
                if k in fx_in:
                    fx[k] = float(fx_in[k])
        fx["brightness"] = max(-100, min(100, float(fx["brightness"])))
        fx["contrast"] = max(.5, min(1.5, float(fx["contrast"])))
        fx["saturation"] = max(0, min(2, float(fx["saturation"])))
        fx["warmth"] = max(-1, min(1, float(fx["warmth"])))
        for k in ("sharpen","vignette","skin_smooth","wrinkle_remove","skin_bright","lip_tint"):
            fx[k] = max(0, min(100, int(float(fx[k]))))
        for k in ("face_slim","forehead_width","forehead_height","midface_width","cheekbone","temple","face_length","jaw_refine","jaw_width","jaw_curve","jaw_angle","chin","chin_height","chin_roundness","chin_tip","eye_size"):
            fx[k] = max(-100, min(100, int(float(fx[k]))))
    except Exception:
        return jsonify(ok=False, error="invalid filter settings"), 400

    token = uuid.uuid4().hex
    job_id = request.headers.get("X-PAOKRA-Job-Id", "").strip() or token
    with V2F_JOB_LOCK: V2F_JOBS[job_id] = {"phase":"upload","progress":5,"error":None}
    src_path = DATA / (".v2f_src_" + token + Path(f.filename).suffix.lower())
    video_path = DATA / (".v2f_video_" + token + ".mp4")
    final_path = DATA / ("PAOKRA_" + Path(f.filename).stem + "_60fps.mp4")
    n = 2
    while final_path.exists():
        final_path = DATA / ("PAOKRA_" + Path(f.filename).stem + f"_60fps_{n}.mp4")
        n += 1
    try:
        f.save(src_path)
        with V2F_JOB_LOCK: V2F_JOBS[job_id] = {"phase":"render","progress":10,"error":None}
        cap = cv2.VideoCapture(str(src_path), cv2.CAP_FFMPEG)
        if not cap.isOpened():
            cap = cv2.VideoCapture(str(src_path))
        if not cap.isOpened():
            raise RuntimeError("video cannot be decoded")
        sw = max(1, int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1))
        sh = max(1, int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 1))
        # H.264/yuv420p requires even dimensions. Crop at most one pixel so
        # odd-sized phone/video sources cannot make AMF/NVENC/x264 reject export.
        sw = max(2, sw - (sw % 2))
        sh = max(2, sh - (sh % 2))
        sf = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
        if not np.isfinite(sf) or sf < 1 or sf > 240: sf = 30.0

        # FFmpeg handles the final H.264 encode. The filter itself remains OpenCV/CPU,
        # while the encoder can use CPU x264, AMD AMF, NVIDIA NVENC or Intel QSV.
        ff = shutil.which("ffmpeg")
        proc = None
        encoder_used = "opencv-mp4v"
        if ff:
            encoders_text = subprocess.run([ff, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=15).stdout.lower()
            encoder_candidates = {
                "auto": ["h264_amf", "h264_nvenc", "h264_qsv", "libx264"],
                "cpu": ["libx264"],
                "amd": ["h264_amf"],
                "amf": ["h264_amf"],
                "cuda": ["h264_nvenc"],
                "nvenc": ["h264_nvenc"],
                "nvidia": ["h264_nvenc"],
                "qsv": ["h264_qsv"],
                "intel": ["h264_qsv"],
            }
            candidates = encoder_candidates.get(encoder_requested, encoder_candidates["auto"])
            # Probe hardware encoders with one tiny frame before starting the real
            # stream. Some FFmpeg builds advertise AMF/NVENC/QSV even when the
            # current driver/device cannot initialize them; without this probe the
            # export would fail after the UI had already started sending frames.
            encoder = None
            probe = np.zeros((sh, sw, 3), dtype=np.uint8)
            for candidate in candidates:
                if candidate not in encoders_text:
                    continue
                probe_path = DATA / (".v2f_probe_" + token + ".mp4")
                try:
                    pprobe = subprocess.run([
                        ff, "-y", "-loglevel", "error",
                        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{sw}x{sh}", "-r", "60", "-i", "-",
                        "-frames:v", "1", "-an", "-c:v", candidate,
                        *([ "-preset", "veryfast", "-crf", "18" ] if candidate == "libx264" else []),
                        "-pix_fmt", "yuv420p", str(probe_path)
                    ], input=probe.tobytes(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=20)
                    if pprobe.returncode == 0 and probe_path.exists() and probe_path.stat().st_size > 0:
                        encoder = candidate
                        break
                except Exception:
                    pass
                finally:
                    try: probe_path.unlink(missing_ok=True)
                    except Exception: pass
            if encoder:
                proc = subprocess.Popen([
                    ff, "-y", "-loglevel", "error",
                    "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{sw}x{sh}", "-r", "60", "-i", "-",
                    "-an", "-c:v", encoder,
                    *([ "-preset", "veryfast", "-crf", "18" ] if encoder == "libx264" else []),
                    "-pix_fmt", "yuv420p", str(video_path)
                ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                encoder_used = encoder
            elif encoder_requested not in ("auto", "cpu"):
                raise RuntimeError(f"Requested encoder '{encoder_requested}' is not available in this FFmpeg build")

        writer = None
        if proc is None:
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            writer = cv2.VideoWriter(str(video_path), fourcc, 60.0, (sw, sh))
            if not writer.isOpened():
                cap.release()
                raise RuntimeError("cannot create output video")

        next_out = 0.0
        frame_index = 0
        last = None
        try:
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                last = frame
                current_t = frame_index / sf
                frame_index += 1
                processed = _apply_video_fx(frame, fx)
                if processed.shape[1] != sw or processed.shape[0] != sh:
                    processed = processed[:sh, :sw]
                processed_bytes = np.ascontiguousarray(processed).tobytes() if proc is not None else None
                if frame_index % 10 == 0:
                    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                    pct = 10 + min(78, (frame_index / max(1, total_frames)) * 78) if total_frames > 0 else 10
                    with V2F_JOB_LOCK: V2F_JOBS[job_id] = {"phase":"render","progress":pct,"error":None}
                while next_out <= current_t + 1e-9:
                    if proc is not None:
                        proc.stdin.write(processed_bytes)
                    else:
                        writer.write(processed)
                    next_out += 1.0 / 60.0
            if last is not None and frame_index:
                # Ensure the final presentation interval is represented.
                duration = frame_index / sf
                processed = _apply_video_fx(last, fx)
                if processed.shape[1] != sw or processed.shape[0] != sh:
                    processed = processed[:sh, :sw]
                processed_bytes = np.ascontiguousarray(processed).tobytes() if proc is not None else None
                while next_out < duration - 1e-9:
                    if proc is not None:
                        proc.stdin.write(processed_bytes)
                    else:
                        writer.write(processed)
                    next_out += 1.0 / 60.0
        finally:
            cap.release()
            if writer is not None:
                writer.release()
            if proc is not None:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
                err = proc.stderr.read().decode("utf-8", "ignore")
                rc = proc.wait(timeout=120)
                if rc != 0:
                    raise RuntimeError("FFmpeg video encode failed: " + err[-500:])

        # Keep original audio when FFmpeg is available.
        with V2F_JOB_LOCK: V2F_JOBS[job_id] = {"phase":"mux","progress":90,"error":None}
        if ff:
            muxed = DATA / (".v2f_mux_" + token + ".mp4")
            p = subprocess.run([
                ff, "-y", "-loglevel", "error",
                "-i", str(video_path), "-i", str(src_path),
                "-map", "0:v:0", "-map", "1:a:0?",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-shortest", str(muxed)
            ], capture_output=True, text=True, timeout=300)
            if p.returncode == 0 and muxed.exists():
                os.replace(muxed, final_path)
            else:
                os.replace(video_path, final_path)
        else:
            os.replace(video_path, final_path)

        if not final_path.exists() or final_path.stat().st_size == 0:
            raise RuntimeError("export produced an empty file")
        with V2F_JOB_LOCK: V2F_JOBS[job_id] = {"phase":"done","progress":100,"error":None}
        # The final export is already complete and separate from both work files.
        # Delete temporary source/render files before returning the response rather
        # than relying only on WSGI response-close callbacks, which may not run when
        # a client consumes the response through fetch(). Keep final_path in DATA so
        # it can immediately be selected from Media on the next step.
        for p in (src_path, video_path):
            try: p.unlink(missing_ok=True)
            except Exception: pass
        resp = send_file(final_path, as_attachment=True,
                         download_name=final_path.name, mimetype="video/mp4")
        @resp.call_on_close
        def _cleanup():
            for p in (src_path, video_path):
                try: p.unlink(missing_ok=True)
                except Exception: pass
        return resp
    except Exception as e:
        with V2F_JOB_LOCK: V2F_JOBS[job_id] = {"phase":"error","progress":0,"error":str(e)[:400]}
        for p in (src_path, video_path, final_path):
            try: p.unlink(missing_ok=True)
            except Exception: pass
        return jsonify(ok=False, error="Video to Filter failed: " + type(e).__name__ + ": " + str(e)[:400]), 500

_LIVE_STUDIO_PATH_CACHE = None
_LIVE_STUDIO_PATH_SCANNED = False
_STUDIO_PROTOCOL_PORT_CACHE = None

def _live_studio_mode():
    raw = str(os.environ.get("PAOKRA_TIKTOK_STUDIO_MODE", "") or "").strip().lower()
    if raw in ("ui", "visible", "normal"): return "ui"
    if raw in ("headless", "hidden"): return "headless"
    try:
        f = DATA / "live_studio_mode.txt"
        raw = f.read_text(encoding="utf-8").strip().lower() if f.is_file() else ""
        if raw in ("ui", "visible", "normal"): return "ui"
        if raw in ("headless", "hidden"): return "headless"
    except Exception: pass
    return "headless"

def _find_live_studio_exe(force_scan=False):
    """Resolve TikTok LIVE Studio across common install layouts and registry."""
    global _LIVE_STUDIO_PATH_CACHE, _LIVE_STUDIO_PATH_SCANNED

    if force_scan:
        _LIVE_STUDIO_PATH_CACHE = None
        _LIVE_STUDIO_PATH_SCANNED = False
    if _LIVE_STUDIO_PATH_CACHE and _LIVE_STUDIO_PATH_CACHE.is_file() and not force_scan:
        return _LIVE_STUDIO_PATH_CACHE

    candidates = []
    for env_name in ("PAOKRA_TIKTOK_STUDIO_EXE", "TIKTOK_LIVE_STUDIO_EXE", "TIKTOK_LIVE_STUDIO_PATH"):
        env_path = os.environ.get(env_name, "").strip().strip('"')
        if env_path: candidates.append(Path(env_path).expanduser())
    try:
        import psutil as _path_psutil
        for proc in _path_psutil.process_iter(["name", "exe"]):
            try:
                pname = str(proc.info.get("name") or "").lower()
                pexe = str(proc.info.get("exe") or "").strip()
                if "tiktok live studio" in pname and pexe: candidates.insert(0, Path(pexe))
            except (_path_psutil.NoSuchProcess, _path_psutil.AccessDenied, OSError): continue
    except Exception: pass

    # 1) saved path from a previous successful discovery, 2) the path of a
    # LIVE Studio that is already running (most reliable), 3) every fixed drive.
    try:
        saved = DATA / "live_studio_path.txt"
        if saved.is_file():
            candidates.append(Path(saved.read_text(encoding="utf-8").strip()))
    except Exception:
        pass
    try:
        _r = subprocess.run(["powershell", "-NoProfile", "-Command",
            "(Get-Process -Name 'TikTok LIVE Studio' -ErrorAction SilentlyContinue | Select-Object -First 1).Path"],
            capture_output=True, text=True, timeout=6, creationflags=0x08000000)
        if _r.stdout.strip():
            candidates.append(Path(_r.stdout.strip()))
    except Exception:
        pass
    try:
        import string as _string
        for _d in _string.ascii_uppercase:
            if os.path.exists(_d + ":\\"):
                for _b in ("Program Files\\TikTok LIVE Studio", "Program Files (x86)\\TikTok LIVE Studio", "TikTok LIVE Studio"):
                    _base = Path(_d + ":\\") / _b
                    candidates.append(_base / "TikTok LIVE Studio.exe")
                    try:
                        candidates.extend(sorted(_base.glob("*/TikTok LIVE Studio.exe"), reverse=True))
                    except Exception:
                        pass
    except Exception:
        pass

    pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    pfx = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    local = Path(os.environ.get("LOCALAPPDATA", "")) if os.environ.get("LOCALAPPDATA") else None
    roaming = Path(os.environ.get("APPDATA", "")) if os.environ.get("APPDATA") else None

    # Common fixed/versioned layouts.
    for root in (pf, pfx, local, roaming):
        if not root:
            continue
        base = root / "TikTok LIVE Studio"
        candidates.extend([
            base / "1.36.6" / "TikTok LIVE Studio.exe",
            base / "TikTok LIVE Studio.exe",
        ])
        try:
            candidates.extend(base.glob("*/TikTok LIVE Studio.exe"))
        except Exception:
            pass

    # Registry lookup catches custom installation directories without doing a
    # full-disk search. Only run this scan once per process unless forced.
    if not _LIVE_STUDIO_PATH_SCANNED or force_scan:
        _LIVE_STUDIO_PATH_SCANNED = True
        try:
            import winreg
            roots = [
                (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            ]
            for hive, uninstall_key in roots:
                try:
                    with winreg.OpenKey(hive, uninstall_key) as base:
                        for i in range(winreg.QueryInfoKey(base)[0]):
                            try:
                                subname = winreg.EnumKey(base, i)
                                with winreg.OpenKey(base, subname) as sub:
                                    display = str(winreg.QueryValueEx(sub, "DisplayName")[0] or "")
                                    if "tiktok live studio" not in display.lower():
                                        continue
                                    for value_name in ("InstallLocation", "DisplayIcon"):
                                        try:
                                            raw = str(winreg.QueryValueEx(sub, value_name)[0] or "").strip().strip('"')
                                        except OSError:
                                            continue
                                        if not raw:
                                            continue
                                        icon_path = raw.split(",")[0].strip().strip('"')
                                        p = Path(icon_path)
                                        if p.suffix.lower() != ".exe":
                                            p = p / "TikTok LIVE Studio.exe"
                                        candidates.append(p)
                            except OSError:
                                continue
                except OSError:
                    continue
        except Exception:
            pass

    seen = set()
    for p in candidates:
        try:
            key = str(p).lower()
            if key in seen:
                continue
            seen.add(key)
            if p.is_file() and p.name.lower() == "tiktok live studio.exe":
                _LIVE_STUDIO_PATH_CACHE = p.resolve()
                os.environ["PAOKRA_TIKTOK_STUDIO_EXE"] = str(_LIVE_STUDIO_PATH_CACHE)
                try: (DATA / "live_studio_path.txt").write_text(str(_LIVE_STUDIO_PATH_CACHE), encoding="utf-8")
                except Exception: pass
                return _LIVE_STUDIO_PATH_CACHE
        except Exception:
            pass
    return None


def _discover_live_studio_versions(force_scan=False):
    """Return every discovered TikTok LIVE Studio executable in common install layouts."""
    found = []
    seen = set()

    def add(value):
        if not value:
            return
        try:
            p = Path(str(value).strip().strip('"')).expanduser()
            if p.is_file() and p.name.lower() == "tiktok live studio.exe":
                resolved = p.resolve()
                key = os.path.normcase(str(resolved))
                if key not in seen:
                    seen.add(key)
                    found.append(resolved)
        except Exception:
            pass

    # Keep the user's existing selection first; discovery below adds other versions.
    try:
        add(os.environ.get("PAOKRA_TIKTOK_STUDIO_EXE", ""))
        saved = DATA / "live_studio_path.txt"
        if saved.is_file():
            add(saved.read_text(encoding="utf-8"))
    except Exception:
        pass
    try:
        add(_find_live_studio_exe(force_scan=force_scan))
    except Exception:
        pass

    roots = []
    for key, default in (("ProgramFiles", r"C:\Program Files"),
                         ("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                         ("LOCALAPPDATA", ""),
                         ("APPDATA", ""),
                         ("PROGRAMDATA", "")):
        raw = os.environ.get(key, default).strip()
        if raw:
            roots.append(Path(raw))
    # Common user-installed app layouts, including versioned folders.
    bases = []
    for root in roots:
        bases.extend([
            root / "TikTok LIVE Studio",
            root / "Programs" / "TikTok LIVE Studio",
            root / "Apps" / "TikTok LIVE Studio",
        ])
    for base_path in bases:
        add(base_path / "TikTok LIVE Studio.exe")
        try:
            for candidate in base_path.glob("*/TikTok LIVE Studio.exe"):
                add(candidate)
        except Exception:
            pass

    entries = []
    for p in found:
        folder = p.parent.name
        label = folder if re.fullmatch(r"[vV]?\d+(?:[._-]\d+){1,4}", folder) else "Installed build"
        entries.append({"path": str(p), "label": label, "folder": folder})
    # Stable ordering with the saved/current choice first, then paths alphabetically.
    selected = ""
    try:
        saved = DATA / "live_studio_path.txt"
        selected = str(Path(saved.read_text(encoding="utf-8").strip()).resolve()) if saved.is_file() else ""
    except Exception:
        selected = ""
    current = os.environ.get("PAOKRA_TIKTOK_STUDIO_EXE", "").strip()
    if current:
        try:
            selected = str(Path(current).resolve())
        except Exception:
            pass
    entries.sort(key=lambda item: (os.path.normcase(item["path"]) != os.path.normcase(selected), item["path"].lower()))
    for entry in entries:
        entry["selected"] = os.path.normcase(entry["path"]) == os.path.normcase(selected)
    return entries


def _studio_processes_for_path(exe_path):
    """Find process objects whose executable matches one selected Studio version."""
    try:
        import psutil
    except Exception as exc:
        raise RuntimeError("psutil is required to control the selected LIVE Studio process") from exc
    target = os.path.normcase(os.path.realpath(str(exe_path)))
    matches = []
    for proc in psutil.process_iter(["pid", "exe", "name"]):
        try:
            exe = proc.info.get("exe")
            if exe and os.path.normcase(os.path.realpath(exe)) == target:
                matches.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    return matches


def _show_studio_windows(process_ids):
    """Restore only windows owned by the selected LIVE Studio process."""
    if os.name != "nt":
        return 0
    user32 = ctypes.windll.user32
    wanted = set(int(x) for x in process_ids)
    shown = [0]
    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _enum(hwnd, _):
        try:
            if not user32.IsWindow(hwnd):
                return True
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in wanted:
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.SetForegroundWindow(hwnd)
                shown[0] += 1
        except Exception:
            pass
        return True
    user32.EnumWindows(_enum, 0)
    return shown[0]


_studio_lock = threading.RLock()
_studio_ws = None
_studio_hide_thread = None

def _hide_studio_windows():
    """Hide Studio windows only while headless mode is selected."""
    if _live_studio_mode() != "headless": return False
    try:
        user32 = ctypes.windll.user32
        pids = set()
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process -Name 'TikTok LIVE Studio' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id"],
            capture_output=True, text=True, timeout=3
        )
        for line in r.stdout.splitlines():
            try: pids.add(int(line.strip()))
            except Exception: pass
        if not pids:
            return False
        hidden = [False]
        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def enum_cb(hwnd, _):
            if not user32.IsWindow(hwnd):
                return True
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids and user32.IsWindowVisible(hwnd):
                user32.ShowWindow(hwnd, 0)  # SW_HIDE; process/protocol remains alive
                hidden[0] = True
            return True
        user32.EnumWindows(enum_cb, 0)
        return hidden[0]
    except Exception:
        return False

def _studio_hide_worker():
    if _live_studio_mode() != "headless": return
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and _studio_running():
        _hide_studio_windows()
        time.sleep(0.25)

def _ensure_studio_hidden():
    global _studio_hide_thread
    if _live_studio_mode() != "headless": return
    _hide_studio_windows()
    if not _studio_hide_thread or not _studio_hide_thread.is_alive():
        _studio_hide_thread = threading.Thread(target=_studio_hide_worker, name="paokra-studio-hide", daemon=True)
        _studio_hide_thread.start()

def _studio_running():
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process -Name 'TikTok LIVE Studio' -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Id"],
            capture_output=True, text=True, timeout=3
        )
        return bool(r.stdout.strip())
    except Exception:
        return False

def _studio_port_ready(timeout=0.8):
    """Win32/TCP readiness gate for LIVE Studio's local Stream Deck endpoint."""
    if os.name != "nt":
        return False
    try:
        import socket
        with socket.create_connection(("127.0.0.1", 28189), timeout=timeout):
            return True
    except Exception:
        return False


def _studio_protocol_ready(timeout=1.5):
    """Perform the Engine.IO + Socket.IO handshake; never emits a LIVE action."""
    # LIVE Studio's bridge is local-only. Do not let machine-wide HTTP(S)
    # proxy variables intercept localhost WebSocket connections.
    try:
        no_proxy = os.environ.get("NO_PROXY", "")
        parts = [x.strip() for x in no_proxy.split(",") if x.strip()]
        for host in ("127.0.0.1", "localhost", "::1"):
            if host not in parts:
                parts.append(host)
        os.environ["NO_PROXY"] = ",".join(parts)
        os.environ["no_proxy"] = os.environ["NO_PROXY"]
    except Exception:
        pass
    return _studio_protocol_port(timeout)


def _wait_for_studio_protocol(timeout=30.0):
    """Wait for process, local listener, and protocol handshake in that order."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _studio_running() and _studio_protocol_ready(1.0):
            return True
        time.sleep(0.25)
    return False


def _launch_studio():
    """Launch LIVE Studio as a real Win32 app, then hide its UI without killing the process."""
    already_running = _studio_running()
    if not already_running:
        exe_path = _find_live_studio_exe()
        if exe_path is None:
            raise RuntimeError("ไม่พบ TikTok LIVE Studio — ติดตั้ง LIVE Studio ก่อน")

        # Let LIVE Studio initialize normally. Some builds do not create the
        # local Stream Deck bridge correctly when their window is hidden at launch.
        startupinfo = None
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 1
        subprocess.Popen(
            [str(exe_path)],
            cwd=str(exe_path.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            startupinfo=startupinfo,
        )

        # Once the normal Win32 window exists, hide only the UI. The LIVE Studio
        # process and its child services remain alive in the background.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if _studio_running():
                _position_studio_window()
                break
            time.sleep(0.25)
    else:
        # Reuse the existing instance; never spawn or kill duplicates here.
        _position_studio_window()

    # Protocol readiness is independent from process launch. Keep polling after
    # launch/hide, but do not turn an unavailable optional bridge into a launch
    # failure when the Studio process itself is alive.
    if _wait_for_studio_protocol(30.0):
        _ensure_studio_hidden()
    else:
        # Studio is still a valid background process even if its local bridge
        # is unavailable. /api/live/status reports protocol_connected separately.
        pass
    return bool(_studio_running())

def _position_studio_window():
    """Hide only in headless mode; keep the normal UI visible when selected."""
    if _live_studio_mode() != "headless": return False
    try:
        user32 = ctypes.windll.user32
        studio_hwnd = 0
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            found = []
            @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
            def enum_cb(hwnd, _):
                pid = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if not pid.value or not user32.IsWindow(hwnd):
                    return True
                title = ctypes.create_unicode_buffer(512)
                user32.GetWindowTextW(hwnd, title, 511)
                if "TikTok LIVE Studio" in title.value:
                    found.append(int(hwnd))
                    return False
                return True
            user32.EnumWindows(enum_cb, 0)
            if found:
                studio_hwnd = found[0]
                break
            time.sleep(0.2)
        if not studio_hwnd:
            return False
        _ensure_studio_hidden()
        return True
    except Exception:
        return False

def _studio_emit_action(action_id, settings=None, timeout=3.0):
    """Emit one verified LIVE Studio action and wait for its result."""
    import websocket
    settings = settings or {}
    port = _studio_protocol_port(min(timeout, 1.0))
    if port is None:
        raise TimeoutError("LIVE Studio Stream Deck protocol port not found")
    url = _studio_protocol_url(port)
    ws = websocket.create_connection(url, subprotocols=["streamdeck_ttls_v1"], timeout=timeout)
    try:
        first = str(ws.recv())
        if not first.startswith("0"):
            raise RuntimeError("LIVE Studio protocol handshake failed")
        ws.send("40")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = str(ws.recv())
            if msg.startswith("2"):
                ws.send("3")
                continue
            if msg.startswith("40"):
                ws.send('42["stream_deck/join_room"]')
                envelope = {
                    "action": action_id,
                    "context": "paokra_" + uuid.uuid4().hex,
                    "payload": {"settings": settings},
                }
                ws.send('42["stream_deck/action_emit",' + _json.dumps(_json.dumps(envelope)) + ']')
                response_event = "stream_deck/" + envelope["context"]
                while time.monotonic() < deadline:
                    msg2 = str(ws.recv())
                    if msg2.startswith("2"):
                        ws.send("3")
                        continue
                    if msg2.startswith("42"):
                        payload = _json.loads(msg2[2:])
                        if isinstance(payload, list) and payload and payload[0] == response_event:
                            data = payload[1] if len(payload) > 1 else {}
                            if isinstance(data, str):
                                data = _json.loads(data)
                            if isinstance(data, dict) and data.get("code") == 0:
                                return data
                            if isinstance(data, dict):
                                code = data.get("code")
                                reason = data.get("message") or data.get("error") or data.get("reason")
                                detail = f"code={code}" if code is not None else "invalid-result"
                                if reason:
                                    detail += f", {reason}"
                                raise RuntimeError("LIVE Studio rejected action: " + detail)
                            raise RuntimeError("LIVE Studio rejected action: invalid response")
                raise TimeoutError("action result timeout")
        raise TimeoutError("protocol connect timeout")
    finally:
        ws.close()

def _studio_toggle_live():
    ready = _studio_readiness()
    if not ready.get("studio_running") or not ready.get("protocol_connected"):
        raise RuntimeError("TikTok LIVE Studio ยังไม่พร้อม")
    current = ready.get("stream_status")
    if current == 2:
        _studio_emit_action("com.tiktok.livestudio.livestartend", {})
        return "stopped"
    _studio_emit_action("com.tiktok.livestudio.livestartend", {})
    return "started"

def _obs_camera_available():
    # pyvirtualcam 0.15 has no camera_names(); detect the OBS DirectShow filter instead.
    try:
        if vcam.active: return True
    except Exception: pass
    try:
        import winreg
        clsid = "CLSID\\{A3FCE0F5-3493-419F-958A-ABA1250EC20B}"
        subs = ((winreg.HKEY_CLASSES_ROOT, clsid),
                (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\Classes\\" + clsid),
                (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\Classes\\" + clsid))
        for root, sub in subs:
            for flag in (0, getattr(winreg, "KEY_WOW64_64KEY", 0), getattr(winreg, "KEY_WOW64_32KEY", 0)):
                try:
                    winreg.CloseKey(winreg.OpenKey(root, sub, 0, winreg.KEY_READ | flag)); return True
                except OSError: pass
    except Exception: pass
    try:
        import pyvirtualcam
        names = getattr(pyvirtualcam, "camera_names", None)
        if names: return any("OBS" in str(n) for n in names())
    except Exception: pass
    return False

def _studio_sync_settings(timeout=2.5):
    """Read-only LIVE Studio protocol check. Never starts LIVE."""
    import websocket
    port = _studio_protocol_port(min(timeout, 1.0))
    if port is None:
        raise TimeoutError("LIVE Studio Stream Deck protocol port not found")
    url = _studio_protocol_url(port)
    ws = websocket.create_connection(url, subprotocols=["streamdeck_ttls_v1"], timeout=timeout)
    try:
        first = ws.recv()
        if not str(first).startswith("0"):
            raise RuntimeError("LIVE Studio protocol handshake failed")
        ws.send("40")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = str(ws.recv())
            if msg.startswith("2"):
                ws.send("3")
                continue
            if msg.startswith("40"):
                ws.send('42["stream_deck/join_room"]')
                ws.send('42["stream_deck/sync_settings"]')
                continue
            if msg.startswith("42"):
                payload = _json.loads(msg[2:])
                if isinstance(payload, list) and len(payload) >= 2 and payload[0] == "stream_deck/sync_settings":
                    data = payload[1]
                    if isinstance(data, str):
                        data = _json.loads(data)
                    return data
        raise RuntimeError("sync_settings timeout")
    finally:
        ws.close()

_sync_fail_count = 0

_STUDIO_PROTOCOL_PORTS = (28189, 39728, 34246, 42205, 38534, 40825, 40622)

def _studio_protocol_url(port):
    return f"ws://127.0.0.1:{int(port)}/socket.io/?EIO=4&transport=websocket"

def _studio_protocol_candidates():
    candidates=[]
    if _STUDIO_PROTOCOL_PORT_CACHE: candidates.append(_STUDIO_PROTOCOL_PORT_CACHE)
    for name in ("PAOKRA_TIKTOK_PROTOCOL_PORT", "TIKTOK_LIVE_STUDIO_PROTOCOL_PORT"):
        try:
            port=int(os.environ.get(name,""))
            if 1024 <= port <= 65535: candidates.append(port)
        except (TypeError,ValueError,OverflowError): pass
    try:
        f=DATA/"live_studio_protocol_port.txt";port=int(f.read_text(encoding="utf-8").strip()) if f.is_file() else 0
        if 1024 <= port <= 65535: candidates.append(port)
    except Exception: pass
    candidates.extend(_STUDIO_PROTOCOL_PORTS)
    try:
        import psutil as _port_psutil
        roots=[]
        for proc in _port_psutil.process_iter(["pid","name","exe"]):
            try:
                name=str(proc.info.get("name") or "").lower();exe=str(proc.info.get("exe") or "").lower()
                if "tiktok live studio" in name or "tiktok live studio.exe" in exe: roots.append(proc)
            except (_port_psutil.NoSuchProcess,_port_psutil.AccessDenied,OSError): continue
        pids=set()
        for proc in roots:
            pids.add(proc.pid)
            try: pids.update(child.pid for child in proc.children(recursive=True))
            except (_port_psutil.NoSuchProcess,_port_psutil.AccessDenied,OSError): pass
        if pids:
            for conn in _port_psutil.net_connections(kind="tcp"):
                try:
                    if conn.pid in pids and conn.status == _port_psutil.CONN_LISTEN and conn.laddr:
                        port=int(conn.laddr.port)
                        if 1024 <= port <= 65535: candidates.insert(0,port)
                except Exception: continue
    except Exception: pass
    ordered=[];seen=set()
    for item in candidates:
        try: port=int(item)
        except (TypeError,ValueError,OverflowError): continue
        if 1024 <= port <= 65535 and port not in seen: seen.add(port);ordered.append(port)
    return ordered


def _studio_protocol_port(timeout=2.0):
    global _STUDIO_PROTOCOL_PORT_CACHE
    import websocket
    try:
        parts=[x.strip() for x in os.environ.get("NO_PROXY","").split(",") if x.strip()]
        for host in ("127.0.0.1","localhost","::1"):
            if host not in parts: parts.append(host)
        os.environ["NO_PROXY"]=",".join(parts);os.environ["no_proxy"]=os.environ["NO_PROXY"]
    except Exception: pass
    for index,port in enumerate(_studio_protocol_candidates()):
        ws=None; per_try=max(.2,min(float(timeout or .5),.75 if index<3 else .4))
        try:
            ws=websocket.create_connection(_studio_protocol_url(port),subprotocols=["streamdeck_ttls_v1"],timeout=per_try);ws.settimeout(per_try)
            if str(ws.recv()).startswith("0"):
                ws.send("40")
                if str(ws.recv()).startswith("40"):
                    _STUDIO_PROTOCOL_PORT_CACHE=int(port)
                    try:(DATA/"live_studio_protocol_port.txt").write_text(str(port),encoding="utf-8")
                    except Exception:pass
                    return int(port)
        except Exception:pass
        finally:
            try:
                if ws is not None:ws.close()
            except Exception:pass
    _STUDIO_PROTOCOL_PORT_CACHE=None
    return None

def _studio_protocol_reachable(timeout=2.0):
    return _studio_protocol_port(timeout) is not None

def _studio_readiness():
    """
    Readiness for LIVE Studio 1.36.x.

    This build accepts the Stream Deck Socket.IO handshake on 28189 but does
    not answer sync_settings, so treating sync_settings as the connection gate
    makes PAOKRA report a false "Connection timed out". Keep the handshake as
    the transport health check and expose state as unknown when sync is
    unavailable; the LIVE action itself still requires a verified action ack.
    """
    studio = _studio_running()
    obs = _obs_camera_available()
    protocol = bool(studio and _studio_protocol_reachable(1.5))
    settings = {}
    return {
        "studio_running": studio,
        "obs_virtual_camera": obs,
        "protocol_connected": protocol,
        "stream_status": settings.get("streamStatus") if isinstance(settings, dict) else None,
        "active_scene": settings.get("activeScene") if isinstance(settings, dict) else None,
    }

@app.post("/api/live/setup")
def live_setup():
    try:
        # First setup explicitly discovers the installed LIVE Studio path. The
        # resolved path is cached and also exported for the bundled CLI.
        studio_path = _find_live_studio_exe(force_scan=True)
        if studio_path:
            os.environ["PAOKRA_TIKTOK_STUDIO_EXE"] = str(studio_path)

        mode = _live_studio_mode()
        result = tiktokls_bridge.setup(mode=mode)

        # Setup LIVE means "prepare the complete control path": keep the studio
        # keep Studio in the selected UI/headless mode, but do not start a broadcast yet.
        studio_start = tiktokls_bridge.studio_start(mode=mode)
        result["studio_start"] = studio_start
        result["studio_path"] = str(studio_path) if studio_path else ""
        result["studio_path_found"] = bool(studio_path)
        result["ok"] = bool(result.get("ok") and studio_start.get("ok", False))

        # PAOKRA owns the media pipeline; the copied CLI owns LIVE Studio lifecycle.
        # Use the same transition lock as Clip Flow and open the device OUTSIDE the
        # non-reentrant state lock; camera creation may block on a driver/device.
        with clip_flow_transition_lock:
            if not vcam.active:
                with lock:
                    cam_width = int(state["width"])
                    cam_height = int(state["height"])
                    fps_mode = str(state.get("fps_mode", "fixed") or "fixed")
                cam_fps = _effective_output_fps()
                cam_ok = bool(vcam.start(
                    cam_width, cam_height, cam_fps,
                    allow_fallback=(fps_mode == "source"),
                ))
                with lock:
                    state["virtualcam"] = bool(vcam.active)
                    state["virtualcam_error"] = vcam.error if not cam_ok else None
                if cam_ok and vcam.active:
                    _start_libobs_bridge(cam_width, cam_height, int(vcam.output_fps or cam_fps))
        result["virtualcam"] = bool(vcam.active)
        result["ready"] = bool(result.get("ok") and result.get("studio_start", {}).get("ok") and vcam.active)
        return jsonify(**result)
    except Exception as e:
        return jsonify(ok=False, error="LIVE setup failed: " + type(e).__name__ + ": " + str(e)[:250]), 500

import threading as _threading, time as _time, traceback as _tb, logging as _logging

_LIVE_JOB = {"running": False, "id": 0, "action": None, "result": None, "started": 0, "finished": 0}
_LIVE_JOB_LOCK = _threading.Lock()
_LIVE_STATUS_CACHE = {"data": None, "t": 0.0}
_TR = tiktokls_bridge.trace

class _WerkzeugTrace(_logging.Handler):
    def emit(self, record):
        try:
            m = record.getMessage()
            if "/api/live/" in m and any(x in m for x in ("/api/live/log", "/api/live/job", "/api/live/status")):
                return
            if '"GET ' in m and "/api/live/" not in m:
                return
            _TR("server", "DEBUG", "werkzeug: " + m)
        except Exception:
            pass
_logging.getLogger("werkzeug").addHandler(_WerkzeugTrace())

@app.before_request
def _live_trace_req():
    p = request.path
    if p.startswith("/api/live/") and p not in ("/api/live/log", "/api/live/job", "/api/live/status", "/api/live/log/clear"):
        _TR("server", "INFO", f"HTTP {request.method} {p}")

def _is_live(d):
    return (d or {}).get("live_state") == "live" or (d or {}).get("state") == "live"

def _live_job_worker(job_id):
    result = {"ok": False}; action = "failed"
    try:
        _TR("server", "INFO", f"job#{job_id}: reading live status")
        current = tiktokls_bridge.live_status()
        _TR("server", "INFO", f"job#{job_id}: live_state={current.get('live_state')!r} running={current.get('running')!r}")
        if _is_live(current):
            stop_cfg = _clip_config_snapshot()
            if stop_cfg.get("stop") and Path(str(stop_cfg["stop"])).is_file():
                _TR("server", "INFO", f"job#{job_id}: currently LIVE -> playing Stop clip before LIVE STOP")
                clip_result = _clip_flow_play_stop_and_wait(reason="live_stop")
                _TR("server", "INFO" if clip_result.get("completed") else "WARN",
                    f"job#{job_id}: Stop clip completed={clip_result.get('completed')!r} error={clip_result.get('error')!r}")
            else:
                _TR("server", "WARN", f"job#{job_id}: no Stop clip configured -> stopping LIVE without closing clip")
            _TR("server", "INFO", f"job#{job_id}: sending LIVE STOP after closing clip")
            result = tiktokls_bridge.live_stop(mode=_live_studio_mode())
            action = "stopped" if result.get("ok") else "stop_failed"
            if action == "stopped":
                try:
                    hidden = tiktokls_bridge.studio_hide()
                    _TR("server", "INFO", f"job#{job_id}: kept TikTok LIVE Studio in background ok={hidden.get('ok')!r}")
                except Exception as hide_exc:
                    _TR("server", "WARN", f"job#{job_id}: LIVE stopped but hide failed: {hide_exc}")
        else:
            st0 = current.get("live_state")
            if current.get("running") and st0 == "unknown_no_debug_port":
                _TR("server", "WARN", f"job#{job_id}: Studio is running WITHOUT the CDP debug port -> restart it (studio stop, then start)")
                tiktokls_bridge.studio_stop(); _time.sleep(3)
                current = {"running": False, "live_state": "studio_not_running"}; st0 = "studio_not_running"
            if st0 == "studio_not_running" or current.get("running") is False:
                _TR("server", "INFO", f"job#{job_id}: LIVE Studio not running -> studio start first")
                pre = tiktokls_bridge.studio_start(mode=_live_studio_mode())
                _TR("server", "INFO", f"job#{job_id}: studio start ok={pre.get('ok')!r}")
            # Wait until Studio is actually ready before sending live start (do not fire it immediately).
            _TR("server", "INFO", f"job#{job_id}: waiting for Studio to become ready (max ~75s)")
            t_wait = _time.time(); ready_seen = False
            while _time.time() - t_wait < 75:
                s1 = tiktokls_bridge.live_status()
                ls1 = s1.get("live_state")
                _TR("server", "INFO", f"job#{job_id}: wait-ready running={s1.get('running')!r} live_state={ls1!r}")
                if s1.get("running") and ls1 not in ("studio_not_running", "unknown_no_debug_port"):
                    ready_seen = True; break
                _time.sleep(3)
            if not ready_seen:
                _TR("server", "WARN", f"job#{job_id}: Studio not confirmed ready after wait; CLI will keep waiting itself (login / main page needed?)")
            _TR("server", "INFO", f"job#{job_id}: sending START (live start --confirm with selected UI/headless mode)")
            result = tiktokls_bridge.live_start(mode=_live_studio_mode())
            action = "started" if result.get("ok") else "start_failed"
        post = tiktokls_bridge.live_status()
        live_now = _is_live(post)
        _TR("server", "INFO", f"job#{job_id}: post-check live_state={post.get('live_state')!r} -> live={live_now}")
        if action == "start_failed" and live_now:
            action = "started"; result["note"] = "CLI reported failure/timeout but Studio is actually LIVE"
            _TR("server", "WARN", f"job#{job_id}: late success detected (state is LIVE despite CLI error)")
        if action == "stop_failed" and not live_now:
            action = "stopped"
        result["live"] = live_now
        result["virtualcam"] = bool(vcam.active)
    except Exception as ex:
        _TR("server", "ERROR", f"job#{job_id}: {type(ex).__name__}: {ex}")
        for ln in _tb.format_exc().splitlines()[-6:]:
            _TR("server", "ERROR", ln)
        result = {"ok": False, "error": f"{type(ex).__name__}: {str(ex)[:300]}"}; action = "failed"
    result["action"] = action
    _TR("server", "INFO" if action in ("started", "stopped") else "ERROR", f"job#{job_id}: DONE action={action}")
    with _LIVE_JOB_LOCK:
        _LIVE_JOB.update(running=False, result=result, action=action, finished=_time.time())

@app.post("/api/live/toggle")
def live_toggle():
    # Non-blocking: run the real LIVE lifecycle in a background job; UI polls /api/live/job and /api/live/log.
    with _LIVE_JOB_LOCK:
        if _LIVE_JOB["running"]:
            return jsonify(ok=True, action="busy", job=_LIVE_JOB["id"])
        _LIVE_JOB.update(running=True, id=_LIVE_JOB["id"] + 1, result=None, action=None, started=_time.time(), finished=0)
        jid = _LIVE_JOB["id"]
    _threading.Thread(target=_live_job_worker, args=(jid,), daemon=True).start()
    return jsonify(ok=True, action="starting", job=jid)

@app.get("/api/live/job")
def live_job():
    with _LIVE_JOB_LOCK:
        return jsonify(**dict(_LIVE_JOB))

@app.get("/api/live/log")
def live_log():
    try:
        since = int(request.args.get("since", 0) or 0)
    except Exception:
        since = 0
    items, last = tiktokls_bridge.trace_since(since)
    return jsonify(ok=True, items=items, last=last, job_running=_LIVE_JOB["running"])

@app.post("/api/live/log/clear")
def live_log_clear():
    tiktokls_bridge.trace_clear()
    return jsonify(ok=True)

@app.get("/api/live/status")
def live_status():
    try:
        _c = _LIVE_STATUS_CACHE
        if _LIVE_JOB["running"] and _c["data"] is not None:
            result = dict(_c["data"])          # don't spawn more CLI processes while a LIVE job is running
        elif _c["data"] is not None and _time.time() - _c["t"] < 4:
            result = dict(_c["data"])
        else:
            result = tiktokls_bridge.live_status()
            _c["data"] = dict(result); _c["t"] = _time.time()
        result["virtualcam"] = bool(vcam.active)
        result["virtualaudio"] = bool(vaudio.active)
        result["virtualaudio_device"] = vaudio.device_name
        _studio_path = _find_live_studio_exe()
        result["studio_path"] = str(_studio_path) if _studio_path else ""
        result["studio_path_found"] = bool(_studio_path)
        result["live"] = result.get("live_state") == "live" or result.get("state") == "live"
        # Fields the UI panel (Studio / OBS Virtual Camera / Studio protocol) reads.
        studio = bool(result.get("running"))
        result["studio_running"] = studio
        result["obs_virtual_camera"] = bool(_obs_camera_available())
        protocol_port = _studio_protocol_port(0.5) if studio else None
        result["protocol_port"] = protocol_port
        result["protocol_connected"] = bool(studio and protocol_port)
        result["studio_mode"] = _live_studio_mode()
        return jsonify(**result)
    except Exception as e:
        return jsonify(ok=False, error="LIVE status failed: " + type(e).__name__), 500


@app.route("/api/live/studio-mode", methods=["GET", "POST"])
def live_studio_mode_config():
    if request.method == "GET": return jsonify(ok=True, mode=_live_studio_mode(), applies="next Studio setup/start")
    body=request.get_json(silent=True) or {}; mode=str(body.get("mode","")).strip().lower()
    if mode in ("visible","normal"): mode="ui"
    if mode=="hidden": mode="headless"
    if mode not in ("ui","headless"): return jsonify(ok=False,error="mode must be ui or headless"),400
    try:
        DATA.mkdir(parents=True,exist_ok=True);(DATA/"live_studio_mode.txt").write_text(mode,encoding="utf-8")
        os.environ["PAOKRA_TIKTOK_STUDIO_MODE"]=mode
    except Exception as exc: return jsonify(ok=False,error=f"could not save Studio mode: {str(exc)[:180]}"),500
    return jsonify(ok=True,mode=mode,applies="next Studio setup/start")


@app.get("/api/live/studio-versions")
def live_studio_versions():
    try:
        refresh = request.args.get("refresh", "0") == "1"
        versions = _discover_live_studio_versions(force_scan=refresh)
        selected = os.environ.get("PAOKRA_TIKTOK_STUDIO_EXE", "").strip()
        if not selected:
            try:
                saved = DATA / "live_studio_path.txt"
                if saved.is_file():
                    selected = saved.read_text(encoding="utf-8").strip()
            except Exception:
                selected = ""
        return jsonify(ok=True, versions=versions, selected_path=selected,
                       count=len(versions))
    except Exception as exc:
        return jsonify(ok=False, error=f"{type(exc).__name__}: {str(exc)[:240]}"), 500


@app.post("/api/live/studio-version/select")
def live_studio_version_select():
    global _LIVE_STUDIO_PATH_CACHE, _LIVE_STUDIO_PATH_SCANNED
    body = request.get_json(silent=True) or {}
    raw = str(body.get("path", "")).strip().strip('"')
    if not raw:
        return jsonify(ok=False, error="กรุณาเลือก LIVE Studio version หรือใส่ EXE path"), 400
    try:
        exe = Path(raw).expanduser().resolve()
    except Exception:
        return jsonify(ok=False, error="EXE path ไม่ถูกต้อง"), 400
    if not exe.is_file() or exe.name.lower() != "tiktok live studio.exe":
        return jsonify(ok=False, error="ต้องเลือกไฟล์ TikTok LIVE Studio.exe ที่มีอยู่จริง"), 400
    _LIVE_STUDIO_PATH_CACHE = exe
    _LIVE_STUDIO_PATH_SCANNED = True
    os.environ["PAOKRA_TIKTOK_STUDIO_EXE"] = str(exe)
    try:
        (DATA / "live_studio_path.txt").write_text(str(exe), encoding="utf-8")
    except Exception as exc:
        return jsonify(ok=False, error=f"บันทึก path ไม่สำเร็จ: {exc}"), 500
    return jsonify(ok=True, selected_path=str(exe))


@app.post("/api/live/studio-control")
def live_studio_control():
    body = request.get_json(silent=True) or {}
    action = str(body.get("action", "")).strip().lower()
    if action not in {"open", "normal", "stop"}:
        return jsonify(ok=False, error="action ต้องเป็น open, normal หรือ stop"), 400
    exe = _find_live_studio_exe()
    if not exe or not exe.is_file():
        return jsonify(ok=False, error="ไม่พบ TikTok LIVE Studio.exe — เลือกเวอร์ชันจาก Source Settings ก่อน"), 404
    if os.name != "nt":
        return jsonify(ok=False, error="คำสั่งนี้รองรับ Windows เท่านั้น"), 400
    try:
        running = _studio_processes_for_path(exe)
        if action == "stop":
            if not running:
                return jsonify(ok=True, action=action, stopped=0, message="เวอร์ชันที่เลือกไม่ได้เปิดอยู่")
            import psutil as _psutil
            to_stop = {}
            for proc in running:
                to_stop[proc.pid] = proc
                try:
                    for child in proc.children(recursive=True):
                        to_stop[child.pid] = child
                except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                    pass
            stopped = 0
            for proc in list(to_stop.values())[::-1]:
                try:
                    proc.terminate()
                    stopped += 1
                except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                    continue
            _, alive = _psutil.wait_procs(list(to_stop.values()), timeout=3)
            for proc in alive:
                try:
                    proc.kill()
                except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                    pass
            return jsonify(ok=True, action=action, stopped=stopped,
                           message="ปิดโปรเซสของเวอร์ชันที่เลือกแล้ว")
        if not running:
            subprocess.Popen([str(exe)], cwd=str(exe.parent),
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True)
            if action == "open":
                return jsonify(ok=True, action=action, launched=True,
                               path=str(exe), message="เปิด LIVE Studio แล้ว")
            # For Normal mode, the newly launched Windows app is already visible.
            return jsonify(ok=True, action=action, launched=True,
                           path=str(exe), message="เปิด LIVE Studio ในโหมดหน้าต่างปกติแล้ว")
        if action == "open":
            return jsonify(ok=True, action=action, launched=False,
                           path=str(exe), message="เวอร์ชันนี้เปิดอยู่แล้ว")
        shown = _show_studio_windows([p.pid for p in running])
        return jsonify(ok=True, action=action, launched=False, shown_windows=shown,
                       path=str(exe), message="คืนหน้าต่าง LIVE Studio เป็นโหมดปกติแล้ว")
    except Exception as exc:
        _TR("server", "ERROR", f"Studio control {action}: {type(exc).__name__}: {exc}")
        return jsonify(ok=False, error=f"{type(exc).__name__}: {str(exc)[:300]}"), 500


@app.post("/api/virtualcam")
def virtualcam_control():
    global _AI_SEAM_NEGOTIATED_FPS_OVERRIDE
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(ok=False, error="virtual camera payload must be an object"), 400
    action = body.get("action")
    if action not in ("start", "stop", "reconfigure-stop"):
        return jsonify(error="unknown action"), 400
    if action == "start" and _ai_seam_batch_is_active():
        return jsonify(ok=False, active=bool(vcam.active), error="Auto Make Frames is running; wait before starting the Virtual Camera."), 409
    if action == "start" and _ai_seam_generation_active(DATA):
        return jsonify(ok=False, active=bool(vcam.active), error="AI Seam generation is running; wait for it to finish before starting the Virtual Camera."), 409

    # Serialize camera open/close with Clip Flow source transitions, but never
    # hold the global render-state lock while opening/closing device handles.
    # Device teardown can block briefly; holding the lock here used to stall state
    # polling and made Engine/Virtual Camera buttons appear frozen.
    with clip_flow_transition_lock:
        if action == "start" and _ai_seam_batch_is_active():
            return jsonify(ok=False, active=bool(vcam.active), error="Auto Make Frames is running; wait before starting the Virtual Camera."), 409
        if action == "start" and _ai_seam_generation_active(DATA):
            return jsonify(ok=False, active=bool(vcam.active), error="AI Seam compute is running; wait before starting the Virtual Camera."), 409
        if action == "start":
            with lock:
                width = int(state["width"])
                height = int(state["height"])
                fps_mode = str(state.get("fps_mode", "fixed") or "fixed")
            # A resolution-only camera restart may pass the already negotiated
            # Auto rate so resolution changes do not silently change cadence
            # halfway through a mixed-FPS Clip Flow.
            if "fps" in body:
                try:
                    requested_fps = int(body["fps"])
                except (TypeError, ValueError, OverflowError):
                    return jsonify(ok=False, error="camera FPS must be an integer from 1 to 120"), 400
                if requested_fps < 1 or requested_fps > 120:
                    return jsonify(ok=False, error="camera FPS must be an integer from 1 to 120"), 400
                fps = requested_fps
            else:
                source_rate_hint = None
                # Start preflight negotiates FPS against the actual Clip Flow OPEN
                # clip, not whichever unrelated source happens to be in Scene Builder.
                if fps_mode == "source" and bool(body.get("seam_preflight")):
                    try:
                        planned = str(_clip_config_snapshot().get("open") or "").strip()
                        if planned and Path(planned).is_file():
                            source_rate_hint, _planned_duration = _probe_source_timing(
                                planned, float(source_fps or 30.0)
                            )
                    except Exception:
                        source_rate_hint = None
                fps = _effective_output_fps(
                    source_rate_hint=source_rate_hint,
                    prefer_source_hint=bool(source_rate_hint),
                )
            ok = bool(vcam.active) or bool(vcam.start(width, height, fps, allow_fallback=(fps_mode == "source")))
            bridge_ok = False
            if ok and vcam.active:
                negotiated_fps = int(vcam.output_fps or fps)
                _AI_SEAM_NEGOTIATED_FPS_OVERRIDE = negotiated_fps if fps_mode == "source" else 0
                bridge_ok = _start_libobs_bridge(width, height, negotiated_fps)
            with lock:
                state["virtualcam"] = bool(vcam.active)
                state["virtualcam_error"] = vcam.error if not ok else None
                if not bridge_ok:
                    # libobs is an optional stage; do not fail an otherwise
                    # healthy virtual camera when its renderer is unavailable.
                    state["libobs_active"] = False
        else:
            vcam.stop()
            _stop_libobs_bridge()
            # Output settings temporarily close the camera backend; keep the
            # audio route alive if the source is still running. A deliberate
            # Stop action continues to stop video and audio together.
            with lock:
                engine_running = bool(state.get("running", False))
            if action == "stop" or not engine_running:
                vaudio.stop()
            with lock:
                state["virtualcam"] = False
                state["virtualcam_error"] = None
                if action == "stop" or not engine_running:
                    state["virtualaudio_error"] = None
        return jsonify(
            ok=bool(vcam.active) if action == "start" else True,
            active=bool(vcam.active),
            audio_active=bool(vaudio.active),
            audio_device=vaudio.device_name,
            libobs_active=bool(state.get("libobs_active", False)),
            libobs_error=state.get("libobs_error"),
            error=vcam.error or vaudio.error,
            output_fps=int(vcam.output_fps or 0),
            measured_fps=float(getattr(vcam, "measured_fps", 0.0) or 0.0),
            output_width=int(vcam.width or 0),
            output_height=int(vcam.height or 0),
        )

@app.post("/api/control")
def control():
    global source_playback_origin, source_sequence_index, source_start_offset
    global source_prepared_frame, source_prepared_serial
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(ok=False, error="control payload must be an object"), 400
    action = body.get("action")
    if action == "start":
        if _ai_seam_batch_is_active():
            return jsonify(ok=False, error="Auto Make Frames is running; wait for the verified transition batch to finish before starting the Engine."), 409
        if _ai_seam_generation_active(DATA):
            return jsonify(ok=False, error="AI Seam generation is running; wait for it to finish before starting the Engine."), 409
        with clip_flow_transition_lock:
            if _ai_seam_batch_is_active():
                return jsonify(ok=False, error="Auto Make Frames is running; wait before starting the Engine."), 409
            if _ai_seam_generation_active(DATA):
                return jsonify(ok=False, error="AI Seam compute is running; wait before starting the Engine."), 409
            cfg = _clip_config_snapshot()
            playback_mode = str(body.get("playback_mode") or "auto").lower()
            if playback_mode not in ("auto", "source", "playlist"):
                return jsonify(ok=False, error="playback_mode must be auto, source, or playlist"), 400
            use_playlist = (
                playback_mode == "playlist"
                or (playback_mode == "auto" and cfg.get("enabled") and cfg.get("open") and cfg.get("normal"))
            )
            if use_playlist:
                if not cfg.get("open") or not cfg.get("normal"):
                    return jsonify(ok=False, error="Playlist mode requires an Open clip and at least one Normal clip."), 400
                result = _clip_flow_begin_open(reason="engine_start")
                if not result.get("ok"):
                    return jsonify(**result), 400
                return jsonify(**result)
            # Arm one presentation clock before publishing running=True. Video and
            # audio workers both use this same deadline, so source playback starts
            # from the same media time on low-spec machines too.
            with source_buffer_lock:
                source_buffer.clear()
                source_start_offset = 0.0
                source_playback_origin = time.monotonic() + MEDIA_START_LEAD_SEC
                source_sequence_index = 0
            # Keep the last valid preview/compositor frame as a hold frame while
            # the decoder warms the first playback buffer. Clearing it here made
            # Engine Start emit a black interval until the first prepared frame
            # arrived, especially after a Scene Builder seek on CPU-bound PCs.
            with lock:
                state["running"] = True
                state["loop_count"] = 0
                src = str(state.get("source") or "")
                source_enabled = bool(state.get("source_enabled", True))
                audio_enabled = bool(state.get("virtualaudio_enabled", True))
                loop_enabled = bool(state.get("loop", True))
            audio_started = False
            audio_error = None
            if audio_enabled and source_enabled and src and Path(src).is_file():
                with source_buffer_lock:
                    start_at = source_playback_origin
                audio_started = bool(vaudio.start(src, start_at=start_at, loop=loop_enabled))
                audio_error = vaudio.error
                with lock:
                    state["virtualaudio_error"] = audio_error
            return jsonify(ok=True, running=True, audio_active=audio_started, audio_device=vaudio.device_name, audio_error=audio_error)
    if action == "stop":
        # Serialize manual Stop with playlist transitions to avoid a late
        # decoder handoff restoring a source after the Engine has stopped.
        with clip_flow_transition_lock:
            with clip_flow_lock:
                flow_active = bool(clip_flow_runtime.get("active"))
            if flow_active:
                _clip_flow_finish(phase="cancelled")
            else:
                with lock:
                    state["running"] = False
                vaudio.stop()
            with source_buffer_lock:
                source_buffer.clear()
                source_playback_origin = None
                source_sequence_index = 0
        return jsonify(ok=True, running=False, audio_active=False)
    if action == "clear_overlay":
        with lock:
            state["image"] = None
        return jsonify(ok=True)
    return jsonify(error="unknown action"), 400

@app.get("/api/frame.jpg")
def frame_snapshot():
    data = latest_jpeg
    if data is None:
        with lock:
            w, h = int(state["width"]), int(state["height"])
        blank = np.zeros((h, w, 3), np.uint8)
        ok, enc = cv2.imencode(".jpg", blank)
        data = enc.tobytes() if ok else b""
    return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})

@app.get("/preview.mjpg")
def preview():
    return Response(frame_stream(), mimetype="multipart/x-mixed-replace; boundary=frame")

@app.get("/source.mp4")
def source_video_preview():
    with lock:
        src = str(state.get("source") or "")
    if not src or "://" in src:
        return jsonify(ok=False, error="native video preview requires a local source"), 404
    if src.startswith("/media/"):
        path = DATA / urllib.parse.unquote(src[len("/media/"):])
    else:
        path = Path(src).expanduser()
    try:
        path = path.resolve()
    except Exception:
        return jsonify(ok=False, error="invalid source path"), 400
    if not path.is_file():
        return jsonify(ok=False, error="source file not found"), 404
    return send_file(path, conditional=True, mimetype="video/mp4")

@app.get("/source.mjpg")
def source_preview():
    quality_mode = str(request.args.get("quality", "balanced")).lower()
    if quality_mode not in ("fast", "balanced", "sharp"):
        quality_mode = "balanced"
    return Response(
        frame_stream(source=True, quality_mode=quality_mode),
        mimetype="multipart/x-mixed-replace; boundary=frame"
    )

@app.get("/web.mjpg")
def web_preview():
    return Response(frame_stream(web=True), mimetype="multipart/x-mixed-replace; boundary=frame")

@app.get("/api/web/config")
def web_config():
    with lock:
        url = str(state.get("web_url") or "")
        order = list(state.get("layer_order") or [])
        return jsonify(url=url, enabled=bool(url and state.get("web_enabled", False) and "web" in order),
                       x=int(state.get("web_x", 0)), y=int(state.get("web_y", 0)),
                       width=int(state.get("web_width", 720)), height=int(state.get("web_height", 1280)))

@app.post("/api/web/config")
def web_config_save():
    global web_frame, web_frame_serial, web_render_cache, web_render_cache_key
    body = request.get_json(silent=True)
    if body is None: body = {}
    if not isinstance(body, dict): return jsonify(ok=False, error="web config payload must be an object"), 400
    if "url" in body:
        url = str(body.get("url") or "").strip()
        if url and not (url.startswith("https://") or url.startswith("http://")):
            return jsonify(ok=False, error="Web URL must start with http:// or https://"), 400
    else:
        with lock: url = str(state.get("web_url") or "")
    try:
        for key in ("x", "y", "width", "height"):
            if key in body:
                value = float(body[key])
                if not np.isfinite(value): raise ValueError(f"{key} must be finite")
                body[key] = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        return jsonify(ok=False, error=f"invalid web overlay position/size: {str(exc)[:160]}"), 400
    with lock:
        if "url" in body:
            state["web_url"] = url; state["web_enabled"] = bool(url)
            order = list(state.get("layer_order") or [])
            if url:
                if "web" not in order: order.append("web")
            else: order = [layer for layer in order if layer != "web"]
            state["layer_order"] = order
        else: order = list(state.get("layer_order") or [])
        if "x" in body: state["web_x"] = max(-10000, min(10000, int(body["x"])))
        if "y" in body: state["web_y"] = max(-10000, min(10000, int(body["y"])))
        if "width" in body: state["web_width"] = max(64, min(1920, int(body["width"])))
        if "height" in body: state["web_height"] = max(64, min(1920, int(body["height"])))
        current_url = str(state.get("web_url") or "")
        enabled = bool(current_url and state.get("web_enabled", False) and "web" in order)
    if "url" in body:
        with web_frame_lock: web_frame = None; web_frame_serial += 1
        web_render_cache = None; web_render_cache_key = None
    return jsonify(ok=True, url=current_url, enabled=enabled, x=state.get("web_x", 0), y=state.get("web_y", 0),
                   width=state.get("web_width", 720), height=state.get("web_height", 1280), layer_order=order)

@app.post("/api/web/frame")
def web_frame_push():
    global web_frame, web_frame_serial, web_render_cache, web_render_cache_key
    raw = request.get_data(cache=False)
    if not raw or len(raw) > 8 * 1024 * 1024:
        return ("", 400)
    with lock:
        if not state.get("web_url") or not state.get("web_enabled", False) or "web" not in (state.get("layer_order") or []):
            return ("", 204)
    arr = np.frombuffer(raw, dtype=np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if frame is None:
        return ("", 400)
    frame_h, frame_w = frame.shape[:2]
    # Bound decoded pixel count, not only compressed request size. Small image
    # payloads can expand into very large arrays and stall a low-memory PC.
    if frame_w > 4096 or frame_h > 4096 or frame_w * frame_h > 12_000_000:
        return ("", 413)
    with web_frame_lock:
        web_frame = frame
        web_frame_serial += 1
    web_render_cache = None
    web_render_cache_key = None
    return ("", 204)

MAX_COOKIE_IMPORT_BYTES = 16 * 1024 * 1024

def _install_cookie_bytes(raw):
    if not raw:
        raise acc_store.AuthError("ไฟล์ cookie ว่าง")
    if len(raw) > MAX_COOKIE_IMPORT_BYTES:
        raise acc_store.AuthError("ไฟล์ cookie ใหญ่เกิน 16 MB")
    old = acc_store.COOKIE_FILE
    tmp = old.with_suffix(".import.tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp.write_bytes(raw)
        original = acc_store.COOKIE_FILE
        try:
            acc_store.COOKIE_FILE = tmp
            cookies = acc_store.load_cookie_file()
        finally:
            acc_store.COOKIE_FILE = original
        if not cookies:
            raise acc_store.AuthError("cookie file ไม่มีรายการที่ใช้ได้")
        os.replace(tmp, old)
        return {"ok": True, "status": acc_store.cookie_file_status(), "message": "นำเข้า Cookie Session แล้ว"}
    except Exception:
        try: tmp.unlink(missing_ok=True)
        except Exception: pass
        raise

@app.post("/api/tiktok-dashboard/import-cookie")
def tiktok_dashboard_import_cookie():
    """Import a user-selected cookie file without exposing its contents to the UI."""
    try:
        b = request.get_json(silent=True) or {}
        src = Path(str(b.get("path", "")).strip()).expanduser().resolve()
        if not src.is_file():
            return jsonify(error="ไม่พบไฟล์ cookie"), 400
        if src.stat().st_size > MAX_COOKIE_IMPORT_BYTES:
            return jsonify(error="ไฟล์ cookie ใหญ่เกิน 16 MB"), 400
        # Read at most one byte over the limit in case the file grows after stat().
        with src.open("rb") as cookie_stream:
            raw = cookie_stream.read(MAX_COOKIE_IMPORT_BYTES + 1)
        return jsonify(**_install_cookie_bytes(raw))
    except acc_store.AuthError as e:
        return jsonify(error=str(e)), 400
    except Exception as e:
        return jsonify(error="นำเข้า cookie ไม่สำเร็จ: " + type(e).__name__), 400

@app.post("/api/tiktok-dashboard/import-cookie-file")
def tiktok_dashboard_import_cookie_file():
    """Browser/Electron-safe multipart fallback for a user-selected cookie file."""
    try:
        f = request.files.get("file")
        if f is None:
            return jsonify(error="ไม่พบไฟล์ cookie ที่เลือก"), 400
        # Bound memory use while parsing a user-supplied multipart upload.
        raw = f.stream.read(MAX_COOKIE_IMPORT_BYTES + 1)
        return jsonify(**_install_cookie_bytes(raw))
    except acc_store.AuthError as e:
        return jsonify(error=str(e)), 400
    except Exception as e:
        return jsonify(error="นำเข้า cookie ไม่สำเร็จ: " + type(e).__name__), 400

@app.get("/api/accounts")
def accounts_list():
    return jsonify(items=acc_store.public_list(), browser_profiles=acc_store.browser_profiles())

@app.get("/api/accounts/browser-profiles")
def browser_profiles():
    return jsonify(items=acc_store.browser_profiles())

_manager_pw = None
_manager_browser = None
_manager_context = None
_manager_page = None
_manager_lock = threading.RLock()

_dashboard_context = None
_dashboard_page = None
_dashboard_profile = DATA / "browser_profiles" / "__tiktok_dashboard__"
_dashboard_lock = threading.RLock()
_dashboard_cmds = queue.Queue()
_dashboard_worker = None
_dashboard_worker_lock = threading.Lock()
_dashboard_worker_ready = threading.Event()
_dashboard_worker_error = None
DASHBOARD_URL = "https://shop.tiktok.com/streamer/live/product/dashboard"
comments = comment_bot.CommentEngine(DASHBOARD_URL, acc_store.load_cookie_file)

def _dashboard_worker_main():
    """Own the Playwright objects on one dedicated thread; Playwright is not thread-safe."""
    global _dashboard_context, _dashboard_page, _dashboard_worker_error
    from playwright.sync_api import sync_playwright
    pw = None
    context = None
    page = None
    try:
        _dashboard_profile.mkdir(parents=True, exist_ok=True)
        pw = sync_playwright().start()
        _dashboard_worker_error = None
        _dashboard_worker_ready.set()
        while True:
            op, payload, slot, done = _dashboard_cmds.get()
            try:
                if op == "open":
                    cookies = payload or []
                    if page is None or page.is_closed():
                        if context is None:
                            context = pw.chromium.launch_persistent_context(
                                str(_dashboard_profile),
                                headless=False,
                                viewport={"width": 1080, "height": 680},
                                args=[
                                    "--window-size=1100,760",
                                    "--window-position=260,100",
                                    "--disable-features=TranslateUI",
                                ],
                            )
                            context.clear_cookies()
                            context.add_cookies(cookies)
                        page = context.pages[0] if context.pages else context.new_page()
                        page.goto(DASHBOARD_URL, wait_until="commit", timeout=30000)
                        page.wait_for_timeout(800)
                    page.bring_to_front()
                    slot["result"] = {
                        "url": page.url,
                        "title": page.title(),
                        "profile": str(_dashboard_profile),
                        "reused": False,
                        "browser_open": True,
                    }
                elif op == "status":
                    open_ = page is not None and not page.is_closed()
                    result = {
                        "open": open_,
                        "url": "",
                        "title": "",
                        "profile": str(_dashboard_profile),
                    }
                    if open_:
                        result["url"] = page.url
                        result["title"] = page.title()
                    slot["result"] = result
                elif op == "stop":
                    if context is not None:
                        try:
                            context.close()
                        except Exception:
                            pass
                    context = None
                    page = None
                    _dashboard_context = None
                    _dashboard_page = None
                    slot["result"] = {"stopped": True}
                else:
                    raise RuntimeError("unknown dashboard browser command")
            except Exception as exc:
                slot["error"] = exc
                if op == "open":
                    page = None
            finally:
                done.set()
    except Exception as exc:
        _dashboard_worker_error = type(exc).__name__ + ": " + str(exc)
        _dashboard_worker_ready.set()
    finally:
        _dashboard_context = None
        _dashboard_page = None
        try:
            if context is not None:
                context.close()
        except Exception:
            pass
        try:
            if pw is not None:
                pw.stop()
        except Exception:
            pass

def _dashboard_call(op, payload=None, timeout=35):
    global _dashboard_worker, _dashboard_worker_error
    with _dashboard_worker_lock:
        if _dashboard_worker is None or not _dashboard_worker.is_alive():
            _dashboard_worker_ready.clear()
            _dashboard_worker_error = None
            _dashboard_worker = threading.Thread(
                target=_dashboard_worker_main,
                name="PAOKRA-TikTok-Dashboard",
                daemon=True,
            )
            _dashboard_worker.start()
    if not _dashboard_worker_ready.wait(12):
        raise RuntimeError("TikTok Dashboard browser worker did not start")
    if _dashboard_worker_error:
        raise RuntimeError("TikTok Dashboard browser worker failed: " + _dashboard_worker_error)
    slot, done = {}, threading.Event()
    _dashboard_cmds.put((op, payload, slot, done))
    if not done.wait(timeout):
        raise RuntimeError("TikTok Dashboard browser command timed out")
    if "error" in slot:
        raise slot["error"]
    return slot.get("result", {})

def _open_tiktok_dashboard_mini():
    cookies = acc_store.load_cookie_file()
    return _dashboard_call("open", cookies, timeout=35)

def _manager_cookie_context():
    global _manager_pw, _manager_browser, _manager_context, _manager_page
    from playwright.sync_api import sync_playwright
    with _manager_lock:
        if _manager_page and not _manager_page.is_closed():
            return _manager_page
        cookies = acc_store.load_cookie_file()
        if _manager_pw is None:
            _manager_pw = sync_playwright().start()
        _manager_browser = _manager_pw.chromium.launch(headless=False, args=["--start-maximized"])
        _manager_context = _manager_browser.new_context()
        _manager_context.add_cookies(cookies)
        _manager_page = _manager_context.new_page()
        _manager_page.goto("https://shop.tiktok.com/streamer/live/product/dashboard",
                           wait_until="domcontentloaded", timeout=30000)
        _manager_page.wait_for_timeout(2500)
        return _manager_page

@app.post("/api/tiktok-dashboard/open")
def tiktok_dashboard_open():
    b = request.get_json(silent=True) or {}
    name = str(b.get("name", "__cookie_file__")).strip()
    try:
        if name != "__cookie_file__":
            return jsonify(error="TikTok Dashboard currently requires the Cookie Session"), 400
        opened = _open_tiktok_dashboard_mini()
        return jsonify(ok=True, message="TikTok Dashboard opened",
                       browser_open=True, **opened)
    except acc_store.AuthError as e:
        return jsonify(error=str(e)), 400
    except Exception as e:
        return jsonify(error="TikTok Dashboard เปิดไม่สำเร็จ: " + type(e).__name__ + ": " + str(e)[:240]), 500

@app.get("/api/tiktok-dashboard/status")
def tiktok_dashboard_status():
    with _dashboard_lock:
        open_ = bool(_dashboard_page and not _dashboard_page.is_closed())
        data = {
            "ok": True,
            "open": open_,
            "url": "",
            "title": "",
            "profile": str(_dashboard_profile),
        }
        if open_:
            try:
                data["url"] = _dashboard_page.url
                data["title"] = _dashboard_page.title()
            except Exception:
                data["open"] = False
        return jsonify(**data)

@app.get("/api/comments/status")
def comments_status():
    return jsonify(ok=True, **comments.snapshot())

@app.post("/api/comments/start")
def comments_start():
    try:
        return jsonify(ok=True, **comments.set_enabled(True))
    except Exception as e:
        return jsonify(ok=False, error="comment reader start failed: " + type(e).__name__ + ": " + str(e)[:250]), 500

@app.post("/api/comments/stop")
def comments_stop():
    comments.stop()
    return jsonify(ok=True, **comments.snapshot())

@app.post("/api/comments/config")
def comments_config():
    try:
        body=request.get_json(silent=True) or {}
        allowed={"reader_enabled","auto_reply","dry_run","sensitivity","match_mode","poll_ms","min_interval_sec","user_cooldown_sec","max_per_minute","rules","blocked_terms","blocked_response_terms"}
        patch={k:body[k] for k in allowed if k in body}
        cfg=comment_bot.save_config(patch)
        if cfg.get("reader_enabled"): comments.start()
        return jsonify(ok=True,config=cfg)
    except Exception as e:
        return jsonify(ok=False,error="comment config failed: "+type(e).__name__+": "+str(e)[:250]),400

@app.post("/api/comments/test-rule")
def comments_test_rule():
    try:
        b=request.get_json(silent=True) or {}
        sample=str(b.get("text","")).strip()[:1000]
        if not sample: return jsonify(ok=False,error="text required"),400
        cfg=comment_bot._config()
        rule=comment_bot.match_rule(sample,cfg)
        return jsonify(ok=True,match=bool(rule),rule=rule or None)
    except Exception as e:
        return jsonify(ok=False,error="rule test failed: "+type(e).__name__+ ": "+str(e)[:200]),400

@app.post("/api/comments/reply")
def comments_reply():
    try:
        b=request.get_json(silent=True) or {}
        cid=str(b.get("id","")).strip(); reply=str(b.get("reply","")).strip()
        if not cid or not reply: return jsonify(ok=False,error="id and reply required"),400
        comments.enqueue_reply(cid,reply)
        return jsonify(ok=True,queued=True)
    except Exception as e:
        return jsonify(ok=False,error="comment reply failed: "+type(e).__name__+": "+str(e)[:250]),400

@app.get("/api/cookie/status")
def cookie_status():
    try:
        return jsonify(ok=True, **acc_store.cookie_file_status())
    except Exception as e:
        return jsonify(ok=False, error="cookie status failed: " + type(e).__name__), 500

@app.get("/api/manager/profiles")
def manager_profiles():
    try:
        cookie_status = acc_store.cookie_file_status()
        if not cookie_status.get("configured"):
            try: cookie_status = {"configured": True, "entries": len(acc_store.load_cookie_file())}
            except Exception: pass
    except Exception:
        cookie_status = {"configured": False, "entries": 0}
    items = [{"name": "__cookie_file__", "label": "Cookie file", "type": "cookie", "status": cookie_status}]
    items += [{"name": p["name"], "label": p["name"], "type": "browser",
               "status": {"configured": True, "entries": 0}} for p in acc_store.browser_profiles()]
    return jsonify(profiles=items)

@app.post("/api/manager/open")
def manager_open():
    b = request.get_json(silent=True) or {}
    name = str(b.get("name", "")).strip()
    try:
        if name == "__cookie_file__":
            page = _manager_cookie_context()
            if bool(b.get("inspect")):
                buttons = page.locator("button").all_inner_texts()
                role_buttons = page.locator('[role="button"]').all_inner_texts()
                links = page.locator("a").all_inner_texts()
                inputs = page.locator("input, textarea, select").count()
                body = page.locator("body").inner_text(timeout=5000)
                inspected = "BUTTONS:\n" + "\n".join(x.strip() for x in buttons if x.strip())[:5000]
                inspected += "\n\nROLE BUTTONS:\n" + "\n".join(x.strip() for x in role_buttons if x.strip())[:5000]
                inspected += "\n\nLINKS:\n" + "\n".join(x.strip() for x in links if x.strip())[:5000]
                inspected += "\n\nFORM CONTROLS: " + str(inputs)
                inspected += "\n\nVISIBLE TEXT:\n" + body[:10000]
                return jsonify(ok=True, mode="cookie", message="LIVE Manager opened", text=inspected)
            return jsonify(ok=True, mode="cookie", message="LIVE Manager opened")
        return jsonify(acc_store.browser_login(name))
    except acc_store.AuthError as e:
        return jsonify(error=str(e)), 400
    except Exception as e:
        return jsonify(error="เปิด LIVE Manager ไม่สำเร็จ: " + type(e).__name__), 500

@app.post("/api/manager/product-probe")
def manager_product_probe():
    """Open the product picker and inspect its visible controls without adding or pinning anything."""
    try:
        page = _manager_cookie_context()
        page.get_by_role("button", name="เพิ่มสินค้า").click(timeout=10000)
        page.wait_for_timeout(1200)
        buttons = page.locator("button").all_inner_texts()
        body = page.locator("body").inner_text(timeout=5000)
        radios = page.locator('input[type="radio"]').count()
        return jsonify(ok=True,
                       buttons=[x.strip() for x in buttons if x.strip()],
                       radios=radios,
                       text=body[:16000])
    except Exception as e:
        return jsonify(error="product probe failed: " + type(e).__name__ + ": " + str(e)[:300]), 500

@app.post("/api/manager/inspect")
def manager_inspect():
    b = request.get_json(silent=True) or {}
    name = str(b.get("name", "")).strip()
    try:
        if name == "__cookie_file__":
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=False, args=["--start-maximized"])
                context = browser.new_context()
                context.add_cookies(acc_store.load_cookie_file())
                page = context.new_page()
                page.goto("https://shop.tiktok.com/streamer/live/product/dashboard",
                           wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2500)
                buttons = page.locator("button").all_inner_texts()
                links = page.locator("a").all_inner_texts()
                text = "BUTTONS:\n" + "\n".join(x.strip() for x in buttons if x.strip())[:8000]
                text += "\n\nLINKS:\n" + "\n".join(x.strip() for x in links if x.strip())[:8000]
                context.close()
                browser.close()
            return jsonify(ok=True, text=text, mode="cookie")
        profiles = {x["name"]: x["path"] for x in acc_store.browser_profiles()}
        profile = profiles.get(name)
        if not profile:
            return jsonify(error="browser profile not found"), 404
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                profile, headless=False, args=["--start-maximized"]
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://shop.tiktok.com/streamer/live/product/dashboard", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2500)
            buttons = page.locator("button").all_inner_texts()
            links = page.locator("a").all_inner_texts()
            text = "BUTTONS:\n" + "\n".join(x.strip() for x in buttons if x.strip())[:8000]
            text += "\n\nLINKS:\n" + "\n".join(x.strip() for x in links if x.strip())[:8000]
            context.close()
        return jsonify(ok=True, text=text)
    except Exception as e:
        return jsonify(error="inspect failed: " + type(e).__name__ + ": " + str(e)[:300]), 500

@app.post("/api/accounts/browser-login")
def browser_login():
    b = request.get_json(silent=True) or {}
    try:
        return jsonify(acc_store.browser_login(b.get("name")))
    except acc_store.AuthError as e:
        return jsonify(error=str(e)), 400

@app.delete("/api/accounts/<aid>")
def accounts_remove(aid):
    if not acc_store.remove(aid):
        return jsonify(error="account not found"), 404
    return jsonify(ok=True)

@app.post("/api/accounts/<aid>/test")
def accounts_test(aid):
    try:
        return jsonify(ok=True, account=acc_store.test(aid))
    except KeyError:
        return jsonify(error="account not found"), 404

@app.get("/api/auth/config")
def auth_config():
    return jsonify(acc_store.config_public())

@app.post("/api/auth/config")
def auth_config_save():
    b = request.get_json(silent=True)
    if not isinstance(b, dict):
        return jsonify(ok=False, error="auth config payload must be an object"), 400
    try:
        acc_store.save_config(b.get("client_key"), b.get("client_secret"), b.get("redirect_uri"))
    except acc_store.AuthError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True, **acc_store.config_public())

@app.post("/api/auth/tiktok/start")
def auth_tiktok_start():
    try:
        return jsonify(ok=True, **acc_store.start_auth())
    except acc_store.AuthError as e:
        return jsonify(error=str(e)), 400

@app.get("/api/auth/result")
def auth_result():
    return jsonify(acc_store.result(request.args.get("state", "")))

@app.get("/auth/tiktok/callback")
def auth_tiktok_callback():
    err = acc_store.handle_callback(request.args.to_dict())
    msg = ("เชื่อมต่อไม่สำเร็จ: " + err) if err else "เชื่อมต่อบัญชีสำเร็จ — ปิดหน้าต่างนี้ได้เลย"
    page = ("<!doctype html><meta charset=utf-8><title>PAOKRA</title>"
            "<body style='font-family:Segoe UI,sans-serif;background:#0b0d10;color:#ddd;padding:40px;text-align:center'>"
            "<h3>PAOKRA</h3><p>" + _html.escape(msg) + "</p><script>setTimeout(()=>window.close(),1500)</script>")
    return Response(page, mimetype="text/html")


VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}

@app.get("/api/media/list")
def media_list():
    items = []
    for f in sorted(DATA.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not f.is_file():
            continue
        ext = f.suffix.lower()
        kind = "video" if ext in VIDEO_EXT else "image" if ext in IMAGE_EXT else None
        if not kind:
            continue
        st = f.stat()
        items.append(dict(name=f.name, kind=kind, size=st.st_size,
                          modified=st.st_mtime, url="/media/" + f.name))
    return jsonify(items=items)

@app.post("/api/overlay/use")
def overlay_use():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(ok=False, error="overlay payload must be an object"), 400
    name = Path(str(body.get("name", ""))).name
    target = DATA / name
    if not name or not target.is_file():
        return jsonify(ok=False, error="file not found"), 404
    probe = cv2.imread(str(target), cv2.IMREAD_UNCHANGED)
    if probe is None:
        return jsonify(ok=False, error="image format could not be decoded"), 400
    ih, iw = probe.shape[:2]
    _attach_image_layer(str(target), iw, ih)
    return jsonify(ok=True, url="/media/" + target.name, layer_order=list(state.get("layer_order", [])))

_AI_SEAM_PLANNED_BOX_CACHE = {}
_AI_SEAM_PLANNED_BOX_CACHE_LOCK = threading.Lock()


def _ai_seam_planned_source_box(source_path, output_width, output_height):
    """Predict the exact initial Source box set_source() will create for its first clip.

    When no video is selected, the UI still has a 16:9 placeholder transform. AI
    Seam must not build caches against that placeholder if Open is a portrait clip.
    The result is cached by file identity and output dimensions; it never changes the
    live Source or opens a decoder used by the renderer.
    """
    try:
        path = Path(str(source_path or "")).expanduser().resolve()
        if not path.is_file():
            return None
        st = path.stat()
        out_w = max(1, int(output_width))
        out_h = max(1, int(output_height))
        key = (os.path.normcase(str(path)), int(st.st_size),
               int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))), out_w, out_h)
        with _AI_SEAM_PLANNED_BOX_CACHE_LOCK:
            cached = _AI_SEAM_PLANNED_BOX_CACHE.get(key)
            if cached is not None:
                return tuple(cached)
        capture = cv2.VideoCapture(str(path))
        try:
            if not capture.isOpened():
                return None
            source_w = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            source_h = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        finally:
            capture.release()
        if source_w <= 0 or source_h <= 0:
            return None
        # Mirror _set_source_locked: preserve ratio and fit inside the output canvas.
        scale = min(1.0, out_w / float(source_w), out_h / float(source_h))
        result = (max(40, int(round(source_w * scale))),
                  max(40, int(round(source_h * scale))))
        with _AI_SEAM_PLANNED_BOX_CACHE_LOCK:
            _AI_SEAM_PLANNED_BOX_CACHE[key] = result
            while len(_AI_SEAM_PLANNED_BOX_CACHE) > 32:
                _AI_SEAM_PLANNED_BOX_CACHE.pop(next(iter(_AI_SEAM_PLANNED_BOX_CACHE)))
        return result
    except (OSError, TypeError, ValueError, OverflowError):
        return None
    except Exception:
        return None


def _ai_seam_runtime_profile():
    # Avoid holding the engine lock and Clip Flow lock at the same time.
    config = _clip_config_snapshot()
    with lock:
        output_width = int(state.get("width", 720) or 720)
        output_height = int(state.get("height", 1280) or 1280)
        profile = {
            "width": output_width,
            "height": output_height,
            "fps": int(_effective_output_fps() or 30),
            "fps_mode": str(state.get("fps_mode", "fixed") or "fixed").lower(),
            "fit": str(state.get("source_fit_mode") or "cover"),
            "box_width": int(state.get("source_width", 720) or 720),
            "box_height": int(state.get("source_height", 405) or 405),
            "source": str(state.get("source") or ""),
            "running": bool(state.get("running", False)),
            "virtualcam_active": bool(vcam.active),
        }
    with clip_flow_lock:
        profile["flow_active"] = bool(clip_flow_runtime.get("active"))
    planned_source = str(config.get("open") or
                        ((config.get("normal") or [""])[0] if config.get("normal") else "") or
                        config.get("stop") or "")
    # Clip Flow fixes the output cadence from the configured OPEN clip before
    # starting the camera. Do not build/validate transition MP4s against the
    # unrelated Scene Builder preview's FPS: if the preview is 24 FPS but OPEN
    # is 60 FPS, the generated A->B seam would be rejected at the first handoff.
    # While Flow is idle, predict that same OPEN-derived cadence. Once Flow is
    # active, _effective_output_fps() sees the camera's already-pinned cadence.
    if (profile.get("fps_mode") == "source" and planned_source
            and not profile.get("flow_active") and not profile.get("virtualcam_active")):
        try:
            planned_rate, _planned_duration = _probe_source_timing(
                planned_source, float(source_fps or profile.get("fps") or 30.0)
            )
            if planned_rate is not None and float(planned_rate) > 0.0:
                profile["fps"] = int(
                    _effective_output_fps(
                        source_rate_hint=planned_rate, prefer_source_hint=True
                    ) or 30
                )
        except Exception:
            # Retain the existing detected FPS if OPEN's metadata is unavailable.
            pass
    # During Auto Make Frames the camera must be stopped. Preserve its most
    # recently negotiated source-mode cadence so a per-PC driver fallback is
    # reflected in generated seam files instead of silently invalidating them
    # when playback starts again.
    if profile.get("fps_mode") == "source" and not profile.get("virtualcam_active"):
        try:
            negotiated = int(_AI_SEAM_NEGOTIATED_FPS_OVERRIDE or 0)
            if 1 <= negotiated <= 120:
                profile["fps"] = negotiated
        except (TypeError, ValueError, OverflowError):
            pass
    profile["box_source"] = planned_source
    profile["box_is_planned"] = False
    # The first selected source is fitted by _set_source_locked when state.source
    # is empty; the persistent 720x405 placeholder is NOT the future live transform.
    # Later Clip Flow switches preserve the existing Source transform, so don't
    # replace box dimensions once a real source is selected.
    if not profile["source"] and not profile["flow_active"] and planned_source:
        planned_box = _ai_seam_planned_source_box(planned_source, output_width, output_height)
        if planned_box:
            profile["box_width"], profile["box_height"] = planned_box
            profile["box_is_planned"] = True
    return profile

def _ai_seam_release_cache_sources(seams):
    """Release OpenCV's current file handle before deleting a generated seam.

    Engine Stop intentionally retains the last selected preview source. If that
    source is a generated seam, switch the idle preview to its original B clip
    at the exact resume timestamp. Never interrupt Engine, Camera or Clip Flow.
    """
    cache_root = (DATA / "ai_seam" / "cache").resolve()
    asset_to_record = {}
    for seam in seams if isinstance(seams, list) else []:
        try:
            asset = Path(str(seam.get("asset") or "")).resolve()
            if asset.parent == cache_root and asset.suffix.lower() == ".mp4":
                asset_to_record[os.path.normcase(str(asset))] = dict(seam)
        except Exception:
            continue
    if not asset_to_record:
        return True
    with clip_flow_transition_lock:
        with clip_flow_lock:
            if clip_flow_runtime.get("active"):
                return False
        with lock:
            current_source = str(state.get("source") or "")
            engine_running = bool(state.get("running", False))
        if engine_running or bool(vcam.active):
            return False
        try:
            current_key = os.path.normcase(str(Path(current_source).resolve())) if current_source else ""
        except Exception:
            current_key = os.path.normcase(current_source)
        record = asset_to_record.get(current_key)
        if record is None:
            # None of the candidate cache files is the active decoder source.
            return True
        replacement_choices = []
        try:
            to_path = str(record.get("to_path") or "").strip()
            resume = max(0.0, float(record.get("resume_seconds") or 0.0))
            if to_path:
                replacement_choices.append((to_path, resume))
        except (TypeError, ValueError, OverflowError):
            pass
        for raw in (record.get("from_path"),):
            path = str(raw or "").strip()
            if path:
                replacement_choices.append((path, 0.0))
        try:
            config = _clip_config_snapshot()
            for raw in [*list(config.get("normal") or []), config.get("open"), config.get("stop")]:
                path = str(raw or "").strip()
                if path:
                    replacement_choices.append((path, 0.0))
        except Exception:
            pass
        tried = set()
        for replacement, offset in replacement_choices:
            try:
                key = os.path.normcase(str(Path(replacement).resolve()))
                if key == current_key or key in tried or key in asset_to_record:
                    continue
                tried.add(key)
                if Path(replacement).is_file() and set_source(replacement, start_seconds=offset):
                    return True
            except Exception:
                continue
        # Do not drop manifest rows or lie about deleting a locked cache asset.
        return False

app.register_blueprint(_create_ai_seam_blueprint(
    ROOT, DATA, _ai_seam_runtime_profile,
    generation_guard=clip_flow_transition_lock,
    release_cache_sources=_ai_seam_release_cache_sources,
))

if __name__ == "__main__":
    try:
        _listen_port = int(os.environ.get("PAOKRA_PORT", "8797"))
    except (TypeError, ValueError, OverflowError):
        _listen_port = 8797
    if not 1024 <= _listen_port <= 65535:
        _listen_port = 8797
    print(f"PAOKRA http://127.0.0.1:{_listen_port}")
    app.run("127.0.0.1", _listen_port, threaded=True, debug=False)
