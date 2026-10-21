/* PAOKRA Smooth preview
 * Browser-side compositor for the Live Console "Program" box.
 *
 * Instead of JPEG-encoding every composed frame in Python (MJPEG), the browser
 * plays the same source file in a GPU-decoded <video> and draws the same
 * layers (image / text / web) from /api/smooth, in output-pixel coordinates
 * scaled with one CSS transform.  The loop position is derived from the
 * server's own presentation clock (origin + loop_duration), so the preview,
 * the Virtual Camera and the Virtual Audio all share ONE clock.
 *
 * Falls back to /preview.mjpg automatically when the source cannot be played
 * by the browser (mkv/avi/url/camera, unsupported codec) or when switched off.
 */
(function () {
  "use strict";
  var LS_KEY = "paokra.smoothPreview";
  var POLL_MS = 300;
  var NATIVE_VIDEO = /\.(mp4|m4v|webm)$/i;
  var IMAGE_EXT = /\.(jpe?g|png|webp|bmp|gif)$/i;
  var LOCAL_SRC = /^(?:[A-Za-z]:[\\/]|\/media\/)/;

  var enabled = true;
  try { enabled = localStorage.getItem(LS_KEY) !== "0"; } catch (e) {}

  var consoleView, box, mjpg, dashboardView, dashboardImg, activeHost = null, wrap, canvas;
  var srcWrap, video, srcImg, imgBox, imgEl, textBox, webImg, svgFilter;
  var chk, statEl;
  var built = false, active = false, reason = "", st = null, recvPerf = 0;
  var curVideoUrl = "", curImgUrl = "", curSrcImgUrl = "", curWebUrl = "";
  var videoFailed = false, lastRunning = null, lastSerial = null;
  var textEls = new Map();
  var inflight = false, timer = 0, ro = null;
  var m = { fps: 0, drift: 0, dropped: 0, total: 0, frames: 0, t0: 0, polls: 0, hardSeeks: 0 };

  function $(id) { return document.getElementById(id); }

  function build() {
    consoleView = $("v-console");
    box = consoleView && consoleView.querySelector(".progbox");
    dashboardView = $("v-dashboard");
    dashboardImg = dashboardView && dashboardView.querySelector("img.thumb[data-stream]");
    if (!box || !dashboardImg) return false;
    mjpg = box.querySelector("img");
    box.style.position = "relative";

    wrap = document.createElement("div");
    wrap.id = "smoothWrap";
    wrap.hidden = true;
    wrap.style.cssText = "position:absolute;inset:0;overflow:hidden;background:#000";
    canvas = document.createElement("div");
    canvas.style.cssText = "position:absolute;left:0;top:0;transform-origin:0 0;background:#000;overflow:hidden;will-change:transform";
    wrap.appendChild(canvas);

    srcWrap = document.createElement("div");
    srcWrap.style.cssText = "position:absolute;overflow:hidden;background:#000";
    video = document.createElement("video");
    video.muted = true; video.defaultMuted = true; video.playsInline = true;
    video.preload = "auto"; video.disablePictureInPicture = true;
    video.setAttribute("muted", ""); video.setAttribute("playsinline", "");
    video.style.cssText = "position:absolute;left:0;top:0;width:100%;height:100%;display:none;object-fit:cover";
    srcImg = document.createElement("img");
    srcImg.style.cssText = "position:absolute;left:0;top:0;width:100%;height:100%;display:none;object-fit:cover";
    srcWrap.appendChild(video); srcWrap.appendChild(srcImg);

    imgBox = document.createElement("div");
    imgBox.style.cssText = "position:absolute;overflow:hidden;display:none";
    imgEl = document.createElement("img");
    imgEl.style.cssText = "position:absolute;max-width:none;max-height:none;user-select:none;-webkit-user-drag:none";
    imgBox.appendChild(imgEl);

    textBox = document.createElement("div");
    textBox.style.cssText = "position:absolute;left:0;top:0;right:0;bottom:0;pointer-events:none;display:none";

    webImg = document.createElement("img");
    webImg.style.cssText = "position:absolute;display:none;object-fit:fill;max-width:none;max-height:none";

    canvas.appendChild(srcWrap); canvas.appendChild(webImg);
    canvas.appendChild(imgBox); canvas.appendChild(textBox);
    box.appendChild(wrap);

    // exact linear transform used by Python's cv2.convertScaleAbs(alpha, beta)
    var ns = "http://www.w3.org/2000/svg";
    svgFilter = document.createElementNS(ns, "svg");
    svgFilter.setAttribute("width", "0"); svgFilter.setAttribute("height", "0");
    svgFilter.style.cssText = "position:absolute;width:0;height:0";
    svgFilter.innerHTML =
      '<filter id="pkVar" color-interpolation-filters="sRGB" x="0" y="0" width="100%" height="100%">' +
      '<feComponentTransfer><feFuncR id="pkVr" type="linear" slope="1" intercept="0"/>' +
      '<feFuncG id="pkVg" type="linear" slope="1" intercept="0"/>' +
      '<feFuncB id="pkVb" type="linear" slope="1" intercept="0"/></feComponentTransfer>' +
      '<feColorMatrix id="pkVs" type="saturate" values="1"/>' +
      '<feColorMatrix id="pkVh" type="hueRotate" values="0"/></filter>' +
      '<filter id="pkVideoFx" color-interpolation-filters="sRGB" x="0" y="0" width="100%" height="100%">' +
      '<feComponentTransfer><feFuncR id="pkFr" type="linear" slope="1" intercept="0"/>' +
      '<feFuncG id="pkFg" type="linear" slope="1" intercept="0"/>' +
      '<feFuncB id="pkFb" type="linear" slope="1" intercept="0"/></feComponentTransfer>' +
      '<feColorMatrix id="pkFSat" type="saturate" values="1"/>' +
      '<feComponentTransfer><feFuncR id="pkFwr" type="linear" slope="1" intercept="0"/>' +
      '<feFuncG id="pkFwg" type="linear" slope="1" intercept="0"/>' +
      '<feFuncB id="pkFwb" type="linear" slope="1" intercept="0"/></feComponentTransfer></filter>';
    wrap.appendChild(svgFilter);

    // toggle + live stats under the Program box
    var bar = document.createElement("div");
    bar.style.cssText = "display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-top:8px;font-size:12.5px";
    var lab = document.createElement("label");
    lab.style.cssText = "display:flex;gap:6px;align-items:center;cursor:pointer";
    chk = document.createElement("input"); chk.type = "checkbox"; chk.checked = enabled;
    lab.appendChild(chk);
    lab.appendChild(document.createTextNode("Smooth preview (GPU decode, ลด CPU ฝั่ง Python)"));
    statEl = document.createElement("span");
    statEl.className = "muted small";
    statEl.id = "smoothStat";
    bar.appendChild(lab); bar.appendChild(statEl);
    box.parentNode.insertBefore(bar, box.nextSibling);
    chk.addEventListener("change", function () { setEnabled(chk.checked); });

    video.addEventListener("error", function () {
      videoFailed = true; applyAvailability("เบราว์เซอร์ถอดรหัสไฟล์นี้ไม่ได้ (codec)");
    });
    video.addEventListener("loadeddata", function () { videoFailed = false; });

    if (window.ResizeObserver) { ro = new ResizeObserver(fitCanvas); ro.observe(box); if (dashboardImg.parentElement) ro.observe(dashboardImg.parentElement); }
    window.addEventListener("resize", fitCanvas);
    startFrameCounter();
    return true;
  }

  function setEnabled(v) {
    enabled = !!v;
    try { localStorage.setItem(LS_KEY, enabled ? "1" : "0"); } catch (e) {}
    if (chk) chk.checked = enabled;
    if (!enabled) { applyAvailability("ปิดไว้ (ใช้ MJPEG)"); } else { videoFailed = false; if (st) apply(st); }
  }

  function visibleTarget() {
    if (consoleView && !consoleView.hidden) return { id: "console", host: box };
    if (dashboardView && !dashboardView.hidden) return { id: "dashboard", host: dashboardImg && dashboardImg.parentElement };
    return null;
  }

  function stopStream(img) {
    if (!img) return;
    if (img.dataset.stream) { img.dataset.streamOff = img.dataset.stream; delete img.dataset.stream; }
    img.removeAttribute("src");
    img.style.visibility = "hidden";
  }

  function restoreStreamAttributes(img) {
    if (!img) return;
    if (img.dataset.streamOff) { img.dataset.stream = img.dataset.streamOff; delete img.dataset.streamOff; }
    img.style.visibility = "";
  }

  function mountTarget(target) {
    if (!target || !wrap) return;
    if (activeHost === target.id) { fitCanvas(); return; }
    if (target.id === "console") {
      dashboardImg.style.display = "";
      wrap.className = "";
      wrap.style.cssText = "position:absolute;inset:0;overflow:hidden;background:#000";
      box.style.position = "relative";
      box.appendChild(wrap);
    } else {
      wrap.className = "thumb";
      wrap.style.cssText = "position:relative;display:block;flex:none;overflow:hidden;background:#000;padding:0";
      dashboardImg.style.display = "none";
      dashboardImg.parentElement.insertBefore(wrap, dashboardImg);
    }
    activeHost = target.id;
    fitCanvas();
  }

  function setActive(on, why) {
    reason = why || "";
    if (!built) { active = !!on; return; }
    const wasActive = active;
    active = !!on;
    if (active) {
      if (!wasActive) {
        // Avoid encoding/transmitting per-frame JPEGs on either primary preview.
        stopStream(mjpg);
        stopStream(dashboardImg);
      }
      wrap.hidden = false;
      mountTarget(visibleTarget());
      return;
    }
    if (wasActive) {
      try { video.pause(); } catch (e) {}
      lastRunning = null;
      wrap.hidden = true;
      activeHost = null;
      wrap.className = "";
      wrap.style.cssText = "position:absolute;inset:0;overflow:hidden;background:#000";
      box.appendChild(wrap);
      dashboardImg.style.display = "";
      restoreStreamAttributes(mjpg);
      restoreStreamAttributes(dashboardImg);
      if (typeof window.streams === "function") window.streams(true);
      else {
        [mjpg, dashboardImg].forEach(img => {
          if (img && img.dataset.stream) img.src = img.dataset.stream + "?t=" + Date.now();
        });
      }
    }
  }

  function sourceKind(s) {
    var src = String((s && s.source) || "");
    if (!src) return "none";
    if (!LOCAL_SRC.test(src) || /:\/\//.test(src)) return "unsupported";
    if (NATIVE_VIDEO.test(src)) return "video";
    if (IMAGE_EXT.test(src)) return "image";
    return "unsupported";
  }

  function applyAvailability(whyNot) {
    if (!enabled) { setActive(false, "ปิดไว้ (ใช้ MJPEG)"); return false; }
    if (whyNot) { setActive(false, whyNot); return false; }
    return true;
  }

  function fitCanvas() {
    if (!built || !st) return;
    var K = wrap.clientWidth / Math.max(1, st.width);
    canvas.style.width = st.width + "px";
    canvas.style.height = st.height + "px";
    canvas.style.transform = "scale(" + K + ")";
  }

  function rect(el, x, y, w, h) {
    el.style.left = (x | 0) + "px"; el.style.top = (y | 0) + "px";
    el.style.width = Math.max(1, w | 0) + "px"; el.style.height = Math.max(1, h | 0) + "px";
  }

  function overlayGeom(iw, ih, fw, fh, fit, zoom, px, py) {
    zoom = Math.max(0.05, Math.min(20, +zoom || 1));
    var sx, sy;
    if (fit === "stretch") { sx = fw / iw; sy = fh / ih; }
    else if (fit === "cover") { sx = sy = Math.max(fw / iw, fh / ih); }
    else if (fit === "free") { sx = sy = 1; }
    else { sx = sy = Math.min(fw / iw, fh / ih); }
    sx *= zoom; sy *= zoom;
    var dw = iw * sx, dh = ih * sy, tx, ty;
    if (fit === "free") { tx = +px || 0; ty = +py || 0; }
    else { tx = (fw - dw) / 2 + (+px || 0); ty = (fh - dh) / 2 + (+py || 0); }
    return { dw: dw, dh: dh, tx: tx, ty: ty };
  }

  function layoutImage(s) {
    var fw = Math.max(1, s.image_width | 0), fh = Math.max(1, s.image_height | 0);
    var iw = imgEl.naturalWidth, ih = imgEl.naturalHeight;
    if (!iw || !ih) return;
    var g = overlayGeom(iw, ih, fw, fh, s.image_fit || "contain", s.image_zoom, s.image_pan_x, s.image_pan_y);
    imgEl.style.left = g.tx + "px"; imgEl.style.top = g.ty + "px";
    imgEl.style.width = g.dw + "px"; imgEl.style.height = g.dh + "px";
  }

  function neutralVariant(v) {
    return !v || (!v.brightness && v.contrast === 1 && v.saturation === 1 && !v.hue && !v.angle && v.scale === 1);
  }

  function setLinear(id, slope, intercept) {
    var el = $(id);
    if (el) { el.setAttribute("slope", String(slope)); el.setAttribute("intercept", String(intercept)); }
  }

  function applyVariant(s, flip) {
    var v = s.variant || {}, tf = flip, filters = [];
    if (s.loop_variation_enabled && !neutralVariant(v)) {
      setLinear("pkVr", v.contrast, v.brightness / 255);
      setLinear("pkVg", v.contrast, v.brightness / 255);
      setLinear("pkVb", v.contrast, v.brightness / 255);
      $("pkVs").setAttribute("values", v.saturation);
      $("pkVh").setAttribute("values", (v.hue || 0) * 2);
      filters.push("url(#pkVar)");
      tf = "rotate(" + (-(v.angle || 0)) + "deg) scale(" + (v.scale || 1) + ") " + flip;
    }
    var f = s.video_fx || {};
    var brightness = Number(f.brightness || 0);
    var contrast = Number(f.contrast == null ? 1 : f.contrast);
    var saturation = Number(f.saturation == null ? 1 : f.saturation);
    var warmth = Math.max(-1, Math.min(1, Number(f.warmth || 0))) * 18;
    setLinear("pkFr", contrast, (brightness / 255));
    setLinear("pkFg", contrast, (brightness / 255));
    setLinear("pkFb", contrast, (brightness / 255));
    $("pkFSat").setAttribute("values", String(saturation));
    setLinear("pkFwr", 1, warmth / 255);
    setLinear("pkFwg", 1, 0);
    setLinear("pkFwb", 1, -warmth / 255);
    if (Math.abs(brightness) > 0.1 || Math.abs(contrast - 1) > 0.001 ||
        Math.abs(saturation - 1) > 0.001 || Math.abs(warmth) > 0.001) {
      filters.push("url(#pkVideoFx)");
    }
    video.style.filter = filters.join(" ");
    video.style.transform = tf;
    srcImg.style.transform = flip;
  }

  function hasFrameOnlyFx(fx) {
    if (!fx) return false;
    // These operations change local pixels/geometry and cannot be faithfully reproduced
    // by a CSS/SVG GPU filter; use the exact Python-processed preview for those scenes.
    var keys = [
      "sharpen", "vignette", "skin_smooth", "wrinkle_remove", "skin_bright", "lip_tint",
      "face_slim", "forehead_width", "forehead_height", "midface_width", "cheekbone",
      "temple", "face_length", "jaw_refine", "jaw_width", "jaw_curve", "jaw_angle",
      "chin", "chin_height", "chin_roundness", "chin_tip", "eye_size"
    ];
    return keys.some(function (key) { return Math.abs(Number(fx[key] || 0)) > 0.001; });
  }

  function stepTexts(s) {
    var list = [{ id: "legacy", text: s.text, x: s.text_x, y: s.text_y, size: s.text_size, enabled: s.text_enabled }]
      .concat(s.texts || []);
    var live = new Set();
    list.forEach(function (t) {
      var on = t.enabled !== false && t.text;
      if (!on) return;
      live.add(t.id);
      var el = textEls.get(t.id);
      if (!el) {
        el = document.createElement("div");
        el.style.cssText = "position:absolute;white-space:pre;color:#fff;line-height:normal;" +
          "paint-order:stroke fill;font-family:Tahoma,'Leelawadee UI','Segoe UI',sans-serif";
        textBox.appendChild(el); textEls.set(t.id, el);
      }
      var size = Math.max(6, parseInt(t.size || s.text_size || 28, 10));
      var sw = Math.max(1, parseInt(t.stroke == null ? 2 : t.stroke, 10) || 1);
      if (el.textContent !== String(t.text)) el.textContent = String(t.text);
      el.style.left = (parseInt(t.x == null ? 24 : t.x, 10) || 0) + "px";
      el.style.top = (parseInt(t.y == null ? 56 : t.y, 10) || 0) + "px";
      el.style.fontSize = size + "px";
      el.style.webkitTextStroke = (2 * sw) + "px #000";
    });
    textEls.forEach(function (el, id) { if (!live.has(id)) { el.remove(); textEls.delete(id); } });
    return live.size > 0;
  }

  function apply(s) {
    if (!built) return;
    // When Virtual Camera is feeding another app, prioritize the output path.
    // Fall back to the server's tiny 15 FPS / 320 px preview instead of running
    // a second full-rate H.264 decoder and GPU compositor in this window.
    if (s && s.virtualcam) return void applyAvailability("Virtual Camera active · low-power preview");
    var kind = sourceKind(s);
    if (s.source && kind === "unsupported") return void applyAvailability("ชนิดไฟล์/แหล่งนี้เบราว์เซอร์เล่นตรงไม่ได้");
    if (s.source && s.source_enabled !== false && kind === "video" && hasFrameOnlyFx(s.video_fx)) {
      return void applyAvailability("เปิด Python FX ขั้นสูงอยู่ — ใช้พรีวิวประมวลผลจริงแทน Smooth");
    }
    if (videoFailed) return void applyAvailability("เบราว์เซอร์ถอดรหัสไฟล์นี้ไม่ได้ (codec)");
    if (!applyAvailability(null)) return;
    setActive(true, "");
    fitCanvas();

    var order = Array.isArray(s.layer_order) ? s.layer_order : [];
    var n = order.length;
    function z(id) { var i = order.indexOf(id); return i < 0 ? -1 : n - i; }  // order[0] = top, like the compositor

    // --- source ---
    var zs = z("source");
    var showSrc = !!s.source && s.source_enabled !== false && zs > 0 && kind !== "none";
    srcWrap.style.display = showSrc ? "block" : "none";
    if (showSrc) {
      rect(srcWrap, s.source_x, s.source_y, s.source_width, s.source_height);
      srcWrap.style.zIndex = zs;
      var fitMode = s.source_fit_mode === "contain" ? "contain" : "cover";
      video.style.objectFit = fitMode; srcImg.style.objectFit = fitMode;
      var flip = (s.source_flip_h ? "scaleX(-1)" : "scaleX(1)") + " " + (s.source_flip_v ? "scaleY(-1)" : "scaleY(1)");
      if (kind === "video") {
        srcImg.style.display = "none"; video.style.display = "block";
        var vurl = "/source.mp4?v=" + encodeURIComponent(s.source_serial || 0);
        if (vurl !== curVideoUrl) { curVideoUrl = vurl; videoFailed = false; video.src = vurl; video.load(); lastRunning = null; }
        video.loop = !!s.loop;
        applyVariant(s, flip);
      } else {
        video.style.display = "none"; srcImg.style.display = "block";
        try { video.pause(); } catch (e) {}
        var iurl = "/source-media?v=" + encodeURIComponent(s.source_serial || 0);
        if (iurl !== curSrcImgUrl) { curSrcImgUrl = iurl; srcImg.src = iurl; }
        applyVariant(s, flip);
      }
    } else {
      try { video.pause(); } catch (e) {}
    }

    // --- web ---
    var zw = z("web");
    var showWeb = zw > 0 && s.web_enabled !== false && !!s.web_url && !!s.web_has_frame;
    webImg.style.display = showWeb ? "block" : "none";
    if (showWeb) {
      rect(webImg, s.web_x, s.web_y, s.web_width, s.web_height); webImg.style.zIndex = zw;
      if (!curWebUrl) { curWebUrl = "/web.mjpg"; webImg.src = curWebUrl + "?t=" + Date.now(); }
    } else if (curWebUrl) { curWebUrl = ""; webImg.removeAttribute("src"); }

    // --- image ---
    var zi = z("image");
    var showImg = zi > 0 && s.image_enabled !== false && !!s.image;
    imgBox.style.display = showImg ? "block" : "none";
    if (showImg) {
      rect(imgBox, s.image_x, s.image_y, s.image_width, s.image_height); imgBox.style.zIndex = zi;
      var oUrl = "/overlay-media?v=" + (s.image_mtime || 0);
      if (oUrl !== curImgUrl) { curImgUrl = oUrl; imgEl.onload = function () { layoutImage(st); }; imgEl.src = oUrl; }
      layoutImage(s);
    }

    // --- text ---
    var zt = z("text");
    var any = stepTexts(s);
    textBox.style.display = (zt > 0 && any) ? "block" : "none";
    textBox.style.zIndex = zt;

    syncClock(s);
  }

  function wrapDiff(d, dur) {
    if (!dur) return d;
    return ((d + dur / 2) % dur + dur) % dur - dur / 2;
  }

  function syncClock(s) {
    if (!video || video.style.display === "none") return;
    var running = !!s.running;
    if (!running) {
      if (lastRunning !== false) { try { video.pause(); video.currentTime = 0; } catch (e) {} }
      lastRunning = false; m.drift = 0; return;
    }
    var dur = s.loop_duration > 0.2 ? s.loop_duration : (isFinite(video.duration) ? video.duration : 0);
    var nowS = s.server_now + (performance.now() - recvPerf) / 1000;
    var t = s.origin == null ? 0 : nowS - s.origin;
    if (t < 0) {                       // start lead: hold on frame 0 like the compositor
      try { video.pause(); video.currentTime = 0; } catch (e) {}
      lastRunning = true; return;
    }
    var exp = s.loop && dur ? t % dur : (dur ? Math.min(t, dur) : t);
    if (video.readyState < 2) return;
    if (video.paused || lastRunning !== true) {
      try { video.currentTime = exp; } catch (e) {}
      video.playbackRate = 1;
      var p = video.play(); if (p && p.catch) p.catch(function () {});
      lastRunning = true; return;
    }
    var d = wrapDiff(exp - video.currentTime, s.loop ? dur : 0);
    m.drift = d * 1000;
    if (Math.abs(d) > 0.25) {          // far off (tab was throttled, seek, long stall): jump
      try { video.currentTime = exp; } catch (e) {}
      video.playbackRate = 1; m.hardSeeks++;
    } else if (Math.abs(d) > 0.03) {   // slightly off: steer playback rate (no visible jump)
      video.playbackRate = 1 + Math.max(-0.08, Math.min(0.08, d * 0.5));
    } else {
      video.playbackRate = 1;
    }
  }

  function startFrameCounter() {
    if (!video.requestVideoFrameCallback) return;
    var cb = function (now) {
      m.frames++;
      if (!m.t0) m.t0 = now;
      if (now - m.t0 >= 1000) { m.fps = m.frames * 1000 / (now - m.t0); m.frames = 0; m.t0 = now; }
      video.requestVideoFrameCallback(cb);
    };
    video.requestVideoFrameCallback(cb);
  }

  function updateStat() {
    if (!built || !statEl) return;
    if (active) {
      var q = video.getVideoPlaybackQuality ? video.getVideoPlaybackQuality() : null;
      if (q) { m.dropped = q.droppedVideoFrames; m.total = q.totalVideoFrames; }
      statEl.textContent = "Smooth ✓ · วิดีโอ " + m.fps.toFixed(1) + " fps · drift " +
        (m.drift >= 0 ? "+" : "") + m.drift.toFixed(0) + " ms · เฟรมตก " + m.dropped + "/" + m.total;
    } else {
      statEl.textContent = "MJPEG" + (reason ? " (" + reason + ")" : "");
    }
  }

  // Treat an unfocused Electron window like a hidden preview. Alt+Tab can leave
  // document.hidden=false, but continuing hardware video decode in the background
  // wastes resources and can trigger a hard seek when the 300 ms clock polling resumes.
  function viewVisible() { return !!visibleTarget() && !document.hidden && document.hasFocus(); }

  function tick() {
    if (!built) return;
    if (!viewVisible()) {
      if (active) { try { video.pause(); } catch (e) {} lastRunning = null; }
      return;
    }
    if (!enabled) { applyAvailability("ปิดไว้ (ใช้ MJPEG)"); updateStat(); return; }
    if (inflight) return;
    inflight = true;
    fetch("/api/smooth", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (j) { st = j; recvPerf = performance.now(); m.polls++; apply(j); })
      .catch(function () { applyAvailability("เชื่อมต่อ backend ไม่ได้"); })
      .then(function () { inflight = false; updateStat(); });
  }

  function init() {
    built = build();
    if (!built) return;
    timer = setInterval(tick, POLL_MS);
    setInterval(updateStat, 1000);
    function suspendPreviewClock() {
      if (video) { try { video.pause(); } catch (e) {} }
      lastRunning = null;
    }
    function resumePreviewClock() {
      lastRunning = null;
      if (viewVisible()) tick();
    }
    window.addEventListener("blur", suspendPreviewClock);
    window.addEventListener("focus", resumePreviewClock);
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) suspendPreviewClock();
      else resumePreviewClock();
    });
    window.addEventListener("hashchange", function () { lastRunning = null; setTimeout(tick, 0); });
    tick();
  }

  window.PaokraSmooth = {
    active: function () { return active; },
    enabled: function () { return enabled; },
    setEnabled: setEnabled,
    metrics: function () { return { fps: m.fps, drift_ms: m.drift, dropped: m.dropped, total: m.total, hardSeeks: m.hardSeeks, polls: m.polls, active: active, reason: reason }; },
    video: function () { return video; },
    tick: tick
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
