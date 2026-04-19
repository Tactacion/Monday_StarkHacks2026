"""
Sinew Hardware Test — run through all commands step by step.
Tests: individual fingers, combos, dance, sequences, orchestrator macros.

Usage: PI_HOST=10.127.152.133 python3 laptop/test_hardware.py
"""

import os
import sys
import time
import requests

PI_HOST = os.environ.get("PI_HOST", "10.127.152.133")
BASE = f"http://{PI_HOST}:5001"
session = requests.Session()


def post(endpoint, data=None):
    try:
        r = session.post(f"{BASE}{endpoint}", json=data, timeout=5)
        print(f"  POST {endpoint} {data or ''} → {r.status_code} {r.json()}")
        return r.json()
    except Exception as e:
        print(f"  FAILED: {e}")
        return None


def wait(msg, sec=1.0):
    print(f"\n{'='*50}")
    print(f"  {msg}")
    print(f"{'='*50}")
    input("  Press ENTER to run (or Ctrl+C to skip)... ")


def test_connection():
    print("\n--- Connection Check ---")
    try:
        r = session.get(f"{BASE}/health", timeout=3)
        health = r.json()
        print(f"  Health: {health}")
        r2 = session.get(f"{BASE}/status", timeout=3)
        status = r2.json()
        print(f"  Status: {status}")
        if not health.get("ok"):
            print("  WARNING: Arduino not connected to Pi!")
            return False
        return True
    except Exception as e:
        print(f"  Cannot reach Pi: {e}")
        return False


def test_individual_fingers():
    wait("TEST 1: Individual fingers — INDEX for 300ms")
    post("/stimulate", {"finger": "INDEX", "action": "ON", "duration_ms": 300})
    time.sleep(0.5)

    wait("TEST 2: Individual fingers — MIDDLE for 300ms")
    post("/stimulate", {"finger": "MIDDLE", "action": "ON", "duration_ms": 300})
    time.sleep(0.5)

    wait("TEST 3: Individual fingers — PINKY for 300ms")
    post("/stimulate", {"finger": "PINKY", "action": "ON", "duration_ms": 300})
    time.sleep(0.5)


def test_stop():
    wait("TEST 4: Turn INDEX ON, then STOP ALL after 1 second")
    post("/stimulate", {"finger": "INDEX", "action": "ON"})
    print("  INDEX is ON... waiting 1s...")
    time.sleep(1)
    post("/stop")


def test_dance():
    wait("TEST 5: DANCE — built-in Arduino sequence")
    post("/dance")


def test_sequence():
    wait("TEST 6: Custom SEQ — index 300ms, middle 300ms, pinky 300ms")
    post("/seq", {"seq": "1:300:2:300:3:300"})


def test_wave():
    wait("TEST 7: Wave pattern — index→middle→pinky→middle→index (200ms each)")
    post("/seq", {"seq": "1:200:2:200:3:200:2:200:1:200"})


def test_grab():
    wait("TEST 8: Simulated GRAB — all three rapid fire then hold")
    for finger in ["INDEX", "MIDDLE", "PINKY"]:
        post("/stimulate", {"finger": finger, "action": "ON"})
        time.sleep(0.05)
    print("  All ON — holding 800ms...")
    time.sleep(0.8)
    post("/stop")


def test_pinch():
    wait("TEST 9: Simulated PINCH — index + middle only")
    post("/stimulate", {"finger": "INDEX", "action": "ON"})
    time.sleep(0.05)
    post("/stimulate", {"finger": "MIDDLE", "action": "ON"})
    print("  INDEX + MIDDLE ON — holding 600ms...")
    time.sleep(0.6)
    post("/stop")


def test_point():
    wait("TEST 10: Point INDEX only for 500ms")
    post("/stimulate", {"finger": "INDEX", "action": "ON", "duration_ms": 500})


def test_drumroll():
    wait("TEST 11: Drumroll — rapid alternating (3 rounds)")
    for _ in range(3):
        for finger in ["INDEX", "MIDDLE", "PINKY"]:
            post("/stimulate", {"finger": finger, "action": "ON", "duration_ms": 100})
            time.sleep(0.15)
    post("/stop")


if __name__ == "__main__":
    print(f"Sinew Hardware Test — Pi at {BASE}")
    print("Each test waits for ENTER before running.\n")

    if not test_connection():
        print("\nFix connection first!")
        sys.exit(1)

    try:
        test_individual_fingers()
        test_stop()
        test_dance()
        test_sequence()
        test_wave()
        test_grab()
        test_pinch()
        test_point()
        test_drumroll()

        print(f"\n{'='*50}")
        print("  ALL TESTS DONE")
        print(f"{'='*50}")
    except KeyboardInterrupt:
        print("\n\nStopping all relays...")
        post("/stop")
        print("Done.")
