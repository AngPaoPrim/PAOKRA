"use strict";
const $=(s,r=document)=>r.querySelector(s),$$=(s,r=document)=>[...r.querySelectorAll(s)];
const TITLES={dashboard:"Dashboard",accounts:"Accounts",media:"Media",scenes:"Scene Builder",["video-to-filter"]:"Video to Filter",editor:"Video Editor",["ai-seam"]:"AI Seam Studio",ai:"AI Studio",console:"Live Console",manager:"LIVE Manager",["tiktok-dashboard"]:"TikTok Dashboard",comments:"Comments",logs:"Logs",settings:"Settings"};
const TEXT_FS=28,TEXT_OFF=26;
let S=null,online=null,view="dashboard",drag=null,toastT=null,nudgeT=null,accSig="",CS=null,selectedCommentId=null,sceneInspectorRequested="";
let stateSnapshotAt=performance.now(),perfSnapshot=null;
const LOG=[];
const J={"Content-Type":"application/json"};
const esc=s=>String(s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const base=p=>String(p||"").split(/[\\/]+/).pop();
const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
const hhmmss=d=>d.toTimeString().slice(0,8);
const fmtBytes=n=>{n=Number(n)||0;if(n>=1073741824)return (n/1073741824).toFixed(2)+" GB";if(n>=1048576)return (n/1048576).toFixed(1)+" MB";if(n>=1024)return (n/1024).toFixed(1)+" KB";return Math.max(0,Math.round(n))+" B"};
const fmtSize=fmtBytes;
let paokraUpdateActivitySignalAt=0;
function signalUpdateUserActivity(){const now=Date.now();if(now-paokraUpdateActivitySignalAt<1500)return;paokraUpdateActivitySignalAt=now;try{window.paokra?.updateActivity?.()}catch{}}
for(const eventName of ["pointerdown","pointermove","keydown","input","change"]){window.addEventListener(eventName,signalUpdateUserActivity,{capture:true,passive:true})}

async function api(url,opt={}){
  const res=await fetch(url,opt);let data={};
  try{data=await res.json()}catch{}
  if(!res.ok)throw new Error(data.error||("HTTP "+res.status));
  return data;
}
const post=(u,b)=>api(u,{method:"POST",headers:J,body:JSON.stringify(b)});

function log(level,m){LOG.unshift({t:new Date(),level,m});if(LOG.length>300)LOG.length=300;renderActivity();if(view==="logs")renderLogs()}
function notify(m,ok=true){
  const el=$("#msg");el.textContent=m;el.className="msg "+(ok?"ok":"bad");el.hidden=false;
  clearTimeout(toastT);toastT=setTimeout(()=>{el.hidden=true},ok?3500:7000);
  log(ok?"INFO":"ERROR",m);
}
async function run(fn,okMsg){try{const r=await fn();if(okMsg)notify(okMsg);await poll();return r}catch(e){notify(e.message,false)}}
async function launchAIStudio(){
  try{
    if(!window.paokra?.openAIStudio)throw new Error("AI Studio bridge ไม่พร้อม");
    const r=await window.paokra.openAIStudio();
    if(!r?.ok)throw new Error(r?.error||"เปิด PAOKRA AI Studio ไม่สำเร็จ");
    notify("เปิด PAOKRA AI Studio แล้ว");
    const s=$("#aiStatus");if(s)s.textContent="Running";
  }catch(e){notify(e.message,false)}
}

const setCfg=async o=>{Object.assign(S,o);await post("/api/settings",o)};
let layerWriteQueue=Promise.resolve();
function commitLayerOrder(order){
  const requested=Array.isArray(order)?[...order]:[];
  S.layer_order=requested;
  renderLayers($("#layerList"));renderLayers($("#consoleLayers"));renderSceneInspectorPanels();
  // Serialize writes using the exact order requested by each action. The old
  // closure read S.layer_order only when its queue item ran, so rapid reorder
  // clicks could collapse into an unrelated/stale request. Do not swallow a
  // failed API write: callers must not display a false success toast.
  const write=layerWriteQueue.catch(()=>{}).then(async()=>{
    const r=await post("/api/layers/reorder",{order:requested});
    if(!r?.ok||!Array.isArray(r.order))throw new Error(r?.error||"Layer order was not saved by the backend");
    const latest=Array.isArray(S.layer_order)?S.layer_order:[];
    if(latest.length===requested.length&&latest.every((id,i)=>id===requested[i]))S.layer_order=[...r.order];
    renderLayers($("#layerList"));renderLayers($("#consoleLayers"));renderSceneInspectorPanels();
    return r;
  });
  layerWriteQueue=write;
  return write;
}


/* ---------- Scene Builder FX ---------- */
const FX_DEFAULT={brightness:0,contrast:1,saturation:1,warmth:0,sharpen:0,vignette:0,skin_smooth:0,face_slim:0,cheekbone:0,temple:0,jaw_refine:0,chin:0,eye_size:0};
let fxTimer=null;
function fxState(){return {...FX_DEFAULT,...(S?.video_fx||{})}}
function renderFX(){
  const f=fxState(),set=(id,v)=>{const el=$(id);if(el&&document.activeElement!==el)el.value=String(v)};
  set("#fxBrightness",f.brightness);set("#fxContrast",Math.round(f.contrast*100));
  set("#fxSaturation",Math.round(f.saturation*100));set("#fxWarmth",Math.round(f.warmth*100));
  set("#fxSharpen",f.sharpen);set("#fxVignette",f.vignette);set("#fxSkin",f.skin_smooth);
  set("#fxFace",f.face_slim);set("#fxJaw",f.jaw_refine);set("#fxEye",f.eye_size);
  const vals={fxSharpenValue:f.sharpen,fxVignetteValue:f.vignette,fxSkinValue:f.skin_smooth,fxFaceValue:f.face_slim,fxJawValue:f.jaw_refine,fxEyeValue:f.eye_size};
  for(const [id,v] of Object.entries(vals)){const el=$("#"+id);if(el)el.textContent=String(v)}
}
function queueFX(patch){
  // Keep slider updates local while dragging; persist after the user pauses.
  S.video_fx={...FX_DEFAULT,...(S.video_fx||{}),...patch};
  renderFX();
  clearTimeout(fxTimer);
  fxTimer=setTimeout(async()=>{
    const fx={...S.video_fx};
    try{ await post("/api/settings",{video_fx:fx}); }
    catch(e){ notify(e.message,false); }
  },250);
}
const FX_BINDINGS={
  "#fxJawWidth":v=>({jaw_width:Number(v)}),"#fxJawCurve":v=>({jaw_curve:Number(v)}),"#fxJawAngle":v=>({jaw_angle:Number(v)}),
  "#fxChinHeight":v=>({chin_height:Number(v)}),"#fxChinRoundness":v=>({chin_roundness:Number(v)}),"#fxChinTip":v=>({chin_tip:Number(v)}),
  "#fxBrightness":v=>({brightness:Number(v)}),"#fxContrast":v=>({contrast:Number(v)/100}),
  "#fxSaturation":v=>({saturation:Number(v)/100}),"#fxWarmth":v=>({warmth:Number(v)/100}),
  "#fxSharpen":v=>({sharpen:Number(v)}),"#fxVignette":v=>({vignette:Number(v)}),
  "#fxSkin":v=>({skin_smooth:Number(v)}),"#fxFace":v=>({face_slim:Number(v)}),
  "#fxCheekbone":v=>({cheekbone:Number(v)}),"#fxTemple":v=>({temple:Number(v)}),
  "#fxJaw":v=>({jaw_refine:Number(v)}),"#fxChin":v=>({chin:Number(v)}),"#fxEye":v=>({eye_size:Number(v)})
};
for(const [id,make] of Object.entries(FX_BINDINGS))$(id)?.addEventListener("input",e=>queueFX(make(e.target.value)));
$$("[data-fx-preset]").forEach(b=>b.addEventListener("click",()=>{
  const p=b.dataset.fxPreset;
  const sets={
    clean:{brightness:3,contrast:1.04,saturation:1.03,warmth:0,sharpen:12,vignette:0},
    soft:{brightness:2,contrast:0.98,saturation:0.98,warmth:-0.05,sharpen:4,vignette:10},
    natural:{brightness:0,contrast:1,saturation:1,warmth:0,sharpen:0,vignette:0}
  };
  queueFX(sets[p]||sets.natural);notify("ใช้ Preset "+p+" แล้ว");
}));
$("#fxResetAll")?.addEventListener("click",()=>{queueFX({...FX_DEFAULT});notify("รีเซ็ต Video FX แล้ว")});
$$("[data-scene-tab]").forEach(tab=>tab.addEventListener("click",()=>{
  const name=tab.dataset.sceneTab;
  $$("[data-scene-tab]").forEach(x=>x.classList.toggle("on",x===tab));
  $$("[data-scene-panel]").forEach(p=>{const on=p.dataset.scenePanel===name;p.hidden=!on;p.classList.toggle("on",on)});
}));


// ---------- Video to Filter ----------
let V2F={file:null,url:null,busy:false,zoom:1,panX:0,panY:0,previewTimer:null,previewBusy:false,previewPending:false,previewUrl:null,playPreviewTimer:null,assetVersion:"20261002-face-retouch3"};
const V2F_DEFAULT={brightness:0,contrast:100,saturation:100,warmth:0,sharpen:0,vignette:0,skin_smooth:0,wrinkle_remove:0,skin_bright:0,lip_tint:0,face_slim:0,forehead_width:0,forehead_height:0,midface_width:0,cheekbone:0,temple:0,face_length:0,jaw_refine:0,jaw_width:0,jaw_curve:0,jaw_angle:0,chin:0,chin_height:0,chin_roundness:0,chin_tip:0,eye_size:0};
function v2fEls(){return {video:$("#v2fVideo"),seek:$("#v2fSeek"),play:$("#v2fPlay"),empty:$("#v2fEmpty"),name:$("#v2fName"),time:$("#v2fTime"),download:$("#v2fDownload")}}
function v2fTime(t){t=Math.max(0,Number(t)||0);const m=Math.floor(t/60),s=Math.floor(t%60);return String(m).padStart(2,"0")+":"+String(s).padStart(2,"0")}
function v2fFX(){
  return {
    brightness:Number($("#v2fBrightness")?.value||0),
    contrast:Number($("#v2fContrast")?.value||100)/100,
    saturation:Number($("#v2fSaturation")?.value||100)/100,
    warmth:Number($("#v2fWarmth")?.value||0)/100,
    sharpen:Number($("#v2fSharpen")?.value||0),
    vignette:Number($("#v2fVignette")?.value||0),
    skin_smooth:Number($("#v2fSkin")?.value||0),
    wrinkle_remove:Number($("#v2fWrinkle")?.value||0),
    skin_bright:Number($("#v2fSkinBright")?.value||0),
    lip_tint:Number($("#v2fLip")?.value||0),
    face_slim:Number($("#v2fFace")?.value||0),
    forehead_width:Number($("#v2fForeheadWidth")?.value||0),
    forehead_height:Number($("#v2fForeheadHeight")?.value||0),
    midface_width:Number($("#v2fMidface")?.value||0),
    cheekbone:Number($("#v2fCheekbone")?.value||0),
    temple:Number($("#v2fTemple")?.value||0),
    face_length:Number($("#v2fFaceLength")?.value||0),
    jaw_refine:Number($("#v2fJaw")?.value||0),
    jaw_width:Number($("#v2fJawWidth")?.value||0),
    jaw_curve:Number($("#v2fJawCurve")?.value||0),
    jaw_angle:Number($("#v2fJawAngle")?.value||0),
    chin:Number($("#v2fChin")?.value||0),
    chin_height:Number($("#v2fChinHeight")?.value||0),
    chin_roundness:Number($("#v2fChinRoundness")?.value||0),
    chin_tip:Number($("#v2fChinTip")?.value||0),
    eye_size:Number($("#v2fEye")?.value||0)
  };
}
const V2F_SLIDER_IDS=[
  "v2fBrightness","v2fContrast","v2fSaturation","v2fWarmth","v2fSharpen","v2fVignette",
  "v2fSkin","v2fWrinkle","v2fSkinBright","v2fLip","v2fFace","v2fForeheadWidth","v2fForeheadHeight",
  "v2fMidface","v2fCheekbone","v2fTemple","v2fFaceLength","v2fJaw","v2fJawWidth","v2fJawCurve",
  "v2fJawAngle","v2fChin","v2fChinHeight","v2fChinRoundness","v2fChinTip","v2fEye"
];
function v2fBindSliders(){
  for(const id of V2F_SLIDER_IDS){
    const el=document.getElementById(id);
    if(!el||el.dataset.v2fBound)continue;
    el.dataset.v2fBound="1";
    el.addEventListener("input",()=>v2fPreviewFX());
    el.addEventListener("change",()=>v2fPreviewFX());
  }
}
function v2fPreviewFX(){
  const f=v2fFX(),v=v2fEls().video;if(!v)return;
  const contrast=f.contrast.toFixed(2),sat=f.saturation.toFixed(2);
  const b=Math.max(-1,Math.min(1,f.brightness/100));
  const brightness=(1+b).toFixed(2);
  const warm=f.warmth,hue=warm<0?(Math.abs(warm)*-18):0;
  v.style.filter=`brightness(${brightness}) contrast(${contrast}) saturate(${sat}) sepia(${warm>0?warm.toFixed(2):0}) hue-rotate(${hue.toFixed(0)}deg)`;
  v.style.opacity="1";
  const vals={v2fBrightnessVal:$("#v2fBrightness")?.value,v2fContrastVal:$("#v2fContrast")?.value,v2fSaturationVal:$("#v2fSaturation")?.value,v2fWarmthVal:$("#v2fWarmth")?.value,v2fSharpenVal:$("#v2fSharpen")?.value,v2fVignetteVal:$("#v2fVignette")?.value,v2fSkinVal:$("#v2fSkin")?.value,v2fWrinkleVal:$("#v2fWrinkle")?.value,v2fSkinBrightVal:$("#v2fSkinBright")?.value,v2fLipVal:$("#v2fLip")?.value,v2fFaceVal:$("#v2fFace")?.value,v2fForeheadWidthVal:$("#v2fForeheadWidth")?.value,v2fForeheadHeightVal:$("#v2fForeheadHeight")?.value,v2fMidfaceVal:$("#v2fMidface")?.value,v2fCheekboneVal:$("#v2fCheekbone")?.value,v2fTempleVal:$("#v2fTemple")?.value,v2fFaceLengthVal:$("#v2fFaceLength")?.value,v2fJawVal:$("#v2fJaw")?.value,v2fJawWidthVal:$("#v2fJawWidth")?.value,v2fJawCurveVal:$("#v2fJawCurve")?.value,v2fJawAngleVal:$("#v2fJawAngle")?.value,v2fChinVal:$("#v2fChin")?.value,v2fChinHeightVal:$("#v2fChinHeight")?.value,v2fChinRoundnessVal:$("#v2fChinRoundness")?.value,v2fChinTipVal:$("#v2fChinTip")?.value,v2fEyeVal:$("#v2fEye")?.value};
  for(const [id,val] of Object.entries(vals)){const e=$("#"+id);if(e)e.textContent=String(val??0)}
  if(v.paused || UI_CFG.smoothOriginal===false)v2fQueueDetailPreview();
}
function v2fApplyView(){
  const t=`translate(${V2F.panX}px,${V2F.panY}px) scale(${V2F.zoom})`;
  [$("#v2fVideo"),$("#v2fPreviewFrame")].filter(Boolean).forEach(el=>el.style.transform=t);
}
function v2fSetZoom(z){
  V2F.zoom=Number(z)||1;
  if(V2F.zoom<=1){V2F.panX=0;V2F.panY=0}
  const vp=$("#v2fViewport");if(!vp)return;
  vp.classList.toggle("v2f-detail",V2F.zoom>1);
  v2fApplyView();
  $$("[data-v2f-zoom]").forEach(b=>b.classList.toggle("active",Number(b.dataset.v2fZoom)===V2F.zoom));
}
async function v2fDetailPreview(){
  const E=v2fEls();
  if(!E.video||!V2F.file)return;
  if(V2F.previewBusy){V2F.previewPending=true;return}
  V2F.previewBusy=true;V2F.previewPending=false;
  try{
    const maxW=640, scale=Math.min(1,maxW/(E.video.videoWidth||maxW));
    const c=document.createElement("canvas");
    c.width=Math.max(2,Math.round((E.video.videoWidth||maxW)*scale));
    c.height=Math.max(2,Math.round((E.video.videoHeight||c.width*9/16)*scale));
    const ctx=c.getContext("2d");ctx.drawImage(E.video,0,0,c.width,c.height);
    const blob=await new Promise(res=>c.toBlob(res,"image/jpeg",0.86));if(!blob)return;
    const fd=new FormData();fd.append("frame",blob,"preview.jpg");fd.append("fx",JSON.stringify(v2fFX()));
    const r=await fetch("/api/video-filter/preview",{method:"POST",body:fd});
    if(!r.ok){let detail="";try{const d=await r.json();detail=d.error||""}catch{}throw new Error(detail||("preview failed (HTTP "+r.status+")"));}
    const out=await r.blob(),url=URL.createObjectURL(out);
    if(V2F.previewUrl)URL.revokeObjectURL(V2F.previewUrl);V2F.previewUrl=url;
    const img=$("#v2fPreviewFrame");if(img){img.src=url;img.hidden=false}
    $("#v2fViewport")?.classList.add("v2f-adjusting");
    const st=$("#v2fPreviewState");if(st)st.textContent="Detail preview · current frame";
  }catch(e){const st=$("#v2fPreviewState");if(st)st.textContent="Preview unavailable · "+(e.message||"error")}
  finally{V2F.previewBusy=false;if(V2F.previewPending){V2F.previewPending=false;v2fQueueDetailPreview()}}
}
function v2fQueueDetailPreview(){
  clearTimeout(V2F.previewTimer);
  if(V2F.file){
    const fps=clamp(Number(UI_CFG.filterPreviewFps)||4,1,15);
    const delay=Math.max(180,Math.round(1000/fps));
    V2F.previewTimer=setTimeout(v2fDetailPreview,delay);
  }
}
function v2fHasFaceFX(){
  const f=v2fFX();
  return [f.skin_smooth,f.wrinkle_remove,f.skin_bright,f.lip_tint,f.face_slim,
    f.forehead_width,f.forehead_height,f.midface_width,f.cheekbone,f.temple,
    f.face_length,f.jaw_refine,f.jaw_width,f.jaw_curve,f.jaw_angle,f.chin,
    f.chin_height,f.chin_roundness,f.chin_tip,f.eye_size].some(v=>Math.abs(Number(v))>0);
}
function v2fStopPlayPreview(){if(V2F.playPreviewTimer){clearInterval(V2F.playPreviewTimer);V2F.playPreviewTimer=null}}
function v2fStartPlayPreview(){
  // Never run server-side JPEG/Face-Mesh processing against a playing H.264 video.
  // Native Chromium playback remains the smooth 30/60 FPS path.
  v2fStopPlayPreview();
}
function v2fReset(){
  const map={brightness:"Brightness",contrast:"Contrast",saturation:"Saturation",warmth:"Warmth",sharpen:"Sharpen",vignette:"Vignette",skin_smooth:"Skin",wrinkle_remove:"Wrinkle",skin_bright:"SkinBright",lip_tint:"Lip",face_slim:"Face",forehead_width:"ForeheadWidth",forehead_height:"ForeheadHeight",midface_width:"Midface",cheekbone:"Cheekbone",temple:"Temple",face_length:"FaceLength",jaw_refine:"Jaw",jaw_width:"JawWidth",jaw_curve:"JawCurve",jaw_angle:"JawAngle",chin:"Chin",chin_height:"ChinHeight",chin_roundness:"ChinRoundness",chin_tip:"ChinTip",eye_size:"Eye"};
  for(const [k,v] of Object.entries(V2F_DEFAULT)){const e=$("#v2f"+map[k]);if(e)e.value=v}
  v2fSetZoom(1);
  v2fPreviewFX();
}
function v2fInit(){
  const E=v2fEls(); if(!E.video||E.video.dataset.v2fReady)return;
  E.video.dataset.v2fReady="1";
  $("#v2fAdd")?.addEventListener("click",()=>$("#v2fFile")?.click());
  $("#v2fFile")?.addEventListener("change",e=>{
    const f=e.target.files?.[0];e.target.value="";if(!f)return;
    if(V2F.url)URL.revokeObjectURL(V2F.url);
    V2F.file=f;V2F.url=URL.createObjectURL(f);V2F.panX=0;V2F.panY=0;v2fSetZoom(1);E.video.src=V2F.url;E.video.load();
    E.empty.hidden=true;E.play.disabled=true;E.seek.disabled=true;E.download.disabled=true;
    E.time.textContent="00:00 / 00:00";E.name.textContent=f.name;
    const status=$("#v2fPreviewState");if(status)status.textContent="Loading video metadata…";
  });
  E.video.addEventListener("loadedmetadata",()=>{
    E.seek.max=String(E.video.duration||0);E.seek.value="0";E.time.textContent="00:00 / "+v2fTime(E.video.duration);
    E.play.disabled=false;E.seek.disabled=false;E.download.disabled=false;
    const status=$("#v2fPreviewState");if(status)status.textContent="Video ready · filters apply to the paused preview";
    v2fPreviewFX();
  });
  E.video.addEventListener("error",()=>{
    if(!V2F.file)return;
    const code=Number(E.video.error?.code||0);
    E.play.disabled=true;E.seek.disabled=true;E.download.disabled=true;
    E.time.textContent="00:00 / 00:00";
    const status=$("#v2fPreviewState");
    if(status)status.textContent=code===4?"Unsupported codec or video stream — try an H.264 MP4 file.":"Video could not be decoded — try another file.";
  });
  E.video.addEventListener("timeupdate",()=>{
    if(document.activeElement!==E.seek)E.seek.value=String(E.video.currentTime);
    E.time.textContent=v2fTime(E.video.currentTime)+" / "+v2fTime(E.video.duration);
  });
  E.video.addEventListener("ended",()=>{E.play.textContent="▶ Play";v2fStopPlayPreview();if(V2F.file&&v2fHasFaceFX())v2fQueueDetailPreview()});
  E.video.addEventListener("play",()=>{
    // Keep the original H.264 decoder/render path untouched while playing.
    $("#v2fPreviewFrame")?.setAttribute("hidden","");
    $("#v2fViewport")?.classList.remove("v2f-adjusting");
    const st=$("#v2fPreviewState");if(st)st.textContent="Smooth H.264 playback · filter preview on pause";
    v2fStopPlayPreview();
  });
  E.video.addEventListener("seeked",()=>{if(V2F.file&&E.video.paused)v2fQueueDetailPreview()});
  E.video.addEventListener("pause",()=>{
    v2fStopPlayPreview();
    if(V2F.file&&v2fHasFaceFX())v2fQueueDetailPreview();
  });
  $$("[data-v2f-zoom]").forEach(b=>b.addEventListener("click",()=>v2fSetZoom(b.dataset.v2fZoom)));
  const zoomViewport=$("#v2fViewport");
  zoomViewport?.addEventListener("wheel",(ev)=>{
    ev.preventDefault();
    const step=ev.deltaY<0?0.1:-0.1;
    v2fSetZoom(Math.max(1,Math.min(8,Math.round((V2F.zoom+step)*10)/10)));
  },{passive:false});
  let v2fPanDrag=null;
  zoomViewport?.addEventListener("pointerdown",ev=>{
    if(V2F.zoom<=1||ev.target.closest(".v2f-zoombar"))return;
    ev.preventDefault();
    zoomViewport.setPointerCapture(ev.pointerId);
    v2fPanDrag={id:ev.pointerId,sx:ev.clientX,sy:ev.clientY,px:V2F.panX,py:V2F.panY};
    zoomViewport.classList.add("v2f-panning");
  });
  zoomViewport?.addEventListener("pointermove",ev=>{
    if(!v2fPanDrag||ev.pointerId!==v2fPanDrag.id)return;
    V2F.panX=v2fPanDrag.px+ev.clientX-v2fPanDrag.sx;
    V2F.panY=v2fPanDrag.py+ev.clientY-v2fPanDrag.sy;
    v2fApplyView();
  });
  const endV2FPan=ev=>{
    if(!v2fPanDrag||ev.pointerId!==v2fPanDrag.id)return;
    v2fPanDrag=null;zoomViewport.classList.remove("v2f-panning");
  };
  zoomViewport?.addEventListener("pointerup",endV2FPan);
  zoomViewport?.addEventListener("pointercancel",endV2FPan);
  E.play.addEventListener("click",async()=>{
    if(E.video.paused){try{await E.video.play();E.play.textContent="⏸ Pause"}catch(err){notify("เล่นวิดีโอไม่ได้: "+err.message,false)}}
    else{E.video.pause();E.play.textContent="▶ Play"}
  });
  E.seek.addEventListener("input",()=>{E.video.currentTime=Number(E.seek.value||0)});
  v2fSetZoom(1);
  $("#v2fReset")?.addEventListener("click",v2fReset);
  v2fBindSliders();
  $$("[data-v2f-tab]").forEach(tab=>tab.addEventListener("click",()=>{
    const n=tab.dataset.v2fTab;
    $$("[data-v2f-tab]").forEach(x=>x.classList.toggle("on",x===tab));
    $$("[data-v2f-panel]").forEach(p=>{const on=p.dataset.v2fPanel===n;p.hidden=!on;p.classList.toggle("on",on)});
  }));
  E.download.addEventListener("click",v2fExport);
}
async function v2fExport(){
  if(!V2F.file||V2F.busy)return;
  const E=v2fEls();V2F.busy=true;E.download.disabled=true;
  const box=$("#v2fProgress"),bar=$("#v2fProgressBar"),txt=$("#v2fProgressText");
  box.hidden=false;bar.style.width="0%";txt.textContent="กำลังส่งวิดีโอเข้า Offline Renderer… 0%";
  const jobId=(crypto.randomUUID?crypto.randomUUID():String(Date.now())+Math.random());
  const fx=JSON.stringify(v2fFX()),encoder=UI_CFG.exportEncoder||"auto";
  const upload=()=>new Promise((resolve,reject)=>{
    const xhr=new XMLHttpRequest();xhr.open("POST","/api/video-filter/export");xhr.responseType="blob";
    xhr.setRequestHeader("X-PAOKRA-Job-Id",jobId);
    xhr.upload.onprogress=e=>{if(e.lengthComputable){const p=Math.round(e.loaded/e.total*100);bar.style.width=Math.min(45,p*0.45)+"%";txt.textContent=`กำลังอัปโหลดวิดีโอ… ${p}%`}};
    xhr.onload=async()=>{if(xhr.status>=200&&xhr.status<300)resolve(xhr);else{let m="";try{m=JSON.parse(await xhr.response.text()).error||""}catch{}reject(new Error(m||`Export failed (HTTP ${xhr.status})`))}};
    xhr.onerror=()=>reject(new Error("ส่งวิดีโอไม่สำเร็จ: network error"));
    const fd=new FormData();fd.append("file",V2F.file,V2F.file.name);fd.append("fx",fx);fd.append("encoder",encoder);xhr.send(fd);
  });
  let timer=null;
  try{
    timer=setInterval(async()=>{try{const j=await api("/api/video-filter/progress/"+encodeURIComponent(jobId));
      if(j.phase==="render"){bar.style.width=Math.max(45,Number(j.progress)||45)+"%";txt.textContent=`Offline Renderer กำลังประมวลผล… ${Math.round(j.progress||0)}%`}
      else if(j.phase==="mux"){bar.style.width="90%";txt.textContent="กำลังรวมเสียงและสร้าง MP4… 90%"}
    }catch{}},500);
    let r;
    try{r=await upload()}catch(first){
      if(encoder!=="cpu"){txt.textContent="GPU encoder ใช้งานไม่ได้ — กำลังลอง CPU x264…";r=await new Promise((resolve,reject)=>{const xhr=new XMLHttpRequest();xhr.open("POST","/api/video-filter/export");xhr.responseType="blob";xhr.setRequestHeader("X-PAOKRA-Job-Id",jobId);xhr.onload=()=>xhr.status>=200&&xhr.status<300?resolve(xhr):reject(first);xhr.onerror=()=>reject(first);const fd=new FormData();fd.append("file",V2F.file,V2F.file.name);fd.append("fx",fx);fd.append("encoder","cpu");xhr.send(fd)})}else throw first;
    }
    clearInterval(timer);timer=null;bar.style.width="92%";txt.textContent="กำลังเตรียมไฟล์ Download…";
    const blob=r.response,cd=r.getResponseHeader("Content-Disposition")||"",m=cd.match(/filename="?([^";]+)"?/i);
    const filename=m?m[1]:"PAOKRA_"+V2F.file.name.replace(/\.[^.]+$/,"")+"_60fps.mp4";
    const url=URL.createObjectURL(blob),a=document.createElement("a");a.href=url;a.download=filename;document.body.appendChild(a);a.click();a.remove();URL.revokeObjectURL(url);
    bar.style.width="100%";txt.textContent="เสร็จแล้ว · 60 FPS MP4 พร้อมใช้";notify("Video to Filter export เสร็จแล้ว");
  }catch(e){clearInterval(timer);bar.style.width="0%";txt.textContent="Export ไม่สำเร็จ: "+e.message;notify(e.message,false)}
  finally{V2F.busy=false;E.download.disabled=!V2F.file}
}

