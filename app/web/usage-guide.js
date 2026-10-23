/* PAOKRA Studio v1.4.9 — isolated user guide */
(function(){
"use strict";
const SUPPRESS_KEY="paokra.guide.suppress.v1";
const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>Array.from(r.querySelectorAll(s));
const tabs=[
{id:"start",icon:"🚀",label:"เริ่มต้น",eyebrow:"QUICK START",title:"เริ่มใช้งาน PAOKRA Studio",intro:"คู่มือฉบับย่อสำหรับตั้งค่าแอปตั้งแต่ครั้งแรกจนพร้อมส่งภาพออก Virtual Camera",body:'<ol class="pk-guide-steps"><li><b>ตรวจเครื่องและเวอร์ชัน</b><br>Settings → Performance ตรวจ CPU/RAM/GPU และ Settings → ระบบ & ซ่อมบำรุง ตรวจเวอร์ชัน</li><li><b>เลือกแหล่งภาพ</b><br>Settings → เอาต์พุต & ซอร์ส แล้วเลือกไฟล์วิดีโอหรือกำหนด Source</li><li><b>ตั้ง Output</b><br>เลือกความละเอียดและ FPS ให้ตรงกับงาน เริ่มทดสอบด้วย 720×1280 ที่ 30 FPS ได้</li><li><b>เริ่มระบบภาพ</b><br>ตรวจ Preview แล้วกด Start Engine และเริ่ม Virtual Camera ตามสถานะที่แสดง</li><li><b>ทดสอบก่อน LIVE จริง</b><br>เปิดแอปปลายทาง เลือก PAOKRA Virtual Camera แล้วตรวจภาพ/เสียงก่อนเริ่ม LIVE</li></ol><div class="pk-guide-callout">Setup LIVE เป็นการเตรียมระบบ ไม่ใช่ปุ่มเริ่ม LIVE จริง</div>'},
{id:"live",icon:"📡",label:"LIVE & กล้องเสมือน",eyebrow:"LIVE OUTPUT",title:"Live Console และ Virtual Camera",intro:"ส่วนนี้ควบคุมการส่งภาพและเสียงจาก PAOKRA ไปยังแอปปลายทาง",body:'<div class="pk-guide-grid"><div class="pk-guide-card"><h4>Live Console</h4><p>ตรวจสถานะ Engine, Preview, แหล่งภาพ, Output และการเตรียม TikTok LIVE Studio</p></div><div class="pk-guide-card"><h4>Virtual Camera</h4><p>เลือก Resolution/FPS ก่อนเริ่ม ตรวจสถานะและ FPS ที่ส่งจริงใน Settings → Output</p></div><div class="pk-guide-card"><h4>เสียงเสมือน</h4><p>เลือกอุปกรณ์เสียงตามการตั้งค่าในเครื่อง ตรวจทั้งอุปกรณ์ส่งเสียงและไมโครโฟนในแอปปลายทาง</p></div><div class="pk-guide-card"><h4>Stop กับ Emergency stop</h4><p>Stop ใช้หยุดตามขั้นตอนปกติ ส่วน Emergency stop หยุด render/Virtual Camera ทันที</p></div></div><div class="pk-guide-callout">ถ้าแอปปลายทางรายงาน FPS ไม่ตรง ให้ตรวจโหมด capture ที่ไดรเวอร์และแอปปลายทางเลือก</div>'},
{id:"clipflow",icon:"🎞️",label:"Clip Flow",eyebrow:"OPEN → NORMAL → STOP",title:"ตั้ง Playlist และ Clip Flow",intro:"Clip Flow ใช้คลิปเปิดหนึ่งครั้ง ตามด้วย Normal แบบสุ่มไม่ซ้ำในรอบ แล้วเล่น Stop เมื่อสั่งจบ",body:'<ol class="pk-guide-steps"><li><b>เลือก Open</b><br>Settings → เพลย์ลิสต์คลิป เลือกคลิปเปิดที่จะเล่นครั้งเดียว</li><li><b>เพิ่ม Normal</b><br>เพิ่มคลิปที่ต้องการให้สุ่มเล่นตามรอบ</li><li><b>เลือก Stop</b><br>กำหนดคลิปปิด หากต้องการให้คลิปจบก่อนปิด LIVE</li><li><b>ตั้ง Output/Fit ก่อน</b><br>เลือก Resolution/FPS ก่อนสร้าง Seam เพื่อให้ cache ตรงกับ Output</li><li><b>ตรวจสถานะ</b><br>กด Refresh status แล้วทดสอบ Flow ก่อนใช้งานจริง</li></ol><div class="pk-guide-callout">อย่าย้ายหรือลบไฟล์ต้นฉบับระหว่างกำลังเล่น</div>'},
{id:"seams",icon:"✨",label:"AI Seam / Make Frames",eyebrow:"DIRECTED TRANSITIONS",title:"สร้างเฟรมคั่นให้รอยต่อเนียนขึ้น",intro:"AI Seam Studio สร้างสะพานระหว่างคลิป A → B แยกตามทิศทาง และ Clip Flow ใช้เฉพาะคู่ที่ตรงกับไฟล์/การตั้งค่าปัจจุบัน",body:'<ol class="pk-guide-steps"><li><b>Scan environment</b><br>เช็ก Vulkan/RIFE, RAM, VRAM และพื้นที่ดิสก์</li><li><b>เลือกคู่และ Analyze</b><br>เลือก A และ B กด Analyze pair แล้ว Generate Seam ตรวจ Preview ก่อนใช้จริง</li><li><b>สร้างทุกทิศทาง</b><br>A → B และ B → A เป็นคนละคู่ ต้องสร้างแยก รวมถึง Open → Normal และ Normal → Stop ที่ใช้งาน</li><li><b>ใช้ Auto Make Frames</b><br>ตรวจรายการคลิปแล้วเริ่ม batch รอจนสถานะเสร็จ</li><li><b>เปิดใช้และทดสอบ</b><br>เปิด Use generated seams in Clip Flow แล้วทดสอบเปลี่ยนคลิปก่อน LIVE</li></ol><div class="pk-guide-callout">Seam จะใช้ได้เมื่อคู่ A → B, ไฟล์, Output/FPS และ AI settings ตรงกับ cache ที่ตรวจสอบแล้ว มิฉะนั้นจะเปลี่ยนคลิปตามปกติ</div><div class="pk-guide-grid"><div class="pk-guide-card"><h4>preloaded first frame used</h4><p>เฟรมเริ่มต้นที่เตรียมไว้ถูกใช้ตอน handoff</p></div><div class="pk-guide-card"><h4>PRELOAD MISS (cold decoder)</h4><p>ต้องเปิด decoder แบบปกติ อาจทำให้หน่วงตรงรอยต่อ</p></div></div>'},
{id:"appearance",icon:"🎨",label:"ธีม & รูปลักษณ์",eyebrow:"APPEARANCE STUDIO",title:"ปรับแต่งหน้าตาแอป",intro:"Settings → รูปลักษณ์ เปลี่ยนธีมและสีได้ทันที พร้อมจำค่าไว้ในเครื่องนี้",body:'<div class="pk-guide-grid"><div class="pk-guide-card"><h4>ธีมสำเร็จรูป</h4><p>Midnight, Ocean, Graphite, Forest, Sunset, Sakura, Paper และธีมพิเศษ Halloween</p></div><div class="pk-guide-card"><h4>สีและพื้นหลัง</h4><p>เลือกสีหลัก โหมดมืด/สว่าง/ตามระบบ Windows และพื้นหลัง</p></div><div class="pk-guide-card"><h4>ความลื่นไหล</h4><p>ปิด Glass effect หรือ Motion effects หากเครื่องทรัพยากรน้อย และเลือก Solid เพื่อลดเอฟเฟกต์</p></div><div class="pk-guide-card"><h4>นำเข้า/ส่งออก</h4><p>Export theme เพื่อสำรองรูปลักษณ์ และ Import theme เพื่อนำกลับมาใช้</p></div></div><div class="pk-guide-callout">🎃 Halloween — ธีมโทนส้มฟักทองกับม่วงกลางคืน เป็น Easter egg ตามคำแนะนำของ OOMSIN</div>'},
{id:"settings",icon:"⚙️",label:"Settings & อัปเดต",eyebrow:"SETTINGS",title:"การตั้งค่าระบบและอัปเดต",intro:"การตั้งค่าหลักถูกแยกเป็นแท็บเพื่อหาตัวเลือกได้ง่ายขึ้น",body:'<div class="pk-guide-grid"><div class="pk-guide-card"><h4>Performance</h4><p>สแกนสเปกและปรับโปรไฟล์ให้เหมาะกับ CPU/RAM/GPU</p></div><div class="pk-guide-card"><h4>Output & Source</h4><p>ตั้ง Resolution, FPS, Loop และเลือกไฟล์ต้นฉบับ</p></div><div class="pk-guide-card"><h4>System & Maintenance</h4><p>ดูเวอร์ชัน เปิดโฟลเดอร์ Log/Data รีสตาร์ท Backend และเลือก Stable/Beta</p></div><div class="pk-guide-card"><h4>ภาษาและเลย์เอาต์</h4><p>เปลี่ยนภาษา ขนาด UI, Sidebar และ Motion effects ได้จาก Appearance settings</p></div></div><div class="pk-guide-callout">หากอัปเดตเจอ HTTP 403 ระบบจะลอง fallback ไปอ่านรายการ Release สาธารณะ หากยังไม่พบตัวติดตั้ง ให้เปิด Settings → ระบบ & ซ่อมบำรุง → เลือกเวอร์ชัน (Beta / Stable)</div>'},
{id:"troubleshoot",icon:"🛠️",label:"แก้ปัญหาเบื้องต้น",eyebrow:"TROUBLESHOOTING",title:"แก้ปัญหาทั่วไปก่อนส่ง Log",intro:"ลองตามขั้นตอนด้านล่างทีละข้อ และจดข้อความผิดพลาดไว้",body:'<div class="pk-guide-grid"><div class="pk-guide-card"><h4>ภาพดำ / Preview ไม่ขึ้น</h4><ul><li>ตรวจว่าไฟล์ Source ยังอยู่</li><li>เลือกไฟล์ใหม่และกด Refresh</li><li>ตรวจ Resolution/FPS</li><li>ถ้ายังไม่หาย ให้ Restart Backend</li></ul></div><div class="pk-guide-card"><h4>Virtual Camera ไม่พบ</h4><ul><li>ตรวจว่ากล้องเริ่มทำงานแล้ว</li><li>ปิดแอปอื่นที่ยึดกล้อง</li><li>เลือก PAOKRA Virtual Camera ใหม่</li><li>ตรวจไดรเวอร์หากยังไม่พบ</li></ul></div><div class="pk-guide-card"><h4>คลิปกระตุกตรงรอยต่อ</h4><ul><li>ตรวจ PRELOAD MISS</li><li>ตรวจคู่ A → B และ Output/FPS</li><li>ทดสอบหนึ่งคู่ก่อน Playlist เต็ม</li><li>ลดภาระ AI/Preview หาก RAM/VRAM เต็ม</li></ul></div><div class="pk-guide-card"><h4>อัปเดตล้มเหลว</h4><ul><li>ตรวจอินเทอร์เน็ตและเวลาระบบ</li><li>ใช้หน้าเลือกเวอร์ชันใน Settings → System</li><li>อย่าติดตั้งไฟล์ที่ดาวน์โหลดไม่ครบ</li><li>เก็บ error และ Log ไว้</li></ul></div></div><div class="pk-guide-callout">หากยังมีปัญหา เปิด Settings → ระบบ & ซ่อมบำรุง → เปิดโฟลเดอร์ Log แล้วส่ง Log พร้อมขั้นตอนที่ทำก่อนเกิดปัญหา อย่าส่ง cookie หรือข้อมูลล็อกอิน</div>'},
{id:"credits",icon:"💜",label:"Easter egg & เครดิต",eyebrow:"A LITTLE THANK YOU",title:"ขอบคุณ OOMSIN 💜",intro:"Easter egg เล็ก ๆ เพื่อให้เกียรติน้อง OOMSIN ผู้ให้คำแนะนำที่ช่วยผลักดัน PAOKRA ให้ดีขึ้น",body:'<div class="pk-guide-card" style="text-align:center;padding:30px 18px;background:radial-gradient(ellipse at top,rgba(255,138,61,.14),transparent 68%),radial-gradient(ellipse at bottom,rgba(151,71,255,.14),transparent 70%)"><div style="font-size:48px">🎃 👻 💜</div><h4 style="font-size:22px;margin:14px 0 8px">OOMSIN</h4><p class="pk-guide-easter">A small idea can make a big difference.</p><p>ขอบคุณสำหรับคำแนะนำและแรงบันดาลใจ<br>จากทีม PAOKRA ด้วยความขอบคุณ</p></div><div class="pk-guide-callout">ลองเข้า Settings → รูปลักษณ์ แล้วเลือกธีม Halloween เพื่อค้นพบ Easter egg นี้อีกแบบ 🎃</div>'}
];
function get(k){try{return localStorage.getItem(k)}catch(_){return null}}
function set(k,v){try{localStorage.setItem(k,v)}catch(_){}}
function create(){
 if($("#pkGuideOverlay"))return;
 const o=document.createElement("div");o.id="pkGuideOverlay";o.setAttribute("aria-hidden","true");
 const nav=tabs.map((t,i)=>'<button class="pk-guide-tab" type="button" role="tab" id="pkGuideTab-'+t.id+'" aria-controls="pkGuidePage-'+t.id+'" aria-selected="'+(i===0)+'" data-guide-tab="'+t.id+'"><span>'+t.icon+'</span><span>'+t.label+'</span></button>').join("");
 const pages=tabs.map((t,i)=>'<article class="pk-guide-page '+(i===0?'pk-guide-active':'')+'" role="tabpanel" id="pkGuidePage-'+t.id+'" aria-labelledby="pkGuideTab-'+t.id+'" data-guide-page="'+t.id+'"><div class="pk-guide-eyebrow">'+t.eyebrow+'</div><h3>'+t.title+'</h3><p>'+t.intro+'</p>'+t.body+'</article>').join("");
 o.innerHTML='<section id="pkGuideDialog" role="dialog" aria-modal="true" aria-labelledby="pkGuideTitle"><header class="pk-guide-head"><div class="pk-guide-brand"><div class="pk-guide-mark">PK</div><div><h2 id="pkGuideTitle">คู่มือการใช้งาน PAOKRA Studio</h2><p>คู่มือแบบแท็บ • ขั้นตอนใช้งาน • แก้ปัญหาเบื้องต้น</p></div></div><button class="pk-guide-close" type="button" data-guide-close aria-label="ปิดคู่มือ">×</button></header><div class="pk-guide-body"><nav class="pk-guide-nav" role="tablist" aria-label="หัวข้อคู่มือ">'+nav+'</nav><div class="pk-guide-content">'+pages+'</div></div><footer class="pk-guide-foot"><label><input id="pkGuideDontShow" type="checkbox"> ไม่ต้องแสดงคู่มือนี้อัตโนมัติอีก</label><button class="pk-guide-btn" type="button" data-guide-close>ปิดชั่วคราว</button><button class="pk-guide-btn primary" type="button" data-guide-done>เข้าใช้งาน PAOKRA</button></footer></section>';
 document.body.appendChild(o);
 o.addEventListener("click",e=>{
  const tab=e.target.closest("[data-guide-tab]");
  if(tab){const id=tab.dataset.guideTab;$$("[data-guide-tab]",o).forEach(b=>b.setAttribute("aria-selected",String(b===tab)));$$("[data-guide-page]",o).forEach(p=>p.classList.toggle("pk-guide-active",p.dataset.guidePage===id));return}
  if(e.target.closest("[data-guide-close]")){close(false);return}
  if(e.target.closest("[data-guide-done]")){close(false);return}
 });
 document.addEventListener("keydown",e=>{if(e.key==="Escape"&&o.classList.contains("pk-guide-open"))close(false)});
}
function open(tab){
 create();const o=$("#pkGuideOverlay");if(!o)return;
 o.classList.add("pk-guide-open");o.setAttribute("aria-hidden","false");document.body.classList.add("pk-guide-lock");
 const b=$('[data-guide-tab="'+(tab||"start")+'"]',o);if(b)b.click();
 const check=$("#pkGuideDontShow",o);if(check)check.checked=get(SUPPRESS_KEY)==="1";
 const x=$("[data-guide-close]",o);if(x)x.focus({preventScroll:true});
}
function close(done){
 const o=$("#pkGuideOverlay");if(!o)return;
 const check=$("#pkGuideDontShow",o);
 if(done||(check&&check.checked))set(SUPPRESS_KEY,"1");
 o.classList.remove("pk-guide-open");o.setAttribute("aria-hidden","true");document.body.classList.remove("pk-guide-lock");
 const startup=$("#pkGuideShowStartup");if(startup)startup.checked=get(SUPPRESS_KEY)!=="1";
}
function addButton(host,tab,label){
 if(!host||host.querySelector('[data-open-guide="'+tab+'"]'))return;
 const b=document.createElement("button");b.type="button";b.className="btn ghost pk-guide-inline";b.dataset.openGuide=tab;b.textContent="📘 "+label;
 const h=host.querySelector("h2");if(h&&h.parentElement)h.parentElement.appendChild(b);else host.prepend(b);
}
function openAfterBoot(){
 const started=Date.now();
 const wait=()=>{if(get(SUPPRESS_KEY)==="1")return;if((window.__pkBootDone===true&&!$("#pkBoot"))||Date.now()-started>35000){open("start");return}setTimeout(wait,150)};
 setTimeout(wait,150);
}
function init(){
 create();
 const appearance=$('#v-settings [data-set-panel="appearance"]'),about=$('#v-settings [data-set-panel="about"]');
 addButton(appearance,"start","เปิดคู่มือการใช้งาน");addButton(about,"troubleshoot","คู่มือ & วิธีแก้ปัญหา");
 const card=appearance&&appearance.querySelector(".card");
 if(card&&!$("#pkGuideStartupSetting",card)){
  const row=document.createElement("div");row.className="pk-row";row.id="pkGuideStartupSetting";
  row.innerHTML='<div><b>คู่มือการใช้งาน</b><small>เลือกให้คู่มือแสดงทุกครั้งที่เปิดแอป หรือเปิดดูเองจาก Settings</small></div><label class="check" style="margin:0"><input id="pkGuideShowStartup" type="checkbox"> แสดงตอนเปิดแอป</label>';
  const preset=card.querySelector("#pkPresets");if(preset&&preset.parentElement)preset.parentElement.insertBefore(row,preset.nextElementSibling);else card.appendChild(row);
  const check=$("#pkGuideShowStartup",row);if(check){check.checked=get(SUPPRESS_KEY)!=="1";check.addEventListener("change",()=>set(SUPPRESS_KEY,check.checked?"0":"1"))}
 }
 document.addEventListener("click",e=>{const b=e.target.closest("[data-open-guide]");if(b){e.preventDefault();open(b.dataset.openGuide)}});
 if(get(SUPPRESS_KEY)!=="1")openAfterBoot();
}
if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",init,{once:true});else init();
window.PAOKRAUsageGuide={open:open};
})();
/* v1.4.9 Easter egg: a small thank-you to OOMSIN, kept inside the guide only. */
(function(){
  'use strict';
  function addOomsinCredit(){
    const dialog=document.querySelector('#pkGuideDialog');
    if(!dialog || dialog.querySelector('.pk-guide-oomsin')) return;
    const credit=document.createElement('div');
    credit.className='pk-guide-oomsin';
    credit.setAttribute('aria-label','Easter egg: OOMSIN');
    credit.innerHTML='<span class="pk-guide-oomsin-star" aria-hidden="true">✦</span><span>Made brighter with a little help from <strong>OOMSIN</strong></span><span class="pk-guide-oomsin-pumpkin" aria-hidden="true">🎃</span>';
    dialog.appendChild(credit);
  }
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',addOomsinCredit,{once:true});
  else addOomsinCredit();
})();
