"""
tests/test_concurrent_stimulate.py

Verifies Fix 2: /stimulate no longer blocks /status during the sleep phase
of a timed pulse. Phase A holds the serial command lock only long enough
for one ACK roundtrip, then returns. Phase B (sleep) and Phase C (auto-OFF)
run on a background thread and release the lock between them.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from hardware.receiver import create_app  # noqa: E402
from tests.fake_link import FakeSerialLink  # noqa: E402


def _make_client():
    link = FakeSerialLink()
    app = create_app(link=link, auto_connect=False)
    app.config["TESTING"] = True
    return app.test_client(), link


def test_status_not_blocked_by_pulse_sleep():
    client, _ = _make_client()

    results = {}

    def fire_stimulate():
        t0 = time.monotonic()
        resp = client.post(
            "/stimulate",
            json={"finger": "INDEX", "action": "ON", "duration_ms": 800},
        )
        results["stim_elapsed"] = time.monotonic() - t0
        results["stim_status"] = resp.status_code

    def fire_status():
        time.sleep(0.2)  # let phase A complete
        t0 = time.monotonic()
        resp = client.get("/status")
        results["status_elapsed"] = time.monotonic() - t0
        results["status_status"] = resp.status_code
        results["status_body"] = resp.get_json()

    t1 = threading.Thread(target=fire_stimulate)
    t2 = threading.Thread(target=fire_status)
    t1.start()
    t2.start()
    t1.join(timeout=3.0)
    t2.join(timeout=3.0)

    # Wait for the pulse tail to finish so we do not leak threads into the
    # next test. 800ms pulse + margin.
    time.sleep(1.0)

    assert results["stim_status"] == 200, results
    assert results["status_status"] == 200, results

    # /stimulate must return quickly (phase A only, not the full 800 ms).
    assert results["stim_elapsed"] < 0.2, (
        f"/stimulate took {results['stim_elapsed']*1000:.1f} ms, "
        f"expected <200 ms (phase A should return immediately)"
    )

    # /status must return well under 100 ms even though a pulse is sleeping.
    assert results["status_elapsed"] < 0.1, (
        f"/status took {results['status_elapsed']*1000:.1f} ms, expected <100 ms"
    )


def test_pulse_tail_fires_auto_off():
    """Happy path: a 300 ms pulse without /stop should produce both ON and OFF."""
    client, link = _make_client()
    resp = client.post(
        "/stimulate",
        json={"finger": "PINKY", "action": "ON", "duration_ms": 300},
    )
    assert resp.status_code == 200
    time.sleep(0.6)  # pulse + margin
    cmds = [c for _, _, c in link.tx_log()]
    assert "FINGER:PINKY:ON" in cmds, cmds
    assert "FINGER:PINKY:OFF" in cmds, cmds
    on_idx = cmds.index("FINGER:PINKY:ON")
    off_idx = cmds.index("FINGER:PINKY:OFF")
    assert on_idx < off_idx


if __name__ == "__main__":
    test_status_not_blocked_by_pulse_sleep()
    test_pulse_tail_fires_auto_off()
    print("all tests passed")
