from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / ".build-temp" / "test-open-fps-profile-145"
DATA_ROOT = TEST_ROOT / "data"
DATA_ROOT.mkdir(parents=True, exist_ok=True)

os.environ["PAOKRA_HOME"] = str(TEST_ROOT)
os.environ["PAOKRA_DATA"] = str(DATA_ROOT)
os.environ["PAOKRA_WEB"] = str(ROOT / "app" / "web")
sys.path.insert(0, str(ROOT / "app"))

import server  # noqa: E402

FIXTURES = ROOT / "qa_clipflow_20261017"
open_path = FIXTURES / "normal-a.mp4"
normal_path = FIXTURES / "normal-b.mp4"
stop_path = FIXTURES / "normal-c.mp4"
for path in (open_path, normal_path, stop_path):
    if not path.is_file():
        raise SystemExit(f"required deterministic fixture is missing: {path}")

with server.clip_flow_lock:
    server.clip_flow_config.update(
        enabled=True,
        open=str(open_path.resolve()),
        normal=[str(normal_path.resolve())],
        stop=str(stop_path.resolve()),
    )
    server.clip_flow_runtime.update(active=False, phase="idle", current_clip="")

with server.lock:
    server.state.update(
        fps_mode="source",
        fps=24,
        width=720,
        height=1280,
        source="",
        running=False,
        source_width=720,
        source_height=405,
        source_fit_mode="cover",
    )
server.source_fps = 24.0
if server.vcam.active:
    raise SystemExit("isolated runtime unexpectedly started a Virtual Camera")

original_probe = server._probe_source_timing
try:
    def fake_probe(path, fallback_fps):
        if os.path.normcase(os.path.realpath(str(path))) == os.path.normcase(os.path.realpath(str(open_path))):
            return 59.94, 8.0
        return original_probe(path, fallback_fps)

    server._probe_source_timing = fake_probe
    profile = server._ai_seam_runtime_profile()
    assert profile["fps_mode"] == "source", profile
    assert profile["fps"] == 60, {
        "expected_flow_fps_from_open": 60,
        "actual_fps": profile["fps"],
        "current_preview_fps": server.source_fps,
        "box_source": profile.get("box_source"),
    }
    assert Path(profile["box_source"]) == open_path.resolve(), profile
    assert profile["box_is_planned"] is True, profile
    print(
        "PASS source_fps_profile_uses_open_clip_cadence: "
        f"preview=24 FPS, OPEN=59.94 FPS, planned_output={profile['fps']} FPS"
    )
    print(
        "PASS first_source_box_is_planned_from_open_clip: "
        f"{profile['box_width']}x{profile['box_height']}"
    )

    with server.lock:
        server.state["fps_mode"] = "fixed"
        server.state["fps"] = 30
    fixed_profile = server._ai_seam_runtime_profile()
    assert fixed_profile["fps"] == 30, fixed_profile
    print("PASS fixed_fps_profile_is_unchanged")
finally:
    server._probe_source_timing = original_probe
    server.stop_event.set()
    with server.render_condition:
        server.render_condition.notify_all()
    with server.preview_condition:
        server.preview_condition.notify_all()
