"""
Sinew Orchestrator — translates high-level macros into multiplexed relay
commands sent to the Pi receiver over HTTP.
"""

import logging
import logging.handlers
import os
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

import requests

os.makedirs("logs", exist_ok=True)
logger = logging.getLogger("orchestrator")
logger.setLevel(logging.DEBUG)
_handler = logging.handlers.RotatingFileHandler(
    "logs/orchestrator.log", maxBytes=1_000_000, backupCount=3
)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_handler)


@dataclass
class Action:
    action: str       # GRAB_ALL, PINCH, POINT_INDEX, POINT_MIDDLE, POINT_PINKY, RELEASE
    duration_ms: int  # 0-1500


class State(Enum):
    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    EXECUTING = "executing"


FINGERS = ["INDEX", "MIDDLE", "PINKY"]


class Orchestrator:
    def __init__(
        self,
        pi_host: str = "sinew.local",
        pi_port: int = 5001,
        multiplex_ms: int = 60,
        relay_on_ms: int = 50,
    ):
        self.base_url = f"http://{pi_host}:{pi_port}"
        self.multiplex_ms = multiplex_ms
        self.relay_on_ms = relay_on_ms
        self._state = State.IDLE
        self._state_lock = threading.Lock()
        self._state_callbacks: list[Callable[[State], None]] = []
        self._cancel_event = threading.Event()
        self._session = requests.Session()
        # Track which finger is currently active (for UI callbacks)
        self._active_finger: Optional[str] = None
        self._finger_callbacks: list[Callable[[Optional[str]], None]] = []

    def set_state(self, state: State):
        with self._state_lock:
            self._state = state
        for cb in self._state_callbacks:
            try:
                cb(state)
            except Exception:
                pass

    def get_state(self) -> State:
        with self._state_lock:
            return self._state

    def on_state_change(self, callback: Callable[[State], None]):
        self._state_callbacks.append(callback)

    def on_finger_change(self, callback: Callable[[Optional[str]], None]):
        """Register callback for when the active finger changes."""
        self._finger_callbacks.append(callback)

    def _set_active_finger(self, finger: Optional[str]):
        self._active_finger = finger
        for cb in self._finger_callbacks:
            try:
                cb(finger)
            except Exception:
                pass

    def _stimulate(self, finger: str, action: str, duration_ms: int = 0) -> Optional[dict]:
        """Send a single stimulate command. Returns response dict or None on error."""
        payload = {"finger": finger, "action": action, "duration_ms": duration_ms}
        try:
            r = self._session.post(f"{self.base_url}/stimulate", json=payload, timeout=3)
            data = r.json()
            logger.info(f"POST /stimulate {payload} → {r.status_code} {data}")
            return data
        except Exception as e:
            logger.error(f"POST /stimulate {payload} failed: {e}")
            return None

    def _post_stop(self) -> Optional[dict]:
        try:
            r = self._session.post(f"{self.base_url}/stop", timeout=2)
            data = r.json()
            logger.info(f"POST /stop → {r.status_code} {data}")
            return data
        except Exception as e:
            logger.error(f"POST /stop failed: {e}")
            return None

    def execute_plan(self, actions: list[Action]) -> bool:
        """Run action sequence. Returns True if completed, False if cancelled/error."""
        self._cancel_event.clear()
        self.set_state(State.EXECUTING)

        try:
            for action in actions:
                if self._cancel_event.is_set():
                    logger.info("Plan cancelled")
                    return False

                if action.action == "RELEASE":
                    self._set_active_finger(None)
                    self._post_stop()
                    continue

                if action.action in ("POINT_INDEX", "POINT_MIDDLE", "POINT_PINKY"):
                    finger = action.action.replace("POINT_", "")
                    self._set_active_finger(finger)
                    self._stimulate(finger, "ON", action.duration_ms)
                    self._set_active_finger(None)
                    continue

                # Multiplexed actions
                if action.action == "PINCH":
                    fingers = ["INDEX", "MIDDLE"]
                elif action.action == "GRAB_ALL":
                    fingers = ["INDEX", "MIDDLE", "PINKY"]
                else:
                    logger.error(f"Unknown action: {action.action}")
                    continue

                self._multiplex(fingers, action.duration_ms)

            return True
        except Exception as e:
            logger.error(f"Plan execution error: {e}")
            return False
        finally:
            self._set_active_finger(None)
            self.set_state(State.IDLE)

    def _multiplex(self, fingers: list[str], total_duration_ms: int):
        """Round-robin pulse through fingers for the given total duration."""
        start = time.monotonic()
        idx = 0
        while (time.monotonic() - start) * 1000 < total_duration_ms:
            if self._cancel_event.is_set():
                self._post_stop()
                return

            finger = fingers[idx % len(fingers)]
            self._set_active_finger(finger)
            self._stimulate(finger, "ON", self.relay_on_ms)

            # Sleep for the remainder of the multiplex cycle
            sleep_ms = self.multiplex_ms - self.relay_on_ms
            if sleep_ms > 0:
                time.sleep(sleep_ms / 1000.0)

            self._set_active_finger(None)
            idx += 1

    def stop(self):
        """Immediate all-off. Safe to call from any thread."""
        self._cancel_event.set()
        self._set_active_finger(None)
        self._post_stop()
        self.set_state(State.IDLE)

    def ping(self) -> bool:
        """Returns True if Pi receiver is reachable."""
        try:
            r = self._session.get(f"{self.base_url}/health", timeout=2)
            return r.status_code == 200 and r.json().get("ok", False)
        except Exception:
            return False

    def status(self) -> Optional[dict]:
        """Get full Arduino status from Pi."""
        try:
            r = self._session.get(f"{self.base_url}/status", timeout=2)
            return r.json()
        except Exception:
            return None


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    pi_host = os.environ.get("PI_HOST", "sinew.local")
    orch = Orchestrator(pi_host=pi_host)

    if not orch.ping():
        print(f"Cannot reach Pi at {pi_host}:5001", file=sys.stderr)
        sys.exit(1)

    print("Pi connected. Running GRAB_ALL for 800ms then RELEASE...")
    result = orch.execute_plan([
        Action("GRAB_ALL", 800),
        Action("RELEASE", 0),
    ])
    print(f"{'Completed' if result else 'Cancelled/Error'}")