/* ---------- AI Seam Studio: cached RIFE bridges, never edits source media ---------- */
let AI_SEAM={clips:[],seams:[],settings:{enabled:false,compute:"auto",max_side:720,transition_frames:8},environment:null,status:null,analysis:null};
let aiSeamSetupDone=false,aiSeamShowLimit=100,aiSeamFromId="",aiSeamToId="",aiSeamLoading=false,aiSeamJobTimer=null,aiSeamActiveJob="";
const AI_SEAM_TEXT={
  th:{hero:"AI Seam Studio",heroCopy:"เตรียมเฟรมคั่นระหว่างคลิปก่อนเริ่ม LIVE โดยไม่แก้ไฟล์ต้นฉบับ",safety:"ระบบจะใช้เฉพาะรอยต่อที่สร้างและตรวจไฟล์แล้ว หากไม่มีคู่ A → B ที่ตรงกันจะเล่น Playlist แบบเดิม",library:"คลังคลิป",libraryNote:"เลือกคลิปจากดิสก์โดยไม่คัดลอกวิดีโอต้นฉบับ",add:"+ เพิ่มคลิป",search:"ค้นหาชื่อคลิป…",from:"ต้นทาง (A)",to:"ปลายทาง (B)",selectA:"เลือกคลิป A",selectB:"เลือกคลิป B",showMore:"แสดงเพิ่ม",replace:"แทนที่ Normal Playlist…",replaceNote:"ต้องกดยืนยันเอง · ไม่แก้ Playlist อัตโนมัติ",environment:"สแกนสภาพแวดล้อม",environmentNote:"ทดสอบ inference จริงขนาดเล็กด้วยอุปกรณ์ที่เลือก",scan:"สแกนเครื่อง",settings:"ตั้งค่า AI",compute:"อุปกรณ์ประมวลผล",side:"ขนาดภาพ AI",frames:"จำนวนเฟรมรอยต่อ",enabled:"ใช้รอยต่อที่สร้างแล้วใน Clip Flow",enabledNote:"ใช้เฉพาะคู่ A → B ที่ตรงกับแคชที่ตรวจแล้ว",save:"บันทึกการตั้งค่า",batch:"โหมดประหยัดหน่วยความจำ: ทำ inference ทีละงาน เพื่อเหลือทรัพยากรให้ Windows และ Live",pair:"วิเคราะห์และสร้างรอยต่อ",pairNote:"เลือกคลิป A และ B วิเคราะห์ความเข้ากันได้ แล้วค่อยสร้างเฟรมคั่น",notAnalyzed:"ยังไม่ได้วิเคราะห์",frameA:"A · เฟรมสุดท้าย",frameB:"B · เฟรมแรก",analyze:"วิเคราะห์คู่คลิป",force:"อนุญาตคู่ที่เสี่ยง (ยืนยันเอง)",generate:"สร้าง Seam",analysisIntro:"RIFE คาดคะเนการเคลื่อนไหวจากภาพสองภาพ แต่ไม่เข้าใจฉากหรือรับประกันท่าทางเหมือนกัน ต้องตรวจพรีวิวก่อนเปิดใช้กับ Live",job:"สถานะการสร้าง",preview:"พรีวิวช่วงเปลี่ยนคลิป",previewNote:"แสดงคลิป A ช่วงท้าย → เฟรมคั่นที่สร้าง → คลิป B หลังจุดที่ระบบกลับไปเล่น เพื่อเช็กอาการวาปก่อนเปิดใช้",cache:"แคชรอยต่อที่ตรวจแล้ว",cacheNote:"เฉพาะคู่ที่สร้างสำเร็จเท่านั้นที่จะเปลี่ยนการเล่น ล้างแคชไม่ลบคลิปต้นฉบับ",clearCache:"ล้างแคชที่สร้าง",emptyClips:"ยังไม่มีคลิป กด เพิ่มคลิป เพื่อเริ่ม",noneCache:"ยังไม่มีรอยต่อที่สร้าง",gpu:"GPU · Vulkan",cpu:"CPU",auto:"Auto · สแกนแล้วเลือก",riskGood:"เข้ากันได้ค่อนข้างดี",riskCaution:"ควรตรวจพรีวิว",riskRisky:"ความเสี่ยงสูง",good:"สำเร็จ",failed:"ไม่ผ่าน",flowBusy:"หยุดหรือจบ Clip Flow ก่อนแก้คลังคลิป/แคช",choosePair:"เลือกคลิป A และ B คนละคลิปก่อน",scanOk:"ทดสอบ inference สำเร็จ",scanBad:"การทดสอบ inference ไม่ผ่าน",added:"เพิ่มคลิปเข้าคลังแล้ว",removed:"นำคลิปออกจากคลังแล้ว",settingsSaved:"บันทึกการตั้งค่า AI แล้ว",analyzed:"วิเคราะห์คู่คลิปแล้ว",generationStarted:"เริ่มสร้างรอยต่อในเบื้องหลังแล้ว",generationDone:"สร้างและตรวจไฟล์รอยต่อสำเร็จ",generationFailed:"สร้างรอยต่อไม่สำเร็จ",noDesktop:"ปุ่มเลือกไฟล์ต้องใช้แอป PAOKRA Studio บนเดสก์ท็อป",applyConfirm:"แทนที่รายการ Normal ทั้งหมดด้วยลำดับคลิปใน AI Seam Studio หรือไม่? คลิปต้นฉบับไม่ถูกลบ",cacheConfirm:"ลบรอยต่อที่สร้างทั้งหมดหรือไม่? ไฟล์คลิปต้นฉบับไม่ถูกลบ",deleteConfirm:"นำคลิปนี้ออกจากคลัง AI Seam? ไฟล์ต้นฉบับบนดิสก์จะไม่ถูกลบ",setFrom:"ใช้เป็น A",setTo:"ใช้เป็น B",up:"ขึ้น",down:"ลง",remove:"นำออก",previewAction:"ดูพรีวิว",enabledCache:"เปิดใช้แล้ว",disabledCache:"ปิดการใช้รอยต่อ",rifeMissing:"ไม่พบ RIFE/model/FFmpeg ครบชุด",flowWarning:"ขณะ Playlist ทำงาน จะไม่อนุญาตให้เปลี่ยนคลังหรือเคลียร์แคช",select:"เลือก",score:"คะแนนความใกล้เคียง",resolution:"ความละเอียดโมเดล",memory:"RAM ว่าง",disk:"ดิสก์ว่าง",device:"อุปกรณ์ตรวจพบ",inference:"ผลทดสอบจริง",cpuThreads:"เธรด CPU",models:"ไฟล์โมเดล"},
  en:{hero:"AI Seam Studio",heroCopy:"Pre-render transition frames between clips before LIVE. Source clips stay untouched.",safety:"Only generated and verified A → B pairs are inserted. Unknown or unmatched pairs use the normal Playlist handoff.",library:"Clip Library",libraryNote:"Select source clips from disk without duplicating the video files",add:"+ Add clips",search:"Search clip names…",from:"From (A)",to:"To (B)",selectA:"Select clip A",selectB:"Select clip B",showMore:"Show more",replace:"Replace Normal Playlist…",replaceNote:"Explicit confirmation · never changes Playlist automatically",environment:"Environment scan",environmentNote:"Runs a small real inference on the selected compute device",scan:"Scan environment",settings:"AI settings",compute:"Compute device",side:"AI input size",frames:"Bridge frames",enabled:"Use generated seams in Clip Flow",enabledNote:"Only exact, verified A → B cache matches are used",save:"Save AI settings",batch:"Memory-safe mode: one inference at a time, leaving resources for Windows and Live",pair:"Analyze & generate a seam",pairNote:"Choose A and B, analyze compatibility, then generate the bridge frames",notAnalyzed:"Not analyzed",frameA:"A · final frame",frameB:"B · first frame",analyze:"Analyze pair",force:"Allow risky pair (manual override)",generate:"Generate Seam",analysisIntro:"RIFE estimates motion between two images; it does not understand scenes or guarantee matching poses. Review the preview before enabling it for Live.",job:"Generation status",preview:"Clip handoff sequence preview",previewNote:"Shows A’s tail → generated bridge frames → B resuming after the bridge so you can inspect the handoff for jumps",cache:"Verified transition cache",cacheNote:"Only successfully generated pairs can alter playback. Clearing cache never deletes source clips.",clearCache:"Clear generated cache",emptyClips:"No clips yet. Choose Add clips to start.",noneCache:"No generated seams yet",gpu:"GPU · Vulkan",cpu:"CPU",auto:"Auto · scan and choose",riskGood:"Higher visual similarity",riskCaution:"Review preview carefully",riskRisky:"High transition risk",good:"Passed",failed:"Failed",flowBusy:"Stop or finish Clip Flow before modifying the library/cache",choosePair:"Choose two different clips A and B first",scanOk:"Inference probe passed",scanBad:"Inference probe failed",added:"Added clips to the library",removed:"Removed clip from library",settingsSaved:"AI settings saved",analyzed:"Pair analyzed",generationStarted:"Background generation started",generationDone:"Transition generated and verified",generationFailed:"Transition generation failed",noDesktop:"Native file selection requires the PAOKRA Studio desktop app",applyConfirm:"Replace the entire Normal Playlist with the AI Seam Studio library order? Source files will not be deleted",cacheConfirm:"Delete all generated seams? Original clips will not be deleted",deleteConfirm:"Remove this clip from the AI Seam library? The original disk file will remain",setFrom:"Set A",setTo:"Set B",up:"Up",down:"Down",remove:"Remove",previewAction:"Preview",enabledCache:"Seams enabled",disabledCache:"Seams disabled",rifeMissing:"RIFE/model/FFmpeg bundle incomplete",flowWarning:"Clip Flow is running; library/cache changes are blocked",select:"Select",score:"Visual similarity score",resolution:"Model resolution",memory:"Available RAM",disk:"Free disk",device:"Detected device",inference:"Inference probe",cpuThreads:"CPU threads",models:"Model files"}
};
function aiSeamText(key){const lang=(typeof UI_CFG!=="undefined"&&UI_CFG.language==="en")?"en":"th";return AI_SEAM_TEXT[lang][key]||AI_SEAM_TEXT.en[key]||key;}
function aiSeamApplyLanguage(){
 const map={aisHeroTitle:"hero",aisHeroCopy:"heroCopy",aisSafetyNote:"safety",aisLibraryTitle:"library",aisLibraryNote:"libraryNote",aisAddClips:"add",aisFromLabel:"from",aisToLabel:"to",aisShowMore:"showMore",aisApplyPlaylist:"replace",aisApplyNote:"replaceNote",aisEnvironmentTitle:"environment",aisEnvironmentNote:"environmentNote",aisScan:"scan",aisSettingsTitle:"settings",aisComputeLabel:"compute",aisResolutionLabel:"side",aisFramesLabel:"frames",aisEnableLabel:"enabled",aisEnableNote:"enabledNote",aisSaveSettings:"save",aisBatchNote:"batch",aisPairTitle:"pair",aisPairNote:"pairNote",aisPairRisk:"notAnalyzed",aisFrameAName:"frameA",aisFrameBName:"frameB",aisAnalyze:"analyze",aisForceLabel:"force",aisGenerate:"generate",aisPreviewLabel:"preview",aisPreviewNote:"previewNote",aisCacheTitle:"cache",aisCacheNote:"cacheNote",aisClearCache:"clearCache"};
 for(const [id,key] of Object.entries(map)){const el=$("#"+id);if(el)el.textContent=aiSeamText(key)}
 const search=$("#aisClipSearch");if(search)search.placeholder=aiSeamText("search");
 const compute=$("#aisCompute");if(compute&&compute.options.length===3){compute.options[0].textContent=aiSeamText("auto");compute.options[1].textContent=aiSeamText("gpu");compute.options[2].textContent=aiSeamText("cpu")}
 const videoTag=$("#aisModelTag");if(videoTag)videoTag.textContent="RIFE v4.6 · Vulkan / CPU · 1 worker";
 aiSeamRender();
}
function aiSeamFormatDuration(value){const n=Math.max(0,Number(value)||0);return n>=60?`${Math.floor(n/60)}m ${(n%60).toFixed(1)}s`:`${n.toFixed(2)}s`}
function aiSeamRender(){
 const list=$("#aisClipsList");if(list){
   const q=String($("#aisClipSearch")?.value||"").toLowerCase().trim();
   const all=AI_SEAM.clips.filter(c=>!q||String(c.name+" "+c.path).toLowerCase().includes(q));
   const visible=all.slice(0,aiSeamShowLimit);
   $("#aisClipCount").textContent=`${AI_SEAM.clips.length} ${AI_SEAM.clips.length===1?"clip":"clips"}`;
   list.innerHTML="";
   if(!visible.length){const empty=document.createElement("div");empty.className="unavail";empty.textContent=aiSeamText("emptyClips");list.appendChild(empty)}
   visible.forEach((clip,index)=>{
     const row=document.createElement("div");row.className="ais-clip-row"+(clip.id===aiSeamFromId?" selected-from":"")+(clip.id===aiSeamToId?" selected-to":"");
     const info=document.createElement("div");info.className="ais-clip-info";
     const name=document.createElement("div");name.className="ais-clip-name";name.textContent=clip.name||base(clip.path);name.title=clip.path;
     const meta=document.createElement("div");meta.className="ais-clip-meta";meta.textContent=`${clip.width||"?"}×${clip.height||"?"} · ${aiSeamFormatDuration(clip.duration)} · ${fmtBytes(clip.size||0)}`;
     info.append(name,meta);
     const actions=document.createElement("div");actions.className="ais-clip-actions";
     const addAction=(action,label,disabled=false)=>{const b=document.createElement("button");b.type="button";b.className="btn ghost";b.textContent=label;b.dataset.aisAct=action;b.dataset.aisId=clip.id;b.disabled=!!disabled;actions.appendChild(b)};
     addAction("from",aiSeamText("setFrom"));addAction("to",aiSeamText("setTo"));
     addAction("up",aiSeamText("up"),AI_SEAM.clips.indexOf(clip)===0);addAction("down",aiSeamText("down"),AI_SEAM.clips.indexOf(clip)===AI_SEAM.clips.length-1);
     addAction("remove",aiSeamText("remove"));row.append(info,actions);list.appendChild(row);
   });
   const more=$("#aisShowMore");if(more){more.hidden=all.length<=aiSeamShowLimit;more.textContent=`${aiSeamText("showMore")} (${Math.min(100,all.length-aiSeamShowLimit)} / ${all.length-aiSeamShowLimit})`}
 }
 const from=AI_SEAM.clips.find(c=>c.id===aiSeamFromId),to=AI_SEAM.clips.find(c=>c.id===aiSeamToId);
 const fromName=$("#aisFromName"),toName=$("#aisToName");if(fromName)fromName.textContent=from?.name||aiSeamText("selectA");if(toName)toName.textContent=to?.name||aiSeamText("selectB");
 const settings=AI_SEAM.settings||{};
 if($("#aisEnabled")&&document.activeElement!==$("#aisEnabled"))$("#aisEnabled").checked=!!settings.enabled;
 if($("#aisCompute")&&document.activeElement!==$("#aisCompute"))$("#aisCompute").value=settings.compute||"auto";
 if($("#aisMaxSide")&&document.activeElement!==$("#aisMaxSide"))$("#aisMaxSide").value=settings.resolution_mode==="auto"?"auto":String(settings.max_side||720);
 if($("#aisFrames")&&document.activeElement!==$("#aisFrames"))$("#aisFrames").value=String(settings.transition_frames||8);
 const pairValid=!!from&&!!to&&from.id!==to.id;
 const flowActive=!!AI_SEAM.status?.clip_flow_active;
 const computeBlocked=!!(AI_SEAM.status?.generation_blocked||AI_SEAM.status?.generation_busy||AI_SEAM.status?.engine_running||AI_SEAM.status?.virtualcam_active);
 if($("#aisAnalyze"))$("#aisAnalyze").disabled=!pairValid||aiSeamLoading||flowActive;
 if($("#aisGenerate"))$("#aisGenerate").disabled=!pairValid||aiSeamLoading||computeBlocked;
 if($("#aisScan"))$("#aisScan").disabled=computeBlocked;
 if($("#aisAddClips"))$("#aisAddClips").disabled=flowActive||computeBlocked;
 if($("#aisApplyPlaylist"))$("#aisApplyPlaylist").disabled=!AI_SEAM.clips.length||flowActive||computeBlocked;
 if($("#aisClearCache"))$("#aisClearCache").disabled=!AI_SEAM.seams.length||flowActive||computeBlocked;
 const scan=AI_SEAM.environment||AI_SEAM.status?.environment;
 const env=$("#aisEnvironment");if(env){
   if(!scan){env.innerHTML=`<div class="unavail">${esc(aiSeamText("environmentNote"))}</div>`}
   else{
     const device=(scan.gpu_device||scan.vulkan_devices?.join(", ")||"No Vulkan GPU detected")+
       (Number.isInteger(Number(scan.gpu_index))?" · Vulkan #"+Number(scan.gpu_index):"");
     const vram=scan.vram_total_bytes?fmtBytes(scan.vram_total_bytes)+" total · "+fmtBytes(scan.vram_available_bytes||0)+" safe-use estimate":"Unknown · safe mode";
     const items=[[aiSeamText("device"),device],["GPU VRAM",vram],[aiSeamText("memory"),fmtBytes(scan.ram_available_bytes)],[aiSeamText("disk"),fmtBytes(scan.disk_free_bytes)],[aiSeamText("resolution"),String(scan.recommended_max_side||settings.max_side||512)+"px"],[aiSeamText("cpuThreads"),String(scan.cpu_logical||"?")],[aiSeamText("models"),scan.rife_available&&scan.model_available&&scan.ffmpeg_available?aiSeamText("good"):aiSeamText("rifeMissing")]];
     env.innerHTML=`<div class="ais-env-grid">${items.map(([k,v])=>`<div class="ais-env-item"><small>${esc(k)}</small><b>${esc(v)}</b></div>`).join("")}</div><div class="ais-env-result ${scan.ok?"ok":"bad"}"><b>${scan.ok?esc(aiSeamText("scanOk")):esc(aiSeamText("scanBad"))}</b><p>${esc(scan.probe?.message||scan.note||"")}</p><p>${esc(scan.recommended_compute||"?").toUpperCase()} · pair batch 1 · RIFE jobs ${esc(scan.recommended_rife_jobs||"1:1:1")} · ${(Number(scan.probe?.seconds)||0).toFixed(2)}s probe</p><p class="muted small">${esc(scan.vram_note||"")}</p></div>`;
   }
 }
 const analysis=AI_SEAM.analysis;
 const risk=$("#aisPairRisk");if(risk){risk.textContent=analysis?({good:aiSeamText("riskGood"),caution:aiSeamText("riskCaution"),risky:aiSeamText("riskRisky")}[analysis.risk]||analysis.risk):aiSeamText("notAnalyzed");risk.className="tag"+(analysis?.risk==="good"?" live":analysis?.risk==="risky"?" warn":"")}
 const fa=$("#aisFrameA"),fb=$("#aisFrameB");
 if(fa){const u=from?`/api/ai-seam/clips/${encodeURIComponent(from.id)}/frame?at=tail&v=${encodeURIComponent(from.signature?.mtime_ns||"")}`:"";if(fa.dataset.src!==u){fa.dataset.src=u;fa.src=u;fa.style.visibility=u?"visible":"hidden"}}
 if(fb){const t=analysis&&analysis.from_id===aiSeamFromId&&analysis.to_id===aiSeamToId?Number(analysis.transition_seconds)||0:0;const u=to?`/api/ai-seam/clips/${encodeURIComponent(to.id)}/frame?at=${encodeURIComponent(t)}&v=${encodeURIComponent(to.signature?.mtime_ns||"")}`:"";if(fb.dataset.src!==u){fb.dataset.src=u;fb.src=u;fb.style.visibility=u?"visible":"hidden"}}
 const ma=$("#aisFrameAMeta"),mb=$("#aisFrameBMeta");if(ma)ma.textContent=from?`${from.width}×${from.height}`:"";if(mb)mb.textContent=to?`${to.width}×${to.height}`:"";
 const output=$("#aisAnalysis");if(output){if(analysis){const msg=analysis.risk==="good"?aiSeamText("riskGood"):analysis.risk==="caution"?aiSeamText("riskCaution"):aiSeamText("riskRisky");output.innerHTML=`<div class="ais-score"><strong>${Math.round((Number(analysis.score)||0)*100)}%</strong><span>${esc(aiSeamText("score"))} · ${esc(msg)}</span></div><p>${esc(from?.name||analysis.from_name||"")} → ${esc(to?.name||analysis.to_name||"")}</p><p>${esc(analysis.transition_frames)} frames · ${esc(analysis.fps)} FPS · ${esc(analysis.input_width)}×${esc(analysis.input_height)} · ${esc(aiSeamFormatDuration(analysis.transition_seconds))}</p><p>${esc(analysis.note||"")}</p>`}else output.innerHTML=`<p class="muted small">${esc(aiSeamText("analysisIntro"))}</p>`}
 const cache=$("#aisCacheList");if(cache){cache.innerHTML="";if(!AI_SEAM.seams.length){const el=document.createElement("div");el.className="unavail";el.textContent=aiSeamText("noneCache");cache.appendChild(el)}else AI_SEAM.seams.slice(0,100).forEach(seam=>{const row=document.createElement("div");row.className="ais-cache-row";const info=document.createElement("div"),head=document.createElement("b"),meta=document.createElement("p");head.textContent=`${seam.from_name||"Clip A"} → ${seam.to_name||"Clip B"}`;meta.textContent=`${Math.round((Number(seam.score)||0)*100)}% similarity · ${seam.frame_count||0} bridge frames · ${seam.fps||"?"} FPS · ${fmtBytes(seam.size||0)} · ${String(seam.compute||"?").toUpperCase()} · ${seam.compatible?"Output compatible":seam.available?"Needs regenerate":"Cache invalid"}`;info.append(head,meta);const btn=document.createElement("button");btn.className="btn ghost";btn.type="button";btn.textContent=aiSeamText("previewAction");btn.dataset.aisPreview=seam.id;btn.disabled=!seam.compatible;row.append(info,btn);cache.appendChild(row)})}
 const status=AI_SEAM.status;
 const enabled=$("#aisEnableLabel");if(enabled)enabled.textContent=aiSeamText("enabled");
 const batchNote=$("#aisBatchNote");
 if(batchNote){const scan=AI_SEAM.environment||AI_SEAM.status?.environment;batchNote.textContent=computeBlocked?aiSeamText("busyCompute"):(scan?"Adaptive low-memory mode · one pair job at a time · RIFE queues "+(scan.recommended_rife_jobs||"1:1:1")+" · recommended input "+(scan.recommended_max_side||512)+"px":"Run Scan to select a VRAM-aware resolution and safe processing queues.");}
}
async function aiSeamLoad(silent=false){
 if(aiSeamLoading)return;aiSeamLoading=true;
 try{
   const [status,clips,seams]=await Promise.all([api("/api/ai-seam/status"),api("/api/ai-seam/clips"),api("/api/ai-seam/seams")]);
   AI_SEAM.status=status;AI_SEAM.settings=status.settings||AI_SEAM.settings;AI_SEAM.clips=Array.isArray(clips.clips)?clips.clips:[];AI_SEAM.seams=Array.isArray(seams.seams)?seams.seams:[];AI_SEAM.environment=status.environment||AI_SEAM.environment;
   clipFlowSeamCache=AI_SEAM.seams;renderClipFlowSeams();
   if(!AI_SEAM.clips.some(c=>c.id===aiSeamFromId))aiSeamFromId=AI_SEAM.clips[0]?.id||"";
   if(!AI_SEAM.clips.some(c=>c.id===aiSeamToId)||aiSeamToId===aiSeamFromId)aiSeamToId=AI_SEAM.clips.find(c=>c.id!==aiSeamFromId)?.id||"";
   aiSeamApplyLanguage();
   if(status.job?.status==="running")aiSeamWatchJob(status.job.id);
   if(!silent&&AI_SEAM.status.clip_flow_active)notify(aiSeamText("flowWarning"),false);
 }catch(e){if(!silent)notify(e.message,false)}finally{aiSeamLoading=false;aiSeamRender()}
}
async function initAiSeam(){
 if(!aiSeamSetupDone){aiSeamSetupDone=true;aiSeamSetupEvents();}
 await aiSeamLoad(true);
}
function aiSeamSetupEvents(){
 $("#aisAddClips")?.addEventListener("click",async()=>{
   try{
     if(!window.paokra?.selectVideos){notify(aiSeamText("noDesktop"),false);return}
     const paths=await window.paokra.selectVideos({role:"seam"});if(!Array.isArray(paths)||!paths.length)return;
     const result=await post("/api/ai-seam/clips",{paths});
     aiSeamShowLimit=Math.max(100,aiSeamShowLimit);await aiSeamLoad(true);
     if(result.errors?.length)notify(`${aiSeamText("added")}: ${result.added?.length||0}; skipped ${result.skipped?.length||0}; errors ${result.errors.length}`,result.added?.length>0);
     else notify(`${aiSeamText("added")}: ${result.added?.length||0}; skipped ${result.skipped?.length||0}`);
   }catch(e){notify(e.message,false)}
 });
 $("#aisFileFallback")?.addEventListener("change",async e=>{const files=Array.from(e.target.files||[]);e.target.value="";const paths=files.map(f=>f.path).filter(Boolean);if(!paths.length){notify(aiSeamText("noDesktop"),false);return}try{const r=await post("/api/ai-seam/clips",{paths});await aiSeamLoad(true);notify(`${aiSeamText("added")}: ${r.added?.length||0}`)}catch(err){notify(err.message,false)}});
 $("#aisClipSearch")?.addEventListener("input",()=>{aiSeamShowLimit=100;aiSeamRender()});
 $("#aisShowMore")?.addEventListener("click",()=>{aiSeamShowLimit+=100;aiSeamRender()});
 $("#aisClipsList")?.addEventListener("click",async e=>{
   const b=e.target.closest("button[data-ais-act]");if(!b)return;const id=b.dataset.aisId,act=b.dataset.aisAct,idx=AI_SEAM.clips.findIndex(c=>c.id===id);if(idx<0)return;
   try{
     if(act==="from"){aiSeamFromId=id;if(id===aiSeamToId)aiSeamToId=AI_SEAM.clips.find(c=>c.id!==id)?.id||"";AI_SEAM.analysis=null;aiSeamRender();return}
     if(act==="to"){aiSeamToId=id;if(id===aiSeamFromId)aiSeamFromId=AI_SEAM.clips.find(c=>c.id!==id)?.id||"";AI_SEAM.analysis=null;aiSeamRender();return}
     if(act==="remove"){
       if(!confirm(aiSeamText("deleteConfirm")))return;
       await api(`/api/ai-seam/clips/${encodeURIComponent(id)}`,{method:"DELETE"});if(id===aiSeamFromId)aiSeamFromId="";if(id===aiSeamToId)aiSeamToId="";AI_SEAM.analysis=null;await aiSeamLoad(true);notify(aiSeamText("removed"));return;
     }
     if(act==="up"||act==="down"){
       const to=act==="up"?idx-1:idx+1;if(to<0||to>=AI_SEAM.clips.length)return;
       const ids=AI_SEAM.clips.map(c=>c.id);[ids[idx],ids[to]]=[ids[to],ids[idx]];
       const r=await post("/api/ai-seam/clips/reorder",{ids});AI_SEAM.clips=r.clips||AI_SEAM.clips;aiSeamRender();return;
     }
   }catch(err){notify(err.message,false)}
 });
 $("#aisAnalyze")?.addEventListener("click",aiSeamAnalyze);
 $("#aisGenerate")?.addEventListener("click",aiSeamGenerate);
 $("#aisSaveSettings")?.addEventListener("click",async()=>{
   try{const side=$("#aisMaxSide").value;const r=await post("/api/ai-seam/settings",{enabled:$("#aisEnabled").checked,compute:$("#aisCompute").value,resolution_mode:side==="auto"?"auto":"manual",max_side:side==="auto"?(Number(AI_SEAM.environment?.recommended_max_side)||512):Number(side),transition_frames:Number($("#aisFrames").value)});AI_SEAM.settings=r.settings;aiSeamRender();await aiSeamLoad(true);notify(aiSeamText("settingsSaved"));}
   catch(e){notify(e.message,false)}
 });
 $("#aisScan")?.addEventListener("click",aiSeamScan);
 $("#aisApplyPlaylist")?.addEventListener("click",async()=>{
   if(!AI_SEAM.clips.length)return;
   if(AI_SEAM.status?.clip_flow_active){notify(aiSeamText("flowWarning"),false);return}
   if(!confirm(aiSeamText("applyConfirm")))return;
   try{const r=await post("/api/clip-flow/config",{normal:AI_SEAM.clips.map(c=>c.path)});if(!r.ok)throw new Error(r.error||"Normal Playlist could not be updated");notify(`Normal Playlist updated: ${AI_SEAM.clips.length} clips`);}
   catch(e){notify(e.message,false)}
 });
 $("#aisClearCache")?.addEventListener("click",async()=>{if(!confirm(aiSeamText("cacheConfirm")))return;try{const v=$("#aisPreviewVideo");if(v){v.pause();v.removeAttribute("src");v.load()}const p=$("#aisPreviewPanel");if(p)p.hidden=true;const r=await api("/api/ai-seam/cache",{method:"DELETE"});if(!r.ok)throw new Error(r.error||"Some generated cache files remain in use");AI_SEAM.seams=[];await aiSeamLoad(true);notify(aiSeamText("good"))}catch(e){notify(e.message,false)}});
 $("#aisCacheList")?.addEventListener("click",e=>{const b=e.target.closest("button[data-ais-preview]");if(!b)return;const video=$("#aisPreviewVideo"),panel=$("#aisPreviewPanel");if(!video||!panel)return;video.src=`/api/ai-seam/seams/${encodeURIComponent(b.dataset.aisPreview)}/preview`;panel.hidden=false;video.load();panel.scrollIntoView({behavior:"smooth",block:"nearest"})});
}
async function aiSeamAnalyze(){
 if(!aiSeamFromId||!aiSeamToId||aiSeamFromId===aiSeamToId){notify(aiSeamText("choosePair"),false);return null}
 const b=$("#aisAnalyze");if(b)b.disabled=true;
 try{const a=await post("/api/ai-seam/analyze",{from_id:aiSeamFromId,to_id:aiSeamToId});AI_SEAM.analysis=a;aiSeamApplyLanguage();notify(aiSeamText("analyzed"));return a}
 catch(e){AI_SEAM.analysis=null;aiSeamRender();notify(e.message,false);return null}
 finally{if(b)b.disabled=false;aiSeamRender()}
}
async function aiSeamGenerate(){
 if(clipFlowAutoMaking){notify("Auto Make Frames กำลังใช้ AI Seam อยู่ รอให้ชุดนี้จบก่อนเริ่มงานแยก",false);return}
 if(!aiSeamFromId||!aiSeamToId||aiSeamFromId===aiSeamToId){notify(aiSeamText("choosePair"),false);return}
 if(AI_SEAM.status?.clip_flow_active){notify(aiSeamText("flowWarning"),false);return}
 const b=$("#aisGenerate");if(b)b.disabled=true;
 try{
   let a=AI_SEAM.analysis;if(!a||a.from_id!==aiSeamFromId||a.to_id!==aiSeamToId)a=await aiSeamAnalyze();
   if(!a)return;
   const threshold=Number(AI_SEAM.settings?.similarity_threshold??0.4);
   if(Number(a.score)<threshold&&!$("#aisForce")?.checked){notify(`${aiSeamText("riskRisky")}: ${Math.round(Number(a.score)*100)}%. ${aiSeamText("force")}`,false);return}
   const r=await post("/api/ai-seam/generate",{from_id:aiSeamFromId,to_id:aiSeamToId,force:!!$("#aisForce")?.checked,compute:$("#aisCompute")?.value||"auto"});
   if(!r.job?.id)throw new Error("AI Seam job was not created");aiSeamWatchJob(r.job.id);notify(aiSeamText("generationStarted"));
 }catch(e){notify(e.message,false)}finally{if(b)b.disabled=false;aiSeamRender()}
}
async function aiSeamScan(){
 const btn=$("#aisScan");if(btn)btn.disabled=true;
 const env=$("#aisEnvironment");if(env)env.innerHTML=`<div class="unavail">${esc(aiSeamText("environmentNote"))} · running probe…</div>`;
 try{const r=await post("/api/ai-seam/environment/scan",{compute:$("#aisCompute")?.value||"auto"});AI_SEAM.environment=r;aiSeamRender();if(r.ok){if(r.recommended_compute&&$("#aisCompute")&&$("#aisCompute").value==="auto"){}notify(aiSeamText("scanOk"))}else notify(`${aiSeamText("scanBad")}: ${r.probe?.message||r.note||"check bundle/driver"}`,false)}
 catch(e){notify(e.message,false)}finally{if(btn)btn.disabled=false}
}
function aiSeamWatchJob(jobId){
 if(aiSeamJobTimer&&aiSeamActiveJob===jobId)return;
 if(aiSeamJobTimer)clearInterval(aiSeamJobTimer);aiSeamActiveJob=jobId;
 const panel=$("#aisJobPanel");if(panel)panel.hidden=false;
 const tick=async()=>{
   try{
     const r=await api(`/api/ai-seam/jobs/${encodeURIComponent(jobId)}`),j=r.job||{};
     if($("#aisJobPercent"))$("#aisJobPercent").textContent=`${clamp(Number(j.progress)||0,0,100)}%`;
     if($("#aisJobBar"))$("#aisJobBar").style.width=`${clamp(Number(j.progress)||0,0,100)}%`;
     if($("#aisJobMessage"))$("#aisJobMessage").textContent=j.error||j.message||j.phase||"";
     if(j.status==="complete"||j.status==="error"){
       clearInterval(aiSeamJobTimer);aiSeamJobTimer=null;aiSeamActiveJob="";
       await aiSeamLoad(true);
       if(j.status==="complete"&&j.result?.id){const video=$("#aisPreviewVideo"),panel=$("#aisPreviewPanel");if(video&&panel){video.src=`/api/ai-seam/seams/${encodeURIComponent(j.result.id)}/preview`;panel.hidden=false;video.load()}notify(`${aiSeamText("generationDone")}: ${j.result.from_name} → ${j.result.to_name}`)}
       else notify(`${aiSeamText("generationFailed")}: ${j.error||"unknown error"}`,false);
     }
   }catch(e){if($("#aisJobMessage"))$("#aisJobMessage").textContent=e.message;}
 };
 tick();aiSeamJobTimer=setInterval(tick,900);
}

