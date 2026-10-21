"""PAOKRA clip guard: duplicate detection + no-repeat rotation.

Duplicate detection (cheap enough for low-spec PCs, ~8 seeks per clip, cached):
  * exact  : same size + SHA1 of head / middle / tail 1 MB
  * near   : same clip re-encoded / resized / recoloured / mirrored / re-wrapped.
             8 evenly spaced frames -> 64-bit dHash (9x8 grayscale gradient).
             Mean Hamming distance <= NEAR_BITS and similar duration => duplicate.
             The mirrored hash is stored too, so a flipped copy is caught.
Rotation: pick the next clip so the same clip (or a near-duplicate of a recent
clip under another file name) never plays back-to-back.
"""
import hashlib
import json
import os
import random
import threading
import time
from pathlib import Path

import cv2
import numpy as np

NEAR_BITS = 10.0          # mean Hamming distance (of 64) below which two clips are "the same"
DUR_TOLERANCE = 0.04      # relative duration difference allowed for "near"
SAMPLES = 8
_CHUNK = 1024 * 1024
_lock = threading.RLock()


def _dhash(gray9x8):
    return int("".join("1" if b else "0" for b in (gray9x8[:, 1:] > gray9x8[:, :-1]).flatten()), 2)


def _hash_frame(frame):
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(g, (9, 8), interpolation=cv2.INTER_AREA)
    return _dhash(small), _dhash(small[:, ::-1])


def _file_digest(path):
    size = os.path.getsize(path)
    h = hashlib.sha1(str(size).encode())
    with open(path, "rb") as f:
        for pos in (0, max(0, size // 2 - _CHUNK // 2), max(0, size - _CHUNK)):
            f.seek(pos)
            h.update(f.read(_CHUNK))
    return size, h.hexdigest()


def fingerprint(path):
    """Return a JSON-serialisable fingerprint, or raise if the file is unreadable."""
    path = str(path)
    size, digest = _file_digest(path)
    fp = {"size": size, "sha1": digest, "frames": 0, "fps": 0.0, "duration": 0.0, "h": [], "hm": []}
    cap = cv2.VideoCapture(path, cv2.CAP_FFMPEG)
    try:
        if not cap.isOpened():
            return fp
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        fp["frames"], fp["fps"] = n, fps
        if n > 0 and fps > 0:
            fp["duration"] = n / fps
        if n >= SAMPLES:
            for i in range(SAMPLES):
                idx = int((i + 0.5) * n / SAMPLES)
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                ok, fr = cap.read()
                if not ok or fr is None:
                    continue
                a, b = _hash_frame(fr)
                fp["h"].append(a)
                fp["hm"].append(b)
        else:  # tiny clip / image-like: single frame
            ok, fr = cap.read()
            if ok and fr is not None:
                a, b = _hash_frame(fr)
                fp["h"].append(a)
                fp["hm"].append(b)
    finally:
        cap.release()
    return fp


def _ham(a, b):
    return bin(a ^ b).count("1")


def compare(a, b):
    """Return None or {'verdict','score','mirrored'}; a/b are fingerprints."""
    if a["size"] == b["size"] and a["sha1"] == b["sha1"]:
        return {"verdict": "exact", "score": 0.0, "mirrored": False}
    da, db = a.get("duration", 0.0), b.get("duration", 0.0)
    if da > 0 and db > 0 and abs(da - db) / max(da, db) > DUR_TOLERANCE:
        return None
    ha, hb = a.get("h", []), b.get("h", [])
    if not ha or not hb or len(ha) != len(hb):
        return None
    straight = sum(_ham(x, y) for x, y in zip(ha, hb)) / len(ha)
    mirrored = sum(_ham(x, y) for x, y in zip(ha, b.get("hm", []))) / len(ha)
    best, mir = (mirrored, True) if mirrored < straight else (straight, False)
    if best <= NEAR_BITS:
        return {"verdict": "near", "score": round(best, 2), "mirrored": mir}
    return None


class Library:
    """Fingerprint cache for a media folder (keyed by file name, invalidated by size+mtime)."""

    def __init__(self, folder, exts):
        self.folder = Path(folder)
        self.exts = set(exts)
        self.cache_file = self.folder / ".fingerprints.json"
        self._db = {}
        self._load()

    def _load(self):
        try:
            self._db = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except Exception:
            self._db = {}

    def _save(self):
        try:
            tmp = self.cache_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._db), encoding="utf-8")
            os.replace(tmp, self.cache_file)
        except Exception:
            pass

    def get(self, name):
        p = self.folder / name
        try:
            st = p.stat()
        except OSError:
            return None
        with _lock:
            ent = self._db.get(name)
            if ent and ent["size"] == st.st_size and ent["mtime"] == st.st_mtime_ns:
                return ent["fp"]
        fp = fingerprint(p)
        with _lock:
            self._db[name] = {"size": st.st_size, "mtime": st.st_mtime_ns, "fp": fp}
            self._save()
        return fp

    def record(self, name, fp):
        p = self.folder / name
        try:
            st = p.stat()
        except OSError:
            return
        with _lock:
            self._db[name] = {"size": st.st_size, "mtime": st.st_mtime_ns, "fp": fp}
            self._save()

    def names(self):
        out = []
        try:
            for f in sorted(self.folder.iterdir()):
                if f.is_file() and f.suffix.lower() in self.exts and not f.name.startswith("."):
                    out.append(f.name)
        except OSError:
            pass
        return out

    def find_matches(self, fp, exclude=None):
        res = []
        for name in self.names():
            if exclude and name == exclude:
                continue
            other = self.get(name)
            if not other:
                continue
            c = compare(fp, other)
            if c:
                res.append({"name": name, **c})
        res.sort(key=lambda r: (r["verdict"] != "exact", r["score"]))
        return res

    def groups(self):
        """Duplicate groups across the whole library (each group = list of names)."""
        names = self.names()
        fps = {n: self.get(n) for n in names}
        seen, groups = set(), []
        for i, a in enumerate(names):
            if a in seen or not fps[a]:
                continue
            grp = [a]
            for b in names[i + 1:]:
                if b in seen or not fps[b]:
                    continue
                if compare(fps[a], fps[b]):
                    grp.append(b)
            if len(grp) > 1:
                seen.update(grp)
                groups.append(grp)
        return groups


def pick_next(candidates, history, library=None, avoid_recent=3, rng=random):
    """Choose the next clip name.

    candidates : playlist names
    history    : names played so far, oldest -> newest (last item = current clip)
    Never returns the current clip or a near-duplicate of it when any alternative exists;
    prefers clips not seen in the last `avoid_recent` plays and the least recently played.
    """
    cands = [c for c in candidates]
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    current = history[-1] if history else None

    def same(a, b):
        if a == b:
            return True
        if not library:
            return False
        fa, fb = library.get(a), library.get(b)
        return bool(fa and fb and compare(fa, fb))

    k = max(0, min(int(avoid_recent), len(cands) - 1))
    for window in range(k, -1, -1):
        recent = history[-window:] if window else []
        block = list(recent)
        if current and current not in block:
            block.append(current)
        pool = [c for c in cands if not any(same(c, r) for r in block)]
        if pool:
            last_seen = {}
            for i, h in enumerate(history):
                last_seen[h] = i
            pool.sort(key=lambda c: last_seen.get(c, -1))   # never-played first, then oldest
            best = last_seen.get(pool[0], -1)
            top = [c for c in pool if last_seen.get(c, -1) == best]
            return rng.choice(top)
    # everything is a duplicate of the current clip: still avoid the identical file
    alt = [c for c in cands if c != current]
    return rng.choice(alt) if alt else cands[0]
