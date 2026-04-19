"""
tests/test_stop_priority.py

Verifies Fix 1: /stop bypasses the serial command lock and returns quickly
even while /stimulate is mid-pulse. Also verifies that the ALL:OFF byte
write precedes the auto-OFF write scheduled by the timed pulse.

Runs against a Flask test client with a FakeSerialLink, no hardware needed.
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


def test_stop_returns_fast_during_pulse():
    client, link = _make_client()

    results = {}

    def fire_stimulate():
        t0 = time.monotonic()
        resp = client.post(
            "/stimulate",
            json={"finger": "INDEX", "action": "ON", "duration_ms": 1000},
        )
        results["stim_elapsed"] = time.monotonic() - t0
        results["stim_status"] = resp.status_code
        results["stim_body"] = resp.get_json()

    def fire_stop():
        time.sleep(0.1)  # let /stimulate finish phase A and enter phase B
        t0 = time.monotonic()
        resp = client.post("/stop", json={})
        results["stop_elapsed"] = time.monotonic() - t0
        results["stop_status"] = resp.status_code
        results["stop_body"] = resp.get_json()

    t1 = threading.Thread(target=fire_stimulate)
    t2 = threading.Thread(target=fire_stop)
    t1.start()
    t2.start()
    t1.join(timeout=3.0)
    t2.join(timeout=3.0)

    # Give the pulse tail thread time to either abort or complete.
    time.sleep(0.3)

    assert results["stim_status"] == 200, results
    assert results["stop_status"] == 200, results
    assert results["stop_elapsed"] < 0.1, (
        f"/stop took {results['stop_elapsed']*1000:.1f} ms, expected <100 ms"
    )

    # Inspect tx log ordering.
    log = link.tx_log()
    assert len(log) >= 2, log
    kinds = [entry[1] for entry in log]
    cmds = [entry[2] for entry in log]

    # Phase A of the pulse must be the first thing on the wire.
    assert cmds[0] == "FINGER:INDEX:ON"
    assert kinds[0] == "cmd"

    # ALL:OFF (priority) must arrive before any auto-OFF (cmd) for INDEX.
    priority_stop_idx = next(
        (i for i, (_, k, c) in enumerate(log) if k == "priority" and c == "ALL:OFF"),
        None,
    )
    assert priority_stop_idx is not None, f"no priority ALL:OFF in log: {log}"

    auto_off_idx = next(
        (i for i, (_, k, c) in enumerate(log) if k == "cmd" and c == "FINGER:INDEX:OFF"),
        None,
    )
    if auto_off_idx is not None:
        assert priority_stop_idx < auto_off_idx, (
            f"ALL:OFF (idx {priority_stop_idx}) must precede auto-OFF (idx {auto_off_idx})"
        )
    # If auto_off_idx is None, the pulse tail aborted correctly. Also acceptable.


def test_bad_finger_returns_400():
    client, _ = _make_client()
    resp = client.post("/stimulate", json={"finger": "THUMB", "action": "ON"})
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["status"] == "error"
    assert "THUMB" in body["reason"]


def test_bad_action_returns_400():
    client, _ = _make_client()
    resp = client.post("/stimulate", json={"finger": "INDEX", "action": "FIRE"})
    assert resp.status_code == 400
    body = resp.get_json()
    assert "FIRE" in body["reason"]


def test_duration_cap():
    client, link = _make_client()
    resp = client.post(
        "/stimulate",
        json={"finger": "MIDDLE", "action": "ON", "duration_ms": 9999},
    )
    assert resp.status_code == 200
    # The cap happens before the pulse tail runs, so the pulse tail will
    # schedule an auto-OFF after 1000 ms, not 9999 ms. We verify indirectly
    # by checking the tail fires within about 1.2s.
    time.sleep(1.2)
    cmds = [c for _, _, c in link.tx_log()]
    assert "FINGER:MIDDLE:OFF" in cmds, cmds


if __name__ == "__main__":
    test_stop_returns_fast_during_pulse()
    test_bad_finger_returns_400()
    test_bad_action_returns_400()
    test_duration_cap()
    print("all tests passed")
