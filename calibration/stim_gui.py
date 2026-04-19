"""
Sinew Calibration GUI — manual EMS relay testing, independent of the AI stack.
Lets you fire individual finger relays, adjust pulse duration, and run test sequences.

Usage: python3 calibration/stim_gui.py
"""

import json
import sys
import threading
import time

import requests
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QThread
from PyQt5.QtGui import QColor, QFont, QPalette
from PyQt5.QtWidgets import (
    QApplication, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QPlainTextEdit, QPushButton, QSlider,
    QVBoxLayout, QWidget,
)


class StatusPoller(QThread):
    status_updated = pyqtSignal(bool, str)

    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self._running = True

    def run(self):
        while self._running:
            try:
                r = requests.get(f"{self.base_url}/health", timeout=2)
                ok = r.status_code == 200 and r.json().get("ok", False)
                self.status_updated.emit(ok, "Connected")
            except Exception as e:
                self.status_updated.emit(False, str(e)[:60])
            time.sleep(1)

    def stop(self):
        self._running = False


class StimGUI(QMainWindow):
    log_signal = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Sinew — EMS Calibration")
        self.setMinimumSize(600, 700)

        self.base_url = "http://sinew.local:5001"
        self.session = requests.Session()

        self._build_ui()
        self.log_signal.connect(self._append_log)

        # Start status polling
        self.poller = StatusPoller(self.base_url)
        self.poller.status_updated.connect(self._update_status)
        self.poller.start()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(12)

        # --- Connection bar ---
        conn_layout = QHBoxLayout()
        conn_layout.addWidget(QLabel("Pi host:port"))
        self.host_input = QLineEdit(self.base_url)
        self.host_input.setFixedWidth(280)
        self.host_input.returnPressed.connect(self._update_host)
        conn_layout.addWidget(self.host_input)

        self.status_dot = QLabel("●")
        self.status_dot.setFont(QFont("Arial", 18))
        self.status_dot.setStyleSheet("color: red;")
        conn_layout.addWidget(self.status_dot)

        self.status_label = QLabel("Connecting...")
        conn_layout.addWidget(self.status_label)
        conn_layout.addStretch()
        layout.addLayout(conn_layout)

        # --- Separator ---
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        layout.addWidget(line)

        # --- Duration slider ---
        dur_layout = QHBoxLayout()
        dur_layout.addWidget(QLabel("Pulse duration (ms):"))
        self.dur_slider = QSlider(Qt.Horizontal)
        self.dur_slider.setMinimum(50)
        self.dur_slider.setMaximum(1000)
        self.dur_slider.setValue(200)
        self.dur_slider.setTickInterval(50)
        self.dur_slider.setTickPosition(QSlider.TicksBelow)
        self.dur_slider.valueChanged.connect(self._dur_changed)
        dur_layout.addWidget(self.dur_slider)
        self.dur_label = QLabel("200 ms")
        self.dur_label.setFixedWidth(60)
        dur_layout.addWidget(self.dur_label)
        layout.addLayout(dur_layout)

        # --- Finger buttons ---
        btn_group = QGroupBox("Individual Fingers")
        btn_layout = QHBoxLayout()
        finger_font = QFont("Arial", 16, QFont.Bold)

        self.btn_index = QPushButton("INDEX")
        self.btn_index.setFont(finger_font)
        self.btn_index.setMinimumHeight(60)
        self.btn_index.setStyleSheet("background-color: #2d5a27; color: white; border-radius: 8px;")
        self.btn_index.clicked.connect(lambda: self._fire_finger("INDEX"))
        btn_layout.addWidget(self.btn_index)

        self.btn_middle = QPushButton("MIDDLE")
        self.btn_middle.setFont(finger_font)
        self.btn_middle.setMinimumHeight(60)
        self.btn_middle.setStyleSheet("background-color: #2d5a27; color: white; border-radius: 8px;")
        self.btn_middle.clicked.connect(lambda: self._fire_finger("MIDDLE"))
        btn_layout.addWidget(self.btn_middle)

        self.btn_pinky = QPushButton("PINKY")
        self.btn_pinky.setFont(finger_font)
        self.btn_pinky.setMinimumHeight(60)
        self.btn_pinky.setStyleSheet("background-color: #2d5a27; color: white; border-radius: 8px;")
        self.btn_pinky.clicked.connect(lambda: self._fire_finger("PINKY"))
        btn_layout.addWidget(self.btn_pinky)

        btn_group.setLayout(btn_layout)
        layout.addWidget(btn_group)

        # --- Macro buttons ---
        macro_group = QGroupBox("Macros")
        macro_layout = QHBoxLayout()

        self.btn_grab = QPushButton("GRAB ALL")
        self.btn_grab.setMinimumHeight(45)
        self.btn_grab.setStyleSheet("background-color: #1a3d5c; color: white; border-radius: 6px;")
        self.btn_grab.clicked.connect(self._grab_all)
        macro_layout.addWidget(self.btn_grab)

        self.btn_pinch = QPushButton("PINCH")
        self.btn_pinch.setMinimumHeight(45)
        self.btn_pinch.setStyleSheet("background-color: #1a3d5c; color: white; border-radius: 6px;")
        self.btn_pinch.clicked.connect(self._pinch)
        macro_layout.addWidget(self.btn_pinch)

        self.btn_sequence = QPushButton("Test Sequence")
        self.btn_sequence.setMinimumHeight(45)
        self.btn_sequence.setStyleSheet("background-color: #3d3d1a; color: white; border-radius: 6px;")
        self.btn_sequence.clicked.connect(self._test_sequence)
        macro_layout.addWidget(self.btn_sequence)

        macro_group.setLayout(macro_layout)
        layout.addWidget(macro_group)

        # --- STOP button ---
        self.btn_stop = QPushButton("STOP ALL")
        self.btn_stop.setFont(QFont("Arial", 20, QFont.Bold))
        self.btn_stop.setMinimumHeight(70)
        self.btn_stop.setStyleSheet(
            "background-color: #cc2222; color: white; border-radius: 10px; border: 2px solid #ff4444;"
        )
        self.btn_stop.clicked.connect(self._stop_all)
        layout.addWidget(self.btn_stop)

        # --- Log panel ---
        log_group = QGroupBox("Log")
        log_layout = QVBoxLayout()
        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumBlockCount(500)
        self.log_text.setFont(QFont("Menlo", 10))
        log_layout.addWidget(self.log_text)
        log_group.setLayout(log_layout)
        layout.addWidget(log_group)

    def _update_host(self):
        self.base_url = self.host_input.text().strip()
        if not self.base_url.startswith("http"):
            self.base_url = "http://" + self.base_url
        self.host_input.setText(self.base_url)
        self.poller.base_url = self.base_url
        self._log(f"Host updated: {self.base_url}")

    def _dur_changed(self, val):
        self.dur_label.setText(f"{val} ms")

    def _update_status(self, ok: bool, msg: str):
        if ok:
            self.status_dot.setStyleSheet("color: #22cc22;")
            self.status_label.setText("Connected")
        else:
            self.status_dot.setStyleSheet("color: #cc2222;")
            self.status_label.setText(msg)

    def _log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self.log_signal.emit(f"[{ts}] {msg}")

    def _append_log(self, text: str):
        self.log_text.appendPlainText(text)

    def _post(self, endpoint: str, data: dict = None) -> dict:
        """POST to Pi receiver. Returns response dict or error dict."""
        url = f"{self.base_url}{endpoint}"
        try:
            if data:
                r = self.session.post(url, json=data, timeout=3)
            else:
                r = self.session.post(url, timeout=3)
            resp = r.json()
            self._log(f"POST {endpoint} {json.dumps(data) if data else ''} → {r.status_code} {json.dumps(resp)}")
            return resp
        except Exception as e:
            self._log(f"POST {endpoint} FAILED: {e}")
            return {"error": str(e)}

    def _fire_finger(self, finger: str):
        dur = self.dur_slider.value()
        threading.Thread(
            target=self._post,
            args=("/stimulate", {"finger": finger, "action": "ON", "duration_ms": dur}),
            daemon=True,
        ).start()

    def _stop_all(self):
        threading.Thread(target=self._post, args=("/stop",), daemon=True).start()

    def _grab_all(self):
        """Fire all three fingers in sequence (multiplexed)."""
        dur = self.dur_slider.value()

        def _run():
            for finger in ["INDEX", "MIDDLE", "PINKY"]:
                self._post("/stimulate", {"finger": finger, "action": "ON", "duration_ms": min(dur, 50)})
                time.sleep(0.06)
            time.sleep(dur / 1000.0)
            self._post("/stop")

        threading.Thread(target=_run, daemon=True).start()

    def _pinch(self):
        """Fire index + middle alternating."""
        dur = self.dur_slider.value()

        def _run():
            for finger in ["INDEX", "MIDDLE"]:
                self._post("/stimulate", {"finger": finger, "action": "ON", "duration_ms": min(dur, 50)})
                time.sleep(0.06)
            time.sleep(dur / 1000.0)
            self._post("/stop")

        threading.Thread(target=_run, daemon=True).start()

    def _test_sequence(self):
        """Fire INDEX → MIDDLE → PINKY with 500ms between each."""
        dur = self.dur_slider.value()

        def _run():
            for finger in ["INDEX", "MIDDLE", "PINKY"]:
                self._log(f"Sequence: {finger}")
                self._post("/stimulate", {"finger": finger, "action": "ON", "duration_ms": dur})
                time.sleep(0.5)
            self._post("/stop")
            self._log("Sequence complete")

        threading.Thread(target=_run, daemon=True).start()

    def closeEvent(self, event):
        self.poller.stop()
        self.poller.wait(2000)
        # Safety: stop all on exit
        try:
            self.session.post(f"{self.base_url}/stop", timeout=1)
        except Exception:
            pass
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # Dark palette
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(30, 30, 30))
    palette.setColor(QPalette.WindowText, QColor(220, 220, 220))
    palette.setColor(QPalette.Base, QColor(20, 20, 20))
    palette.setColor(QPalette.Text, QColor(200, 200, 200))
    palette.setColor(QPalette.Button, QColor(50, 50, 50))
    palette.setColor(QPalette.ButtonText, QColor(220, 220, 220))
    app.setPalette(palette)

    window = StimGUI()
    window.show()
    sys.exit(app.exec_())
