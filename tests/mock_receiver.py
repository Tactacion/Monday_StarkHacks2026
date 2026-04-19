#!/usr/bin/env python3
"""
tests/mock_receiver.py

Mock hardware receiver. Matches the real /stimulate /stop /status /health
contract exactly. No serial port, no Arduino needed.

Two ways to use it:

1. Script. Runs a Flask dev server on port 5001 (or MOCK_RECEIVER_PORT).

       python tests/mock_receiver.py

2. Library. Tests import create_mock_app() to get a fresh Flask app with
   an in-memory request log and an optional per-POST sleep for timing
   tests.

       app = create_mock_app()
       app.config["STIMULATE_SLEEP_S"] = 0.1
       # start werkzeug make_server on your chosen port...
       app.config["REQUESTS"]  # list of (path, payload_or_None) tuples
"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import List, Tuple

sys.path.insert(0, str(Path(__file__).parent.parent))

from flask import Flask, jsonify, request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MOCK] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


RequestLog = List[Tuple[str, dict]]


def create_mock_app() -> Flask:
    """Build a fresh Flask app. Each call returns a new app with its own log."""
    app = Flask("sinew_mock_receiver")

    app.config["REQUESTS"] = []          # list of (path, payload dict) tuples
    app.config["STIMULATE_SLEEP_S"] = 0.0  # artificial delay on /stimulate

    @app.route("/stimulate", methods=["POST"])
    def stimulate():
        data = request.get_json(force=True, silent=True) or {}
        app.config["REQUESTS"].append(("/stimulate", data))
        sleep_s = float(app.config.get("STIMULATE_SLEEP_S", 0.0))
        if sleep_s > 0:
            time.sleep(sleep_s)
        finger = data.get("finger", "UNKNOWN")
        action = data.get("action", "UNKNOWN")
        duration_ms = data.get("duration_ms", 0)
        logger.info(
            "stimulate finger=%s action=%s duration_ms=%s", finger, action, duration_ms
        )
        ack = f"MOCK:OK:{finger}:{action}"
        return jsonify({"status": "ok", "ack": ack})

    @app.route("/stop", methods=["POST"])
    def stop():
        app.config["REQUESTS"].append(("/stop", {}))
        logger.info("STOP")
        return jsonify({"status": "ok"})

    @app.route("/status", methods=["GET"])
    def status():
        return jsonify(
            {"connected": True, "last_ack": "MOCK", "watchdog_remaining_ms": 3000}
        )

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"alive": True})

    return app


if __name__ == "__main__":
    port = int(os.environ.get("MOCK_RECEIVER_PORT", "5001"))
    host = os.environ.get("MOCK_RECEIVER_HOST", "0.0.0.0")
    app = create_mock_app()
    logger.info("Mock receiver starting on %s:%d", host, port)
    logger.info("Endpoints: POST /stimulate  POST /stop  GET /status  GET /health")
    app.run(host=host, port=port, debug=False)
