"""
hardware/receiver.py

Flask bridge between the Sinew application layer and the Arduino firmware.
Listens on localhost:5001, translates HTTP requests into newline-terminated
ASCII commands over USB serial, reads the firmware's ACK, and returns JSON.

This process owns the serial port. Nothing else in the system is allowed to
open it. The application layer talks to the hardware only through the HTTP
endpoints defined in the project contract.

Safety posture: this bridge is a translation layer, not the safety layer.
The firmware enforces mutex, watchdog, and per finger caps independently.
This file adds defense in depth by capping duration_ms at 1000 and by
validating finger and action strings before anything hits the wire.

Locking model:
  _cmd_lock   serializes full "send command, read ACK" exchanges between
              /stimulate, /stop background ACK drainer, and reconnect logic.
              Held for the duration of one request's ACK roundtrip.
  _wire_lock  guards only the raw byte write. Held for microseconds. Lets
              /stop slip an ALL:OFF onto the wire without waiting for a
              timed pulse's ACK. Callers that hold _cmd_lock must also
              acquire _wire_lock before writing bytes, to order their
              writes correctly against priority writes.

  _abort_pulse is a threading.Event. /stop sets it. Any in-flight timed
  pulse in /stimulate polls it during its sleep and skips the auto-OFF
  write if set. The firmware's 2 s per finger cap is the backstop if the
  auto-OFF is skipped.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

import serial
from flask import Flask, jsonify, request
from serial.tools import list_ports

# Make the repo root importable so app.state resolves when we run this file
# directly from the hardware/ directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.state import Action, Finger  # noqa: E402

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

ARDUINO_VID = 0x2341  # Arduino SA / Arduino LLC
BAUD = 115200
READY_TIMEOUT_S = 5.0
ACK_TIMEOUT_S = 0.5
MAX_DURATION_MS = 1000  # validator in brain.py also caps this
WATCHDOG_WINDOW_MS = 3000  # must match firmware WATCHDOG_MS
PULSE_POLL_INTERVAL_S = 0.05  # how often phase B checks the abort event

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_FILE = LOG_DIR / "receiver.log"

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------

LOG_DIR.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("sinew.receiver")
log.setLevel(logging.INFO)
_handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
_handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
)
log.addHandler(_handler)
_console = logging.StreamHandler()
_console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_console)


# -----------------------------------------------------------------------------
# Serial wrapper
# -----------------------------------------------------------------------------


class SerialLink:
    """
    Thread safe wrapper around the pyserial port.

    Holds two locks:
      _cmd_lock  serializes full command + ACK exchanges
      _wire_lock serializes raw byte writes, including priority ALL:OFF
                 from /stop which must not wait for ACKs in flight
    """

    def __init__(self) -> None:
        self._port: Optional[serial.Serial] = None
        self._port_path: Optional[str] = None
        self._cmd_lock = threading.Lock()
        self._wire_lock = threading.Lock()
        self._last_ack: str = ""
        self._last_send_ms: int = 0
        # Count of priority writes whose ACKs still sit in the input buffer.
        # The next locked read drains that many lines before reading its own.
        self._pending_drain: int = 0
        self._drain_lock = threading.Lock()

    # ------------------------ connection management ------------------------

    @staticmethod
    def _detect_port() -> Optional[str]:
        override = os.environ.get("SINEW_SERIAL_PORT")
        if override:
            log.info("serial port override from env: %s", override)
            return override
        for p in list_ports.comports():
            if p.vid == ARDUINO_VID:
                log.info("found Arduino at %s vid=0x%04x pid=0x%04x", p.device, p.vid, p.pid or 0)
                return p.device
        log.warning("no Arduino found by VID 0x%04x", ARDUINO_VID)
        return None

    def connect(self) -> bool:
        path = self._detect_port()
        if path is None:
            return False
        try:
            port = serial.Serial(path, BAUD, timeout=READY_TIMEOUT_S)
        except serial.SerialException as e:
            log.error("failed to open %s: %s", path, e)
            return False

        time.sleep(0.2)  # Arduino Micro DTR reset settle
        banner = self._read_line(port, timeout_s=READY_TIMEOUT_S)
        if banner != "READY":
            log.error("expected READY, got %r on %s", banner, path)
            try:
                port.close()
            except Exception:
                pass
            return False

        self._port = port
        self._port_path = path
        self._last_ack = "READY"
        self._last_send_ms = self._now_ms()
        with self._drain_lock:
            self._pending_drain = 0
        log.info("connected to %s, firmware READY", path)
        return True

    def _close_quiet(self) -> None:
        if self._port is not None:
            try:
                self._port.close()
            except Exception:
                pass
        self._port = None

    def is_connected(self) -> bool:
        return self._port is not None and self._port.is_open

    # ------------------------ core IO ------------------------

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    @staticmethod
    def _read_line(port: serial.Serial, timeout_s: float) -> str:
        port.timeout = timeout_s
        raw = port.readline()
        if not raw:
            return ""
        return raw.decode("ascii", errors="replace").strip()

    def _drain_pending(self) -> None:
        """
        Read and log any ACK lines left behind by priority writes (/stop).
        Caller must hold _cmd_lock. Also drains any async messages from the
        firmware like TIMEOUT or WATCHDOG that arrived while we were idle.
        """
        assert self._port is not None
        with self._drain_lock:
            n = self._pending_drain
            self._pending_drain = 0
        for _ in range(n):
            line = self._read_line(self._port, ACK_TIMEOUT_S)
            if line:
                log.info("drained stale ack: %r", line)
                self._last_ack = line
        # Opportunistic drain of any other pending lines (TIMEOUT, WATCHDOG).
        while self._port.in_waiting > 0:
            line = self._read_line(self._port, 0.05)
            if not line:
                break
            log.info("drained async: %r", line)
            self._last_ack = line

    def _write_and_ack(self, command: str) -> str:
        """Caller must hold _cmd_lock. Raises SerialException on IO error."""
        assert self._port is not None
        self._drain_pending()
        line = (command + "\n").encode("ascii")
        with self._wire_lock:
            self._port.write(line)
            self._port.flush()
        ack = self._read_line(self._port, ACK_TIMEOUT_S)
        self._last_send_ms = self._now_ms()
        if ack:
            self._last_ack = ack
        log.info("serial tx=%r rx=%r", command, ack)
        return ack

    def send(self, command: str) -> str:
        """Public send with auto reconnect. Acquires _cmd_lock for the full roundtrip."""
        with self._cmd_lock:
            try:
                if not self.is_connected():
                    raise serial.SerialException("port not open")
                ack = self._write_and_ack(command)
                if ack == "":
                    raise serial.SerialException("empty ack")
                return ack
            except serial.SerialException as first_err:
                log.warning("first attempt failed for %r: %s, reconnecting", command, first_err)
                self._close_quiet()
                if not self._reconnect_locked():
                    raise
                try:
                    ack = self._write_and_ack(command)
                    if ack == "":
                        raise serial.SerialException("empty ack after reconnect")
                    return ack
                except serial.SerialException as second_err:
                    log.error("retry failed for %r: %s", command, second_err)
                    self._close_quiet()
                    raise

    def priority_write(self, command: str) -> bool:
        """
        Fire and forget a short command. Acquires only _wire_lock, never
        _cmd_lock. Returns True if the bytes went out. Does NOT read the ACK.
        The next locked command will drain the stale ACK.

        Intended for /stop. Safe against concurrent priority writes via
        _wire_lock. Idempotent at the firmware layer for ALL:OFF.
        """
        if not self.is_connected():
            log.error("priority_write %r skipped: port not open", command)
            return False
        line = (command + "\n").encode("ascii")
        try:
            with self._wire_lock:
                assert self._port is not None
                self._port.write(line)
                self._port.flush()
            with self._drain_lock:
                self._pending_drain += 1
            self._last_send_ms = self._now_ms()
            log.info("priority tx=%r", command)
            return True
        except serial.SerialException as e:
            log.error("priority_write %r failed: %s", command, e)
            return False

    def _reconnect_locked(self) -> bool:
        """Same as connect() but assumes the caller holds _cmd_lock."""
        path = self._detect_port()
        if path is None:
            return False
        try:
            port = serial.Serial(path, BAUD, timeout=READY_TIMEOUT_S)
        except serial.SerialException as e:
            log.error("reconnect open %s failed: %s", path, e)
            return False
        time.sleep(0.2)
        banner = self._read_line(port, timeout_s=READY_TIMEOUT_S)
        if banner != "READY":
            log.error("reconnect banner wrong: %r", banner)
            try:
                port.close()
            except Exception:
                pass
            return False
        self._port = port
        self._port_path = path
        self._last_ack = "READY"
        self._last_send_ms = self._now_ms()
        with self._drain_lock:
            self._pending_drain = 0
        log.info("reconnected to %s", path)
        return True

    # ------------------------ status ------------------------

    def snapshot(self) -> dict:
        remaining = WATCHDOG_WINDOW_MS - (self._now_ms() - self._last_send_ms)
        if remaining < 0:
            remaining = 0
        return {
            "connected": self.is_connected(),
            "last_ack": self._last_ack,
            "watchdog_remaining_ms": remaining,
        }


# -----------------------------------------------------------------------------
# Flask app
# -----------------------------------------------------------------------------


def create_app(link: Optional["SerialLink"] = None, auto_connect: bool = True) -> Flask:
    """
    Build the Flask app.

    link         optional pre-built SerialLink. Tests inject a fake here.
    auto_connect if True and link is None, create a real SerialLink and
                 call connect() once at startup. Set False in tests.
    """
    app = Flask(__name__)
    if link is None:
        link = SerialLink()
        if auto_connect and not link.connect():
            log.warning("serial not connected at startup, will retry on first request")
    abort_pulse = threading.Event()

    app.config["SINEW_LINK"] = link
    app.config["SINEW_ABORT"] = abort_pulse

    # ------------------------ /health ------------------------

    @app.get("/health")
    def health():
        log.info("GET /health")
        return jsonify({"alive": True}), 200

    # ------------------------ /status ------------------------

    @app.get("/status")
    def status():
        snap = link.snapshot()
        log.info("GET /status -> %s", snap)
        return jsonify(snap), 200

    # ------------------------ /stop ------------------------

    @app.post("/stop")
    def stop():
        log.info("POST /stop")
        # Signal any sleeping timed pulse to abort its auto-OFF first, so the
        # pulse's phase C is guaranteed to see the flag before ALL:OFF lands.
        abort_pulse.set()
        ok = link.priority_write("ALL:OFF")
        if not ok:
            return jsonify({"status": "error", "reason": "serial not open"}), 503
        return jsonify({"status": "ok"}), 200

    # ------------------------ /stimulate ------------------------

    @app.post("/stimulate")
    def stimulate():
        body = request.get_json(silent=True) or {}
        log.info("POST /stimulate body=%s", body)

        finger_raw = body.get("finger")
        action_raw = body.get("action")
        duration_ms = body.get("duration_ms")

        try:
            finger = Finger(finger_raw)
        except ValueError:
            return jsonify({"status": "error", "reason": f"bad finger: {finger_raw!r}"}), 400
        try:
            action = Action(action_raw)
        except ValueError:
            return jsonify({"status": "error", "reason": f"bad action: {action_raw!r}"}), 400

        if duration_ms is not None:
            if not isinstance(duration_ms, int) or isinstance(duration_ms, bool):
                return jsonify({"status": "error", "reason": "duration_ms must be int"}), 400
            if duration_ms < 0:
                return jsonify({"status": "error", "reason": "duration_ms must be >= 0"}), 400
            if duration_ms > MAX_DURATION_MS:
                log.warning("duration_ms %d capped to %d", duration_ms, MAX_DURATION_MS)
                duration_ms = MAX_DURATION_MS

        # A fresh /stimulate clears any lingering abort from a prior /stop.
        # The clear happens before phase A so the pulse starts clean.
        abort_pulse.clear()

        on_cmd = f"FINGER:{finger.value}:{action.value}"

        # ---- Phase A: locked ON write + ACK ----
        try:
            ack = link.send(on_cmd)
        except serial.SerialException as e:
            log.error("/stimulate phase A serial error: %s", e)
            return jsonify({"status": "error", "reason": f"serial: {e}"}), 503

        # ---- Phases B and C only apply to timed ON pulses ----
        if action is Action.ON and duration_ms is not None and duration_ms > 0:
            # Phase B runs in a background thread so the HTTP response can
            # return immediately with the phase A ACK. The firmware's 2 s per
            # finger cap is the ultimate backstop for this auto-OFF.
            t = threading.Thread(
                target=_run_pulse_tail,
                args=(link, finger, duration_ms, abort_pulse),
                daemon=True,
                name=f"pulse-tail-{finger.value}",
            )
            t.start()

        return jsonify({"status": "ok", "ack": ack}), 200

    # ------------------------ /piano ------------------------

    @app.post("/piano")
    def piano_route():
        body = request.get_json(silent=True) or {}
        song = (body.get("song") or "").upper()
        valid_songs = {"MARY", "HOTCROSS", "SCALE", "TRILL", "ARPEGGIO"}
        if song not in valid_songs:
            return jsonify({"status": "error", "reason": f"unknown song: {song!r}"}), 400
        log.info("POST /piano song=%s", song)
        try:
            ack = link.send(f"PIANO:{song}")
        except serial.SerialException as e:
            return jsonify({"status": "error", "reason": f"serial: {e}"}), 503
        return jsonify({"status": "ok", "ack": ack}), 200

    # ------------------------ /sign ------------------------

    @app.post("/sign")
    def sign_route():
        body = request.get_json(silent=True) or {}
        word = (body.get("word") or "").upper()[:12]
        hold_ms = body.get("hold_ms", 500)
        if not word or not word.isalpha():
            return jsonify({"status": "error", "reason": "word must be 1-12 alpha chars"}), 400
        if not isinstance(hold_ms, int) or isinstance(hold_ms, bool):
            hold_ms = 500
        hold_ms = max(50, min(2000, hold_ms))
        log.info("POST /sign word=%s hold_ms=%d", word, hold_ms)
        try:
            ack = link.send(f"SIGN:{word}:{hold_ms}")
        except serial.SerialException as e:
            return jsonify({"status": "error", "reason": f"serial: {e}"}), 503
        return jsonify({"status": "ok", "ack": ack}), 200

    # ------------------------ /chord ------------------------

    @app.post("/chord")
    def chord_route():
        body = request.get_json(silent=True) or {}
        mask = body.get("mask")
        duration_ms = body.get("duration_ms", 400)
        if not isinstance(mask, int) or isinstance(mask, bool) or mask < 1 or mask > 7:
            return jsonify({"status": "error", "reason": "mask must be int 1-7"}), 400
        if not isinstance(duration_ms, int) or isinstance(duration_ms, bool):
            return jsonify({"status": "error", "reason": "duration_ms must be int"}), 400
        duration_ms = max(0, min(2000, duration_ms))
        log.info("POST /chord mask=%d duration_ms=%d", mask, duration_ms)
        try:
            ack = link.send(f"CHORD:{mask}:{duration_ms}")
        except serial.SerialException as e:
            return jsonify({"status": "error", "reason": f"serial: {e}"}), 503
        return jsonify({"status": "ok", "ack": ack}), 200

    # ------------------------ /dance ------------------------

    @app.post("/dance")
    def dance_route():
        log.info("POST /dance")
        try:
            ack = link.send("DANCE")
        except serial.SerialException as e:
            return jsonify({"status": "error", "reason": f"serial: {e}"}), 503
        return jsonify({"status": "ok", "ack": ack}), 200

    # ------------------------ /seq ------------------------

    @app.post("/seq")
    def seq_route():
        body = request.get_json(silent=True) or {}
        seq_str = body.get("seq", "")
        if not seq_str:
            return jsonify({"status": "error", "reason": "seq string required"}), 400
        log.info("POST /seq seq=%s", seq_str)
        try:
            ack = link.send(f"SEQ:{seq_str}")
        except serial.SerialException as e:
            return jsonify({"status": "error", "reason": f"serial: {e}"}), 503
        return jsonify({"status": "ok", "ack": ack}), 200

    # ------------------------ /stress ------------------------

    @app.post("/stress")
    def stress_route():
        log.info("POST /stress")
        try:
            ack = link.send("STRESS")
        except serial.SerialException as e:
            return jsonify({"status": "error", "reason": f"serial: {e}"}), 503
        return jsonify({"status": "ok", "ack": ack}), 200

    # ------------------------ /rapid ------------------------

    @app.post("/rapid")
    def rapid_route():
        body = request.get_json(silent=True) or {}
        finger = body.get("finger", 1)
        reps = body.get("reps", 10)
        on_ms = body.get("on_ms", 50)
        off_ms = body.get("off_ms", 50)
        if not isinstance(finger, int) or finger < 1 or finger > 3:
            return jsonify({"status": "error", "reason": "finger must be 1-3"}), 400
        reps = max(1, min(100, int(reps)))
        on_ms = max(1, min(1000, int(on_ms)))
        off_ms = max(1, min(1000, int(off_ms)))
        log.info("POST /rapid finger=%d reps=%d on=%d off=%d", finger, reps, on_ms, off_ms)
        try:
            ack = link.send(f"RAPID:{finger}:{reps}:{on_ms}:{off_ms}")
        except serial.SerialException as e:
            return jsonify({"status": "error", "reason": f"serial: {e}"}), 503
        return jsonify({"status": "ok", "ack": ack}), 200

    return app


def _run_pulse_tail(
    link: SerialLink,
    finger: Finger,
    duration_ms: int,
    abort: threading.Event,
) -> None:
    """
    Phase B + Phase C of a timed pulse. Runs off the request thread.

    Phase B: sleep duration_ms in 50ms slices, checking the abort event each
    slice. If aborted, skip phase C and rely on /stop's ALL:OFF plus the
    firmware per finger cap.

    Phase C: best effort FINGER:X:OFF. Errors are logged, not raised.
    """
    deadline = time.monotonic() + (duration_ms / 1000.0)
    while True:
        now = time.monotonic()
        if now >= deadline:
            break
        if abort.is_set():
            log.info("pulse tail for %s aborted during sleep", finger.value)
            return
        remaining = deadline - now
        time.sleep(min(PULSE_POLL_INTERVAL_S, remaining))

    if abort.is_set():
        log.info("pulse tail for %s aborted before phase C", finger.value)
        return

    off_cmd = f"FINGER:{finger.value}:OFF"
    try:
        off_ack = link.send(off_cmd)
        log.info("pulse tail auto-off ack=%r", off_ack)
    except serial.SerialException as e:
        # Firmware per finger cap (2 s) will force OFF. Safe to swallow.
        log.error("pulse tail auto-off failed: %s, firmware cap will handle it", e)


# -----------------------------------------------------------------------------
# Entrypoint
# -----------------------------------------------------------------------------


if __name__ == "__main__":
    app = create_app()
    # threaded=True so /stop can run while /stimulate's phase A is writing.
    # Serial IO is protected by SerialLink's own locks.
    app.run(host="127.0.0.1", port=5001, threaded=True)