/* ---------- views ---------- */
function go(){
  const requested=location.hash.slice(1);
  if(requested==="ai"&&!UI_CFG.aiMode){location.hash="#dashboard";return}
  const v=TITLES[requested]?requested:"dashboard";view=v;
  $$(".view").forEach(s=>s.hidden=s.id!=="v-"+view);
  $$(".side a").forEach(a=>a.classList.toggle("on",a.dataset.view===view));
  $("#title").textContent=TITLES[view];
  streams();
  if(view==="media")loadMedia();
  if(view==="ai-seam")initAiSeam();
  if(view==="accounts")loadAuth();
  if(view==="manager")loadManager();
  if(view==="tiktok-dashboard")loadManager();
  if(view==="video-to-filter")v2fInit();
  if(view==="comments")loadComments(true);
  if(view==="logs")renderLogs();
  render();layout();
}
function streams(force){
  const windowActive=!document.hidden&&document.hasFocus();
  $$("img[data-stream]").forEach(img=>{
    const host=img.closest(".view");const vis=!host||!host.hidden;
    if(windowActive&&vis){if(force||!img.getAttribute("src"))img.src=img.dataset.stream+"?t="+Date.now()}
    else img.removeAttribute("src");
  });
}
let streamWindowActive=!document.hidden&&document.hasFocus();
function syncWindowStreams(){
  const active=!document.hidden&&document.hasFocus();
  if(!active){
    // Close the MJPEG connection while another app is in front. The server can
    // then stop JPEG work instead of buffering stale preview frames. Camera
    // output is a separate worker and continues uninterrupted.
    $$("img[data-stream]").forEach(img=>img.removeAttribute("src"));
    streamWindowActive=false;
    return;
  }
  if(!streamWindowActive){
    streamWindowActive=true;
    streams(true);  // reconnect on the newest frame; never replay a backlog
    poll();
  }
}
window.addEventListener("blur",syncWindowStreams);
window.addEventListener("focus",syncWindowStreams);
document.addEventListener("visibilitychange",syncWindowStreams);
window.addEventListener("hashchange",go);

/* ---------- polling ---------- */
async function poll(){
  try{
    S=await api("/api/state");
    stateSnapshotAt=performance.now();
    if(online===false){log("INFO","Backend reconnected");streams(true)}
    online=true;
    window.__pkPollError="";
  }catch(e){
    const detail=String(e&&e.message||e||"unknown fetch error").slice(0,220);
    window.__pkPollError=detail;
    if(online!==false)log("ERROR","Backend ไม่ตอบสนอง กำลังเชื่อมต่อใหม่... ("+detail+")");
    online=false;
  }
  render();
}

/* ---------- render ---------- */
function pill(id,cls,text){const el=$(id);el.className="pill "+cls;el.lastChild.textContent=text}
function render(){
  if(online===false||!S){
    document.documentElement.dataset.performance="normal";
    pill("#pBackend","bad","Backend offline");pill("#pEngine","","Engine");pill("#pCam","","Virtual Camera");pill("#pLibObs","bad","LibOBS offline");return;
  }
  const isRun=S.running,cam=!!S.virtualcam;
  const sceneToggle=$("#sceneClipPlay");
  if(sceneToggle){sceneToggle.disabled=!!engineActionBusy;sceneToggle.textContent=engineActionBusy?"Working…":(isRun?"■ Stop Engine":"▶ Start Engine");sceneToggle.className="btn scene-engine-toggle "+(isRun?"danger":"primary");sceneToggle.setAttribute("aria-pressed",isRun?"true":"false");sceneToggle.title=isRun?"Stop Engine and Virtual Audio":"Start Engine with current Console settings";}
  // Drop decorative GPU effects while frame delivery is active, then restore
  // the user's normal theme when Engine and Virtual Camera are both stopped.
  document.documentElement.dataset.performance=(isRun||cam)?"live":"normal";
  pill("#pBackend","ok","Backend");
  pill("#pEngine",isRun?"ok":"warn",isRun?"Engine · Rendering":"Engine · Stopped");
  const camMeasuredFps=Number(S.virtualcam_measured_fps||0),camTargetFps=Number(S.virtualcam_output_fps||S.effective_fps||30);pill("#pCam",cam?(camMeasuredFps>0&&camMeasuredFps+3<camTargetFps?"warn":"ok"):(S.virtualcam_error?"bad":""),cam?("Camera · "+Math.round(camMeasuredFps>=1?camMeasuredFps:camTargetFps)+" FPS sent"):"Virtual Camera · Stopped");
  const vcamTag=$("#vcamFpsTag"),vcamDetail=$("#vcamFpsDetail");
  if(vcamTag){vcamTag.textContent=cam?(Math.round(camTargetFps)+" FPS target"):"Stopped";vcamTag.className="tag"+(cam&&camMeasuredFps>0&&camMeasuredFps+3<camTargetFps?" warn":"");}
  if(vcamDetail){
    const language=UI_CFG.language==="th",ow=Number(S.virtualcam_output_width||0),oh=Number(S.virtualcam_output_height||0);
    if(!cam)vcamDetail.textContent=language?"เปิด Virtual Camera เพื่อวัดอัตราการส่งเฟรมจริง":"Start Virtual Camera to measure the actual sender rate.";
    else if(camMeasuredFps>0&&camMeasuredFps+3<camTargetFps)vcamDetail.textContent=language?("ส่ง "+camMeasuredFps.toFixed(1)+" FPS จากเป้าหมาย "+camTargetFps+" FPS ที่ "+ow+"×"+oh+" · แอปปลายทางอาจเลือกโหมดรับต่ำกว่านี้"):("Sender measured "+camMeasuredFps.toFixed(1)+" FPS vs "+camTargetFps+" FPS target at "+ow+"×"+oh+". The receiving app may negotiate a lower capture rate.");
    else vcamDetail.textContent=language?("ขนาด "+ow+"×"+oh+" · เป้าหมาย "+camTargetFps+" FPS · ส่งจริง "+camMeasuredFps.toFixed(1)+" FPS (ค่าฝั่งส่ง ไม่ใช่ค่าที่เว็บปลายทางรายงาน)"):("Output "+ow+"×"+oh+" · target "+camTargetFps+" FPS · measured sender "+camMeasuredFps.toFixed(1)+" FPS (sender-side only, not the receiver's reported rate).");
  }
  const res=`${S.width}×${S.height} · ${S.effective_fps||S.fps} FPS`;
  const libobs=!!S.libobs_active;
  const libobsFallback=!!cam&&!!S.libobs_error;
  pill("#pLibObs",libobs?"ok":(libobsFallback?"warn":""),libobs?`LibOBS · ${S.libobs_version||"Active"}`:(libobsFallback?"LibOBS · Direct fallback":"LibOBS · Standby"));
  // Live Console timeline is display-only; seeking belongs to Scene Builder.
  const timelineBox=$("#consoleClipTimeline");
  const duration=Math.max(0,Number(S.source_duration_seconds)||0);
  let position=Math.max(0,Number(S.source_position_seconds)||0);
  if(isRun&&duration>0){
    position+=Math.max(0,performance.now()-stateSnapshotAt)/1000;
    if(S.loop)position%=duration;else position=Math.min(duration,position);
  }else position=0;
  if(timelineBox){
    timelineBox.hidden=!(S.source&&isRun&&duration>0);
    const clipName=$("#consoleClipName"),clipTime=$("#consoleClipTime"),clipBar=$("#consoleClipBar"),clipMeta=$("#consoleClipMeta");
    if(clipName)clipName.textContent=base(S.source)||"Current clip";
    if(clipTime)clipTime.textContent=formatDuration(position)+" / "+formatDuration(duration);
    if(clipBar)clipBar.style.width=(duration>0?clamp(position/duration*100,0,100):0)+"%";
    if(clipMeta)clipMeta.textContent=(S.loop?"Looping clip":"One-shot clip")+" · Source "+(Number(S.source_fps)>0?Number(S.source_fps).toFixed(1):"—")+" FPS · Target "+(S.effective_fps||S.fps)+" FPS";
  }
  // Report measured counters, not the configured target FPS. This makes preview
  // starvation and camera-send stalls visible instead of claiming a nominal 30.
  const perfEl=$("#consolePerf"),nowPerf=performance.now();
  const counters={render:Number(S.render_frame_count)||0,preview:Number(S.preview_frame_count)||0,camera:Number(S.vcam_frame_count)||0};
  let rates={render:0,preview:0,camera:0};
  if(perfSnapshot){const dt=(nowPerf-perfSnapshot.t)/1000;if(dt>0.2){for(const k of Object.keys(rates))rates[k]=Math.max(0,(counters[k]-perfSnapshot[k])/dt)}}
  perfSnapshot={...counters,t:nowPerf};
  if(perfEl)perfEl.textContent=`Render ${rates.render.toFixed(0)} · Console ${rates.preview.toFixed(0)} · Camera ${rates.camera.toFixed(0)} FPS`;
  // Live Console Program frame must always have the exact selected output ratio.
  const progBox=$(".progbox");
  if(progBox){
    progBox.style.setProperty("--out-ratio", `${S.width}/${S.height}`);
    layoutConsolePreview();
  }
  $("#dStatus").textContent=isRun?"Rendering":"Stopped";
  $("#dSource").textContent=S.source?base(S.source):"No source";
  $("#dRes").textContent=res;$("#dCam").textContent=cam?"Running":"Stopped";
  $("#dAcc").textContent=S.accounts.length;
  $("#sysList").innerHTML=[
    ["Backend","ok","Connected"],
    ["Renderer",isRun?"ok":"warn",isRun?"Rendering":"Idle"],
    ["Virtual Camera",cam?"ok":(S.virtualcam_error?"bad":""),cam?"Running":"Stopped"],
    ["Virtual Audio",S.virtualaudio?"ok":(S.virtualaudio_error?"bad":"warn"),S.virtualaudio
      ? ("Running · "+(S.virtualaudio_device||"Virtual output"))
      : (S.virtualaudio_error ? "Error · "+S.virtualaudio_error : (!S.source||!S.source_enabled ? "Ready · no source" : (!S.running ? "Ready · starts with Engine" : "Starting…")))],
    ["Virtual Mic",S.virtualaudio_device&&/CABLE Input|CABLE Output/i.test(S.virtualaudio_device) ? "ok" : "warn",
      S.virtualaudio_device&&/CABLE Input|CABLE Output/i.test(S.virtualaudio_device) ? "CABLE Output · Windows Mic" : "Audio output sink only"],
    ["Platform output","warn","Not available"]
  ].map(([n,c,t])=>`<li><span>${n}</span><span><i class="dot ${c}"></i>${t}</span></li>`).join("");
  const tag=$("#cTag");tag.textContent=isRun?"RENDERING":"STOPPED";tag.className="tag"+(isRun?" live":"");
  const clipFlowInStop=!!(clipFlowRuntime&&clipFlowRuntime.active&&(clipFlowRuntime.phase==="stop"||clipFlowRuntime.stop_requested));
  // A long Stop clip (e.g. a 30-minute closing video) must not lock the user out
  // of starting Open again. Start becomes an explicit "Restart Flow" override.
  const startBtn=$("#bStart");
  if(startBtn){
    startBtn.disabled=engineActionBusy||(isRun&&!clipFlowInStop);
    startBtn.textContent=engineActionBusy?"Working…":(clipFlowInStop?"Restart Flow":"Start");startBtn.setAttribute("aria-busy",engineActionBusy?"true":"false");
    startBtn.title=clipFlowInStop?"หยุดคลิป Stop ปัจจุบันและเริ่ม Open ใหม่":"เริ่ม Engine / Clip Flow";
  }
  $("#bStop").disabled=engineActionBusy||!isRun;
  $("#vcStatus").textContent=cam?(S.virtualcam_error?"Running · Frame error":"Running"):"Stopped";
  $("#vcRes").textContent=cam?res:"-";
  const va=$("#vaStatus");
  if(va)va.textContent=S.virtualaudio
    ? ("Running · "+(S.virtualaudio_device||"Virtual output"))
    : (S.virtualaudio_error ? "Error · "+S.virtualaudio_error : (!S.source||!S.source_enabled ? "Ready · no source" : "Ready · starts with Engine"));
  const vm=$("#vaMicStatus");
  if(vm)vm.textContent=S.virtualaudio_device&&/CABLE Input|CABLE Output/i.test(S.virtualaudio_device)
    ? "Ready · CABLE Output (Windows Mic)"
    : "Needs VB-CABLE pair";
  const ve=$("#vcErr");ve.hidden=!S.virtualcam_error;
  if(!ve.hidden)ve.textContent="Virtual Camera error: "+S.virtualcam_error;
  $("#bCamStart").disabled=cameraActionBusy||cam;$("#bCamStop").disabled=cameraActionBusy||!cam;
  const webTag=$("#webSourceTag");
  if(webTag){
    webTag.textContent=S.web_url&&S.web_enabled&&(S.layer_order||[]).includes("web")?"ON":"OFF";
    webTag.className="tag"+(S.web_url&&S.web_enabled&&(S.layer_order||[]).includes("web")?" live":"");
  }
  const wf=id=>{const el=$(id);if(el&&document.activeElement!==el)return};
  wf("#webUrl");wf("#webX");wf("#webY");wf("#webW");wf("#webH");
  if(!drag){
    const setWeb=(id,v)=>{const el=$(id);if(el)el.value=v};
    setWeb("#webUrl",S.web_url||"");
    setWeb("#webX",S.web_x??0);setWeb("#webY",S.web_y??0);
    setWeb("#webW",S.web_width??S.width);setWeb("#webH",S.web_height??S.height);
  }
  const sig=JSON.stringify(S.accounts);
  if(sig!==accSig){accSig=sig;renderAccounts()}
  renderLayers($("#layerList"));renderLayers($("#consoleLayers"));renderSceneInspectorPanels();
  const rs=$("#setRes"),fp=$("#setFps"),lp=$("#setLoop");
  if(document.activeElement!==rs)rs.value=`${S.width}x${S.height}`;
  if(document.activeElement!==fp)fp.value=S.fps_mode==="source"?"source":String(S.fps);
  lp.checked=!!S.loop;
  // Output can be changed while Virtual Camera is running; the handler below
  // performs a clean camera restart so the new frame size is applied.
  rs.disabled=fp.disabled=false;
  const sf=Number(S.source_fps||0);
  $("#setNote").textContent=S.source
    ? `Source clock: ${sf.toFixed(2)} FPS · Output: ${Number(S.effective_fps||S.fps)} FPS${S.fps_mode==="source"?" (Auto; pinned while camera is active)":""} · ไม่เร่งคลิปตาม output FPS`
    : "เพิ่มวิดีโอเพื่อเริ่ม Source clock";

  const hasImg=!!S.image;
  $("#imgInsp").hidden=!hasImg;$("#noImg").hidden=hasImg;
  if(!drag){
    const item=selectedText();
    if(item)syncTextInspector(item);
    else{
      const f=(id,v)=>{const el=$(id);if(document.activeElement!==el)el.value=v};
      f("#inText",S.legacy_text_added?(S.text||""):textDraft);f("#inTX",S.text_x);f("#inTY",S.text_y);
      f("#inTSize",S.text_size||TEXT_FS);
    }
    const f=(id,v)=>{const el=$(id);if(document.activeElement!==el)el.value=v};
    f("#inIX",S.image_x);f("#inIY",S.image_y);f("#inIW",S.image_width);f("#inIH",S.image_height);f("#inIFit",S.image_fit||"contain");f("#inIZoom",Math.round((S.image_zoom||1)*100));f("#audioOffset",S.audio_offset_ms||0);
    layout();
  }
  renderFX();
  const lv=$("#setLoopVariation"),ls=$("#setVariationStrength"),lvv=$("#variationStrengthValue");
  if(lv)lv.checked=!!S.loop_variation_enabled;
  if(ls)ls.value=String(S.loop_variation_strength??4);
  if(lvv)lvv.textContent=String(S.loop_variation_strength??4);
  $("#stageInfo").textContent=res;
}

