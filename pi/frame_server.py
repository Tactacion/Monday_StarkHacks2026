"""
Sinew Pi Frame Server — USB webcam capture and JPEG streaming.

Endpoints:
  GET /frame      — latest frame as image/jpeg (single shot)
  GET /frame.mjpg — MJPEG multipart stream (for browser debugging)
  GET /health     — camera status and grab count

Runs on port 5002, bound to 0.0.0.0.
Dependencies: flask, opencv-python-headless (NOT full opencv-python on Pi).
"""

import logging
import os
import signal
import sys
import threading
import time

import cv2
from flask import Flask, Response, jsonify

app = Flask(__name__)

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("frame_server")

# --- Configuration ---
CAMERA_INDEX = int(os.environ.get("SINEW_CAMERA_INDEX", "0"))
FRAME_WIDTH = 640
FRAME_HEIGHT = 480
JPEG_QUALITY = 80
GRAB_FPS = 15
HEALTH_STALE_MS = 2000


class CameraGrabber:
    def __init__(self):
        self._cap = None
        self._lock = threading.Lock()
        self._latest_frame = None  # JPEG bytes
        self._grab_count = 0
        self._last_grab_ns = None  # monotonic nanoseconds
        self._running = True
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._grab_loop, daemon=True)
        self._thread.start()

    def _open_camera(self):
        logger.info(f"Opening camera index {CAMERA_INDEX}")
        cap = cv2.VideoCapture(CAMERA_INDEX)
        if not cap.isOpened():
            logger.error(f"Failed to open camera {CAMERA_INDEX}")
            return None
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FPS, 30)
        logger.info(f"Camera opened: {int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}")
        return cap

    def _grab_loop(self):
        while self._running:
            if self._cap is None or not self._cap.isOpened():
                self._cap = self._open_camera()
                if self._cap is None:
                    logger.warning("Camera not available, retrying in 2s")
                    time.sleep(2)
                    continue

            ret, frame = self._cap.read()
            if not ret:
                logger.warning("Frame read failed, will retry")
                time.sleep(0.1)
                continue

            # Encode to JPEG
            encode_params = [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
            ok, jpeg_buf = cv2.imencode(".jpg", frame, encode_params)
            if ok:
                with self._lock:
                    self._latest_frame = jpeg_buf.tobytes()
                    self._grab_count += 1
                    self._last_grab_ns = time.monotonic_ns()

            # Throttle to target FPS
            time.sleep(1.0 / GRAB_FPS)

    def get_frame(self):
        with self._lock:
            return self._latest_frame

    def get_stats(self):
        with self._lock:
            last_ms_ago = None
            if self._last_grab_ns is not None:
                last_ms_ago = int((time.monotonic_ns() - self._last_grab_ns) / 1_000_000)
            return {
                "has_frame": self._latest_frame is not None,
                "grab_count": self._grab_count,
                "last_grab_ms_ago": last_ms_ago,
            }

    def release(self):
        self._running = False
        if self._cap is not None:
            self._cap.release()
            logger.info("Camera released")


# Global grabber
grabber = CameraGrabber()


@app.route("/frame", methods=["GET"])
def frame():
    jpeg = grabber.get_frame()
    if jpeg is None:
        return "no frame available", 503
    return Response(
        jpeg,
        mimetype="image/jpeg",
        headers={"Cache-Control": "no-cache, no-store"},
    )


@app.route("/frame.mjpg", methods=["GET"])
def mjpeg_stream():
    def generate():
        while True:
            jpeg = grabber.get_frame()
            if jpeg is not None:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n"
                    + jpeg
                    + b"\r\n"
                )
            time.sleep(1.0 / GRAB_FPS)

    return Response(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/health", methods=["GET"])
def health():
    stats = grabber.get_stats()
    ok = stats["has_frame"]
    if ok and stats["last_grab_ms_ago"] is not None:
        ok = stats["last_grab_ms_ago"] < HEALTH_STALE_MS
    return jsonify({
        "ok": ok,
        "has_frame": stats["has_frame"],
        "last_grab_ms_ago": stats["last_grab_ms_ago"],
        "grab_count": stats["grab_count"],
    })


def _signal_handler(signum, frame):
    logger.info(f"Received signal {signum}, shutting down")
    grabber.release()
    sys.exit(0)


signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)


if __name__ == "__main__":
    grabber.start()
    app.run(host="0.0.0.0", port=5002, threaded=True, debug=False)
