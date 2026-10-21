import cv2
import ctypes
import numpy as np
import os
import subprocess
import threading
import time


class VirtualCamera:
    def __init__(self):
        self.cam = None
        self.error = None
        self.output_fps = 0
        self.width = 0
        self.height = 0
        self._lock = threading.RLock()
        self._io_lock = threading.RLock()
        # OBS Virtual Camera is a single-sender device. Reserve it across
        # processes so two PAOKRA copies fail clearly instead of racing the DLL.
        self._camera_mutex_handle = None
        self._camera_mutex_owned = False
        self._kernel32 = None

    def _acquire_camera_mutex(self):
        if os.name != "nt":
            return
        with self._lock:
            if self._camera_mutex_owned and self._camera_mutex_handle:
                return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, True, "Local\\PAOKRA.OBSVirtualCamera.Output")
        if not handle:
            raise OSError(ctypes.get_last_error(), "Could not reserve the OBS Virtual Camera output")
        error_code = ctypes.get_last_error()
        if error_code == 183:  # ERROR_ALREADY_EXISTS: another PAOKRA process owns the sender lock.
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_bool
            kernel32.CloseHandle(handle)
            raise RuntimeError(
                "OBS Virtual Camera is already being used by another PAOKRA instance. "
                "Stop Virtual Camera in the other instance before starting this one."
            )
        with self._lock:
            self._kernel32 = kernel32
            self._camera_mutex_handle = handle
            self._camera_mutex_owned = True

    def _release_camera_mutex(self):
        with self._lock:
            kernel32 = self._kernel32
            handle = self._camera_mutex_handle
            owned = self._camera_mutex_owned
            self._kernel32 = None
            self._camera_mutex_handle = None
            self._camera_mutex_owned = False
        if not handle or kernel32 is None:
            return
        try:
            if owned:
                kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
                kernel32.ReleaseMutex.restype = ctypes.c_bool
                kernel32.ReleaseMutex(handle)
        except Exception:
            pass
        try:
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_bool
            kernel32.CloseHandle(handle)
        except Exception:
            pass

    def start(self, width, height, fps, allow_fallback=False):
        try:
            import pyvirtualcam
        except Exception as exc:
            with self._lock:
                self.error = f"ไม่สามารถโหลด pyvirtualcam: {exc}"
            return False

        try:
            width = int(width)
            height = int(height)
            requested_fps = max(1, min(120, int(round(float(fps)))))
        except (TypeError, ValueError, OverflowError):
            with self._lock:
                self.error = "Virtual Camera received an invalid width, height, or FPS"
            return False

        with self._lock:
            current = self.cam
            same_mode = bool(current and self.width == width and self.height == height and self.output_fps == requested_fps)
        if same_mode:
            return True
        if current is not None:
            self.stop()
        try:
            self._acquire_camera_mutex()
        except Exception as exc:
            with self._lock:
                self.error = str(exc)
            return False

        candidates = [requested_fps]
        if allow_fallback:
            # Some virtual-camera drivers expose only a small set of frame-rate
            # modes. Preserve the exact source integer when accepted; use the
            # nearest common mode only when the driver rejects that exact rate.
            for candidate in sorted((24, 30, 60), key=lambda value: (abs(value - requested_fps), value)):
                if candidate not in candidates:
                    candidates.append(candidate)

        last_error = None
        for candidate_fps in candidates:
            new_cam = None
            try:
                # PAOKRA provisions OBS Virtual Camera as its supported sender.
                # Do not let pyvirtualcam silently fall through to UnityCapture:
                # that driver may not exist, and its unrelated error obscures the
                # actual OBS sender-lock failure when another PAOKRA instance is active.
                new_cam = pyvirtualcam.Camera(
                    width=int(width), height=int(height), fps=int(candidate_fps),
                    fmt=pyvirtualcam.PixelFormat.BGR,
                    backend="obs",
                    print_fps=False
                )
                # Serialize device replacement with sends/closes; never close a
                # backend while another worker is inside cam.send().
                with self._io_lock:
                    with self._lock:
                        old = self.cam
                        self.cam = new_cam
                        self.width = int(width)
                        self.height = int(height)
                        self.output_fps = int(candidate_fps)
                        self.error = None
                    if old and old is not new_cam:
                        try:
                            old.close()
                        except Exception:
                            pass
                return True
            except Exception as exc:
                last_error = exc
                if new_cam is not None:
                    try:
                        new_cam.close()
                    except Exception:
                        pass

        raw_error = str(last_error or "Virtual Camera could not be opened")
        low_error = raw_error.lower()
        if ("virtual camera output could not be started" in low_error
                or "already in use" in low_error
                or "already active" in low_error):
            message = (
                "OBS Virtual Camera could not start its sender. Another PAOKRA/OBS instance may already "
                "own the output device; stop Virtual Camera in that instance before retrying. "
                "This is different from an app merely receiving the camera feed. Details: " + raw_error[:260]
            )
        elif "no camera registered" in low_error:
            message = (
                "Windows has no registered OBS Virtual Camera sender. Confirm OBS Virtual Camera is installed "
                "and registered, then restart PAOKRA. Details: " + raw_error[:260]
            )
        else:
            message = raw_error[:420]
        with self._lock:
            self.error = message
        self._release_camera_mutex()
        return False

    @property
    def active(self):
        with self._lock:
            return self.cam is not None

    @property
    def measured_fps(self):
        """Measured sender-call rate from pyvirtualcam, not merely the selected target."""
        with self._io_lock:
            with self._lock:
                cam = self.cam
            if cam is None:
                return 0.0
            try:
                value = float(getattr(cam, "current_fps", 0.0) or 0.0)
                return round(value, 1) if np.isfinite(value) and value >= 0 else 0.0
            except Exception:
                return 0.0

    def status(self):
        with self._lock:
            cam = self.cam
            active = cam is not None
            result = {
                "active": active,
                "width": int(self.width) if active else 0,
                "height": int(self.height) if active else 0,
                "target_fps": int(self.output_fps) if active else 0,
                "error": self.error,
            }
        result["measured_fps"] = self.measured_fps if active else 0.0
        return result

    def send(self, bgr_frame):
        # `stop()` may be called from the UI while the render worker is sending.
        # Serialize backend I/O so close() can never race an in-flight send().
        with self._io_lock:
            with self._lock:
                cam = self.cam
                width, height = self.width, self.height
            if not cam:
                # A send racing with an intentional Stop is not a device error.
                # The output worker re-checks active state before surfacing failures.
                return False
            try:
                frame = np.asarray(bgr_frame)
                if frame.ndim == 2:
                    frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
                elif frame.ndim == 3 and frame.shape[2] == 4:
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                elif frame.ndim != 3 or frame.shape[2] != 3:
                    raise ValueError(f"Expected BGR frame (H,W,3), received shape={frame.shape}")
                if frame.dtype != np.uint8:
                    frame = np.clip(frame, 0, 255).astype(np.uint8)
                # Output settings may change while a compositor frame is already
                # in flight. Match the negotiated camera mode before send(); the
                # DirectShow/OBS backend rejects a mismatched frame size/stride.
                if width > 0 and height > 0 and frame.shape[:2] != (height, width):
                    frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_LINEAR)
                frame = np.ascontiguousarray(frame, dtype=np.uint8)
                cam.send(frame)
                with self._lock:
                    self.error = None
                return True
            except Exception as e:
                with self._lock:
                    self.error = f"{type(e).__name__}: {str(e)[:220]}"
                return False

    def pace(self):
        # Compatibility path; serialize it against camera teardown as well.
        with self._io_lock:
            with self._lock:
                cam = self.cam
            if not cam:
                return
            try:
                cam.sleep_until_next_frame()
            except Exception as e:
                with self._lock:
                    self.error = str(e)

    def stop(self):
        with self._io_lock:
            with self._lock:
                cam = self.cam
                self.cam = None
                self.output_fps = 0
                self.width = 0
                self.height = 0
                self.error = None
            if cam:
                try:
                    cam.close()
                except Exception:
                    pass
        self._release_camera_mutex()


