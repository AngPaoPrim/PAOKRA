"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

const root = __dirname;
const source = fs.readFileSync(path.join(root, "app", "web", "app.js"), "utf8");
const backend = fs.readFileSync(path.join(root, "app", "server.py"), "utf8");
const aiSeam = fs.readFileSync(path.join(root, "app", "ai_seam.py"), "utf8");
const start = source.indexOf("async function ensureClipFlowSeamsBeforeStart(){");
const end = source.indexOf('\n$("#clipFlowPickOpen")', start);
assert.ok(start >= 0 && end > start, "could not extract the production preflight function");
const productionPreflight = source.slice(start, end);

const pairs = [
  { from: "Open.mp4", to: "Normal-A.mp4" },
  { from: "Normal-A.mp4", to: "Normal-B.mp4" },
];
const key = (pair) => pair.from + "->" + pair.to;

function makeContext(options = {}) {
  const state = {
    enabled: options.enabled ?? false,
    virtualcam_active: options.virtualcamActive ?? false,
    engine_running: false,
    clip_flow_active: false,
  };
  const ready = new Set(options.allReady ? pairs.map(key) : []);
  const ctx = {
    clipFlowConfig: { enabled: true, open: "Open.mp4", normal: ["Normal-A.mp4", "Normal-B.mp4"], stop: "Stop.mp4" },
    clipFlowPreflightStoppedCamera: false,
    autoMakeCalls: 0,
    autoMakeOptions: null,
    confirmCalls: 0,
    confirmMessages: [],
    posts: [],
    polls: 0,
    pairs,
    clipFlowPlayablePairs: () => pairs,
    clipFlowHasCachedSeam: (from, to) => ready.has(from + "->" + to),
    loadClipFlowSeams: async () => ({ seams: [] }),
    api: async (url) => {
      if (url !== "/api/ai-seam/status") throw new Error("unexpected API request: " + url);
      return {
        ok: true,
        engine_running: state.engine_running,
        clip_flow_active: state.clip_flow_active,
        virtualcam_active: state.virtualcam_active,
        settings: { enabled: state.enabled },
      };
    },
    confirm: (message) => {
      ctx.confirmCalls++;
      ctx.confirmMessages.push(String(message));
      return options.confirmAnswer ?? true;
    },
    post: async (url, body) => {
      ctx.posts.push({ url, body });
      if (url === "/api/virtualcam" && body.action === "start") {
        state.virtualcam_active = true;
        return { ok: true, active: true, output_fps: 30 };
      }
      if (url === "/api/virtualcam" && body.action === "stop") {
        state.virtualcam_active = false;
        return { ok: true, active: false };
      }
      throw new Error("unexpected POST: " + url);
    },
    poll: async () => { ctx.polls++; },
    clipFlowAutoMakeFrames: async (opts) => {
      ctx.autoMakeCalls++;
      ctx.autoMakeOptions = opts;
      if (options.makeSucceeds === false) return false;
      for (const pair of pairs) ready.add(key(pair));
      state.enabled = true;
      return true;
    },
    __state: state,
  };
  return ctx;
}

async function callPreflight(ctx) {
  return await vm.runInNewContext(
    productionPreflight + "\nensureClipFlowSeamsBeforeStart()",
    ctx,
    { timeout: 5000 }
  );
}

async function main() {
  let ctx = makeContext({ enabled: true, allReady: true });
  assert.equal(await callPreflight(ctx), true);
  assert.equal(ctx.autoMakeCalls, 0);
  assert.equal(ctx.confirmCalls, 0);
  console.log("PASS ready matrix starts without regenerating");

  ctx = makeContext({ enabled: false, allReady: false, confirmAnswer: true });
  assert.equal(await callPreflight(ctx), true);
  assert.equal(ctx.autoMakeCalls, 1);
  assert.equal(ctx.autoMakeOptions.automatic, true);
  assert.equal(ctx.__state.enabled, true);
  assert.equal(ctx.confirmCalls, 1);
  console.log("PASS missing matrix auto-generates once, enables AI Seam, then verifies all pairs");

  ctx = makeContext({ enabled: false, allReady: false, confirmAnswer: false });
  await assert.rejects(callPreflight(ctx), /ยกเลิก Start Engine เพราะเฟรมคั่นยังไม่พร้อม/);
  assert.equal(ctx.autoMakeCalls, 0);
  assert.deepEqual(ctx.posts.map((item) => item.body.action), ["start", "stop"]);
  assert.equal(ctx.__state.virtualcam_active, false);
  assert.equal(ctx.clipFlowPreflightStartedCamera, false);
  console.log("PASS cancelling preflight closes the temporary camera probe and blocks Engine start");

  ctx = makeContext({ enabled: false, allReady: false, confirmAnswer: true, makeSucceeds: false });
  await assert.rejects(callPreflight(ctx), /เตรียมเฟรมคั่นไม่สำเร็จ/);
  assert.equal(ctx.autoMakeCalls, 1);
  console.log("PASS generation failure blocks playback");

  ctx = makeContext({ enabled: false, allReady: false, virtualcamActive: true, confirmAnswer: true });
  assert.equal(await callPreflight(ctx), true);
  assert.equal(ctx.posts.length, 1);
  assert.equal(ctx.posts[0].url, "/api/virtualcam");
  assert.equal(ctx.posts[0].body.action, "stop");
  assert.equal(ctx.clipFlowPreflightStoppedCamera, true);
  assert.equal(ctx.polls, 1);
  console.log("PASS active Virtual Camera is stopped for generation and marked for safe restoration");

  assert.ok(source.includes('if(view==="scenes")void ensureClipFlowScenePreview()'));
  assert.ok(source.includes('post("/api/source",{source:open})'));
  const directStart = source.slice(source.indexOf('$("#clipFlowStart")?.addEventListener'), source.indexOf('$("#clipFlowStopPreview")?.addEventListener'));
  assert.ok(directStart.includes("await ensureClipFlowSeamsBeforeStart();"));
  console.log("PASS Scene Builder auto-preview and direct Playlist Start are wired to the new workflow");

  const profileStart = backend.indexOf("def _ai_seam_runtime_profile():");
  const profileEnd = backend.indexOf("def _ai_seam_release_cache_sources", profileStart);
  const profileSource = backend.slice(profileStart, profileEnd);
  assert.ok(profileStart >= 0 && profileEnd > profileStart, "runtime profile provider was not found");
  assert.match(profileSource, /and not profile\.get\("virtualcam_active"\)/);
  assert.match(profileSource, /_AI_SEAM_NEGOTIATED_FPS_OVERRIDE/);
  assert.match(backend, /AI Seam profile changed after Virtual Camera negotiation; playback was blocked to avoid hard cuts/);
  console.log("PASS runtime profile follows the actual negotiated camera FPS and blocks post-negotiation cache mismatches");

  assert.match(aiSeam, /diagnostics: dict\[str, Any\] \| None = None/);
  assert.match(aiSeam, /output FPS differs/);
  assert.match(aiSeam, /directed pair is not present in this computer's AI Seam cache/);
  assert.ok(source.includes("lookup.reason"));
  console.log("PASS no-insert diagnostics expose directed-pair and FPS mismatch reasons in the UI");
  console.log("SEAM_WORKFLOW_147=8 PASS / 0 FAIL");
}

main().catch((error) => {
  console.error(error && error.stack || error);
  process.exitCode = 1;
});