function layerDefs(){
  const defs={
    source:{id:"source",k:"source_enabled",n:"Video source",sub:S.source?base(S.source):"No source"},
    web:{id:"web",k:"web_enabled",n:"Web source",sub:S.web_url||"(none)"},
    image:{id:"image",k:"image_enabled",n:"Image overlay",sub:S.image?base(S.image):"(none)"},
    text:{id:"text",k:"text_enabled",n:"Text",sub:(S.text||"(empty)")+((S.texts||[]).length?" + "+S.texts.length+" more":"")}
  };
  return (Array.isArray(S.layer_order)?S.layer_order:[]).filter(id=>defs[id]).map(id=>defs[id]);
}
function renderSceneInspectorPanels(){
  const textPanel=$("#textInspector"),webPanel=$("#webInspector");
  if(textPanel)textPanel.hidden=sceneInspectorRequested!=="text";
  if(webPanel)webPanel.hidden=sceneInspectorRequested!=="web";
}
function renderLayers(ul){
  if(!S||!ul)return;
  const defs=layerDefs();
  ul.innerHTML=defs.map((l,i)=>`<li class="${S[l.k]?"":"off"}">
    <span><div>${l.n}</div><div class="muted small">${esc(l.sub)}</div></span>
    <span class="layer-actions">
      <button type="button" data-k="${l.k}" title="แสดง/ซ่อน"><svg class="i"><use href="#i-${S[l.k]?"eye":"eyeoff"}"/></svg></button>
      <button type="button" data-move-id="${l.id}" data-dir="-1" ${i===0?"disabled":""} title="เลื่อนขึ้น">↑</button>
      <button type="button" data-move-id="${l.id}" data-dir="1" ${i===defs.length-1?"disabled":""} title="เลื่อนลง">↓</button>
      <button type="button" data-remove-layer="${l.id}" title="เอา Layer ออก">×</button>
    </span>
  </li>`).join("");
  const hint=$("#layerHint");
  if(hint)hint.hidden=defs.length>0;
}
async function addSceneLayerFromMenu(id){
  if(!S)return;
  // Polling can leave S one cycle behind a backend restart/source transition.
  // Use the authoritative state before deciding whether Video/Image/Web needs
  // a picker; otherwise an empty Video layer can be inserted instead of opening
  // the file dialog, which looks like the Scene Builder ignored the click.
  S=await api("/api/state");
  stateSnapshotAt=performance.now();
  sceneInspectorRequested=(id==="text"||id==="web")?id:"";
  renderSceneInspectorPanels();
  if(id==="source"){
    // Video is a singleton source: always open the picker so this action can
    // create the first source OR replace the current clip. Merely re-adding the
    // existing layer made "+ Video" appear broken when users tried to switch clips.
    if(window.paokra?.selectVideo){
      const path=await window.paokra.selectVideo();
      if(path){$("#srcLocalPath").value=path;await useLocalSource(path)}
      return;
    }
    const input=$("#srcFile");if(input){input.value="";input.click();return}
    throw new Error("ไม่พบตัวเลือกไฟล์วิดีโอ");
  }
  if(id==="image"&&!S.image){
    const input=$("#sceneImageFile");if(input){input.value="";input.click();return}
    throw new Error("ไม่พบตัวเลือกไฟล์ภาพ");
  }
  if(id==="web"&&!String(S.web_url||"").trim()){
    const field=$("#webUrl");
    if(field){field.focus();field.scrollIntoView({behavior:"smooth",block:"center"})}
    notify("ใส่ Web URL ใน Inspector แล้วกด Add / Apply ก่อนเพิ่ม Web Layer",false);
    return;
  }
  if(id==="text"){
    // Each explicit Layers + Text action creates a text layer, including when
    // other text layers already exist. Inspector visibility is driven by this action.
    await addTextLayer();
    return;
  }
  const order=[...(Array.isArray(S.layer_order)?S.layer_order:[])];
  if(!order.includes(id)){
    order.push(id);
    await commitLayerOrder(order);
    await poll();
    notify("เพิ่ม "+id+" Layer แล้ว");
  }
}
document.addEventListener("click",e=>{
  if(e.target.closest("#addLayerBtn")){
    const menu=$("#layerAddMenu");if(menu)menu.hidden=!menu.hidden;return;
  }
  const choice=e.target.closest("#layerAddMenu [data-add-layer]");
  if(choice&&S){
    const id=choice.dataset.addLayer;
    const menu=$("#layerAddMenu");if(menu)menu.hidden=true;
    run(()=>addSceneLayerFromMenu(id));
    return;
  }
  const rm=e.target.closest(".layers button[data-remove-layer]");
  if(rm&&S){
    const id=rm.dataset.removeLayer;
    if(sceneInspectorRequested===id)sceneInspectorRequested="";
    const order=[...(Array.isArray(S.layer_order)?S.layer_order:[])].filter(x=>x!==id);
    run(async()=>{
      try{
        if(id==="source"){
          // Stop first; only update the local UI after the server confirms it.
          const pv=$("#stageBgVideo");
          if(pv){pv.pause();try{pv.currentTime=0}catch{}}
          await post("/api/control",{action:"stop"});
        }
        await commitLayerOrder(order);
      }catch(err){
        // Restore the server's authoritative Source/layer state if Stop or
        // reorder failed instead of showing a false successful removal.
        try{await poll()}catch{}
        render();
        throw err;
      }
      if(id==="source")S.source=null;
      await poll();
      render();
      notify(id==="source"?"ลบ Video Source + หยุดเสียงแล้ว":"เอา Layer ออกแล้ว");
    });
    return;
  }
  const b=e.target.closest(".layers button[data-k]");if(b&&S){run(()=>setCfg({[b.dataset.k]:!S[b.dataset.k]}));return}
  const m=e.target.closest(".layers button[data-move-id]");if(m&&S){
    const order=[...(Array.isArray(S.layer_order)?S.layer_order:[])];
    const i=order.indexOf(m.dataset.moveId),j=i+Number(m.dataset.dir);
    if(i>=0&&j>=0&&j<order.length){
      [order[i],order[j]]=[order[j],order[i]];
      run(async()=>{
        await commitLayerOrder(order);
        await poll();
        notify("จัดลำดับ Layer แล้ว");
      });
    }
  }
});
function renderActivity(){
  const ul=$("#actList");if(!ul)return;
  ul.innerHTML=LOG.length?LOG.slice(0,6).map(l=>`<li><time>${hhmmss(l.t)}</time><span>${esc(l.m)}</span></li>`).join(""):`<li class="muted">ยังไม่มีกิจกรรม</li>`;
}
function renderLogs(){
  $("#logList").innerHTML=LOG.length?LOG.map(l=>`<div><span>${hhmmss(l.t)}</span><span class="lv ${l.level}">${l.level}</span><span>${esc(l.m)}</span></div>`).join(""):`<div class="empty"><b>No entries</b></div>`;
}
const STATUS={connected:["ok","Connected"],reauth_required:["warn","ต้องเชื่อมต่อใหม่"]};
const CAPN=[["canReadProfile","Profile"],["canPublish","Publish"],["canStartLive","Start live"],["canReadLiveState","Live state"]];
const ago=t=>{const s=Math.max(0,Date.now()/1000-t);return s<90?"เมื่อสักครู่":s<5400?Math.round(s/60)+" นาทีที่แล้ว":s<172800?Math.round(s/3600)+" ชม.":Math.round(s/86400)+" วัน"};
function renderAccounts(){
  const L=$("#accList");
  L.innerHTML=S.accounts.length?S.accounts.map(a=>{
    const [c,t]=STATUS[a.status]||["","Local"],cp=a.capabilities||{};
    const av=a.avatar?`<img class="av" referrerpolicy="no-referrer" src="${esc(a.avatar)}" alt="">`:`<div class="av">${esc((a.displayName||"?")[0].toUpperCase())}</div>`;
    return `<div class="accrow">${av}<div class="grow"><div><b>${esc(a.displayName||a.name||"-")}</b> <span class="pill ${c}"><i></i>${t}</span></div>
    <div class="muted small">${esc(a.platform||"local")}${a.lastSync?" · "+ago(a.lastSync):""}${a.metadata?.error?" · "+esc(a.metadata.error):""}</div>
    <div class="chips">${CAPN.map(([k,n])=>`<span class="${cp[k]?"on":""}">${n}</span>`).join("")}</div></div>
    <div class="row" style="margin:0"><button class="btn" data-test="${esc(a.id)}">Test</button><button class="btn danger" data-rm="${esc(a.id)}">Remove</button></div></div>`}).join("")
    :`<div class="empty"><b>ยังไม่มีบัญชี</b>กด Connect เพื่อเพิ่ม</div>`;
}
let AUTH={configured:false};
let MANAGER={profiles:[]};
let UI_CFG=JSON.parse(localStorage.getItem("paokra.ui")||"{}" );
const UI_DEFAULT={theme:"light",accent:"blue",gradient:"soft",fontScale:100,density:"comfortable",radius:12,animations:true,sidebar:"normal",language:"th",previewQuality:"balanced",exportEncoder:"auto",cpuThreads:8,filterPreviewFps:4,decodeBackend:"auto",renderBackend:"auto",smoothOriginal:true,aiMode:false,hiddenMenus:[]};
UI_CFG={...UI_DEFAULT,...UI_CFG};UI_CFG.hiddenMenus=Array.isArray(UI_CFG.hiddenMenus)?UI_CFG.hiddenMenus.filter(v=>typeof v==="string"):[];
UI_CFG={...UI_DEFAULT,...UI_CFG};
function applyUI(){
  const root=document.documentElement;
  const accents={blue:["#4f7fff","#3d6be8"],purple:["#7c5cff","#6240d9"],green:["#16a34a","#15803d"],orange:["#f97316","#ea580c"],pink:["#ec4899","#db2777"]};
  const a=accents[UI_CFG.accent]||accents.blue;
  root.style.setProperty("--acc",a[0]);root.style.setProperty("--acc2",a[1]);
  root.style.setProperty("--ui-scale",(Number(UI_CFG.fontScale)||100)/100);
  root.style.setProperty("--radius",clamp(Number(UI_CFG.radius)||12,6,24)+"px");
  root.style.setProperty("--radius-s",Math.max(6,clamp(Number(UI_CFG.radius)||12,6,24)-4)+"px");
  root.dataset.theme=UI_CFG.theme;root.dataset.gradient=UI_CFG.gradient;root.dataset.density=UI_CFG.density;
  root.dataset.animations=UI_CFG.animations?"on":"off";root.dataset.sidebar=UI_CFG.sidebar;
  root.dataset.quality=UI_CFG.previewQuality||"balanced";
  document.body.style.fontSize=((Number(UI_CFG.fontScale)||100)/100)+"em";
}
function syncAIView(){
  const nav=$("#aiNav");
  const enabled=!!UI_CFG.aiMode;
  if(nav)nav.hidden=!enabled;
  if(!enabled&&view==="ai"){
    location.hash="#dashboard";
  }
}
function saveUI(){localStorage.setItem("paokra.ui",JSON.stringify(UI_CFG));applyUI();if(typeof applyMenuVisibility==="function")applyMenuVisibility();renderUIControls();syncAIView();if(view==="scenes")layout()}
function renderUIControls(){
  const map={"#uiTheme":"theme","#uiAccent":"accent","#uiGradient":"gradient","#uiFont":"fontScale","#uiDensity":"density","#uiRadius":"radius","#uiAnim":"animations","#uiSidebar":"sidebar","#uiLang":"language","#uiQuality":"previewQuality","#v2fEncoder":"exportEncoder","#hwCpuThreads":"cpuThreads","#hwPreviewFps":"filterPreviewFps","#hwEncoder":"exportEncoder","#hwDecode":"decodeBackend","#hwBackend":"renderBackend","#hwSmoothOriginal":"smoothOriginal","#aiModeEnable":"aiMode"};
  for(const [id,k] of Object.entries(map)){const el=$(id);if(!el)continue;if(el.type==="checkbox")el.checked=!!UI_CFG[k];else el.value=String(UI_CFG[k]);}
  const fv=$("#uiFontValue");if(fv)fv.textContent=UI_CFG.fontScale+"%";
  const rv=$("#uiRadiusValue");if(rv)rv.textContent=UI_CFG.radius+"px";
  const ai=$("#aiSettingStatus");if(ai){ai.textContent=UI_CFG.aiMode?"ON":"OFF";ai.className="tag"+(UI_CFG.aiMode?" live":"")}
}
function setupUISettings(){
  const binds=["#uiTheme","#uiAccent","#uiGradient","#uiFont","#uiDensity","#uiRadius","#uiSidebar","#uiLang","#uiQuality","#v2fEncoder"];
  binds.forEach(id=>$(id)?.addEventListener("change",e=>{const key={"#uiTheme":"theme","#uiAccent":"accent","#uiGradient":"gradient","#uiFont":"fontScale","#uiDensity":"density","#uiSidebar":"sidebar","#uiLang":"language","#uiQuality":"previewQuality","#v2fEncoder":"exportEncoder"}[id];UI_CFG[key]=e.target.value;saveUI();if(key==="language")applyLanguage()}));
  $("#uiAnim")?.addEventListener("change",e=>{UI_CFG.animations=e.target.checked;saveUI()});
  $("#uiRadius")?.addEventListener("input",e=>{UI_CFG.radius=Number(e.target.value);applyUI();renderUIControls()});
  $("#uiFont")?.addEventListener("input",e=>{UI_CFG.fontScale=Number(e.target.value);applyUI();renderUIControls()});
  $("#uiReset")?.addEventListener("click",()=>{UI_CFG={...UI_DEFAULT};saveUI();applyLanguage();notify("ตั้งค่า UI กลับเป็นค่าเริ่มต้นแล้ว")});
  renderUIControls();applyUI();applyLanguage();syncAIView();
  $("#aiLaunch")?.addEventListener("click",launchAIStudio);
  $("#aiLaunchTop")?.addEventListener("click",launchAIStudio);
  setupHardwareSettings();
}
function setupHardwareSettings(){
  const renderHardware=async()=>{
    const tag=$("#hwProfileTag"),info=$("#hwProfileInfo");
    if(tag)tag.textContent="CHECKING…";
    try{
      let h;
      try{h=await api("/api/system/hardware");}
      catch(primary){if(!/HTTP 404/.test(primary.message||""))throw primary;h=await api("/api/hardware");}
      if(!h.ok)throw new Error(h.error||"hardware check failed");
      if(tag)tag.textContent=h.profile_name||"DETECTED";
      const gpu=(h.gpu_names||[]).join(", ")||"ไม่พบ GPU";
      const enc=Object.entries(h.encoders||{}).filter(([,v])=>v).map(([k])=>k.replace("_"," ")).join(", ")||"none";
      const hw=(h.ffmpeg_hwaccels||[]).join(", ")||"software";
      const compute=[h.vulkan_supported?"Vulkan":"",h.rocm_supported?"ROCm":""].filter(Boolean).join(" + ")||"CPU";
      if(info)info.textContent=`CPU ${h.cpu_physical||h.cpu_count||"?"} cores / ${h.cpu_count||"?"} threads · RAM ${h.ram_gb||"?"} GB · GPU ${gpu} · H.264: ${enc} · Decode: ${hw} · Compute: ${compute}`;
      const max=Math.max(1,Number(h.cpu_count)||1);
      const currentThreads=Math.max(1,Math.min(max,Number(h.cpu_threads_current)||0));
      const cpu=$("#hwCpuThreads");if(cpu){cpu.max=String(max);if(currentThreads)UI_CFG.cpuThreads=currentThreads;else if(!UI_CFG.cpuThreads||Number(UI_CFG.cpuThreads)>max)UI_CFG.cpuThreads=Number(h.recommended_cpu_threads||max);}
      const cpuVal=$("#hwCpuThreadsValue");if(cpuVal)cpuVal.textContent=String(UI_CFG.cpuThreads);
      const pf=$("#hwPreviewFps");if(pf)pf.value=String(UI_CFG.filterPreviewFps||4);
      const pfv=$("#hwPreviewFpsValue");if(pfv)pfv.textContent=String(UI_CFG.filterPreviewFps||4);
      for(const [id,key] of [["#hwEncoder","exportEncoder"],["#hwDecode","decodeBackend"],["#hwBackend","renderBackend"]]){const el=$(id);if(el)el.value=String(UI_CFG[key]||"auto");}
      const smooth=$("#hwSmoothOriginal");if(smooth)smooth.checked=UI_CFG.smoothOriginal!==false;
      // The current compositor is OpenCV/NumPy CPU. Vulkan/ROCm runtime
      // detection alone does not mean a GPU render backend is implemented.
      const vul=$("#hwBackend option[value=\"vulkan\"]"),roc=$("#hwBackend option[value=\"rocm\"]");
      if(vul)vul.disabled=true;if(roc)roc.disabled=true;
      if(!["auto","cpu"].includes(String(UI_CFG.renderBackend||"auto").toLowerCase())){
        UI_CFG.renderBackend="auto";
        saveUI();
      }
      return h;
    }catch(e){
      if(tag)tag.textContent="ERROR";
      if(info)info.textContent=e.message;
      return null;
    }
  };
  const saveHardware=async()=>{
    try{await post("/api/settings",{cpu_threads:Number(UI_CFG.cpuThreads)||1,decode_backend:UI_CFG.decodeBackend||"auto",render_backend:UI_CFG.renderBackend||"auto"});}
    catch(e){notify("บันทึก Performance ไม่สำเร็จ: "+e.message,false)}
  };
  $("#aiModeEnable")?.addEventListener("change",e=>{UI_CFG.aiMode=e.target.checked;saveUI();notify(e.target.checked?"เปิดโหมด AI แล้ว — แท็บ AI จะปรากฏในเมนู":"ปิดโหมด AI แล้ว — ซ่อนแท็บ AI")});
  $("#hwRecommend")?.addEventListener("click",async()=>{
    const h=await renderHardware();if(!h)return;
    UI_CFG.exportEncoder=h.recommended_encoder||"auto";
    UI_CFG.previewQuality=h.preview_quality||"fast";
    UI_CFG.cpuThreads=Number(h.recommended_cpu_threads||UI_CFG.cpuThreads||1);
    UI_CFG.filterPreviewFps=Number(h.recommended_filter_preview_fps||4);
    UI_CFG.decodeBackend=h.recommended_decode_backend||"auto";
    UI_CFG.renderBackend=h.recommended_render_backend||"auto";
    UI_CFG.smoothOriginal=true;
    saveUI();await saveHardware();notify("ตรวจสภาพเครื่องแล้ว ใช้ Profile ที่แนะนำแล้ว");
  });
  $("#hwScan")?.addEventListener("click",renderHardware);
  $("#hwCpuThreads")?.addEventListener("input",e=>{UI_CFG.cpuThreads=Number(e.target.value)||1;const v=$("#hwCpuThreadsValue");if(v)v.textContent=String(UI_CFG.cpuThreads);});
  $("#hwCpuThreads")?.addEventListener("change",saveHardware);
  $("#hwPreviewFps")?.addEventListener("input",e=>{UI_CFG.filterPreviewFps=Number(e.target.value)||4;const v=$("#hwPreviewFpsValue");if(v)v.textContent=String(UI_CFG.filterPreviewFps);saveUI();});
  $("#hwEncoder")?.addEventListener("change",e=>{UI_CFG.exportEncoder=e.target.value;saveUI();});
  $("#hwDecode")?.addEventListener("change",e=>{UI_CFG.decodeBackend=e.target.value;saveUI();saveHardware();});
  $("#hwBackend")?.addEventListener("change",e=>{UI_CFG.renderBackend=e.target.value;saveUI();saveHardware();});
  $("#hwSmoothOriginal")?.addEventListener("change",e=>{UI_CFG.smoothOriginal=e.target.checked;saveUI();});
  $("#uiSaveProfile")?.addEventListener("click",()=>{localStorage.setItem("paokra.settings.profile",JSON.stringify(UI_CFG));notify("บันทึก Settings Profile แล้ว");});
  $("#uiLoadProfile")?.addEventListener("click",()=>{try{const p=JSON.parse(localStorage.getItem("paokra.settings.profile")||"null");if(!p)throw new Error("ยังไม่มี Settings Profile ที่บันทึกไว้");UI_CFG={...UI_DEFAULT,...p};saveUI();saveHardware();notify("โหลด Settings Profile แล้ว")}catch(e){notify(e.message,false)}});
  $("#uiResetProfile")?.addEventListener("click",()=>{UI_CFG={...UI_DEFAULT};saveUI();saveHardware();notify("Reset Settings Profile แล้ว");});
  renderHardware();
}
const LANG={th:{Dashboard:"แดชบอร์ด",Accounts:"บัญชี",Media:"มีเดีย",["Scene Builder"]:"สร้างฉาก",["Video to Filter"]:"แต่งวิดีโอ",editor:"Video Editor",["AI Studio"]:"AI Studio",["Live Console"]:"คอนโซล LIVE",["LIVE Manager"]:"ตัวจัดการ LIVE",["TikTok Dashboard"]:"TikTok Dashboard",Comments:"คอมเมนต์",Logs:"บันทึก",Settings:"ตั้งค่า"},en:{Dashboard:"Dashboard",Accounts:"Accounts",Media:"Media",["Scene Builder"]:"Scene Builder",["Video to Filter"]:"Video to Filter",editor:"Video Editor",["AI Studio"]:"AI Studio",["Live Console"]:"Live Console",["LIVE Manager"]:"LIVE Manager",["TikTok Dashboard"]:"TikTok Dashboard",Comments:"Comments",Logs:"Logs",Settings:"Settings"}};
LANG.th["AI Seam Studio"]="AI Seam Studio";
LANG.en["AI Seam Studio"]="AI Seam Studio";
function applyLanguage(){
  const dict=LANG[UI_CFG.language]||LANG.th;
  $$(".side a").forEach(a=>{const key=TITLES[a.dataset.view];const textNode=[...a.childNodes].find(n=>n.nodeType===3);if(textNode)textNode.textContent=" "+(dict[key]||key)});
  $("#title").textContent=dict[TITLES[view]]||TITLES[view];
  const th=UI_CFG.language!=="en";
  const setText=(sel,txt)=>{const e=$(sel);if(e)e.textContent=txt};
  setText("#v2fHeadTitle",th?"แต่งวิดีโอ":"Video to Filter");
  setText("#v2fAdd",th?"＋ เพิ่มวิดีโอ":"＋ Add Video");
  setText("#v2fDownload",th?"ดาวน์โหลด":"Download");
  setText("#v2fPreviewState",th?"พรีวิวละเอียด · เฟรมปัจจุบัน":"Detail preview · current frame");
  setText("#v2fPlay",th?"▶ เล่น":"▶ Play");
  setText("#v2fReset",th?"รีเซ็ต":"Reset");
  setText("#v2fTabFilters",th?"ฟิลเตอร์":"Filters");
  setText("#v2fTabBeauty",th?"บิวตี้":"Beauty");
  setText("#v2fTabFace",th?"โครงหน้า":"Face");
  setText("#v2fEmptyTitle",th?"เพิ่มวิดีโอ":"Add a video");
  setText("#v2fPreviewState",th?"พรีวิวสด":"Live preview");
  setText("#v2fFps",th?"เอาต์พุต: 60 FPS":"Output: 60 FPS");
  setText("#v2fColorTitle",th?"สีและฟิลเตอร์":"Color & Filter");
  setText("#v2fColorNote",th?"ปรับก่อนส่งออก":"Adjust before export");
  const labels={
    v2fBrightness:th?"ความสว่าง":"Brightness",v2fContrast:th?"คอนทราสต์":"Contrast",v2fSaturation:th?"ความอิ่มสี":"Saturation",v2fWarmth:th?"โทนอบอุ่น":"Warmth",v2fSharpen:th?"ความคม":"Sharpen",v2fVignette:th?"ขอบมืด":"Vignette",v2fSkin:th?"ผิวเนียน":"Skin smooth",v2fWrinkle:th?"ลบรอยย่น":"Wrinkle remove",v2fSkinBright:th?"ผิวสว่าง":"Skin bright",v2fLip:th?"สีริมฝีปาก":"Lip tint",v2fFace:th?"เรียวหน้า":"Face slim",v2fForeheadWidth:th?"ความกว้างหน้าผาก":"Forehead width",v2fForeheadHeight:th?"ความสูงหน้าผาก":"Forehead height",v2fMidface:th?"ความกว้างกลางหน้า":"Midface width",v2fCheekbone:th?"โหนกแก้ม":"Cheekbone",v2fTemple:th?"ขมับ":"Temple",v2fFaceLength:th?"ความยาวใบหน้า":"Face length",v2fJaw:th?"ปรับแนวกราม":"Jaw refine",v2fJawWidth:th?"ความกว้างกราม":"Jaw width",v2fJawCurve:th?"ความโค้งกราม":"Jaw curve",v2fJawAngle:th?"มุมกราม":"Jaw angle",v2fChin:th?"ความกว้างคาง":"Chin width",v2fChinHeight:th?"ความสูงคาง":"Chin height",v2fChinRoundness:th?"ความกลมคาง":"Chin roundness",v2fChinTip:th?"ปลายคาง":"Chin tip",v2fEye:th?"การเปิดตา":"Eye opening"
  };
  for(const [id,txt] of Object.entries(labels)){const e=$("#"+id);const l=e?.closest(".field")?.querySelector("label");if(l){const out=l.querySelector("output");l.firstChild.nodeValue=txt+" ";}}
  aiSeamApplyLanguage();
}


async function loadManager(){
  try{MANAGER=await api("/api/manager/profiles")}catch(e){notify(e.message,false);return}
  const sel=$("#managerAccount");
  if(!sel)return;
  const current=sel.value;
  sel.innerHTML=MANAGER.profiles.length
    ? MANAGER.profiles.map(p=>`<option value="${esc(p.name)}">${esc(p.label||p.name)}</option>`).join("")
    : `<option value="">ยังไม่มี Session</option>`;
  if(current&&MANAGER.profiles.some(p=>p.name===current))sel.value=current;
  const p=MANAGER.profiles.find(x=>x.name===sel.value);
  const st=$("#managerSessionStatus");
  if(st){
    const ok=p?.type==="cookie"&&p.status?.configured;
    st.innerHTML=ok
      ? `<b>Cookie session พร้อม</b><p>${p.status.entries} entries · values hidden · เก็บไว้สำหรับขั้นต่อไป</p>`
      : `<b>Cookie file ยังไม่พร้อม</b><p>ยังไม่เปิดเผยหรือแก้ไขค่า cookie จาก UI</p>`;
    st.className=ok?"unavail":"errbox";
  }
}
$("#managerAccount")?.addEventListener("change",loadManager);
async function refreshCookieStatus(){
  try{const r=await api("/api/manager/profiles");const p=r.profiles?.find(x=>x.name==="__cookie_file__");const s=p?.status||{};
    const tag=$("#cookieTag"),box=$("#cookieStatus");if(tag){tag.textContent=s.configured?`LOADED · ${s.entries}`:"NOT LOADED";tag.className="tag"+(s.configured?" live":"")}
    if(box)box.innerHTML=s.configured?`<b>Cookie Session พร้อม</b><span>${s.entries} entries · values hidden</span>`:`<b>ยังไม่ได้โหลด</b><span>เลือกไฟล์ Cookie เพื่อใช้งาน Dashboard</span>`;
  }catch(e){}}
async function importCookieFile(file){
  if(!file)return;
  const name=String(file.name||"cookie.txt");
  if(!/\.(txt|cookies|json)$/i.test(name)){
    notify("รองรับไฟล์ Cookie .txt, .cookies หรือ .json เท่านั้น",false);return;
  }
  const d=new FormData();d.append("file",file,name);
  try{
    notify("กำลังตรวจสอบ Cookie Session…");
    const r=await api("/api/tiktok-dashboard/import-cookie-file",{method:"POST",body:d});
    notify(r.message||"นำเข้า Cookie Session แล้ว");
    await refreshCookieStatus();await loadManager();
  }catch(e){notify(e.message,false)}
}
$("#cookiePick")?.addEventListener("click",async()=>{
  try{
    if(window.paokra?.importCookie){
      const r=await window.paokra.importCookie();
      if(r?.canceled)return;
      if(!r?.ok)throw new Error(r?.error||"นำเข้า Cookie ไม่สำเร็จ");
      notify("นำเข้า Cookie Session แล้ว: "+(r.name||"ไฟล์ Cookie"));
      await refreshCookieStatus();await loadManager();
      return;
    }
  }catch(e){
    notify(e.message,false);return;
  }
  const input=$("#cookieFileFallback");
  if(input){input.value="";input.click();return}
  notify("ตัวเลือกไฟล์ Cookie ไม่พร้อม",false);
});
$("#cookieFileFallback")?.addEventListener("change",e=>{
  const file=e.target.files?.[0];e.target.value="";importCookieFile(file);
});
async function refreshTikTokSessionStatus(){
  const tag=$("#cookieTag"),box=$("#cookieStatus");
  try{
    if(window.paokra?.getTikTokDashboardStatus){
      const s=await window.paokra.getTikTokDashboardStatus();
      if(tag){tag.textContent=s.open?"OPEN":"READY";tag.className="tag"+(s.open?" live":"")}
      if(box)box.innerHTML=s.open
        ? "<b>TikTok Browser Session เปิดอยู่</b><span>URL: "+esc(s.url||"TikTok Dashboard")+"</span>"
        : "<b>Browser Session พร้อม</b><span>กด Open TikTok Session เพื่อเปิดและล็อกอินตามปกติ</span>";
      return s;
    }
  }catch(e){
    if(box)box.innerHTML="<b>Session status unavailable</b><span>"+esc(e.message)+"</span>";
  }
  if(tag){tag.textContent="READY";tag.className="tag"}
  if(box)box.innerHTML="<b>Browser Session พร้อม</b><span>เปิดผ่าน PAOKRA Desktop เพื่อใช้งาน TikTok Dashboard</span>";
  return null;
}
$("#cookieReload")?.addEventListener("click",refreshCookieStatus);

async function refreshTikTokDashboardStatus(){
  const s=$("#tiktokDashState");
  try{
    if(window.paokra?.getTikTokDashboardStatus){
      const r=await window.paokra.getTikTokDashboardStatus();
      if(r.open){
        if(s)s.innerHTML="<b>TikTok Dashboard เปิดอยู่</b><span>PAOKRA Browser Session · "+esc(r.url||"พร้อมใช้งาน")+"</span>";
      }else{
        if(s)s.innerHTML="<b>ยังไม่ได้เปิด</b><span>กด Open TikTok Dashboard เพื่อเปิดเซสชันของ PAOKRA</span>";
      }
      return r;
    }
    const r=await api("/api/tiktok-dashboard/status");
    return r;
  }catch(e){
    if(s)s.innerHTML="<b>ตรวจสอบ Dashboard ไม่ได้</b><span>"+esc(e.message)+"</span>";
    return null;
  }
}
async function openTikTokSession(){
  try{
    if(window.paokra?.openTikTokDashboard){
      const r=await window.paokra.openTikTokDashboard();
      if(!r?.ok)throw new Error(r?.error||"เปิด TikTok Browser Session ไม่สำเร็จ");
    }else{
      window.open("https://shop.tiktok.com/streamer/live/product/dashboard","_blank","noopener");
    }
    await refreshTikTokDashboardStatus();
    notify("TikTok Browser Session เปิดแล้ว — ใช้การล็อกอินปกติในหน้าต่างนี้");
    return true;
  }catch(e){
    notify(e.message,false);
    return false;
  }
}
$("#tiktokDashOpen")?.addEventListener("click",async()=>{
  const b=$("#tiktokDashOpen");
  if(b)b.disabled=true;
  try{ await openTikTokSession(); }
  finally{if(b)b.disabled=false}
});
$("#tiktokSessionOpen")?.addEventListener("click",openTikTokSession);
$("#tiktokSessionRefresh")?.addEventListener("click",refreshTikTokSessionStatus);
$("#accBrowserLogin")?.addEventListener("click",async()=>{
  const name=prompt("ชื่อ Browser Profile เช่น tiktok-main");
  if(!name?.trim())return;
  try{
    await post("/api/accounts/browser-login",{name:name.trim()});
    notify("เปิด Browser Profile แล้ว — Login ครั้งแรกในหน้าต่างนี้");
    loadManager();
  }catch(e){notify(e.message,false)}
});
async function loadAuth(){
  try{AUTH=await api("/api/auth/config")}catch{return}
  $("#cfgTag").textContent=AUTH.configured?"CONFIGURED":"NOT CONFIGURED";
  $("#cfgTag").className="tag"+(AUTH.configured?" live":"");
  $("#accConnect").disabled=!AUTH.configured;
  if(document.activeElement!==$("cfgKey"))$("#cfgKey").value="";
  $("#cfgKey").placeholder=AUTH.client_key?AUTH.client_key+" (บันทึกแล้ว)":"";
  if(document.activeElement!==$("#cfgRedirect"))$("#cfgRedirect").value=AUTH.redirect_uri||"";
}
$("#cfgSave").addEventListener("click",async()=>{
  const key=$("#cfgKey").value.trim();
  if(!key&&!AUTH.client_key){notify("ต้องระบุ Client key",false);return}
  try{await post("/api/auth/config",{client_key:key||"",client_secret:$("#cfgSecret").value,redirect_uri:$("#cfgRedirect").value});
    $("#cfgSecret").value="";notify("บันทึกแล้ว");loadAuth()}catch(e){notify(e.message,false)}
});
let authT=null,authWin=null;
$("#accConnect").addEventListener("click",async()=>{
  try{
    const r=await post("/api/auth/tiktok/start",{});
    authWin=window.open(r.url,"paokra_auth","width=520,height=760");
    if(!authWin){notify("เบราว์เซอร์บล็อกป๊อปอัป",false);return}
    notify("เปิดหน้าล็อกอิน TikTok แล้ว");clearInterval(authT);let ct=0;
    authT=setInterval(async()=>{
      try{const s=await api("/api/auth/result?state="+encodeURIComponent(r.state));
        if(s.status==="ok"){clearInterval(authT);notify("เชื่อมต่อ "+s.account.displayName+" แล้ว");poll()}
        else if(s.status==="error"){clearInterval(authT);notify(s.message,false)}
        else if(authWin?.closed&&++ct>2){clearInterval(authT);notify("ยกเลิก",false)}
      }catch{}},1500);
  }catch(e){notify(e.message,false)}
});
$("#accList").addEventListener("click",async e=>{
  const t=e.target.closest("[data-test]"),r=e.target.closest("[data-rm]");
  if(t){t.disabled=true;try{const x=await post("/api/accounts/"+t.dataset.test+"/test",{});notify(x.account.status==="connected"?"บัญชีปกติ":"ต้องเชื่อมต่อใหม่",x.account.status==="connected")}catch(err){notify(err.message,false)}poll()}
  if(r&&confirm("ลบบัญชีนี้?")){await run(()=>api("/api/accounts/"+r.dataset.rm,{method:"DELETE"}),"ลบแล้ว")}
});

