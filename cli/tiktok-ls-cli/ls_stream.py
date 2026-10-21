"""ls_stream.py — set LIVE title/topic via CDP and start streaming.

Protocol: CDP (Chromium DevTools Protocol).
  ui mode:       window is visible → click title button → edit → save → go live
  headless mode: show_window → click title button → edit → save → hide_window → go live
                 (window visible only while interacting, then hidden again)

All DOM interactions use CDP Input.dispatchMouseEvent / React nativeInputValueSetter.
No OS mouse or keyboard involved.
"""
import json, time, urllib.request
from ls_golive import _CDP, _ui, _targets, _page, _buttons, _click_best, _ensure, _finish, resolve_mode

# ── Selectors (confirmed from live CDP dump) ──────────────────────────────────
_SEL_TITLE_INPUT  = "textarea[placeholder]"   # first textarea in the title dialog
_SEL_TOPIC_SEARCH = "input.rc-select-selection-search-input,input[type=search]"

_SAVE_NAMES = ["ยืนยัน","บันทึก","Save","Confirm","OK","ตกลง","Done","Apply","ใช้","เสร็จสิ้น","เสร็จ"]

# ── JS: find title dialog (tt-live-modalV4 containing a textarea) ─────────────
_JS_FIND_DIALOG = r"""(()=>{
  const d=[...document.querySelectorAll('.tt-live-modalV4')]
    .find(m=>m.querySelector('textarea[placeholder]'));
  if(!d) return null;
  const b=d.getBoundingClientRect();
  const ta=d.querySelector('textarea[placeholder]');
  const search=d.querySelector('input[type=search],input.rc-select-selection-search-input');
  const btns=[...d.querySelectorAll('button,[role=button]')]
    .filter(e=>e.offsetWidth||e.offsetHeight)
    .map(e=>{const r=e.getBoundingClientRect();
      return {t:(e.innerText||'').trim().replace(/\s+/g,' ').slice(0,40),
              x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};});
  return JSON.stringify({found:true,
    title_val:ta?(ta.value||ta.innerText||''):'',
    title_ph:ta?ta.placeholder:'',
    has_search:!!search,
    buttons:btns.slice(0,12)});
})()"""

# ── JS: React-safe value setter ───────────────────────────────────────────────
def _js_set_value(selector, value):
    return r"""(()=>{
  const e=document.querySelector(%s);
  if(!e) return 'not_found';
  e.focus();
  const proto=e.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
  const desc=Object.getOwnPropertyDescriptor(proto,'value');
  if(desc&&desc.set) desc.set.call(e,%s);
  else e.value=%s;
  e.dispatchEvent(new Event('input',{bubbles:true}));
  e.dispatchEvent(new Event('change',{bubbles:true}));
  return 'ok:'+e.value.slice(0,30);
})()""" % (json.dumps(selector), json.dumps(value), json.dumps(value))

# ── JS: click matching topic from dropdown ────────────────────────────────────
def _js_click_topic(query):
    return r"""((q)=>{
  const sels=['.rc-select-item-option-content','[class*=option-content]','[class*=topic-name]',
              '[class*=game-name]','[role=option]','li[class*=option]'];
  for(const s of sels){
    const items=[...document.querySelectorAll(s)]
      .filter(e=>e.offsetHeight&&(e.innerText||'').toLowerCase().includes(q.toLowerCase()));
    if(items.length){items[0].click();return (items[0].innerText||'').trim().slice(0,40);}
  }
  return null;
})(%s)""" % json.dumps(query)

# ── Helpers ───────────────────────────────────────────────────────────────────
def _open_dialog_and_wait(cdp, retries=2):
    """Click the title button (coord 662,25) and wait for the title dialog."""
    for attempt in range(retries):
        cdp.click(662, 25)
        deadline = time.time() + 3.0
        while time.time() < deadline:
            time.sleep(0.4)
            v = cdp.eval(_JS_FIND_DIALOG)
            if v:
                d = json.loads(v)
                if d.get("found") and d.get("title_ph"):
                    return d
        time.sleep(0.5)
    return None

def _find_save_btn(cdp):
    v = cdp.eval(r"""(()=>{
  const names=%s;
  const d=[...document.querySelectorAll('.tt-live-modalV4')]
    .find(m=>m.querySelector('textarea[placeholder]'));
  if(!d) return null;
  const btn=[...d.querySelectorAll('button,[role=button]')]
    .filter(e=>e.offsetWidth||e.offsetHeight)
    .find(e=>names.some(n=>(e.innerText||'').trim().includes(n)));
  if(!btn) return null;
  const r=btn.getBoundingClientRect();
  return JSON.stringify({t:(btn.innerText||'').trim().slice(0,40),
    x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)});
})()""" % json.dumps(_SAVE_NAMES, ensure_ascii=False))
    return json.loads(v) if v else None

def _headless_show(backend):
    if backend and hasattr(backend, "show_window"):
        backend.show_window()
        time.sleep(0.6)

def _headless_hide(backend, mode):
    if mode == "headless" and backend and hasattr(backend, "hide_window"):
        try: backend.hide_window()
        except Exception: pass

# ── Public API ────────────────────────────────────────────────────────────────

