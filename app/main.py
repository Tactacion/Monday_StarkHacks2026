"""
app/main.py

End-to-end demo entry point. Wires VisionCapture, Orchestrator, VoiceListener,
ManualTrigger, and the cv2 overlay window into one runnable process.

Run: `python -m app.main`

Structure is deliberately split so tests can exercise startup and shutdown
without a camera:

    load_config(path)     -> dict
    build_stack(cfg)      -> Stack dataclass
    run_overlay_loop(st)  -> None        cv2 windowing, requires display
    shutdown(st, reason)  -> None        safe to call more than once
    main()                -> int         ties them together with signal handlers
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from dotenv import load_dotenv
    # First-wins semantics: repo-root .env overrides, testing/.env is the
    # default the Pi credentials/URLs live in.
    load_dotenv(_REPO_ROOT / ".env")
    load_dotenv(_REPO_ROOT / "testing" / ".env")
except ImportError:
    pass

import cv2
import numpy as np
import requests
import yaml

from app.orchestrator import Orchestrator
from app.state import Confidence, Mode, SystemState, TriggerEvent
from app.vision import VisionCapture
from app.voice import ManualTrigger, VoiceListener

log = logging.getLogger("sinew.main")

DEFAULT_CONFIG_PATH = _REPO_ROOT / "config.yaml"


# =============================================================================
# Config
# =============================================================================


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> dict:
    with path.open("r") as f:
        return yaml.safe_load(f) or {}


# =============================================================================
# Stack
# =============================================================================


@dataclass
class Stack:
    """Bundle of live components. Shutdown walks this from top to bottom."""
    config: dict
    vision: VisionCapture
    tts_engine: Any  # pyttsx3 Engine or None
    orchestrator: Orchestrator
    voice_listener: Optional[VoiceListener]
    manual_trigger: ManualTrigger
    voice_enabled: bool
    receiver_url: str
    shutdown_done: bool = field(default=False)


def _build_tts_engine() -> Any:
    """Initialize pyttsx3 if available. Returns the engine or None."""
    try:
        import pyttsx3  # type: ignore
    except ImportError:
        log.warning("pyttsx3 not installed, TTS disabled")
        return None
    try:
        engine = pyttsx3.init()
        return engine
    except Exception as e:
        log.warning("pyttsx3 init failed, TTS disabled: %s", e)
        return None


def _build_confidence_scale(cfg: dict) -> dict:
    raw = cfg.get("orchestrator", {}).get("confidence_scale", {})
    # Map string keys to Confidence enum members.
    return {
        Confidence.HIGH: float(raw.get("high", 1.0)),
        Confidence.MEDIUM: float(raw.get("medium", 0.75)),
        Confidence.LOW: float(raw.get("low", 0.5)),
    }


def build_stack(cfg: dict) -> Stack:
    """
    Construct every component in the required order:

      1. VisionCapture (fails fast if camera missing)
      2. TTS engine (soft-fails to None)
      3. Orchestrator (needs vision and tts)
      4. VoiceListener (soft-fails if audio deps missing)
      5. ManualTrigger (always works)

    Both trigger paths register the same callback: orchestrator.on_trigger.
    Orchestrator's own state check drops triggers while it is not IDLE, so
    concurrent triggers do not conflict.
    """
    log.info("build_stack: starting")

    # 1. Vision. `frame_url` (or SINEW_CAMERA_URL) routes capture to the Pi
    # frame server. If unset, fall back to a local webcam.
    frame_url = cfg.get("frame_url") or os.environ.get("SINEW_CAMERA_URL") or None
    vision = VisionCapture(
        frame_url=frame_url,
        camera_index=int(cfg.get("camera_index", 0)),
        width=int(cfg.get("camera_width", 1280)),
        height=int(cfg.get("camera_height", 720)),
    )
    log.info("build_stack: vision ready (mode=%s)", "http" if frame_url else "local")

    # 2. TTS (optional)
    tts_engine = _build_tts_engine()

    # 3. Orchestrator
    receiver_url = (
        os.environ.get("SINEW_RECEIVER_URL")
        or cfg.get("receiver_url", "http://127.0.0.1:5001")
    )
    orch_cfg = cfg.get("orchestrator", {})
    orchestrator = Orchestrator(
        vision=vision,
        receiver_url=receiver_url,
        tts_engine=tts_engine,
        abort_window_ms=int(orch_cfg.get("abort_window_ms", 500)),
        confidence_scale=_build_confidence_scale(cfg),
    )
    log.info("build_stack: orchestrator ready")

    # 4. VoiceListener (soft)
    voice_cfg = cfg.get("voice", {})
    voice_enabled = True
    voice_listener: Optional[VoiceListener] = None
    try:
        voice_listener = VoiceListener(
            wake_phrase=voice_cfg.get("wake_phrase"),
            model_size=voice_cfg.get("whisper_model"),
            device=voice_cfg.get("whisper_device"),
            vad_threshold=voice_cfg.get("vad_threshold"),
            input_device=voice_cfg.get("input_device"),
            on_trigger=orchestrator.on_trigger,
        )
        voice_listener.start()
        log.info("build_stack: voice ready")
    except Exception as e:
        voice_enabled = False
        voice_listener = None
        print("=" * 64)
        print("Voice disabled: typing fallback active.")
        print(f"  reason: {e}")
        print("=" * 64)
        log.warning("voice disabled: %s", e)

    # 5. ManualTrigger (always)
    manual_trigger = ManualTrigger(on_trigger=orchestrator.on_trigger)
    manual_trigger.start()
    log.info("build_stack: manual trigger ready")

    return Stack(
        config=cfg,
        vision=vision,
        tts_engine=tts_engine,
        orchestrator=orchestrator,
        voice_listener=voice_listener,
        manual_trigger=manual_trigger,
        voice_enabled=voice_enabled,
        receiver_url=receiver_url,
    )


# =============================================================================
# Shutdown
# =============================================================================


def shutdown(stack: Stack, reason: str = "shutdown") -> None:
    """
    Orderly teardown. Safe to call more than once. Swallows per-component
    exceptions so a failing component cannot block the rest of the cleanup.
    """
    if stack.shutdown_done:
        return
    stack.shutdown_done = True
    log.info("shutdown: reason=%s", reason)

    _try(lambda: stack.orchestrator.abort(reason), "orchestrator.abort")
    _try(lambda: stack.voice_listener and stack.voice_listener.stop(), "voice.stop")
    _try(lambda: stack.manual_trigger.stop(), "manual.stop")
    _try(lambda: stack.vision.release(), "vision.release")

    # Belt and suspenders: fire /stop one more time regardless of state.
    # The firmware watchdog would save us in 3 s anyway, but this closes
    # the window earlier.
    try:
        requests.post(f"{stack.receiver_url}/stop", json={}, timeout=1.0)
    except requests.RequestException as e:
        log.error("final /stop POST failed: %s (firmware watchdog is the backstop)", e)


def _try(fn, label: str) -> None:
    try:
        fn()
    except Exception as e:
        log.warning("shutdown: %s raised %s: %s", label, type(e).__name__, e)


# =============================================================================
# Overlay
# =============================================================================


STATE_COLORS = {
    SystemState.IDLE:          (0, 150, 0),       # green
    SystemState.LISTENING:     (0, 200, 200),     # yellow
    SystemState.CAPTURING:     (0, 200, 200),     # yellow
    SystemState.PROCESSING:    (0, 200, 200),     # yellow
    SystemState.ACKNOWLEDGING: (200, 120, 0),     # blue
    SystemState.EXECUTING:     (0, 0, 200),       # red
}

CONFIDENCE_COLORS = {
    Confidence.HIGH:   (0, 200, 0),
    Confidence.MEDIUM: (0, 220, 220),
    Confidence.LOW:    (0, 0, 220),
}


@dataclass
class RefusalFlash:
    """Tracks the active refusal overlay."""
    response_id: int        # id() of the BrainResponse that triggered this flash
    started_at: float       # time.monotonic()
    refusal_text: str
    duration_s: float


def render_overlay(
    frame: np.ndarray,
    stack: Stack,
    flash: Optional[RefusalFlash],
    fps: float = 0.0,
) -> np.ndarray:
    """
    Draw the full overlay on a copy of the frame. Every panel pulls live
    data from the orchestrator. No mutation of state happens here; the
    caller drives flash lifecycle.
    """
    out = frame.copy()
    h, w = out.shape[:2]
    orch = stack.orchestrator
    overlay_cfg = stack.config.get("overlay", {})

    state = orch.get_state()
    mode = orch.get_mode()
    response = orch.get_last_response()
    trigger = orch.get_last_trigger()
    cmd_idx = orch.get_current_command_index()
    recent = orch.get_recent_commands(n=3)

    # ---------- confidence border (drawn first so text sits on top) ----------
    if response is not None and not response.is_refusal:
        border_color = CONFIDENCE_COLORS.get(response.confidence)
        border_w = int(overlay_cfg.get("confidence_border_width", 8))
        if border_color is not None:
            cv2.rectangle(out, (0, 0), (w - 1, h - 1), border_color, thickness=border_w)

    # ---------- state badge (top-left) ----------
    badge_h = int(overlay_cfg.get("state_badge_size", 24)) + 16
    badge_w = 220
    badge_color = STATE_COLORS.get(state, (100, 100, 100))
    cv2.rectangle(out, (14, 14), (14 + badge_w, 14 + badge_h), badge_color, thickness=-1)
    _put_text(
        out, state.value.upper(), (26, 14 + badge_h - 10),
        size=overlay_cfg.get("state_badge_size", 24) / 30.0,
        color=(255, 255, 255), thickness=2, outline=False,
    )

    # ---------- mode badge (below state badge) ----------
    mode_colors = {
        Mode.GRASP: (0, 150, 0),
        Mode.PIANO: (180, 100, 0),
        Mode.SIGN:  (150, 0, 150),
    }
    mode_y = 14 + badge_h + 6
    mode_color = mode_colors.get(mode, (100, 100, 100))
    cv2.rectangle(out, (14, mode_y), (14 + 140, mode_y + badge_h), mode_color, thickness=-1)
    _put_text(
        out, mode.value.upper(), (26, mode_y + badge_h - 10),
        size=overlay_cfg.get("state_badge_size", 24) / 30.0,
        color=(255, 255, 255), thickness=2, outline=False,
    )

    # ---------- transcript (top-right) ----------
    transcript_text = (
        _truncate(trigger.transcript, 60) if trigger is not None else "waiting for voice…"
    )
    font_scale = overlay_cfg.get("transcript_font_size", 16) / 24.0
    (tw, th), _ = cv2.getTextSize(transcript_text, cv2.FONT_HERSHEY_SIMPLEX, font_scale, 2)
    _put_text(
        out, transcript_text, (w - tw - 14, 14 + th),
        size=font_scale, color=(255, 255, 255), thickness=2, outline=True,
    )

    # ---------- command in flight (bottom-left) ----------
    cmd_text = "idle"
    if state is SystemState.EXECUTING and response is not None and response.commands:
        idx = min(cmd_idx, len(response.commands) - 1)
        c = response.commands[idx]
        cmd_text = f"{c.finger.value} {c.action.value} {c.duration_ms}ms"
    _put_text(
        out, cmd_text, (14, h - 18),
        size=overlay_cfg.get("transcript_font_size", 16) / 24.0,
        color=(255, 255, 255), thickness=2, outline=True,
    )

    # ---------- command history (bottom-right) ----------
    if recent:
        y = h - 14
        font = overlay_cfg.get("transcript_font_size", 16) / 24.0
        for c in reversed(recent):  # newest at bottom
            line = f"{c.finger.value} {c.action.value} {c.duration_ms}ms"
            (lw, lh), _ = cv2.getTextSize(line, cv2.FONT_HERSHEY_SIMPLEX, font, 2)
            _put_text(out, line, (w - lw - 14, y), size=font,
                      color=(220, 220, 220), thickness=2, outline=True)
            y -= lh + 8

    # ---------- fps (tiny, top-center) ----------
    if fps > 0:
        _put_text(out, f"{fps:.0f} fps", (w // 2 - 30, 30),
                  size=0.5, color=(180, 180, 180), thickness=1, outline=True)

    # ---------- refusal flash (fullscreen, applied last so it sits on top) ----------
    if flash is not None:
        elapsed = time.monotonic() - flash.started_at
        if elapsed < flash.duration_s:
            # Linear alpha decay over the final 1 second.
            tail = 1.0
            base_alpha = 0.5
            if elapsed > flash.duration_s - tail:
                alpha = base_alpha * max(0.0, (flash.duration_s - elapsed) / tail)
            else:
                alpha = base_alpha
            red = np.zeros_like(out)
            red[:, :, 2] = 180  # BGR red
            cv2.addWeighted(red, alpha, out, 1.0 - alpha, 0, dst=out)

            _put_text(
                out, "REFUSED", (w // 2 - 120, h // 2 - 30),
                size=1.4, color=(255, 255, 255), thickness=3, outline=True,
            )
            reason = _truncate(flash.refusal_text, 60)
            (rw, _), _ = cv2.getTextSize(reason, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 2)
            _put_text(
                out, reason, (w // 2 - rw // 2, h // 2 + 30),
                size=0.8, color=(255, 255, 255), thickness=2, outline=True,
            )

    return out


def _put_text(img, text, org, size, color, thickness, outline=True):
    if outline:
        cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, size,
                    (0, 0, 0), thickness + 2, cv2.LINE_AA)
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, size,
                color, thickness, cv2.LINE_AA)


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


# =============================================================================
# Overlay loop
# =============================================================================


WINDOW_NAME = "Sinew"


def run_overlay_loop(stack: Stack) -> None:
    """
    Render the webcam feed with overlays at ~30 fps. Blocks the main thread
    until q/ESC is pressed or the process is signaled. Hotkeys:

      SPACE   pause for stdin prompt, fire orchestrator.on_trigger
      S       orchestrator.abort("hotkey stop")
      Q, ESC  shutdown and return
    """
    flash: Optional[RefusalFlash] = None
    last_frame_t = time.monotonic()
    fps_ema = 0.0

    try:
        cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_AUTOSIZE)
    except cv2.error as e:
        log.error("cv2.namedWindow failed: %s (no display?)", e)
        raise

    while True:
        # Pull the latest frame. If the grabber hasn't produced anything
        # yet, show a black placeholder.
        try:
            frame = stack.vision.get_latest_frame()
        except RuntimeError as e:
            log.warning("vision timeout: %s", e)
            frame = np.zeros((480, 640, 3), dtype=np.uint8)

        # Refusal flash lifecycle: if a new refusal response landed, start a flash.
        resp = stack.orchestrator.get_last_response()
        if resp is not None and resp.is_refusal:
            if flash is None or flash.response_id != id(resp):
                flash_duration = float(
                    stack.config.get("overlay", {}).get("refusal_flash_duration_s", 3.0)
                )
                flash = RefusalFlash(
                    response_id=id(resp),
                    started_at=time.monotonic(),
                    refusal_text=resp.refusal or resp.acknowledgement or "refusal",
                    duration_s=flash_duration,
                )
        if flash is not None and (time.monotonic() - flash.started_at) >= flash.duration_s:
            flash = None

        # FPS estimate (exponential moving average for display smoothness).
        now = time.monotonic()
        dt = max(now - last_frame_t, 1e-6)
        last_frame_t = now
        inst_fps = 1.0 / dt
        fps_ema = 0.9 * fps_ema + 0.1 * inst_fps if fps_ema > 0 else inst_fps

        annotated = render_overlay(frame, stack, flash, fps=fps_ema)
        cv2.imshow(WINDOW_NAME, annotated)

        key = cv2.waitKey(1) & 0xFF
        if key == 0xFF:
            continue
        if key in (ord("q"), 27):  # q or ESC
            log.info("hotkey quit")
            break
        if key == ord("s"):
            log.info("hotkey stop")
            stack.orchestrator.abort("hotkey stop")
        elif key == ord("g"):
            stack.orchestrator.set_mode(Mode.GRASP)
            print("[MODE] GRASP — AI-assisted grasping")
        elif key == ord("p"):
            stack.orchestrator.set_mode(Mode.PIANO)
            print("[MODE] PIANO — finger exercises (MARY/HOTCROSS/SCALE/TRILL/ARPEGGIO)")
        elif key == ord("l"):
            stack.orchestrator.set_mode(Mode.SIGN)
            print("[MODE] SIGN — ASL fingerspelling")
        elif key == ord("d"):
            _fire_dance(stack)
        elif key == ord("t"):
            _fire_stress(stack)
        elif key == 32:  # SPACE
            _prompt_manual(stack)

    cv2.destroyAllWindows()


def _prompt_manual(stack: Stack) -> None:
    """SPACE handler: mode-aware prompt, fires orchestrator.on_trigger."""
    mode = stack.orchestrator.get_mode()
    print()
    try:
        if mode is Mode.PIANO:
            text = input("Song (MARY/HOTCROSS/SCALE/TRILL/ARPEGGIO): ").strip()
        elif mode is Mode.SIGN:
            text = input("Word to spell (e.g. HELLO): ").strip()
        else:
            text = input("Enter intent (or blank to cancel): ").strip()
    except EOFError:
        return
    if not text:
        return
    stack.orchestrator.on_trigger(
        TriggerEvent(transcript=text, timestamp=time.time())
    )


def _fire_dance(stack: Stack) -> None:
    """D hotkey: run the dance test sequence."""
    print("[TEST] Running DANCE sequence...")
    try:
        r = requests.post(f"{stack.receiver_url}/dance", json={}, timeout=2.0)
        print(f"[TEST] DANCE -> {r.status_code}")
    except requests.RequestException as e:
        print(f"[TEST] DANCE failed: {e}")


def _fire_stress(stack: Stack) -> None:
    """T hotkey: run the full stress test."""
    print("[TEST] Running STRESS test...")
    try:
        r = requests.post(f"{stack.receiver_url}/stress", json={}, timeout=2.0)
        print(f"[TEST] STRESS -> {r.status_code}")
    except requests.RequestException as e:
        print(f"[TEST] STRESS failed: {e}")


# =============================================================================
# Signal handling and main
# =============================================================================


_shutdown_flag = threading.Event()


def _install_signal_handlers(stack: Stack) -> None:
    def handler(signum, frame):
        log.info("signal %s received, shutting down", signum)
        _shutdown_flag.set()
        shutdown(stack, f"signal {signum}")
        # cv2.destroyAllWindows happens inside run_overlay_loop on its exit
        # path. We push a break by raising KeyboardInterrupt on the main
        # thread, but that only works if this handler runs on the main
        # thread, which signal.signal guarantees.

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            # Windows won't allow SIGTERM, and signal only works on main thread.
            pass


def main(config_path: Path = DEFAULT_CONFIG_PATH) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        cfg = load_config(config_path)
    except FileNotFoundError:
        print(f"config not found at {config_path}")
        return 1

    try:
        stack = build_stack(cfg)
    except RuntimeError as e:
        # Typically: camera not available.
        print(f"startup failed: {e}")
        return 1

    _install_signal_handlers(stack)

    try:
        run_overlay_loop(stack)
    finally:
        shutdown(stack, "main exit")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