/* ---------- media ---------- */
async function loadMedia(){
  const g=$("#mediaGrid");
  try{
    const {items}=await api("/api/media/list");
    $("#dMedia").textContent=items.length;
    g.innerHTML=items.length?items.map(m=>{
      const u="/media/"+encodeURIComponent(m.name);
      const pv=m.kind==="image"?`<img loading="lazy" src="${u}" alt="">`:`<video preload="metadata" muted src="${u}#t=0.5"></video>`;
      return `<div class="asset"><div class="pv">${pv}</div><div class="meta"><div class="nm" title="${esc(m.name)}">${esc(m.name)}</div><span>${m.kind} · ${fmtSize(m.size)}</span></div>
      <button class="btn ghost" data-use="${esc(m.name)}" data-kind="${m.kind}">${m.kind==="video"?"Use as source":"Use as overlay"}</button></div>`}).join("")
      :`<div class="empty" style="grid-column:1/-1"><b>ยังไม่มีไฟล์</b>อัปโหลดวิดีโอหรือรูป</div>`;
  }catch(e){g.innerHTML=`<div class="errbox" style="grid-column:1/-1">${esc(e.message)}</div>`}
}
$("#mediaGrid").addEventListener("click",e=>{
  const b=e.target.closest("[data-use]");if(!b)return;
  const name=b.dataset.use;
  if(b.dataset.kind==="video")run(()=>post("/api/source",{source:"/media/"+name}),"ตั้ง Source: "+name);
  else run(()=>post("/api/overlay/use",{name}),"ตั้ง Overlay: "+name);
});
let activeUploadXhr=null;
function setUploadProgress(visible,title,pct,detail,speed,eta,error=false){
  const box=$("#uploadProgress");if(!box)return;
  box.hidden=!visible;
  if(!visible)return;
  const bar=$("#uploadProgressBar");
  const p=clamp(Number(pct)||0,0,100);
  $("#uploadProgressTitle").textContent=title;
  $("#uploadProgressPct").textContent=p.toFixed(p>=99.95?0:1)+"%";
  $("#uploadProgressDetail").textContent=detail;
  $("#uploadProgressSpeed").textContent=speed||"—";
  $("#uploadProgressEta").textContent=eta||"—";
  if(bar){bar.style.width=p+"%";bar.classList.toggle("bad",!!error)}
  box.classList.toggle("error",!!error);
}
function uploadFileWithProgress(file,endpoint,index,totalFiles,totalBytes,completedBytes){
  return new Promise((resolve,reject)=>{
    const xhr=new XMLHttpRequest(),form=new FormData(),started=performance.now();
    let lastLoaded=0,lastAt=started;
    xhr._uploadedBytes=0;
    form.append("file",file);
    activeUploadXhr=xhr;
    xhr.open("POST",endpoint,true);
    xhr.timeout=0;
    xhr.upload.onprogress=e=>{
      const loaded=Math.min(e.loaded,file.size||e.loaded);
      xhr._uploadedBytes=loaded;
      const overallLoaded=Math.min(totalBytes,completedBytes+loaded);
      const pct=totalBytes?overallLoaded/totalBytes*100:0;
      const now=performance.now(),elapsed=Math.max(.001,(now-started)/1000);
      const recentSeconds=Math.max(.001,(now-lastAt)/1000);
      const recentBytes=Math.max(0,loaded-lastLoaded);
      const instant=recentBytes/recentSeconds;
      const avg=loaded/elapsed;
      const speed=avg>0?fmtBytes(avg)+"/s":"—";
      const remaining=Math.max(0,totalBytes-overallLoaded);
      const eta=avg>0?formatDuration(remaining/avg):"—";
      const title=totalFiles>1
        ? "กำลังอัปโหลด "+(index+1)+"/"+totalFiles+" · "+file.name
        : "กำลังอัปโหลด · "+file.name;
      setUploadProgress(true,title,pct,fmtBytes(overallLoaded)+" / "+fmtBytes(totalBytes),speed,"เหลือ ~"+eta);
      lastLoaded=loaded;lastAt=now;
    };
    xhr.onload=()=>{
      activeUploadXhr=null;
      let data={};
      try{data=JSON.parse(xhr.responseText||"{}")}catch{}
      if(xhr.status>=200&&xhr.status<300&&data.ok!==false){
        const overallLoaded=Math.min(totalBytes,completedBytes+file.size);
        const pct=totalBytes?overallLoaded/totalBytes*100:100;
        setUploadProgress(true,"กำลังตรวจสอบไฟล์ · "+file.name,pct,fmtBytes(overallLoaded)+" / "+fmtBytes(totalBytes),"อัปโหลดแล้ว","กำลังเตรียม Source…");
        resolve(data);
      }else{
        reject(new Error(data.error||("HTTP "+xhr.status)));
      }
    };
    xhr.onerror=()=>{activeUploadXhr=null;const err=new Error("เครือข่ายหรือการเชื่อมต่ออัปโหลดมีปัญหา");err.uploadedBytes=xhr._uploadedBytes||0;reject(err)};
    xhr.onabort=()=>{activeUploadXhr=null;const err=new Error("ยกเลิกการอัปโหลด");err.uploadedBytes=xhr._uploadedBytes||0;reject(err)};
    xhr.ontimeout=()=>{activeUploadXhr=null;const err=new Error("อัปโหลดหมดเวลา");err.uploadedBytes=xhr._uploadedBytes||0;reject(err)};
    xhr.send(form);
  });
}
function formatDuration(sec){
  sec=Math.max(0,Number(sec)||0);
  if(sec<60)return Math.ceil(sec)+"s";
  const m=Math.floor(sec/60),s=Math.ceil(sec%60);
  if(m<60)return m+"m "+s+"s";
  const h=Math.floor(m/60),rm=m%60;
  return h+"h "+rm+"m";
}
async function uploadFiles(files){
  const valid=[...files].filter(f=>{
    const ext=/\.(mp4|mov|mkv|webm|m4v|avi|png|jpg|jpeg|webp|bmp|gif)$/i.test(f.name);
    const isImg=f.type.startsWith("image/")||(!f.type&&/\.(png|jpg|jpeg|webp|bmp|gif)$/i.test(f.name));
    const isVid=f.type.startsWith("video/")||(!f.type&&/\.(mp4|mov|mkv|webm|m4v|avi)$/i.test(f.name));
    if((!isImg&&!isVid)||!ext){notify(f.name+": รองรับเฉพาะวิดีโอและรูป",false);return false}
    return true;
  });
  if(!valid.length)return;
  const totalBytes=valid.reduce((sum,f)=>sum+(Number(f.size)||0),0);
  let completedBytes=0;
  setUploadProgress(true,"กำลังเตรียมอัปโหลด…",0,"0 B / "+fmtBytes(totalBytes),"—","กำลังเริ่ม");
  try{
    for(let i=0;i<valid.length;i++){
      const f=valid[i];
      const isImage=f.type.startsWith("image/")||/\.(png|jpg|jpeg|webp|bmp|gif)$/i.test(f.name);
      const endpoint=isImage?"/api/overlay":"/api/media";
      const r=await uploadFileWithProgress(f,endpoint,i,valid.length,totalBytes,completedBytes);
      completedBytes+=Number(f.size)||0;
      notify("อัปโหลดแล้ว: "+f.name);
      if(r?.error)throw new Error(r.error);
    }
    await poll();await loadMedia();
    setUploadProgress(true,"อัปโหลดเสร็จทั้งหมด",100,fmtBytes(totalBytes)+" / "+fmtBytes(totalBytes),"เสร็จแล้ว","พร้อมใช้งาน");
    setTimeout(()=>setUploadProgress(false),1800);
  }catch(e){
    const attemptedBytes=Math.min(totalBytes,completedBytes+(Number(e.uploadedBytes)||0));
    setUploadProgress(true,"อัปโหลดไม่สำเร็จ",totalBytes?attemptedBytes/totalBytes*100:0,
      fmtBytes(attemptedBytes)+" / "+fmtBytes(totalBytes),"หยุด","ตรวจสอบข้อความแจ้งเตือน");
    notify("อัปโหลดไม่สำเร็จ: "+e.message,false);
    await poll();loadMedia();
  }
}
$("#upFile").addEventListener("change",e=>{uploadFiles([...e.target.files]);e.target.value=""});
$("#sceneImageFile")?.addEventListener("change",async e=>{
  const file=e.target.files?.[0];e.target.value="";if(!file)return;
  if(!file.type.startsWith("image/")&&!/\.(png|jpg|jpeg|webp|bmp|gif)$/i.test(file.name)){
    notify("กรุณาเลือกไฟล์ภาพ PNG/JPG/WebP/BMP/GIF",false);return;
  }
  const form=new FormData();form.append("file",file);
  notify("กำลังเพิ่ม Image Overlay: "+file.name);
  try{
    const r=await api("/api/overlay",{method:"POST",body:form});
    if(!r?.ok||!r.image)throw new Error(r?.error||"Image Overlay upload did not return an image path");
    await poll();await loadMedia();location.hash="#scenes";
    notify("เพิ่ม Image Overlay และ Layer แล้ว: "+file.name);
  }catch(err){notify("เพิ่ม Image Overlay ไม่สำเร็จ: "+err.message,false)}
});
const dz=$("#dropzone");
["dragenter","dragover"].forEach(t=>dz.addEventListener(t,e=>{e.preventDefault();dz.classList.add("over")}));
["dragleave","drop"].forEach(t=>dz.addEventListener(t,e=>{e.preventDefault();dz.classList.remove("over")}));
dz.addEventListener("drop",e=>uploadFiles([...e.dataTransfer.files]));

/* ════════════════════════════════════════════════════
   SCENE BUILDER — OBS-style media management
   
   หลักการ:
   - stageBg (source): naturalWidth/Height ของ stream
     แสดงแบบ contain ใน stage — ไม่บีบ ไม่ stretch
     เหลือพื้นที่ดำ (letterbox/pillarbox) ถ้าสัดส่วนไม่ตรง
   
   - image-item: กรอบ = "หน้าต่าง" ที่เปิดดูภาพ
     ภาพมีขนาดจริง (imgW x imgH) อยู่ใน wrapper
     wrapper มี overflow:hidden → ถ้ากรอบเล็กกว่าภาพ
     ภาพทะลุออกด้านหลัง (clipped) — แบบ OBS
     
     Resize กรอบ = ขยาย/ย่อหน้าต่าง ไม่ scale ภาพ
     Drag ภาพใน กรอบ = เลื่อน pan offset ของภาพ
   ════════════════════════════════════════════════════ */

const stage=$("#stage"),eT=$("#dragText"),eI=$("#dragImage"),eS=$("#dragSource"),eW=$("#dragWeb"),sourceImg=$("#stageBg"),sourceVideo=$("#stageBgVideo");
const K=()=>stage.clientWidth/(S?S.width:720);
let selectedTextId=null, textEls=new Map();
const textSaveTimers=new Map(),textSaveQueues=new Map(),textEditVersions=new Map();
let textDraft="";
function selectedText(){return selectedTextId?(S.texts||[]).find(x=>x.id===selectedTextId)||null:null}
function syncTextInspector(item,force=false){
  if(!item)return;
  const set=(id,v)=>{const el=$(id);if(force||document.activeElement!==el)el.value=v};
  set("#inText",item.text||"");set("#inTX",item.x??0);set("#inTY",item.y??0);set("#inTSize",item.size??28);
  const scroll=$("#inTScroll"),speed=$("#inTScrollSpeed");if(scroll)scroll.checked=!!item.scroll;if(speed)speed.value=String(Math.max(20,Math.min(600,Number(item.scroll_speed)||120)));
  const c=$("#textCount");if(c)c.textContent=(item.text||"").length+" / 4000";
}
function selectText(id){
  const item=(S.texts||[]).find(x=>x.id===id);
  if(!item)return;
  selectedTextId=id;textDraft=item.text||"";
  syncTextInspector(item,true);
  renderTextList();
  renderExtraTexts();
}
function renderTextList(){
  const box=$("#textLayers");if(!box)return;
  box.innerHTML=(S.texts||[]).map((t,i)=>`<div class="text-layer-row ${selectedTextId===t.id?"sel":""}" data-text-select="${t.id}">
    <span><b>${esc(t.text||"ข้อความ")}</b><small>${t.size||28}px · X ${t.x||0} Y ${t.y||0}</small></span>
    <button data-text-remove="${t.id}">×</button></div>`).join("");
}
async function addTextLayer(){
  const draft=String($("#inText")?.value??textDraft??"").slice(0,4000);
  const x=Math.round(Number($("#inTX")?.value)||24),y=Math.round(Number($("#inTY")?.value)||56);
  const size=Math.max(6,Math.min(400,Math.round(Number($("#inTSize")?.value)||28)));
  const scroll=!!$("#inTScroll")?.checked,scroll_speed=Math.max(20,Math.min(600,Math.round(Number($("#inTScrollSpeed")?.value)||120)));
  const text=draft.trim()?draft:("\u0e02\u0e49\u0e2d\u0e04\u0e27\u0e32\u0e21 "+((S.texts||[]).length+1));
  const r=await post("/api/text",{action:"add",text,x,y,size,scroll,scroll_speed});
  if(!r?.ok||!r.item)throw new Error(r?.error||"Could not add Text layer");
  S.texts=[...(S.texts||[]),r.item];S.layer_order=Array.isArray(r.layer_order)?r.layer_order:[...(Array.isArray(S.layer_order)?S.layer_order:[]),"text"].filter((v,i,a)=>a.indexOf(v)===i);
  textDraft=r.item.text||"";selectText(r.item.id);notify("Text layer added"+(r.item.scroll?" (marquee)":""));render();
}
$("#addText")?.addEventListener("click",()=>run(addTextLayer));
$("#textLayers")?.addEventListener("click",e=>{
  const rm=e.target.closest("[data-text-remove]");
  if(rm){run(async()=>{
    const id=rm.dataset.textRemove;clearTimeout(textSaveTimers.get(id));textSaveTimers.delete(id);textEditVersions.set(id,(textEditVersions.get(id)||0)+1);
    try{textEls.get(id)?._marqueeAnimation?.cancel()}catch(_){}
    await (textSaveQueues.get(id)||Promise.resolve()).catch(()=>{});
    const r=await post("/api/text",{action:"remove",id});if(!r?.ok)throw new Error(r?.error||"Could not remove Text layer");
    S.texts=(S.texts||[]).filter(x=>x.id!==id);if(Array.isArray(r.layer_order))S.layer_order=r.layer_order;
    if(selectedTextId===id){selectedTextId=null;textDraft=""}render();notify("Text layer removed");
  });return}
  const row=e.target.closest("[data-text-select]");if(row)selectText(row.dataset.textSelect);
});

function renderExtraTexts(){
  if(!S)return;
  const live=new Set((S.texts||[]).map(t=>t.id));
  for(const [id,el] of textEls){if(!live.has(id)){el.remove();textEls.delete(id)}}
  const k=K();
  for(const t of (S.texts||[])){
    let el=textEls.get(t.id);
    if(!el){el=document.createElement("div");el.className="item text-item extra-text";el.dataset.textId=t.id;el.tabIndex=0;stage.appendChild(el);textEls.set(t.id,el);
      el.addEventListener("pointerdown",e=>{
        e.preventDefault();selectText(t.id);el.setPointerCapture(e.pointerId);
        const item=(S.texts||[]).find(x=>x.id===t.id)||t;
        el._drag={sx:e.clientX,sy:e.clientY,ox:item.x*k,oy:item.y*k};
      });
      el.addEventListener("pointermove",e=>{if(!el._drag)return;el.style.left=(el._drag.ox+e.clientX-el._drag.sx)+"px";el.style.top=(el._drag.oy+e.clientY-el._drag.sy)+"px"});
      el.addEventListener("pointerup",async()=>{if(!el._drag)return;const x=Math.round(parseFloat(el.style.left)/K()),y=Math.round(parseFloat(el.style.top)/K());el._drag=null;await run(()=>post("/api/text",{action:"update",id:t.id,x,y}))});
    }
    el.textContent=t.text||"";el.style.fontSize=((t.size||28)*k)+"px";el.style.whiteSpace=t.scroll?"pre":"pre-wrap";el.style.left=(t.scroll?0:(t.x*k))+"px";el.style.top=(t.y*k)+"px";el.style.opacity=t.enabled===false?.3:1;
    const scrolling=!!(t.scroll&&t.enabled!==false&&String(t.text||""));
    if(scrolling){const sig=[S.width,k,t.size,t.scroll_speed,t.text].join("|");if(el.dataset.marqueeSignature!==sig){try{el._marqueeAnimation?.cancel()}catch(_){};const speed=Math.max(20,Math.min(600,Number(t.scroll_speed)||120)),from=Math.max(1,S.width*k),to=-(el.scrollWidth||String(t.text||"").length*(Number(t.size)||28)*.55);el._marqueeAnimation=el.animate([{transform:`translateX(${from}px)`},{transform:`translateX(${to}px)`}],{duration:Math.max(800,((from-to)/(speed*k))*1000),easing:"linear",iterations:Infinity});el.dataset.marqueeSignature=sig}}
    else{try{el._marqueeAnimation?.cancel()}catch(_){}el._marqueeAnimation=null;delete el.dataset.marqueeSignature;el.style.transform="none"}
    el.classList.toggle("sel",selectedTextId===t.id);
  }
  renderTextList();
}

/* --- layout stage และ items --- */
function layoutConsolePreview(){
  if(!S||view!=="console")return;
  const box=$(".progbox"),card=$(".prog");
  if(!box||!card)return;
  const maxW=Math.max(240,card.clientWidth-40);
  const maxH=Math.max(240,innerHeight-190);
  const ratio=S.width/S.height;
  let w=Math.min(maxW,maxH*ratio),h=w/ratio;
  if(h>maxH){h=maxH;w=h*ratio;}
  w=Math.floor(w);h=Math.floor(h);
  box.style.width=w+"px";
  box.style.height=h+"px";
  box.style.aspectRatio="auto";
  box.style.maxWidth="none";
  box.style.maxHeight="none";
}

function layout(){
  if(!S||view!=="scenes"||drag)return;
  const wrap=$(".canvas-wrap");
  const maxW=wrap.clientWidth||600;
  const maxH=Math.max(320,innerHeight-200);
  const ratio=S.width/S.height;
  let sw=Math.min(maxW,maxH*ratio);
  let sh=sw/ratio;
  if(sh>maxH){sh=maxH;sw=sh*ratio;}
  sw=Math.floor(sw);sh=Math.floor(sh);
  stage.style.width=sw+"px";stage.style.height=sh+"px";
  $(".stage-bar").style.width=sw+"px";
  const k=K();

  // Video source — real transform box, preserving source aspect ratio
  const layerOrder=Array.isArray(S.layer_order)?S.layer_order:[];
  eS.hidden=!(S.source&&layerOrder.includes("source"));
  if(S.source&&layerOrder.includes("source")){
    eS.style.left=(S.source_x*k)+"px";
    eS.style.top=(S.source_y*k)+"px";
    eS.style.width=(S.source_width*k)+"px";
    eS.style.height=(S.source_height*k)+"px";
    eS.style.zIndex=(S.layer_order||["text","image","web","source"]).length-(S.layer_order||["text","image","web","source"]).indexOf("source");
    // Video keeps its aspect ratio. The Source box clips it instead of
    // stretching/squeezing it when the box shape changes.
    const fitMode=S.source_fit_mode==="contain"?"contain":"cover";
    const flip=(S.source_flip_h?"scaleX(-1)":"scaleX(1)")+" "+(S.source_flip_v?"scaleY(-1)":"scaleY(1)");
    [sourceImg,sourceVideo].filter(Boolean).forEach(el=>{
      el.style.position="absolute";el.style.left="0";el.style.top="0";
      el.style.width="100%";el.style.height="100%";el.style.maxWidth="none";el.style.maxHeight="none";
      el.style.objectFit=fitMode;el.style.objectPosition="center";el.style.transform=flip;
    });
    const src=String(S.source||"");
    const nativeLocal=/^(?:[A-Za-z]:[\\/]|\/media\/)/.test(src) && /\.(mp4|m4v|webm)$/i.test(src);
    if(nativeLocal && sourceVideo){
      sourceImg.hidden=true;sourceImg.removeAttribute("src");
      sourceVideo.hidden=false;
      const srcUrl="/source.mp4?v="+encodeURIComponent(S.source_serial||0);
      if(sourceVideo.getAttribute("src")!==srcUrl){sourceVideo.src=srcUrl;sourceVideo.load();}
      // Scene Builder is a monitor of the same presentation clock, not a second
      // looping player. Clip Flow sets S.loop=false, so it cannot rewind a clip
      // while the backend is waiting for EOF and switching Open/Normal/Stop.
      sourceVideo.muted=true;sourceVideo.volume=0;sourceVideo.loop=!!S.loop;sourceVideo.autoplay=false;
    }else{
      sourceVideo?.pause();sourceVideo?.removeAttribute("src");
      if(sourceVideo)sourceVideo.hidden=true;
      sourceImg.hidden=false;
      const srcUrl="/source.mjpg?quality="+encodeURIComponent(UI_CFG.previewQuality||"balanced")+"&v="+encodeURIComponent(S.source_serial||0);
      if(sourceImg.getAttribute("src")!==srcUrl)sourceImg.src=srcUrl;
    }
    eS.style.opacity=S.source_enabled?1:.35;
    if(sourceVideo){
      const shouldPlayPreview=!!(S.running&&S.source_enabled&&layerOrder.includes("source"));
      if(shouldPlayPreview && sourceVideo.paused)sourceVideo.play().catch(()=>{});
      if(!shouldPlayPreview && !sourceVideo.paused)sourceVideo.pause();
    }
  }

  // Text
  eT.textContent=S.text||"";
  eT.style.fontSize=((S.text_size||TEXT_FS)*k)+"px";
  eT.style.left=(S.text_x*k)+"px";
  eT.style.top=((S.text_y-TEXT_OFF)*k)+"px";
  eT.style.opacity=S.text_enabled?1:.3;
  eT.hidden=!(S.legacy_text_added&&S.text&&layerOrder.includes("text"));

  // Image overlay — OBS style
  eI.hidden=!(S.image&&layerOrder.includes("image"));
  if(S.image&&layerOrder.includes("image")){
    const img=$("img",eI);
    const src="/media/"+encodeURIComponent(base(S.image));
    if(img.getAttribute("src")!==src)img.src=src;

    // กรอบ = พื้นที่ที่เห็น (image_width x image_height ในพิกเซล output)
    const fw=(S.image_width||200)*k;
    const fh=(S.image_height||200)*k;
    eI.style.left=(S.image_x*k)+"px";
    eI.style.top=(S.image_y*k)+"px";
    eI.style.width=fw+"px";
    eI.style.height=fh+"px";

    // ภาพจริง: ขนาด natural (ไม่ scale) — ทะลุออกด้านหลัง
    // pan offset: image_pan_x, image_pan_y (default 0,0 = top-left ชิดกรอบ)
    const nw=img.naturalWidth||0,nh=img.naturalHeight||0;
    if(!nw&&!img._pkL){img._pkL=1;img.addEventListener("load",()=>{img._pkL=0;window.dispatchEvent(new Event("resize"))},{once:true});}
    const fit=S.image_fit||"contain",zm=S.image_zoom||1,FW=S.image_width||200,FH=S.image_height||200;
    let s1=1,s2=1;
    if(nw&&nh){
      if(fit==="stretch"){s1=FW/nw;s2=FH/nh}
      else if(fit==="cover"){s1=s2=Math.max(FW/nw,FH/nh)}
      else if(fit==="free"){s1=s2=1}
      else{s1=s2=Math.min(FW/nw,FH/nh)}
    }
    s1*=zm;s2*=zm;
    const dw=(nw||FW)*s1,dh=(nh||FH)*s2;
    const ox=fit==="free"?0:(FW-dw)/2,oy=fit==="free"?0:(FH-dh)/2;
    img.style.position="absolute";
    img.style.left=((ox+(S.image_pan_x||0))*k)+"px";
    img.style.top=((oy+(S.image_pan_y||0))*k)+"px";
    img.style.width=(dw*k)+"px";
    img.style.height=(dh*k)+"px";
    img.style.maxWidth="none";
    img.style.maxHeight="none";
    img.style.transform="none";
    eI.style.opacity=S.image_enabled?1:.3;
  }
  const ew=$("#dragWeb"),wimg=$("#stageWeb");
  ew.hidden=!(S.web_url&&S.web_enabled&&layerOrder.includes("web"));
  if(S.web_url&&S.web_enabled&&layerOrder.includes("web")){
    ew.style.left=(S.web_x*k)+"px";ew.style.top=(S.web_y*k)+"px";
    ew.style.width=(S.web_width*k)+"px";ew.style.height=(S.web_height*k)+"px";
    const wu="/web.mjpg";
    if(wimg.getAttribute("src")!==wu)wimg.src=wu;
    ew.style.opacity=1;
  }
  if(!(S.web_url&&S.web_enabled&&layerOrder.includes("web")))wimg?.removeAttribute("src");
  renderExtraTexts();
  const zo=(Array.isArray(S.layer_order)?S.layer_order:[]);
  const z=(id)=>{const i=zo.indexOf(id);return i<0?0:zo.length-i};
  $("#stageBg").style.zIndex=z("source");
  $("#dragWeb").style.zIndex=z("web");
  eI.style.zIndex=z("image");
  eT.style.zIndex=z("text");
  for(const el of textEls.values())el.style.zIndex=z("text");
}

// Scene Builder's native video preview follows the same monotonic presentation
// clock as the compositor. This prevents returning to Scenes from reloading a
// long-running clip at 00:00 and prevents a one-shot Clip Flow preview looping
// while Open/Normal/Stop is transitioning between decoder generations.
let sceneClockInFlight=false, sceneClockWasRunning=null, sceneClockReceivedPerf=0;
async function syncScenePreviewClock(){
  const video=sourceVideo;
  if(!video)return;
  if(view!=="scenes" || document.hidden || !document.hasFocus() || video.hidden || !S?.source){
    if(!video.paused)video.pause();
    sceneClockWasRunning=null;
    return;
  }
  if(sceneClockInFlight)return;
  sceneClockInFlight=true;
  try{
    const res=await fetch("/api/smooth",{cache:"no-store"});
    if(!res.ok)return;
    const q=await res.json();
    sceneClockReceivedPerf=performance.now();
    if(!q.source || !q.running || q.source_enabled===false || q.origin==null){
      video.pause();
      if(video.readyState>=2 && (sceneClockWasRunning===true || video.currentTime>0.05)){
        try{video.currentTime=0}catch{}
      }
      video.playbackRate=1;
      sceneClockWasRunning=false;
      return;
    }
    video.loop=!!q.loop;
    const dur=Number(q.loop_duration)>0.2?Number(q.loop_duration):(Number.isFinite(video.duration)?video.duration:0);
    const serverNow=Number(q.server_now||0)+(performance.now()-sceneClockReceivedPerf)/1000;
    const elapsed=serverNow-Number(q.origin);
    if(elapsed<0){
      video.pause();
      if(video.readyState>=2 && video.currentTime>0.05){try{video.currentTime=0}catch{}}
      sceneClockWasRunning=true;
      return;
    }
    const expected=q.loop&&dur>0.2?((elapsed%dur)+dur)%dur:(dur>0.2?Math.min(elapsed,dur):elapsed);
    if(video.readyState<2)return;
    if(video.paused || sceneClockWasRunning!==true){
      try{video.currentTime=expected}catch{}
      video.playbackRate=1;
      const p=video.play();if(p&&p.catch)p.catch(()=>{});
      sceneClockWasRunning=true;
      return;
    }
    let drift=expected-video.currentTime;
    if(q.loop&&dur>0.2)drift=((drift+dur/2)%dur+dur)%dur-dur/2;
    if(Math.abs(drift)>0.25){
      try{video.currentTime=expected}catch{}
      video.playbackRate=1;
    }else if(Math.abs(drift)>0.03){
      video.playbackRate=1+Math.max(-0.08,Math.min(0.08,drift*0.5));
    }else{
      video.playbackRate=1;
    }
    sceneClockWasRunning=true;
  }catch(_){}
  finally{sceneClockInFlight=false}
}
setInterval(syncScenePreviewClock,300);
window.addEventListener("hashchange",()=>{sceneClockWasRunning=null;setTimeout(syncScenePreviewClock,0)});
document.addEventListener("visibilitychange",()=>{if(!document.hidden){sceneClockWasRunning=null;syncScenePreviewClock()}});
if(sourceVideo)sourceVideo.addEventListener("loadeddata",()=>{sceneClockWasRunning=null;syncScenePreviewClock()});
window.addEventListener("resize",()=>{layout();layoutConsolePreview()});
$("#sceneRefresh")?.addEventListener("click",async()=>{
  const b=$("#sceneRefresh");if(b)b.disabled=true;
  try{
    await poll();
    layout();
    if(S?.source)$("#stageBg").src="/source.mjpg?v="+encodeURIComponent((S.source_serial||0)+"_"+Date.now());
    if(S?.web_url)$("#stageWeb").src="/web.mjpg?v="+Date.now();
    notify("Scene refreshed");
  }finally{if(b)b.disabled=false}
});

/* claude-fix-source */
/* --- Text drag --- */
eT.addEventListener("pointerdown",e=>{
  e.preventDefault();eT.focus();eT.setPointerCapture(e.pointerId);
  drag={mode:"move",el:eT,kind:"text",sx:e.clientX,sy:e.clientY,
        ox:parseFloat(eT.style.left)||0,oy:parseFloat(eT.style.top)||0};
});
eT.addEventListener("pointermove",e=>{
  if(!drag||drag.el!==eT||drag.mode!=="move")return;
  const T=6,w=eT.offsetWidth,h=eT.offsetHeight;
  let x=clamp(drag.ox+e.clientX-drag.sx,0,stage.clientWidth-w);
  let y=clamp(drag.oy+e.clientY-drag.sy,0,stage.clientHeight-h);
  if(Math.abs(x+w/2-stage.clientWidth/2)<T)x=stage.clientWidth/2-w/2;
  if(Math.abs(y+h/2-stage.clientHeight/2)<T)y=stage.clientHeight/2-h/2;
  eT.style.left=x+"px";eT.style.top=y+"px";
  $("#gv").hidden=Math.abs(x+w/2-stage.clientWidth/2)>1;
  $("#gh").hidden=Math.abs(y+h/2-stage.clientHeight/2)>1;
});
eT.addEventListener("pointerup",async()=>{
  if(!drag||drag.el!==eT)return;
  drag=null;$("#gv").hidden=$("#gh").hidden=true;
  const k=K();
  await run(()=>setCfg({text_x:Math.round(parseFloat(eT.style.left)/k),text_y:Math.round(parseFloat(eT.style.top)/k)+TEXT_OFF}));
});
eT.addEventListener("keydown",e=>{
  const d={ArrowLeft:[-1,0],ArrowRight:[1,0],ArrowUp:[0,-1],ArrowDown:[0,1]}[e.key];if(!d)return;
  e.preventDefault();const n=(e.shiftKey?10:1)*K();
  eT.style.left=clamp(parseFloat(eT.style.left)+d[0]*n,0,stage.clientWidth-eT.offsetWidth)+"px";
  eT.style.top=clamp(parseFloat(eT.style.top)+d[1]*n,0,stage.clientHeight-eT.offsetHeight)+"px";
  clearTimeout(nudgeT);nudgeT=setTimeout(async()=>{
    const k=K();await run(()=>setCfg({text_x:Math.round(parseFloat(eT.style.left)/k),text_y:Math.round(parseFloat(eT.style.top)/k)+TEXT_OFF}));
  },300);
});

/* --- Image overlay: OBS-style drag + resize ---
   
   3 โหมดบน image-item:
   A) drag บนภาพ (ไม่ใช่ handle) → เลื่อน pan (offset ภาพในกรอบ)
   B) drag handle ขอบ/มุม     → resize กรอบ (ภาพไม่เปลี่ยนขนาด)
   C) drag กรอบด้วย Shift      → ย้ายกรอบทั้งหมด
*/
function makeHandles(){
  ["n","s","e","w","ne","nw","se","sw"].forEach(s=>{
    const h=document.createElement("span");
    h.className="hdl hdl-"+s;h.dataset.hdl=s;eI.appendChild(h);
  });
}
makeHandles();
function makeSourceHandles(){
  ["n","s","e","w","nw","ne","sw","se"].forEach(s=>{
    const h=document.createElement("span");
    h.className="hdl hdl-"+s;h.dataset.sourceHdl=s;eS.appendChild(h);
  });
}
makeSourceHandles();
function makeWebHandles(){
  ["n","s","e","w","nw","ne","sw","se"].forEach(s=>{
    const h=document.createElement("span");
    h.className="hdl hdl-"+s;h.dataset.webHdl=s;eW.appendChild(h);
  });
}
makeWebHandles();

