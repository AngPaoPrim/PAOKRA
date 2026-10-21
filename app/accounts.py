"""PAOKRA account manager: TikTok Login Kit (OAuth) + secure local token storage.
Tokens/secrets are encrypted with Windows DPAPI and never returned by any API."""
import base64, ctypes, hashlib, json, os, secrets, threading, time, subprocess, re
import urllib.error, urllib.parse, urllib.request
from ctypes import wintypes
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("PAOKRA_DATA", str(DEFAULT_ROOT / "data"))).expanduser().resolve()
STORE = DATA / "accounts.json"
CFG = DATA / "tiktok_config.json"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
USER_URL = "https://open.tiktokapis.com/v2/user/info/?fields=open_id,avatar_url,display_name"
DEFAULT_REDIRECT = f"http://127.0.0.1:{os.environ.get('PAOKRA_PORT', '8797')}/auth/tiktok/callback"
BROWSER_LOGIN_URL = "https://shop.tiktok.com/streamer/live/product/dashboard"
BROWSER_PROFILES = DATA / "browser_profiles"
COOKIE_FILE = Path(os.environ.get("PAOKRA_COOKIE_FILE", str(DEFAULT_ROOT / "shop.tiktok.com_cookies.txt"))).expanduser().resolve()
CHROME_CANDIDATES = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
]
CAPS = dict(canPublish=False, canReadProfile=True, canReadComments=False, canStartLive=False,
            canStopLive=False, canReadLiveState=False, canControlProducts=False)
_lock = threading.RLock()
_pending, _results = {}, {}


