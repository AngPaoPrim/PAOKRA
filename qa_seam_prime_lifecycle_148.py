import os, sys, threading, shutil
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parent
QA_ROOT = ROOT / ".build-temp" / "test-prime-runtime-148"
if QA_ROOT.exists():
    shutil.rmtree(QA_ROOT)
QA_ROOT.mkdir(parents=True)
DATA_ROOT = QA_ROOT / "data"
DATA_ROOT.mkdir()
os.environ["PAOKRA_HOME"] = str(QA_ROOT)
os.environ["PAOKRA_DATA"] = str(DATA_ROOT)
os.environ["PAOKRA_WEB"] = str(ROOT / "app" / "web")
sys.path.insert(0, str(ROOT / "app"))
import server

class FakeCapture:
    def __init__(self, color=25):
        self.color=color; self.opened=True; self.reads=0; self.released=False
    def isOpened(self): return self.opened and not self.released
    def get(self, prop):
        if prop == server.cv2.CAP_PROP_FPS: return 30.0
        if prop == server.cv2.CAP_PROP_FRAME_WIDTH: return 32
        if prop == server.cv2.CAP_PROP_FRAME_HEIGHT: return 48
        if prop == server.cv2.CAP_PROP_FRAME_COUNT: return 300
        return 0.0
    def set(self, prop, value): return True
    def read(self):
        self.reads += 1
        return (True, np.full((48,32,3),self.color,dtype=np.uint8)) if self.isOpened() else (False,None)
    def release(self):
        self.released=True; self.opened=False

def check(ok,msg):
    if not ok: raise AssertionError(msg)
    print("PASS",msg,flush=True)

def stop_module():
    try:
        server._clear_source_prime_cache()
        server.stop_event.set()
        with server.render_condition: server.render_condition.notify_all()
    except Exception: pass

try:
    clip_a=QA_ROOT/"normal-a.mp4"; clip_b=QA_ROOT/"normal-b.mp4"; bridge=DATA_ROOT/"bridge.mp4"
    for f in (clip_a,clip_b,bridge): f.write_bytes(b"test media placeholder")
    captures=[]
    server._open_video_capture=lambda src: captures.append(FakeCapture(len(captures)+30)) or captures[-1]
    server._clear_source_prime_cache()
    epoch=server._source_prime_epoch
    check(server._prime_source_capture(str(clip_a),0.0,expected_epoch=epoch),"first frame is decoded into the prime cache")
    cached=server._take_source_prime(str(clip_a),0.0)
    check(cached is not None and cached["cap"].isOpened(),"prepared capture transfers to the active source without reopening")
    check(cached["first_frame"].shape==(48,32,3) and cached["cap"].reads==1,"prime retains exactly the first decoded frame")
    cached["cap"].release()

    started=threading.Event(); resume=threading.Event(); stale_caps=[]
    def delayed_open(src):
        cap=FakeCapture(99); stale_caps.append(cap); started.set(); resume.wait(3); return cap
    server._clear_source_prime_cache()
    server._open_video_capture=delayed_open
    old_epoch=server._source_prime_epoch
    result={}
    worker=threading.Thread(target=lambda: result.setdefault("ok",server._prime_source_capture(str(clip_a),0.0,expected_epoch=old_epoch)),daemon=True)
    worker.start()
    check(started.wait(3),"prime worker entered capture-open stage")
    server._clear_source_prime_cache()
    resume.set(); worker.join(5)
    check(not worker.is_alive(),"cancelled prime worker exits promptly")
    check(result.get("ok") is False and stale_caps[0].released,"stale prime closes its capture instead of re-filling a cleared cache")
    check(not server._source_prime_cache,"stop/clear epoch leaves no stale prepared entries")

    server._open_video_capture=lambda src: captures.append(FakeCapture(len(captures)+50)) or captures[-1]
    server._ai_seam_runtime_profile=lambda: {"fps":30,"fit":"cover","width":32,"height":48}
    server.find_cached_transition=lambda *a,**k: {
        "asset":str(bridge),"resume_seconds":0.5,"to_duration":10.0,
        "fps":30,"frame_count":8
    }
    server._clear_source_prime_cache()
    check(server._prime_flow_directed_pair(str(clip_a),str(clip_b)),"directed pair primes seam and destination concurrently")
    expected={server._source_prime_key(str(bridge),0.0),server._source_prime_key(str(clip_b),0.5)}
    check(expected.issubset(set(server._source_prime_cache)),"cache holds exact bridge and destination/resume pair")
    moved_b=server._take_source_prime(str(clip_b),0.5)
    check(moved_b is not None and moved_b["start_seconds"]==0.5,"destination capture is keyed at its seam-resume timestamp")
    moved_b["cap"].release()
    server._clear_source_prime_cache()
    print("PRIME_RUNTIME_QA=PASS",flush=True)
finally:
    stop_module()