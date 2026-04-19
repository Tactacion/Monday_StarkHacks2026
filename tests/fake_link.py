"""
tests/fake_link.py

In-memory stand-in for SerialLink. Simulates firmware ACK latency, records
every write in the order it hit the wire with a timestamp, and honors the
same locking contract as the real SerialLink so tests exercise the same
critical sections as production.
"""

from __future__ import annotations

import threading
import time
from typing import List, Tuple


class FakeSerialLink:
    # Roughly matches the real ACK roundtrip on a close-by Arduino.
    ACK_LATENCY_S = 0.01

    def __init__(self) -> None:
        self._cmd_lock = threading.Lock()
        self._wire_lock = threading.Lock()
        self._connected = True
        self._last_ack = "READY"
        self._last_send_ms = int(time.time() * 1000)
        self._tx_log: List[Tuple[float, str, str]] = []
        # entries are (monotonic_ts, kind, command) where kind in {"cmd","priority"}
        self._tx_log_lock = threading.Lock()

    # --------- connection ---------

    def connect(self) -> bool:
        self._connected = True
        return True

    def is_connected(self) -> bool:
        return self._connected

    # --------- IO ---------

    def _record(self, kind: str, command: str) -> None:
        with self._tx_log_lock:
            self._tx_log.append((time.monotonic(), kind, command))

    def send(self, command: str) -> str:
        """Full locked command + ACK exchange. Simulates blocking ACK wait."""
        with self._cmd_lock:
            with self._wire_lock:
                self._record("cmd", command)
            time.sleep(self.ACK_LATENCY_S)
            self._last_send_ms = int(time.time() * 1000)
            self._last_ack = f"OK:{command}"
            return self._last_ack

    def priority_write(self, command: str) -> bool:
        """Byte-level fire and forget. Does not read ACK."""
        with self._wire_lock:
            self._record("priority", command)
        self._last_send_ms = int(time.time() * 1000)
        return True

    # --------- introspection ---------

    def snapshot(self) -> dict:
        from hardware.receiver import WATCHDOG_WINDOW_MS
        remaining = WATCHDOG_WINDOW_MS - (int(time.time() * 1000) - self._last_send_ms)
        if remaining < 0:
            remaining = 0
        return {
            "connected": self._connected,
            "last_ack": self._last_ack,
            "watchdog_remaining_ms": remaining,
        }

    def tx_log(self) -> List[Tuple[float, str, str]]:
        with self._tx_log_lock:
            return list(self._tx_log)