class AuthError(Exception):
    pass


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    if os.name != "nt":
        raise AuthError("secure storage requires Windows (DPAPI)")
    buf = ctypes.create_string_buffer(data, len(data))
    bin_ = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(bin_), None, None, None, None, 0, ctypes.byref(out)):
        raise AuthError("secure storage failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def protect(text): return base64.b64encode(_dpapi(text.encode(), True)).decode()
def unprotect(b64): return _dpapi(base64.b64decode(b64), False).decode()


def _read(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write(path, obj):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _cfg():
    c = _read(CFG, {})
    sec = ""
    if c.get("secret_enc"):
        try:
            sec = unprotect(c["secret_enc"])
        except Exception:
            sec = ""
    return (c.get("client_key") or os.environ.get("TIKTOK_CLIENT_KEY", ""),
            sec or os.environ.get("TIKTOK_CLIENT_SECRET", ""),
            c.get("redirect_uri") or os.environ.get("TIKTOK_REDIRECT_URI") or DEFAULT_REDIRECT)


def config_public():
    k, s, r = _cfg()
    return dict(configured=bool(k and s), client_key=(k[:4] + "…") if k else "", redirect_uri=r)


def save_config(client_key, client_secret, redirect_uri):
    c = _read(CFG, {})
    client_key = (client_key or "").strip() or c.get("client_key", "")
    redirect_uri = (redirect_uri or "").strip() or DEFAULT_REDIRECT
    if not client_key:
        raise AuthError("ต้องระบุ Client key")
    if not redirect_uri.startswith(("http://", "https://")):
        raise AuthError("Redirect URI ไม่ถูกต้อง")
    c.update(client_key=client_key, redirect_uri=redirect_uri)
    if (client_secret or "").strip():
        c["secret_enc"] = protect(client_secret.strip())
    elif not c.get("secret_enc"):
        raise AuthError("ต้องระบุ Client secret")
    _write(CFG, c)


def _http(url, data=None, headers=None):
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    h = {"Content-Type": "application/x-www-form-urlencoded"} if data is not None else {}
    h.update(headers or {})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, body, h), timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            raise AuthError(f"TikTok ตอบกลับ HTTP {e.code}")
    except Exception as e:
        raise AuthError("เชื่อมต่อ TikTok ไม่สำเร็จ: " + type(e).__name__)


def _user(access):
    r = _http(USER_URL, headers={"Authorization": "Bearer " + access})
    err = r.get("error") or {}
    if err.get("code") not in (None, "", "ok"):
        raise AuthError(err.get("message") or err.get("code"))
    return (r.get("data") or {}).get("user") or {}


def start_auth():
    k, s, r = _cfg()
    if not (k and s):
        raise AuthError("ยังไม่ได้ตั้งค่า TikTok app (Client key/secret)")
    now = time.time()
    with _lock:
        for st in [x for x, v in _pending.items() if now - v[0] > 600]:
            _pending.pop(st, None)
        st = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(64)[:96]  # PKCE (43-128 chars)
        _pending[st] = (now, verifier)
        _results[st] = {"status": "pending"}
    q = urllib.parse.urlencode(dict(client_key=k, scope="user.info.basic", response_type="code",
                                    redirect_uri=r, state=st,
                                    code_challenge=hashlib.sha256(verifier.encode()).hexdigest(),
                                    code_challenge_method="S256"))
    return {"url": AUTH_URL + "?" + q, "state": st}


def result(state):
    return _results.get(state) or {"status": "unknown"}


def handle_callback(params):
    """Returns error message (str) or None on success."""
    st = params.get("state", "")
    with _lock:
        entry = _pending.pop(st, None)
    if entry is None:
        return "state ไม่ถูกต้องหรือหมดอายุ — เริ่มเชื่อมต่อใหม่จาก PAOKRA"
    try:
        if params.get("error"):
            raise AuthError(params.get("error_description") or params["error"])
        if not params.get("code"):
            raise AuthError("ไม่ได้รับ authorization code")
        k, s, r = _cfg()
        tok = _http(TOKEN_URL, dict(client_key=k, client_secret=s, code=params["code"],
                                    grant_type="authorization_code", redirect_uri=r,
                                    code_verifier=entry[1]))
        if "access_token" not in tok:
            raise AuthError(tok.get("error_description") or tok.get("error") or "แลก token ไม่สำเร็จ")
        acc = _save(tok)
        _results[st] = {"status": "ok", "account": public(acc)}
        return None
    except AuthError as e:
        _results[st] = {"status": "error", "message": str(e)}
        return str(e)


def _blob(tok):
    now = time.time()
    return dict(access_token=tok["access_token"], refresh_token=tok.get("refresh_token", ""),
                expires_at=now + int(tok.get("expires_in", 0)),
                refresh_expires_at=now + int(tok.get("refresh_expires_in", 0)), scope=tok.get("scope", ""))


def _save(tok):
    blob = _blob(tok)
    user = _user(blob["access_token"])
    oid = user.get("open_id") or tok.get("open_id")
    if not oid:
        raise AuthError("ไม่พบ open_id จาก TikTok")
    now = time.time()
    acc = dict(id="tt_" + hashlib.sha1(oid.encode()).hexdigest()[:10], platform="tiktok",
               displayName=user.get("display_name") or "TikTok user", avatar=user.get("avatar_url") or "",
               status="connected", authorizedAt=now, lastSync=now, capabilities=dict(CAPS),
               metadata=dict(scope=blob["scope"], error=None), token_enc=protect(json.dumps(blob)))
    with _lock:
        d = _read(STORE, {"accounts": []})
        d["accounts"] = [a for a in d["accounts"] if a["id"] != acc["id"]] + [acc]
        _write(STORE, d)
    return acc


def public(a): return {k: v for k, v in a.items() if k != "token_enc"}
def public_list(): return [public(a) for a in _read(STORE, {"accounts": []}).get("accounts", [])]


def _refresh(blob):
    if blob.get("refresh_expires_at", 0) < time.time() or not blob.get("refresh_token"):
        raise AuthError("สิทธิ์หมดอายุ ต้องเชื่อมต่อใหม่")
    k, s, _ = _cfg()
    r = _http(TOKEN_URL, dict(client_key=k, client_secret=s, grant_type="refresh_token",
                              refresh_token=blob["refresh_token"]))
    if "access_token" not in r:
        raise AuthError(r.get("error_description") or "refresh token ไม่สำเร็จ")
    return _blob(r)


def test(aid):
    with _lock:
        d = _read(STORE, {"accounts": []})
        a = next((x for x in d["accounts"] if x["id"] == aid), None)
        if not a:
            raise KeyError(aid)
        try:
            blob = json.loads(unprotect(a["token_enc"]))
            if blob["expires_at"] - 60 < time.time():
                blob = _refresh(blob)
            u = _user(blob["access_token"])
            a["token_enc"] = protect(json.dumps(blob))
            a.update(status="connected", lastSync=time.time(),
                     displayName=u.get("display_name") or a["displayName"], avatar=u.get("avatar_url") or a["avatar"])
            a["metadata"]["error"] = None
        except (AuthError, KeyError, ValueError) as e:
            a["status"] = "reauth_required"
            a["metadata"]["error"] = str(e)
        _write(STORE, d)
        return public(a)


def remove(aid):
    with _lock:
        d = _read(STORE, {"accounts": []})
        n = len(d["accounts"])
        d["accounts"] = [a for a in d["accounts"] if a["id"] != aid]
        _write(STORE, d)
        return len(d["accounts"]) < n


def _safe_profile_name(name):
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or "").strip())[:80]
    return name.strip("._") or "account"


