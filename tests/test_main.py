"""
tests/test_main.py

Structural verification of app.main's build_stack and shutdown. Real
VisionCapture, VoiceListener, and pyttsx3 are monkeypatched with stubs
that record every interaction. The test asserts:

  * build_stack constructs components in the specified order
  * Voice soft-fail path kicks in when VoiceListener raises
  * shutdown walks the components in reverse and fires /stop at the end
  * shutdown is idempotent
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from app import main as app_main


# -----------------------------------------------------------------------------
# Stubs
# -----------------------------------------------------------------------------


class FakeVision:
    instances: list = []

    def __init__(self, camera_index=None, width=None, height=None, fps=30):
        FakeVision.instances.append(self)
        self.events: list[str] = ["init"]
        self.camera_index = camera_index
        self.width = width
        self.height = height

    def get_latest_frame(self):
        return np.zeros((100, 100, 3), dtype=np.uint8)

    def encode_for_claude(self, frame) -> str:
        return "stub"

    def release(self):
        self.events.append("release")


class FakeVoiceListener:
    instances: list = []
    raise_on_construct: bool = False

    def __init__(self, wake_phrase=None, model_size=None, device=None,
                 vad_threshold=None, input_device=None, on_trigger=None):
        if FakeVoiceListener.raise_on_construct:
            raise ImportError("simulated: sounddevice missing")
        FakeVoiceListener.instances.append(self)
        self.events: list[str] = ["init"]
        self.on_trigger = on_trigger

    def start(self):
        self.events.append("start")

    def stop(self):
        self.events.append("stop")


class FakeManualTrigger:
    instances: list = []

    def __init__(self, on_trigger=None):
        FakeManualTrigger.instances.append(self)
        self.events: list[str] = ["init"]
        self.on_trigger = on_trigger

    def start(self):
        self.events.append("start")

    def stop(self):
        self.events.append("stop")


@pytest.fixture(autouse=True)
def reset_stubs():
    FakeVision.instances.clear()
    FakeVoiceListener.instances.clear()
    FakeVoiceListener.raise_on_construct = False
    FakeManualTrigger.instances.clear()
    yield


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(app_main, "VisionCapture", FakeVision)
    monkeypatch.setattr(app_main, "VoiceListener", FakeVoiceListener)
    monkeypatch.setattr(app_main, "ManualTrigger", FakeManualTrigger)
    # No TTS for the dry run.
    monkeypatch.setattr(app_main, "_build_tts_engine", lambda: None)
    # Neutralize the final /stop POST so tests don't depend on network.
    fake_requests = MagicMock()
    monkeypatch.setattr(app_main, "requests", fake_requests)
    return fake_requests


def _minimal_config() -> dict:
    return {
        "camera_index": 0,
        "camera_width": 640,
        "camera_height": 480,
        "receiver_url": "http://127.0.0.1:5999",
        "voice": {
            "wake_phrase": "hey sinew",
            "whisper_model": "base",
            "whisper_device": "cpu",
            "vad_threshold": 500,
        },
        "orchestrator": {
            "abort_window_ms": 500,
            "confidence_scale": {"high": 1.0, "medium": 0.75, "low": 0.5},
        },
    }


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------


def test_build_stack_order_and_contents(patched):
    cfg = _minimal_config()
    stack = app_main.build_stack(cfg)

    # Vision built first.
    assert len(FakeVision.instances) == 1
    v = FakeVision.instances[0]
    assert v.camera_index == 0
    assert v.width == 640
    assert v.events == ["init"]

    # Orchestrator constructed and holds the stub vision.
    assert stack.orchestrator is not None
    assert stack.orchestrator._vision is v
    assert stack.tts_engine is None  # we stubbed _build_tts_engine to return None

    # Voice started.
    assert len(FakeVoiceListener.instances) == 1
    vl = FakeVoiceListener.instances[0]
    assert vl.events == ["init", "start"]
    assert stack.voice_enabled is True
    assert stack.voice_listener is vl
    # Voice's callback is wired to orchestrator.on_trigger.
    assert vl.on_trigger == stack.orchestrator.on_trigger

    # Manual trigger started after voice.
    assert len(FakeManualTrigger.instances) == 1
    mt = FakeManualTrigger.instances[0]
    assert mt.events == ["init", "start"]
    assert mt.on_trigger == stack.orchestrator.on_trigger

    # Sanity: the Orchestrator has the confidence scale from config.
    from app.state import Confidence
    assert stack.orchestrator._confidence_scale[Confidence.HIGH] == 1.0
    assert stack.orchestrator._confidence_scale[Confidence.MEDIUM] == 0.75
    assert stack.orchestrator._confidence_scale[Confidence.LOW] == 0.5


def test_build_stack_soft_fails_voice_when_deps_missing(patched):
    FakeVoiceListener.raise_on_construct = True
    cfg = _minimal_config()
    stack = app_main.build_stack(cfg)

    assert stack.voice_enabled is False
    assert stack.voice_listener is None
    # Manual trigger still up so the user can type.
    assert len(FakeManualTrigger.instances) == 1
    assert FakeManualTrigger.instances[0].events == ["init", "start"]


def test_shutdown_walks_every_component(patched):
    fake_requests = patched
    cfg = _minimal_config()
    stack = app_main.build_stack(cfg)

    # Spy on orchestrator.abort to confirm it gets called.
    abort_calls: list[str] = []
    original_abort = stack.orchestrator.abort
    def spy_abort(reason):
        abort_calls.append(reason)
    stack.orchestrator.abort = spy_abort  # type: ignore[assignment]

    app_main.shutdown(stack, reason="test shutdown")

    # Orchestrator aborted first.
    assert abort_calls == ["test shutdown"]
    # Voice and manual stopped.
    assert FakeVoiceListener.instances[0].events == ["init", "start", "stop"]
    assert FakeManualTrigger.instances[0].events == ["init", "start", "stop"]
    # Vision released.
    assert FakeVision.instances[0].events == ["init", "release"]
    # Final /stop POST fired.
    assert fake_requests.post.called
    call = fake_requests.post.call_args
    assert call.args[0].endswith("/stop") or call.args[0].endswith("/stop/")


def test_shutdown_is_idempotent(patched):
    cfg = _minimal_config()
    stack = app_main.build_stack(cfg)

    # First call fires the teardown.
    app_main.shutdown(stack, reason="first")
    first_vision_events = list(FakeVision.instances[0].events)

    # Second call should no-op because shutdown_done is True.
    app_main.shutdown(stack, reason="second")
    assert FakeVision.instances[0].events == first_vision_events


def test_shutdown_survives_component_failure(patched):
    """One component blowing up in stop() must not block later cleanup."""
    cfg = _minimal_config()
    stack = app_main.build_stack(cfg)

    # Make voice.stop explode.
    def boom():
        raise RuntimeError("voice stop failed")
    stack.voice_listener.stop = boom  # type: ignore[assignment]

    app_main.shutdown(stack, reason="robustness")

    # Manual trigger and vision still cleaned up.
    assert FakeManualTrigger.instances[0].events[-1] == "stop"
    assert FakeVision.instances[0].events[-1] == "release"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
