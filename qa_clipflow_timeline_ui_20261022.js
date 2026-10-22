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

process.stdout.write("CLIPFLOW_TIMELINE_QA=" + passed.length + " PASS\n");