const gv=$("#gv"),gh=$("#gh");
const SNAP_PX=8;
function snapBox(x,y,w,h){
  const sw=stage.clientWidth,sh=stage.clientHeight;
  const xc=x+w/2,yc=y+h/2;
  const xCandidates=[
    {d:Math.abs(x),v:0,left:0},
    {d:Math.abs(xc-sw/2),v:sw/2,left:sw/2-w/2},
    {d:Math.abs(x+w-sw),v:sw,left:sw-w}
  ];
  const yCandidates=[
    {d:Math.abs(y),v:0,top:0},
    {d:Math.abs(yc-sh/2),v:sh/2,top:sh/2-h/2},
    {d:Math.abs(y+h-sh),v:sh,top:sh-h}
  ];
  const sx=xCandidates.find(c=>c.d<=SNAP_PX),sy=yCandidates.find(c=>c.d<=SNAP_PX);
  if(sx){x=sx.left;gv.hidden=false;gv.style.left=sx.v+"px"}else gv.hidden=true;
  if(sy){y=sy.top;gh.hidden=false;gh.style.top=sy.v+"px"}else gh.hidden=true;
  return {x,y};
}
function clearSnapGuides(){gv.hidden=true;gh.hidden=true}

// ใช้ document-level listener เพื่อไม่ให้ pointer หลุดเมื่อขยับเร็ว
let _pid=null; // active pointer id

eS.addEventListener("pointerdown",e=>{
  if(!S?.source||!Array.isArray(S.layer_order)||!S.layer_order.includes("source"))return;
  e.preventDefault();e.stopPropagation();_pid=e.pointerId;eS.setPointerCapture(e.pointerId);
  const side=e.target.dataset.sourceHdl;
  const k=K();
  drag={mode:side?"source-resize":"source-move",side,sx:e.clientX,sy:e.clientY,
        ox:S.source_x*k,oy:S.source_y*k,ow:S.source_width*k,oh:S.source_height*k,
        aspect:(S.source_width||1)/(S.source_height||1)};
});
eS.addEventListener("pointermove",e=>{
  if(!drag||e.pointerId!==_pid)return;
  if(drag.mode==="source-move"){
    const sn=snapBox(drag.ox+e.clientX-drag.sx,drag.oy+e.clientY-drag.sy,drag.ow,drag.oh);
    eS.style.left=sn.x+"px";eS.style.top=sn.y+"px";
  }else{
    const dx=e.clientX-drag.sx,dy=e.clientY-drag.sy,s=drag.side;
    let x=drag.ox,y=drag.oy,w=drag.ow,h=drag.oh;
    if(s.includes("e"))w=Math.max(40,drag.ow+dx);
    if(s.includes("w")){w=Math.max(40,drag.ow-dx);x=drag.ox+drag.ow-w}
    if(s.includes("s"))h=Math.max(40,drag.oh+dy);
    if(s.includes("n")){h=Math.max(40,drag.oh-dy);y=drag.oy+drag.oh-h}
    if(e.shiftKey&&(s.length===2)){
      const aspect=drag.aspect;
      const dw=w/drag.ow,dh=h/drag.oh,scale=Math.max(dw,dh);
      w=Math.max(40,drag.ow*scale);h=Math.max(40,w/aspect);
      if(s.includes("w"))x=drag.ox+drag.ow-w;
      if(s.includes("n"))y=drag.oy+drag.oh-h;
    }
    eS.style.left=x+"px";eS.style.top=y+"px";eS.style.width=w+"px";eS.style.height=h+"px";
    drag.cur={x,y,w,h};
  }
});
eS.addEventListener("pointerup",async e=>{
  if(!drag||e.pointerId!==_pid)return;
  const k=K(),mode=drag.mode,cur=drag.cur;
  const x=Math.round(parseFloat(eS.style.left)/k),y=Math.round(parseFloat(eS.style.top)/k);
  const w=Math.max(40,Math.round(eS.offsetWidth/k)),h=Math.max(40,Math.round(eS.offsetHeight/k));
  drag=null;_pid=null;clearSnapGuides();
  if(mode==="source-move")await run(()=>setCfg({source_x:x,source_y:y}));
  else await run(()=>setCfg({source_x:x,source_y:y,source_width:w,source_height:h}));
});
// Double click = fit the full clip inside the current output without distortion.
eS.addEventListener("dblclick",e=>{
  if(!S?.source||e.target.dataset.sourceHdl)return;
  const asp=Number(S.source_aspect)||((S.source_width||1)/(S.source_height||1))||1;
  let w=S.width,h=Math.round(w/asp);
  if(h>S.height){h=S.height;w=Math.round(h*asp)}
  run(()=>setCfg({source_x:Math.round((S.width-w)/2),source_y:Math.round((S.height-h)/2),
                  source_width:Math.max(40,w),source_height:Math.max(40,h),source_fit_mode:"contain",
                  source_flip_h:false,source_flip_v:false}),"Reset Source แล้ว");
});

const sourceMenu=(()=>{
  const m=document.createElement("div");
  m.id="sourceContextMenu";
  m.className="source-context-menu";
  m.hidden=true;
  m.innerHTML='<div class="source-context-title">Video settings</div>'
    +'<button type="button" data-source-action="cover">▣ <span><b>เต็มพื้นที่ Preview</b><small>เต็มกรอบอัตโนมัติ · ตัดส่วนเกินตามสัดส่วน</small></span></button>'
    +'<button type="button" data-source-action="contain">□ <span><b>พอดีโดยไม่ตัด</b><small>รักษาสัดส่วน · มีพื้นที่ว่างได้</small></span></button>'
    +'<div class="source-context-sep"></div>'
    +'<button type="button" data-source-action="flip-h">↔ <span><b>กระจกซ้าย ↔ ขวา</b><small>Mirror แนวนอน</small></span><i data-check="h"></i></button>'
    +'<button type="button" data-source-action="flip-v">↕ <span><b>กลับหัว</b><small>กลับด้านบน ↔ ล่าง</small></span><i data-check="v"></i></button>'
    +'<button type="button" data-source-action="mirror-both">◈ <span><b>กระจกกลับด้าน</b><small>กลับทั้งแนวนอนและแนวตั้ง</small></span><i data-check="both"></i></button>'
    +'<div class="source-context-sep"></div>'
    +'<button type="button" data-source-action="reset">↺ <span><b>Reset Video</b><small>จัดกลางและคืนสัดส่วนต้นฉบับ</small></span></button>';
  document.body.appendChild(m);
  return m;
})();
function closeSourceMenu(){sourceMenu.hidden=true}
function openSourceMenu(e){
  if(!S?.source||!Array.isArray(S.layer_order)||!S.layer_order.includes("source"))return;
  e.preventDefault();
  sourceMenu.querySelector('[data-check="h"]').textContent=S.source_flip_h?"✓":"";
  sourceMenu.querySelector('[data-check="v"]').textContent=S.source_flip_v?"✓":"";
  sourceMenu.querySelector('[data-check="both"]').textContent=(S.source_flip_h&&S.source_flip_v)?"✓":"";
  sourceMenu.style.left=Math.min(e.clientX,innerWidth-320)+"px";
  sourceMenu.style.top=Math.min(e.clientY,innerHeight-390)+"px";
  sourceMenu.hidden=false;
}
eS.addEventListener("contextmenu",openSourceMenu);
const programPreviewBox=$(".progbox");
if(programPreviewBox)programPreviewBox.addEventListener("contextmenu",openSourceMenu);
sourceMenu.addEventListener("click",async e=>{
  const b=e.target.closest("[data-source-action]");if(!b||!S?.source)return;
  const action=b.dataset.sourceAction;closeSourceMenu();
  const patch={};
  if(action==="cover"){
    Object.assign(patch,{source_x:0,source_y:0,source_width:S.width,source_height:S.height,source_fit_mode:"cover"});
  }else if(action==="contain"){
    const asp=Number(S.source_aspect)||((S.source_width||1)/(S.source_height||1))||1;
    let w=S.width,h=Math.round(w/asp);
    if(h>S.height){h=S.height;w=Math.round(h*asp)}
    Object.assign(patch,{source_x:Math.round((S.width-w)/2),source_y:Math.round((S.height-h)/2),
      source_width:Math.max(40,w),source_height:Math.max(40,h),source_fit_mode:"contain"});
  }else if(action==="flip-h"){
    patch.source_flip_h=!S.source_flip_h;
  }else if(action==="flip-v"){
    patch.source_flip_v=!S.source_flip_v;
  }else if(action==="mirror-both"){
    const both=!!S.source_flip_h&&!!S.source_flip_v;
    patch.source_flip_h=!both;patch.source_flip_v=!both;
  }else if(action==="reset"){
    const asp=Number(S.source_aspect)||((S.source_width||1)/(S.source_height||1))||1;
    let w=S.width,h=Math.round(w/asp);
    if(h>S.height){h=S.height;w=Math.round(h*asp)}
    Object.assign(patch,{source_x:Math.round((S.width-w)/2),source_y:Math.round((S.height-h)/2),
      source_width:Math.max(40,w),source_height:Math.max(40,h),source_fit_mode:"contain",source_flip_h:false,source_flip_v:false});
  }
  await run(()=>setCfg(patch),"บันทึก Video settings แล้ว");
});
document.addEventListener("pointerdown",e=>{if(!sourceMenu.hidden&&!e.target.closest("#sourceContextMenu"))closeSourceMenu()});
document.addEventListener("scroll",closeSourceMenu,true);
window.addEventListener("resize",closeSourceMenu);

eW.addEventListener("pointerdown",e=>{
  if(!S?.web_url)return;
  e.preventDefault();e.stopPropagation();_pid=e.pointerId;eW.setPointerCapture(e.pointerId);
  const side=e.target.dataset.webHdl;
  drag={mode:side?"web-resize":"web-move",side,sx:e.clientX,sy:e.clientY,
        ox:parseFloat(eW.style.left)||0,oy:parseFloat(eW.style.top)||0,
        ow:eW.offsetWidth,oh:eW.offsetHeight};
});
eW.addEventListener("pointermove",e=>{
  if(!drag||e.pointerId!==_pid)return;
  if(drag.mode==="web-move"){
    const sn=snapBox(drag.ox+e.clientX-drag.sx,drag.oy+e.clientY-drag.sy,drag.ow,drag.oh);
    eW.style.left=sn.x+"px";eW.style.top=sn.y+"px";
  }else if(drag.mode==="web-resize"){
    const dx=e.clientX-drag.sx,dy=e.clientY-drag.sy,s=drag.side;
    let w=drag.ow,h=drag.oh,x=drag.ox,y=drag.oy;
    if(s==="e")w=Math.max(64,drag.ow+dx);
    if(s==="w"){w=Math.max(64,drag.ow-dx);x=drag.ox+drag.ow-w}
    if(s==="s")h=Math.max(64,drag.oh+dy);
    if(s==="n"){h=Math.max(64,drag.oh-dy);y=drag.oy+drag.oh-h}
    if(["nw","ne","sw","se"].includes(s)){
      const sx=s.includes("e")?1:-1,sy=s.includes("s")?1:-1;
      const delta=Math.abs(dx)>Math.abs(dy)?dx*sx:dy*sy;
      w=Math.max(64,drag.ow+delta);h=Math.max(64,drag.oh+delta*(drag.oh/Math.max(1,drag.ow)));
      if(s.includes("w"))x=drag.ox+drag.ow-w;
      if(s.includes("n"))y=drag.oy+drag.oh-h;
    }
    eW.style.left=x+"px";eW.style.top=y+"px";eW.style.width=w+"px";eW.style.height=h+"px";
  }
});
eW.addEventListener("pointerup",async e=>{
  if(!drag||e.pointerId!==_pid)return;
  const k=K(),x=Math.round(parseFloat(eW.style.left)/k),y=Math.round(parseFloat(eW.style.top)/k);
  const w=Math.max(64,Math.round(eW.offsetWidth/k)),h=Math.max(64,Math.round(eW.offsetHeight/k));
  const mode=drag.mode;
  drag=null;_pid=null;clearSnapGuides();
  await run(()=>post("/api/web/config",{x,y,...(mode==="web-resize"?{width:w,height:h}:{})}),
            mode==="web-resize"?"บันทึกขนาด Web แล้ว":"บันทึกตำแหน่ง Web แล้ว");
});

eI.addEventListener("pointerdown",e=>{
  e.preventDefault();e.stopPropagation();
  _pid=e.pointerId;
  const hdl=e.target.dataset.hdl;
  const k=K();
  if(hdl){
    // โหมด B: resize กรอบ
    eI.setPointerCapture(e.pointerId);
    drag={mode:"resize",side:hdl,
          sx:e.clientX,sy:e.clientY,
          ox:parseFloat(eI.style.left)||0,oy:parseFloat(eI.style.top)||0,
          ow:eI.offsetWidth,oh:eI.offsetHeight};
  } else if(e.shiftKey){
    // โหมด C: ย้ายกรอบ
    eI.setPointerCapture(e.pointerId);
    drag={mode:"move-frame",
          sx:e.clientX,sy:e.clientY,
          ox:parseFloat(eI.style.left)||0,oy:parseFloat(eI.style.top)||0};
  } else {
    // โหมด A: pan ภาพในกรอบ
    eI.setPointerCapture(e.pointerId);
    const img=$("img",eI);
    drag={mode:"pan",
          sx:e.clientX,sy:e.clientY,
          opx:parseFloat(img.style.left)||0,
          opy:parseFloat(img.style.top)||0};
  }
});

eI.addEventListener("pointermove",e=>{
  if(!drag||e.pointerId!==_pid)return;
  const img=$("img",eI);
  const k=K();

  if(drag.mode==="pan"){
    // เลื่อนภาพในกรอบ — ภาพทะลุออกนอก (overflow:hidden clip)
    img.style.left=(drag.opx+e.clientX-drag.sx)+"px";
    img.style.top=(drag.opy+e.clientY-drag.sy)+"px";

  }else if(drag.mode==="move-frame"){
    const w=eI.offsetWidth,h=eI.offsetHeight;
    // OBS-style snap to stage edges and center while dragging.
    const rawX=drag.ox+e.clientX-drag.sx,rawY=drag.oy+e.clientY-drag.sy;
    const sn=snapBox(rawX,rawY,w,h);
    eI.style.left=sn.x+"px";
    eI.style.top=sn.y+"px";

  }else if(drag.mode==="resize"){
    const s=drag.side;
    const dx=e.clientX-drag.sx,dy=e.clientY-drag.sy;
    let x=drag.ox,y=drag.oy,w=drag.ow,h=drag.oh;
    if(s.includes("e"))w=Math.max(20,drag.ow+dx);
    if(s.includes("s"))h=Math.max(20,drag.oh+dy);
    if(s.includes("w")){w=Math.max(20,drag.ow-dx);x=drag.ox+drag.ow-w;}
    if(s.includes("n")){h=Math.max(20,drag.oh-dy);y=drag.oy+drag.oh-h;}
    eI.style.left=x+"px";eI.style.top=y+"px";
    eI.style.width=w+"px";eI.style.height=h+"px";
    // ภาพคงที่ ไม่ scale
  }
});

eI.addEventListener("pointerup",async e=>{
  if(!drag||e.pointerId!==_pid)return;
  const k=K();
  const img=$("img",eI);
  const mode=drag.mode;const pdx=drag.opx!==undefined?parseFloat(img.style.left)-drag.opx:0,pdy=drag.opy!==undefined?parseFloat(img.style.top)-drag.opy:0;
  drag=null;_pid=null;clearSnapGuides();
  if(mode==="pan"){
    await run(()=>setCfg({
      image_pan_x:Math.round((S.image_pan_x||0)+pdx/k),
      image_pan_y:Math.round((S.image_pan_y||0)+pdy/k)
    }));
  }else if(mode==="move-frame"){
    await run(()=>setCfg({
      image_x:Math.round(parseFloat(eI.style.left)/k),
      image_y:Math.round(parseFloat(eI.style.top)/k)
    }));
  }else if(mode==="resize"){
    await run(()=>setCfg({
      image_x:Math.round(parseFloat(eI.style.left)/k),
      image_y:Math.round(parseFloat(eI.style.top)/k),
      image_width:Math.round(eI.offsetWidth/k),
      image_height:Math.round(eI.offsetHeight/k)
    }));
  }
});

/* ---------- inspector ---------- */
const num=(id,key)=>$(id)&&$(id).addEventListener("change",e=>{
  const v=Math.round(Number(e.target.value));if(!Number.isFinite(v))return;
  const item=selectedText();
  if(item){
    const field=key==="text_x"?"x":key==="text_y"?"y":"size";
    item[field]=field==="size"?clamp(v,6,400):v;
    e.target.value=item[field];
    renderExtraTexts();renderTextList();queueTextSave(item.id,{[field]:item[field]});
  }else run(()=>setCfg({[key]:v}));
});
num("#inTX","text_x");num("#inTY","text_y");num("#inTSize","text_size");
num("#inIX","image_x");num("#inIY","image_y");num("#inIW","image_width");num("#inIH","image_height");
$("#inIFit")&&$("#inIFit").addEventListener("change",e=>run(()=>setCfg({image_fit:e.target.value,image_pan_x:0,image_pan_y:0})));
$("#inIZoom")&&$("#inIZoom").addEventListener("change",e=>{const v=Number(e.target.value);if(Number.isFinite(v))run(()=>setCfg({image_zoom:Math.max(.05,Math.min(20,v/100))}))});
$("#audioOffset")&&$("#audioOffset").addEventListener("change",e=>{const v=Math.round(Number(e.target.value));if(Number.isFinite(v))run(()=>setCfg({audio_offset_ms:v}))});
(function(){let t=null,z=null;
  eI.addEventListener("wheel",e=>{e.preventDefault();z=Math.max(.05,Math.min(20,(z||S.image_zoom||1)*Math.pow(1.1,-e.deltaY/100)));
    const img=$("img",eI);if(img&&img.naturalWidth){S.image_zoom=z;layout();}
    clearTimeout(t);t=setTimeout(()=>{const v=z;z=null;run(()=>setCfg({image_zoom:v}))},250)},{passive:false});
  eI.addEventListener("dblclick",()=>{const order=["contain","cover","stretch","free"];const n=order[(order.indexOf(S.image_fit||"contain")+1)%4];run(()=>setCfg({image_fit:n,image_pan_x:0,image_pan_y:0}))});
})();

function webNumber(id,key,min=0,max=1920){
  const el=$(id);if(!el)return;
  el.addEventListener("change",async()=>{
    let v=Math.round(Number(el.value));
    if(!Number.isFinite(v))return;
    v=clamp(v,min,max);el.value=v;
    try{await post("/api/web/config",{[key]:v});await poll()}catch(e){notify(e.message,false)}
  });
}
webNumber("#webX","x",-10000,10000);
webNumber("#webY","y",-10000,10000);
webNumber("#webW","width",64,1920);
webNumber("#webH","height",64,1920);
$("#webApply").addEventListener("click",async()=>{
  const url=$("#webUrl").value.trim(),width=clamp(Math.round(Number($("#webW").value)||S.width),64,1920),height=clamp(Math.round(Number($("#webH").value)||S.height),64,1920),x=Math.round(Number($("#webX").value)||0),y=Math.round(Number($("#webY").value)||0);
  try{const r=await post("/api/web/config",{url,width,height,x,y});if(!r?.ok)throw new Error(r?.error||"Web Source apply failed");await poll();layout();notify(url?"Web Source added":"Web Source removed")}catch(e){notify(e.message,false)}
});
$("#webRemove").addEventListener("click",async()=>{
  try{const r=await post("/api/web/config",{url:""});if(!r?.ok)throw new Error(r?.error||"Web Source remove failed");if(S){S.web_url="";S.web_enabled=false;S.layer_order=(S.layer_order||[]).filter(x=>x!=="web")}$("#stageWeb").removeAttribute("src");await poll();layout();notify("Web Source removed")}catch(e){notify(e.message,false)}
});

function queueTextSave(id,patch){
  clearTimeout(textSaveTimers.get(id));const version=(textEditVersions.get(id)||0)+1;textEditVersions.set(id,version);
  const timer=setTimeout(()=>{
    textSaveTimers.delete(id);const prior=textSaveQueues.get(id)||Promise.resolve();const job=prior.catch(()=>{}).then(()=>post("/api/text",{action:"update",id,...patch}));textSaveQueues.set(id,job);
    job.then(r=>{if(textEditVersions.get(id)!==version)return;const fresh=(r.texts||[]).find(x=>x.id===id),local=(S.texts||[]).find(x=>x.id===id);if(fresh&&local)Object.assign(local,fresh);if(Array.isArray(r.layer_order))S.layer_order=r.layer_order;renderExtraTexts();renderTextList()}).catch(e=>notify("Text save failed: "+e.message,false));
  },250);textSaveTimers.set(id,timer);
}
$("#inText").addEventListener("input",e=>{
  const v=e.target.value.slice(0,4000);e.target.value=v;const c=$("#textCount");if(c)c.textContent=v.length+" / 4000";
  const item=selectedText();if(item){item.text=v;textDraft=v;renderExtraTexts();renderTextList();queueTextSave(item.id,{text:v})}
  else if(S?.legacy_text_added){clearTimeout(textSaveTimers.get("__legacy__"));const timer=setTimeout(()=>run(()=>setCfg({text:v})),250);textSaveTimers.set("__legacy__",timer)}else{textDraft=v}
});
$("#inTScroll")?.addEventListener("change",()=>{const item=selectedText();if(!item)return;item.scroll=!!$("#inTScroll").checked;renderExtraTexts();queueTextSave(item.id,{scroll:item.scroll})});
$("#inTScrollSpeed")?.addEventListener("change",()=>{const item=selectedText();if(!item)return;const v=Math.max(20,Math.min(600,Math.round(Number($("#inTScrollSpeed").value)||120)));$("#inTScrollSpeed").value=String(v);item.scroll_speed=v;renderExtraTexts();queueTextSave(item.id,{scroll_speed:v})});
$("#clearImg").addEventListener("click",async()=>{
  await run(()=>post("/api/control",{action:"clear_overlay"}),"ลบ Overlay แล้ว");
  if(Array.isArray(S?.layer_order)&&S.layer_order.includes("image")){
    S.layer_order=S.layer_order.filter(x=>x!=="image");
    await post("/api/layers/reorder",{order:S.layer_order});
    await poll();
  }
});

/* ---------- controls ---------- */
let engineActionBusy=false,cameraActionBusy=false;
async function stopEnginePlayback(){
  if(engineActionBusy)return;
  engineActionBusy=true;render();
  const pv=$("#stageBgVideo");
  if(pv){pv.pause();try{pv.currentTime=0}catch{}}
  try{
    await post("/api/control",{action:"stop"});
    await Promise.all([poll(),loadClipFlow(true)]);
    notify("หยุด Engine + Virtual Audio แล้ว");
  }catch(e){notify("หยุด Engine ไม่สำเร็จ: "+e.message,false)}finally{engineActionBusy=false;await Promise.all([poll(),loadClipFlow(true)])}
}
const CONSOLE_START_KEY="paokra.console.start.v1";
const CONSOLE_OUTPUT_PRESETS=["720x1280","1080x1920","1280x720","1920x1080"];
function readConsoleStartSettings(){
  const currentRes=String((S?.width||720)+"x"+(S?.height||1280));
  const currentFps=String(S?.fps_mode==="source"?"source":(S?.fps||30));
  let saved={};try{saved=JSON.parse(localStorage.getItem(CONSOLE_START_KEY)||"{}")||{}}catch{}
  const defaultMode=(clipFlowConfig.enabled&&clipFlowConfig.open&&(clipFlowConfig.normal||[]).length)?"playlist":"source";
  const mode=["source","playlist"].includes(saved.mode)?saved.mode:defaultMode;
  const resolution=CONSOLE_OUTPUT_PRESETS.includes(saved.resolution)?saved.resolution:(CONSOLE_OUTPUT_PRESETS.includes(currentRes)?currentRes:"720x1280");
  const fps=["24","30","60","source"].includes(String(saved.fps))?String(saved.fps):(["24","30","60","source"].includes(currentFps)?currentFps:"30");
  return {mode,resolution,fps};
}
function hydrateConsoleStartSettings(){
  const p=readConsoleStartSettings();
  const mode=$("#consoleClipMode"),resolution=$("#consoleOutputRes"),fps=$("#consoleOutputFps");
  if(mode)mode.value=p.mode;if(resolution)resolution.value=p.resolution;if(fps)fps.value=p.fps;
  const status=$("#consoleSettingsStatus");if(status)status.textContent="ค่าที่บันทึก: "+(p.mode==="playlist"?"เพลย์ลิสต์คลิป":"เอาต์พุต & ซอร์ส")+" · "+p.resolution.replace("x","×")+" · "+(p.fps==="source"?"Source FPS (Auto)":p.fps+" FPS")+" · ใช้เมื่อกด Start Engine";
}
function saveConsoleStartSettings(){
  const mode=String($("#consoleClipMode")?.value||"source");
  const resolution=String($("#consoleOutputRes")?.value||"720x1280");
  const fps=String($("#consoleOutputFps")?.value||"30");
  if(!["source","playlist"].includes(mode)||!CONSOLE_OUTPUT_PRESETS.includes(resolution)||!["24","30","60","source"].includes(fps))throw new Error("ค่าตั้งค่าโหมด/ความละเอียด/FPS ไม่ถูกต้อง");
  localStorage.setItem(CONSOLE_START_KEY,JSON.stringify({mode,resolution,fps}));
  hydrateConsoleStartSettings();notify("บันทึกค่าตั้งค่า Live Console แล้ว · จะใช้ตอนกด Start Engine");
}
$("#consoleSettingsToggle")?.addEventListener("click",async()=>{
  const panel=$("#consoleSettingsPanel"),button=$("#consoleSettingsToggle");if(!panel||!button)return;
  if(panel.hidden){await loadClipFlow(true,false);hydrateConsoleStartSettings();panel.hidden=false;button.setAttribute("aria-expanded","true")}
  else{panel.hidden=true;button.setAttribute("aria-expanded","false")}
});
$("#consoleSettingsClose")?.addEventListener("click",()=>{const panel=$("#consoleSettingsPanel");if(panel)panel.hidden=true;$("#consoleSettingsToggle")?.setAttribute("aria-expanded","false")});
$("#consoleSettingsSave")?.addEventListener("click",()=>{try{saveConsoleStartSettings()}catch(e){notify(e.message,false)}});
async function startEnginePlayback(){
  if(engineActionBusy)return;
  if(clipFlowAutoMaking){notify("Auto Make Frames กำลังสร้างและตรวจสอบเฟรมอยู่ รอให้จบก่อนเริ่ม Engine",false);return}
  engineActionBusy=true;render();
  try{
    if(!S)await poll();
    let prefs=readConsoleStartSettings();
    if(!S?.running){
      await loadClipFlow(true,false);
      prefs=readConsoleStartSettings();
      if(prefs.mode==="playlist"&&(!clipFlowConfig.open||!(clipFlowConfig.normal||[]).length))throw new Error("โหมด Playlist ต้องตั้งคลิป Open และ Normal ก่อนที่ Settings → Clip Flow");
      const [width,height]=prefs.resolution.split("x").map(Number);
      const outputPatch={width,height,fps_mode:prefs.fps==="source"?"source":"fixed"};
      if(prefs.fps!=="source")outputPatch.fps=Number(prefs.fps);
      const changed=Number(S?.width)!==width||Number(S?.height)!==height||
        String(S?.fps_mode||"fixed")!==outputPatch.fps_mode||
        (prefs.fps!=="source"&&Number(S?.fps)!==Number(prefs.fps));
      const cameraWasActive=!!S?.virtualcam;
      let cameraStopped=false;
      try{
        if(cameraWasActive&&changed){await post("/api/virtualcam",{action:"reconfigure-stop"});cameraStopped=true}
        if(changed)await post("/api/settings",outputPatch);
        if(cameraStopped){const restarted=await post("/api/virtualcam",{action:"start"});if(!restarted?.active)throw new Error(restarted?.error||"Virtual Camera เริ่มใหม่หลังเปลี่ยน Output ไม่สำเร็จ")}
      }catch(e){if(cameraStopped){try{await post("/api/virtualcam",{action:"start"})}catch{}}throw e}
      await poll();
    }
    await post("/api/control",{action:"start",...(!S?.running?{playback_mode:prefs.mode}:{})});
    await Promise.all([poll(),loadClipFlow(true,false)]);
    const pv=$("#stageBgVideo");if(pv&&S?.source_enabled)pv.play().catch(()=>{});
    notify(prefs.mode==="playlist"?"เริ่ม Playlist: Open → Normal แล้ว":"เริ่ม Engine สำหรับ Source ปัจจุบันแล้ว");
  }catch(e){notify("เริ่ม Engine ไม่สำเร็จ: "+e.message,false)}
  finally{engineActionBusy=false;await Promise.all([poll(),loadClipFlow(true,false)]);renderClipFlow()}
}
$("#bStart")?.addEventListener("click",startEnginePlayback);
$("#sceneClipPlay")?.addEventListener("click",()=>S?.running?stopEnginePlayback():startEnginePlayback());
$("#bStop")?.addEventListener("click",stopEnginePlayback);
$("#sceneStop")?.addEventListener("click",stopEnginePlayback);