def get_live_info(cfg):
    """Return current title and topic from LIVE Studio header."""
    u = _ui(cfg)
    if _targets(u) is None:
        return {"ok": False, "error": "no_debug_port",
                "hint": "Run `tiktokls studio start` first."}
    main = _page(u, "main/index")
    if not main:
        return {"ok": False, "error": "main_page_not_found"}
    try:
        c = _CDP(main["webSocketDebuggerUrl"])
        v = c.eval(r"""(()=>{
          const t=document.querySelector('[class*=live-info-upgrade-title-r]');
          const tp=document.querySelector('[class*=live-info-upgrade-topic]');
          return JSON.stringify({title:t?(t.innerText||'').trim():null,
                                  topic:tp?(tp.innerText||'').trim():null});
        })()""")
        c.close()
        d = json.loads(v) if v else {}
        return {"ok": True, "title": d.get("title"), "topic": d.get("topic")}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def set_live_title(cfg, title, topic=None, backend=None, mode=None):
    """
    Set LIVE title (and optionally topic/game) via CDP.
    Headless-safe: if mode=='headless', shows window → edits → hides window.
    """
    mode = resolve_mode(cfg, mode)
    u = _ui(cfg)

    if _targets(u) is None:
        return {"ok": False, "error": "no_debug_port",
                "hint": "Run `tiktokls studio start` first."}
    main = _page(u, "main/index")
    if not main:
        return {"ok": False, "error": "main_page_not_found"}

    # Show window for headless
    if mode == "headless":
        _headless_show(backend)

    try:
        c = _CDP(main["webSocketDebuggerUrl"])

        # 1. Open title dialog
        dialog = _open_dialog_and_wait(c)
        if not dialog:
            c.close()
            return {"ok": False, "error": "title_dialog_not_opened",
                    "hint": "Title edit dialog did not appear. "
                            "Studio may already be live (title is locked while live), "
                            "or the button position changed."}

        # 2. Set title via React-safe setter
        r = c.eval(_js_set_value(_SEL_TITLE_INPUT, title))
        if r == "not_found":
            r = c.eval(_js_set_value("textarea", title))
        time.sleep(0.25)

        # 3. Set topic (optional)
        topic_clicked = None
        if topic:
            sr = c.eval(_js_set_value(_SEL_TOPIC_SEARCH, topic))
            if sr != "not_found":
                time.sleep(1.0)
                topic_clicked = c.eval(_js_click_topic(topic))
                time.sleep(0.3)

        # 4. Click Save button
        save_btn = _find_save_btn(c)
        if save_btn:
            c.click(save_btn["x"], save_btn["y"])
            saved_via = save_btn["t"]
        else:
            c.call("Input.dispatchKeyEvent", {"type":"keyDown","key":"Enter","code":"Enter"})
            c.call("Input.dispatchKeyEvent", {"type":"keyUp","key":"Enter","code":"Enter"})
            saved_via = "enter_key"
        time.sleep(0.4)
        c.close()

        # 5. Verify
        info = get_live_info(cfg)
        return {"ok": True, "set_title": title, "set_topic": topic,
                "topic_matched": topic_clicked, "save_button": saved_via,
                "current": info}

    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        _headless_hide(backend, mode)

def go_live_with_title(cfg, backend, title, topic=None, confirm=False,
                        timeout=40, mode=None, restart=False):
    """
    Combined: set title/topic → go LIVE (headless-safe).
    """
    import ls_golive
    if not confirm:
        return {"ok": False, "error": "confirm_required",
                "note": "This starts a real public broadcast. Re-run with --confirm."}

    mode = resolve_mode(cfg, mode)
    u = _ui(cfg)

    # Ensure Studio is up with CDP port
    e = _ensure(cfg, backend, mode, restart)
    if not e.get("ok"):
        return {**e, "step": "ensure"}

    # Show window for headless (keep it visible through both set-title and go-live)
    if mode == "headless" and hasattr(backend, "show_window"):
        backend.show_window()
        time.sleep(0.8)

    try:
        # Set title (pass mode=ui so it won't show/hide again inside)
        tr = set_live_title(cfg, title, topic, backend=None, mode="ui")
        if not tr.get("ok"):
            return {**tr, "step": "set_title"}

        time.sleep(0.5)

        # Go live
        if ls_golive.live_state(cfg) == "live":
            return {"ok": True, "state": "live", "action": "already_live",
                    "title_set": tr.get("current")}

        end_t = time.time() + timeout
        clicks = []
        while time.time() < end_t and len(clicks) < 5:
            main = _page(u, "main/index")
            b = _buttons(main, u["go_live_names"]) if main else []
            if not b:
                time.sleep(1.5); continue
            clicks.append(_click_best(main, b))
            for _ in range(6):
                time.sleep(1.4)
                if ls_golive.live_state(cfg) == "live":
                    return {"ok": True, "state": "live", "clicks": len(clicks),
                            "mode": mode, "title_set": tr.get("current")}

        return {"ok": False, "state": ls_golive.live_state(cfg),
                "clicks": len(clicks), "mode": mode,
                "title_set": tr.get("current"),
                "hint": "Title set but Go LIVE not confirmed. Check LIVE Studio for dialogs."}
    finally:
        _finish(backend, mode)  # hides window if headless
