import json, re, sqlite3, threading, time, hashlib, collections, unicodedata
from pathlib import Path
from dataclasses import dataclass
from typing import Callable, Optional

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
DATA = Path(__import__("os").environ.get("PAOKRA_DATA", str(DEFAULT_ROOT / "data"))).expanduser().resolve()
DB = DATA / "comments.db"
MAX_TEXT = 500
DEFAULT_CONFIG = {
    "reader_enabled": False, "auto_reply": False, "dry_run": True,
    "sensitivity": "strict", "match_mode": "contains",
    "poll_ms": 1500, "min_interval_sec": 8,
    "user_cooldown_sec": 60, "max_per_minute": 6,
    "rules": [], "blocked_terms": [], "blocked_response_terms": []
}

def _conn():
    DATA.mkdir(parents=True, exist_ok=True)
    c=sqlite3.connect(DB, timeout=10)
    c.row_factory=sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS comments(
      id TEXT PRIMARY KEY, username TEXT, text TEXT, seen_at REAL,
      status TEXT DEFAULT 'new', reply TEXT DEFAULT '', rule TEXT DEFAULT '',
      source TEXT DEFAULT '' )""")
    c.execute("""CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY,v TEXT)""")
    c.commit(); return c

def _config():
    c=_conn(); row=c.execute("SELECT v FROM meta WHERE k='config'").fetchone(); c.close()
    try: return {**DEFAULT_CONFIG, **(json.loads(row[0]) if row else {})}
    except Exception: return dict(DEFAULT_CONFIG)

def save_config(patch):
    cfg=_config(); cfg.update(patch)
    cfg["poll_ms"]=max(800,min(10000,int(cfg.get("poll_ms",1500))))
    cfg["min_interval_sec"]=max(2,min(300,int(cfg.get("min_interval_sec",8))))
    cfg["user_cooldown_sec"]=max(5,min(3600,int(cfg.get("user_cooldown_sec",60))))
    cfg["max_per_minute"]=max(1,min(30,int(cfg.get("max_per_minute",6))))
    c=_conn(); c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('config',?)",(json.dumps(cfg,ensure_ascii=False),)); c.commit(); c.close()
    return cfg


def normalize(s):
    s=unicodedata.normalize("NFKC",str(s or ""))
    return re.sub(r"\s+"," ",s).strip().casefold()

def _safe(s): return str(s or "").replace("\x00"," ").strip()[:MAX_TEXT]

def _hash(username,text,seen):
    raw=f"{normalize(username)}\0{normalize(text)}\0{int(seen//3)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]

def add_comment(username,text,source="dom",comment_id=None):
    username=_safe(username) or "Unknown"; text=_safe(text)
    if not text: return None,False
    now=time.time(); cid=str(comment_id or _hash(username,text,now))
    c=_conn(); cur=c.execute("INSERT OR IGNORE INTO comments(id,username,text,seen_at,source) VALUES(?,?,?,?,?)",(cid,username,text,now,source)); c.commit(); c.close()
    return cid, bool(cur.rowcount)
def recent_comments(limit=100):
    c=_conn(); rows=c.execute("SELECT id,username,text,seen_at,status,reply,rule FROM comments ORDER BY seen_at DESC LIMIT ?",(max(1,min(500,int(limit))),)).fetchall(); c.close()
    return [dict(r) for r in rows]

def mark_comment(cid,status,reply="",rule=""):
    c=_conn(); c.execute("UPDATE comments SET status=?,reply=?,rule=? WHERE id=?",(status,_safe(reply),_safe(rule),cid)); c.commit(); c.close()

def get_comment(cid):
    c=_conn(); r=c.execute("SELECT id,username,text,status FROM comments WHERE id=?",(cid,)).fetchone(); c.close(); return dict(r) if r else None

def _blocked(text,terms):
    n=normalize(text)
    return any(normalize(t) and normalize(t) in n for t in (terms or []))

def _loose(s):
    return "".join(ch for ch in normalize(s) if ch.isalnum())
def _keyword_hit(text,keyword,mode,sensitivity):
    if mode=="regex":
        if len(keyword)>200: return False
        try: return bool(re.search(keyword,text,re.I))
        except re.error: return False
    if sensitivity=="loose":
        a,b=_loose(text),_loose(keyword)
    else:
        a,b=normalize(text),normalize(keyword)
    if not b: return False
    if mode=="exact": return a==b
    if mode=="prefix": return a.startswith(b)
    return b in a

def match_rule(text,cfg):
    mode=cfg.get("match_mode","contains"); sensitivity=cfg.get("sensitivity","strict")
    for rule in cfg.get("rules",[]):
        if not rule.get("enabled",True): continue
        keys=[k.strip() for k in str(rule.get("keywords","")).split(",") if k.strip()]
        try:
            hits=[_keyword_hit(text,k,mode,sensitivity) for k in keys]
        except Exception:
            hits=[]
        if not hits: continue
        # strict = every configured keyword must match; normal = any keyword;
        # loose = punctuation/spacing-tolerant any-keyword matching.
        if sensitivity=="strict": hit=all(hits)
        else: hit=any(hits)
        if hit: return rule
    return None

class CommentEngine:
    def __init__(self, page_url, cookie_loader:Callable, window_hook:Optional[Callable]=None):
        self.page_url=page_url; self.cookie_loader=cookie_loader; self.window_hook=window_hook
        self._thread=None; self._stop=threading.Event(); self._wake=threading.Event()
        self._reply_q=collections.deque(); self._lock=threading.RLock()
        self.state={"running":False,"connected":False,"url":"","title":"","last_poll":None,"last_error":None,"found":0,"sent":0,"dry_run":True}
        self._sent_times=collections.deque(); self._user_last={}; self._page=None
    def snapshot(self):
        with self._lock: s=dict(self.state)
        s["thread_alive"]=bool(self._thread and self._thread.is_alive())
        s["config"]=_config(); s["comments"]=recent_comments(100); return s
    def set_enabled(self,enabled):
        save_config({"reader_enabled":bool(enabled)})
        if enabled: self.start()
        return self.snapshot()
    def enqueue_reply(self,cid,text):
        text=_safe(text)
        if not text: raise ValueError("reply required")
        if not get_comment(cid): raise ValueError("comment not found")
        with self._lock: self._reply_q.append((cid,text,"manual"))
        self.start(); return True
    def start(self):
        save_config({"reader_enabled":True})
        if self._thread and self._thread.is_alive(): return
        self._stop.clear(); self._set_state(running=True,last_error=None)
        self._thread=threading.Thread(target=self._run,name="paokra-comment-worker",daemon=True); self._thread.start()
    def stop(self):
        save_config({"reader_enabled":False}); self._stop.set(); self._wake.set()
        self._set_state(connected=False,last_error=None)
    def _run(self):
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser=pw.chromium.launch(headless=False,args=["--window-size=900,650","--window-position=-1200,80","--disable-features=TranslateUI"])
                context=browser.new_context(viewport={"width":860,"height":560})
                context.add_cookies(self.cookie_loader())
                page=context.new_page(); self._page=page
                try:
                    page.goto(self.page_url,wait_until="domcontentloaded",timeout=30000)
                    page.wait_for_timeout(5000)
                    if not page.locator("body").inner_text(timeout=3000).strip():
                        page.goto("https://shop.tiktok.com/streamer/welcome",wait_until="domcontentloaded",timeout=30000); page.wait_for_timeout(4000)
                except Exception as e:
                    self._set_error(type(e).__name__+": "+str(e)[:220])
                if self.window_hook:
                    try:self.window_hook(page)
                    except Exception:pass
                self._set_state(running=True,url=page.url,title=page.title(),connected=True)
                while not self._stop.is_set() and _config().get("reader_enabled",False):
                    cfg=_config()
                    self._set_state(dry_run=bool(cfg.get("dry_run",True)),url=page.url,title=page.title())
                    try:
                        self._poll_page(page,cfg); self._process_reply_queue(page,cfg)
                        self._set_state(last_poll=time.time(),last_error=None)
                    except Exception as e: self._set_error(type(e).__name__+": "+str(e)[:220])
                    self._wake.wait(max(.8,float(cfg.get("poll_ms",1500))/1000)); self._wake.clear()
                try: context.close(); browser.close()
                except Exception: pass
        except Exception as e: self._set_error(type(e).__name__+": "+str(e)[:220])
        finally:
            self._page=None
            self._set_state(running=False,connected=False)
    def _set_state(self,**kw):
        with self._lock:self.state.update(kw)
    def _set_error(self,msg): self._set_state(last_error=msg,connected=False)

    def _poll_page(self,page,cfg):
        self._set_state(connected=True)
        candidates=_extract_candidates(page)
        new_count=0
        for item in candidates:
            cid,created=add_comment(item["username"],item["text"],item.get("source","dom"),item.get("id"))
            if not cid or not created: continue
            new_count+=1
            self._auto_decide(cid,item["username"],item["text"],cfg)
        if new_count:
            with self._lock:self.state["found"] += new_count

    def _auto_decide(self,cid,username,text,cfg):
        if not cfg.get("auto_reply") or cfg.get("dry_run",True): return
        if _blocked(text,cfg.get("blocked_terms")): mark_comment(cid,"blocked"); return
        rule=match_rule(text,cfg)
        if not rule: mark_comment(cid,"ignored"); return
        if _blocked(rule.get("reply",""),cfg.get("blocked_response_terms")): mark_comment(cid,"blocked_response",rule=rule.get("keywords","")); return
        now=time.time(); user=normalize(username)
        with self._lock:
            while self._sent_times and now-self._sent_times[0]>60:self._sent_times.popleft()
            allowed=(not self._sent_times or now-self._sent_times[-1]>=cfg.get("min_interval_sec",8)) and len(self._sent_times)<cfg.get("max_per_minute",6) and now-self._user_last.get(user,0)>=cfg.get("user_cooldown_sec",60)
        if not allowed: mark_comment(cid,"rate_limited",rule=rule.get("keywords","")); return
        with self._lock:self._reply_q.append((cid,str(rule.get("reply",""))[:MAX_TEXT],"auto"))
        mark_comment(cid,"queued",rule=rule.get("keywords",""))
    def _process_reply_queue(self,page,cfg):
        if not self._reply_q: return
        cid,text,kind=self._reply_q.popleft(); row=get_comment(cid)
        if not row:return
        if cfg.get("dry_run",True): mark_comment(cid,"dry_run",reply=text); return
        if kind=="auto" and not cfg.get("auto_reply"): mark_comment(cid,"approval_needed"); return
        now=time.time(); user=normalize(row.get("username",""))
        with self._lock:
            while self._sent_times and now-self._sent_times[0]>60:self._sent_times.popleft()
            if self._sent_times and now-self._sent_times[-1]<cfg.get("min_interval_sec",8): self._reply_q.appendleft((cid,text,kind)); return
            if len(self._sent_times)>=cfg.get("max_per_minute",6): self._reply_q.appendleft((cid,text,kind)); return
            if kind=="auto" and now-self._user_last.get(user,0)<cfg.get("user_cooldown_sec",60): mark_comment(cid,"rate_limited"); return
        ok,err=_reply_via_dom(page,row["text"],text)
        if ok:
            mark_comment(cid,"sent",reply=text); with_lock=self._lock
            with with_lock:self._sent_times.append(now);self._user_last[user]=now;self.state["sent"]+=1
        else: mark_comment(cid,"send_failed",reply=text,rule=err)


def _extract_candidates(page):
    js=r'''() => {
      const out=[]; const seen=new Set();
      const sels=[
        '[data-e2e="comment-level-1"]','[data-e2e*="comment" i]',
        '[class*="comment" i]','[id*="comment" i]'
      ];
      const roots=[document];
      const walk=(root)=>{try{root.querySelectorAll('*').forEach(el=>{if(el.shadowRoot)roots.push(el.shadowRoot)})}catch{}};
      walk(document);
      for(const root of roots){
        for(const el of root.querySelectorAll(sels.join(','))){
          const text=(el.innerText||el.textContent||'').replace(/\s+/g,' ').trim();
          if(!text || text.length>500) continue;
          const user=(el.getAttribute('data-username')||el.getAttribute('data-user-name')||el.getAttribute('title')||'').trim();
          const id=(el.getAttribute('data-comment-id')||el.getAttribute('data-id')||el.getAttribute('data-e2e-id')||'').trim();
          const key=(id||text+'|'+user);
          if(seen.has(key))continue; seen.add(key);
          out.push({username:user||'Viewer',text,id,source:'dom'});
        }
      }
      return out.slice(-100);
    }'''
    rows=page.evaluate(js)
    return [x for x in (rows or []) if isinstance(x,dict) and str(x.get('text','')).strip()]


def _reply_via_dom(page,comment_text,reply):
    try:
        loc=page.get_by_text(comment_text,exact=True).first
        loc.scroll_into_view_if_needed(timeout=3000); loc.click(timeout=3000)
        page.wait_for_timeout(250)
        buttons=page.locator('button,[role="button"]')
        names=["Reply","ตอบกลับ","返信","回复"]
        clicked=False
        for name in names:
            try: buttons.get_by_text(name,exact=True).last.click(timeout=700); clicked=True; break
            except Exception: pass
        if not clicked:
            try: buttons.filter(has_text=re.compile("reply|ตอบ",re.I)).last.click(timeout=900); clicked=True
            except Exception: pass
        box=page.locator('textarea,[contenteditable="true"],[role="textbox"],input[placeholder*="reply" i]').last
        box.fill(reply,timeout=3000); box.press("Enter"); page.wait_for_timeout(250)
        return True,""
    except Exception as e:return False,type(e).__name__+": "+str(e)[:160]
def engine_config(): return _config()

def engine_status():
    return {"config":_config(),"comments":recent_comments(100)}

# Validate persisted database/config at import time without exposing secrets.
try: save_config({})
except Exception: pass
