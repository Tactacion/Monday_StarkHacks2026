"""
Sinew Brain — Claude API wrapper that converts (camera frame, user intent) into
a validated sequence of high-level EMS action macros.
"""

import base64
import json
import logging
import logging.handlers
import os
import sys
from dataclasses import dataclass
from typing import Optional

import anthropic

# --- Logging ---
os.makedirs("logs", exist_ok=True)
logger = logging.getLogger("brain")
logger.setLevel(logging.DEBUG)
handler = logging.handlers.RotatingFileHandler(
    "logs/brain.log", maxBytes=1_000_000, backupCount=3
)
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(request_id)s] %(message)s"))
logger.addHandler(handler)

VALID_ACTIONS = {"GRAB_ALL", "PINCH", "POINT_INDEX", "POINT_MIDDLE", "POINT_PINKY", "RELEASE"}
MAX_ACTIONS = 4
MAX_DURATION_MS = 1500

_request_counter = 0


def _next_request_id():
    global _request_counter
    _request_counter += 1
    return f"req-{_request_counter:04d}"


@dataclass
class Action:
    action: str       # one of VALID_ACTIONS
    duration_ms: int  # 0-1500


class Brain:
    def __init__(
        self,
        api_key: str,
        system_prompt_path: str = "prompts/system_prompt.txt",
        model: Optional[str] = None,
    ):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model or os.environ.get("SINEW_MODEL", "claude-sonnet-4-6")

        with open(system_prompt_path, "r") as f:
            self.system_prompt = f.read().strip()

    def plan(self, frame_jpeg: bytes, intent: str) -> Optional[list[Action]]:
        """
        Send a camera frame and user intent to Claude.
        Returns a validated list of Actions, or None if invalid/empty.
        """
        rid = _next_request_id()
        extra = {"request_id": rid}

        logger.info("Calling Claude API", extra=extra)
        logger.debug(f"Intent: {intent}", extra=extra)
        logger.debug(f"Frame size: {len(frame_jpeg)} bytes", extra=extra)

        frame_b64 = base64.standard_b64encode(frame_jpeg).decode("utf-8")

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=400,
                temperature=0,
                system=self.system_prompt,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": frame_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": intent,
                        },
                    ],
                }],
            )
        except Exception as e:
            logger.error(f"API call failed: {e}", extra=extra)
            return None

        raw_text = response.content[0].text.strip()
        logger.info(f"Raw response: {raw_text}", extra=extra)

        # Strip markdown code fences if present
        if raw_text.startswith("```"):
            lines = raw_text.split("\n")
            # Remove first and last lines (fences)
            lines = [l for l in lines if not l.strip().startswith("```")]
            raw_text = "\n".join(lines).strip()

        # Parse JSON
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse failed: {e}", extra=extra)
            return None

        # Validate
        actions = self._validate(parsed, rid)
        if actions is not None:
            logger.info(f"Valid plan: {[f'{a.action}({a.duration_ms}ms)' for a in actions]}", extra=extra)
        else:
            logger.warning("Validation failed", extra=extra)
        return actions

    def _validate(self, parsed, rid: str) -> Optional[list[Action]]:
        extra = {"request_id": rid}

        if not isinstance(parsed, list):
            logger.error(f"Expected list, got {type(parsed).__name__}", extra=extra)
            return None

        if len(parsed) == 0:
            logger.info("Empty plan (scene mismatch)", extra=extra)
            return None

        if len(parsed) > MAX_ACTIONS:
            logger.error(f"Too many actions: {len(parsed)} > {MAX_ACTIONS}", extra=extra)
            return None

        actions = []
        for i, item in enumerate(parsed):
            if not isinstance(item, dict):
                logger.error(f"Action {i} is not a dict", extra=extra)
                return None

            if "action" not in item or "duration_ms" not in item:
                logger.error(f"Action {i} missing required keys", extra=extra)
                return None

            action_name = item["action"]
            duration = item["duration_ms"]

            if action_name not in VALID_ACTIONS:
                logger.error(f"Action {i}: unknown action '{action_name}'", extra=extra)
                return None

            if not isinstance(duration, int) or duration < 0 or duration > MAX_DURATION_MS:
                logger.error(f"Action {i}: invalid duration_ms {duration}", extra=extra)
                return None

            actions.append(Action(action=action_name, duration_ms=duration))

        return actions


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Set ANTHROPIC_API_KEY in .env", file=sys.stderr)
        sys.exit(1)

    test_frame_path = "test_frame.jpg"
    if not os.path.exists(test_frame_path):
        print(f"Drop a test image at {test_frame_path} and re-run", file=sys.stderr)
        sys.exit(1)

    with open(test_frame_path, "rb") as f:
        frame_bytes = f.read()

    brain = Brain(api_key=api_key)
    result = brain.plan(frame_bytes, "grab the object in front of me")
    print(f"Result: {result}")
