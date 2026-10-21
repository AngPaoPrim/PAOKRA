"""PAOKRA clip effects: colour grade, beauty skin smoothing, face reshape.

Face features use OpenCV YuNet (5 landmarks) - no extra pip packages.
apply() never raises: on any error the untouched input frame is returned.
"""
import json, os, threading
from pathlib import Path
import cv2
import numpy as np
from flask import jsonify, request, Response, send_from_directory

RANGES = {
    "brightness": (-100, 100), "contrast": (-100, 100), "saturation": (-100, 100), "temperature": (-100, 100),
    "sharpen": (0, 100), "soften": (0, 100), "vignette": (0, 100),
    "skin_smooth": (0, 100), "skin_tone": (0, 100),
    "eye_enlarge": (0, 100), "face_slim": (0, 100),
}
DEFAULT = {"enabled": True, **{k: 0 for k in RANGES}}
PRESETS = {
    "natural": {"skin_smooth": 35, "skin_tone": 15, "soften": 10, "sharpen": 15, "brightness": 4, "saturation": 6},
    "bright": {"brightness": 12, "contrast": 8, "saturation": 12, "skin_smooth": 25, "skin_tone": 25, "sharpen": 10},
    "warm": {"temperature": 28, "saturation": 8, "skin_smooth": 20, "vignette": 15},
    "cool": {"temperature": -25, "contrast": 10, "sharpen": 15},
    "cinematic": {"contrast": 22, "saturation": -12, "temperature": -8, "vignette": 35, "sharpen": 12},
    "facelift": {"eye_enlarge": 25, "face_slim": 22, "skin_smooth": 30, "skin_tone": 12},
}

_MODEL_NAME = "face_detection_yunet_2023mar.onnx"
_model_dirs = [Path(__file__).resolve().parent / "web" / "models", Path(__file__).resolve().parent / "models"]
_det = None
_det_failed = False
_faces = []
_miss = 0
_tick = 0
_vig_cache = {}
_state_lock = threading.Lock()


def _model_path():
    for d in _model_dirs:
        p = d / _MODEL_NAME
        if p.exists():
            return p
    return None


def face_available():
    return (not _det_failed) and _model_path() is not None


def active(fx):
    if not fx or not fx.get("enabled", True):
        return False
    return any(int(fx.get(k, 0) or 0) != 0 for k in RANGES)


def _detector():
    global _det, _det_failed
    if _det is None and not _det_failed:
        try:
            _det = cv2.FaceDetectorYN.create(str(_model_path()), "", (320, 320), 0.7, 0.3, 5)
        except Exception:
            _det_failed = True
    return _det


def _detect(img):
    det = _detector()
    if det is None:
        return []
    h, w = img.shape[:2]
    s = 480.0 / max(h, w)
    small = cv2.resize(img, (max(32, int(w * s)), max(32, int(h * s))), interpolation=cv2.INTER_AREA) if s < 1 else img
    sh, sw = small.shape[:2]
    det.setInputSize((sw, sh))
    _, res = det.detect(small)
    if res is None:
        return []
    k = w / float(sw)
    out = [r[:14].astype(np.float32) * k for r in res]
    out.sort(key=lambda f: -f[2] * f[3])
    return out[:3]


def _faces_for(img, track):
    """Detect every 4th frame and smooth positions so the warp does not jitter."""
    global _faces, _miss, _tick
    if not track:
        return _detect(img)
    with _state_lock:
        _tick += 1
        if _tick % 4 == 1:
            new = _detect(img)
            if new:
                merged = []
                for f in new:
                    c = np.array([f[0] + f[2] / 2, f[1] + f[3] / 2])
                    best = None
                    for g in _faces:
                        if np.linalg.norm(c - np.array([g[0] + g[2] / 2, g[1] + g[3] / 2])) < 0.6 * f[2]:
                            best = g
                            break
                    merged.append(f if best is None else best * 0.55 + f * 0.45)
                _faces = merged
                _miss = 0
            else:
                _miss += 1
                if _miss > 8:
                    _faces = []
        return list(_faces)


