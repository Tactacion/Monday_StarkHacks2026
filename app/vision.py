"""
app/vision.py

Frame capture for the Sinew orchestrator. Two modes:

  * Remote (preferred for the demo): poll the Pi frame server's `/frame`
    endpoint over HTTP. Selected when `frame_url` is set or the
    `SINEW_CAMERA_URL` env var is present.
  * Local: open a cv2.VideoCapture on the laptop. Fallback for bench work
    when the Pi is not reachable.

A background grabber thread continuously refreshes the latest frame so the
main thread never blocks on IO.

load_file_as_b64 is a @staticmethod so the brain eval harness can encode
fixture JPEGs without opening the camera.

Environment overrides:
  SINEW_CAMERA_URL     str, e.g. http://10.0.0.5:5002 (or with /frame)
  SINEW_CAMERA_INDEX   int, default 0       (local mode only)
  SINEW_CAMERA_WIDTH   int, default 1280    (local mode only)
  SINEW_CAMERA_HEIGHT  int, default 720     (local mode only)
"""

from __future__ import annotations

import base64
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import requests

logger = logging.getLogger(__name__)

FRAME_TIMEOUT_S = 5.0        # get_latest_frame waits this long before raising
GRABBER_SLEEP_S = 0.0        # local tight loop, cap.read() paces the thread
HTTP_POLL_INTERVAL_S = 0.05  # ~20 Hz poll of Pi frame server
# (connect, read). Connect is generous because the Pi frame server can stall
# briefly during camera init / MJPG grab cycles; read is tight because frames
# are small and LAN is fast once the connection is warm.
HTTP_REQUEST_TIMEOUT = (5.0, 4.0)


