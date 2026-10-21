/* PAOKRA Studio — Pro UI layer (appearance studio, boot screen, preview manager, command palette) */
(function(){
"use strict";
if(typeof UI_CFG==="undefined"||typeof UI_DEFAULT==="undefined"||!window.PKC){console.warn("[pk] app.js not ready");return}
const $=(s,r=document)=>r.querySelector(s),$$=(s,r=document)=>[...r.querySelectorAll(s)];
const PKC=window.PKC,B=window.paokra||{};
const L=()=>UI_CFG.language==="en"?"en":"th";
const T=(th,en)=>L()==="en"?en:th;
const root=document.documentElement;

/* ───────── defaults + migration ───────── */
Object.assign(UI_DEFAULT,{theme:"dark",accent:"violet",radius:14,uiv:2,bg:"aurora",glass:true,card:"elevated",font:"system",boot:"full",accentCustom:"#7c6cff"});
UI_CFG=Object.assign({},UI_DEFAULT,UI_CFG);

/* ───────── range slider progress fill ───────── */
function fill(el){
  const mn=Number(el.min)||0,mx=el.max===""?100:Number(el.max),v=Number(el.value);
  el.style.setProperty("--p",(mx>mn?Math.max(0,Math.min(100,(v-mn)/(mx-mn)*100)):0)+"%");
}
(function(){
  const d=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value");
  if(!d||!d.set)return;
  Object.defineProperty(HTMLInputElement.prototype,"value",{configurable:true,enumerable:d.enumerable,get:d.get,set(v){d.set.call(this,v);if(this.type==="range")fill(this)}});
})();
document.addEventListener("input",e=>{if(e.target&&e.target.type==="range")fill(e.target)},true);
const fillAll=()=>$$("input[type=range]").forEach(fill);

/* ───────── i18n for pro UI strings ───────── */
function pkI18n(){
  const en=L()==="en";
  $$("[data-th]").forEach(el=>{const v=en?el.dataset.en:el.dataset.th;if(v!=null&&el.textContent!==v)el.textContent=v});
}

/* ───────── appearance studio ───────── */
const PRESETS=[
  {id:"midnight",n:"Midnight",sw:"linear-gradient(135deg,#1d1650,#080b14)",pa:"#7c6cff",c:{theme:"dark",accent:"violet",bg:"aurora",card:"elevated",glass:true}},
  {id:"ocean",n:"Ocean",sw:"linear-gradient(135deg,#0a3347,#080b14)",pa:"#22c7e8",c:{theme:"dark",accent:"cyan",bg:"aurora",card:"elevated",glass:true}},
  {id:"graphite",n:"Graphite",sw:"linear-gradient(135deg,#1b2030,#0b0d14)",pa:"#4f8cff",c:{theme:"dark",accent:"blue",bg:"solid",card:"outline",glass:false}},
  {id:"forest",n:"Forest",sw:"linear-gradient(135deg,#0c3326,#080b14)",pa:"#2dd4a0",c:{theme:"dark",accent:"emerald",bg:"soft",card:"elevated",glass:true}},
  {id:"sunset",n:"Sunset",sw:"linear-gradient(135deg,#40200e,#0d0a10)",pa:"#ff8a3d",c:{theme:"dark",accent:"orange",bg:"aurora",card:"elevated",glass:true}},
  {id:"sakura",n:"Sakura",sw:"linear-gradient(135deg,#ffe3ec,#f5f0f6)",pa:"#ff5c8a",c:{theme:"light",accent:"rose",bg:"soft",card:"elevated",glass:true}},
  {id:"paper",n:"Paper",sw:"linear-gradient(135deg,#ffffff,#e9edf5)",pa:"#4f8cff",c:{theme:"light",accent:"blue",bg:"solid",card:"elevated",glass:false}}
];
const SW=["violet","blue","cyan","emerald","lime","gold","orange","rose","pink","purple","green"];
const KEYS=["theme","accent","accentCustom","bg","glass","card","font","radius","density","sidebar","fontScale","animations","gradient","boot","previewQuality","language","aiMode","hiddenMenus"];
let lastZoom=0;

function pkApply(){
  PKC.apply(UI_CFG);
  const sc=(Number(UI_CFG.fontScale)||100)/100;
  if(B.setZoom){document.body.style.fontSize="";if(sc!==lastZoom){lastZoom=sc;try{B.setZoom(sc)}catch(_){}}}
  pkSync();
}
const _apply=window.applyUI;
window.applyUI=function(){try{if(_apply)_apply.apply(this,arguments)}catch(e){console.error(e)}pkApply()};

const OPTIONAL_MENUS=[
  {id:"video-to-filter",th:"Video to Filter",en:"Video to Filter"},
  {id:"editor",th:"Video Editor",en:"Video Editor"},
  {id:"media",th:"มีเดีย",en:"Media"},
  {id:"console",th:"คอนโซล LIVE",en:"Live Console"},
  {id:"comments",th:"คอมเมนต์",en:"Comments"},
  {id:"manager",th:"ตัวจัดการ LIVE",en:"LIVE Manager"},
  {id:"tiktok-dashboard",th:"TikTok Dashboard",en:"TikTok Dashboard"},
  {id:"accounts",th:"บัญชี",en:"Accounts"},
  {id:"logs",th:"บันทึกระบบ",en:"Logs"}
];
function applyMenuVisibility(){
  const hidden=new Set(Array.isArray(UI_CFG.hiddenMenus)?UI_CFG.hiddenMenus:[]);
  document.querySelectorAll(".side [data-view]").forEach(el=>{
    const id=el.dataset.view;
    el.classList.toggle("pk-nav-hidden",OPTIONAL_MENUS.some(x=>x.id===id)&&hidden.has(id));
  });
  document.querySelectorAll(".side .side-group").forEach(group=>{
    let node=group.nextElementSibling,hasVisiblePage=false;
    while(node&&!node.classList.contains("side-group")&&!node.classList.contains("side-foot")){
      if(node.matches("a[data-view]")&&!node.hidden&&!node.classList.contains("pk-nav-hidden")){hasVisiblePage=true;break;}
      node=node.nextElementSibling;
    }
    group.classList.toggle("pk-nav-group-hidden",!hasVisiblePage);
  });
  const current=String(location.hash||"").replace(/^#/,"");
  if(current&&hidden.has(current)&&OPTIONAL_MENUS.some(x=>x.id===current))location.hash="#dashboard";
}
const NAV_GROUP_STORAGE = "paokra.nav.groups.v1";
const NAV_GROUP_DEFAULTS = {studio:false,live:true,system:true};
let navGroupState = {...NAV_GROUP_DEFAULTS};

function navGroupLinks(group){
  const links=[];
  let node=group.nextElementSibling;
  while(node&&!node.classList.contains("side-group")&&!node.classList.contains("side-foot")){
    if(node.matches?.("a[data-view]"))links.push(node);
    node=node.nextElementSibling;
  }
  return links;
}
function renderSidebarGroup(group){
  const key=group.dataset.navGroup;
  const collapsed=navGroupState[key]===true;
  group.setAttribute("aria-expanded",collapsed?"false":"true");
  group.classList.toggle("pk-nav-group-collapsed",collapsed);
  navGroupLinks(group).forEach(link=>link.classList.toggle("pk-nav-collapsed",collapsed));
}
function saveSidebarGroups(){
  try{localStorage.setItem(NAV_GROUP_STORAGE,JSON.stringify(navGroupState))}catch(_){}
}
function setupSidebarGroups(){
  try{
    const saved=JSON.parse(localStorage.getItem(NAV_GROUP_STORAGE)||"{}");
    for(const key of Object.keys(NAV_GROUP_DEFAULTS)){
      if(typeof saved?.[key]==="boolean")navGroupState[key]=saved[key];
    }
  }catch(_){navGroupState={...NAV_GROUP_DEFAULTS}}
  const groups=$$(".side .side-group[data-nav-group]");
  groups.forEach(group=>{
    const key=group.dataset.navGroup;
    group.setAttribute("role","button");
    group.tabIndex=0;
    if(typeof navGroupState[key]!=="boolean")navGroupState[key]=false;
    renderSidebarGroup(group);
    const toggle=()=>{
      navGroupState[key]=!navGroupState[key];
      renderSidebarGroup(group);
      saveSidebarGroups();
    };
    group.addEventListener("click",toggle);
    group.addEventListener("keydown",e=>{
      if(e.key==="Enter"||e.key===" "){e.preventDefault();toggle()}
    });
  });
  const expandActiveGroup=()=>{
    const page=String(location.hash||"#dashboard").replace(/^#/,"");
    const active=$$(".side a[data-view]").find(a=>a.dataset.view===page);
    if(!active)return;
    let node=active.previousElementSibling;
    while(node&&!node.classList.contains("side-group"))node=node.previousElementSibling;
    if(!node)return;
    const key=node.dataset.navGroup;
    if(navGroupState[key]===true){
      navGroupState[key]=false;
      renderSidebarGroup(node);
      saveSidebarGroups();
    }
  };
  window.addEventListener("hashchange",expandActiveGroup);
  expandActiveGroup();
}
function setupMenuVisibilitySettings(){
  const host=document.querySelector('#v-settings [data-set-panel="appearance"]');
  if(!host)return;
  let card=host.querySelector("#pkNavigationSettings");
  if(!card){
    card=document.createElement("div");card.className="card wide-card";card.id="pkNavigationSettings";
    card.innerHTML='<h2 data-th="เมนูและความลื่นไหล" data-en="Navigation & Motion">เมนูและความลื่นไหล</h2><p class="muted small" data-th="ซ่อนหน้าเสริมที่ไม่ใช้ได้จากตรงนี้ หน้าหลัก Scene Builder และ Settings จะคงอยู่เสมอ" data-en="Hide optional pages you do not use. Dashboard, Scene Builder and Settings always stay visible.">ซ่อนหน้าเสริมที่ไม่ใช้ได้จากตรงนี้ หน้าหลัก Scene Builder และ Settings จะคงอยู่เสมอ</p><div class="pk-nav-toggle-grid" id="pkOptionalMenuList"></div><label class="pk-nav-toggle pk-motion-toggle"><input type="checkbox" id="pkMotionToggle"><span><b data-th="เอฟเฟกต์การเคลื่อนไหว" data-en="Motion effects">เอฟเฟกต์การเคลื่อนไหว</b><small data-th="ปิดเพื่อลดภาพเคลื่อนไหวและลดภาระ UI" data-en="Turn off to reduce UI animation work.">ปิดเพื่อลดภาพเคลื่อนไหวและลดภาระ UI</small></span></label>';
    host.appendChild(card);
    const list=card.querySelector("#pkOptionalMenuList");
    list.innerHTML=OPTIONAL_MENUS.map(x=>'<label class="pk-nav-toggle"><input type="checkbox" data-pk-menu="'+x.id+'"><span data-th="'+x.th+'" data-en="'+x.en+'">'+x.th+'</span></label>').join("");
    card.addEventListener("change",e=>{
      const box=e.target.closest("input[data-pk-menu]");
      if(box){const id=box.dataset.pkMenu,hidden=new Set(Array.isArray(UI_CFG.hiddenMenus)?UI_CFG.hiddenMenus:[]);if(box.checked)hidden.delete(id);else hidden.add(id);UI_CFG.hiddenMenus=Array.from(hidden);saveUI();applyMenuVisibility();return;}
      if(e.target.id==="pkMotionToggle"){UI_CFG.animations=!!e.target.checked;saveUI();}
    });
  }
  const hidden=new Set(Array.isArray(UI_CFG.hiddenMenus)?UI_CFG.hiddenMenus:[]);
  card.querySelectorAll("input[data-pk-menu]").forEach(box=>box.checked=!hidden.has(box.dataset.pkMenu));
  const motion=card.querySelector("#pkMotionToggle");if(motion)motion.checked=UI_CFG.animations!==false;
  applyMenuVisibility();
  if(typeof applyLanguage==="function")applyLanguage();
}
function pkSet(p){Object.assign(UI_CFG,p);saveUI();applyMenuVisibility();}
function pkSync(){
  $$(".pk-seg[data-pk]").forEach(seg=>{const k=seg.dataset.pk;$$("button",seg).forEach(b=>b.classList.toggle("on",String(UI_CFG[k])===b.dataset.v))});
  $$(".pk-sw[data-acc]").forEach(b=>b.classList.toggle("on",UI_CFG.accent===b.dataset.acc));
  const cs=$(".pk-sw.custom");if(cs){cs.classList.toggle("on",UI_CFG.accent==="custom");cs.style.setProperty("--c",UI_CFG.accentCustom||"#7c6cff");const ci=$("input",cs);if(ci&&ci.value!==UI_CFG.accentCustom)ci.value=UI_CFG.accentCustom||"#7c6cff"}
  $$(".pk-preset").forEach(b=>{const p=PRESETS.find(x=>x.id===b.dataset.preset);b.classList.toggle("on",!!p&&Object.keys(p.c).every(k=>UI_CFG[k]===p.c[k]))});
  const g=$("#pkGlass");if(g)g.checked=UI_CFG.glass!==false;
  const f=$("#pkFont");if(f&&f.value!==UI_CFG.font)f.value=UI_CFG.font;
}
function buildAppearance(){
  const pr=$("#pkPresets");
  if(pr)pr.innerHTML=PRESETS.map(p=>`<button type="button" class="pk-preset" data-preset="${p.id}" style="--pa:${p.pa}"><div class="sw" style="background:${p.sw}"></div><b>${p.n}</b></button>`).join("");
  const sw=$("#pkSwatches");
  if(sw)sw.innerHTML=SW.map(k=>`<button type="button" class="pk-sw" data-acc="${k}" style="--c:${PKC.ACC[k]}" title="${k}" aria-label="${k}"></button>`).join("")+`<span class="pk-sw custom" title="Custom"><input type="color" value="${UI_CFG.accentCustom||"#7c6cff"}" aria-label="Custom color"></span>`;
}
document.addEventListener("click",e=>{
  const t=e.target.closest("button");if(!t)return;
  if(t.dataset.preset){const p=PRESETS.find(x=>x.id===t.dataset.preset);if(p)pkSet(p.c);return}
  if(t.dataset.acc){pkSet({accent:t.dataset.acc});return}
  const seg=t.closest(".pk-seg[data-pk]");if(seg&&t.dataset.v){pkSet({[seg.dataset.pk]:t.dataset.v});return}
  const tab=t.closest(".set-tab");if(tab){pkTab(tab.dataset.setTab);return}
});
document.addEventListener("input",e=>{
  if(e.target.matches(".pk-sw.custom input"))pkSet({accent:"custom",accentCustom:e.target.value});
});
$("#pkGlass")?.addEventListener("change",e=>pkSet({glass:e.target.checked}));
$("#pkFont")?.addEventListener("change",e=>pkSet({font:e.target.value}));
$("#pkReset")?.addEventListener("click",()=>{UI_CFG=Object.assign({},UI_DEFAULT);saveUI();pkI18n();notify(T("รีเซ็ตรูปลักษณ์แล้ว","Appearance reset"))});
$("#pkExport")?.addEventListener("click",()=>{
  const o={paokraTheme:1};KEYS.forEach(k=>o[k]=UI_CFG[k]);
  const a=document.createElement("a");a.href=URL.createObjectURL(new Blob([JSON.stringify(o,null,2)],{type:"application/json"}));a.download="paokra-theme.json";document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(a.href),2000);
});
$("#pkImport")?.addEventListener("click",()=>$("#pkImportFile").click());
$("#pkImportFile")?.addEventListener("change",async e=>{
  const f=e.target.files[0];e.target.value="";if(!f)return;
  try{const o=JSON.parse(await f.text());if(!o.paokraTheme)throw new Error("not a PAOKRA theme");const p={};KEYS.forEach(k=>{if(k in o)p[k]=o[k]});pkSet(p);notify(T("นำเข้าธีมแล้ว","Theme imported"))}
  catch(err){notify(T("ไฟล์ธีมไม่ถูกต้อง: ","Invalid theme file: ")+err.message,false)}
});

/* ───────── settings tabs ───────── */
function pkTab(n){
  if(!$(`[data-set-panel="${n}"]`))n="appearance";
  $$(".set-tab").forEach(b=>b.classList.toggle("on",b.dataset.setTab===n));
  $$(".set-panel").forEach(p=>p.classList.toggle("on",p.dataset.setPanel===n));
  try{localStorage.setItem("paokra.settings.tab",n)}catch(_){}
  fillAll();
}
function openSettings(tab,target){
  const go=()=>{pkTab(tab);if(target){const el=$(target);if(el){el.scrollIntoView({block:"center",behavior:"smooth"});const h=el.closest(".pk-row,.field,.card-mini")||el;h.animate([{boxShadow:"0 0 0 0 rgb(var(--acc-rgb)/.0)"},{boxShadow:"0 0 0 6px rgb(var(--acc-rgb)/.45)"},{boxShadow:"0 0 0 0 rgb(var(--acc-rgb)/0)"}],{duration:1100})}}};
  if(location.hash!=="#settings"){location.hash="#settings";setTimeout(go,60)}else go();
}
window.addEventListener("hashchange",()=>{if(location.hash==="#settings"){let t="appearance";try{t=localStorage.getItem("paokra.settings.tab")||t}catch(_){}pkTab(t)}fillAll()});

/* ───────── system / about (Electron bridge) ───────── */
async function loadAbout(){
  if(!B.getInfo){["#pkOpenLogs","#pkOpenData","#pkRestartBackend"].forEach(s=>{const e=$(s);if(e)e.hidden=true});return}
  try{const i=await B.getInfo();
    $("#pkVersion").textContent="v"+i.version+" · Electron "+i.electron+" · "+i.platform;
    $("#pkDataDir").textContent=i.dataDir||"—";$("#pkLogDir").textContent=i.logDir||"—";
    $("#pkFootTxt").dataset.ver="v"+i.version;
  }catch(_){}
}
$("#pkOpenLogs")?.addEventListener("click",()=>B.openPath&&B.openPath("logs"));
$("#pkOpenData")?.addEventListener("click",()=>B.openPath&&B.openPath("data"));
$("#pkRestartBackend")?.addEventListener("click",async()=>{notify(T("กำลังรีสตาร์ท Backend…","Restarting backend…"));try{await B.restartBackend()}catch(e){notify(e.message,false)}});
if(!B.checkForUpdates){const e=$("#pkCheckUpdates");if(e)e.hidden=true}
$("#pkCheckUpdates")?.addEventListener("click",async()=>{
  const b=$("#pkCheckUpdates");
  if(!B.checkForUpdates){notify(T("ระบบตรวจอัปเดตไม่พร้อมใช้งานในโหมดนี้","Update checking is not available in this mode"),false);return}
  if(b){b.disabled=true;b.textContent=T("กำลังตรวจสอบ…","Checking…")}
  try{
    const r=await B.checkForUpdates();
    if(r?.ok===false){if(!r.dialog)throw new Error(r.error||"Update check failed");return}
    if(r?.state!=="already-checking"&&r?.state!=="notice")notify(T("ส่งคำขอตรวจสอบเวอร์ชันแล้ว โปรดดูผลจากหน้าต่างแจ้งเตือน","Update check started; see the result dialog"));
  }catch(e){notify(T("ตรวจสอบการอัปเดตไม่สำเร็จ: ","Update check failed: ")+e.message,false)}
  finally{if(b){b.disabled=false;b.textContent=L()==="en"?(b.dataset.en||"Check for updates"):(b.dataset.th||"ตรวจสอบการอัปเดต")}}
});
$("#pkReload")?.addEventListener("click",()=>location.reload());

/* ───────── boot / loading screen ───────── */
const boot=$("#pkBoot");
window.__pkBootDone=false;
const TIPS=[
  ["กด Ctrl+K เพื่อค้นหาเมนูและคำสั่งทั้งหมดได้ทันที","Press Ctrl+K to search every page and command"],
  ["ปรับธีม สี และฟอนต์ได้ที่ ตั้งค่า › รูปลักษณ์","Customize theme, color and font in Settings › Appearance"],
  ["Scene Builder ใช้จัดองค์ประกอบ · ฟิลเตอร์หนักๆ ทำใน Video to Filter","Scene Builder is for layout · heavy filters live in Video to Filter"],
  ["ไฟล์วิดีโอใหญ่ไม่ต้องอัปโหลด เลือกจากดิสก์ได้โดยตรง","Large videos need no upload — pick them straight from disk"]
];
const STEPS=[["กำลังเชื่อมต่อ Backend…","Connecting to backend…"],["กำลังโหลดอินเทอร์เฟซ…","Loading interface…"],["กำลังเตรียมพรีวิว…","Preparing preview…"],["พร้อมใช้งาน","Ready"]];
function runBoot(el,opts){
  opts=opts||{};
  const bar=$("#pkbBar",el),pct=$("#pkbPct",el),stat=$("#pkbStat",el),tip=$("#pkbTip",el),err=$("#pkbErr",el),dots=$$(".pkb-steps i",el);
  let shown=0,target=6,stage=-1,t0=performance.now(),ended=false,tipI=Math.floor(Math.random()*TIPS.length);
  const setStage=i=>{if(stage===i)return;stage=i;stat.textContent=T(...STEPS[i]);dots.forEach((d,k)=>{d.className=k<i?"done":k===i?"on":""})};
  const showTip=()=>{tip.textContent=T(...TIPS[tipI%TIPS.length]);tipI++};
  showTip();const tipTimer=setInterval(showTip,3200);
  const previewReady=()=>{const v=$$("img[data-stream]").filter(i=>i.getAttribute("src")&&i.offsetParent!==null);return v.every(i=>i.classList.contains("pk-live"))};
  const end=()=>{if(ended)return;ended=true;clearInterval(tick);clearInterval(tipTimer);el.classList.add("out");setTimeout(()=>el.remove(),750);if(!opts.demo){window.__pkBootDone=true;root.classList.add("pk-ready")}if(opts.done)opts.done()};
  setStage(0);
  const tick=setInterval(()=>{
    const el_=performance.now()-t0;
    if(opts.demo){target=Math.min(100,el_/2300*100);setStage(target<30?0:target<65?1:target<95?2:3)}
    else{
      const be=(typeof online!=="undefined"&&online===true);
      if(!be){setStage(0);target=Math.min(28,4+el_/300);if(el_>12000&&err.hidden){err.hidden=false}}
      else{
        if(!err.hidden)err.hidden=true;
        if(stage<1)setStage(1);
        if(stage===1){target=Math.max(target,82);if(document.readyState==="complete"||el_>300)setStage(2)}
        if(stage===2){target=95;if(previewReady()||el_>1800){setStage(3);target=100}}
      }
    }
    shown+=(target-shown)*0.22;
    bar.style.width=shown.toFixed(1)+"%";pct.textContent=Math.round(shown)+"%";
    if((stage===3&&shown>98.4)||(opts.demo&&shown>99))end();
  },60);
  if(!opts.demo){
    $("#pkbRetry",el)?.addEventListener("click",()=>location.reload());
    $("#pkbGo",el)?.addEventListener("click",end);
    const lb=$("#pkbLogs",el);if(lb&&B.openPath){lb.hidden=false;lb.addEventListener("click",()=>B.openPath("logs"))}
    setTimeout(()=>{if(!ended)end()},30000);
  }
}
const bootTpl=boot?boot.cloneNode(true):null;
if(boot)runBoot(boot);else window.__pkBootDone=true;
$("#pkBootDemo")?.addEventListener("click",()=>{
  if(!bootTpl)return;
  const old=UI_CFG.boot;root.dataset.boot=old==="off"?"full":old;
  const c=bootTpl.cloneNode(true);c.classList.remove("out");document.body.appendChild(c);
  runBoot(c,{demo:true,done:()=>{root.dataset.boot=old}});
});

/* ───────── live preview manager ───────── */
function pvBox(img){return img.closest(".progbox")}
function pvArm(img){
  // One watchdog per preview element. A changed src used to leave the previous
  // watchdog alive until its 12 s timeout, allowing overlapping retry loops.
  if(img._pkArmTimer){clearInterval(img._pkArmTimer);img._pkArmTimer=0}
  if(!img.getAttribute("src")){
    if(img._pkRetryTimer){clearTimeout(img._pkRetryTimer);img._pkRetryTimer=0}
    img.classList.remove("pk-live");const b=pvBox(img);if(b)b.classList.remove("pk-loading");return
  }
  img.decoding="async";img.classList.remove("pk-live");
  const b=pvBox(img);if(b)b.classList.add("pk-loading");
  const t=performance.now();
  const iv=setInterval(()=>{
    if(!img.isConnected||!img.getAttribute("src")){clearInterval(iv);if(img._pkArmTimer===iv)img._pkArmTimer=0;return}
    if(img.complete&&img.naturalWidth>0){clearInterval(iv);if(img._pkArmTimer===iv)img._pkArmTimer=0;img.classList.add("pk-live");img._pkTry=0;if(b)b.classList.remove("pk-loading")}
    else if(performance.now()-t>12000){clearInterval(iv);if(img._pkArmTimer===iv)img._pkArmTimer=0;pvRetry(img)}
  },250);
  img._pkArmTimer=iv;
}
function pvRetry(img){
  if(img._pkRetryTimer){clearTimeout(img._pkRetryTimer);img._pkRetryTimer=0}
  const n=(img._pkTry=(img._pkTry||0)+1);
  if(!img.dataset.stream||img.closest(".view")?.hidden||document.hidden)return;
  img._pkRetryTimer=setTimeout(()=>{
    img._pkRetryTimer=0;
    if(img.isConnected&&!img.closest(".view")?.hidden&&!document.hidden&&!(img.classList.contains("pk-live")))img.src=img.dataset.stream+"?t="+Date.now();
  },Math.min(5000,800*n));
}
new MutationObserver(ms=>{for(const m of ms){const i=m.target;if(i.tagName==="IMG"&&i.matches("img[data-stream]"))pvArm(i)}}).observe(document.body,{subtree:true,attributes:true,attributeFilter:["src"]});
$$("img[data-stream]").forEach(i=>{i.addEventListener("error",()=>{if(i.getAttribute("src"))pvRetry(i)});pvArm(i)});
document.addEventListener("visibilitychange",()=>{
  if(document.hidden)$$("img[data-stream]").forEach(i=>i.removeAttribute("src"));
  else if(typeof streams==="function")streams(true);
});
/* HUD on Live Console preview: measure output counters, not the throttled JPEG preview. */
const pb=$(".progbox");let hud=null;
if(pb){hud=document.createElement("div");hud.className="pk-hud";hud.innerHTML="<i></i><span>—</span>";pb.appendChild(hud)}
let lastC=null,lastT=0,lastChangeT=0,hudSource="",hudSamples=[];
setInterval(()=>{
  if(typeof S==="undefined"||!S||!hud||view!=="console")return;
  const active=!!S.running,useCamera=!!S.virtualcam;
  const source=useCamera?"vcam_frame_count":"render_frame_count";
  const count=Number(S[source]),now=performance.now();
  const target=Math.max(1,Number(S.effective_fps||S.fps)||30),label=useCamera?"Camera":"Output";
  if(!active||!Number.isFinite(count)||source!==hudSource){
    hudSource=source;lastC=active&&Number.isFinite(count)?count:null;lastT=now;lastChangeT=now;hudSamples=[];
    hud.querySelector("span").textContent=(active?"WARMING UP":"STOPPED")+" · "+label+" target "+target+" · "+S.width+"×"+S.height;
    hud.classList.toggle("on",active);return;
  }
  if(lastC===null){lastC=count;lastT=now;lastChangeT=now;return;}
  const elapsed=now-lastT,delta=count-lastC;lastC=count;lastT=now;
  if(delta>0&&elapsed>0){lastChangeT=now;hudSamples.push({count,time:now});if(hudSamples.length>4)hudSamples.shift();}
  let measured=null;
  if(hudSamples.length>=2){const first=hudSamples[0],last=hudSamples[hudSamples.length-1],span=last.time-first.time;if(span>=500&&last.count>=first.count)measured=(last.count-first.count)*1000/span;}
  if(measured===null&&delta>0&&elapsed>0)measured=delta*1000/elapsed;
  const status=now-lastChangeT>2500?"NO NEW FRAMES":(measured===null?"— FPS":Math.round(measured)+" FPS");
  hud.querySelector("span").textContent=status+" · "+label+" target "+target+" · "+S.width+"×"+S.height;
  hud.classList.toggle("on",active&&now-lastChangeT<=2500);
},1000);
const off=document.createElement("div");off.className="pk-offline";off.setAttribute("role","alert");
off.innerHTML='<span class="sp"></span><span id="pkOffTxt"></span><button class="btn" id="pkOffBtn" type="button" hidden></button>';
document.body.appendChild(off);
const offBtn=$("#pkOffBtn");
if(B.restartBackend){offBtn.hidden=false;offBtn.addEventListener("click",async()=>{offBtn.disabled=true;try{await B.restartBackend()}catch(_){}setTimeout(()=>{offBtn.disabled=false},4000)})}
let offSince=0;
setInterval(()=>{
  const isOff=(typeof online!=="undefined"&&online===false);
  offSince=isOff?(offSince||Date.now()):0;
  const baseMsg=T("Backend ไม่ตอบสนอง — กำลังเชื่อมต่อใหม่…","Backend not responding — reconnecting…");
  const detail=isOff?String(window.__pkPollError||"").slice(0,180):"";
  $("#pkOffTxt").textContent=baseMsg+(detail?" ("+detail+")":"");
  if(offBtn)offBtn.textContent=T("รีสตาร์ท Backend","Restart backend");
  off.classList.toggle("show",isOff&&window.__pkBootDone&&Date.now()-offSince>1800);
  const dot=$("#pkFootDot"),tx=$("#pkFootTxt");
  if(dot&&tx){dot.className="dot "+(online===true?"ok":online===false?"bad":"warn");
    tx.textContent=(online===true?T("พร้อมใช้งาน","Ready"):online===false?T("ไม่เชื่อมต่อ","Offline"):T("กำลังเชื่อมต่อ…","Connecting…"))+(tx.dataset.ver?" · "+tx.dataset.ver:"")}
  const bs=$("#pkBackendState");if(bs)bs.textContent=online===true?"Online":online===false?"Offline":"…";
},500);
if(B.onBackend)B.onBackend(m=>{
  if(!m)return;
  if(m.state==="restarting")notify(T("Backend หยุดทำงาน กำลังรีสตาร์ทอัตโนมัติ…","Backend stopped — restarting automatically…"),false);
  else if(m.state==="ready"&&window.__pkBootDone)notify(T("Backend กลับมาพร้อมใช้งานแล้ว","Backend is back online"));
  else if(m.state==="failed")notify(T("Backend เริ่มทำงานไม่สำเร็จ — เปิดโฟลเดอร์ Log เพื่อตรวจสอบ","Backend failed to start — check the log folder"),false);
});


/* ───────── Ctrl+K command palette ───────── */
const PALETTE_ITEMS=[
  {label:{th:"Dashboard",en:"Dashboard"},icon:"#i-grid",view:"dashboard"},
  {label:{th:"Scene Builder",en:"Scene Builder"},icon:"#i-layers",view:"scenes"},
  {label:{th:"Video to Filter",en:"Video to Filter"},icon:"#i-wand",view:"video-to-filter"},
  {label:{th:"Video Editor",en:"Video Editor"},icon:"#i-film",view:"editor"},
  {label:{th:"AI Seam Studio",en:"AI Seam Studio"},icon:"#i-wand",view:"ai-seam"},
  {label:{th:"AI Studio",en:"AI Studio"},icon:"#i-wand",view:"ai"},
  {label:{th:"Media",en:"Media"},icon:"#i-image",view:"media"},
  {label:{th:"Live Console",en:"Live Console"},icon:"#i-radio",view:"console"},
  {label:{th:"Comments",en:"Comments"},icon:"#i-chat",view:"comments"},
  {label:{th:"LIVE Manager",en:"LIVE Manager"},icon:"#i-bolt",view:"manager"},
  {label:{th:"TikTok Dashboard",en:"TikTok Dashboard"},icon:"#i-globe",view:"tiktok-dashboard"},
  {label:{th:"Accounts",en:"Accounts"},icon:"#i-users",view:"accounts"},
  {label:{th:"Logs",en:"Logs"},icon:"#i-log",view:"logs"},
  {label:{th:"Settings",en:"Settings"},icon:"#i-sliders",view:"settings"},
  {label:{th:"ตั้งค่า › รูปลักษณ์",en:"Settings › Appearance"},icon:"#i-palette",action:()=>openSettings("appearance")},
  {label:{th:"ตั้งค่า › ระบบ",en:"Settings › System"},icon:"#i-cpu",action:()=>openSettings("system")},
  {label:{th:"ตั้งค่า › เกี่ยวกับ",en:"Settings › About"},icon:"#i-monitor",action:()=>openSettings("about")},
  {label:{th:"รีสตาร์ท Backend",en:"Restart backend"},icon:"#i-refresh",action:async()=>{if(B.restartBackend){notify(T("กำลังรีสตาร์ท Backend…","Restarting backend…"));try{await B.restartBackend()}catch(e){notify(e.message,false)}}}},
  {label:{th:"รีโหลดหน้า",en:"Reload page"},icon:"#i-refresh",action:()=>location.reload()},
  {label:{th:"เปิดโฟลเดอร์ Log",en:"Open log folder"},icon:"#i-log",action:()=>B.openPath&&B.openPath("logs")},
  {label:{th:"เปิดโฟลเดอร์ข้อมูล",en:"Open data folder"},icon:"#i-monitor",action:()=>B.openPath&&B.openPath("data")},
];

/* build palette DOM */
let pal=null,palInput=null,palList=null,palOpen=false,palIdx=0,palQuery="";
function buildPalette(){
  if(pal)return;
  pal=document.createElement("div");pal.id="pkPalette";pal.className="pk-pal";pal.setAttribute("role","dialog");pal.setAttribute("aria-modal","true");pal.setAttribute("aria-label",T("ค้นหา","Search"));
  pal.innerHTML=`<div class="pk-pal-box"><div class="pk-pal-in"><svg class="i"><use href="#i-search"/></svg><input id="pkPalInput" type="text" autocomplete="off" spellcheck="false" placeholder="${T("ค้นหาเมนู / คำสั่ง…","Search pages / commands…")}"><button class="pk-pal-close" id="pkPalClose" type="button" aria-label="${T("ปิด","Close")}"><kbd>Esc</kbd></button></div><ul class="pk-pal-list" id="pkPalList" role="listbox"></ul></div>`;
  document.body.appendChild(pal);
  palInput=$("#pkPalInput");palList=$("#pkPalList");
  pal.addEventListener("mousedown",e=>{if(e.target===pal)closePalette()});
  $("#pkPalClose").addEventListener("click",closePalette);
  palInput.addEventListener("input",()=>renderPalette(palInput.value));
  palInput.addEventListener("keydown",onPalKey);
}
function renderPalette(q){
  palQuery=q.trim().toLowerCase();
  const en=L()==="en";
  const items=PALETTE_ITEMS.filter(it=>{
    if(it.view){
      const nav=document.querySelector('.side a[data-view="'+it.view+'"]');
      if(!nav||nav.hidden||nav.classList.contains("pk-nav-hidden"))return false;
    }    const lbl=(en?it.label.en:it.label.th).toLowerCase();
    return !palQuery||lbl.includes(palQuery);
  });
  palList.innerHTML=items.map((it,i)=>{
    const lbl=en?it.label.en:it.label.th;
    const hi=palQuery?lbl.replace(new RegExp("("+palQuery.replace(/[.*+?^${}()|[\]\\]/g,"\\$&")+")","gi"),"<mark>$1</mark>"):lbl;
    return `<li role="option" data-pi="${i}" class="pk-pal-it ${i===palIdx?"sel":""}">${it.icon?`<svg class="i"><use href="${it.icon}"/></svg>`:""}<span>${hi}</span></li>`;
  }).join("");
  palIdx=0;$$("li[data-pi]",palList).forEach((li,i)=>li.addEventListener("mousedown",e=>{e.preventDefault();palIdx=i;execPaletteItem(items[i])}));
  if(items.length)$$("li",palList)[0]?.classList.add("sel");
  pal._items=items;
}

function onPalKey(e){
  const lis=$$("li[data-pi]",palList);const n=lis.length;
  if(!n)return;
  if(e.key==="ArrowDown"){e.preventDefault();palIdx=(palIdx+1)%n;lis.forEach((l,i)=>l.classList.toggle("sel",i===palIdx));lis[palIdx]?.scrollIntoView({block:"nearest"})}
  else if(e.key==="ArrowUp"){e.preventDefault();palIdx=(palIdx-1+n)%n;lis.forEach((l,i)=>l.classList.toggle("sel",i===palIdx));lis[palIdx]?.scrollIntoView({block:"nearest"})}
  else if(e.key==="Enter"){e.preventDefault();const it=(pal._items||[])[palIdx];if(it)execPaletteItem(it)}
  else if(e.key==="Escape"){e.preventDefault();closePalette()}
}
function execPaletteItem(it){
  closePalette();
  if(it.action){it.action();return}
  if(it.view){location.hash="#"+it.view}
}
function openPalette(){
  buildPalette();palOpen=true;
  pal.classList.add("open");
  palInput.value="";renderPalette("");
  requestAnimationFrame(()=>palInput.focus());
}
function closePalette(){
  if(!pal)return;palOpen=false;pal.classList.remove("open");
  setTimeout(()=>{if(!palOpen&&palInput)palInput.value=""},200);
}
/* trigger from sidebar search button */
$("#pkSearch")?.addEventListener("click",openPalette);

/* ───────── keyboard shortcuts ───────── */
document.addEventListener("keydown",e=>{
  /* Ctrl+K / Cmd+K → palette */
  if((e.ctrlKey||e.metaKey)&&e.key==="k"){e.preventDefault();palOpen?closePalette():openPalette();return}
  /* Escape → close palette or settings */
  if(e.key==="Escape"&&palOpen){closePalette();return}
  /* Ctrl+, → settings */
  if((e.ctrlKey||e.metaKey)&&e.key===","){e.preventDefault();openSettings("appearance");return}
  /* Ctrl+Shift+L → logs */
  if((e.ctrlKey||e.metaKey)&&e.shiftKey&&e.key==="L"){e.preventDefault();location.hash="#logs";return}
  /* [ / ] → previous / next view */
  if((e.ctrlKey||e.metaKey)&&!e.shiftKey&&(e.key==="["||e.key==="]")){
    e.preventDefault();
    const views=$$("[data-view]",document.querySelector("nav.side")).map(a=>a.dataset.view);
    const cur=location.hash.replace("#","");
    const ci=views.indexOf(cur);if(ci<0)return;
    const ni=e.key==="["?(ci-1+views.length)%views.length:(ci+1)%views.length;
    location.hash="#"+views[ni];
  }
});

/* ───────── startup (init sequence) ───────── */
function pkInit(){
  buildAppearance();
  pkApply();
  pkSync();
  pkI18n();
  fillAll();
  setupMenuVisibilitySettings();
  setupSidebarGroups();
  /* restore last settings tab */
  if(location.hash==="#settings"){
    let t="appearance";try{t=localStorage.getItem("paokra.settings.tab")||t}catch(_){}
    pkTab(t);
  }
  loadAbout();
  /* language observer — re-run i18n when UI_CFG.language changes */
  let _lang=UI_CFG.language;
  setInterval(()=>{if(UI_CFG.language!==_lang){_lang=UI_CFG.language;pkI18n();if(palInput){palInput.placeholder=T("ค้นหาเมนู / คำสั่ง…","Search pages / commands…")};renderPalette&&palOpen&&renderPalette(palInput?.value||"")}},800);
}
if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",pkInit);
else pkInit();

/* ───────── close file helpers ───────── */
/* expose pkCloseFile globally so app.js / views can call it */
window.pkCloseFile=function pkCloseFile(opts){
  opts=opts||{};
  /* notify if needed */
  if(opts.label)notify(T("ปิดไฟล์: ","Closed: ")+opts.label);
  /* reset preview images */
  $$("img[data-stream]").forEach(i=>{i.removeAttribute("src");i.classList.remove("pk-live");const b=pvBox(i);if(b)b.classList.remove("pk-loading")});
  /* fire custom event so other modules can react */
  document.dispatchEvent(new CustomEvent("pk:close-file",{detail:opts}));
};
/* listen for close-file from Electron IPC */
if(B.onCloseFile)B.onCloseFile(info=>window.pkCloseFile(info||{}));

})(); /* end PAOKRA UI IIFE */
