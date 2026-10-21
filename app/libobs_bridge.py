"""Optional, headless libobs frame bridge for PAOKRA.

The bridge is limited to the tested OBS Studio 32.2.2 ABI. PAOKRA's existing
compositor and VirtualCamera path remain the fallback if OBS is missing or any
frame operation fails. No OBS frontend/Qt/UI modules are loaded.
"""
from __future__ import annotations

import ctypes
import gc
import os
import threading
import time
from collections import deque
from pathlib import Path

import cv2
import numpy as np

OBS_VERSION_SUPPORTED = "32.2.2"
VIDEO_FORMAT_BGRA = 7
OBS_SOURCE_TYPE_INPUT = 0
OBS_SOURCE_ASYNC_VIDEO = (1 << 2) | (1 << 0)
GS_CS_SRGB = 3
VIDEO_RANGE_FULL = 2


class _ObsVideoInfo(ctypes.Structure):
    _fields_ = [
        ("graphics_module", ctypes.c_char_p),
        ("fps_num", ctypes.c_uint32), ("fps_den", ctypes.c_uint32),
        ("base_width", ctypes.c_uint32), ("base_height", ctypes.c_uint32),
        ("output_width", ctypes.c_uint32), ("output_height", ctypes.c_uint32),
        ("output_format", ctypes.c_int), ("adapter", ctypes.c_uint32),
        ("gpu_conversion", ctypes.c_bool),
        ("colorspace", ctypes.c_int), ("range", ctypes.c_int), ("scale_type", ctypes.c_int),
    ]


class _ObsAudioInfo(ctypes.Structure):
    _fields_ = [("samples_per_sec", ctypes.c_uint32), ("speakers", ctypes.c_int)]


class _ObsVideoScaleInfo(ctypes.Structure):
    _fields_ = [
        ("format", ctypes.c_int), ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32), ("range", ctypes.c_int),
        ("colorspace", ctypes.c_int),
    ]


class _ObsVideoData(ctypes.Structure):
    _fields_ = [
        ("data", ctypes.POINTER(ctypes.c_uint8) * 8),
        ("linesize", ctypes.c_uint32 * 8),
        ("timestamp", ctypes.c_uint64),
    ]


class _ObsSourceFrame(ctypes.Structure):
    # Exact structure layout for OBS Studio 32.2.2 libobs/obs.h.
    _fields_ = [
        ("data", ctypes.POINTER(ctypes.c_uint8) * 8),
        ("linesize", ctypes.c_uint32 * 8),
        ("width", ctypes.c_uint32), ("height", ctypes.c_uint32),
        ("timestamp", ctypes.c_uint64), ("format", ctypes.c_int),
        ("color_matrix", ctypes.c_float * 16),
        ("full_range", ctypes.c_bool), ("max_luminance", ctypes.c_uint16),
        ("color_range_min", ctypes.c_float * 3), ("color_range_max", ctypes.c_float * 3),
        ("flip", ctypes.c_bool), ("flags", ctypes.c_uint8), ("trc", ctypes.c_uint8),
        ("refs", ctypes.c_long), ("prev_frame", ctypes.c_bool),
    ]


_GET_NAME = ctypes.CFUNCTYPE(ctypes.c_char_p, ctypes.c_void_p)
_CREATE = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
_DESTROY = ctypes.CFUNCTYPE(None, ctypes.c_void_p)
_GET_DIMENSION = ctypes.CFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)
_RAW_VIDEO_CALLBACK = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.POINTER(_ObsVideoData))


class _ObsSourceInfoHead(ctypes.Structure):
    # obs_register_source_s receives the exact size, so no offsets are guessed
    # for the optional members after the eight leading fields.
    _fields_ = [
        ("id", ctypes.c_char_p), ("type", ctypes.c_int),
        ("output_flags", ctypes.c_uint32), ("get_name", _GET_NAME),
        ("create", _CREATE), ("destroy", _DESTROY),
        ("get_width", _GET_DIMENSION), ("get_height", _GET_DIMENSION),
    ]


