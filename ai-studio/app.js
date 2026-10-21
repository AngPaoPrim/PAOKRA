const $ = (id) => document.getElementById(id);
let inputPath = "";
let outputPath = "";
let busy = false;

function setProgress(p, stage, message) {
  $("bar").style.width = p + "%";
  $("percent").textContent = Math.round(p) + "%";
  $("stage").textContent = stage || "กำลังทำงาน";
  $("message").textContent = message || "";
}

$("pickBtn").addEventListener("click", async () => {
  const p = await window.paokraAI.chooseVideo();
  if (!p) return;
  inputPath = p;
  $("inputPath").textContent = p;
  $("startBtn").disabled = false;
  $("openBtn").disabled = true;
  outputPath = "";
  $("outputPath").textContent = "ค่าเริ่มต้น: โฟลเดอร์ Videos";
  $("preview").src = "file:///" + p.replaceAll("\\", "/").replaceAll("#", "%23");
  setProgress(0, "พร้อม", "เลือกวิดีโอแล้ว กดเริ่ม AI Enhance");
});

$("outputBtn").addEventListener("click", async () => {
  const parts = inputPath ? inputPath.split(/[\\/]/) : [];
  const name = parts.length ? parts[parts.length - 1] : "video.mp4";
  const dot = name.lastIndexOf(".");
  const base = (dot > 0 ? name.slice(0, dot) : name) + "_AI.mp4";
  const p = await window.paokraAI.chooseOutput(base);
  if (!p) return;
  outputPath = p;
  $("outputPath").textContent = p;
});

$("startBtn").addEventListener("click", async () => {
  if (!inputPath || busy) return;
  busy = true;
  $("startBtn").disabled = true;
  $("pickBtn").disabled = true;
  $("outputBtn").disabled = true;
  $("cancelBtn").disabled = false;
  $("openBtn").disabled = true;
  setProgress(1, "เริ่ม", "กำลังเตรียม AI…");

  const result = await window.paokraAI.enhance({
    input: inputPath,
    output: outputPath || undefined,
    scale: Number($("scale").value),
    tile: Number($("tile").value),
    model: $("model") ? $("model").value : "fast",
    vram: $("vram") ? $("vram").value : "auto"
  });

  busy = false;
  $("startBtn").disabled = false;
  $("pickBtn").disabled = false;
  $("outputBtn").disabled = false;
  $("cancelBtn").disabled = true;

  if (result.ok) {
    outputPath = result.result.output;
    $("outputPath").textContent = outputPath;
    $("openBtn").disabled = false;
    setProgress(100, "เสร็จแล้ว", "AI Enhance สำเร็จ · " + result.result.totalFrames + " เฟรม · " + result.result.scale + "×");
  } else if (result.error === "ยกเลิกแล้ว") {
    setProgress(0, "ยกเลิกแล้ว", "หยุดการประมวลผลแล้ว และล้างงานชั่วคราวเรียบร้อย");
  } else {
    setProgress(0, "เกิดข้อผิดพลาด", result.error);
  }
});

$("cancelBtn").addEventListener("click", async () => {
  $("cancelBtn").disabled = true;
  $("message").textContent = "กำลังหยุด process…";
  await window.paokraAI.cancel();
});

$("openBtn").addEventListener("click", async () => {
  if (outputPath) await window.paokraAI.openOutput(outputPath);
});

window.paokraAI.onProgress((d) => setProgress(d.percent, d.stage, d.message));