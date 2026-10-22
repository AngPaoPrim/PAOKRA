import os, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEST_ROOT = ROOT / ".build-temp" / "test-makeframes-stability-145"
DATA_ROOT = TEST_ROOT / "data"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["PAOKRA_HOME"] = str(TEST_ROOT)
os.environ["PAOKRA_DATA"] = str(DATA_ROOT)
os.environ["PAOKRA_WEB"] = str(ROOT / "app" / "web")
sys.path.insert(0, str(ROOT / "app"))

import server

client = server.app.test_client()
with server.lock:
    server.state["running"] = False
    server.state["virtualcam"] = False
with server.clip_flow_lock:
    server.clip_flow_runtime.update(active=False, phase="idle", current_clip="")
with server.ai_seam_batch_guard_lock:
    server.ai_seam_batch_guard.update(token="", lease_until=0.0, started_at=0.0)

res = client.post("/api/ai-seam/batch-lock", json={"action": "acquire"})
assert res.status_code == 200, (res.status_code, res.get_json())
token = res.get_json()["token"]
assert token and len(token) >= 16
print("PASS: idle backend can reserve the entire Auto Make Frames batch")

second_reservation = client.post("/api/ai-seam/batch-lock", json={"action": "acquire"})
assert second_reservation.status_code == 409, (second_reservation.status_code, second_reservation.get_json())
wrong_release = client.post("/api/ai-seam/batch-lock", json={"action": "release", "token": "not-the-owner"})
assert wrong_release.status_code == 409 and server._ai_seam_batch_is_active(), (
    wrong_release.status_code, wrong_release.get_json()
)
print("PASS: a second UI cannot steal or release another batch reservation")

checks = [
    ("Engine start", "/api/control", {"action": "start"}),
    ("Virtual Camera start", "/api/virtualcam", {"action": "start"}),
    ("regular source seek", "/api/source/seek", {"position_seconds": 1.0}),
    ("Clip Flow seek", "/api/clip-flow/seek", {"position_seconds": 1.0}),
    ("Scene/Output settings", "/api/settings", {}),
    ("Clip Flow playlist edit", "/api/clip-flow/config", {}),
    ("active source switch", "/api/source", {"source": "X:/does-not-exist.mp4"}),
    ("AI Seam settings without token", "/api/ai-seam/settings", {"enabled": False}),
    ("AI Seam clips without token", "/api/ai-seam/clips", {"paths": []}),
    ("AI Seam generation without token", "/api/ai-seam/generate", {"from_id": "fake-a", "to_id": "fake-b"}),
]
for label, route, payload in checks:
    response = client.post(route, json=payload)
    assert response.status_code == 409, (label, response.status_code, response.get_json())
    print("PASS: reserved batch blocks " + label)

settings_response = client.post(
    "/api/ai-seam/settings",
    json={"enabled": True, "batch_token": token},
)
assert settings_response.status_code == 200, (settings_response.status_code, settings_response.get_json())
clips_response = client.post(
    "/api/ai-seam/clips",
    json={"paths": [], "batch_token": token},
)
assert clips_response.status_code == 200 and clips_response.get_json().get("ok") is True, (
    clips_response.status_code, clips_response.get_json()
)
bad_generate = client.post(
    "/api/ai-seam/generate",
    json={"from_id": "fake-a", "to_id": "fake-b", "batch_token": token},
)
assert bad_generate.status_code == 404, (bad_generate.status_code, bad_generate.get_json())
print("PASS: owner-token batch calls work; invalid clip pair starts no generation job")

heartbeat = client.post("/api/ai-seam/batch-lock", json={"action": "heartbeat", "token": token})
assert heartbeat.status_code == 200 and heartbeat.get_json().get("ok") is True, (
    heartbeat.status_code, heartbeat.get_json()
)
released = client.post("/api/ai-seam/batch-lock", json={"action": "release", "token": token})
assert released.status_code == 200 and released.get_json().get("released") is True, (
    released.status_code, released.get_json()
)
assert not server._ai_seam_batch_is_active()
print("PASS: heartbeat extends the lease and release unlocks controls")

with server.ai_seam_batch_guard_lock:
    server.ai_seam_batch_guard.update(token="expired-test", lease_until=time.monotonic() - 1, started_at=0.0)
assert not server._ai_seam_batch_is_active()
assert server.ai_seam_batch_guard["token"] == ""
print("PASS: expired reservation recovers automatically")

server.stop_event.set()
with server.render_condition:
    server.render_condition.notify_all()
with server.preview_condition:
    server.preview_condition.notify_all()
print("MAKEFRAMES_STABILITY_145=PASS")
