from __future__ import annotations

import hashlib
from contextlib import nullcontext
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np
import psutil
from flask import Blueprint, Response, jsonify, request, send_file

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
MAX_CLIPS_PER_REQUEST = 10000
MAX_SINGLE_FILE_BYTES = 64 * 1024 ** 3
MAX_CACHE_BYTES = 1 * 1024 ** 3  # Generated transitions are disposable cache, never source media.
_SAFE_SIDES = {512, 720, 960}
_DEFAULT_SETTINGS = {
    "enabled": False,
    "compute": "auto",
    "resolution_mode": "auto",
    "transition_frames": 8,
    "max_side": 720,
    "similarity_threshold": 0.40,
    "workers": 1,
}
_SERVICES: dict[str, "AiSeamService"] = {}
_SERVICES_LOCK = threading.RLock()


def _path_key(value: str | Path) -> str:
    try:
        return os.path.normcase(str(Path(value).resolve()))
    except Exception:
        return os.path.normcase(str(value))


def _device_name_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _device_names_match(left: Any, right: Any) -> bool:
    a, b = _device_name_key(left), _device_name_key(right)
    return bool(a and b and (a in b or b in a))


def _dxgi_video_adapters() -> list[dict[str, Any]]:
    """Read exact dedicated-memory descriptors from DXGI on Windows.

    Win32_VideoController.AdapterRAM is a 32-bit field and saturates close to
    4 GiB for some 8 GiB adapters. IDXGIAdapter1::GetDesc1 uses SIZE_T and
    reports the driver's 64-bit dedicated-memory value instead.
    """
    if os.name != "nt":
        return []
    try:
        import ctypes
        import ctypes.wintypes as wt
        import uuid

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", wt.DWORD), ("HighPart", wt.LONG)]

        class DXGI_ADAPTER_DESC1(ctypes.Structure):
            _fields_ = [
                ("Description", ctypes.c_wchar * 128),
                ("VendorId", wt.UINT), ("DeviceId", wt.UINT),
                ("SubSysId", wt.UINT), ("Revision", wt.UINT),
                ("DedicatedVideoMemory", ctypes.c_size_t),
                ("DedicatedSystemMemory", ctypes.c_size_t),
                ("SharedSystemMemory", ctypes.c_size_t),
                ("AdapterLuid", LUID), ("Flags", wt.UINT),
            ]

        u = uuid.UUID("770aae78-f26f-4dba-a829-253c83d1b387")
        iid = GUID(u.time_low, u.time_mid, u.time_hi_version,
                   (ctypes.c_ubyte * 8).from_buffer_copy(u.bytes[8:]))

        def vmethod(pointer, index, result_type, arg_types):
            table = ctypes.cast(
                pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
            ).contents
            return ctypes.WINFUNCTYPE(result_type, *arg_types)(table[index])

        dxgi = ctypes.WinDLL("dxgi")
        create_factory = dxgi.CreateDXGIFactory1
        create_factory.argtypes = [
            ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)
        ]
        create_factory.restype = ctypes.c_long
        factory = ctypes.c_void_p()
        hr = create_factory(ctypes.byref(iid), ctypes.byref(factory))
        if hr != 0 or not factory.value:
            return []

        enum_adapter = vmethod(
            factory, 12, ctypes.c_long,
            [ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_void_p)],
        )
        found: list[dict[str, Any]] = []
        try:
            for index in range(32):
                adapter = ctypes.c_void_p()
                hr = enum_adapter(factory, index, ctypes.byref(adapter))
                if hr != 0 or not adapter.value:
                    break
                try:
                    get_desc = vmethod(
                        adapter, 10, ctypes.c_long,
                        [ctypes.c_void_p, ctypes.POINTER(DXGI_ADAPTER_DESC1)],
                    )
                    desc = DXGI_ADAPTER_DESC1()
                    if get_desc(adapter, ctypes.byref(desc)) == 0:
                        found.append({
                            "name": str(desc.Description).strip(),
                            "vendor_id": int(desc.VendorId),
                            "dedicated_bytes": int(desc.DedicatedVideoMemory),
                            "shared_bytes": int(desc.SharedSystemMemory),
                            "flags": int(desc.Flags),
                        })
                finally:
                    vmethod(adapter, 2, ctypes.c_ulong, [ctypes.c_void_p])(adapter)
        finally:
            vmethod(factory, 2, ctypes.c_ulong, [ctypes.c_void_p])(factory)
        return found
    except Exception:
        # Detection must fall through to Vulkan/NVIDIA telemetry/WMI rather than
        # preventing the rest of PAOKRA from starting.
        return []


def _file_signature(path: str | Path) -> dict[str, int]:
    p = Path(path)
    st = p.stat()
    return {"size": int(st.st_size), "mtime_ns": int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))}


def _atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex[:8])
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _safe_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _frame_from(path: Path, at_seconds: float | None = None, tail: bool = False) -> tuple[np.ndarray, dict[str, Any]]:
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        cap.release()
        raise ValueError(f"Video cannot be decoded: {path.name}")
    try:
        width = max(1, int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1))
        height = max(1, int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 1))
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        if not np.isfinite(fps) or fps < 1.0 or fps > 240.0:
            fps = 30.0
        count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        duration = count / fps if count > 0 else 0.0
        if tail:
            if count > 0:
                cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, count - 1))
            else:
                cap.set(cv2.CAP_PROP_POS_MSEC, 0)
        elif at_seconds is not None:
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, float(at_seconds)) * 1000.0)
        ok, frame = cap.read()
        if not ok or frame is None:
            raise ValueError(f"Could not read a video frame: {path.name}")
        meta = {"width": width, "height": height, "fps": fps, "frame_count": count, "duration": duration}
        return frame, meta
    finally:
        cap.release()


def _clip_metadata(path: Path) -> dict[str, Any]:
    frame, meta = _frame_from(path, 0.0, False)
    sig = _file_signature(path)
    return {
        "width": meta["width"], "height": meta["height"], "fps": round(meta["fps"], 5),
        "frame_count": meta["frame_count"], "duration": round(meta["duration"], 4),
        "signature": sig,
    }


