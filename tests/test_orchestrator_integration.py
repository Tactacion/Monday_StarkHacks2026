"""
tests/test_orchestrator_integration.py

Runnable integration script (not a pytest). Fires three real triggers at a
real Orchestrator backed by a real Claude call, with the mock receiver
standing in for the Arduino bridge. Prints a full state and HTTP trace.

This HITS THE CLAUDE API. Three calls per run. Do not loop while debugging.
Requires ANTHROPIC_API_KEY in env or .env. Model picked via SINEW_CLAUDE_MODEL
(default Haiku 4.5 for cost).

Run: python tests/test_orchestrator_integration.py
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Default to Haiku for this script unless the operator overrides.
os.environ.setdefault("SINEW_CLAUDE_MODEL", "claude-haiku-4-5-20251001")

import base64

import numpy as np
from werkzeug.serving import make_server

from app.orchestrator import Orchestrator
from app.state import SystemState, TriggerEvent
from tests.mock_receiver import create_mock_app


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


class FixtureVision:
    """
    Stands in for VisionCapture. get_latest_frame returns a dummy numpy
    frame (the orchestrator only passes it to encode_for_claude, which we
    override to return a preloaded fixture's base64 string).
    """

    def __init__(self, fixture_path: Path) -> None:
        with fixture_path.open("rb") as f:
            self._b64 = base64.b64encode(f.read()).decode("ascii")
        self._fake_frame = np.zeros((100, 100, 3), dtype=np.uint8)

    def get_latest_frame(self):
        return self._fake_frame

    def encode_for_claude(self, frame) -> str:
        return self._b64


def _state_watcher(orch: Orchestrator, stop_flag: threading.Event, label: str) -> None:
    """Poll state every 20 ms and print the transition trail."""
    last = None
    while not stop_flag.is_set():
        s = orch.get_state()
        if s is not last:
            ts = time.strftime("%H:%M:%S")
            print(f"  [{ts}] state: {s.value}")
            last = s
        time.sleep(0.02)


def run_case(label: str, fixture: str, transcript: str) -> None:
    print(f"\n{'='*72}")
    print(f"CASE: {label}")
    print(f"  fixture:    {fixture}")
    print(f"  transcript: {transcript!r}")
    print("-" * 72)

    vision = FixtureVision(_REPO_ROOT / fixture)
    app = create_mock_app()
    server = _ThreadedServer(app)
    server.start()
    time.sleep(0.05)

    try:
        orch = Orchestrator(
            vision=vision,
            receiver_url=RECEIVER_URL,
            tts_engine=None,
            abort_window_ms=300,
        )

        stop_watch = threading.Event()
        watcher = threading.Thread(
            target=_state_watcher, args=(orch, stop_watch, label), daemon=True
        )
        watcher.start()

        orch.on_trigger(TriggerEvent(transcript=transcript, timestamp=time.time()))

        ok = orch.wait_for_idle(timeout_s=60.0)
        if not ok:
            # Worker is still running. Abort it so it stops touching the
            # port once we shut the mock down. Otherwise a slow Claude
            # response lands on the next case's server.
            orch.abort("integration script timeout")
            ok = orch.wait_for_idle(timeout_s=5.0)
        stop_watch.set()
        watcher.join(timeout=1.0)

        print(f"  settled in IDLE: {ok}")

        resp = orch.get_last_response()
        if resp is None:
            print("  BrainResponse: None (API or validator failure)")
        else:
            print(f"  BrainResponse.confidence: {resp.confidence.value}")
            print(f"  BrainResponse.refusal:    {resp.refusal!r}")
            print(f"  BrainResponse.grip_type:  {resp.grip_type.value}")
            print(f"  BrainResponse.ack:        {resp.acknowledgement!r}")
            print(f"  BrainResponse.commands:   "
                  f"{[c.__dict__ for c in resp.commands]}")

        print("  HTTP trace (mock receiver request log):")
        for i, (path, payload) in enumerate(app.config["REQUESTS"]):
            print(f"    [{i+1}] {path}  {payload}")
        if not app.config["REQUESTS"]:
            print("    (none)")
    finally:
        server.stop()


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set. Put it in .env or export it first.")
        return 1

    print(f"model: {os.environ['SINEW_CLAUDE_MODEL']}")
    print(f"receiver: {RECEIVER_URL} (mock)")

    run_case("high confidence cylindrical", "tests/fixtures/mug.jpg", "grab the cup")
    time.sleep(0.5)
    run_case("refusal", "tests/fixtures/unsafe.jpg", "grab the knife")
    time.sleep(0.5)
    run_case("uncertain scaled execution", "tests/fixtures/ambiguous.jpg", "grab it")

    print(f"\n{'='*72}")
    print("integration script done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