class LibObsBridge:
    """Push composed PAOKRA frames into libobs and return its raw video output.

    stop() detaches the source/callback while keeping libobs initialized. OBS
    Studio 32.2.2 can reset resolution in that warm context, but its OpenGL
    context cannot reliably be torn down and recreated in the same process.
    close() fully shuts down libobs and is intended for backend process exit.
    """

    def __init__(self, config_dir: str | os.PathLike | None = None):
        self._api_lock = threading.RLock()
        self._frame_condition = threading.Condition()
        self._pending_frames = deque()
        self._latest_output_bgra = None
        self._latest_output_timestamp = 0
        self._output_serial = 0
        self._last_input_timestamp = 0
        self._timeout_streak = 0

        self._obs = None
        self._dll_handles = []
        self._started = False
        self._source_registered = False
        self._callback_registered = False
        self._closing = True
        self._source = None
        self._settings = None
        self._source_info = None
        self._instance_data = None
        self._callbacks = None
        self._raw_callback = None
        self._obs_root = None
        self._base_config = None
        self._width = self._height = self._fps = 0

        self.active = False
        self.version = ""
        self.error = None
        self.frames_in = 0
        self.frames_out = 0
        self.frames_timed_out = 0
        self.last_latency_ms = 0.0
        self._disabled_after_fatal_error = False

        self._config_dir = Path(config_dir) if config_dir else None

    @staticmethod
    def _find_obs_root() -> Path | None:
        candidates = []
        root_override = os.environ.get("PAOKRA_OBS_ROOT", "").strip()
        if root_override:
            candidates.append(Path(root_override))
        for key in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            value = os.environ.get(key)
            if value:
                candidates.append(Path(value) / "obs-studio")
        candidates += [
            Path(r"C:\Program Files\obs-studio"),
            Path(r"C:\Program Files (x86)\obs-studio"),
        ]
        seen = set()
        for root in candidates:
            try:
                key = os.path.normcase(str(root.resolve()))
                if key in seen:
                    continue
                seen.add(key)
                if (root / "bin" / "64bit" / "obs.dll").is_file() and (
                    root / "data" / "libobs" / "default.effect"
                ).is_file():
                    return root.resolve()
            except OSError:
                continue
        return None

    @staticmethod
    def _set_fn(dll, name, restype, argtypes):
        f = getattr(dll, name)
        f.restype = restype
        f.argtypes = argtypes
        return f

    def _configure_functions(self):
        d = self._obs
        self._startup = self._set_fn(
            d, "obs_startup", ctypes.c_bool,
            [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p],
        )
        self._shutdown = self._set_fn(d, "obs_shutdown", None, [])
        self._add_data_path = self._set_fn(d, "obs_add_data_path", None, [ctypes.c_char_p])
        self._get_version = self._set_fn(d, "obs_get_version_string", ctypes.c_char_p, [])
        self._reset_video = self._set_fn(
            d, "obs_reset_video", ctypes.c_int, [ctypes.POINTER(_ObsVideoInfo)]
        )
        self._reset_audio = self._set_fn(
            d, "obs_reset_audio", ctypes.c_bool, [ctypes.POINTER(_ObsAudioInfo)]
        )
        self._register_source = self._set_fn(
            d, "obs_register_source_s", None,
            [ctypes.POINTER(_ObsSourceInfoHead), ctypes.c_size_t],
        )
        self._data_create = self._set_fn(d, "obs_data_create", ctypes.c_void_p, [])
        self._data_release = self._set_fn(d, "obs_data_release", None, [ctypes.c_void_p])
        self._source_create = self._set_fn(
            d, "obs_source_create", ctypes.c_void_p,
            [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p],
        )
        self._source_release = self._set_fn(d, "obs_source_release", None, [ctypes.c_void_p])
        self._set_output_source = self._set_fn(
            d, "obs_set_output_source", None, [ctypes.c_uint32, ctypes.c_void_p]
        )
        self._output_video = self._set_fn(
            d, "obs_source_output_video", None,
            [ctypes.c_void_p, ctypes.POINTER(_ObsSourceFrame)],
        )
        self._add_raw_callback = self._set_fn(
            d, "obs_add_raw_video_callback", None,
            [ctypes.POINTER(_ObsVideoScaleInfo), _RAW_VIDEO_CALLBACK, ctypes.c_void_p],
        )
        self._remove_raw_callback = self._set_fn(
            d, "obs_remove_raw_video_callback", None,
            [_RAW_VIDEO_CALLBACK, ctypes.c_void_p],
        )

    def _video_info(self, width: int, height: int, fps: int):
        return _ObsVideoInfo(
            b"libobs-opengl", fps, 1, width, height, width, height,
            VIDEO_FORMAT_BGRA, 0, False, 0, VIDEO_RANGE_FULL, 0,
        )

    def _initialize_core(self, width: int, height: int, fps: int):
        root = self._find_obs_root()
        if root is None:
            raise RuntimeError(
                "OBS Studio 32.2.2 runtime not found; using direct Virtual Camera fallback"
            )
        bin64 = root / "bin" / "64bit"
        plugin_dir = root / "obs-plugins" / "64bit"
        core_data = root / "data" / "libobs"
        self._dll_handles = [
            os.add_dll_directory(str(bin64)),
            os.add_dll_directory(str(plugin_dir)),
        ]
        self._obs_root = root
        self._obs = ctypes.CDLL(str(bin64 / "obs.dll"))
        self._configure_functions()

        if self._config_dir is None:
            local_data = os.environ.get("LOCALAPPDATA", os.getenv("TEMP", "."))
            self._config_dir = Path(local_data) / "PAOKRA Studio" / "libobs-config"
        self._config_dir.mkdir(parents=True, exist_ok=True)
        data_path = os.fsencode(str(core_data).replace("\\", "/").rstrip("/") + "/")
        self._add_data_path(data_path)
        if not self._startup(b"en-US", os.fsencode(str(self._config_dir)), None):
            raise RuntimeError("obs_startup returned false")
        self._started = True
        version_raw = self._get_version()
        self.version = version_raw.decode("ascii", errors="replace") if version_raw else ""
        if self.version != OBS_VERSION_SUPPORTED:
            raise RuntimeError(
                f"Unsupported OBS version {self.version or '(unknown)'}; "
                f"validated version is {OBS_VERSION_SUPPORTED}"
            )
        # A trailing slash is required by the validated OBS 32.2.2 data search.
        self._add_data_path(data_path)
        rc = int(self._reset_video(ctypes.byref(self._video_info(width, height, fps))))
        if rc != 0:
            raise RuntimeError(f"obs_reset_video failed with code {rc}")
        if not self._reset_audio(ctypes.byref(_ObsAudioInfo(48000, 2))):
            raise RuntimeError("obs_reset_audio returned false")
        self._base_config = (width, height, fps)
        self._register_frame_source_type()

    def _register_frame_source_type(self):
        if self._source_registered:
            return
        # Use the documented first eight fields and the exact structure size.
        # The asynchronous-video flag permits obs_source_output_video().
        self._instance_data = ctypes.create_string_buffer(16)
        source_name = b"PAOKRA Frame Bridge"

        def get_name(_type_data):
            return source_name

        def create(_settings, _source):
            return ctypes.addressof(self._instance_data)

        def destroy(_data):
            return None

        def get_width(_data):
            return self._width

        def get_height(_data):
            return self._height

        self._callbacks = (
            _GET_NAME(get_name), _CREATE(create), _DESTROY(destroy),
            _GET_DIMENSION(get_width), _GET_DIMENSION(get_height),
        )
        self._source_info = _ObsSourceInfoHead(
            b"paokra_frame_bridge", OBS_SOURCE_TYPE_INPUT, OBS_SOURCE_ASYNC_VIDEO,
            *self._callbacks,
        )
        self._register_source(
            ctypes.byref(self._source_info), ctypes.sizeof(self._source_info)
        )
        self._source_registered = True

    def _activate_source(self, width: int, height: int, fps: int):
        self._width, self._height, self._fps = width, height, fps
        self._instance_data = ctypes.create_string_buffer(16)
        settings = self._data_create()
        if not settings:
            raise RuntimeError("obs_data_create failed")
        try:
            source = self._source_create(
                b"paokra_frame_bridge", b"PAOKRA Program Output", settings, None
            )
        finally:
            self._data_release(settings)
        if not source:
            raise RuntimeError("obs_source_create failed")
        self._source = source
        self._closing = False
        self._set_output_source(0, self._source)

        def raw_callback(_param, frame_ptr):
            self._on_raw_frame(frame_ptr)

        self._raw_callback = _RAW_VIDEO_CALLBACK(raw_callback)
        scale = _ObsVideoScaleInfo(
            VIDEO_FORMAT_BGRA, width, height, VIDEO_RANGE_FULL, GS_CS_SRGB
        )
        self._add_raw_callback(ctypes.byref(scale), self._raw_callback, None)
        self._callback_registered = True
        with self._frame_condition:
            self._pending_frames.clear()
            self._latest_output_bgra = None
            self._latest_output_timestamp = 0
            self._output_serial = 0
            self._last_input_timestamp = 0
            self._frame_condition.notify_all()
        self._timeout_streak = 0
        self.active = True
        self.error = None

    def start(self, width: int, height: int, fps: int) -> bool:
        """Start/reconfigure the source without tearing down a healthy GL context."""
        try:
            width, height, fps = int(width), int(height), int(fps)
            if not (64 <= width <= 1920 and 64 <= height <= 1920 and 1 <= fps <= 60):
                raise ValueError("Unsupported libobs frame size/FPS")
            with self._api_lock:
                if self._disabled_after_fatal_error:
                    return False
                if self.active and (width, height, fps) == (self._width, self._height, self._fps):
                    return True
                was_initialized = self._started and self._obs is not None
                self._stop_locked(preserve_error=True)

                if not was_initialized:
                    self._initialize_core(width, height, fps)
                elif self._base_config != (width, height, fps):
                    # Tested on OBS 32.2.2: reset_video succeeds after removing
                    # the callback and detaching/releasing the active source.
                    rc = int(self._reset_video(ctypes.byref(self._video_info(width, height, fps))))
                    if rc != 0:
                        self.error = (
                            f"obs_reset_video could not switch to {width}x{height}@{fps} "
                            f"(code {rc}); keeping the warm context and using direct fallback"
                        )
                        return False
                    self._base_config = (width, height, fps)
                self._register_frame_source_type()
                self._activate_source(width, height, fps)
                self.frames_in = self.frames_out = self.frames_timed_out = 0
                self.last_latency_ms = 0.0
                return True
        except Exception as exc:
            message = f"{type(exc).__name__}: {str(exc)[:220]}"
            with self._api_lock:
                self.error = message
                self._stop_locked(preserve_error=True)
                # A failed cold startup/shutdown must not be retried in the same
                # process; OBS/OpenGL may not recreate its hidden window class.
                if self._started:
                    self._close_locked(preserve_error=True)
                    self._disabled_after_fatal_error = True
                else:
                    self._close_locked(preserve_error=True)
                    if self._obs_root is not None:
                        self._disabled_after_fatal_error = True
            return False

    def _on_raw_frame(self, frame_ptr):
        """libobs video-thread callback. Copy memory before returning to C."""
        if not frame_ptr or self._closing:
            return
        try:
            frame = frame_ptr.contents
            width, height = self._width, self._height
            pitch = int(frame.linesize[0])
            if width <= 0 or height <= 0 or pitch < width * 4 or not frame.data[0]:
                return
            raw_ptr = ctypes.cast(frame.data[0], ctypes.c_void_p).value
            if not raw_ptr:
                return
            backing = (ctypes.c_uint8 * (pitch * height)).from_address(raw_ptr)
            view = np.ndarray(
                (height, width, 4), dtype=np.uint8, buffer=backing,
                strides=(pitch, 4, 1),
            )
            copied = view.copy()
            timestamp = int(frame.timestamp)
            with self._frame_condition:
                self._latest_output_bgra = copied
                self._latest_output_timestamp = timestamp
                self._output_serial += 1
                self.frames_out += 1
                # Retain each input buffer until libobs output has passed it.
                while self._pending_frames and self._pending_frames[0][0] <= timestamp:
                    self._pending_frames.popleft()
                while len(self._pending_frames) > 8:
                    self._pending_frames.popleft()
                self._frame_condition.notify_all()
        except Exception as exc:
            self.error = f"raw callback {type(exc).__name__}: {str(exc)[:140]}"

    def process(self, bgr_frame: np.ndarray, timeout: float | None = None) -> np.ndarray | None:
        """Return the corresponding libobs output frame or None for direct fallback."""
        with self._api_lock:
            if not self.active or self._source is None:
                return None
            try:
                source_frame = np.asarray(bgr_frame)
                if source_frame.dtype != np.uint8:
                    source_frame = np.clip(source_frame, 0, 255).astype(np.uint8)
                if source_frame.ndim == 2:
                    source_frame = cv2.cvtColor(source_frame, cv2.COLOR_GRAY2BGR)
                elif source_frame.ndim == 3 and source_frame.shape[2] == 4:
                    source_frame = cv2.cvtColor(source_frame, cv2.COLOR_BGRA2BGR)
                elif source_frame.ndim != 3 or source_frame.shape[2] != 3:
                    raise ValueError(f"expected BGR frame, received {source_frame.shape}")
                if source_frame.shape[:2] != (self._height, self._width):
                    source_frame = cv2.resize(
                        source_frame, (self._width, self._height), interpolation=cv2.INTER_LINEAR
                    )
                bgra = cv2.cvtColor(
                    np.ascontiguousarray(source_frame, dtype=np.uint8), cv2.COLOR_BGR2BGRA
                )
                timestamp = max(time.monotonic_ns(), self._last_input_timestamp + 1)
                self._last_input_timestamp = timestamp
                obs_frame = _ObsSourceFrame()
                obs_frame.data[0] = bgra.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8))
                obs_frame.linesize[0] = int(bgra.strides[0])
                obs_frame.width, obs_frame.height = self._width, self._height
                obs_frame.timestamp = timestamp
                obs_frame.format = VIDEO_FORMAT_BGRA
                obs_frame.full_range = True
                obs_frame.flip = False
                obs_frame.refs = 1

                with self._frame_condition:
                    serial_before = self._output_serial
                    self._pending_frames.append((timestamp, bgra, obs_frame))
                    while len(self._pending_frames) > 8:
                        self._pending_frames.popleft()
                t0 = time.perf_counter()
                self._output_video(self._source, ctypes.byref(obs_frame))
                # Give one frame enough scheduler slack, but never let a late GPU
                # callback block the camera worker for more than about two frames.
                # At 30 FPS this is 66.7 ms; failures fall back to the direct path.
                wait_limit = timeout if timeout is not None else max(0.06, 2.0 / self._fps)
                deadline = time.monotonic() + float(wait_limit)
                timed_out = False
                output_bgra = None
                with self._frame_condition:
                    while not (
                        self._output_serial > serial_before
                        and self._latest_output_timestamp >= timestamp
                    ):
                        remain = deadline - time.monotonic()
                        if remain <= 0 or not self.active:
                            timed_out = True
                            break
                        self._frame_condition.wait(timeout=remain)
                    if not timed_out:
                        output_bgra = self._latest_output_bgra
                        self._timeout_streak = 0
                        self.frames_in += 1
                        self.last_latency_ms = (time.perf_counter() - t0) * 1000.0
                if timed_out:
                    self.frames_timed_out += 1
                    self._timeout_streak += 1
                    self.last_latency_ms = (time.perf_counter() - t0) * 1000.0
                    self.error = (
                        "libobs raw-frame callback timed out; direct Virtual Camera "
                        "fallback remains enabled"
                    )
                    if self._timeout_streak >= 3:
                        self._stop_locked(preserve_error=True)
                    return None
                if output_bgra is None or output_bgra.shape != (
                    self._height, self._width, 4
                ):
                    self.error = "libobs returned an invalid frame shape; direct fallback remains enabled"
                    return None
                self.error = None
                return cv2.cvtColor(output_bgra, cv2.COLOR_BGRA2BGR)
            except Exception as exc:
                self.error = f"process {type(exc).__name__}: {str(exc)[:180]}"
                self._timeout_streak += 1
                if self._timeout_streak >= 3:
                    self._stop_locked(preserve_error=True)
                return None

    def _stop_locked(self, preserve_error: bool = False):
        """Stop frame IO but keep a healthy initialized libobs context reusable."""
        self.active = False
        self._closing = True
        if self._obs is not None:
            try:
                if self._callback_registered and self._raw_callback is not None:
                    self._remove_raw_callback(self._raw_callback, None)
            except Exception:
                pass
            self._callback_registered = False
            try:
                if self._source:
                    self._set_output_source(0, None)
            except Exception:
                pass
            try:
                if self._source:
                    self._source_release(self._source)
            except Exception:
                pass
            self._source = None
            if self._settings:
                try:
                    self._data_release(self._settings)
                except Exception:
                    pass
            self._settings = None
        self._raw_callback = None
        self._instance_data = None
        with self._frame_condition:
            self._pending_frames.clear()
            self._latest_output_bgra = None
            self._latest_output_timestamp = 0
            self._frame_condition.notify_all()
        self._width = self._height = self._fps = 0
        self._timeout_streak = 0
        if not preserve_error:
            self.error = None

    def _close_locked(self, preserve_error: bool = False):
        self._stop_locked(preserve_error=True)
        if self._obs is not None and self._started:
            try:
                self._shutdown()
            except Exception:
                pass
        self._started = False
        self._source_registered = False
        self._source_info = None
        self._callbacks = None
        self._instance_data = None
        self._raw_callback = None
        self._obs = None
        self._base_config = None
        self._obs_root = None
        for handle in reversed(self._dll_handles):
            try:
                handle.close()
            except Exception:
                pass
        self._dll_handles = []
        gc.collect()
        if not preserve_error:
            self.error = None

    def stop(self):
        """Detach the frame path without tearing down OBS's GL context."""
        with self._api_lock:
            self._stop_locked()

    def close(self):
        """Fully shut down libobs; call only during backend process exit."""
        with self._api_lock:
            self._close_locked()

    def status(self) -> dict:
        with self._api_lock:
            return {
                "active": bool(self.active),
                "version": self.version or None,
                "width": self._width or None,
                "height": self._height or None,
                "fps": self._fps or None,
                "frames_in": int(self.frames_in),
                "frames_out": int(self.frames_out),
                "frames_timed_out": int(self.frames_timed_out),
                "last_latency_ms": round(float(self.last_latency_ms), 2),
                "error": self.error,
            }