let LIVE_READY=null;
async function refreshLiveStatus(){
  try{
    LIVE_READY=await api("/api/live/status");
    const studio=$("#studioStatus"),spath=$("#studioPathStatus"),cam=$("#studioCamStatus"),proto=$("#studioProtocolStatus"),tag=$("#liveSetupTag"),modeEl=$("#studioMode");
    if(modeEl&&document.activeElement!==modeEl)modeEl.value=LIVE_READY.studio_mode==="ui"?"ui":"headless";
    if(studio)studio.textContent=(LIVE_READY.studio_running??LIVE_READY.running)?"Running":"Stopped";
    if(spath){
      const p=String(LIVE_READY.studio_path||"");
      spath.textContent=LIVE_READY.studio_path_found?(p.split(/[\\/]/).pop()||"Found"):"Not found";
      spath.title=p||"TikTok LIVE Studio path not detected";
    }
    if(cam)cam.textContent=LIVE_READY.obs_virtual_camera?"Available":"Missing";
    if(proto)proto.textContent=LIVE_READY.protocol_connected?"Connected":"Unavailable";
    if(tag){
      const ready=!!LIVE_READY.virtualcam;
      tag.textContent=ready?(LIVE_READY.studio_running?"READY":"READY · Studio ปิดอยู่ (เปิดเองตอนกดเริ่มไลฟ์)"):"NOT READY";
      tag.className="tag"+(ready?" live":"");
    }
    const liveBtn=$("#bLiveToggle");
    if(liveBtn){
      // Studio is launched by the toggle job itself, so only the virtual camera must be ready.
      const ready=!!LIVE_READY.virtualcam;
      liveBtn.disabled=!ready;
      liveBtn.textContent=(LIVE_READY.live===true || LIVE_READY.live_state==="live" || LIVE_READY.state==="live")?"หยุดไลฟ์":"เริ่มไลฟ์";
      liveBtn.className="btn "+((LIVE_READY.live===true || LIVE_READY.live_state==="live" || LIVE_READY.state==="live")?"danger":"ok");
    }
  }catch{}
}
$("#bLiveToggle")?.addEventListener("click",async()=>{
  const b=$("#bLiveToggle"); if(b)b.disabled=true;
  try{
    const r=await post("/api/live/toggle",{});
    if(r.action==="busy"){notify("คำสั่ง LIVE ก่อนหน้ายังทำงานอยู่ รอสักครู่");return}
    log("INFO","LIVE job #"+r.job+" ส่งคำสั่งแล้ว (ดู Logs › Server + CLI)");
    notify("ส่งคำสั่ง LIVE แล้ว — ดูความคืบหน้าที่ Logs › Server + CLI");
    window.vlogStartPolling&&window.vlogStartPolling();
    for(let i=0;i<1800;i++){
      await new Promise(s=>setTimeout(s,1000));
      const j=await (await fetch("/api/live/job")).json();
      if(!j.running){
        const x=j.result||{};
        if(x.action==="started")notify("เริ่ม LIVE แล้ว");
        else if(x.action==="stopped")notify("หยุด LIVE แล้ว");
        else notify("คำสั่ง LIVE ไม่สำเร็จ: "+(x.error||x.action||"ดู log"),false);
        log(x.ok?"INFO":"ERROR","LIVE job จบ: "+(x.action||"?"));
        break;
      }
    }
    await poll();
  }catch(e){notify(e.message,false)}
  finally{await refreshLiveStatus()}
});

$("#bSetupLive")?.addEventListener("click",async()=>{
  const b=$("#bSetupLive");if(b)b.disabled=true;
  try{
    const r=await post("/api/live/setup",{});
    LIVE_READY=r;
    notify(r.ready
      ?"LIVE Setup พร้อมใช้งาน"
      :("LIVE Studio เปิดแล้ว แต่ยังไม่พร้อม: "+(r.setup_message||"ตรวจสถานะอีกครั้ง")),
      !!r.ready);
    await poll();refreshLiveStatus();
  }catch(e){notify(e.message,false)}
  finally{if(b)b.disabled=false}
});
$("#studioMode")?.addEventListener("change",async e=>{
  const mode=e.target.value==="ui"?"ui":"headless";e.target.disabled=true;
  try{const r=await post("/api/live/studio-mode",{mode});if(!r?.ok)throw new Error(r?.error||"Could not save Studio mode");LIVE_READY={...(LIVE_READY||{}),studio_mode:mode};notify("Next Studio start: "+(mode==="ui"?"visible UI":"headless"));await refreshLiveStatus()}
  catch(err){notify(err.message,false)}finally{e.target.disabled=false}
});
$("#studioRescan")?.addEventListener("click",async e=>{
  const b=e.currentTarget;b.disabled=true;
  try{const r=await api("/api/live/studio-versions?refresh=1");await refreshLiveStatus();notify(r?.count?`Found ${r.count} LIVE Studio path(s)`:"No LIVE Studio path found; select the EXE path in Settings")}
  catch(err){notify(err.message,false)}finally{b.disabled=false}
});
setInterval(()=>{if(view==="console")refreshLiveStatus()},3000);
$("#bEmerg")?.addEventListener("click",()=>run(async()=>{
  if(engineActionBusy||cameraActionBusy)throw new Error("คำสั่ง Engine/Camera กำลังทำงานอยู่ รอสักครู่");
  engineActionBusy=true;cameraActionBusy=true;render();
  try{
    await post("/api/control",{action:"stop"});
    await post("/api/virtualcam",{action:"stop"});
    await Promise.all([poll(),loadClipFlow(true)]);
  }finally{engineActionBusy=false;cameraActionBusy=false;await Promise.all([poll(),loadClipFlow(true)])}
},"Emergency stop"));

async function checkVcam(){
  try{
    const devs=await navigator.mediaDevices.enumerateDevices();
    const cams=devs.filter(d=>d.kind==="videoinput");
    const vc=cams.find(d=>/virtual|obs|paokra/i.test(d.label));
    if(vc)log("INFO","เบราว์เซอร์เจอ: "+vc.label);
    else if(cams.length)log("WARN","เจอกล้อง "+cams.length+" ตัว แต่ไม่มี Virtual Camera");
    else log("WARN","ไม่เจอกล้องเลย — อนุญาต camera permission ก่อน");
  }catch(e){log("WARN","ตรวจกล้องไม่ได้: "+e.message)}
}
$("#bCamStart")?.addEventListener("click",async()=>{
  if(cameraActionBusy)return;
  cameraActionBusy=true;render();
  try{
    const r=await post("/api/virtualcam",{action:"start"});await poll();
    if(r.active){notify("Virtual Camera เริ่มแล้ว");setTimeout(checkVcam,1500)}
    else notify("เริ่มไม่สำเร็จ: "+(r.error||"?"),false);
  }catch(e){notify(e.message,false)}finally{cameraActionBusy=false;await poll()}
});
$("#bCamStop")?.addEventListener("click",async()=>{
  if(cameraActionBusy)return;
  cameraActionBusy=true;render();
  try{await post("/api/virtualcam",{action:"stop"});notify("หยุด Virtual Camera แล้ว")}
  catch(e){notify(e.message,false)}
  finally{cameraActionBusy=false;await poll()}
});
async function changeOutput(patch,label){
  const wasCam=!!S?.virtualcam;
  const previous={};for(const key of Object.keys(patch))previous[key]=S?.[key];
  const oldMode=String(S?.fps_mode||"fixed");
  const oldEffectiveFps=Number(S?.effective_fps||S?.fps)||30;
  const preserveAutoRate=oldMode==="source"&&
    (patch.width!==undefined||patch.height!==undefined)&&patch.fps===undefined&&patch.fps_mode===undefined;
  const startCamera=(pinPreviousAutoRate=false)=>post("/api/virtualcam",(preserveAutoRate||pinPreviousAutoRate)?{action:"start",fps:oldEffectiveFps}:{action:"start"});
  let applied=false;
  try{
    if(wasCam)await post("/api/virtualcam",{action:"reconfigure-stop"});
    await setCfg(patch);applied=true;
    if(wasCam){const r=await startCamera();if(!r.active)throw new Error(r.error||"Virtual Camera restart failed");}
    notify(label);await poll();
  }catch(e){
    let recovery="";
    if(wasCam){
      try{
        if(applied){const oldPatch={};for(const key of Object.keys(previous))if(previous[key]!==undefined)oldPatch[key]=previous[key];if(Object.keys(oldPatch).length)await setCfg(oldPatch);}
        const r=await startCamera(oldMode==="source");if(!r.active)throw new Error(r.error||"Virtual Camera restart failed");
        recovery=" กล้องกลับสู่ค่าก่อนหน้าแล้ว";
      }catch(restoreError){recovery=" กู้คืนกล้องอัตโนมัติไม่สำเร็จ: "+restoreError.message;}
    }
    notify(e.message+recovery,false);await poll();
  }
}
$("#setRes")?.addEventListener("change",e=>{const [w,h]=e.target.value.split("x").map(Number);changeOutput({width:w,height:h},`${w}×${h}`)});
$("#setFps")?.addEventListener("change",e=>{const v=String(e.target.value);changeOutput(v==="source"?{fps_mode:"source"}:{fps:Number(v),fps_mode:"fixed"},v==="source"?"Source FPS Auto":"Fixed "+v+" FPS")});
$("#setLoop")?.addEventListener("change",e=>run(()=>setCfg({loop:e.target.checked})));
$("#setLoopVariation")?.addEventListener("change",e=>run(()=>setCfg({loop_variation_enabled:e.target.checked}),"บันทึก Loop Variation แล้ว"));
$("#setVariationStrength")?.addEventListener("input",e=>{
  const v=clamp(Math.round(Number(e.target.value)||4),1,10);
  $("#variationStrengthValue").textContent=String(v);
});
$("#setVariationStrength")?.addEventListener("change",e=>{
  const v=clamp(Math.round(Number(e.target.value)||4),1,10);
  run(()=>setCfg({loop_variation_strength:v}),"บันทึกระดับ Variation แล้ว");
});
$("#srcSet")?.addEventListener("click",()=>{
  const s=$("#srcInput").value.trim();
  if(!s){notify("ยังไม่ได้ระบุ URL",false);return}
  run(()=>post("/api/source",{source:s}),"ตั้ง Source แล้ว");
});
async function useLocalSource(path){
  path=String(path||"").trim();
  if(!path){notify("ยังไม่ได้ระบุ path ของไฟล์",false);return}
  notify("กำลังเปิด Source จากดิสก์: "+path);
  try{
    const r=await post("/api/source",{source:path});
    await poll();notify("เพิ่ม Source แล้ว: "+(r.source||path));location.hash="#scenes";
  }catch(err){notify("เปิด Source ไม่สำเร็จ: "+err.message,false)}
}
$("#srcLocalUse")?.addEventListener("click",()=>useLocalSource($("#srcLocalPath").value));
$("#srcPick")?.addEventListener("click",async()=>{
  if(window.paokra?.selectVideo){
    try{const path=await window.paokra.selectVideo();if(path){$("#srcLocalPath").value=path;await useLocalSource(path)}}catch(e){notify(e.message,false)}
    return;
  }
  $("#srcFile").click();
});
$("#srcFile")?.addEventListener("change",async e=>{
  const f=e.target.files?.[0];e.target.value="";
  if(!f)return;
  if(!f.type.startsWith("video/")&&!/\.(mp4|mov|mkv|webm|m4v|avi)$/i.test(f.name)){
    notify("ไฟล์นี้ไม่ใช่วิดีโอที่รองรับ",false);return;
  }
  // Browser fallback only: normal browser security prevents exposing a local path.
  const d=new FormData();d.append("file",f);
  notify("กำลังอัปโหลด "+f.name+"...");
  try{await api("/api/media",{method:"POST",body:d});await poll();notify("เพิ่ม Source แล้ว: "+f.name);location.hash="#scenes"}
  catch(err){notify("เพิ่ม Source ไม่สำเร็จ: "+err.message,false)}
});

/* ---------- Open / Normal / Stop clip flow ---------- */
let clipFlowConfig={enabled:true,open:"",normal:[],stop:""};
let clipFlowRuntime={active:false,phase:"idle",current_clip:"",current_role:"",stop_requested:false,normal_queue_remaining:0,played_in_cycle:0,last_error:""};
let clipFlowPendingRole="";
let clipFlowBusy=false;
let clipFlowLoadSeq=0;
let clipFlowActionEpoch=0;
let clipFlowActionBusy=false;
let clipFlowSeekBusy=false;
let clipFlowUiPosition=0;
let clipFlowUiIdentity="";
let clipFlowStatusPollInFlight=false;
let clipFlowUiAt=performance.now();
let clipFlowSeamCache=[];
let clipFlowSeamLoadSeq=0;
let clipFlowAutoMaking=false;

