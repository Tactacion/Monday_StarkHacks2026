"""
tests/test_orchestrator.py

Pytest suite for app.orchestrator.Orchestrator.

Strategy:
  * FakeVisionCapture returns a fixed frame and fixed base64 string.
  * FakeBrain returns a preloaded BrainResponse; tests swap it per case.
  * Mock receiver Flask app runs in-process on 127.0.0.1:5999 via werkzeug
    make_server on a background thread. Each test gets a fresh app so the
    request log starts empty.

Timing:
  * The mock's /stimulate has a configurable sleep. Tests that need to
    abort mid-execution set this to 0.1 s per command so abort() has a
    real window before the loop finishes.
  * The orchestrator's abort_window_ms defaults to 50 ms in most tests to
    keep the suite fast. The abort-window test uses 400 ms so the test
    thread has time to act.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np
import pytest
from werkzeug.serving import make_server

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from app.orchestrator import Orchestrator
from app.state import (
    Action,
    BrainResponse,
    Command,
    Confidence,
    Finger,
    SystemState,
    TriggerEvent,
)
from tests.mock_receiver import create_mock_app


# -----------------------------------------------------------------------------
# Test doubles
# -----------------------------------------------------------------------------


class FakeVisionCapture:
    """Matches the subset of VisionCapture the orchestrator calls."""

    def __init__(self) -> None:
        self.frame = np.zeros((100, 100, 3), dtype=np.uint8)
        self.b64 = "ZmFrZQ=="  # base64 of "fake"

    def get_latest_frame(self) -> np.ndarray:
        return self.frame

    def encode_for_claude(self, frame: np.ndarray) -> str:
        return self.b64


class FakeBrain:
    """Callable planner. Set .response (or .raise_exc) per test."""

    def __init__(self) -> None:
        self.response: Optional[BrainResponse] = None
        self.raise_exc: Optional[Exception] = None
        self.calls: list[tuple[str, str]] = []

    def __call__(self, frame_b64: str, transcript: str) -> Optional[BrainResponse]:
        self.calls.append((frame_b64, transcript))
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.response


# -----------------------------------------------------------------------------
# Mock server fixture
# -----------------------------------------------------------------------------


MOCK_HOST = "127.0.0.1"
MOCK_PORT = 5999
RECEIVER_URL = f"http://{MOCK_HOST}:{MOCK_PORT}"


class _ThreadedServer:
    def __init__(self, app):
        self.app = app
        self.srv = make_server(MOCK_HOST, MOCK_PORT, app)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.srv.shutdown()
        self.thread.join(timeout=2.0)


@pytest.fixture
def mock_server():
    app = create_mock_app()
    server = _ThreadedServer(app)
    server.start()
    # Tiny settle so the socket is accepting.
    time.sleep(0.05)
    try:
        yield app
    finally:
        server.stop()


# -----------------------------------------------------------------------------
# Helpers to build BrainResponses
# -----------------------------------------------------------------------------


def _cylindrical_high(duration_ms: int = 400) -> BrainResponse:
    return BrainResponse(
        acknowledgement="grabbing the cup with a cylindrical grip",
        confidence=Confidence.HIGH,
        refusal=None,
        commands=[
            Command(Finger.INDEX, Action.ON, duration_ms),
            Command(Finger.MIDDLE, Action.ON, duration_ms),
            Command(Finger.PINKY, Action.ON, duration_ms),
        ],
    )


def _cylindrical_medium(duration_ms: int = 400) -> BrainResponse:
    return BrainResponse(
        acknowledgement="grabbing the can, say stop if wrong",
        confidence=Confidence.MEDIUM,
        refusal=None,
        commands=[
            Command(Finger.INDEX, Action.ON, duration_ms),
            Command(Finger.MIDDLE, Action.ON, duration_ms),
            Command(Finger.PINKY, Action.ON, duration_ms),
        ],
    )


def _refusal() -> BrainResponse:
    return BrainResponse(
        acknowledgement="I see a knife. I cannot assist with sharp objects.",
        confidence=Confidence.HIGH,
        refusal="Sharp object detected.",
        commands=[],
    )


def _stimulate_posts(app) -> list[dict]:
    return [p for path, p in app.config["REQUESTS"] if path == "/stimulate"]


def _stop_posts(app) -> list[dict]:
    return [p for path, p in app.config["REQUESTS"] if path == "/stop"]


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------


def test_idle_to_executing_happy_path(mock_server):
    brain = FakeBrain()
    brain.response = _cylindrical_high(duration_ms=400)
    vision = FakeVisionCapture()
    orch = Orchestrator(
        vision=vision,
        receiver_url=RECEIVER_URL,
        abort_window_ms=50,
        planner=brain,
    )

    orch.on_trigger(TriggerEvent(transcript="grab the cup", timestamp=time.time()))
    assert orch.wait_for_idle(timeout_s=5.0), f"state stuck at {orch.get_state()}"

    posts = _stimulate_posts(mock_server)
    assert len(posts) == 3, f"expected 3 /stimulate, got {len(posts)}: {posts}"
    assert [p["finger"] for p in posts] == ["INDEX", "MIDDLE", "PINKY"]
    assert all(p["action"] == "ON" for p in posts)
    # HIGH confidence -> scale 1.0 -> unscaled durations
    assert all(p["duration_ms"] == 400 for p in posts), posts
    assert _stop_posts(mock_server) == []


def test_refusal_path(mock_server):
    brain = FakeBrain()
    brain.response = _refusal()
    vision = FakeVisionCapture()
    orch = Orchestrator(
        vision=vision,
        receiver_url=RECEIVER_URL,
        abort_window_ms=50,
        planner=brain,
    )

    orch.on_trigger(TriggerEvent(transcript="grab the knife", timestamp=time.time()))
    assert orch.wait_for_idle(timeout_s=5.0)

    assert _stimulate_posts(mock_server) == []
    assert _stop_posts(mock_server) == []


def test_abort_during_window(mock_server):
    brain = FakeBrain()
    brain.response = _cylindrical_high()
    vision = FakeVisionCapture()
    orch = Orchestrator(
        vision=vision,
        receiver_url=RECEIVER_URL,
        abort_window_ms=400,  # plenty of time for the test to abort
        planner=brain,
    )

    orch.on_trigger(TriggerEvent(transcript="grab the cup", timestamp=time.time()))

    # Wait until we reach ACKNOWLEDGING (which is when the window starts).
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if orch.get_state() is SystemState.ACKNOWLEDGING:
            break
        time.sleep(0.01)
    assert orch.get_state() is SystemState.ACKNOWLEDGING, f"never reached ACK, state={orch.get_state()}"

    orch.abort("user hit STOP")
    assert orch.wait_for_idle(timeout_s=3.0)

    assert _stimulate_posts(mock_server) == []
    assert len(_stop_posts(mock_server)) >= 1


def test_abort_during_execution(mock_server):
    # Slow down each /stimulate so the abort has time to land mid-sequence.
    mock_server.config["STIMULATE_SLEEP_S"] = 0.15

    brain = FakeBrain()
    brain.response = BrainResponse(
        acknowledgement="grabbing the cup with a cylindrical grip",
        confidence=Confidence.HIGH,
        refusal=None,
        commands=[
            Command(Finger.INDEX, Action.ON, 200),
            Command(Finger.MIDDLE, Action.ON, 200),
            Command(Finger.PINKY, Action.ON, 200),
            Command(Finger.INDEX, Action.OFF, 100),
            Command(Finger.MIDDLE, Action.OFF, 100),
            Command(Finger.PINKY, Action.OFF, 100),
        ],
    )
    vision = FakeVisionCapture()
    orch = Orchestrator(
        vision=vision,
        receiver_url=RECEIVER_URL,
        abort_window_ms=50,
        planner=brain,
    )

    orch.on_trigger(TriggerEvent(transcript="grab the cup", timestamp=time.time()))

    # Wait until EXECUTING.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if orch.get_state() is SystemState.EXECUTING:
            break
        time.sleep(0.01)
    assert orch.get_state() is SystemState.EXECUTING

    # Let one or two POSTs fire, then abort.
    time.sleep(0.15)
    orch.abort("user hit STOP mid-exec")
    assert orch.wait_for_idle(timeout_s=5.0)

    posts = _stimulate_posts(mock_server)
    assert 0 < len(posts) < 6, f"expected partial sequence, got {len(posts)}: {posts}"
    assert len(_stop_posts(mock_server)) >= 1


def test_confidence_scaling(mock_server):
    brain = FakeBrain()
    brain.response = _cylindrical_medium(duration_ms=400)
    vision = FakeVisionCapture()
    orch = Orchestrator(
        vision=vision,
        receiver_url=RECEIVER_URL,
        abort_window_ms=50,
        planner=brain,
    )

    orch.on_trigger(TriggerEvent(transcript="grab it", timestamp=time.time()))
    assert orch.wait_for_idle(timeout_s=5.0)

    posts = _stimulate_posts(mock_server)
    assert len(posts) == 3
    # MEDIUM scale is 0.75 -> 400 * 0.75 = 300
    assert all(p["duration_ms"] == 300 for p in posts), posts


def test_trigger_ignored_when_not_idle(mock_server):
    # Slow /stimulate so the first trigger is still executing when the second arrives.
    mock_server.config["STIMULATE_SLEEP_S"] = 0.3

    brain = FakeBrain()
    brain.response = _cylindrical_high(duration_ms=100)
    vision = FakeVisionCapture()
    orch = Orchestrator(
        vision=vision,
        receiver_url=RECEIVER_URL,
        abort_window_ms=50,
        planner=brain,
    )

    orch.on_trigger(TriggerEvent(transcript="grab one", timestamp=time.time()))

    # Wait briefly so state leaves IDLE.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if orch.get_state() is not SystemState.IDLE:
            break
        time.sleep(0.01)
    assert orch.get_state() is not SystemState.IDLE

    # Second trigger should be ignored, no crash.
    orch.on_trigger(TriggerEvent(transcript="grab two", timestamp=time.time()))

    assert orch.wait_for_idle(timeout_s=10.0)

    # FakeBrain should have been called once, not twice.
    assert len(brain.calls) == 1
    assert brain.calls[0][1] == "grab one"
    posts = _stimulate_posts(mock_server)
    assert len(posts) == 3, f"expected 3 POSTs for first trigger only, got {len(posts)}"


def test_get_recent_commands(mock_server):
    brain = FakeBrain()
    brain.response = BrainResponse(
        acknowledgement="five step sequence",
        confidence=Confidence.HIGH,
        refusal=None,
        commands=[
            Command(Finger.INDEX, Action.ON, 100),
            Command(Finger.MIDDLE, Action.ON, 100),
            Command(Finger.PINKY, Action.ON, 100),
            Command(Finger.INDEX, Action.OFF, 50),
            Command(Finger.MIDDLE, Action.OFF, 50),
        ],
    )
    vision = FakeVisionCapture()
    orch = Orchestrator(
        vision=vision,
        receiver_url=RECEIVER_URL,
        abort_window_ms=50,
        planner=brain,
    )

    orch.on_trigger(TriggerEvent(transcript="five step", timestamp=time.time()))
    assert orch.wait_for_idle(timeout_s=5.0)

    # All five should have POSTed, but get_recent_commands(3) returns the
    # last three in order.
    last3 = orch.get_recent_commands(3)
    assert len(last3) == 3
    assert [(c.finger, c.action) for c in last3] == [
        (Finger.PINKY, Action.ON),
        (Finger.INDEX, Action.OFF),
        (Finger.MIDDLE, Action.OFF),
    ]

    # Ring buffer caps at 16, but the 5 from this run should all fit.
    assert len(orch.get_recent_commands(n=16)) == 5


def test_brain_none_handled(mock_server):
    brain = FakeBrain()
    brain.response = None
    vision = FakeVisionCapture()
    orch = Orchestrator(
        vision=vision,
        receiver_url=RECEIVER_URL,
        abort_window_ms=50,
        planner=brain,
    )

    orch.on_trigger(TriggerEvent(transcript="fuzzy intent", timestamp=time.time()))
    assert orch.wait_for_idle(timeout_s=5.0)

    assert _stimulate_posts(mock_server) == []
    assert _stop_posts(mock_server) == []
    assert orch.get_last_response() is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
