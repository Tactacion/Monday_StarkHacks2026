"""
tests/test_integration_mock.py

End-to-end smoke test: Claude plan_grasp -> HTTP POSTs against the mock
receiver. Proves the brain's output flows through the HTTP contract that
the real hardware bridge also speaks.

Requires:
  - ANTHROPIC_API_KEY in env or .env
  - tests/mock_receiver.py running on http://localhost:5001

Prints the full trace: transcript, raw BrainResponse, each POST payload and
response. If the brain refuses (expected for placeholder fixtures that show
no object), the HTTP loop is empty and the test records that outcome.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests

from app.brain import plan_grasp
from app.vision import VisionCapture

RECEIVER_URL = "http://localhost:5001"
FIXTURE = "tests/fixtures/mug.jpg"
TRANSCRIPT = "grab the cup"


def main() -> int:
    print(f"\n=== Integration smoke: {FIXTURE} + {TRANSCRIPT!r} ===\n")

    try:
        requests.get(f"{RECEIVER_URL}/health", timeout=1.0).raise_for_status()
    except requests.RequestException as e:
        print(f"FAIL: mock receiver not reachable at {RECEIVER_URL}: {e}")
        return 1

    vision = VisionCapture()
    frame_b64 = vision.load_file_as_b64(FIXTURE)
    print(f"loaded {FIXTURE}, {len(frame_b64)} bytes base64")

    print(f"\n> plan_grasp(transcript={TRANSCRIPT!r})")
    resp = plan_grasp(frame_b64, TRANSCRIPT)
    if resp is None:
        print("FAIL: plan_grasp returned None (API/parse/validation error)")
        return 1

    print("\nBrainResponse:")
    print(f"  acknowledgement: {resp.acknowledgement!r}")
    print(f"  confidence:      {resp.confidence.value}")
    print(f"  refusal:         {resp.refusal!r}")
    print(f"  grip_type:       {resp.grip_type.value}")
    print(f"  commands:        {[c.to_dict() for c in resp.commands]}")

    if not resp.commands:
        print("\nno commands returned (refusal path). HTTP loop skipped.")
        print("PASS: refusal path handled cleanly, no HTTP traffic to mock.")
        return 0

    print(f"\nissuing {len(resp.commands)} POST(s) to {RECEIVER_URL}/stimulate")
    all_ok = True
    for i, cmd in enumerate(resp.commands):
        payload = cmd.to_dict()
        r = requests.post(f"{RECEIVER_URL}/stimulate", json=payload, timeout=2.0)
        print(f"  [{i+1}/{len(resp.commands)}] POST {payload} -> {r.status_code} {r.json()}")
        if r.status_code != 200:
            all_ok = False

    print()
    if all_ok:
        print(f"PASS: all {len(resp.commands)} POST(s) returned 200")
        return 0
    print("FAIL: at least one POST did not return 200")
    return 1


if __name__ == "__main__":
    sys.exit(main())
