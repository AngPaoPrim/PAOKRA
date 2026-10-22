"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = __dirname;
const source = fs.readFileSync(path.join(root, "app", "web", "app.js"), "utf8");
const passed = [];
function check(name, fn) {
  fn();
  passed.push(name);
  process.stdout.write("PASS: " + name + "\n");
}

check("clipFlowUiIdentity is declared before clock helpers", () => {
  assert.match(source, /let clipFlowUiIdentity\s*=\s*["']["'];/);
});
check("clipFlowStatusPollInFlight is declared before the status-poll interval", () => {
  assert.match(source, /let clipFlowStatusPollInFlight\s*=\s*false;/);
  assert.match(source, /if\(\(view!=="scenes"&&view!=="settings"\)\|\|clipFlowActionBusy\|\|clipFlowSeekBusy\|\|clipFlowStatusPollInFlight\)return;/);
});
const server = fs.readFileSync(path.join(root, "app", "server.py"), "utf8");

check("v1.4.5 batch reservation protects Engine/seek/source/settings across pair gaps", () => {
  assert.ok(server.includes("AI_SEAM_BATCH_LEASE_SECONDS = 120.0"));
  assert.ok(server.includes('@app.post("/api/ai-seam/batch-lock")'));
  assert.ok(server.includes("def _ai_seam_batch_is_active():"));
  assert.ok(server.includes("def _protect_ai_seam_batch_mutations():"));
  assert.ok(server.includes("wait for the verified transition batch to finish before starting the Engine"));
  assert.ok(server.includes("Auto Make Frames is active; Scene/Output/Source changes are temporarily locked"));
  assert.ok(source.includes('post("/api/ai-seam/batch-lock",{action:"acquire"})'));
  assert.ok(source.includes('action:"heartbeat",token:batchToken'));
  assert.ok(source.includes('action:"release",token:batchToken'));
  assert.ok(source.includes("batch_token:batchToken"));
});
check("Auto Make verifies saved bridges after retrying reads and blocks overlapping manual generation", () => {
  assert.ok(source.includes("let clipFlowSeamLoadSeq=0"));
  assert.ok(source.includes("if(requestSeq!==clipFlowSeamLoadSeq)return r"));
  assert.ok(source.includes("async function waitForClipFlowSeam(fromPath,toPath)"));
  assert.ok(source.includes("await waitForClipFlowSeam(pair.from,pair.to)"));
  assert.ok(source.includes("Auto Make Frames กำลังใช้ AI Seam อยู่"));
});
check("AI Seam reports complete only after compute lock is released", () => {
  const ai = fs.readFileSync(path.join(root, "app", "ai_seam.py"), "utf8");
  assert.ok(ai.includes('status="finalizing", phase="finalizing"'));
  assert.ok(ai.includes('self.generation_lock.release()'));
  assert.ok(ai.includes('job.update(status="complete"'));
  assert.ok(ai.indexOf('self.generation_lock.release()') < ai.indexOf('job.update(status="complete"'));
});
check("Engine start retains the last valid frame while decoder pre-roll fills", () => {
  const controlStart = server.indexOf('if action == "start":', server.indexOf('@app.post("/api/control")'));
  const controlStop = server.indexOf('if action == "stop":', controlStart);
  assert.ok(controlStart >= 0 && controlStop > controlStart);
  const body = server.slice(controlStart, controlStop);
  assert.ok(body.includes("Keep the last valid preview/compositor frame as a hold frame"));
  assert.ok(!body.includes("source_prepared_frame = None; source_prepared_serial += 1"));
});
check("Normal queue remains randomized per cycle, not source-list order", () => {
  const a = server.indexOf("def _clip_flow_shuffle_next_locked():");
  const b = server.indexOf("def _ensure_clip_flow_controller()", a);
  assert.ok(a >= 0 && b > a, "shuffle function was not found");
  const body = server.slice(a, b);
  assert.match(body, /random\.shuffle\(normals\)/);
  assert.match(body, /if len\(normals\) > 1 and normals\[0\] == previous/);
  assert.match(body, /chosen = queue\.pop\(0\)/);
  assert.match(body, /clip_flow_runtime\["normal_queue"\] = queue/);
});
check("AI Seam uses an exact directed pair and resumes destination B", () => {
  assert.match(server, /find_cached_transition\(DATA, current_path, next_path, runtime_profile=seam_runtime_profile\)/);
  assert.match(server, /target_phase = "seam"/);
  assert.match(server, /seam_next_path=next_path/);
  assert.match(server, /elif phase == "seam":/);
  assert.match(server, /set_source\(next_path, start_seconds=\(seam_resume_seconds if preserve_seam_audio else 0\.0\)\)/);
});
check("render and timer share one duration helper", () => {
  const useCount = (source.match(/const duration=clipFlowTimelineDuration\(\),position=clipFlowDisplayPosition\(\);/g) || []).length;
  assert.ok(useCount >= 2, "Expected render + timer to call the same helpers; count=" + useCount);
  assert.doesNotMatch(source, /const duration=Math\.max\(0,Number\(clipFlowRuntime\.active\?clipFlowRuntime\.duration_seconds:S\?\.source_duration_seconds\)\|\|0\),position=clipFlowDisplayPosition\(\);/);
});

const start = source.indexOf("let clipFlowUiPosition=0;");
const end = source.indexOf("function renderClipFlow()", start);
assert.ok(start >= 0 && end > start, "could not isolate Clip Flow clock functions");
const core = source.slice(start, end);
let now = 1000;
const ctx = vm.createContext({
  performance: { now: () => now },
  Math, Number, String,
  S: {
    running: true,
    source: "C:\\clips\\open.mp4",
    source_duration_seconds: 88.12,
    source_position_seconds: 88.10,
    source_fps: 29.97,
    effective_fps: 30,
    loop: false
  },
  stateSnapshotAt: 900,
  clipFlowRuntime: {
    active: true, phase: "stop", current_clip: "C:\\clips\\open.mp4",
    changed_at: 1, position_seconds: 88.10, duration_seconds: 88.12,
    source_fps: 29.97, output_fps: 30
  },
  clipFlowPathKey(value) { return String(value || "").toLowerCase(); },
  $() { return null; }
});
vm.runInContext(core, ctx, { filename: "clipflow-clock-core.js" });

check("clock initializes without clipFlowUiIdentity ReferenceError", () => {
  assert.doesNotThrow(() => vm.runInContext("clipFlowUpdateClockAnchor(clipFlowRuntime)", ctx));
});
check("restarting the same filename resets old EOF position to zero", () => {
  ctx.clipFlowRuntime.phase = "open";
  ctx.clipFlowRuntime.changed_at = 2;
  ctx.clipFlowRuntime.position_seconds = 0;
  ctx.clipFlowRuntime.duration_seconds = 0;
  vm.runInContext('clipFlowUiIdentity=""; clipFlowUiPosition=0; clipFlowUiAt=performance.now(); clipFlowUpdateClockAnchor(clipFlowRuntime);', ctx);
  assert.equal(vm.runInContext("clipFlowTimelineDuration()", ctx), 88.12);
  assert.equal(vm.runInContext("clipFlowDisplayPosition()", ctx), 0);
});
check("playhead advances smoothly from the fresh per-clip anchor", () => {
  now += 500;
  assert.ok(Math.abs(vm.runInContext("clipFlowDisplayPosition()", ctx) - 0.5) < 0.01);
});
check("a delayed backend poll cannot rewind the playhead", () => {
  ctx.clipFlowRuntime.position_seconds = 0.35;
  vm.runInContext("clipFlowUpdateClockAnchor(clipFlowRuntime)", ctx);
  assert.ok(Math.abs(vm.runInContext("clipFlowDisplayPosition()", ctx) - 0.5) < 0.01);
});
check("a forward backend correction is adopted without losing continuity", () => {
  ctx.clipFlowRuntime.position_seconds = 1.25;
  vm.runInContext("clipFlowUpdateClockAnchor(clipFlowRuntime)", ctx);
  assert.ok(Math.abs(vm.runInContext("clipFlowDisplayPosition()", ctx) - 1.25) < 0.01);
  now += 250;
  assert.ok(Math.abs(vm.runInContext("clipFlowDisplayPosition()", ctx) - 1.5) < 0.01);
});
check("a different clip handoff resets position and duration", () => {
  ctx.S.source = "C:\\clips\\normal-b.mp4";
  ctx.S.source_duration_seconds = 67.5;
  ctx.S.source_position_seconds = 0;
  ctx.clipFlowRuntime = {
    active: true, phase: "normal", current_clip: "C:\\clips\\normal-b.mp4",
    changed_at: 3, position_seconds: 0.2, duration_seconds: 67.5,
    source_fps: 30, output_fps: 30
  };
  vm.runInContext("clipFlowUpdateClockAnchor(clipFlowRuntime)", ctx);
  assert.equal(vm.runInContext("clipFlowTimelineDuration()", ctx), 67.5);
  assert.ok(Math.abs(vm.runInContext("clipFlowDisplayPosition()", ctx) - 0.2) < 0.01);
  now += 250;
  assert.ok(Math.abs(vm.runInContext("clipFlowDisplayPosition()", ctx) - 0.45) < 0.01);
});
check("remaining-time basis matches the slider duration", () => {
  const duration = vm.runInContext("clipFlowTimelineDuration()", ctx);
  const position = vm.runInContext("clipFlowDisplayPosition()", ctx);
  assert.ok(duration > position);
  assert.ok(Math.abs((duration - position) - (67.5 - position)) < 1e-9);
});


const pairStart = source.indexOf("function clipFlowPathKey");
const pairEnd = source.indexOf("async function loadClipFlowSeams", pairStart);
assert.ok(pairStart >= 0 && pairEnd > pairStart, "could not isolate playlist pair helpers");
const pairCore = source.slice(pairStart, pairEnd);
const pairCtx = vm.createContext({
  clipFlowConfig: {
    open: "C:\\clips\\open.mp4",
    normal: ["C:\\clips\\1mp4.mp4", "C:\\clips\\2mp4.mp4", "C:\\clips\\3.mp4", "c:/clips/1mp4.mp4"],
    stop: "C:\\clips\\stop.mp4"
  },
  clipFlowSeamCache: [],
  String, Set, Array, Number
});
vm.runInContext(pairCore, pairCtx, { filename: "clipflow-pair-helpers.js" });
check("Auto Make covers all reachable directed Playlist edges, not only Normal", () => {
  const pairs = vm.runInContext("clipFlowPlayablePairs()", pairCtx);
  const key = (p) => String(p).replace(/\//g, "\\").toLowerCase();
  const keys = pairs.map(p => key(p.from) + " -> " + key(p.to));
  assert.equal(pairs.length, 13, "3 Normal clips + Open + Stop should produce 13 distinct reachable directed edges");
  assert.ok(keys.includes(key("C:\\clips\\open.mp4") + " -> " + key("C:\\clips\\1mp4.mp4")));
  assert.ok(keys.includes(key("C:\\clips\\open.mp4") + " -> " + key("C:\\clips\\stop.mp4")));
  assert.ok(keys.includes(key("C:\\clips\\1mp4.mp4") + " -> " + key("C:\\clips\\3.mp4")));
  assert.ok(keys.includes(key("C:\\clips\\3.mp4") + " -> " + key("C:\\clips\\1mp4.mp4")));
  assert.ok(keys.includes(key("C:\\clips\\3.mp4") + " -> " + key("C:\\clips\\stop.mp4")));
  assert.ok(!keys.includes(key("C:\\clips\\stop.mp4") + " -> " + key("C:\\clips\\1mp4.mp4")), "Stop is terminal, so no outgoing Playlist edge");
  assert.equal(new Set(keys).size, 13, "case/slash-duplicate paths must not create duplicate pairs");
});
check("Auto Make requires current-output compatibility before saying ready", () => {
  const cache = [
    { from_path: "C:\\clips\\1mp4.mp4", to_path: "C:\\clips\\2mp4.mp4", available: true },
    { from_path: "C:\\clips\\1mp4.mp4", to_path: "C:\\clips\\2mp4.mp4", available: true, compatible: false },
    { from_path: "C:\\clips\\1mp4.mp4", to_path: "C:\\clips\\2mp4.mp4", available: true, compatible: true }
  ];
  const query = 'clipFlowHasCachedSeam("C:\\\\clips\\\\1mp4.mp4","C:\\\\clips\\\\2mp4.mp4")';
  pairCtx.clipFlowSeamCache = cache.slice(0, 1);
  assert.equal(vm.runInContext(query, pairCtx), false);
  pairCtx.clipFlowSeamCache = cache.slice(1, 2);
  assert.equal(vm.runInContext(query, pairCtx), false);
  pairCtx.clipFlowSeamCache = cache.slice(2, 3);
  assert.equal(vm.runInContext(query, pairCtx), true);
});
check("Scene Builder uses a single Start/Stop toggle under the preview", () => {
  const html = fs.readFileSync(path.join(root, "app", "web", "index.html"), "utf8");
  const css = fs.readFileSync(path.join(root, "app", "web", "style.css"), "utf8");
  assert.ok(html.includes('class="card-mini scene-timeline"') && html.includes('id="sceneClipPlay"'));
  assert.ok(!html.includes('id="sceneClipStop"'), "old separate Stop button should not be in the visible UI");
  assert.ok(source.includes('$("#sceneClipPlay")?.addEventListener("click",()=>S?.running?stopEnginePlayback():startEnginePlayback())'));
  const flowRenderStart = source.indexOf("function renderClipFlow()");
  const flowRenderEnd = source.indexOf('$("#clipFlowSeek")?.addEventListener("input"', flowRenderStart);
  assert.ok(flowRenderStart >= 0 && flowRenderEnd > flowRenderStart);
  const flowRenderBody = source.slice(flowRenderStart, flowRenderEnd);
  assert.doesNotMatch(flowRenderBody, /sceneClipPlay|sceneClipStop/, "Clip Flow polling must not override the dedicated Start/Stop toggle");
  assert.match(source, /sceneToggle\.disabled=!!engineActionBusy/);
  assert.ok(css.includes(".scene-timeline{") && css.includes("align-self:center"));
});
check("Text and Web inspectors stay hidden until the Layers action selects them", () => {
  const html = fs.readFileSync(path.join(root, "app", "web", "index.html"), "utf8");
  assert.ok(html.includes('id="textInspector" hidden'));
  assert.ok(html.includes('id="webInspector" class="card-mini" hidden'));
  assert.ok(source.includes('sceneInspectorRequested=(id==="text"||id==="web")?id:""'));
  assert.ok(source.includes("function renderSceneInspectorPanels()"));
});
check("AI Seam Preview composes source + generated bridge + destination", () => {
  const ai = fs.readFileSync(path.join(root, "app", "ai_seam.py"), "utf8");
  assert.ok(ai.includes("def build_sequence_preview("));
  assert.ok(ai.includes("[a][s][b]concat=n=3:v=1:a=0[outv]"));
  assert.ok(ai.includes('@bp.get("/seams/<seam_id>/preview")'));
  assert.ok(source.includes('/api/ai-seam/seams/\${encodeURIComponent(b.dataset.aisPreview)}/preview'));
});
check("AI Seam interior timing avoids duplicated clip endpoints", () => {
  const ai = fs.readFileSync(path.join(root, "app", "ai_seam.py"), "utf8");
  assert.ok(ai.includes('next_frame, _ = _frame_from(Path(b["path"]), at_seconds=transition_seconds)'));
  assert.ok(ai.includes("first_b, _ = _frame_from(p_b, at_seconds=resume)"));
  assert.ok(ai.includes('transition_seconds = (int(settings["transition_frames"]) + 1) / max(1, fps)'));
  assert.ok(ai.includes("resume = (frames_count + 1) / float(fps)"));
  assert.ok(ai.includes("fractions = [i / float(frames_count + 1) for i in range(1, frames_count + 1)]"));
  assert.ok(ai.includes('"bridge_algorithm": "interior-only-v2"'));
  assert.ok(server.includes('seam_next_role="stop"'));
  assert.ok(server.includes('keep_stop_pending = bool(clip_flow_runtime.get("stop_requested")) and target_phase == "normal"'));
});
check("VRAM-aware scanner recommends GPU memory/queue profile without parallel pair jobs", () => {
  const ai = fs.readFileSync(path.join(root, "app", "ai_seam.py"), "utf8");
  assert.ok(ai.includes("def _vulkan_memory_info("));
  assert.ok(ai.includes("vram_available_bytes"));
  assert.ok(ai.includes("recommended_rife_jobs"));
  assert.ok(ai.includes('"generation_batch": 1'));
  assert.ok(ai.includes('"-j", jobs'));
});
check("Auto-update retries startup checks and explicitly requests silent install", () => {
  const main = fs.readFileSync(path.join(root, "desktop", "main.cjs"), "utf8");
  assert.ok(main.includes('scheduleUpdateCheckRetry(12000, "startup-confirmation")'));
  assert.ok(main.includes('if (!manual) scheduleUpdateCheckRetry(null, "check-error")'));
  assert.ok(main.includes("updaterInstance.autoInstallOnAppQuit = false"));
  assert.ok(main.includes("updaterInstance.quitAndInstall(true, true)"));
  assert.ok(main.includes("lastUserActivityAt"));
  assert.ok(main.includes("flowActive"));
  assert.ok(main.includes("generationBusy"));
  assert.ok(main.includes('readRuntimeJsonForUpdate("/api/ai-seam/status")'));
  assert.ok(main.includes("appIsQuitting = true"));
});
check("System Update FIX offers versions, is directly reachable from Settings, and verifies downloaded installer", () => {
  const main = fs.readFileSync(path.join(root, "desktop", "main.cjs"), "utf8");
  const preload = fs.readFileSync(path.join(root, "desktop", "preload.cjs"), "utf8");
  const html = fs.readFileSync(path.join(root, "app", "web", "index.html"), "utf8");
  const aboutUi = fs.readFileSync(path.join(root, "app", "web", "paokra-ui.js"), "utf8");
  const updateUi = fs.readFileSync(path.join(root, "desktop", "system-update.html"), "utf8");
  assert.ok(html.includes('id="pkOpenSystemUpdateFix"') && html.includes("Choose version (Beta / Stable)"), "Settings > About needs a visible version-selector button");
  assert.ok(aboutUi.includes('$("#pkOpenSystemUpdateFix")?.addEventListener("click"') && aboutUi.includes("B.openSystemUpdateFix()"), "visible selector button must be wired");
  assert.ok(preload.includes('openSystemUpdateFix: () => ipcRenderer.invoke("open-system-update-fix")'));
  assert.ok(main.includes('ipcMain.handle("open-system-update-fix"') && main.includes("openSystemUpdateFix();return{ok:true}"));
  assert.ok(updateUi.includes('id="v"') && updateUi.includes("system-update-list"));
  assert.ok(updateUi.includes("rs.find(x=>x.tag==='v1.4.5-beta.1')||rs.find(x=>x.tag==='v1.4.4-system-update-fix')"), "System Update should default to this beta when available, then fall back to the supplemental v1.4.4 fix");
  assert.ok(updateUi.includes('id="db"') && updateUi.includes('id="ib"'));
  assert.ok(updateUi.includes("system-update-download',r.asset,r.tag,r.size,r.digest"));
  assert.ok(main.includes('digest:String(a.digest||"")'));
  assert.ok(main.includes('expectedBytes>0&&got!==expectedBytes'));
  assert.ok(main.includes('hash.digest("hex")'));
  assert.ok(main.includes('expectedSha.slice(7).toLowerCase()'));
  assert.ok(main.includes("Download redirected outside the approved GitHub HTTPS hosts"));
  assert.ok(main.includes('fs.renameSync(partial,dest)'));
});
check("every sidebar menu target maps to one existing view", () => {
  const html = fs.readFileSync(path.join(root, "app", "web", "index.html"), "utf8");
  const nav = [...html.matchAll(/<a\b[^>]*data-view="([^"]+)"[^>]*>/g)].map(m => m[1]);
  const views = [...html.matchAll(/<section\s+id="v-([^"]+)"/g)].map(m => m[1]);
  assert.equal(nav.length, 14, "sidebar should expose the expected 14 main entries");
  assert.equal(new Set(nav).size, nav.length, "navigation entries must not be duplicated");
  assert.deepEqual([...new Set(nav)].sort(), [...new Set(views)].sort(), "every menu must land on an existing matching section");
});
check("all page DOM IDs are unique", () => {
  const html = fs.readFileSync(path.join(root, "app", "web", "index.html"), "utf8");
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(m => m[1]);
  const duplicates = ids.filter((id, i) => ids.indexOf(id) !== i);
  assert.deepEqual(duplicates, [], "duplicate DOM IDs: " + duplicates.join(", "));
});
check("auto-update activity signal is connected end-to-end", () => {
  const main = fs.readFileSync(path.join(root, "desktop", "main.cjs"), "utf8");
  const preload = fs.readFileSync(path.join(root, "desktop", "preload.cjs"), "utf8");
  assert.ok(preload.includes('updateActivity: () => ipcRenderer.send("update-user-activity")'));
  assert.ok(main.includes('ipcMain.on("update-user-activity"'));
  assert.ok(source.includes('["pointerdown","pointermove","keydown","input","change"]'));
});

check("Scene/Output profile changes immediately refresh AI Seam compatibility", () => {
  assert.match(source, /const CLIP_FLOW_SEAM_PROFILE_KEYS=new Set\(\["width","height","fps","fps_mode","source_width","source_height","source_fit_mode"\]\)/);
  assert.match(source, /await post\("\/api\/settings",o\);[\s\S]*?if\(Object\.keys\(o\|\|\{\}\)\.some\(key=>CLIP_FLOW_SEAM_PROFILE_KEYS\.has\(key\)\)\)\{[\s\S]*?await loadClipFlowSeams\(\{attempts:2\}\)/);
});
check("Seam UI does not claim cached bridges are ready while a fresh compatibility check is pending", () => {
  assert.match(source, /let clipFlowSeamChecking=false/);
  assert.match(source, /clipFlowSeamChecking=true;clipFlowSeamCheckError="";renderClipFlowSeams\(\)/);
  assert.match(source, /readyNow=!clipFlowSeamChecking&&!clipFlowSeamCheckError&&clipFlowHasCachedSeam/);
  assert.ok(source.includes("กำลังตรวจสอบ Output/AI Settings ของเฟรมคั่น"));
  assert.ok(source.includes("สถานะเดิมยังไม่ยืนยัน"));
});
check("Incompatible seam rows explain the actual backend reason", () => {
  assert.ok(source.includes("function clipFlowSeamCompatibilityReason(seam)"));
  assert.ok(source.includes("Output/FPS/สัดส่วนภาพ/Fit เปลี่ยน"));
  assert.ok(source.includes("ความละเอียด AI แบบ Manual เปลี่ยน"));
  assert.ok(source.includes("badge.title=String(seam.compatibility_reason||"));
});
process.stdout.write("CLIPFLOW_TIMELINE_QA=" + passed.length + " PASS\n");
