"""
tests/measure_stop_latency.py

Direct measurement of /stop HTTP latency while a timed pulse is in flight.
This is the path the GUI's STOP ALL button takes: button click triggers
POST /stop on a daemon thread, and the measured quantity is the HTTP
request to response roundtrip on that thread.

Runs N iterations against a Flask test client with FakeSerialLink so the
numbers reflect the bridge's locking model, not real Arduino latency.
Real serial adds a few ms on top, well under the 100 ms budget.
"""

from __future__ import annotations

import statistics
import sys
import threading
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from hardware.receiver import create_app  # noqa: E402
from tests.fake_link import FakeSerialLink  # noqa: E402


ITERATIONS = 20
PULSE_MS = 1000
STOP_DELAY_S = 0.100
BUDGET_S = 0.100


def run_one(client, link: FakeSerialLink) -> tuple[float, bool]:
    """Start a 1000 ms pulse, wait 100 ms, fire /stop, return latency and priority ordering."""
    results = {}

    def fire_stimulate():
        client.post(
            "/stimulate",
            json={"finger": "INDEX", "action": "ON", "duration_ms": PULSE_MS},
        )

    def fire_stop():
        time.sleep(STOP_DELAY_S)
        t0 = time.monotonic()
        client.post("/stop", json={})
        results["stop_elapsed"] = time.monotonic() - t0

    t1 = threading.Thread(target=fire_stimulate)
    t2 = threading.Thread(target=fire_stop)
    t1.start()
    t2.start()
    t1.join(timeout=3.0)
    t2.join(timeout=3.0)

    # Give the pulse tail thread a moment to abort.
    time.sleep(0.200)

    log = link.tx_log()
    priority_idx = next(
        (i for i, (_, k, c) in enumerate(log) if k == "priority" and c == "ALL:OFF"),
        None,
    )
    auto_off_idx = next(
        (i for i, (_, k, c) in enumerate(log) if k == "cmd" and c == "FINGER:INDEX:OFF"),
        None,
    )
    priority_first = priority_idx is not None and (auto_off_idx is None or priority_idx < auto_off_idx)

    return results["stop_elapsed"], priority_first


def main() -> int:
    latencies: list[float] = []
    orderings: list[bool] = []

    for i in range(ITERATIONS):
        link = FakeSerialLink()
        app = create_app(link=link, auto_connect=False)
        app.config["TESTING"] = True
        client = app.test_client()
        latency, priority_first = run_one(client, link)
        latencies.append(latency)
        orderings.append(priority_first)
        status = "OK " if latency < BUDGET_S and priority_first else "FAIL"
        print(f"  iter {i+1:2d}  latency={latency*1000:6.1f} ms  priority_first={priority_first}  {status}")

    ms = [x * 1000 for x in latencies]
    print()
    print(f"samples:        {len(ms)}")
    print(f"min:            {min(ms):.1f} ms")
    print(f"max:            {max(ms):.1f} ms")
    print(f"mean:           {statistics.mean(ms):.1f} ms")
    print(f"median:         {statistics.median(ms):.1f} ms")
    print(f"stdev:          {statistics.stdev(ms):.1f} ms")
    print(f"budget:         {BUDGET_S*1000:.0f} ms")
    print(f"over budget:    {sum(1 for m in ms if m >= BUDGET_S*1000)} / {len(ms)}")
    print(f"priority first: {sum(orderings)} / {len(orderings)}")

    worst = max(ms)
    all_priority = all(orderings)
    passed = worst < BUDGET_S * 1000 and all_priority
    print()
    print(f"result: {'PASS' if passed else 'FAIL'} (worst case {worst:.1f} ms, budget {BUDGET_S*1000:.0f} ms)")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