class VirtualAudioRouter:
    """Route the active source video's own audio to a virtual audio device.

    A/V sync design (v2):
      * The video side selects frames from a monotonic clock whose origin is
        `start_at`. The audio side is aligned to that SAME origin.
      * No `-re` pacing: the audio device (blocking write) is the only audio
        clock, so there are not two free-running clocks fighting each other.
      * If audio starts late (ffmpeg / PortAudio startup), the late part is
        skipped so audio lands at the same media time as video.
      * Looping is done per cycle with an exact length (= video loop length),
        so audio and video never drift apart across loop boundaries.
      * A slow drift controller compares audio content time against the
        monotonic clock and inserts / drops 10 ms at a time.
      * `offset_ms` (+ = delay audio) compensates the latency of the receiving
        app (TikTok LIVE Studio adds video latency) and can be changed live.
    """
    def __init__(self):
        self._lock = threading.RLock()
        self._thread = None
        self._stop = threading.Event()
        self._proc = None
        self._stream = None
        self.active = False
        self.error = None
        self._generation = 0
        # Prefer a real VB-CABLE sink. If it is not installed, use the
        # AudioRelay virtual playback endpoint already present on this PC.
        self.device_name = "CABLE Input (VB-Audio Virtual Cable)"
        self.fallback_device_names = [
            "Virtual Speakers (Virtual Speakers for AudioRelay)",
            "Virtual Speakers for AudioRelay",
        ]
        self.sample_rate = 48000
        self.channels = 2
        self.chunk_frames = 960
        self.loop_duration = 0.0      # seconds of one video loop (set by server)
        self.offset_ms = 0            # + delays audio, - advances audio
        self._shift_frames = 0
        self.sync = {"drift_ms": 0.0, "corrections": 0, "late_skip_ms": 0.0, "latency_ms": 0.0}

    def _find_ffmpeg(self):
        import shutil
        candidates = [
            os.environ.get("PAOKRA_FFMPEG", ""),
            shutil.which("ffmpeg"),
            r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        ]
        # PAOKRA bundles a Windows FFmpeg runtime under ms-playwright.
        # Playwright names it ffmpeg-win64.exe, so do not require a system
        # FFmpeg installation (or winget) on the target PC.
        try:
            root = os.environ.get("PAOKRA_HOME", "")
            if root:
                import glob
                for pat in (
                    os.path.join(root, "ms-playwright", "ffmpeg-*", "ffmpeg.exe"),
                    os.path.join(root, "ms-playwright", "ffmpeg-*", "ffmpeg-win64.exe"),
                    os.path.join(root, "ffmpeg", "ffmpeg.exe"),
                    os.path.join(root, "ffmpeg", "ffmpeg-win64.exe"),
                ):
                    candidates.extend(glob.glob(pat))
        except Exception:
            pass
        for p in candidates:
            if p and os.path.isfile(p):
                return p
        return None

    def _find_device(self, sd):
        devices = list(sd.query_devices())
        names = [self.device_name] + list(self.fallback_device_names)
        for wanted in names:
            matches = []
            for i, d in enumerate(devices):
                name = str(d.get("name", ""))
                if wanted.lower() in name.lower() and int(d.get("max_output_channels", 0)) >= 2:
                    matches.append((i, d))
            if matches:
                matches.sort(key=lambda x: (
                    abs(float(x[1].get("default_samplerate", 44100.0)) - self.sample_rate),
                    x[0]
                ))
                self.device_name = str(matches[0][1].get("name", wanted))
                # Follow the device native rate (AudioRelay is commonly 44.1 kHz).
                self.sample_rate = int(round(float(matches[0][1].get("default_samplerate", self.sample_rate))))
                return matches[0][0]
        available = [str(d.get("name", "")) for d in devices if int(d.get("max_output_channels", 0)) >= 2]
        raise RuntimeError("ไม่พบ Virtual Audio output: " + ", ".join(available[:8]))

    def set_offset(self, ms):
        """Change the A/V offset while running (+ = delay audio)."""
        try:
            ms = int(max(-500, min(1500, int(float(ms)))))
        except Exception:
            return
        with self._lock:
            delta = ms - int(self.offset_ms)
            self.offset_ms = ms
            if self.active and delta:
                self._shift_frames += int(round(delta * self.sample_rate / 1000.0))

    def start(self, source_path, playback_generation=None, start_at=None, loop=False,
              loop_duration=None, offset_ms=None, source_offset=0.0):
        source_path = str(source_path or "")
        if not source_path or not os.path.isfile(source_path):
            self.error = "ไม่พบไฟล์ Source สำหรับ Virtual Audio"
            return False
        ffmpeg = self._find_ffmpeg()
        if not ffmpeg:
            self.error = "ไม่พบ ffmpeg.exe สำหรับถอดเสียงจาก Source"
            return False
        with self._lock:
            existing = self._thread
            already_active = bool(self.active)
            stopping = self._stop.is_set()
        if already_active:
            return True
        if existing and existing.is_alive() and existing is not threading.current_thread():
            if not stopping:
                # Another caller is already starting this exact router. Do not
                # create a second PortAudio stream / FFmpeg process.
                return True
            existing.join(timeout=2.5)
            with self._lock:
                if self._thread is existing and existing.is_alive():
                    self.error = "Virtual Audio previous session is still stopping"
                    return False
                if self._thread is existing:
                    self._thread = None
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._stop.clear()
            self.error = None
            self._shift_frames = 0
            if offset_ms is not None:
                try: self.offset_ms = int(max(-500, min(1500, int(float(offset_ms)))))
                except Exception: pass
            if loop_duration:
                try: self.loop_duration = float(loop_duration)
                except Exception: pass
            self._thread = threading.Thread(
                target=self._worker,
                args=(ffmpeg, source_path, generation, start_at, bool(loop), source_offset),
                name="paokra-virtual-audio",
                daemon=True,
            )
            self._thread.start()
        return True

    def _spawn(self, ffmpeg, src, duration, loop, source_offset=0.0):
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error"]
        if loop and not duration:
            # Unknown loop length: fall back to ffmpeg's own looping.
            cmd += ["-stream_loop", "-1"]
        try:
            source_offset = max(0.0, float(source_offset or 0.0))
        except (TypeError, ValueError, OverflowError):
            source_offset = 0.0
        if source_offset > 0.001:
            cmd += ["-ss", "%.6f" % source_offset]
        cmd += ["-i", src, "-vn", "-ac", str(self.channels), "-ar", str(self.sample_rate)]
        if duration:
            # Exactly one video-loop worth of audio (padded / trimmed).
            cmd += ["-af", "apad=whole_dur=%.6f" % duration, "-t", "%.6f" % duration]
        cmd += ["-f", "s16le", "pipe:1"]
        return subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    @staticmethod
    def _read_exact(proc, n):
        buf = b""
        while len(buf) < n:
            part = proc.stdout.read(n - len(buf))
            if not part:
                break
            buf += part
        return buf

    @staticmethod
    def _kill(proc):
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass
        # Repeated seeks / clip transitions create new FFmpeg pipes. Reap the
        # child and close stdout so long sessions do not accumulate handles.
        try:
            proc.wait(timeout=0.5)
        except Exception:
            pass
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except Exception:
            pass

    def _worker(self, ffmpeg, source_path, generation, start_at=None, loop=False, source_offset=0.0):
        proc = None
        nxt = None            # (process, duration) prepared for the next loop cycle
        stream = None
        current = False
        try:
            import sounddevice as sd
            device = self._find_device(sd)
            sr = int(self.sample_rate)
            ch = int(self.channels)
            frame_bytes = ch * 2
            chunk_frames = int(self.chunk_frames)
            chunk_bytes = chunk_frames * frame_bytes

            def cycle_duration():
                if not loop:
                    return 0.0
                d = float(self.loop_duration or 0.0)
                return d if d > 0.2 else 0.0

            dur = cycle_duration()
            proc = self._spawn(ffmpeg, source_path, dur, loop, source_offset=source_offset)
            try:
                stream = sd.RawOutputStream(
                    samplerate=sr, channels=ch, dtype="int16", device=device,
                    blocksize=chunk_frames, latency="low",
                )
            except Exception:
                stream = sd.RawOutputStream(
                    samplerate=sr, channels=ch, dtype="int16", device=device,
                    blocksize=chunk_frames,
                )
            stream.start()
            try:
                latency_s = float(getattr(stream, "latency", 0.0) or 0.0)
                if latency_s != latency_s or latency_s > 1.0:
                    latency_s = 0.0
            except Exception:
                latency_s = 0.0
            with self._lock:
                current = (generation == self._generation and not self._stop.is_set())
                if current:
                    self._proc = proc
                    self._stream = stream
                    self.active = True
                    self.error = None
                    self.sync = {"drift_ms": 0.0, "corrections": 0, "late_skip_ms": 0.0,
                                 "latency_ms": round(latency_s * 1000.0, 1)}
            if not current:
                return

            state = {"proc": proc, "cycle_total": int(round(dur * sr)) * frame_bytes if dur else 0}
            state["cycle_left"] = state["cycle_total"]

            def fetch(nbytes):
                """Return exactly nbytes of audio (looping per exact cycle)."""
                nonlocal nxt
                out = b""
                while len(out) < nbytes and not self._stop.is_set():
                    want = nbytes - len(out)
                    if state["cycle_total"]:
                        take = min(want, state["cycle_left"])
                        data = self._read_exact(state["proc"], take) if take else b""
                        if len(data) < take:
                            data += bytes(take - len(data))   # ffmpeg ended early -> silence
                        out += data
                        state["cycle_left"] -= take
                        if nxt is None and state["cycle_left"] <= 2 * sr * frame_bytes:
                            d2 = cycle_duration()
                            if d2:
                                nxt = (self._spawn(ffmpeg, source_path, d2, loop, source_offset=source_offset), d2)
                        if state["cycle_left"] <= 0:
                            self._kill(state["proc"])
                            if nxt is None:
                                d2 = cycle_duration()
                                nxt = (self._spawn(ffmpeg, source_path, d2, loop, source_offset=source_offset), d2)
                            nproc, d2 = nxt
                            nxt = None
                            state["proc"] = nproc
                            state["cycle_total"] = int(round(d2 * sr)) * frame_bytes if d2 else 0
                            state["cycle_left"] = state["cycle_total"]
                            with self._lock:
                                if generation == self._generation:
                                    self._proc = nproc
                    else:
                        data = self._read_exact(state["proc"], want)
                        out += data
                        if len(data) < want:
                            break       # real end of file (non-looping)
                return out

            offset_s = float(self.offset_ms) / 1000.0
            if start_at is not None:
                release_at = float(start_at) - latency_s + offset_s
            else:
                release_at = time.monotonic()

            # Pre-roll: decode one chunk, then hold it until the shared start time.
            first = fetch(chunk_bytes)
            if not first:
                return
            while time.monotonic() < release_at and not self._stop.is_set():
                with self._lock:
                    if generation != self._generation:
                        return
                time.sleep(0.001)

            late = time.monotonic() - release_at
            content_frames = 0
            if late > 0.015:
                # Audio pipeline started after the video clock. Skip the part of
                # the audio that video already "played", so both are at the same
                # media time right now.
                skip_frames = min(int(late * sr), sr * 10)
                content_frames = skip_frames
                remaining = max(0, skip_frames - chunk_frames) * frame_bytes
                while remaining > 0 and not self._stop.is_set():
                    step = min(remaining, sr * frame_bytes)
                    got = fetch(step)
                    if not got:
                        break
                    remaining -= len(got)
                with self._lock:
                    self.sync["late_skip_ms"] = round(1000.0 * skip_frames / sr, 1)
            else:
                stream.write(first)
                content_frames = len(first) // frame_bytes

            last_check = time.monotonic()
            base_lead = None
            corrections = 0
            step_frames = max(1, int(0.010 * sr))

            while not self._stop.is_set():
                with self._lock:
                    if generation != self._generation:
                        break
                    shift = int(self._shift_frames)
                    self._shift_frames = 0
                if shift > 0:
                    stream.write(bytes(shift * frame_bytes))      # delay audio
                    release_at += shift / float(sr)
                elif shift < 0:
                    fetch((-shift) * frame_bytes)                 # advance audio
                    content_frames += -shift
                    release_at += shift / float(sr)

                data = fetch(chunk_bytes)
                if not data:
                    break
                stream.write(data)
                content_frames += len(data) // frame_bytes

                now = time.monotonic()
                if now - last_check >= 1.0:
                    last_check = now
                    lead = content_frames / float(sr) - (now - release_at)
                    if base_lead is None:
                        if now - release_at > 2.0:
                            base_lead = lead
                    else:
                        dev = lead - base_lead
                        with self._lock:
                            self.sync["drift_ms"] = round(dev * 1000.0, 1)
                            self.sync["corrections"] = corrections
                        adj = max(step_frames, min(int(0.040 * sr), int(abs(dev) * sr / 2)))
                        if dev > 0.030:
                            stream.write(bytes(adj * frame_bytes))   # content ahead
                            corrections += 1
                        elif dev < -0.030:
                            fetch(adj * frame_bytes)                  # content behind
                            content_frames += adj
                            corrections += 1
        except Exception as exc:
            with self._lock:
                if generation == self._generation and not self._stop.is_set():
                    self.error = str(exc)
        finally:
            try:
                if stream:
                    stream.stop()
                    stream.close()
            except Exception:
                pass
            self._kill(proc)
            try:
                self._kill(nxt[0] if nxt else None)
            except Exception:
                pass
            try:
                self._kill(state["proc"])
            except Exception:
                pass
            with self._lock:
                if generation == self._generation:
                    self._proc = None
                    self._stream = None
                    self.active = False
                    if self._thread is threading.current_thread():
                        self._thread = None

    def stop(self):
        # Stop is intentionally coordinated through the worker thread.
        # Calling PortAudio abort/close from the HTTP thread while the worker
        # is inside stream.write() can terminate the native backend.
        with self._lock:
            self._generation += 1
            self._stop.set()
            proc = self._proc
            thread = self._thread
            self.active = False
        self._kill(proc)
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.5)
        with self._lock:
            if self._thread is thread and (not thread or not thread.is_alive()):
                self._thread = None