class VisionCapture:
    """
    Webcam capture with a background grabber thread.

    Typical use from the orchestrator:

        vc = VisionCapture()                 # opens camera, starts grabber
        try:
            frame = vc.get_latest_frame()    # blocks up to FRAME_TIMEOUT_S
            b64 = vc.encode_for_claude(frame)
        finally:
            vc.release()
    """

    def __init__(
        self,
        camera_index: Optional[int] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        fps: int = 30,
        frame_url: Optional[str] = None,
    ) -> None:
        env_url = os.environ.get("SINEW_CAMERA_URL") or None
        url = frame_url if frame_url is not None else env_url
        self.frame_url: Optional[str] = self._normalize_frame_url(url)

        self.camera_index = (
            camera_index if camera_index is not None
            else int(os.environ.get("SINEW_CAMERA_INDEX", "0"))
        )
        self.width = (
            width if width is not None
            else int(os.environ.get("SINEW_CAMERA_WIDTH", "1280"))
        )
        self.height = (
            height if height is not None
            else int(os.environ.get("SINEW_CAMERA_HEIGHT", "720"))
        )
        self.fps = fps

        self._cap: Optional[cv2.VideoCapture] = None
        self._session: Optional[requests.Session] = None
        self._latest_frame: Optional[np.ndarray] = None
        self._lock = threading.Lock()
        self._frame_ready = threading.Event()
        self._running = False
        self._thread: Optional[threading.Thread] = None

        if self.frame_url:
            self._start_http_grabber()
        else:
            self._open_camera()
            self._start_grabber()

    @staticmethod
    def _normalize_frame_url(url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        url = url.strip()
        if not url:
            return None
        # Accept a base like http://host:5002 and append /frame.
        if url.rstrip("/").endswith("/frame") or url.rstrip("/").endswith("/frame.mjpg"):
            return url
        return url.rstrip("/") + "/frame"

    # ---------------------------------------------------------------------
    # Camera open and grabber loop
    # ---------------------------------------------------------------------

    def _open_camera(self) -> None:
        # CAP_ANY lets OpenCV pick the best backend. cv2.VideoCapture returns
        # an object whose isOpened() is the real truth on some platforms; a
        # failed open on Linux still builds an object that then fails to read.
        cap = cv2.VideoCapture(self.camera_index, cv2.CAP_ANY)
        if not cap.isOpened():
            raise RuntimeError(
                f"VisionCapture could not open camera at index {self.camera_index}. "
                f"On Linux check that /dev/video{self.camera_index} exists and the user "
                f"has permission to read it. On WSL2 the host webcam is not forwarded "
                f"by default."
            )
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)

        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        logger.info(
            "camera opened index=%s requested=%dx%d@%d actual=%dx%d@%.1f",
            self.camera_index, self.width, self.height, self.fps,
            actual_w, actual_h, actual_fps,
        )
        self._cap = cap

    def _start_grabber(self) -> None:
        self._running = True
        self._thread = threading.Thread(
            target=self._grabber_loop, daemon=True, name="vision-grabber"
        )
        self._thread.start()

    def _start_http_grabber(self) -> None:
        assert self.frame_url is not None
        self._session = requests.Session()
        logger.info("camera mode=http url=%s", self.frame_url)
        self._running = True
        self._thread = threading.Thread(
            target=self._http_grabber_loop, daemon=True, name="vision-http-grabber"
        )
        self._thread.start()

    def _http_grabber_loop(self) -> None:
        """Poll the Pi frame server. Each response body is a JPEG we decode to BGR."""
        assert self._session is not None and self.frame_url is not None
        consecutive_failures = 0
        first_success_logged = False
        while self._running:
            try:
                resp = self._session.get(self.frame_url, timeout=HTTP_REQUEST_TIMEOUT)
                resp.raise_for_status()
                buf = np.frombuffer(resp.content, dtype=np.uint8)
                frame = cv2.imdecode(buf, cv2.IMREAD_COLOR)
                if frame is None:
                    raise ValueError("cv2.imdecode returned None")
            except (requests.RequestException, ValueError) as e:
                consecutive_failures += 1
                if consecutive_failures == 1 or consecutive_failures % 10 == 0:
                    logger.warning(
                        "frame_url fetch failed x%d (%s): %s",
                        consecutive_failures, self.frame_url, e,
                    )
                time.sleep(0.5)
                continue
            if not first_success_logged:
                logger.info(
                    "frame_url first frame ok (%dx%d)", frame.shape[1], frame.shape[0]
                )
                first_success_logged = True
            consecutive_failures = 0
            with self._lock:
                self._latest_frame = frame
            self._frame_ready.set()
            if HTTP_POLL_INTERVAL_S:
                time.sleep(HTTP_POLL_INTERVAL_S)

    def _grabber_loop(self) -> None:
        """
        Tight read loop. cap.read() blocks until the next frame is available,
        so the thread is self-paced to the camera's FPS. Each frame replaces
        the previous one in _latest_frame, so late consumers only ever see
        the most recent image.
        """
        assert self._cap is not None
        consecutive_failures = 0
        while self._running:
            ok, frame = self._cap.read()
            if not ok or frame is None:
                consecutive_failures += 1
                if consecutive_failures == 1 or consecutive_failures % 30 == 0:
                    logger.warning("camera read failed x%d", consecutive_failures)
                time.sleep(0.05)
                continue
            consecutive_failures = 0
            with self._lock:
                self._latest_frame = frame
            self._frame_ready.set()
            if GRABBER_SLEEP_S:
                time.sleep(GRABBER_SLEEP_S)

    # ---------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------

    def get_latest_frame(self) -> np.ndarray:
        """
        Return a copy of the most recent frame. Blocks up to FRAME_TIMEOUT_S
        waiting for the grabber to produce at least one frame. Raises
        RuntimeError if no frame arrives in that window.
        """
        if not self._frame_ready.is_set():
            if not self._frame_ready.wait(timeout=FRAME_TIMEOUT_S):
                raise RuntimeError(
                    f"no frame available after {FRAME_TIMEOUT_S}s. Camera "
                    f"opened but grabber produced no frames."
                )
        with self._lock:
            if self._latest_frame is None:
                raise RuntimeError("latest frame is None after frame_ready signaled")
            return self._latest_frame.copy()

    def encode_for_claude(self, frame: np.ndarray) -> str:
        """Encode a BGR numpy frame as base64 JPEG (quality 80). No data URL prefix."""
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if not ok:
            raise RuntimeError("cv2.imencode failed on frame")
        return base64.b64encode(buf.tobytes()).decode("ascii")

    def release(self) -> None:
        """Stop the grabber and release the camera. Safe to call more than once."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        if self._session is not None:
            self._session.close()
            self._session = None
        logger.info("VisionCapture released")

    # ---------------------------------------------------------------------
    # Static helper, used by the brain eval harness
    # ---------------------------------------------------------------------

    @staticmethod
    def load_file_as_b64(path: str) -> str:
        """Read a JPEG file and return its raw base64 encoding. No camera needed."""
        with Path(path).open("rb") as f:
            return base64.b64encode(f.read()).decode("ascii")


# =============================================================================
# Operator smoke test
# =============================================================================

def _main() -> int:
    """
    Open the camera, wait 2s for the grabber, grab three frames 500ms apart,
    save to /tmp/sinew_test_frame_{1,2,3}.jpg, and print a short summary.

    Eyeball the three files: if they're identical the camera is stuck on a
    buffered frame, which means the grabber is not actually draining new
    frames. If they differ (background noise, your hand moving, anything),
    the live path is working.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    print(f"opening camera at index {os.environ.get('SINEW_CAMERA_INDEX', '0')}...")
    try:
        vc = VisionCapture()
    except RuntimeError as e:
        print(f"FAIL: {e}")
        return 1

    try:
        print("waiting 2s for grabber to fill...")
        time.sleep(2.0)

        for i in (1, 2, 3):
            frame = vc.get_latest_frame()
            out_path = f"/tmp/sinew_test_frame_{i}.jpg"
            cv2.imwrite(out_path, frame)
            if i == 1:
                b64 = vc.encode_for_claude(frame)
                print(f"frame {i}  shape={frame.shape}  dtype={frame.dtype}  saved={out_path}")
                print(f"base64 prefix: {b64[:50]}...")
            else:
                print(f"frame {i}  shape={frame.shape}  saved={out_path}")
            if i < 3:
                time.sleep(0.5)

        print()
        print("open the three saved JPEGs and confirm they differ (hand motion,")
        print("lighting noise, anything). Identical files mean the grabber is")
        print("not draining new frames.")
    finally:
        vc.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