def _fit_frame(frame: np.ndarray, width: int, height: int, fit: str = "cover") -> np.ndarray:
    width, height = max(32, int(width)), max(32, int(height))
    sh, sw = frame.shape[:2]
    if fit == "cover":
        want = width / float(height)
        actual = sw / float(max(1, sh))
        if actual > want:
            crop = max(1, int(round(sh * want)))
            x = max(0, (sw - crop) // 2)
            frame = frame[:, x:x + crop]
        elif actual < want:
            crop = max(1, int(round(sw / want)))
            y = max(0, (sh - crop) // 2)
            frame = frame[y:y + crop, :]
        return cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA if frame.shape[1] > width else cv2.INTER_LINEAR)
    scale = min(width / float(max(1, sw)), height / float(max(1, sh)))
    rw, rh = max(1, int(round(sw * scale))), max(1, int(round(sh * scale)))
    resized = cv2.resize(frame, (rw, rh), interpolation=cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR)
    canvas = np.zeros((height, width, 3), np.uint8)
    x, y = max(0, (width - rw) // 2), max(0, (height - rh) // 2)
    canvas[y:y + rh, x:x + rw] = resized
    return canvas


def _similarity(a: np.ndarray, b: np.ndarray) -> dict[str, Any]:
    aa = cv2.resize(a, (160, 160), interpolation=cv2.INTER_AREA)
    bb = cv2.resize(b, (160, 160), interpolation=cv2.INTER_AREA)
    ga, gb = cv2.cvtColor(aa, cv2.COLOR_BGR2GRAY), cv2.cvtColor(bb, cv2.COLOR_BGR2GRAY)
    ha = cv2.calcHist([aa], [0, 1, 2], None, [8, 8, 8], [0, 256, 0, 256, 0, 256])
    hb = cv2.calcHist([bb], [0, 1, 2], None, [8, 8, 8], [0, 256, 0, 256, 0, 256])
    cv2.normalize(ha, ha, alpha=1.0, norm_type=cv2.NORM_L1)
    cv2.normalize(hb, hb, alpha=1.0, norm_type=cv2.NORM_L1)
    corr = float(cv2.compareHist(ha, hb, cv2.HISTCMP_CORREL))
    hist_score = max(0.0, min(1.0, (corr + 1.0) / 2.0))
    pixel_score = max(0.0, min(1.0, 1.0 - float(cv2.absdiff(ga, gb).mean()) / 255.0))
    score = max(0.0, min(1.0, 0.55 * hist_score + 0.45 * pixel_score))
    risk = "good" if score >= 0.62 else "caution" if score >= 0.40 else "risky"
    return {"score": round(score, 4), "histogram_similarity": round(hist_score, 4),
            "pixel_similarity": round(pixel_score, 4), "risk": risk,
            "note": "Similarity is a lightweight visual heuristic, not pose or scene understanding."}


class AiSeamService:
    def __init__(self, root: Path, data: Path, output_profile: Callable[[], dict[str, Any]] | None = None,
                 generation_guard: Any = None,
                 release_cache_sources: Callable[[list[dict[str, Any]]], bool] | None = None):
        self.root = root.resolve()
        self.data = data.resolve()
        self.base = self.data / "ai_seam"
        self.clips_dir = self.base / "clips"
        self.cache_dir = self.base / "cache"
        self.work_dir = self.base / "work"
        self.preview_dir = self.work_dir / "preview-cache"
        self.manifest_path = self.base / "project.json"
        self.output_profile = output_profile or (lambda: {})
        self.generation_guard = generation_guard
        self.release_cache_sources = release_cache_sources
        self.lock = threading.RLock()
        self.generation_lock = threading.Lock()
        self.jobs: dict[str, dict[str, Any]] = {}
        self.last_scan: dict[str, Any] | None = None
        self.last_scan_at = 0.0
        self._hardware_profile_cache: dict[str, Any] | None = None
        self._hardware_profile_at = 0.0
        self.base.mkdir(parents=True, exist_ok=True)
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.preview_dir.mkdir(parents=True, exist_ok=True)
        self.manifest = self._load_manifest()

    def _load_manifest(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raw = {}
        except Exception:
            raw = {}
        clips = raw.get("clips") if isinstance(raw.get("clips"), list) else []
        seams = raw.get("seams") if isinstance(raw.get("seams"), list) else []
        settings = dict(_DEFAULT_SETTINGS)
        if isinstance(raw.get("settings"), dict):
            settings.update(raw["settings"])
        settings["enabled"] = bool(settings.get("enabled", False))
        settings["compute"] = settings.get("compute") if settings.get("compute") in ("auto", "gpu", "cpu") else "auto"
        settings["resolution_mode"] = settings.get("resolution_mode") if settings.get("resolution_mode") in ("auto", "manual") else "auto"
        settings["transition_frames"] = _safe_int(settings.get("transition_frames"), 4, 12, 8)
        settings["max_side"] = _safe_int(settings.get("max_side"), 512, 960, 720)
        if settings["max_side"] not in _SAFE_SIDES:
            settings["max_side"] = 720
        try:
            settings["similarity_threshold"] = min(0.8, max(0.2, float(settings["similarity_threshold"])))
        except Exception:
            settings["similarity_threshold"] = 0.40
        return {"version": 1, "clips": clips, "seams": seams, "settings": settings}

    def _safe_cache_asset(self, raw: Any) -> Path | None:
        try:
            path = Path(str(raw or "")).resolve()
            if path.suffix.lower() != ".mp4" or path.parent != self.cache_dir.resolve():
                return None
            return path
        except Exception:
            return None

    def save(self) -> None:
        _atomic_json(self.manifest_path, self.manifest)

    def settings(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.manifest["settings"])

    def update_settings(self, body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise ValueError("Settings must be a JSON object")
        with self.lock:
            current = dict(self.manifest["settings"])
            if "enabled" in body:
                current["enabled"] = bool(body["enabled"])
            if "compute" in body:
                value = str(body["compute"]).lower()
                if value not in ("auto", "gpu", "cpu"):
                    raise ValueError("compute must be auto, gpu or cpu")
                current["compute"] = value
            if "resolution_mode" in body:
                value = str(body["resolution_mode"]).lower()
                if value not in ("auto", "manual"):
                    raise ValueError("resolution_mode must be auto or manual")
                current["resolution_mode"] = value
            if "transition_frames" in body:
                current["transition_frames"] = _safe_int(body["transition_frames"], 4, 12, 8)
            if "max_side" in body:
                value = _safe_int(body["max_side"], 512, 960, 720)
                current["max_side"] = value if value in _SAFE_SIDES else 720
            if "similarity_threshold" in body:
                try:
                    value = float(body["similarity_threshold"])
                    if not np.isfinite(value):
                        raise ValueError()
                    current["similarity_threshold"] = min(0.8, max(0.2, value))
                except Exception:
                    raise ValueError("similarity_threshold must be a finite number")
            self.manifest["settings"] = current
            self.save()
            return dict(current)

    def clips(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(c) for c in self.manifest["clips"]]

    def _find_clip_locked(self, clip_id: str) -> dict[str, Any] | None:
        for clip in self.manifest["clips"]:
            if str(clip.get("id")) == str(clip_id):
                return clip
        return None

    def get_clip(self, clip_id: str) -> dict[str, Any]:
        with self.lock:
            clip = self._find_clip_locked(clip_id)
            if not clip:
                raise KeyError("clip not found")
            return dict(clip)

    def add_paths(self, paths: Any) -> dict[str, Any]:
        profile = self._active_profile()
        if profile.get("flow_active") or profile.get("running") or profile.get("virtualcam_active"):
            raise RuntimeError("Stop Engine, Clip Flow and Virtual Camera before scanning new clips into AI Seam")
        if self.generation_lock.locked():
            raise RuntimeError("Wait for the current AI Seam compute job before changing the clip library")
        if not isinstance(paths, list):
            raise ValueError("paths must be a list of file paths")
        if len(paths) > MAX_CLIPS_PER_REQUEST:
            raise ValueError(f"Choose at most {MAX_CLIPS_PER_REQUEST} files per add operation")
        added, skipped, errors = [], [], []
        known: set[str] = set()
        with self.lock:
            for old in self.manifest["clips"]:
                known.add(_path_key(old.get("path", "")))
            for raw in paths:
                try:
                    raw_path = str(raw or "").strip()
                    if not raw_path:
                        continue
                    path = Path(raw_path).expanduser().resolve(strict=True)
                    if path.suffix.lower() not in VIDEO_EXTS:
                        raise ValueError(f"Unsupported video type: {path.suffix or '(no extension)'}")
                    if not path.is_file():
                        raise ValueError("Path is not a file")
                    if path.stat().st_size <= 0 or path.stat().st_size > MAX_SINGLE_FILE_BYTES:
                        raise ValueError("File is empty or exceeds 64 GiB")
                    key = _path_key(path)
                    if key in known:
                        skipped.append(path.name)
                        continue
                    meta = _clip_metadata(path)
                    clip = {"id": uuid.uuid4().hex, "name": path.name, "path": str(path),
                            "size": int(path.stat().st_size), "added_at": time.time(), **meta}
                    self.manifest["clips"].append(clip)
                    known.add(key)
                    added.append(dict(clip))
                except Exception as exc:
                    errors.append({"path": str(raw)[:260], "error": str(exc)[:220]})
            self.save()
        return {"ok": True, "added": added, "skipped": skipped, "errors": errors,
                "clip_count": len(self.clips())}

    def reorder(self, ids: Any) -> list[dict[str, Any]]:
        profile = self._active_profile()
        if profile.get("flow_active") or profile.get("running") or profile.get("virtualcam_active"):
            raise RuntimeError("Stop Engine, Clip Flow and Virtual Camera before reordering the AI Seam library")
        if self.generation_lock.locked():
            raise RuntimeError("Wait for the current AI Seam compute job before reordering the clip library")
        if not isinstance(ids, list):
            raise ValueError("ids must be a list")
        with self.lock:
            existing = {str(c["id"]): c for c in self.manifest["clips"]}
            if len(ids) != len(existing) or len(set(map(str, ids))) != len(ids) or set(map(str, ids)) != set(existing):
                raise ValueError("Reorder must contain every current clip ID exactly once")
            self.manifest["clips"] = [existing[str(i)] for i in ids]
            self.save()
            return [dict(c) for c in self.manifest["clips"]]

    def _active_profile(self) -> dict[str, Any]:
        try:
            profile = self.output_profile()
            return profile if isinstance(profile, dict) else {}
        except Exception:
            return {}

    def remove_clip(self, clip_id: str) -> dict[str, Any]:
        profile = self._active_profile()
        if profile.get("flow_active") or profile.get("running") or profile.get("virtualcam_active"):
            raise RuntimeError("Stop Engine, Clip Flow and Virtual Camera before removing clips or their generated seam files")
        if self.generation_lock.locked():
            raise RuntimeError("Wait for the current AI Seam compute operation before removing clips")
        active_path = _path_key(profile.get("source", "")) if profile.get("source") else ""
        with self.lock:
            clip = self._find_clip_locked(clip_id)
            if not clip:
                raise KeyError("clip not found")
            self.manifest["clips"] = [c for c in self.manifest["clips"] if c.get("id") != clip_id]
            removed, kept = [], []
            for seam in self.manifest["seams"]:
                if seam.get("from_id") == clip_id or seam.get("to_id") == clip_id:
                    asset = self._safe_cache_asset(seam.get("asset"))
                    if asset is not None and active_path and _path_key(asset) == active_path:
                        kept.append(seam)
                    elif asset is not None:
                        removed.append(str(asset))
                else:
                    kept.append(seam)
            self.manifest["seams"] = kept
            self.save()
        for asset in removed:
            try:
                Path(asset).unlink(missing_ok=True)
            except Exception:
                pass
        return {"ok": True, "clip_count": len(self.clips()), "removed_cache_files": len(removed)}

    def _exe_paths(self) -> tuple[Path, Path, Path]:
        exe = self.root / "ai-studio" / "runtime" / "rife" / "rife-ncnn-vulkan.exe"
        model = self.root / "ai-studio" / "runtime" / "rife" / "rife-v4.6"
        ffmpeg_env = str(os.environ.get("PAOKRA_FFMPEG") or "").strip()
        ffmpeg = Path(ffmpeg_env) if ffmpeg_env else self.root / "ffmpeg" / "ffmpeg.exe"
        if not ffmpeg.is_file():
            ffmpeg = self.root / "ai-studio" / "runtime" / "ffmpeg.exe"
        if not ffmpeg.is_file():
            candidate = shutil.which("ffmpeg")
            if candidate:
                ffmpeg = Path(candidate)
        return exe, model, ffmpeg

    def _vulkan_devices(self) -> tuple[bool, list[str], str]:
        vulkaninfo = shutil.which("vulkaninfo")
        if not vulkaninfo:
            candidate = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "vulkaninfo.exe"
            vulkaninfo = str(candidate) if candidate.is_file() else None
        if not vulkaninfo:
            return False, [], "vulkaninfo not found"
        try:
            p = subprocess.run([vulkaninfo, "--summary"], capture_output=True, text=True,
                               timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            output = (p.stdout or "") + "\n" + (p.stderr or "")
            names = re.findall(r"deviceName\s*=\s*(.+)", output, flags=re.IGNORECASE)
            names = [re.sub(r"\s+", " ", n).strip() for n in names if n.strip()]
            ok = p.returncode == 0 and bool(names)
            return ok, names, output[-1200:]
        except Exception as exc:
            return False, [], f"{type(exc).__name__}: {str(exc)[:180]}"

    def _run_rife(self, a: Path, b: Path, out: Path, fraction: float, compute: str,
                  model: Path, timeout: int = 45, jobs: str = "1:1:1",
                  gpu_index: int = 0) -> tuple[bool, str, float]:
        exe, _, _ = self._exe_paths()
        gpu_arg = "-1" if compute == "cpu" else str(max(0, min(31, int(gpu_index or 0))))
        jobs = jobs if re.fullmatch(r"[1-2]:[1-2]:[1-2]", str(jobs or "")) else "1:1:1"
        if compute == "cpu":
            jobs = "1:1:1"
        command = [str(exe), "-0", str(a), "-1", str(b), "-o", str(out),
                   "-s", f"{fraction:.7f}", "-m", str(model), "-g", gpu_arg, "-j", jobs]
        start = time.monotonic()
        try:
            p = subprocess.run(command, cwd=str(exe.parent), capture_output=True, text=True,
                               timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            elapsed = time.monotonic() - start
            note = ((p.stdout or "") + "\n" + (p.stderr or ""))[-1800:]
            if p.returncode != 0 or not out.is_file() or out.stat().st_size < 128:
                return False, f"RIFE exited {p.returncode}: {note[-650:]}", elapsed
            image = cv2.imread(str(out), cv2.IMREAD_COLOR)
            if image is None or image.size == 0:
                return False, "RIFE output image could not be decoded", elapsed
            return True, note, elapsed
        except subprocess.TimeoutExpired:
            return False, f"RIFE timed out after {timeout}s", time.monotonic() - start
        except Exception as exc:
            return False, f"{type(exc).__name__}: {str(exc)[:400]}", time.monotonic() - start

    def _wmi_vram_estimate(self, reason: str) -> dict[str, Any]:
        """Use Windows adapter memory as a conservative fallback when live Vulkan heap info is too slow/unavailable."""
        try:
            powershell = shutil.which("powershell.exe") or str(
                Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" /
                "WindowsPowerShell" / "v1.0" / "powershell.exe"
            )
            if Path(powershell).is_file():
                script = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM | ConvertTo-Json -Compress"
                result = subprocess.run(
                    [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
                    capture_output=True, text=True, timeout=5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                )
                adapters = json.loads(result.stdout or "[]")
                if isinstance(adapters, dict):
                    adapters = [adapters]
                if isinstance(adapters, list):
                    visible = [
                        item for item in adapters
                        if isinstance(item, dict) and int(item.get("AdapterRAM") or 0) > 0
                        and not re.search(r"microsoft basic|remote display|software", str(item.get("Name") or ""), re.I)
                    ]
                    if visible:
                        adapter = max(visible, key=lambda item: int(item.get("AdapterRAM") or 0))
                        total = int(adapter.get("AdapterRAM") or 0)
                        # AdapterRAM is a 32-bit field on many Windows drivers. A value
                        # near 0xFFFFFFFF can mean either a 4 GiB card or an 8+ GiB card
                        # clipped by WMI; never report that ambiguous value as exact VRAM.
                        if total >= (4 * 1024 ** 3 - 8 * 1024 ** 2):
                            return {
                                "vram_total_bytes": 0, "vram_budget_bytes": 0,
                                "vram_used_bytes": 0, "vram_available_bytes": 0,
                                "vram_source": "unavailable (WMI AdapterRAM saturated)",
                                "gpu_device": str(adapter.get("Name") or ""),
                                "vram_note": "Windows AdapterRAM is at its 32-bit ceiling and cannot distinguish 4 GiB from larger VRAM. " + str(reason)[:160],
                            }
                        # WMI does not report the live budget. Reserve 30% for Windows/driver usage.
                        budget = int(total * 0.85)
                        available = int(total * 0.70)
                        return {
                            "vram_total_bytes": total, "vram_budget_bytes": budget,
                            "vram_used_bytes": max(0, budget - available), "vram_available_bytes": available,
                            "vram_source": "Windows Win32_VideoController.AdapterRAM (conservative estimate)",
                            "gpu_device": str(adapter.get("Name") or ""),
                            "vram_note": "Estimated from Windows AdapterRAM; live Vulkan memory budget unavailable. " + str(reason)[:160],
                        }
        except Exception as exc:
            reason = str(reason) + "; WMI fallback: " + f"{type(exc).__name__}: {str(exc)[:120]}"
        return {
            "vram_total_bytes": 0, "vram_budget_bytes": 0, "vram_used_bytes": 0,
            "vram_available_bytes": 0, "vram_source": "unavailable",
            "vram_note": str(reason)[:300],
        }

    def _vulkan_memory_info(self, devices: list[str] | None = None) -> dict[str, Any]:
        """Read the selected RIFE adapter's VRAM without trusting 32-bit WMI values."""
        devices = list(devices or [])
        try:
            adapters = [
                x for x in _dxgi_video_adapters()
                if int(x.get("dedicated_bytes") or 0) > 0 and not (int(x.get("flags") or 0) & 2)
                and not re.search(r"microsoft basic|remote display|software|swiftshader|lavapipe",
                                  str(x.get("name") or ""), re.I)
            ]
            selected = None
            selected_index = 0
            # RIFE's -g argument is the Vulkan adapter index. Match that ordered
            # list to DXGI instead of accidentally displaying a different GPU's VRAM.
            for index, vulkan_name in enumerate(devices):
                selected = next((a for a in adapters
                                 if _device_names_match(a.get("name"), vulkan_name)), None)
                if selected is not None:
                    selected_index = index
                    break
            if selected is None and adapters:
                selected = max(adapters, key=lambda x: int(x.get("dedicated_bytes") or 0))
                matching_index = next((i for i, name in enumerate(devices)
                                       if _device_names_match(name, selected.get("name"))), None)
                selected_index = int(matching_index) if matching_index is not None else 0
            if selected is not None:
                total = int(selected["dedicated_bytes"])
                # DXGI reports dedicated capacity, not live free memory. Use a
                # conservative sizing allowance and label it as an estimate.
                budget = int(total * 0.80)
                available = int(total * 0.65)
                return {
                    "vram_total_bytes": total, "vram_budget_bytes": budget,
                    "vram_used_bytes": max(0, total - available),
                    "vram_available_bytes": available,
                    "vram_source": "DXGI IDXGIAdapter1::GetDesc1 (64-bit dedicated memory)",
                    "gpu_device": str(selected.get("name") or ""),
                    "gpu_index": selected_index,
                    "vram_note": (
                        "Dedicated VRAM capacity read from the matching DXGI adapter. "
                        "DXGI does not provide live free/used VRAM; budget and available "
                        "values are conservative estimates, not live telemetry."
                    ),
                }
        except Exception:
            pass

        nvidia_smi = shutil.which("nvidia-smi")
        if nvidia_smi:
            try:
                telemetry = subprocess.run(
                    [nvidia_smi, "--query-gpu=name,memory.total,memory.free,memory.used",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=3,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                )
                if telemetry.returncode == 0:
                    for line in (telemetry.stdout or "").splitlines():
                        fields = [part.strip() for part in line.rsplit(",", 3)]
                        if len(fields) != 4:
                            continue
                        name = fields[0]
                        try:
                            total_mb, free_mb, used_mb = [int(float(value)) for value in fields[1:]]
                        except (ValueError, OverflowError):
                            continue
                        if total_mb > 0:
                            total, free, used = total_mb * 1024**2, free_mb * 1024**2, used_mb * 1024**2
                            return {
                                "vram_total_bytes": total, "vram_budget_bytes": total,
                                "vram_used_bytes": max(0, used), "vram_available_bytes": max(0, min(total, free)),
                                "vram_source": "NVIDIA-SMI live memory telemetry", "gpu_device": name,
                                "vram_note": "Live NVIDIA-SMI GPU memory; Auto profile uses available memory with a safety margin",
                            }
            except Exception:
                pass
        vulkaninfo = shutil.which("vulkaninfo")
        if not vulkaninfo:
            candidate = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "vulkaninfo.exe"
            vulkaninfo = str(candidate) if candidate.is_file() else None
        if not vulkaninfo:
            return self._wmi_vram_estimate("vulkaninfo executable not found")
        try:
            result = subprocess.run(
                [vulkaninfo], capture_output=True, text=True, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
            output = (result.stdout or "") + "\n" + (result.stderr or "")
            start = output.find("VkPhysicalDeviceMemoryProperties:")
            if start < 0:
                return self._wmi_vram_estimate("Vulkan memory heap details were not present")
            end = output.find("VkPhysicalDeviceMemoryProperties:", start + 1)
            block = output[start:] if end < 0 else output[start:end]
            total = budget = used = 0
            heaps = list(re.finditer(
                r"memoryHeaps\[\d+\]:([\s\S]*?)(?=memoryHeaps\[\d+\]:|memoryTypes:)",
                block
            ))
            for match in heaps:
                heap = match.group(1)
                if "MEMORY_HEAP_DEVICE_LOCAL_BIT" not in heap:
                    continue
                size_match = re.search(r"(?m)^\s*size\s*=\s*(\d+)", heap)
                if not size_match:
                    continue
                size = int(size_match.group(1))
                budget_match = re.search(r"(?m)^\s*budget\s*=\s*(\d+)", heap)
                usage_match = re.search(r"(?m)^\s*usage\s*=\s*(\d+)", heap)
                heap_budget = int(budget_match.group(1)) if budget_match else size
                heap_used = int(usage_match.group(1)) if usage_match else 0
                total += size
                budget += min(size, max(0, heap_budget))
                used += min(size, max(0, heap_used))
            if total <= 0:
                return self._wmi_vram_estimate("No device-local Vulkan heap could be identified")
            available = max(0, min(total, budget) - min(total, used))
            return {"vram_total_bytes": total, "vram_budget_bytes": budget,
                    "vram_used_bytes": used, "vram_available_bytes": available,
                    "vram_source": "vulkaninfo device-local heaps",
                    "vram_note": "Vulkan heap budget is an estimate and may be shared with the OS/driver"}
        except Exception as exc:
            return self._wmi_vram_estimate(f"{type(exc).__name__}: {str(exc)[:180]}")

    def _resource_profile(self, force: bool = False, devices: list[str] | None = None) -> dict[str, Any]:
        """Recommend safe resolution and RIFE queue depth from VRAM budget + system RAM."""
        now = time.monotonic()
        if (not force and self._hardware_profile_cache is not None
                and now - self._hardware_profile_at < 180.0):
            gpu_info = dict(self._hardware_profile_cache)
        else:
            names = list(devices or self._vulkan_devices()[1])
            gpu_info = self._vulkan_memory_info(names)
            if not gpu_info.get("gpu_device"):
                gpu_info["gpu_device"] = next(
                    (name for name in names if not re.search(r"microsoft|swiftshader|lavapipe|software", name, re.I)), ""
                )
            if not isinstance(gpu_info.get("gpu_index"), int):
                gpu_info["gpu_index"] = next(
                    (index for index, name in enumerate(names)
                     if not re.search(r"microsoft|swiftshader|lavapipe|software", name, re.I)), 0
                )
            gpu_info["vulkan_devices"] = names
            self._hardware_profile_cache = dict(gpu_info)
            self._hardware_profile_at = now
        mem = psutil.virtual_memory()
        ram_available = int(mem.available)
        total_vram = int(gpu_info.get("vram_total_bytes") or 0)
        avail_vram = int(gpu_info.get("vram_available_bytes") or 0)
        gib = 1024 ** 3
        if ram_available < 4 * gib:
            max_side = 512
        elif total_vram > 0:
            if total_vram <= int(5.0 * gib) or avail_vram < int(3.5 * gib):
                max_side = 512
            elif total_vram >= 8 * gib and avail_vram >= 6 * gib and ram_available >= 8 * gib:
                max_side = 960
            else:
                max_side = 720
        else:
            # No reliable VRAM reading: cap the workload at the low-memory profile.
            max_side = 512
        if (total_vram <= int(5.0 * gib) or avail_vram < int(3.5 * gib)
                or ram_available < 4 * gib or not total_vram):
            rife_jobs = "1:1:1"
        elif total_vram < 8 * gib or ram_available < 8 * gib:
            rife_jobs = "1:2:1"
        else:
            rife_jobs = "1:2:2"
        return {
            **gpu_info,
            "ram_total_bytes": int(mem.total),
            "ram_available_bytes": ram_available,
            "ram_available_percent": round(float(ram_available) / max(1, int(mem.total)) * 100, 1),
            "recommended_max_side": max_side,
            "recommended_rife_jobs": rife_jobs,
            # RIFE-NCNN is a single pair-of-frames CLI. Keep pair concurrency at one;
            # adapt its internal load/process/save queues instead of launching
            # multiple GPU processes that can exhaust low-VRAM cards.
            "generation_batch": 1,
        }

    def _effective_settings(self) -> dict[str, Any]:
        settings = self.settings()
        if settings.get("resolution_mode", "auto") == "auto":
            settings["max_side"] = int(self._resource_profile().get("recommended_max_side") or 512)
        return settings

    def scan_environment(self, compute_override: str | None = None) -> dict[str, Any]:
        guard = self.generation_guard if self.generation_guard is not None else nullcontext()
        with guard:
            profile = self._active_profile()
            if profile.get("flow_active") or profile.get("running") or profile.get("virtualcam_active"):
                mem = psutil.virtual_memory()
                disk = shutil.disk_usage(str(self.data))
                return {"ok": False, "scanned_at": time.time(), "ram_total_bytes": int(mem.total),
                        "ram_available_bytes": int(mem.available), "disk_free_bytes": int(disk.free),
                        "vulkan_available": False, "vulkan_devices": [], "rife_available": self._exe_paths()[0].is_file(),
                        "model_available": self._exe_paths()[1].joinpath("flownet.bin").is_file(),
                        "ffmpeg_available": self._exe_paths()[2].is_file(), "recommended_compute": "auto",
                        "recommended_max_side": 512, "generation_batch": 1,
                        "probe": {"ok": False, "compute": "auto", "seconds": 0.0,
                                  "message": "Scan refused while Engine, Clip Flow or Virtual Camera is active. Stop playback first."},
                        "note": "No model inference was run while the live pipeline was active."}
            if not self.generation_lock.acquire(blocking=False):
                raise RuntimeError("Another AI Seam compute operation is already running")
        try:
            return self._scan_environment_locked(compute_override)
        finally:
            self.generation_lock.release()

    def _scan_environment_locked(self, compute_override: str | None = None) -> dict[str, Any]:
        started = time.monotonic()
        exe, model, ffmpeg = self._exe_paths()
        vulkan_ok, devices, vulkan_note = self._vulkan_devices()
        # An explicit Scan is a fresh hardware check; do not reuse a stale
        # 180-second resource profile when this user action promises a rescan.
        resource = self._resource_profile(force=True, devices=devices)
        mem = psutil.virtual_memory()
        disk = shutil.disk_usage(str(self.data))
        gpu = str(resource.get("gpu_device") or next(
            (x for x in devices if not re.search(r"microsoft|swiftshader|lavapipe|software", x, re.I)), ""
        ))
        test_compute = compute_override or str(self.settings().get("compute", "auto"))
        # RIFE inference itself verifies Vulkan device 0; vulkaninfo is only a diagnostic.
        chosen = "gpu" if test_compute == "auto" else test_compute
        probe = {"ok": False, "compute": chosen, "seconds": 0.0, "message": "not tested"}
        if exe.is_file() and (model / "flownet.param").is_file() and (model / "flownet.bin").is_file():
            probe_dir = self.work_dir / ("scan-" + uuid.uuid4().hex[:8])
            probe_dir.mkdir(parents=True, exist_ok=True)
            try:
                y, x = np.indices((256, 256))
                one = np.empty((256, 256, 3), np.uint8)
                one[:, :, 0] = (x % 256).astype(np.uint8)
                one[:, :, 1] = (y % 256).astype(np.uint8)
                one[:, :, 2] = ((x + y) % 256).astype(np.uint8)
                two = np.roll(one, 5, axis=1)
                a, b, out = probe_dir / "a.png", probe_dir / "b.png", probe_dir / "out.png"
                cv2.imwrite(str(a), one); cv2.imwrite(str(b), two)
                ok, note, elapsed = self._run_rife(
                    a, b, out, 0.5, chosen, model, timeout=25,
                    gpu_index=int(resource.get("gpu_index") or 0),
                )
                probe = {"ok": bool(ok), "compute": chosen, "seconds": round(elapsed, 3),
                         "message": "inference passed" if ok else str(note)[-750:]}
                if not ok and test_compute == "auto" and chosen == "gpu":
                    ok2, note2, elapsed2 = self._run_rife(
                        a, b, out, 0.5, "cpu", model, timeout=30, gpu_index=0,
                    )
                    probe = {"ok": bool(ok2), "compute": "cpu" if ok2 else "gpu", "seconds": round(elapsed + elapsed2, 3),
                             "message": "GPU probe failed; CPU inference passed" if ok2 else str(note2)[-750:],
                             "gpu_probe_error": str(note)[-500:]}
            finally:
                shutil.rmtree(probe_dir, ignore_errors=True)
        vulkan_ver = ""
        try:
            if vulkan_ok:
                vulkan_ver = "Vulkan available"
        except Exception:
            pass
        recommended_side = int(resource.get("recommended_max_side") or 512)
        recommended_jobs = str(resource.get("recommended_rife_jobs") or "1:1:1")
        result = {
            "ok": bool(probe.get("ok")), "scanned_at": time.time(),
            "cpu_count": int(os.cpu_count() or 1), "cpu_logical": int(psutil.cpu_count(logical=True) or 1),
            "ram_total_bytes": int(mem.total), "ram_available_bytes": int(mem.available),
            "ram_available_percent": round(float(mem.available) / max(1, int(mem.total)) * 100, 1),
            "disk_free_bytes": int(disk.free), "disk_total_bytes": int(disk.total),
            "vulkan_available": bool(vulkan_ok), "vulkan_devices": devices,
            "gpu_device": gpu, "gpu_index": int(resource.get("gpu_index") or 0),
            "vram_total_bytes": int(resource.get("vram_total_bytes") or 0),
            "vram_budget_bytes": int(resource.get("vram_budget_bytes") or 0),
            "vram_used_bytes": int(resource.get("vram_used_bytes") or 0),
            "vram_available_bytes": int(resource.get("vram_available_bytes") or 0),
            "vram_source": str(resource.get("vram_source") or "unavailable"),
            "vram_note": str(resource.get("vram_note") or ""),
            "vulkan_note": vulkan_note[-800:], "rife_available": bool(exe.is_file()),
            "model_available": bool((model / "flownet.bin").is_file() and (model / "flownet.param").is_file()),
            "ffmpeg_available": bool(ffmpeg and ffmpeg.is_file()), "probe": probe,
            "recommended_compute": probe.get("compute", chosen) if probe.get("ok") else "cpu",
            "recommended_max_side": recommended_side, "recommended_transition_frames": 8,
            "recommended_workers": 1, "recommended_rife_jobs": recommended_jobs,
            "generation_batch": 1,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "note": "Scans real GPU inference, Vulkan local-memory budget and system RAM. Keeps one pair job at a time; tunes RIFE's internal queues to avoid GPU memory exhaustion.",
        }
        self.last_scan, self.last_scan_at = result, time.monotonic()
        return result

    def environment_summary(self) -> dict[str, Any]:
        mem = psutil.virtual_memory(); disk = shutil.disk_usage(str(self.data))
        exe, model, ffmpeg = self._exe_paths()
        with self.lock:
            clip_count, seam_count = len(self.manifest["clips"]), len(self.manifest["seams"])
        try:
            profile = self._active_profile()
        except Exception:
            profile = {}
        total_cache = 0
        try:
            total_cache = sum(p.stat().st_size for p in self.cache_dir.glob("*.mp4") if p.is_file())
        except Exception:
            pass
        generating = self.generation_lock.locked()
        running = bool(profile.get("running"))
        camera_active = bool(profile.get("virtualcam_active"))
        flow_active = bool(profile.get("flow_active"))
        return {
            "ok": True, "clip_count": clip_count, "seam_count": seam_count,
            "settings": self.settings(), "job": self.current_job(),
            "environment": self.last_scan, "rife_available": exe.is_file(),
            "model_available": (model / "flownet.bin").is_file() and (model / "flownet.param").is_file(),
            "ffmpeg_available": bool(ffmpeg and ffmpeg.is_file()),
            "ram_available_bytes": int(mem.available), "disk_free_bytes": int(disk.free),
            "cache_bytes": int(total_cache), "clip_flow_active": flow_active,
            "engine_running": running, "virtualcam_active": camera_active,
            "generation_busy": generating, "generation_blocked": bool(flow_active or running or camera_active or generating),
        }

    def current_job(self) -> dict[str, Any] | None:
        with self.lock:
            active = [dict(j) for j in self.jobs.values() if j.get("status") == "running"]
            if active:
                return max(active, key=lambda j: j.get("started_at", 0))
            finished = list(self.jobs.values())
            return dict(max(finished, key=lambda j: j.get("updated_at", 0))) if finished else None

    def _pair_data(self, from_id: str, to_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        with self.lock:
            a = self._find_clip_locked(from_id); b = self._find_clip_locked(to_id)
            if not a or not b:
                raise KeyError("Select two clips that are present in the Seam library")
            if a["id"] == b["id"] or _path_key(a["path"]) == _path_key(b["path"]):
                raise ValueError("From and To must be two different clips")
            return dict(a), dict(b), dict(self.manifest["settings"])

    def _canvas_profile(self, a: dict[str, Any], settings: dict[str, Any],
                        profile_override: dict[str, Any] | None = None) -> tuple[int, int, str, int, float]:
        profile = profile_override if isinstance(profile_override, dict) else self._active_profile()
        out_w = _safe_int(profile.get("width"), 240, 4096, 720)
        out_h = _safe_int(profile.get("height"), 240, 4096, 1280)
        fit = str(profile.get("fit") or "cover").lower()
        fit = fit if fit in ("cover", "contain") else "cover"
        box_src = str(profile.get("box_source") or "")
        if profile.get("box_width") and profile.get("box_height"):
            box_w = _safe_int(profile.get("box_width"), 40, 4096, int(a.get("width") or out_w))
            box_h = _safe_int(profile.get("box_height"), 40, 4096, int(a.get("height") or out_h))
        else:
            box_w, box_h = int(a.get("width") or out_w), int(a.get("height") or out_h)
        if not (profile.get("box_width") and profile.get("box_height")) and box_src and Path(box_src).is_file():
            try:
                _, meta = _frame_from(Path(box_src), 0.0)
                scale = min(1.0, out_w / float(meta["width"]), out_h / float(meta["height"]))
                box_w, box_h = max(40, int(round(meta["width"] * scale))), max(40, int(round(meta["height"] * scale)))
            except Exception:
                pass
        if box_w <= 1 or box_h <= 1:
            box_w, box_h = int(a.get("width") or 720), int(a.get("height") or 1280)
        # Keep the same aspect ratio as the actual source box; RIFE internally
        # pads alignment as needed. Independent rounding to multiples of 32 warped
        # portrait output (for example, 405x720 becoming 416x704).
        side = _safe_int(settings.get("max_side"), 512, 960, 720)
        scale = min(1.0, side / float(max(box_w, box_h)))
        w, h = max(32, int(round(box_w * scale))), max(32, int(round(box_h * scale)))
        fps = _safe_int(profile.get("fps"), 24, 60, 30)
        return w, h, fit, fps, float(a.get("duration") or 0.0)

    def analyze_pair(self, from_id: str, to_id: str) -> dict[str, Any]:
        a, b, _stored_settings = self._pair_data(from_id, to_id)
        settings = self._effective_settings()
        for c in (a, b):
            p = Path(c["path"])
            if not p.is_file() or _file_signature(p) != c.get("signature"):
                raise ValueError(f"Clip changed or disappeared; remove and re-add it: {c['name']}")
        w, h, fit, fps, _ = self._canvas_profile(a, settings)
        transition_seconds = (int(settings["transition_frames"]) + 1) / max(1, fps)
        if float(b.get("duration") or 0.0) <= transition_seconds + 1.0 / max(1.0, float(b.get("fps") or 30.0)):
            raise ValueError("The destination clip is too short for this transition length")
        last, _ = _frame_from(Path(a["path"]), tail=True)
        next_frame, _ = _frame_from(Path(b["path"]), at_seconds=transition_seconds)
        aa, bb = _fit_frame(last, w, h, fit), _fit_frame(next_frame, w, h, fit)
        result = _similarity(aa, bb)
        result.update({"ok": True, "from_id": a["id"], "to_id": b["id"],
                       "from_name": a["name"], "to_name": b["name"],
                       "transition_frames": settings["transition_frames"], "fps": fps,
                       "transition_seconds": round(transition_seconds, 4),
                       "input_width": w, "input_height": h,
                       "model": "RIFE v4.6 ncnn/Vulkan", "compute": settings["compute"]})
        return result

    def start_generation(self, body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise ValueError("Generate request must be a JSON object")
        from_id, to_id = str(body.get("from_id") or ""), str(body.get("to_id") or "")
        force = bool(body.get("force", False))
        requested_compute = str(body.get("compute") or self.settings().get("compute") or "auto").lower()
        if requested_compute not in ("auto", "gpu", "cpu"):
            raise ValueError("compute must be auto, gpu or cpu")
        self._pair_data(from_id, to_id)
        guard = self.generation_guard if self.generation_guard is not None else nullcontext()
        with guard:
            profile = self._active_profile()
            if profile.get("flow_active") or profile.get("running") or profile.get("virtualcam_active"):
                raise RuntimeError("Stop the Engine and Virtual Camera before generating AI Seam frames. This protects the live frame rate.")
            if not self.generation_lock.acquire(blocking=False):
                raise RuntimeError("Another AI Seam job is already running; wait for it to finish")
        job_id = uuid.uuid4().hex
        job = {"id": job_id, "status": "running", "phase": "queued", "progress": 0,
               "message": "Queued", "error": None, "from_id": from_id, "to_id": to_id,
               "force": force, "requested_compute": requested_compute,
               "started_at": time.time(), "updated_at": time.time()}
        with self.lock:
            self.jobs[job_id] = job
        threading.Thread(target=self._generate_worker,
                         args=(job_id, from_id, to_id, force, requested_compute),
                         name="paokra-ai-seam-" + job_id[:8], daemon=True).start()
        return dict(job)

    def _job_update(self, job_id: str, **kwargs: Any) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            job.update(kwargs)
            job["updated_at"] = time.time()

    def _generate_worker(self, job_id: str, from_id: str, to_id: str, force: bool, requested_compute: str) -> None:
        job_dir = self.work_dir / job_id
        try:
            job_dir.mkdir(parents=True, exist_ok=True)
            pruned = self._prune_cache(MAX_CACHE_BYTES - 32 * 1024 ** 2)
            self._job_update(job_id, phase="validating", progress=3,
                             message=("Removed old generated cache to protect disk space" if pruned["files_deleted"] else "Checking clip metadata and available memory"))
            a, b, settings = self._pair_data(from_id, to_id)
            p_a, p_b = Path(a["path"]), Path(b["path"])
            if not p_a.is_file() or not p_b.is_file():
                raise ValueError("A source clip is missing")
            if _file_signature(p_a) != a.get("signature") or _file_signature(p_b) != b.get("signature"):
                raise ValueError("A source clip changed; remove and re-add it before generating")
            mem = psutil.virtual_memory()
            resources = self._resource_profile()
            # Never launch multiple RIFE processes for one pair; set a safe internal queue depth.
            if int(mem.available) < 1500 * 1024 ** 2:
                raise RuntimeError("Available RAM is below 1.5 GiB. Close other applications and run Scan again.")
            if shutil.disk_usage(str(self.data)).free < 512 * 1024 ** 2:
                raise RuntimeError("Less than 512 MiB disk space remains for temporary and cached frames")
            if settings.get("resolution_mode", "auto") == "auto":
                settings["max_side"] = int(resources.get("recommended_max_side") or 512)
            elif int(mem.available) < 4 * 1024 ** 3:
                settings["max_side"] = min(512, int(settings.get("max_side") or 512))
            rife_jobs = str(resources.get("recommended_rife_jobs") or "1:1:1")
            w, h, fit, fps, _ = self._canvas_profile(a, settings)
            # PNG image sequences require one stable, even-sized canvas.
            w = max(32, int(w) & ~1)
            h = max(32, int(h) & ~1)
            frames_count = _safe_int(settings.get("transition_frames"), 4, 12, 8)
            # Generate only INTERIOR frames. Including A-last and B-resume as
            # first/last bridge frames duplicated those timestamps at the source
            # handoff and made the live feed briefly freeze at both ends.
            resume = (frames_count + 1) / float(fps)
            if float(b.get("duration") or 0.0) <= resume + 1.0 / max(1.0, float(b.get("fps") or 30.0)):
                raise ValueError("Destination clip is too short to make this transition safely")
            self._job_update(job_id, phase="analyzing", progress=8, message="Comparing the last frame of A with the matching-time frame of B")
            last, _ = _frame_from(p_a, tail=True)
            first_b, _ = _frame_from(p_b, at_seconds=resume)
            last = _fit_frame(last, w, h, fit)
            first_b = _fit_frame(first_b, w, h, fit)
            similarity = _similarity(last, first_b)
            threshold = float(settings.get("similarity_threshold", 0.40))
            if similarity["score"] < threshold and not force:
                raise ValueError(f"This pair is visually risky (score {similarity['score']:.2f} < {threshold:.2f}). Enable 'Generate anyway' only after reviewing the pair.")
            # RIFE is most stable with small, even dimensions on low-VRAM GPUs.
            # Keep batch/thread counts fixed at 1 rather than exhausting the RX 570.
            a_png, b_png = job_dir / "from.png", job_dir / "to.png"
            if not cv2.imwrite(str(a_png), last) or not cv2.imwrite(str(b_png), first_b):
                raise RuntimeError("Could not prepare the input frames")
            exe, model, ffmpeg = self._exe_paths()
            if not exe.is_file() or not (model / "flownet.param").is_file() or not (model / "flownet.bin").is_file():
                raise RuntimeError("RIFE v4.6 executable/model files are missing")
            if not ffmpeg or not ffmpeg.is_file():
                raise RuntimeError("Bundled FFmpeg is unavailable; cannot package the generated frames as a clip")
            requested = requested_compute
            # Auto tries Vulkan first and retries the entire bridge on CPU on failure.
            compute = "gpu" if requested == "auto" else requested
            if settings.get("resolution_mode", "auto") == "auto":
                settings["max_side"] = int(resources.get("recommended_max_side") or 512)
            generated = job_dir / "frames"
            generated.mkdir(exist_ok=True)
            elapsed_rife = 0.0
            actual_compute = compute
            errors: list[str] = []
            # RIFE rejects timestep 1.0. Render only interior points between
            # A's final displayed frame and B's resume frame. The exact endpoints
            # remain in the source clips; repeating them inside the bridge caused
            # an extra held frame at each clip handoff.
            fractions = [i / float(frames_count + 1) for i in range(1, frames_count + 1)]
            def run_all(device: str) -> tuple[bool, float, str]:
                total = 0.0
                note = ""
                for idx, fraction in enumerate(fractions, 1):
                    out = generated / f"{idx:08d}.png"
                    try:
                        out.unlink(missing_ok=True)
                    except Exception:
                        pass
                    ok, msg, duration = self._run_rife(
                        a_png, b_png, out, fraction, device, model, timeout=45,
                        jobs=(rife_jobs if device == "gpu" else "1:1:1"),
                        gpu_index=int(resources.get("gpu_index") or 0),
                    )
                    total += duration
                    if not ok:
                        return False, total, f"frame {idx}/{frames_count}: {msg}"
                    # Some RIFE builds can emit padded outputs. Normalize every
                    # intermediate frame before giving the sequence to FFmpeg.
                    image = cv2.imread(str(out), cv2.IMREAD_COLOR)
                    if image is None or image.size == 0:
                        return False, total, f"frame {idx}/{frames_count}: RIFE output is not a readable image"
                    if image.shape[:2] != (h, w):
                        image = cv2.resize(image, (w, h), interpolation=cv2.INTER_LINEAR)
                        if not cv2.imwrite(str(out), image):
                            return False, total, f"frame {idx}/{frames_count}: could not normalize frame size"
                        image = cv2.imread(str(out), cv2.IMREAD_COLOR)
                    if image is None or image.shape[:2] != (h, w):
                        got = None if image is None else (image.shape[1], image.shape[0])
                        return False, total, f"frame {idx}/{frames_count}: expected {w}x{h}, got {got}"
                    self._job_update(
                        job_id, phase="inference",
                        progress=12 + int(66 * idx / frames_count),
                        message=f"Generating interior bridge frame {idx}/{frames_count} on "
                                f"{'Vulkan GPU' if device == 'gpu' else 'CPU'} | "
                                f"RIFE jobs {(rife_jobs if device == 'gpu' else '1:1:1')}",
                    )
                    note = msg
                self._job_update(
                    job_id, phase="inference", progress=82,
                    message="Interior bridge frames verified; exact source endpoints remain in A and B",
                )
                return True, total, note
            ok, elapsed_rife, note = run_all(compute)
            if not ok and requested_compute == "auto" and compute == "gpu":
                for file in generated.glob("*.png"):
                    file.unlink(missing_ok=True)
                self._job_update(job_id, phase="fallback", progress=12, message="GPU inference failed; retrying the whole job on CPU")
                ok, cpu_elapsed, cpu_note = run_all("cpu")
                elapsed_rife += cpu_elapsed
                actual_compute = "cpu"
                if not ok:
                    raise RuntimeError(f"GPU failed ({note}); CPU fallback failed ({cpu_note})")
            elif not ok:
                raise RuntimeError(note)
            self._job_update(job_id, phase="encoding", progress=86, message="Validating bridge frames and encoding the transition")
            cache_id = uuid.uuid4().hex
            asset = self.cache_dir / (cache_id + ".mp4")
            # Don't expose a partial output to playback/cache lookup.
            asset_tmp = self.cache_dir / (cache_id + ".part.mp4")
            # Normalize every RIFE PNG before FFmpeg sees the sequence. This avoids
            # implicit auto_scale failures from inconsistent dimensions or alpha formats.
            encode_w, encode_h = max(2, w - (w % 2)), max(2, h - (h % 2))
            for frame_index in range(1, frames_count + 1):
                frame_path = generated / f"{frame_index:08d}.png"
                if not frame_path.is_file():
                    raise RuntimeError(f"RIFE output is missing frame {frame_index}/{frames_count}: {frame_path.name}")
                image = cv2.imread(str(frame_path), cv2.IMREAD_UNCHANGED)
                if image is None or image.size == 0:
                    raise RuntimeError(f"RIFE output frame {frame_index}/{frames_count} is unreadable: {frame_path.name}")
                original_shape = image.shape
                if image.ndim == 2:
                    image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
                elif image.ndim == 3 and image.shape[2] == 4:
                    image = cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
                elif image.ndim != 3 or image.shape[2] != 3:
                    raise RuntimeError(f"RIFE output frame {frame_index}/{frames_count} has unsupported shape {image.shape}")
                if image.shape[1] != encode_w or image.shape[0] != encode_h:
                    image = cv2.resize(image, (encode_w, encode_h), interpolation=cv2.INTER_LINEAR)
                if image.shape != original_shape:
                    if not cv2.imwrite(str(frame_path), image):
                        raise RuntimeError(f"Could not normalize RIFE output frame {frame_index}/{frames_count}")
            frame_pattern = str(generated / "%08d.png")
            # Keep a partial asset until the entire clip passes a decoder integrity check.
            temporary_asset = self.cache_dir / (cache_id + ".partial.mp4")
            cmd = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-filter_threads", "1",
                   "-framerate", str(fps), "-start_number", "1", "-i", frame_pattern,
                   "-frames:v", str(frames_count), "-map", "0:v:0",
                   "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2:flags=bilinear,setsar=1,format=yuv420p",
                   "-an", "-c:v", "libx264", "-preset", "ultrafast", "-threads:v", "1",
                   "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                   "-video_track_timescale", "90000", str(temporary_asset)]
            try:
                try:
                    enc = subprocess.run(cmd, capture_output=True, text=True, timeout=90,
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                except subprocess.TimeoutExpired:
                    raise RuntimeError(f"FFmpeg encoding timed out (frames={frames_count}, size={encode_w}x{encode_h}, fps={fps})")
                if enc.returncode != 0 or not temporary_asset.is_file() or temporary_asset.stat().st_size < 512:
                    details = (enc.stderr or enc.stdout or "unknown error")[-1600:]
                    raise RuntimeError(f"FFmpeg failed to encode transition (frames={frames_count}, size={encode_w}x{encode_h}, fps={fps}, rc={enc.returncode}): {details}")
                probe = cv2.VideoCapture(str(temporary_asset))
                try:
                    if not probe.isOpened():
                        raise RuntimeError("FFmpeg produced an MP4 that cannot be reopened")
                    ok_frame, verification_frame = probe.read()
                    out_w = int(probe.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
                    out_h = int(probe.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
                    out_n = int(probe.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                    out_fps = float(probe.get(cv2.CAP_PROP_FPS) or 0.0)
                    if (not ok_frame or verification_frame is None or out_n != frames_count or
                            out_w != encode_w or out_h != encode_h or abs(out_fps - fps) > 0.2):
                        raise RuntimeError(f"Generated MP4 verification failed (frames={out_n}/{frames_count}, size={out_w}x{out_h}/{encode_w}x{encode_h}, fps={out_fps:.3f}/{fps})")
                finally:
                    probe.release()
                os.replace(temporary_asset, asset)
            except Exception:
                try:
                    temporary_asset.unlink(missing_ok=True)
                except Exception:
                    pass
                raise
            self._prune_cache(max(0, MAX_CACHE_BYTES - int(asset.stat().st_size)), preserve_paths=(asset,))
            record = {
                "id": cache_id, "from_id": a["id"], "to_id": b["id"],
                "from_name": a["name"], "to_name": b["name"],
                "from_path": str(p_a), "to_path": str(p_b),
                "from_signature": _file_signature(p_a), "to_signature": _file_signature(p_b),
                "asset": str(asset), "resume_seconds": round(resume, 6),
                "to_duration": float(b.get("duration") or 0.0), "fps": fps,
                "to_endpoint_seconds": 0.0, "frame_count": frames_count, "width": out_w, "height": out_h,
                "max_side": int(settings.get("max_side") or 512),
                "bridge_algorithm": "interior-only-v2",
                "target_aspect": round(float(w) / max(1.0, float(h)), 7), "fit": fit,
                "score": similarity["score"], "risk": similarity["risk"],
                "compute": actual_compute, "rife_jobs": (rife_jobs if actual_compute == "gpu" else "1:1:1"),
                "resolution_mode": str(self.settings().get("resolution_mode") or "auto"),
                "inference_seconds": round(elapsed_rife, 3),
                "size": asset.stat().st_size, "created_at": time.time(),
            }
            # Commit the newly verified bridge before removing a superseded
            # bridge for this exact directed pair. If persistence fails, the old
            # asset remains available; unrelated pairs and orphaned complete assets
            # are never deleted as a side effect of generation.
            with self.lock:
                superseded = [
                    dict(s) for s in self.manifest["seams"]
                    if s.get("from_id") == a["id"] and s.get("to_id") == b["id"]
                ]
                self.manifest["seams"] = [
                    s for s in self.manifest["seams"]
                    if not (s.get("from_id") == a["id"] and s.get("to_id") == b["id"])
                ]
                self.manifest["seams"].append(record)
                self.save()
            current_asset_key = _path_key(asset)
            for old_record in superseded:
                old_asset = self._safe_cache_asset(old_record.get("asset"))
                if old_asset is None or _path_key(old_asset) == current_asset_key:
                    continue
                try:
                    old_asset.unlink(missing_ok=True)
                except OSError:
                    # Keep any file Windows still has open; it is no longer a
                    # playback entry and can be removed by an explicit Clear Cache.
                    pass
            self._job_update(job_id, status="complete", phase="complete", progress=100,
                             message="Transition generated and verified", error=None, result=record,
                             compute_used=actual_compute, inference_seconds=round(elapsed_rife, 3),
                             similarity=similarity)
        except Exception as exc:
            # Remove only this job's incomplete output. Original clips and verified
            # cache records must remain untouched after a failed generation.
            try:
                if "asset_tmp" in locals() and isinstance(asset_tmp, Path):
                    asset_tmp.unlink(missing_ok=True)
            except Exception:
                pass
            self._job_update(job_id, status="error", phase="error", progress=100,
                             message="Generation failed safely; the original clips are unchanged",
                             error=f"{type(exc).__name__}: {str(exc)[:1600]}")
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)
            self.generation_lock.release()

    def find_transition(self, from_path: str | Path, to_path: str | Path,
                        runtime_profile: dict[str, Any] | None = None) -> dict[str, Any] | None:
        with self.lock:
            if not bool(self.manifest["settings"].get("enabled")):
                return None
            seams = [dict(s) for s in self.manifest["seams"]]
        ka, kb = _path_key(from_path), _path_key(to_path)
        for seam in reversed(seams):
            # Direction is part of the key: A -> B is never accepted as B -> A.
            if _path_key(seam.get("from_path", "")) != ka or _path_key(seam.get("to_path", "")) != kb:
                continue
            try:
                pa, pb = Path(seam["from_path"]), Path(seam["to_path"])
                asset = self._safe_cache_asset(seam.get("asset"))
                if asset is None or not pa.is_file() or not pb.is_file() or not asset.is_file():
                    continue
                if _file_signature(pa) != seam.get("from_signature") or _file_signature(pb) != seam.get("to_signature"):
                    continue
                current_settings = self._effective_settings()
                if int(seam.get("frame_count") or 0) != int(current_settings.get("transition_frames") or 8):
                    continue
                # Adaptive profiles vary by PC. A verified bridge remains usable
                # across Auto-profile changes: only an explicit/manual input size
                # change requires regeneration. Never invalidate a good cache just
                # because another PC reports a different amount of VRAM.
                if (str(seam.get("resolution_mode") or "manual") != "auto"
                        and int(seam.get("max_side") or 0) != int(current_settings.get("max_side") or 0)):
                    continue
                # Version 2 uses interior-only interpolation; the earlier algorithm
                # repeated A/B endpoints and could cause a held frame at handoff.
                if seam.get("bridge_algorithm") != "interior-only-v2":
                    continue
                if runtime_profile is not None:
                    cw, ch, current_fit, current_fps, _duration = self._canvas_profile(
                        {"width": int(runtime_profile.get("box_width") or runtime_profile.get("width") or seam.get("width") or 720),
                         "height": int(runtime_profile.get("box_height") or runtime_profile.get("height") or seam.get("height") or 1280),
                         "duration": 0.0}, current_settings, profile_override=runtime_profile)
                    if int(seam.get("fps") or 0) != int(current_fps):
                        continue
                    if str(seam.get("fit") or "cover") != str(current_fit or "cover"):
                        continue
                    # Resolution may scale between equal-aspect scene boxes without
                    # distorting the generated bridge. Aspect, FPS, fit, and model
                    # limit matter; exact pixel width/height does not.
                    target_aspect = float(seam.get("target_aspect") or (float(seam.get("width") or 1) / max(1.0, float(seam.get("height") or 1))))
                    if abs((float(cw) / max(1.0, float(ch))) - target_aspect) > 0.02:
                        continue
                return seam
            except Exception:
                continue
        return None

    def list_seams(self) -> list[dict[str, Any]]:
        with self.lock:
            seams = [dict(s) for s in reversed(self.manifest["seams"])]
        # Availability means the asset/source signatures are intact. Compatibility
        # separately confirms the current FPS, scene canvas/fit and saved AI settings.
        profile = {}
        try:
            profile = self._active_profile()
        except Exception:
            pass
        current_settings = self._effective_settings()
        for seam in seams:
            available = False
            compatible = False
            reason = "cache missing or source changed"
            try:
                pa = Path(str(seam.get("from_path") or ""))
                pb = Path(str(seam.get("to_path") or ""))
                asset = self._safe_cache_asset(seam.get("asset"))
                resume = float(seam.get("resume_seconds") or 0)
                duration = float(seam.get("to_duration") or 0)
                available = bool(
                    asset is not None and asset.is_file() and asset.stat().st_size >= 512
                    and pa.is_file() and pb.is_file()
                    and _file_signature(pa) == seam.get("from_signature")
                    and _file_signature(pb) == seam.get("to_signature")
                    and resume > 0 and duration > resume
                )
                if not available:
                    reason = "cache missing or source changed"
                elif int(seam.get("frame_count") or 0) != int(current_settings.get("transition_frames") or 8):
                    reason = "bridge frame count differs from AI Settings"
                elif (str(seam.get("resolution_mode") or "manual") != "auto"
                      and int(seam.get("max_side") or 0) != int(current_settings.get("max_side") or 0)):
                    reason = "Manual AI input resolution changed; regenerate this seam"
                elif seam.get("bridge_algorithm") != "interior-only-v2":
                    reason = "Older bridge contains repeated source endpoints; regenerate once for smoother handoff"
                elif not profile:
                    reason = "current output profile is unavailable"
                else:
                    cw, ch, fit, fps, _duration = self._canvas_profile(
                        {"width": int(profile.get("box_width") or profile.get("width") or seam.get("width") or 720),
                         "height": int(profile.get("box_height") or profile.get("height") or seam.get("height") or 1280),
                         "duration": 0.0}, current_settings, profile_override=profile)
                    target_aspect = float(seam.get("target_aspect") or (float(seam.get("width") or 1) / max(1.0, float(seam.get("height") or 1))))
                    compatible = bool(
                        int(seam.get("fps") or 0) == int(fps)
                        and str(seam.get("fit") or "cover") == str(fit or "cover")
                        and abs((float(cw) / max(1.0, float(ch))) - target_aspect) <= 0.02
                    )
                    reason = "compatible with current output" if compatible else "FPS/output aspect/fit differs from current settings"
            except (OSError, TypeError, ValueError, OverflowError):
                available, compatible = False, False
                reason = "cache metadata could not be verified"
            seam["available"] = available
            seam["compatible"] = bool(available and compatible)
            seam["compatibility_reason"] = reason
        return seams

    def build_sequence_preview(self, seam_id: str) -> Path:
        """Build a short real A-tail -> generated seam -> B-resume preview for review."""
        seam = next((x for x in self.list_seams() if str(x.get("id") or "") == str(seam_id)), None)
        if not seam or not seam.get("available"):
            raise KeyError("verified seam preview not found")
        if not seam.get("compatible"):
            raise ValueError("This cached seam does not match the current Output/AI Settings; regenerate it first")
        source_a = Path(str(seam.get("from_path") or ""))
        source_b = Path(str(seam.get("to_path") or ""))
        asset = self._safe_cache_asset(seam.get("asset"))
        if asset is None or not source_a.is_file() or not source_b.is_file():
            raise ValueError("A source clip or generated bridge is missing")
        with self.lock:
            clip_a = next((dict(c) for c in self.manifest["clips"] if str(c.get("id")) == str(seam.get("from_id"))), None)
            clip_b = next((dict(c) for c in self.manifest["clips"] if str(c.get("id")) == str(seam.get("to_id"))), None)
        if not clip_a or not clip_b:
            raise ValueError("Re-add the source clips to the AI Seam library before previewing")
        if _file_signature(source_a) != seam.get("from_signature") or _file_signature(source_b) != seam.get("to_signature"):
            raise ValueError("Source clips changed since this seam was generated")
        exe, _model, ffmpeg = self._exe_paths()
        if not ffmpeg or not ffmpeg.is_file():
            raise RuntimeError("Bundled FFmpeg is unavailable")
        width = max(32, int(seam.get("width") or 512))
        height = max(32, int(seam.get("height") or 288))
        fps = max(1, int(seam.get("fps") or 30))
        fit = str(seam.get("fit") or "cover")
        resume = max(0.0, float(seam.get("resume_seconds") or 0.0))
        a_duration = max(0.0, float(clip_a.get("duration") or 0.0))
        b_duration = max(0.0, float(clip_b.get("duration") or 0.0))
        bridge_seconds = max(0.1, float(seam.get("frame_count") or 8) / float(fps))
        pre_seconds = min(1.25, a_duration)
        post_seconds = min(1.25, max(0.0, b_duration - resume))
        if pre_seconds < 0.15 or post_seconds < 0.15:
            raise ValueError("Source/destination clips are too short for a useful sequence preview")
        key_material = json.dumps({
            "seam": seam.get("id"), "from": seam.get("from_signature"),
            "to": seam.get("to_signature"), "asset_mtime": asset.stat().st_mtime_ns,
            "size": [width, height], "fps": fps, "pre": round(pre_seconds, 3),
            "post": round(post_seconds, 3), "resume": round(resume, 4),
            "frame_count": int(seam.get("frame_count") or 8), "fit": fit
        }, sort_keys=True).encode("utf-8")
        key = hashlib.sha256(key_material).hexdigest()[:32]
        self.preview_dir.mkdir(parents=True, exist_ok=True)
        preview_path = self.preview_dir / (key + ".mp4")
        if preview_path.is_file() and preview_path.stat().st_size >= 1024:
            try:
                if _file_signature(source_a) == seam.get("from_signature") and _file_signature(source_b) == seam.get("to_signature"):
                    return preview_path
            except Exception:
                pass
        temp_path = self.preview_dir / (key + ".partial.mp4")
        start_a = max(0.0, a_duration - pre_seconds)
        scale_flags = "lanczos"
        if fit == "contain":
            fit_filter = f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags={scale_flags},pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"
        else:
            fit_filter = f"scale={width}:{height}:force_original_aspect_ratio=increase:flags={scale_flags},crop={width}:{height}"
        common = f"{fit_filter},fps={fps},setsar=1,format=yuv420p,setpts=PTS-STARTPTS"
        filt = (
            f"[0:v]trim=duration={pre_seconds:.6f},{common}[a];"
            f"[1:v]trim=duration={bridge_seconds:.6f},{common}[s];"
            f"[2:v]trim=duration={post_seconds:.6f},{common}[b];"
            f"[a][s][b]concat=n=3:v=1:a=0[outv]"
        )
        cmd = [
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-filter_threads", "1",
            "-ss", f"{start_a:.6f}", "-t", f"{pre_seconds:.6f}", "-i", str(source_a),
            "-i", str(asset),
            "-ss", f"{resume:.6f}", "-t", f"{post_seconds:.6f}", "-i", str(source_b),
            "-filter_complex", filt, "-map", "[outv]", "-an",
            "-c:v", "libx264", "-preset", "ultrafast", "-threads:v", "1",
            "-crf", "24", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-video_track_timescale", "90000", str(temp_path)
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if proc.returncode != 0 or not temp_path.is_file() or temp_path.stat().st_size < 1024:
                details = (proc.stderr or proc.stdout or "unknown error")[-1200:]
                raise RuntimeError("Could not assemble A → seam → B preview: " + details)
            cap = cv2.VideoCapture(str(temp_path))
            try:
                ok, frame = cap.read()
                got_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
                got_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
                got_n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                got_fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
                if not ok or frame is None or got_w != width or got_h != height or got_n < int((pre_seconds + bridge_seconds + post_seconds) * fps * 0.75) or abs(got_fps - fps) > 0.25:
                    raise RuntimeError(f"Sequence preview verification failed ({got_w}x{got_h}, {got_n} frames, {got_fps:.2f} FPS)")
            finally:
                cap.release()
            os.replace(temp_path, preview_path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
        # Keep only a bounded number of composed previews; they are review artifacts,
        # not source media or playback cache entries.
        previews = sorted((x for x in self.preview_dir.glob("*.mp4") if x.is_file()),
                          key=lambda x: x.stat().st_mtime, reverse=True)
        for old in previews[16:]:
            try:
                old.unlink(missing_ok=True)
            except Exception:
                pass
        return preview_path

    def _prune_cache(self, max_bytes: int = MAX_CACHE_BYTES,
                     preserve_paths: tuple[str | Path, ...] = ()) -> dict[str, int]:
        """Clean abandoned partial encodes only; never evict a generated bridge automatically.

        Directed transitions are expensive inference products. Deleting the oldest
        complete MP4 here made Auto Make appear to lose frames when a playlist's
        transition matrix exceeded MAX_CACHE_BYTES. The quota is informational now:
        only the explicit Clear Cache action may remove finished bridge assets.
        Unregistered complete MP4s are preserved too, as they may be the last result
        of a process interrupted between atomic rename and manifest persistence.
        """
        del max_bytes  # Retained in the signature for compatibility with older callers.
        deleted_files = 0
        deleted_bytes = 0
        cutoff = time.time() - 24 * 60 * 60
        # A partial/part asset older than 24 hours cannot belong to a live job because
        # generation is serialized. Keep fresh partials so a future concurrent build
        # or a process restart cannot race this cleanup.
        for pattern in ("*.part.mp4", "*.partial.mp4"):
            for path in self.cache_dir.glob(pattern):
                try:
                    if not path.is_file() or path.stat().st_mtime > cutoff:
                        continue
                    size = int(path.stat().st_size)
                    path.unlink()
                    deleted_files += 1
                    deleted_bytes += size
                except OSError:
                    continue
        finished = [p for p in self.cache_dir.glob("*.mp4")
                    if p.is_file() and not p.name.endswith((".part.mp4", ".partial.mp4"))]
        total = sum(int(p.stat().st_size) for p in finished)
        return {
            "files_deleted": deleted_files,
            "bytes_deleted": deleted_bytes,
            "remaining_bytes": max(0, total),
            "quota_exceeded": total > MAX_CACHE_BYTES,
            "quota_bytes": MAX_CACHE_BYTES,
        }

    def clear_cache(self) -> dict[str, Any]:
        guard = self.generation_guard if self.generation_guard is not None else nullcontext()
        with guard:
            profile = self._active_profile()
            if profile.get("flow_active") or profile.get("running") or profile.get("virtualcam_active"):
                raise RuntimeError("Stop Engine, Clip Flow and Virtual Camera before clearing generated transitions")
            if self.generation_lock.locked():
                raise RuntimeError("Wait for the current AI Seam compute operation before clearing the generated cache")
            with self.lock:
                seams = [dict(x) for x in self.manifest["seams"]]
            candidates: dict[str, Path] = {}
            for seam in seams:
                asset = self._safe_cache_asset(seam.get("asset"))
                if asset is not None:
                    candidates[str(asset)] = asset
            for asset in self.cache_dir.glob("*.mp4"):
                if asset.is_file() and asset.suffix.lower() == ".mp4":
                    candidates[str(asset.resolve())] = asset.resolve()
            # The composite preview cache contains only re-encoded review snippets.
            preview_files = [p for p in self.preview_dir.glob("*") if p.is_file()]
            # The native decoder can keep a file open even after Engine Stop.
            # Ask the host to switch away from a generated seam before unlinking it.
            if self.release_cache_sources and candidates:
                release_records = list(seams)
                known = {_path_key(x.get("asset", "")) for x in seams if x.get("asset")}
                release_records.extend({"asset": str(asset), "from_path": "", "to_path": ""}
                                       for asset in candidates.values() if _path_key(asset) not in known)
                try:
                    released = bool(self.release_cache_sources(release_records))
                except Exception:
                    released = False
                if not released:
                    return {"ok": False, "removed": 0, "files_deleted": 0,
                            "files_remaining": len(candidates),
                            "error": "Could not release the active decoder's generated source. Select an original clip, stop preview playback, then retry."}
            deleted_keys: set[str] = set()
            deleted = 0
            for asset in candidates.values():
                key = str(asset.resolve())
                try:
                    existed = asset.is_file()
                    asset.unlink(missing_ok=True)
                    if existed and not asset.exists():
                        deleted += 1
                        deleted_keys.add(key)
                except OSError:
                    # Keep the manifest entry if Windows still has the file open.
                    continue
                except Exception:
                    continue
            remaining_paths = {str(path.resolve()) for path in candidates.values() if path.exists()}
            with self.lock:
                remaining_seams = []
                removed_records = 0
                for seam in self.manifest["seams"]:
                    asset = self._safe_cache_asset(seam.get("asset"))
                    if asset is None or str(asset.resolve()) not in remaining_paths:
                        removed_records += 1
                    else:
                        remaining_seams.append(seam)
                self.manifest["seams"] = remaining_seams
                self.save()
            preview_deleted = 0
            for preview in preview_files:
                try:
                    preview.unlink(missing_ok=True)
                    preview_deleted += 1
                except Exception:
                    pass
            return {"ok": not remaining_paths, "removed": removed_records,
                    "files_deleted": deleted, "preview_files_deleted": preview_deleted,
                    "files_remaining": len(remaining_paths),
                    "error": ("Some generated files are still in use; the manifest entries were preserved. Switch away from the generated source and retry." if remaining_paths else None)}



def create_blueprint(root: str | Path, data: str | Path,
                     output_profile: Callable[[], dict[str, Any]] | None = None,
                     generation_guard: Any = None,
                     release_cache_sources: Callable[[list[dict[str, Any]]], bool] | None = None) -> Blueprint:
    key = str(Path(data).resolve())
    with _SERVICES_LOCK:
        service = _SERVICES.get(key)
        if service is None:
            service = AiSeamService(Path(root), Path(data), output_profile,
                                    generation_guard=generation_guard,
                                    release_cache_sources=release_cache_sources)
            _SERVICES[key] = service
        else:
            service.root = Path(root).resolve()
            if output_profile:
                service.output_profile = output_profile
            if generation_guard is not None:
                service.generation_guard = generation_guard
            if release_cache_sources is not None:
                service.release_cache_sources = release_cache_sources
    bp = Blueprint("ai_seam", __name__, url_prefix="/api/ai-seam")

    @bp.get("/status")
    def status():
        try:
            return jsonify(service.environment_summary())
        except Exception as exc:
            return jsonify(ok=False, error=f"{type(exc).__name__}: {str(exc)[:250]}"), 500

    @bp.get("/clips")
    def clips_get():
        return jsonify(ok=True, clips=service.clips(), count=len(service.clips()))

    @bp.post("/clips")
    def clips_add():
        body = request.get_json(silent=True) or {}
        try:
            result = service.add_paths(body.get("paths"))
            return jsonify(**result)
        except RuntimeError as exc:
            return jsonify(ok=False, error=str(exc)[:500]), 409
        except Exception as exc:
            return jsonify(ok=False, error=str(exc)[:500]), 400

    @bp.post("/clips/reorder")
    def clips_reorder():
        body = request.get_json(silent=True) or {}
        try:
            updated = service.reorder(body.get("ids"))
            return jsonify(ok=True, clips=updated, count=len(updated))
        except RuntimeError as exc:
            return jsonify(ok=False, error=str(exc)[:500]), 409
        except Exception as exc:
            return jsonify(ok=False, error=str(exc)[:500]), 400

    @bp.delete("/clips/<clip_id>")
    def clips_delete(clip_id: str):
        try:
            return jsonify(**service.remove_clip(clip_id))
        except KeyError as exc:
            return jsonify(ok=False, error=str(exc)), 404
        except RuntimeError as exc:
            return jsonify(ok=False, error=str(exc)), 409
        except Exception as exc:
            return jsonify(ok=False, error=f"{type(exc).__name__}: {str(exc)[:250]}"), 500

    @bp.get("/clips/<clip_id>/frame")
    def clip_frame(clip_id: str):
        try:
            clip = service.get_clip(clip_id)
            path = Path(clip["path"])
            if not path.is_file():
                return jsonify(ok=False, error="The original clip is no longer available"), 404
            at = request.args.get("at", "0")
            frame, _meta = _frame_from(path, tail=True) if str(at).lower() == "tail" else _frame_from(path, at_seconds=float(at))
            width, height, fit, _fps, _duration = service._canvas_profile(clip, service.settings())
            frame = _fit_frame(frame, width, height, fit)
            h, w = frame.shape[:2]
            scale = min(1.0, 640.0 / max(1, w, h))
            if scale < 1.0:
                frame = cv2.resize(frame, (max(1, round(w * scale)), max(1, round(h * scale))), interpolation=cv2.INTER_AREA)
            ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 78])
            if not ok:
                raise ValueError("Could not encode a preview frame")
            return Response(encoded.tobytes(), mimetype="image/jpeg", headers={"Cache-Control": "no-store"})
        except KeyError:
            return jsonify(ok=False, error="clip not found"), 404
        except Exception as exc:
            return jsonify(ok=False, error=f"frame preview failed: {type(exc).__name__}: {str(exc)[:180]}"), 400

    @bp.get("/clips/<clip_id>/video")
    def clip_video(clip_id: str):
        try:
            clip = service.get_clip(clip_id)
            path = Path(clip["path"])
            if not path.is_file():
                return jsonify(ok=False, error="The original clip is no longer available"), 404
            return send_file(str(path), mimetype="video/mp4" if path.suffix.lower() == ".mp4" else None,
                             conditional=True, as_attachment=False, download_name=path.name)
        except KeyError:
            return jsonify(ok=False, error="clip not found"), 404

    @bp.post("/settings")
    def settings_post():
        body = request.get_json(silent=True) or {}
        try:
            return jsonify(ok=True, settings=service.update_settings(body))
        except Exception as exc:
            return jsonify(ok=False, error=str(exc)[:400]), 400

    @bp.post("/environment/scan")
    def environment_scan():
        body = request.get_json(silent=True) or {}
        compute = str(body.get("compute") or service.settings().get("compute") or "auto").lower()
        if compute not in ("auto", "gpu", "cpu"):
            return jsonify(ok=False, error="compute must be auto, gpu or cpu"), 400
        try:
            result = service.scan_environment(compute)
            return jsonify(result), 200
        except Exception as exc:
            return jsonify(ok=False, error=f"environment scan failed: {type(exc).__name__}: {str(exc)[:300]}"), 500

    @bp.post("/analyze")
    def analyze():
        body = request.get_json(silent=True) or {}
        try:
            return jsonify(**service.analyze_pair(str(body.get("from_id") or ""), str(body.get("to_id") or "")))
        except KeyError as exc:
            return jsonify(ok=False, error=str(exc)), 404
        except Exception as exc:
            return jsonify(ok=False, error=f"{type(exc).__name__}: {str(exc)[:500]}"), 400

    @bp.post("/generate")
    def generate():
        try:
            job = service.start_generation(request.get_json(silent=True) or {})
            return jsonify(ok=True, job=job), 202
        except KeyError as exc:
            return jsonify(ok=False, error=str(exc)), 404
        except RuntimeError as exc:
            return jsonify(ok=False, error=str(exc)), 409
        except Exception as exc:
            return jsonify(ok=False, error=f"{type(exc).__name__}: {str(exc)[:400]}"), 400

    @bp.get("/jobs/<job_id>")
    def job_get(job_id: str):
        with service.lock:
            job = service.jobs.get(job_id)
            if not job:
                return jsonify(ok=False, error="job not found"), 404
            return jsonify(ok=True, job=dict(job))

    @bp.get("/seams")
    def seams_get():
        seams = service.list_seams()
        return jsonify(ok=True, seams=seams, count=len(seams))

    @bp.get("/seams/<seam_id>/video")
    def seam_video(seam_id: str):
        for seam in service.list_seams():
            if seam.get("id") == seam_id:
                path = service._safe_cache_asset(seam.get("asset"))
                if path is not None and path.is_file():
                    return send_file(str(path), mimetype="video/mp4", conditional=True, as_attachment=False,
                                     download_name=path.name)
                break
        return jsonify(ok=False, error="seam preview not found"), 404

    @bp.get("/seams/<seam_id>/preview")
    def seam_sequence_preview(seam_id: str):
        try:
            path = service.build_sequence_preview(seam_id)
            response = send_file(str(path), mimetype="video/mp4", conditional=True,
                                 as_attachment=False, download_name=path.name, max_age=60)
            response.headers["Cache-Control"] = "private, max-age=60"
            return response
        except KeyError:
            return jsonify(ok=False, error="verified seam preview not found"), 404
        except Exception as exc:
            return jsonify(ok=False, error=f"{type(exc).__name__}: {str(exc)[:700]}"), 400

    @bp.delete("/cache")
    def cache_clear():
        try:
            result = service.clear_cache()
            return jsonify(**result), (200 if result.get("ok") else 409)
        except RuntimeError as exc:
            return jsonify(ok=False, error=str(exc)), 409
        except Exception as exc:
            return jsonify(ok=False, error=f"cache clear failed: {type(exc).__name__}: {str(exc)[:250]}"), 500

    return bp


def find_cached_transition(data_root: str | Path, from_path: str | Path, to_path: str | Path,
                           runtime_profile: dict[str, Any] | None = None) -> dict[str, Any] | None:
    key = str(Path(data_root).resolve())
    with _SERVICES_LOCK:
        service = _SERVICES.get(key)
    if service is None:
        return None
    return service.find_transition(from_path, to_path, runtime_profile=runtime_profile)


def generation_active(data_root: str | Path) -> bool:
    key = str(Path(data_root).resolve())
    with _SERVICES_LOCK:
        service = _SERVICES.get(key)
    return bool(service is not None and service.generation_lock.locked())


def validate_transition_matrix(data_root: str | Path,
                               pairs: list[tuple[str | Path, str | Path]]) -> dict[str, Any]:
    """Check every directed Clip Flow edge against the currently compatible cache.

    Build the ready-pair index once instead of repeatedly scanning the whole seam
    manifest for every possible randomized transition. A->B and B->A are distinct.
    """
    key = str(Path(data_root).resolve())
    with _SERVICES_LOCK:
        service = _SERVICES.get(key)
    if service is None:
        return {"enabled": False, "total": len(pairs), "ready": 0, "missing": []}
    enabled = bool(service.settings().get("enabled"))
    if not enabled:
        return {"enabled": False, "total": len(pairs), "ready": 0, "missing": []}

    ready = set()
    for seam in service.list_seams():
        if seam.get("available") is True and seam.get("compatible") is True:
            ready.add((_path_key(seam.get("from_path", "")),
                       _path_key(seam.get("to_path", ""))))

    missing = []
    seen = set()
    for from_path, to_path in pairs:
        a, b = _path_key(from_path), _path_key(to_path)
        if not a or not b or a == b or (a, b) in seen:
            continue
        seen.add((a, b))
        if (a, b) not in ready:
            missing.append({"from_path": str(from_path), "to_path": str(to_path)})
    return {"enabled": True, "total": len(seen), "ready": len(seen) - len(missing),
            "missing": missing}