const clipBaseName=p=>String(p||"").split(/[\\/]+/).pop();
function clipFlowMessage(message,bad=false){
  for(const el of [$("#clipFlowStatus"),$("#sceneClipStatus")].filter(Boolean)){
    el.textContent=String(message||"");el.style.color=bad?"var(--danger, #ff6b75)":"";
  }
}
function clipFlowClock(value){
  const seconds=Math.max(0,Number(value)||0),whole=Math.floor(seconds),sec=String(whole%60).padStart(2,"0"),min=String(Math.floor(whole/60)%60).padStart(2,"0"),hours=Math.floor(whole/3600);
  return hours>0?String(hours).padStart(2,"0")+":"+min+":"+sec:String(Math.floor(whole/60)).padStart(2,"0")+":"+sec;
}
function clipFlowTimelineInput(){
  const seek=$("#clipFlowSeek"),time=$("#clipFlowTime");if(!seek||!time)return;
  const value=Number(seek.value)||0,duration=Number(seek.max)||0;
  time.textContent=clipFlowClock(value)+" / "+(duration>0?clipFlowClock(duration):"--:--");
}
function clipFlowTimelineDuration(){
  const sourceDuration=Math.max(0,Number(S?.source_duration_seconds)||0);
  if(!clipFlowRuntime.active)return sourceDuration;
  const flowDuration=Math.max(0,Number(clipFlowRuntime.duration_seconds)||0);
  if(flowDuration>0)return flowDuration;
  const runtimePath=clipFlowPathKey(clipFlowRuntime.current_clip||"");
  const sourcePath=clipFlowPathKey(S?.source||"");
  return runtimePath&&runtimePath===sourcePath?sourceDuration:0;
}
function clipFlowUpdateClockAnchor(runtime){
  const now=performance.now(),current=runtime||{};
  const identity=[current.active?1:0,current.phase||"idle",clipFlowPathKey(current.current_clip||""),Number(current.changed_at)||0].join("|");
  const position=Math.max(0,Number(current.position_seconds)||0);
  if(identity!==clipFlowUiIdentity){
    // A new phase/source generation starts a fresh per-clip clock, including
    // replaying the exact same filename. Never inherit the previous clip's end.
    clipFlowUiIdentity=identity;clipFlowUiPosition=position;clipFlowUiAt=now;return;
  }
  const projected=clipFlowUiPosition+Math.max(0,now-clipFlowUiAt)/1000;
  // Runtime polling may correct the UI clock forward only. A stale response
  // must not rewind it. Seek/restart changes the identity and resets the anchor.
  if(position>projected){
    clipFlowUiPosition=position;clipFlowUiAt=now;
  }
}
function clipFlowDisplayPosition(){
  const now=performance.now(),duration=clipFlowTimelineDuration();
  if(!S?.running||duration<=0)return 0;
  if(!clipFlowRuntime.active){
    let position=Math.max(0,Number(S.source_position_seconds)||0)+Math.max(0,now-stateSnapshotAt)/1000;
    return S.loop?position%duration:Math.min(duration,position);
  }
  // The runtime anchor is authoritative for Clip Flow. /api/state can still
  // describe the previous source briefly during a handoff; do not let its stale
  // position pin the new clip at EOF.
  const local=clipFlowUiPosition+Math.max(0,now-clipFlowUiAt)/1000;
  return Math.min(duration,Math.max(0,local));
}
function renderClipFlow(){
  const en=$("#clipFlowEnabled");if(en&&document.activeElement!==en)en.checked=!!clipFlowConfig.enabled;
  const open=$("#clipFlowOpenName");if(open)open.textContent=clipFlowConfig.open||"ยังไม่ได้เลือกคลิป Open";
  const stop=$("#clipFlowStopName");if(stop)stop.textContent=clipFlowConfig.stop||"ยังไม่ได้เลือกคลิป Stop";
  const list=$("#clipFlowNormalList");
  if(list){
    list.replaceChildren();
    const rows=clipFlowConfig.normal||[];
    if(!rows.length){const empty=document.createElement("div");empty.className="muted small";empty.textContent="ยังไม่ได้เพิ่มคลิป Normal";list.appendChild(empty)}
    rows.forEach((path,index)=>{
      const row=document.createElement("div");row.className="row between";row.style.cssText="gap:8px;align-items:center;flex-wrap:wrap;padding:6px 8px;border:1px solid var(--border,rgba(128,128,128,.22));border-radius:8px";
      const name=document.createElement("span");name.className="small";name.style.cssText="flex:1;min-width:180px;overflow-wrap:anywhere";name.textContent=(index+1)+". "+clipBaseName(path);name.title=path;
      const remove=document.createElement("button");remove.className="btn ghost";remove.type="button";remove.textContent="ลบ";remove.dataset.clipRemove=String(index);remove.title=path;
      row.append(name,remove);list.appendChild(row);
    });
  }
  const phase=clipFlowRuntime.phase||"idle";
  const labels={open:"กำลังเล่น Open",normal:"กำลังเล่น Normal",stop:"กำลังเล่น Stop",finished:"จบ Flow",error:"Flow มีข้อผิดพลาด",idle:"ยังไม่ได้เริ่ม"};
  const rt=$("#clipFlowRuntime");
  if(rt){
    const current=clipFlowRuntime.current_clip?clipBaseName(clipFlowRuntime.current_clip):"-";
    rt.textContent=(clipFlowRuntime.active?"RUNNING":"STOPPED")+" · "+(labels[phase]||phase)+" · current: "+current+
      " · Normal queue remaining: "+(Number(clipFlowRuntime.normal_queue_remaining)||0)+
      " · played this cycle: "+(Number(clipFlowRuntime.played_in_cycle)||0)+(clipFlowRuntime.stop_requested?" · Stop queued: current clip will finish first":"");
  }
  if(clipFlowRuntime.last_error)clipFlowMessage(clipFlowRuntime.last_error,true);
  const timeline=$("#clipFlowSeek"),time=$("#clipFlowTime"),remaining=$("#clipFlowRemaining"),fpsInfo=$("#clipFlowFps");
  const duration=clipFlowTimelineDuration(),position=clipFlowDisplayPosition();
  if(timeline){timeline.max=String(duration);timeline.disabled=!(S?.running&&duration>0)||clipFlowSeekBusy||clipFlowActionBusy;if(document.activeElement!==timeline)timeline.value=String(position);}
  if(time&&document.activeElement!==timeline)time.textContent=clipFlowClock(position)+" / "+(duration>0?clipFlowClock(duration):"--:--");
  if(remaining)remaining.textContent="เหลือ "+(duration>0?clipFlowClock(Math.max(0,duration-position)):"--:--");
  if(fpsInfo)fpsInfo.textContent="Source "+(Number(clipFlowRuntime.active?clipFlowRuntime.source_fps:S?.source_fps)>0?Number(clipFlowRuntime.active?clipFlowRuntime.source_fps:S.source_fps).toFixed(2):"—")+" FPS · Output "+(Number(S?.effective_fps||S?.fps)>0?(S.effective_fps||S.fps):"—")+" FPS";

  renderClipFlowSeams();

  // Keep the Flow controls usable while a long Stop clip is playing. Starting
  // Open again is an intentional replacement action, not a request to wait for
  // the closing clip (which may be many minutes long).
  const startBtn=$("#clipFlowStart"),stopBtn=$("#clipFlowStopPreview"),refreshBtn=$("#clipFlowRefresh");
  const busy=!!clipFlowActionBusy;
  if(startBtn){
    const restarting=!!(clipFlowRuntime.active&&(clipFlowRuntime.phase==="stop"||clipFlowRuntime.stop_requested));
    startBtn.disabled=busy;
    startBtn.textContent=restarting?"Restart Open → Normal":"เริ่ม Open → Normal";
    startBtn.title=restarting?"หยุดคลิป Stop ปัจจุบันแล้วเริ่ม Open ใหม่":"เริ่มหรือเริ่มใหม่จากคลิป Open";
    startBtn.setAttribute("aria-busy",busy?"true":"false");
  }
  if(stopBtn){
     stopBtn.disabled=busy||!clipFlowConfig.stop||!!(clipFlowRuntime.active&&(clipFlowRuntime.phase==="stop"||clipFlowRuntime.stop_requested));
     stopBtn.title=clipFlowRuntime.stop_requested?"Stop is queued; the current clip will finish first":(clipFlowRuntime.active&&clipFlowRuntime.phase==="stop"?"Stop clip is already playing":"Finish current clip, then play Stop");
     stopBtn.setAttribute("aria-label",stopBtn.title);
   }
  if(refreshBtn)refreshBtn.disabled=busy;
    // Scene Builder transport is a real single Engine toggle rendered by render().
    // Clip Flow polling must not overwrite its label or disable Stop while running.
}
$("#clipFlowSeek")?.addEventListener("input",clipFlowTimelineInput);
$("#clipFlowSeek")?.addEventListener("change",async e=>{
  const position=Number(e.target.value);if(!Number.isFinite(position)||clipFlowSeekBusy)return;
  clipFlowSeekBusy=true;renderClipFlow();
  try{
    const r=clipFlowRuntime.active
      ? await post("/api/clip-flow/seek",{position_seconds:position})
      : await post("/api/source/seek",{position_seconds:position});
    if(!r?.ok)throw new Error(r?.error||"เลื่อนคลิปไม่สำเร็จ");
    if(r.runtime){clipFlowRuntime=r.runtime;clipFlowUiIdentity="";clipFlowUpdateClockAnchor(clipFlowRuntime);}
    else {await poll();clipFlowUiPosition=Number(r.position_seconds)||position;clipFlowUiAt=performance.now();}
    renderClipFlow();clipFlowMessage("เลื่อนไป "+clipFlowClock(position)+" แล้ว");
    await loadClipFlow(true);
  }catch(err){clipFlowMessage("เลื่อนคลิปไม่สำเร็จ: "+err.message,true);await loadClipFlow(true)}
  finally{
    const slider=$("#clipFlowSeek");if(slider)slider.value=String(clipFlowDisplayPosition());
    clipFlowSeekBusy=false;clipFlowTimelineInput();renderClipFlow();
  }
});
setInterval(()=>{
  if((view!=="scenes"&&view!=="settings")||clipFlowSeekBusy)return;
  const slider=$("#clipFlowSeek"),time=$("#clipFlowTime"),remaining=$("#clipFlowRemaining");
  const duration=clipFlowTimelineDuration(),position=clipFlowDisplayPosition();
  if(slider&&document.activeElement!==slider&&S?.running&&duration>0)slider.value=String(position);
  if(time&&document.activeElement!==slider)time.textContent=clipFlowClock(position)+" / "+(duration>0?clipFlowClock(duration):"--:--");
  if(remaining)remaining.textContent="เหลือ "+(duration>0?clipFlowClock(Math.max(0,duration-position)):"--:--");
},250);
async function loadClipFlow(quiet=true,refreshSeams=true){
  const requestSeq=++clipFlowLoadSeq;
  const actionEpoch=clipFlowActionEpoch;
  try{
    const r=await api("/api/clip-flow");
    // GET requests may complete out of order. Never let a refresh that began
    // before Start/Stop repaint the UI with an older runtime snapshot.
    if(requestSeq!==clipFlowLoadSeq||actionEpoch!==clipFlowActionEpoch)return r;
    clipFlowConfig={enabled:r.config?.enabled!==false,open:String(r.config?.open||""),normal:Array.isArray(r.config?.normal)?r.config.normal:[],stop:String(r.config?.stop||"")};
    clipFlowRuntime=r.runtime||clipFlowRuntime;clipFlowUpdateClockAnchor(clipFlowRuntime);renderClipFlow();
    if(refreshSeams)void loadClipFlowSeams();
    if(!quiet)clipFlowMessage("โหลด Clip Flow status แล้ว");
    return r;
  }catch(e){
    if(requestSeq===clipFlowLoadSeq&&actionEpoch===clipFlowActionEpoch){
      clipFlowMessage("อ่าน Clip Flow ไม่สำเร็จ: "+e.message,true);
    }
    return null;
  }
}
async function saveClipFlow(patch={},message="บันทึก Clip Flow แล้ว"){
  if(clipFlowBusy)return;
  clipFlowBusy=true;
  clipFlowActionEpoch++;
  clipFlowLoadSeq++;
  try{
    const next={...clipFlowConfig,...patch};
    const r=await post("/api/clip-flow/config",next);
    clipFlowConfig={enabled:r.config?.enabled!==false,open:String(r.config?.open||""),normal:Array.isArray(r.config?.normal)?r.config.normal:[],stop:String(r.config?.stop||"")};
    clipFlowRuntime=r.runtime||clipFlowRuntime;renderClipFlow();clipFlowMessage(message);return r;
  }catch(e){clipFlowMessage("บันทึก Clip Flow ไม่สำเร็จ: "+e.message,true);notify(e.message,false);throw e}
  finally{clipFlowBusy=false}
}
async function chooseClipFiles(role){
  if(window.paokra?.selectVideos){
    try{
      const paths=await window.paokra.selectVideos({role});
      const picked=Array.isArray(paths)?paths.filter(Boolean):[];if(!picked.length)return;
      if(role==="normal"){
        const combined=[...(clipFlowConfig.normal||[]),...picked],seen=new Set();
        const unique=combined.filter(p=>{const k=String(p).toLowerCase();if(seen.has(k))return false;seen.add(k);return true});
        await saveClipFlow({normal:unique.slice(0,100)},"เพิ่มคลิป Normal "+picked.length+" ไฟล์แล้ว");
      }else await saveClipFlow({[role]:picked[0]},role==="open"?"ตั้งค่า Open แล้ว":"ตั้งค่า Stop แล้ว");
      return;
    }catch(e){clipFlowMessage("เลือกไฟล์ไม่สำเร็จ: "+e.message,true);return}
  }
  clipFlowPendingRole=role;const input=$("#clipFlowFileInput");
  if(!input){clipFlowMessage("File picker ไม่พร้อมใช้งาน",true);return}
  input.multiple=role==="normal";input.value="";input.click();
}
async function uploadClipFallback(files,role){
  const paths=[];
  for(const file of files){
    if(!file.type.startsWith("video/")&&!/\.(mp4|mov|mkv|webm|m4v|avi)$/i.test(file.name)){clipFlowMessage("ไฟล์ไม่ใช่วิดีโอที่รองรับ: "+file.name,true);continue}
    const form=new FormData();form.append("file",file);
    // Upload queued Open/Normal/Stop clips without replacing the live Source.
    // /api/media defaults to activate_source=true for the normal Source picker.
    form.append("activate_source","false");
    const r=await api("/api/media",{method:"POST",body:form});
    if(!r?.ok||!r.source)throw new Error(r?.error||("Upload did not return a media path: "+file.name));
    paths.push(String(r.source));
  }
  if(!paths.length)return;
  if(role==="normal")await saveClipFlow({normal:[...(clipFlowConfig.normal||[]),...paths].slice(0,100)},"เพิ่มคลิป Normal แล้ว");
  else await saveClipFlow({[role]:paths[0]},role==="open"?"ตั้งค่า Open แล้ว":"ตั้งค่า Stop แล้ว");
}
function clipFlowPathKey(value){
  return String(value||"").trim().replace(/\//g,"\\").replace(/\\+/g,"\\").toLowerCase();
}
function clipFlowPairKey(fromPath,toPath){return clipFlowPathKey(fromPath)+"\u0000"+clipFlowPathKey(toPath)}
function clipFlowPairs(paths){
  const unique=[],seen=new Set();
  for(const raw of (Array.isArray(paths)?paths:[])){
    const p=String(raw||"").trim(),k=clipFlowPathKey(p);
    if(p&&k&&!seen.has(k)){seen.add(k);unique.push(p)}
  }
  const pairs=[];
  for(const from of unique)for(const to of unique)if(clipFlowPathKey(from)!==clipFlowPathKey(to))pairs.push({from,to});
  return pairs;
}
function clipFlowPlayablePairs(config=clipFlowConfig){
  const unique=(values)=>{const out=[],seen=new Set();for(const raw of values){const p=String(raw||"").trim(),k=clipFlowPathKey(p);if(p&&k&&!seen.has(k)){seen.add(k);out.push(p)}}return out};
  const normals=unique(config.normal||[]),open=String(config.open||"").trim(),stop=String(config.stop||"").trim();
  const pairs=[],seen=new Set();
  const add=(from,to,fromRole,toRole)=>{const key=clipFlowPairKey(from,to);if(!from||!to||clipFlowPathKey(from)===clipFlowPathKey(to)||seen.has(key))return;seen.add(key);pairs.push({from,to,fromRole,toRole})};
  if(open)for(const to of normals)add(open,to,"open","normal");
  if(open&&stop)add(open,stop,"open","stop");
  for(const from of normals)for(const to of normals)add(from,to,"normal","normal");
  if(stop)for(const from of normals)add(from,stop,"normal","stop");
  return pairs;
}
function clipFlowHasCachedSeam(fromPath,toPath){
  const key=clipFlowPairKey(fromPath,toPath);
  return clipFlowSeamCache.some(s=>clipFlowPairKey(s.from_path,s.to_path)===key&&s.available===true&&s.compatible===true);
}
async function loadClipFlowSeams(options={}){
  const requestSeq=++clipFlowSeamLoadSeq;
  const attempts=Math.max(1,Math.min(5,Number(options.attempts)||3));
  let lastError=null;
  for(let attempt=0;attempt<attempts;attempt++){
    try{
      const r=await api("/api/ai-seam/seams");
      // Older list requests can finish after a newer request and repaint a stale
      // manifest, making completed generated bridges look like they disappeared.
      if(requestSeq!==clipFlowSeamLoadSeq)return r;
      if(Array.isArray(r.seams)){clipFlowSeamCache=r.seams;renderClipFlowSeams()}
      return r;
    }catch(e){
      lastError=e;
      if(requestSeq!==clipFlowSeamLoadSeq)return null;
      if(attempt+1<attempts)await new Promise(resolve=>setTimeout(resolve,180*(attempt+1)));
    }
  }
  if(requestSeq===clipFlowSeamLoadSeq){
    const status=$("#clipFlowSeamStatus");
    if(status)status.textContent="อ่านสถานะเฟรมคั่นไม่สำเร็จ: "+String(lastError?.message||lastError||"unknown error");
  }
  return null;
}
async function waitForClipFlowSeam(fromPath,toPath){
  for(let attempt=0;attempt<5;attempt++){
    const result=await loadClipFlowSeams({attempts:2});
    if(result&&clipFlowHasCachedSeam(fromPath,toPath))return true;
    if(attempt<4)await new Promise(resolve=>setTimeout(resolve,250*(attempt+1)));
  }
  return false;
}
function renderClipFlowSeams(){
  const list=$("#clipFlowSeamList"),status=$("#clipFlowSeamStatus"),button=$("#clipFlowAutoMake");
  if(!list||!status||!button)return;
  const pairs=clipFlowPlayablePairs(clipFlowConfig);
  const ready=pairs.filter(p=>clipFlowHasCachedSeam(p.from,p.to)).length,missing=pairs.length-ready;
  button.disabled=clipFlowAutoMaking||pairs.length===0||!!clipFlowRuntime.active;
  button.textContent=clipFlowAutoMaking?"กำลังสร้างเฟรม…":(missing?"Auto Make Frames · "+missing:"Auto Make Frames · พร้อมแล้ว");
  if(!pairs.length){
    status.textContent="เพิ่มคลิป Normal และตั้งค่า Open/Stop ที่ต้องใช้ก่อนสร้างคู่เฟรมคั่น";
    list.replaceChildren();return;
  }
  status.textContent="เฟรมคั่นพร้อมใช้ "+ready+"/"+pairs.length+" คู่ทิศทาง · ต้องสร้างเพิ่ม "+missing+" คู่ · ระบบจะแทรกให้อัตโนมัติโดยไม่เพิ่มลงใน Normal";
  list.replaceChildren();
  const shown=pairs.slice(0,20);
  for(const pair of shown){
    const row=document.createElement("div");row.className="row between";
    row.style.cssText="gap:8px;align-items:center;flex-wrap:wrap;padding:6px 8px;border:1px solid var(--border,rgba(128,128,128,.22));border-radius:8px";
    const name=document.createElement("span");name.className="small";name.style.cssText="flex:1;min-width:180px;overflow-wrap:anywhere";
    name.textContent=(pair.fromRole==="open"&&pair.toRole==="stop"?"OPEN → STOP · ":pair.fromRole==="open"?"OPEN → NORMAL · ":pair.toRole==="stop"?"NORMAL → STOP · ":"NORMAL → NORMAL · ")+clipBaseName(pair.from)+" → "+clipBaseName(pair.to);
    const badge=document.createElement("span");badge.className="tag"+(clipFlowHasCachedSeam(pair.from,pair.to)?" live":"");
    badge.textContent=clipFlowHasCachedSeam(pair.from,pair.to)?"พร้อมใช้":clipFlowSeamCache.some(s=>clipFlowPairKey(s.from_path,s.to_path)===clipFlowPairKey(pair.from,pair.to)&&s.available===true)?"ต้องสร้างใหม่ (Output/Settings เปลี่ยน)":"ยังไม่มีเฟรม";
    row.append(name,badge);list.appendChild(row);
  }
  if(pairs.length>shown.length){
    const more=document.createElement("div");more.className="muted small";more.textContent="แสดง "+shown.length+" จากทั้งหมด "+pairs.length+" คู่";list.appendChild(more);
  }
}
async function clipFlowAutoMakeFrames(){
  if(clipFlowAutoMaking)return;
  if(clipFlowRuntime.active){clipFlowMessage("กรุณาหยุด Clip Flow ก่อนสร้าง AI Seam",true);return}
  clipFlowAutoMaking=true;renderClipFlowSeams();
  const button=$("#clipFlowAutoMake"),status=$("#clipFlowSeamStatus");
  const setStatus=m=>{if(status)status.textContent=String(m||"")};
  let batchToken="",lastBatchHeartbeat=Date.now();
  try{
    const flow=await api("/api/clip-flow");
    clipFlowConfig={enabled:flow.config?.enabled!==false,open:String(flow.config?.open||""),normal:Array.isArray(flow.config?.normal)?flow.config.normal:[],stop:String(flow.config?.stop||"")};
    clipFlowRuntime=flow.runtime||clipFlowRuntime;renderClipFlowSeams();
    const allPairs=clipFlowPlayablePairs(clipFlowConfig);
    if(!allPairs.length)throw new Error("ตั้งค่าอย่างน้อยหนึ่งคลิป Normal และกำหนด Open/Stop ตามการใช้งานจริงก่อน Auto Make Frames");
    if(allPairs.length>250){const ok=confirm("ต้องสร้างเฟรมคั่น "+allPairs.length+" คู่แบบมีทิศทาง งานอาจใช้เวลานาน แต่จะไม่ตัดคู่ที่ Playlist สามารถเล่นได้ ต้องการทำต่อหรือไม่?");if(!ok){setStatus("ยกเลิกโดยผู้ใช้ — ยังไม่ได้สร้างเฟรม");return}}
    const health=await api("/api/ai-seam/status");
    if(health.clip_flow_active||health.engine_running||health.virtualcam_active||health.generation_busy)throw new Error("ปิด Engine และ Virtual Camera แล้วหยุด Clip Flow ก่อน Auto Make Frames เพื่อไม่ให้กระทบ LIVE/FPS");
    const reservation=await post("/api/ai-seam/batch-lock",{action:"acquire"});
    batchToken=String(reservation.token||"");
    if(!batchToken)throw new Error(reservation.error||"ไม่ได้รับสิทธิ์ล็อก Engine ระหว่าง Auto Make Frames");
    lastBatchHeartbeat=Date.now();
    setStatus("กำลังซิงก์ Open / Normal / Stop กับ AI Seam Studio…");
    const paths=[],seen=new Set();
    for(const raw of [clipFlowConfig.open,...clipFlowConfig.normal,clipFlowConfig.stop]){const p=String(raw||"").trim(),k=clipFlowPathKey(p);if(p&&k&&!seen.has(k)){seen.add(k);paths.push(p)}}
    const synced=await post("/api/ai-seam/clips",{paths,batch_token:batchToken});
    if(synced.errors?.length)throw new Error("ซิงก์คลิปไม่ครบ: "+synced.errors.slice(0,3).map(x=>(x.path||"")+" "+(x.error||"")).join("; "));
    const results=await Promise.all([api("/api/ai-seam/clips"),api("/api/ai-seam/seams"),api("/api/ai-seam/status")]);
    const clipsResult=results[0],seamsResult=results[1],statusResult=results[2];
    const keyToClip=new Map((clipsResult.clips||[]).map(c=>[clipFlowPathKey(c.path),c]));
    const unregistered=paths.filter(p=>!keyToClip.has(clipFlowPathKey(p)));
    if(unregistered.length)throw new Error("AI Seam ไม่พบคลิป: "+unregistered.slice(0,4).map(clipBaseName).join(", "));
    clipFlowSeamCache=Array.isArray(seamsResult.seams)?seamsResult.seams:[];
    const mappedPairs=allPairs.map(p=>Object.assign({},p,{fromClip:keyToClip.get(clipFlowPathKey(p.from)),toClip:keyToClip.get(clipFlowPathKey(p.to))}));
    const unmapped=mappedPairs.filter(p=>!p.fromClip||!p.toClip);
    if(unmapped.length)throw new Error("แมปคู่คลิปไม่สำเร็จ: "+unmapped.slice(0,4).map(p=>clipBaseName(p.from)+" → "+clipBaseName(p.to)).join("; "));
    const todo=mappedPairs.filter(p=>!clipFlowHasCachedSeam(p.from,p.to));
    renderClipFlowSeams();
    if(!todo.length){
      if(statusResult.settings?.enabled!==true){const enabled=await post("/api/ai-seam/settings",{enabled:true,batch_token:batchToken});if(!enabled.ok||enabled.settings?.enabled!==true)throw new Error(enabled.error||"เปิด AI Seam ไม่สำเร็จ")}
      setStatus("เฟรมคั่นครบแล้ว "+mappedPairs.length+"/"+mappedPairs.length+" คู่ที่ Playlist ใช้งานได้จริง — Open/Normal/Stop ครอบคลุมครบ");
      notify("AI Seam: เฟรมคั่นครบทุกเส้นทางของ Playlist แล้ว");return;
    }
    setStatus("เริ่ม Auto Make Frames · สร้าง "+todo.length+" คู่ที่ขาดจาก "+mappedPairs.length+" คู่ที่ใช้งานได้จริง");
    for(let i=0;i<todo.length;i++){
      const pair=todo[i],edge=pair.fromRole==="open"&&pair.toRole==="stop"?"OPEN → STOP":pair.fromRole==="open"?"OPEN → NORMAL":pair.toRole==="stop"?"NORMAL → STOP":"NORMAL → NORMAL";
      setStatus("กำลังสร้าง "+(i+1)+"/"+todo.length+": "+edge+" · "+clipBaseName(pair.from)+" → "+clipBaseName(pair.to)+" · กำลังส่งงาน…");
      const started=await post("/api/ai-seam/generate",{from_id:pair.fromClip.id,to_id:pair.toClip.id,force:true,compute:statusResult.settings?.compute||"auto",batch_token:batchToken});
      const jobId=started.job?.id;
      if(!jobId)throw new Error(started.error||("เริ่มสร้าง "+clipBaseName(pair.from)+" → "+clipBaseName(pair.to)+" ไม่สำเร็จ"));
      let complete=false,lastMessage="",pollFailures=0;
      const deadline=Date.now()+60*60*1000;
      while(Date.now()<deadline){
        await new Promise(resolve=>setTimeout(resolve,900));
        if(batchToken&&Date.now()-lastBatchHeartbeat>=10000){
          await post("/api/ai-seam/batch-lock",{action:"heartbeat",token:batchToken});
          lastBatchHeartbeat=Date.now();
        }
        let current;
        try{current=await api("/api/ai-seam/jobs/"+encodeURIComponent(jobId));pollFailures=0}
        catch(pollError){
          pollFailures++;
          if(pollFailures>=5)throw new Error("อ่านสถานะงานสร้างเฟรมไม่ได้ต่อเนื่อง 5 ครั้ง: "+pollError.message);
          setStatus("กำลังสร้าง "+(i+1)+"/"+todo.length+": "+clipBaseName(pair.from)+" → "+clipBaseName(pair.to)+" · เชื่อมต่อ log อีกครั้ง ("+pollFailures+"/5)");
          continue;
        }
        const job=current.job||{};
        const progress=Math.max(0,Math.min(100,Number(job.progress)||0));
        const message=String(job.message||job.phase||"กำลังประมวลผล");
        if(message!==lastMessage){lastMessage=message;setStatus("กำลังสร้าง "+(i+1)+"/"+todo.length+": "+edge+" · "+clipBaseName(pair.from)+" → "+clipBaseName(pair.to)+" · "+progress+"% · "+message)}
        if(job.status==="complete"){complete=true;break}
        if(job.status==="error")throw new Error(clipBaseName(pair.from)+" → "+clipBaseName(pair.to)+": "+(job.error||job.message||"สร้างเฟรมไม่สำเร็จ"));
      }
      if(!complete)throw new Error("หมดเวลารอเฟรม "+clipBaseName(pair.from)+" → "+clipBaseName(pair.to)+" (60 นาที)");
      if(!await waitForClipFlowSeam(pair.from,pair.to))throw new Error("งานสร้างเสร็จแล้ว แต่ตรวจ cache ที่ยืนยันได้ไม่พบหลังลองอ่านซ้ำ 5 รอบ: "+clipBaseName(pair.from)+" → "+clipBaseName(pair.to)+" — กรุณาดู AI Seam Jobs/Logs ก่อนลองซ้ำ");
    }
    await loadClipFlowSeams({attempts:4});
    const finalPairs=clipFlowPlayablePairs(clipFlowConfig),finalReady=finalPairs.filter(p=>clipFlowHasCachedSeam(p.from,p.to)).length;
    if(finalReady!==finalPairs.length)throw new Error("ตรวจยืนยันเฟรมคั่นไม่ครบ: "+finalReady+"/"+finalPairs.length+" คู่");
    if(statusResult.settings?.enabled!==true){const enabled=await post("/api/ai-seam/settings",{enabled:true,batch_token:batchToken});if(!enabled.ok||enabled.settings?.enabled!==true)throw new Error(enabled.error||"สร้างเฟรมครบแล้วแต่เปิดใช้ AI Seam ไม่สำเร็จ")}
    await aiSeamLoad(true);
    setStatus("เสร็จแล้ว: เฟรมคั่นพร้อมใช้ "+finalReady+"/"+finalPairs.length+" คู่ · Open → Normal/Stop, Normal ↔ Normal และ Normal → Stop ครบ");
    notify("Auto Make Frames เสร็จแล้ว "+finalReady+"/"+finalPairs.length+" คู่ที่ Playlist เล่นได้จริง");
  }catch(e){
    setStatus("หยุดสร้างเฟรมคั่น: "+e.message);
    clipFlowMessage("Auto Make Frames ไม่สำเร็จ: "+e.message,true);
    notify(e.message,false);
  }finally{
    if(batchToken){
      try{await post("/api/ai-seam/batch-lock",{action:"release",token:batchToken})}
      catch(releaseError){console.warn("Could not release Auto Make Frames reservation; the server lease will expire safely.",releaseError)}
      batchToken="";
    }
    clipFlowAutoMaking=false;
    if(button)button.disabled=false;
    renderClipFlowSeams();
  }
}
$("#clipFlowAutoMake")?.addEventListener("click",clipFlowAutoMakeFrames);
$("#clipFlowPickOpen")?.addEventListener("click",()=>chooseClipFiles("open"));
$("#clipFlowAddNormal")?.addEventListener("click",()=>chooseClipFiles("normal"));
$("#clipFlowPickStop")?.addEventListener("click",()=>chooseClipFiles("stop"));
$("#clipFlowClearOpen")?.addEventListener("click",()=>saveClipFlow({open:""},"ล้างคลิป Open แล้ว"));
$("#clipFlowClearNormal")?.addEventListener("click",()=>saveClipFlow({normal:[]},"ล้างรายการ Normal แล้ว"));
$("#clipFlowClearStop")?.addEventListener("click",()=>saveClipFlow({stop:""},"ล้างคลิป Stop แล้ว"));

$("#clipFlowNormalList")?.addEventListener("click",e=>{
  const b=e.target.closest("button[data-clip-remove]");if(!b)return;
  const index=Number(b.dataset.clipRemove),list=[...(clipFlowConfig.normal||[])];
  if(Number.isInteger(index)&&index>=0&&index<list.length){list.splice(index,1);saveClipFlow({normal:list},"ลบคลิป Normal แล้ว")}
});
$("#clipFlowFileInput")?.addEventListener("change",async e=>{
  const files=Array.from(e.target.files||[]),role=clipFlowPendingRole;e.target.value="";clipFlowPendingRole="";
  if(!files.length)return;
  try{await uploadClipFallback(files,role)}catch(err){clipFlowMessage("เพิ่มไฟล์ไม่สำเร็จ: "+err.message,true)}
});
$("#clipFlowRefresh")?.addEventListener("click",()=>loadClipFlow(false));

$("#clipFlowStart")?.addEventListener("click",async()=>{
  if(clipFlowActionBusy)return;
  clipFlowActionBusy=true;
  clipFlowActionEpoch++;
  clipFlowLoadSeq++; // invalidate a GET that began before this Start request
  renderClipFlow();
  try{
    if(!clipFlowConfig.enabled)await saveClipFlow({enabled:true},"เปิด Auto Clip Flow แล้ว");
    const r=await post("/api/clip-flow/start",{});
    if(!r?.ok)throw new Error(r?.error||"Backend ปฏิเสธคำสั่งเริ่ม Flow");
    // Reflect the accepted transition immediately; the following ordered GET
    // will fill in the full queue/status. Do not keep showing the previous Stop.
    clipFlowUiIdentity="";clipFlowUiPosition=0;clipFlowUiAt=performance.now();
    clipFlowRuntime={...clipFlowRuntime,active:true,phase:"open",current_clip:r.source||clipFlowConfig.open,current_role:"open",
      position_seconds:0,duration_seconds:0,remaining_seconds:0,stop_requested:false,last_error:"",reason:"settings_start",changed_at:0};
    clipFlowUpdateClockAnchor(clipFlowRuntime);
    renderClipFlow();
    clipFlowMessage("เริ่ม Flow แล้ว: Open → สุ่ม Normal แบบไม่ซ้ำในรอบ");
    notify("เริ่ม Open → Normal แล้ว");
    await loadClipFlow(true);
  }catch(e){
    clipFlowMessage("เริ่ม Flow ไม่สำเร็จ: "+e.message,true);
    notify(e.message,false);
    await loadClipFlow(true);
  }finally{
    clipFlowActionBusy=false;
    renderClipFlow();
  }
});

$("#clipFlowStopPreview")?.addEventListener("click",async()=>{
  if(clipFlowActionBusy)return;
  clipFlowActionBusy=true;
  clipFlowActionEpoch++;
  clipFlowLoadSeq++;
  renderClipFlow();
  try{
    const r=await post("/api/clip-flow/play-stop",{});
    if(!r?.ok)throw new Error(r?.error||"Backend rejected the Playlist Stop request");
    if(r.stop_pending){
      // Keep the real Open/Normal phase and source visible while the decoder
      // finishes its current clip. Do not optimistically jump the UI to Stop.
      clipFlowRuntime={...clipFlowRuntime,active:true,stop_requested:true,last_error:"",reason:r.reason||"settings_stop_preview"};
      clipFlowMessage("Stop queued — the current clip will finish before Stop plays.");
      notify("Stop queued: finishing the current clip first");
    }else{
      clipFlowUiIdentity="";clipFlowUiPosition=0;clipFlowUiAt=performance.now();
      clipFlowRuntime={...clipFlowRuntime,active:true,phase:r.phase||"stop",current_clip:r.source||clipFlowConfig.stop,current_role:r.current_role||"stop",
        position_seconds:0,duration_seconds:0,remaining_seconds:0,stop_requested:false,last_error:"",reason:r.reason||"settings_stop_preview",changed_at:0};
      clipFlowUpdateClockAnchor(clipFlowRuntime);
      clipFlowMessage(r.already_playing_stop?"Stop clip is already playing.":"Playing Stop clip.");
      notify(r.already_playing_stop?"Stop clip is already playing":"Started Stop clip");
    }
    renderClipFlow();
    await loadClipFlow(true);
  }catch(e){
    clipFlowMessage(e.message,true);
    notify(e.message,false);
    await loadClipFlow(true);
  }finally{
    clipFlowActionBusy=false;
    renderClipFlow();
  }
});

setTimeout(()=>loadClipFlow(true),800);
setInterval(async()=>{
  if((view!=="scenes"&&view!=="settings")||clipFlowActionBusy||clipFlowSeekBusy||clipFlowStatusPollInFlight)return;
  clipFlowStatusPollInFlight=true;
  try{await loadClipFlow(true,false)}finally{clipFlowStatusPollInFlight=false}
},1000);


/* ---------- comments ---------- */
function lines(v){return String(v||"").split(/\r?\n/).map(function(x){return x.trim()}).filter(Boolean)}
function commentsRuleText(rules){return (rules||[]).map(function(r){return String(r.keywords||"")+" => "+String(r.reply||"")}).join("\n")}
function parseRules(v){return lines(v).map(function(line){var i=line.indexOf("=>");if(i<1)return null;var keywords=line.slice(0,i).trim(),reply=line.slice(i+2).trim();return keywords&&reply?{keywords:keywords,reply:reply,enabled:true}:null}).filter(Boolean)}
function selectedComment(){return (CS&&CS.comments||[]).find(function(x){return x.id===selectedCommentId})||null}
function renderCommentList(){
  var box=$("#commentList");if(!box)return;var rows=CS&&CS.comments||[];
  box.innerHTML=rows.length?rows.map(function(c){
    var dt=new Date((c.seen_at||0)*1000),active=c.id===selectedCommentId;
    var rule=c.rule?'<span>rule: '+esc(c.rule)+'</span>':"",reply=c.reply?'<span>↳ '+esc(c.reply)+'</span>':"";
    return '<button class="comment-row '+(active?"sel":"")+'" data-comment="'+esc(c.id)+'"><div class="comment-head"><b>'+esc(c.username||"Viewer")+'</b><span>'+dt.toLocaleTimeString([], {hour:"2-digit",minute:"2-digit",second:"2-digit"})+'</span></div><div class="comment-text">'+esc(c.text||"")+'</div><div class="comment-meta"><span>'+esc(c.status||"new")+'</span>'+rule+reply+'</div></button>'
  }).join(""):'<div class="empty"><b>No comments yet</b>Start Reader and enter LIVE Manager</div>';
  var sel=selectedComment(),sb=$("#selectedComment"),rb=$("#commentReplySend");
  if(sb)sb.innerHTML=sel?'<b>'+esc(sel.username)+'</b><div>'+esc(sel.text)+'</div>':"ยังไม่ได้เลือกคอมเมนต์";
  if(rb)rb.disabled=!sel;
}
function renderCommentConfig(){
  var c=CS&&CS.config||{},set=function(id,v){var e=$(id);if(e&&document.activeElement!==e)e.value=v},ck=function(id,v){var e=$(id);if(e)e.checked=!!v};
  ck("#autoReplyEnable",c.auto_reply);ck("#autoReplyDry",c.dry_run!==false);
  set("#replyMatch",c.match_mode||"contains");set("#replySensitivity",c.sensitivity||"strict");
  set("#replyPollMs",c.poll_ms||1500);set("#replyMinInterval",c.min_interval_sec||8);set("#replyCooldown",c.user_cooldown_sec||60);set("#replyMaxMinute",c.max_per_minute||6);
  set("#replyRules",commentsRuleText(c.rules));set("#replyBlocked",(c.blocked_terms||[]).join("\n"));set("#replyBlockedOut",(c.blocked_response_terms||[]).join("\n"));
  var st=$("#replyConfigStatus");if(st)st.textContent=c.auto_reply?(c.dry_run!==false?"Auto Reply: DRY RUN":"Auto Reply: LIVE SEND"):"Auto Reply: OFF";
}
function renderComments(){
  var tag=$("#commentsTag"),health=$("#commentsHealth"),st=CS||{};
  if(tag){tag.textContent=st.running?"RUNNING":"STOPPED";tag.className="tag"+(st.running?" live":"")}
  if(health){if(st.last_error){health.className="errbox";health.innerHTML="<b>Reader error</b><p>"+esc(st.last_error)+"</p>"}else{health.className="unavail";health.innerHTML="<b>"+(st.connected?"Session connected":"Session not connected")+"</b><p>URL: "+esc(st.url||"-")+" · new: "+(st.found||0)+" · sent: "+(st.sent||0)+"</p>"}}
  renderCommentList();renderCommentConfig();
}
async function loadComments(){try{CS=await api("/api/comments/status");renderComments()}catch(e){if(view==="comments")notify("โหลด comments ไม่สำเร็จ: "+e.message,false)}}
$("#commentsStart")?.addEventListener("click",async function(){try{CS=await post("/api/comments/start",{});renderComments();notify("Comment Reader เริ่มแล้ว")}catch(e){notify(e.message,false)}});
$("#commentsStop")?.addEventListener("click",async function(){try{CS=await post("/api/comments/stop",{});renderComments();notify("หยุด Comment Reader แล้ว")}catch(e){notify(e.message,false)}});
$("#commentsRefresh")?.addEventListener("click",function(){loadComments()});
$("#commentList")?.addEventListener("click",function(e){var row=e.target.closest("[data-comment]");if(!row)return;selectedCommentId=row.dataset.comment;renderCommentList()});
$("#commentReplySend")?.addEventListener("click",async function(){var c=selectedComment(),t=$("#commentReplyText").value.trim();if(!c||!t)return;try{await post("/api/comments/reply",{id:c.id,reply:t});$("#commentReplyText").value="";notify("Queue reply แล้ว");await loadComments()}catch(e){notify(e.message,false)}});
$("#replySave")?.addEventListener("click",async function(){
  var patch={auto_reply:$("#autoReplyEnable").checked,dry_run:$("#autoReplyDry").checked,match_mode:$("#replyMatch").value,sensitivity:$("#replySensitivity").value,
    poll_ms:Number($("#replyPollMs").value),min_interval_sec:Number($("#replyMinInterval").value),user_cooldown_sec:Number($("#replyCooldown").value),max_per_minute:Number($("#replyMaxMinute").value),
    rules:parseRules($("#replyRules").value),blocked_terms:lines($("#replyBlocked").value),blocked_response_terms:lines($("#replyBlockedOut").value)};
  try{var r=await post("/api/comments/config",patch);CS=Object.assign({},CS,{config:r.config});renderComments();notify("บันทึก Auto Reply Settings แล้ว")}catch(e){notify(e.message,false)}
});
$("#ruleTestBtn")?.addEventListener("click",async function(){
  var sample=$("#ruleTestInput").value.trim(),out=$("#ruleTestResult");
  if(!sample){out.textContent="ใส่ข้อความสำหรับทดสอบก่อน";return}
  try{
    var r=await post("/api/comments/test-rule",{text:sample});
    if(r.match&&r.rule) out.innerHTML="<b>Matched</b> · "+esc(r.rule.keywords)+" → "+esc(r.rule.reply);
    else out.innerHTML="<b>No match</b> · ไม่มี rule ที่ตรง";
  }catch(e){out.textContent="ทดสอบไม่สำเร็จ: "+e.message}
});

setInterval(function(){if(view==="comments")loadComments()},1800);

$("#logClear")?.addEventListener("click",()=>{LOG.length=0;renderLogs();renderActivity()});

/* ---------- Temporary open-source video editor ---------- */
(function(){
  const w=document.getElementById("pkEditorWeb");
  document.getElementById("pkEditorReload")?.addEventListener("click",()=>{try{w?.reload()}catch{}});
  document.getElementById("pkEditorExternal")?.addEventListener("click",async()=>{
    try{
      const r=await window.paokra?.openExternal?.("https://openreel.video/");
      if(r&&!r.ok)notify(r.error||"เปิด Browser ไม่สำเร็จ",false);
    }catch(e){notify(e.message||String(e),false)}
  });
})();

/* ---------- UI / Browser session boot ---------- */
setupUISettings();
refreshTikTokSessionStatus();

/* ---------- boot ---------- */
const timer=setInterval(poll,1000);
window.addEventListener("pagehide",()=>{clearInterval(timer);$$("img[data-stream]").forEach(i=>i.removeAttribute("src"))});
renderActivity();go();poll().then(()=>{if(S)log("INFO","PAOKRA UI loaded");loadMedia()});

/* ===== verbose Server + CLI log ===== */
(function(){
  let since=0,timer=null,items=[],busy=false;
  const box=()=>document.getElementById("vlog");
  const fmt=t=>{const d=new Date(t*1000);return d.toTimeString().slice(0,8)+"."+String(d.getMilliseconds()).padStart(3,"0")};
  function draw(){
    const bx=box(); if(!bx)return;
    const f=(document.getElementById("vlFilter")||{}).value||"all";
    const stick=(document.getElementById("vlAuto")||{}).checked;
    const rows=items.filter(e=>f==="all"||e.src===f).map(e=>`<div><span class="t">${fmt(e.t)}</span> <span class="s-${e.src}">[${e.src}]</span> <span class="lv-${e.lv}">${esc(e.lv)} ${esc(e.m)}</span></div>`).join("");
    bx.innerHTML=rows||'<div class="t">ยังไม่มี log — กดเริ่มไลฟ์ หรือ LIVE Setup แล้วจะขึ้นที่นี่</div>';
    if(stick)bx.scrollTop=bx.scrollHeight;
  }
  async function tick(){
    if(busy)return; busy=true;
    try{
      const r=await (await fetch("/api/live/log?since="+since)).json();
      if(r.last<since){since=0;items=[]}
      if(r.items&&r.items.length){items=items.concat(r.items).slice(-1500);since=r.last;draw()}
    }catch(e){}
    busy=false;
  }
  function start(){if(!timer){tick();timer=setInterval(tick,1000)}}
  function stop(){if(timer){clearInterval(timer);timer=null}}
  window.vlogStartPolling=start;
  document.querySelectorAll(".vtab").forEach(btn=>btn.addEventListener("click",()=>{
    const k=btn.dataset.vt;
    document.querySelectorAll(".vtab").forEach(x=>x.classList.toggle("on",x===btn));
    document.getElementById("vt-act").hidden=k!=="act";
    document.getElementById("vt-srv").hidden=k!=="srv";
    if(k==="srv")start(); else stop();
  }));
  document.getElementById("vlFilter")?.addEventListener("change",draw);
  document.getElementById("vlClear")?.addEventListener("click",async()=>{try{await fetch("/api/live/log/clear",{method:"POST"})}catch(e){} items=[];since=0;draw()});
  document.getElementById("vlCopy")?.addEventListener("click",()=>{
    const txt=items.map(e=>`${fmt(e.t)} [${e.src}] ${e.lv} ${e.m}`).join("\n");
    navigator.clipboard&&navigator.clipboard.writeText(txt);
  });
})();
