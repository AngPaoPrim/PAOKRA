/* ════════════════════════════════════════════════
   PAOKRA Filter Addon — v2fFilter.js
   Drop-in enhancement for Video to Filter page
   ════════════════════════════════════════════════ */

(function(){
"use strict";

/* ─── Preset Definitions ─────────────────────── */
const FILTER_PRESETS = {
  none:    { brightness:0, contrast:100, saturation:100, warmth:0, sharpen:0, vignette:0 },
  clean:   { brightness:3, contrast:104, saturation:103, warmth:0,  sharpen:12, vignette:0  },
  soft:    { brightness:2, contrast:98,  saturation:98,  warmth:-5, sharpen:4,  vignette:10 },
  vivid:   { brightness:0, contrast:110, saturation:145, warmth:0,  sharpen:8,  vignette:5  },
  warm:    { brightness:5, contrast:102, saturation:108, warmth:30, sharpen:6,  vignette:8  },
  cool:    { brightness:2, contrast:103, saturation:95,  warmth:-25,sharpen:8,  vignette:6  },
  vintage: { brightness:-5,contrast:98,  saturation:75,  warmth:35, sharpen:0,  vignette:22 },
  cinema:  { brightness:-8,contrast:112, saturation:88,  warmth:-10,sharpen:10, vignette:28 },
  dream:   { brightness:8, contrast:96,  saturation:115, warmth:-8, sharpen:2,  vignette:14 },
};

/* Active preset and intensity */
let activePreset = "none";
let userEdited = false;       // true once the user moves a slider by hand
let intensityPct = 100;
let compareMode = false;

/* ─── Slider fill updater ─────────────────────── */
function updateSliderFill(el){
  const min = parseFloat(el.min);
  const max = parseFloat(el.max);
  const val = parseFloat(el.value);
  const pct = ((val - min) / (max - min)) * 100;
  el.style.setProperty("--flt-fill", pct.toFixed(1) + "%");
}

function initSliderFills(){
  document.querySelectorAll("input.flt-range, .scene-panel input[type=range]").forEach(el=>{
    updateSliderFill(el);
    el.addEventListener("input", ()=>updateSliderFill(el));
  });
}

/* ─── Apply preset with intensity ───────────────── */
function applyPresetWithIntensity(presetName, intensity){
  const preset = FILTER_PRESETS[presetName];
  if (!preset) return;
  const neutral = FILTER_PRESETS.none;
  const factor = intensity / 100;

  const lerp = (a, b, t) => a + (b - a) * t;

  const vals = {};
  for (const k of Object.keys(neutral)){
    vals[k] = lerp(neutral[k], preset[k], factor);
  }
  setSliderGroup(vals);
  markPresetActive(presetName);
  // Trigger preview update (app.js already has v2fPreviewFX bound to input events)
  document.getElementById("v2fBrightness")?.dispatchEvent(new Event("input"));
}

function setSliderGroup(vals){
  const map = {
    brightness:"v2fBrightness", contrast:"v2fContrast",
    saturation:"v2fSaturation", warmth:"v2fWarmth",
    sharpen:"v2fSharpen", vignette:"v2fVignette"
  };
  for (const [k, id] of Object.entries(map)){
    const el = document.getElementById(id);
    if (el && vals[k] !== undefined){
      el.value = String(Math.round(vals[k]));
      updateSliderFill(el);
    }
  }
}

function markPresetActive(name){
  document.querySelectorAll(".flt-preset-btn").forEach(b=>{
    b.classList.toggle("active", b.dataset.fltPreset === name);
  });
}

/* ─── Preset bar click ─────────────────────────── */
function initPresetBar(){
  document.querySelectorAll(".flt-preset-btn").forEach(btn=>{
    btn.addEventListener("click", ()=>{
      const name = btn.dataset.fltPreset;
      activePreset = name;
      userEdited = false;
      const pct = parseInt(document.getElementById("v2fIntensity")?.value || 100);
      applyPresetWithIntensity(name, pct);
    });

    // (hover-preview removed: it overwrote manual edits and spammed the server preview)
  });
}

/* ─── Intensity slider ─────────────────────────── */
function initIntensity(){
  const intensityEl = document.getElementById("v2fIntensity");
  const intensityVal = document.getElementById("v2fIntensityVal");
  if (!intensityEl) return;

  updateSliderFill(intensityEl);
  intensityEl.addEventListener("input", ()=>{
    const pct = parseInt(intensityEl.value);
    if (intensityVal) intensityVal.textContent = String(pct);
    updateSliderFill(intensityEl);
    if (userEdited) return; // keep the user's manual tweaks
    applyPresetWithIntensity(activePreset, pct);
  });
}

/* ─── Compare (Before/After) ──────────────────── */
function initCompare(){
  const btn = document.getElementById("v2fCompare");
  if (!btn) return;

  btn.addEventListener("click", ()=>{
    compareMode = !compareMode;
    btn.classList.toggle("active", compareMode);
    btn.textContent = compareMode ? "✕ Exit Compare" : "⊞ Compare";

    const vp = document.getElementById("v2fViewport");
    if (!vp) return;
    vp.classList.toggle("v2f-compare-active", compareMode);

    // Add/remove labels
    vp.querySelector(".v2f-compare-label-before")?.remove();
    vp.querySelector(".v2f-compare-label-after")?.remove();
    if (compareMode){
      const lb = document.createElement("div");
      lb.className = "v2f-compare-label-before";
      lb.textContent = "Before";
      const la = document.createElement("div");
      la.className = "v2f-compare-label-after";
      la.textContent = "After";
      vp.appendChild(lb);
      vp.appendChild(la);
    }
  });
}

/* ─── Intercept Reset to also clear preset ──────── */
function patchReset(){
  const origBtn = document.getElementById("v2fReset");
  if (!origBtn) return;
  origBtn.addEventListener("click", ()=>{
    activePreset = "none";
    userEdited = false;
    markPresetActive("none");
    const intensityEl = document.getElementById("v2fIntensity");
    if (intensityEl){ intensityEl.value = "100"; updateSliderFill(intensityEl); }
    const intensityVal = document.getElementById("v2fIntensityVal");
    if (intensityVal) intensityVal.textContent = "100";
    // app.js v2fReset handles the sliders
  }, true);
}

/* ─── Also fill beauty/face range sliders ────────── */
function initBeautyFaceSliders(){
  const panels = ["beauty","face"];
  panels.forEach(panel=>{
    const sec = document.querySelector(`[data-v2f-panel="${panel}"]`);
    if (!sec) return;
    sec.querySelectorAll("input[type=range]").forEach(el=>{
      updateSliderFill(el);
      el.addEventListener("input", ()=>updateSliderFill(el));
    });
  });
}

/* ─── Live preview parity: vignette, sharpen hint, cool tone ─── */
function initPreviewParity(){
  if (document.getElementById("v2f-parity-style")) return;
  const st = document.createElement("style");
  st.id = "v2f-parity-style";
  st.textContent =
    "#v2fViewport{position:relative}" +
    "#v2fViewport::after{content:'';position:absolute;inset:0;pointer-events:none;z-index:2;" +
    "background:radial-gradient(ellipse at center,rgba(0,0,0,0) 50%,rgba(0,0,0,calc(var(--v2f-vig,0)*.9)) 100%)}";
  document.head.appendChild(st);

  const ids = ["v2fBrightness","v2fContrast","v2fSaturation","v2fWarmth","v2fSharpen","v2fVignette"];
  const sync = ()=>{
    const g = id => Number(document.getElementById(id)?.value || 0);
    const vp = document.getElementById("v2fViewport");
    const vid = document.getElementById("v2fVideo");
    if (vp) vp.style.setProperty("--v2f-vig", String(Math.min(1, g("v2fVignette")/60)));
    if (vid){
      const w = g("v2fWarmth")/100;
      let f = vid.style.filter || "";
      if (w < 0) f = f.replace(/hue-rotate\([^)]*\)/, "hue-rotate(" + (w*45).toFixed(0) + "deg)");
      const s = g("v2fSharpen");
      f = f.replace(/\s*drop-shadow\([^)]*\)/g, "");
      if (s > 0) f += " contrast(" + (1 + s/400).toFixed(3) + ")";
      vid.style.filter = f;
    }
  };
  ids.forEach(id=>{
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener("input", e=>{
      if (e.isTrusted) userEdited = true;     // real user drag, not preset apply
      if (e.isTrusted) markPresetActive("__custom__");
      setTimeout(sync, 0);                     // run after app.js preview handler
    });
  });
  sync();
}

/* ─── Boot ──────────────────────────────────────── */
function boot(){
  initPreviewParity();
  initSliderFills();
  initBeautyFaceSliders();
  initPresetBar();
  initIntensity();
  initCompare();
  patchReset();

  // Re-init whenever Video to Filter view opens (app.js calls v2fInit)
  const origHash = window.location.hash;
  const observer = new MutationObserver(()=>{
    const panel = document.querySelector('[data-v2f-panel="filters"].on');
    if (panel && !panel.dataset.fltBooted){
      panel.dataset.fltBooted = "1";
      initSliderFills();
      initBeautyFaceSliders();
    }
  });
  const vSection = document.getElementById("v-video-to-filter");
  if (vSection) observer.observe(vSection, {childList:true, subtree:true, attributes:false});
}

if (document.readyState === "loading"){
  document.addEventListener("DOMContentLoaded", boot);
} else {
  // app.js has already run; hook in after a tick so v2fInit has fired
  setTimeout(boot, 80);
}

})();
