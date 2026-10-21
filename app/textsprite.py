"""Text rendering for the compositor (Pillow): any size, Thai-capable, cached sprites."""
import re, threading
import numpy as np
from PIL import Image, ImageDraw, ImageFont, features

FONTS = r"C:\Windows\Fonts"
# Keep the live compositor's historical font preference to avoid changing line
# wrapping/layout when switching from per-frame PIL drawing to cached sprites.
REGULAR = ["tahoma.ttf", "leelawui.ttf", "thsarabunnew.ttf", "arial.ttf"]
BOLD = ["tahomabd.ttf", "leelauib.ttf", "THSarabunNew Bold.ttf", "arialbd.ttf"]
LAYOUT = ImageFont.Layout.RAQM if features.check("raqm") else ImageFont.Layout.BASIC
_lock = threading.Lock()
_fonts, _sprites = {}, {}
MAX_CACHED_SPRITES = 256


def _font(size, bold):
    key = (size, bold)
    f = _fonts.get(key)
    if f is None:
        for name in (BOLD if bold else REGULAR):
            try:
                f = ImageFont.truetype(FONTS + "\\" + name, size, layout_engine=LAYOUT)
                break
            except Exception:
                continue
        if f is None:
            f = ImageFont.load_default()
        _fonts[key] = f
    return f


def sprite(text, size, color="#ffffff", bold=False, outline=True, stroke=None):
    """Return a cached BGRA sprite. A (stroke+2) transparent border permits exact placement."""
    text = str(text or "")
    color = color if re.fullmatch(r"#[0-9a-fA-F]{6}", color or "") else "#ffffff"
    size = max(6, min(400, int(size)))
    sw = (max(1, min(20, int(stroke))) if stroke is not None else max(1, size // 14)) if outline else 0
    key = (text, size, color, bool(bold), bool(outline), sw)
    with _lock:
        sp = _sprites.get(key)
        if sp is not None:
            # Ordered eviction: keep frequently used strings hot without clearing
            # every sprite when a scene has more than 64 text layers.
            _sprites.pop(key)
            _sprites[key] = sp
            return sp
        font = _font(size, bool(bold))
        pad = sw + 2
        probe = ImageDraw.Draw(Image.new("L", (1, 1)))
        _, _, r, b = probe.multiline_textbbox((0, 0), text, font=font, stroke_width=sw)
        img = Image.new("RGBA", (max(1, int(r) + 2 * pad), max(1, int(b) + 2 * pad)), (0, 0, 0, 0))
        rgb = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
        ImageDraw.Draw(img).multiline_text((pad, pad), text, font=font, fill=rgb + (255,),
                                           stroke_width=sw, stroke_fill=(0, 0, 0, 255))
        sp = np.ascontiguousarray(np.array(img)[:, :, [2, 1, 0, 3]])
        while len(_sprites) >= MAX_CACHED_SPRITES:
            _sprites.pop(next(iter(_sprites)))
        _sprites[key] = sp
        return sp


def blend(frame, ov, x, y):
    """Alpha-blend BGRA sprite onto BGR frame in place, clipped to the frame."""
    h, w = frame.shape[:2]
    oh, ow = ov.shape[:2]
    x0, y0, x1, y1 = max(0, x), max(0, y), min(w, x + ow), min(h, y + oh)
    if x1 <= x0 or y1 <= y0:
        return
    sub = ov[y0 - y:y1 - y, x0 - x:x1 - x]
    a = sub[:, :, 3:4].astype(np.float32) / 255.0
    base = frame[y0:y1, x0:x1].astype(np.float32)
    frame[y0:y1, x0:x1] = (base * (1 - a) + sub[:, :, :3] * a).astype(np.uint8)