def cookie_file_status():
    """Return safe metadata only; never expose cookie names or values."""
    try:
        if not COOKIE_FILE.is_file():
            return {"configured": False, "entries": 0}
        entries = 0
        with COOKIE_FILE.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and len(line.split("\t")) >= 7:
                    entries += 1
        return {"configured": entries > 0, "entries": entries}
    except Exception:
        return {"configured": False, "entries": 0}


def load_cookie_file():
    """Load Netscape cookies or common JSON cookie exports locally."""
    if not COOKIE_FILE.is_file():
        raise AuthError("ไม่พบ cookie file")
    raw = COOKIE_FILE.read_text(encoding="utf-8", errors="replace")
    cookies = []
    # Accept JSON exports from browser cookie extensions as well as Netscape txt.
    try:
        obj = json.loads(raw)
        rows = obj.get("cookies", obj) if isinstance(obj, dict) else obj
        if isinstance(rows, list):
            for x in rows:
                if not isinstance(x, dict) or not x.get("name") or not x.get("domain"):
                    continue
                c = {"name": str(x["name"]), "value": str(x.get("value", "")),
                     "domain": str(x["domain"]), "path": str(x.get("path") or "/"),
                     "secure": bool(x.get("secure", False)), "httpOnly": bool(x.get("httpOnly", False))}
                if x.get("expirationDate") is not None:
                    try: c["expires"] = float(x["expirationDate"])
                    except Exception: pass
                if x.get("sameSite") in ("Strict", "Lax", "None"):
                    c["sameSite"] = x["sameSite"]
                cookies.append(c)
    except Exception:
        pass
    if not cookies:
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            domain, flag, path, secure, expiration, name, value = parts[:7]
            if not domain or not name:
                continue
            c = {"name": name, "value": value, "domain": domain,
                 "path": path or "/", "secure": secure.upper() == "TRUE", "httpOnly": False}
            if expiration.isdigit() and int(expiration) > 0:
                c["expires"] = int(expiration)
            cookies.append(c)
    if not cookies:
        raise AuthError("cookie file ไม่มีรายการที่ใช้ได้")
    return cookies


def browser_profiles():
    BROWSER_PROFILES.mkdir(parents=True, exist_ok=True)
    return [{"name": p.name, "path": str(p), "dashboard": BROWSER_LOGIN_URL}
            for p in sorted(BROWSER_PROFILES.iterdir()) if p.is_dir()]


def browser_login(name):
    """Open a persistent, isolated Chrome profile for manual TikTok login.

    Chrome owns the session/cookies inside the profile. PAOKRA deliberately does
    not export, decrypt, copy, or expose raw TikTok cookies/tokens.
    """
    profile = _safe_profile_name(name)
    profile_dir = BROWSER_PROFILES / profile
    profile_dir.mkdir(parents=True, exist_ok=True)
    chrome = next((p for p in CHROME_CANDIDATES if p.exists()), None)
    if chrome is None:
        raise AuthError("หา Chrome ไม่พบ")
    try:
        subprocess.Popen([str(chrome),
                          f"--user-data-dir={profile_dir}",
                          "--profile-directory=Default",
                          BROWSER_LOGIN_URL],
                         close_fds=True)
    except Exception as e:
        raise AuthError("เปิด Chrome login ไม่สำเร็จ: " + type(e).__name__)
    return {"ok": True, "name": profile, "path": str(profile_dir), "url": BROWSER_LOGIN_URL}
