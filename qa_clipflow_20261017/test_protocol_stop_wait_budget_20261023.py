from __future__ import annotations

"""Focused regression QA for the LIVE-stop wait budget (v1.4.5 beta)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import server  # noqa: E402


class CapturingDoneEvent:
    def __init__(self) -> None:
        self.timeout: float | None = None

    def wait(self, timeout: float | None = None) -> bool:
        self.timeout = float(timeout or 0.0)
        return True


def run_case(name: str, started: dict, runtime: dict, expected_timeout: float) -> None:
    original_begin = server._clip_flow_begin_stop
    original_event = server.clip_flow_done_event
    original_runtime = dict(server.clip_flow_runtime)
    captured = CapturingDoneEvent()
    try:
        server._clip_flow_begin_stop = lambda reason="qa": dict(started)
        server.clip_flow_done_event = captured
        server.clip_flow_runtime.clear()
        server.clip_flow_runtime.update(runtime)
        result = server._clip_flow_play_stop_and_wait(reason="qa-test")
        assert result.get("completed") is True, f"{name}: helper did not complete: {result!r}"
        assert captured.timeout is not None, f"{name}: wait timeout was not captured"
        assert abs(captured.timeout - expected_timeout) < 0.001, (
            f"{name}: expected timeout {expected_timeout}, got {captured.timeout}"
        )
        print(f"PASS {name}: timeout={captured.timeout:.3f}s", flush=True)
    finally:
        server._clip_flow_begin_stop = original_begin
        server.clip_flow_done_event = original_event
        server.clip_flow_runtime.clear()
        server.clip_flow_runtime.update(original_runtime)


def main() -> int:
    common = {"last_error": ""}
    run_case(
        "seam-to-normal Stop budget includes the unplayed destination clip",
        {"ok": True, "stop_pending": True, "phase": "seam",
         "current_remaining_seconds": 5.0, "stop_duration_seconds": 30.0},
        {**common, "seam_next_role": "normal", "seam_audio_duration": 20.0,
         "seam_resume_seconds": 8.0, "seam_next_path": "normal-b.mp4"},
        77.0,
    )
    run_case(
        "normal-to-Stop seam does not double-count a destination Normal",
        {"ok": True, "stop_pending": True, "phase": "seam",
         "current_remaining_seconds": 5.0, "stop_duration_seconds": 30.0},
        {**common, "seam_next_role": "stop", "seam_audio_duration": 20.0,
         "seam_resume_seconds": 8.0, "seam_next_path": "stop.mp4"},
        65.0,
    )
    run_case(
        "ordinary Normal Stop budget remains current clip plus Stop",
        {"ok": True, "stop_pending": True, "phase": "normal",
         "current_remaining_seconds": 5.0, "stop_duration_seconds": 30.0},
        {**common, "phase": "normal"},
        65.0,
    )
    run_case(
        "long clips are not cut off by an arbitrary two-hour timeout cap",
        {"ok": True, "stop_pending": True, "phase": "normal",
         "current_remaining_seconds": 7300.0, "stop_duration_seconds": 30.0},
        {**common, "phase": "normal"},
        7360.0,
    )
    print("PROTOCOL_STOP_WAIT_BUDGET_145=PASS (4/4)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