def _warp_faces(img, faces, eye, slim):
    H, W = img.shape[:2]
    for f in faces:
        fx, fy, fw, fh = [float(v) for v in f[:4]]
        x0, x1 = int(max(0, fx - 0.35 * fw)), int(min(W, fx + 1.35 * fw))
        y0, y1 = int(max(0, fy - 0.3 * fh)), int(min(H, fy + 1.3 * fh))
        if x1 - x0 < 8 or y1 - y0 < 8:
            continue
        gx, gy = np.meshgrid(np.arange(x0, x1, dtype=np.float32), np.arange(y0, y1, dtype=np.float32))
        mx, my = gx.copy(), gy.copy()
        if eye > 0:
            s = min(0.55, eye / 100.0 * 0.55)
            r = 0.2 * fw
            for ex, ey in ((f[4], f[5]), (f[6], f[7])):
                dx, dy = gx - ex, gy - ey
                d2 = (dx * dx + dy * dy) / (r * r)
                k = np.where(d2 < 1, 1 - s * (1 - d2) ** 2, 1.0).astype(np.float32)
                mx += dx * (k - 1)
                my += dy * (k - 1)
        if slim > 0:
            kx = slim / 100.0 * 0.14
            cx = float(f[8])
            eye_y = (float(f[5]) + float(f[7])) / 2
            t = np.clip((gy - eye_y) / max(1.0, fy + fh - eye_y), 0, 1)
            wy = (np.sin(t * np.pi / 2) ** 1.5) * np.clip((fy + fh * 1.25 - gy) / (0.25 * fh), 0, 1)
            adx = np.abs(gx - cx)
            wx = np.clip(adx / (0.35 * fw), 0, 1) * np.clip((0.95 * fw - adx) / (0.4 * fw), 0, 1)
            mx += (gx - cx) * kx * wy * wx
        src = img[y0:y1, x0:x1].copy()
        img[y0:y1, x0:x1] = cv2.remap(src, mx - x0, my - y0, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


def _smooth(img, faces, smooth, tone):
    H, W = img.shape[:2]
    a = min(1.0, smooth / 100.0) * 0.9
    regions = []
    if faces:
        for f in faces:
            fx, fy, fw, fh = [float(v) for v in f[:4]]
            regions.append((int(max(0, fx - 0.2 * fw)), int(max(0, fy - 0.25 * fh)),
                            int(min(W, fx + 1.2 * fw)), int(min(H, fy + 1.25 * fh)), True))
    else:
        regions.append((0, 0, W, H, False))
    for x0, y0, x1, y1, ell in regions:
        roi = img[y0:y1, x0:x1]
        rh, rw = roi.shape[:2]
        if rw < 16 or rh < 16:
            continue
        sc = min(1.0, 480.0 / max(rw, rh))
        small = cv2.resize(roi, (max(8, int(rw * sc)), max(8, int(rh * sc))), interpolation=cv2.INTER_AREA) if sc < 1 else roi
        m = cv2.inRange(cv2.cvtColor(small, cv2.COLOR_BGR2YCrCb), (0, 133, 77), (255, 173, 127))
        if ell:
            em = np.zeros(m.shape, np.uint8)
            cv2.ellipse(em, (m.shape[1] // 2, m.shape[0] // 2), (int(m.shape[1] * 0.42), int(m.shape[0] * 0.46)), 0, 0, 360, 255, -1)
            m = cv2.bitwise_and(m, em)
        m = cv2.GaussianBlur(m, (0, 0), max(1.5, m.shape[1] / 60.0))
        if sc < 1:
            m = cv2.resize(m, (rw, rh), interpolation=cv2.INTER_LINEAR)
        mf = (m.astype(np.float32) / 255.0)[..., None]
        out = roi.astype(np.float32)
        if smooth > 0:
            sm = cv2.bilateralFilter(small, 7, 25 + 0.5 * smooth, 5 + 0.06 * smooth)
            if sc < 1:
                sm = cv2.resize(sm, (rw, rh), interpolation=cv2.INTER_LINEAR)
            out = out * (1 - mf * a) + sm.astype(np.float32) * (mf * a)
        if tone > 0:
            out = out + mf * (tone / 100.0 * 22.0)
        img[y0:y1, x0:x1] = np.clip(out, 0, 255).astype(np.uint8)


def _vignette_mask(w, h, v):
    key = (w, h, v)
    m = _vig_cache.get(key)
    if m is None:
        if len(_vig_cache) > 6:
            _vig_cache.clear()
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        d = np.sqrt(((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2) / 1.414
        m = (1 - (v / 100.0) * 0.75 * np.clip(d, 0, 1) ** 2.2)[..., None].astype(np.float32)
        _vig_cache[key] = m
    return m


def apply(frame, fx, track=True):
    """Return a processed copy of BGR frame (or the input itself when nothing to do / on error)."""
    try:
        if frame is None or not active(fx) or frame.ndim != 3 or frame.shape[2] != 3:
            return frame
        g = lambda k: int(fx.get(k, 0) or 0)
        img = frame.copy()
        eye, slim, smooth, tone = g("eye_enlarge"), g("face_slim"), g("skin_smooth"), g("skin_tone")
        faces = _faces_for(img, track) if (eye or slim or smooth or tone) and face_available() else []
        if faces and (eye or slim):
            _warp_faces(img, faces, eye, slim)
        if smooth or tone:
            _smooth(img, faces, smooth, tone)
        if g("soften"):
            k = g("soften") / 100.0 * 0.6
            img = cv2.addWeighted(img, 1 - k, cv2.GaussianBlur(img, (0, 0), 4), k, 0)
        b, c, t = g("brightness"), g("contrast"), g("temperature")
        if b or c or t:
            i = np.arange(256, dtype=np.float32)
            alpha = 1 + c / 100.0 if c >= 0 else 1 + c / 200.0
            base = (i - 128) * alpha + 128 + b * 1.27
            lut = np.stack([np.clip(base * gn, 0, 255) for gn in (1 - t / 250.0, 1.0, 1 + t / 250.0)], axis=-1)
            img = cv2.LUT(img, lut.astype(np.uint8).reshape(256, 1, 3))
        if g("saturation"):
            hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
            hsv[..., 1] = np.clip(hsv[..., 1].astype(np.float32) * (1 + g("saturation") / 100.0), 0, 255).astype(np.uint8)
            img = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
        if g("sharpen"):
            a2 = g("sharpen") / 100.0 * 1.2
            img = cv2.addWeighted(img, 1 + a2, cv2.GaussianBlur(img, (0, 0), 1.2), -a2, 0)
        if g("vignette"):
            h, w = img.shape[:2]
            img = np.clip(img.astype(np.float32) * _vignette_mask(w, h, g("vignette")), 0, 255).astype(np.uint8)
        return img
    except Exception:
        return frame


def _clean(body, cur):
    out = dict(cur)
    if body.get("reset"):
        out = dict(DEFAULT)
    p = body.get("preset")
    if p in PRESETS:
        out = {**DEFAULT, "enabled": out.get("enabled", True), **PRESETS[p]}
    for k, (lo, hi) in RANGES.items():
        if k in body:
            try:
                out[k] = int(max(lo, min(hi, round(float(body[k])))))
            except (TypeError, ValueError):
                pass
    if "enabled" in body:
        out["enabled"] = bool(body["enabled"])
    return out


def register(app, state, lock, web_dir, data_dir, get_frame):
    """Attach /api/fx*, /effects.js and the model folder. Call once from server.py."""
    _model_dirs.insert(0, Path(web_dir) / "models")
    store = Path(data_dir) / "fx.json"
    fx0 = dict(DEFAULT)
    try:
        fx0 = _clean(json.loads(store.read_text(encoding="utf-8")), DEFAULT)
    except Exception:
        pass
    with lock:
        state["fx"] = fx0
        state["fx_rev"] = 1

    def save():
        try:
            store.write_text(json.dumps(state["fx"]), encoding="utf-8")
        except Exception:
            pass

    @app.get("/effects.js")
    def fx_js():
        return send_from_directory(str(web_dir), "effects.js")

    @app.get("/api/fx")
    def fx_get():
        with lock:
            fx = dict(state["fx"])
        return jsonify(fx=fx, ranges=RANGES, presets=sorted(PRESETS), face_available=face_available())

    @app.post("/api/fx")
    def fx_set():
        body = request.get_json(silent=True) or {}
        with lock:
            state["fx"] = _clean(body, state["fx"])
            state["fx_rev"] = int(state.get("fx_rev", 0)) + 1
            fx = dict(state["fx"])
        save()
        return jsonify(ok=True, fx=fx)

    @app.get("/api/fx/preview.jpg")
    def fx_preview():
        fr = get_frame() if get_frame else None
        if fr is None:
            return Response(status=204)
        h, w = fr.shape[:2]
        s = min(1.0, 540.0 / max(h, w))
        small = cv2.resize(fr, (max(2, int(w * s)), max(2, int(h * s))), interpolation=cv2.INTER_AREA) if s < 1 else fr
        with lock:
            fx = dict(state["fx"])
        out = apply(small, fx, track=False)
        ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            return Response(status=204)
        return Response(buf.tobytes(), mimetype="image/jpeg", headers={"Cache-Control": "no-store"})
