"""
Sinew Pi Receiver — HTTP-to-serial bridge for Arduino EMS relay control.

Endpoints:
  POST /stimulate  — fire a finger relay {finger, action, duration_ms}
  POST /stop       — immediate all-off
  POST /dance      — run dance sequence
  POST /seq        — run custom sequence {seq: "1:300:2:400:3:250"}
  GET  /status     — Arduino connection info
  GET  /health     — ok/not-ok
"""

import glob
import logging
import logging.handlers
import os
import sys
import threading
import time

from flask import Flask, jsonify, request
import serial
import serial.tools.list_ports

app = Flask(__name__)

BAUD_RATE = 9600
SERIAL_TIMEOUT = 0.5
READY_WAIT = 5
RECONNECT_SEC = 2.0

KNOWN_VIDS = {
    (0x2341, 0x0043),
    (0x2341, 0x8037),
    (0x1A86, 0x7523),
}

os.makedirs("logs", exist_ok=True)
logger = logging.getLogger("receiver")
logger.setLevel(logging.DEBUG)
fh = logging.handlers.RotatingFileHandler("logs/serial.log", maxBytes=1_000_000, backupCount=3)
fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(fh)
sh = logging.StreamHandler(sys.stderr)
sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(sh)

# Finger name → Arduino command number
FINGER_ON  = {"INDEX": "1", "MIDDLE": "2", "PINKY": "3"}
FINGER_OFF = {"INDEX": "4", "MIDDLE": "5", "PINKY": "6"}


class ArduinoConnection:
    def __init__(self):
        self.ser = None
        self.port_path = None
        self.connected = False
        self.last_cmd = ""
        self.last_ack = ""
        self.last_ack_time = 0.0
        self._lock = threading.Lock()
        self._running = True
        self._reconnecting = False

    def start(self):
        port = self._detect()
        if port:
            self._open(port)
        else:
            logger.warning("No Arduino found, retrying in background")
            self._reconnect_bg()
        threading.Thread(target=self._reader, daemon=True).start()

    def _detect(self):
        env = os.environ.get("SINEW_ARDUINO_PORT")
        if env:
            return env
        for p in serial.tools.list_ports.comports():
            if p.vid and (p.vid, p.pid) in KNOWN_VIDS:
                logger.info(f"Found Arduino at {p.device}")
                return p.device
        for pattern in ["/dev/ttyACM*", "/dev/ttyUSB*"]:
            ports = sorted(glob.glob(pattern))
            if ports:
                logger.info(f"Fallback: {ports[0]}")
                return ports[0]
        return None

    def _open(self, port):
        try:
            self.ser = serial.Serial(port, BAUD_RATE, timeout=SERIAL_TIMEOUT)
            self.port_path = port
            self.connected = True
            logger.info(f"Opened {port}")
            start = time.monotonic()
            while time.monotonic() - start < READY_WAIT:
                if self.ser.in_waiting:
                    line = self.ser.readline().decode("utf-8", errors="replace").strip()
                    if line:
                        logger.info(f"Boot: {line}")
                time.sleep(0.1)
        except serial.SerialException as e:
            logger.error(f"Open failed: {e}")
            self.connected = False

    def _reconnect_bg(self):
        if self._reconnecting:
            return
        self._reconnecting = True
        def _loop():
            while self._running and not self.connected:
                time.sleep(RECONNECT_SEC)
                port = self._detect()
                if port:
                    with self._lock:
                        self._open(port)
            self._reconnecting = False
        threading.Thread(target=_loop, daemon=True).start()

    def _reader(self):
        while self._running:
            if not self.connected or not self.ser:
                time.sleep(0.1)
                continue
            try:
                if self.ser.in_waiting:
                    line = self.ser.readline().decode("utf-8", errors="replace").strip()
                    if line:
                        logger.info(f"← {line}")
                        self.last_ack = line
                        self.last_ack_time = time.monotonic()
                else:
                    time.sleep(0.01)
            except serial.SerialException as e:
                logger.error(f"Read error: {e}")
                self.connected = False
                self.ser = None
                self._reconnect_bg()

    def send(self, cmd):
        with self._lock:
            if not self.connected or not self.ser:
                return False
            try:
                self.last_cmd = cmd
                self.ser.write((cmd + "\n").encode())
                self.ser.flush()
                logger.info(f"→ {cmd}")
                return True
            except serial.SerialException as e:
                logger.error(f"Write error: {e}")
                self.connected = False
                self.ser = None
                self._reconnect_bg()
                return False

    def read_ack(self, timeout=1.0):
        start = time.monotonic()
        prev = self.last_ack
        while time.monotonic() - start < timeout:
            if self.last_ack != prev or self.last_ack_time > start:
                return self.last_ack
            time.sleep(0.01)
        return None


arduino = ArduinoConnection()


@app.route("/stimulate", methods=["POST"])
def stimulate():
    if not arduino.connected:
        return jsonify({"error": "Arduino not connected"}), 503
    data = request.get_json(silent=True) or {}
    finger = data.get("finger", "").upper()
    action = data.get("action", "").upper()
    dur = data.get("duration_ms", 0)

    if finger not in FINGER_ON:
        return jsonify({"error": f"Bad finger: {finger}"}), 400
    if action not in ("ON", "OFF"):
        return jsonify({"error": f"Bad action: {action}"}), 400

    cmd = FINGER_ON[finger] if action == "ON" else FINGER_OFF[finger]

    if action == "ON" and isinstance(dur, int) and dur > 0:
        arduino.send(cmd)
        time.sleep(min(dur, 2000) / 1000.0)
        arduino.send(FINGER_OFF[finger])
        ack = arduino.read_ack(0.5)
        return jsonify({"ack": ack, "timed": True})

    ok = arduino.send(cmd)
    if not ok:
        return jsonify({"error": "Serial write failed"}), 503
    ack = arduino.read_ack(0.5)
    return jsonify({"ack": ack})


@app.route("/stop", methods=["POST"])
def stop():
    if not arduino.connected:
        return jsonify({"error": "Arduino not connected"}), 503
    arduino.send("OFF")
    ack = arduino.read_ack(0.5)
    return jsonify({"ack": ack})


@app.route("/dance", methods=["POST"])
def dance():
    if not arduino.connected:
        return jsonify({"error": "Arduino not connected"}), 503
    arduino.send("DANCE")
    ack = arduino.read_ack(5.0)
    return jsonify({"ack": ack})


@app.route("/seq", methods=["POST"])
def seq():
    if not arduino.connected:
        return jsonify({"error": "Arduino not connected"}), 503
    data = request.get_json(silent=True) or {}
    s = data.get("seq", "")
    if not s:
        return jsonify({"error": "Missing seq field"}), 400
    arduino.send(f"SEQ:{s}")
    ack = arduino.read_ack(10.0)
    return jsonify({"ack": ack})


@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "connected": arduino.connected,
        "port": arduino.port_path or "",
        "last_cmd": arduino.last_cmd,
        "last_ack": arduino.last_ack,
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": arduino.connected})


if __name__ == "__main__":
    arduino.start()
    app.run(host="0.0.0.0", port=5001, threaded=True, debug=False)
