"""
Brain module — calls Claude with a camera frame and voice transcript,
validates the response against the brain contract, returns a BrainResponse.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

import anthropic

from app.state import Action, BrainResponse, Command, Confidence, Finger

DEFAULT_MODEL = "claude-sonnet-4-5-20250929"
SYSTEM_PROMPT_PATH = Path("prompts/system_prompt.txt")
LOG_PATH = Path("app/logs/brain.log")

Path("app/logs").mkdir(parents=True, exist_ok=True)

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
_file_handler = logging.FileHandler(LOG_PATH)
_file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_file_handler)

_client: Optional[anthropic.Anthropic] = None
_system_prompt: Optional[str] = None
_system_prompt_mtime: float = 0.0


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


def _get_system_prompt() -> str:
    """
    Load system_prompt.txt, reloading whenever the file changes on disk.
    This means edits to the prompt take effect immediately without restarting
    the process — critical during prompt iteration with test_brain.py.
    """
    global _system_prompt, _system_prompt_mtime
    try:
        mtime = SYSTEM_PROMPT_PATH.stat().st_mtime
    except FileNotFoundError:
        raise FileNotFoundError(f"System prompt not found at {SYSTEM_PROMPT_PATH}")
    if _system_prompt is None or mtime != _system_prompt_mtime:
        _system_prompt = SYSTEM_PROMPT_PATH.read_text()
        _system_prompt_mtime = mtime
        logger.info("System prompt loaded from %s (mtime=%.0f)", SYSTEM_PROMPT_PATH, mtime)
    return _system_prompt


def plan_grasp(frame_b64: str, transcript: str) -> Optional[BrainResponse]:
    """
    Call Claude with the base64 JPEG frame and voice transcript.
    Returns a validated BrainResponse or None on API/parse/validation failure.
    Retries once with an explicit JSON reminder if the first attempt fails to parse.
    """
    model = os.environ.get("SINEW_CLAUDE_MODEL", DEFAULT_MODEL)
    client = _get_client()
    base_system = _get_system_prompt()

    logger.info("plan_grasp | transcript='%s' | model=%s", transcript, model)

    messages = [
        {
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
                    "text": f"User said: {transcript}",
                },
            ],
        }
    ]

    for attempt in range(2):
        system = base_system
        if attempt == 1:
            system = base_system + "\n\nCRITICAL: Respond with ONLY valid JSON. No markdown fences. No preamble."

        try:
            resp = client.messages.create(
                model=model,
                max_tokens=500,
                system=system,
                messages=messages,
            )
            raw = resp.content[0].text.strip()
            logger.info("Raw response (attempt %d): %s", attempt + 1, raw)

            cleaned = _strip_fences(raw)

            try:
                data = json.loads(cleaned)
            except json.JSONDecodeError as exc:
                logger.warning("JSON parse failure (attempt %d): %s | raw='%s'", attempt + 1, exc, raw)
                continue

            result = validate_response(data)
            if result is not None:
                logger.info(
                    "Validated | confidence=%s refusal=%s grip=%s commands=%d",
                    result.confidence.value,
                    result.refusal,
                    result.grip_type.value,
                    len(result.commands),
                )
            else:
                logger.warning("Validation failed | data=%s", data)
            return result

        except anthropic.APIError as exc:
            logger.error("API error (attempt %d): %s", attempt + 1, exc)
            continue

    logger.error("plan_grasp failed after 2 attempts for transcript='%s'", transcript)
    return None


def _strip_fences(text: str) -> str:
    """Remove markdown code fences defensively."""
    if not text.startswith("```"):
        return text
    lines = text.split("\n")
    # Drop first line (```json or ```) and last line (```)
    inner = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
    return "\n".join(inner).strip()


def validate_response(data: dict) -> Optional[BrainResponse]:
    """
    Validate Claude's JSON dict against the brain contract.
    Returns BrainResponse on success or None on any violation (logged).
    """
    required = {"acknowledgement", "confidence", "refusal", "commands"}
    missing = required - set(data.keys())
    if missing:
        logger.warning("validate: missing keys %s", missing)
        return None

    if data["confidence"] not in ("high", "medium", "low"):
        logger.warning("validate: invalid confidence '%s'", data["confidence"])
        return None
    confidence = Confidence(data["confidence"])

    has_refusal = data["refusal"] is not None and str(data["refusal"]).strip() != ""
    has_commands = bool(data["commands"])

    if has_refusal and has_commands:
        logger.warning("validate: refusal and commands both present — contract violation")
        return None

    if not isinstance(data["commands"], list):
        logger.warning("validate: commands is not a list")
        return None

    if len(data["commands"]) > 8:
        logger.warning("validate: command sequence length %d exceeds 8", len(data["commands"]))
        return None

    valid_fingers = {f.value for f in Finger}
    valid_actions = {a.value for a in Action}
    commands: list[Command] = []

    for i, cmd in enumerate(data["commands"]):
        if not isinstance(cmd, dict):
            logger.warning("validate: command %d is not a dict", i)
            return None
        if cmd.get("finger") not in valid_fingers:
            logger.warning("validate: command %d invalid finger '%s'", i, cmd.get("finger"))
            return None
        if cmd.get("action") not in valid_actions:
            logger.warning("validate: command %d invalid action '%s'", i, cmd.get("action"))
            return None
        dur = cmd.get("duration_ms", 0)
        if not isinstance(dur, int) or not (0 <= dur <= 1000):
            logger.warning("validate: command %d invalid duration_ms %s", i, dur)
            return None
        commands.append(Command(
            finger=Finger(cmd["finger"]),
            action=Action(cmd["action"]),
            duration_ms=dur,
        ))

    return BrainResponse(
        acknowledgement=str(data["acknowledgement"]),
        confidence=confidence,
        refusal=str(data["refusal"]) if has_refusal else None,
        commands=commands,
    )


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    from app.vision import VisionCapture

    test_cases = [
        ("tests/fixtures/mug.jpg", "grab the cup"),
        ("tests/fixtures/pen.jpg", "grab the pen"),
        ("tests/fixtures/key.jpg", "grab the key"),
        ("tests/fixtures/unsafe.jpg", "grab the knife"),
        ("tests/fixtures/empty.jpg", "grab it"),
    ]

    vision = VisionCapture()

    for fixture_path, transcript in test_cases:
        print(f"\n{'='*55}")
        print(f"Fixture:    {fixture_path}")
        print(f"Transcript: {transcript}")
        try:
            frame_b64 = vision.load_file_as_b64(fixture_path)
            result = plan_grasp(frame_b64, transcript)
            if result:
                print(f"Confidence: {result.confidence.value}")
                print(f"Refusal:    {result.refusal}")
                print(f"Grip:       {result.grip_type.value}")
                print(f"Commands:   {[c.to_dict() for c in result.commands]}")
                print(f"ACK:        {result.acknowledgement}")
            else:
                print("Result: None (API / parse / validation failure)")
        except FileNotFoundError:
            print(f"Fixture not found: {fixture_path} — replace placeholder per TODO.md")
