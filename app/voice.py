"""
app/voice.py

Two ways to get a voice intent into the orchestrator:

  VoiceListener  continuous microphone path with wake phrase and VAD-gated
                 intent capture. Transcribes with faster-whisper.

  ManualTrigger  fallback for demos or dev boxes without a usable mic. Waits
                 for the spacebar, then reads a typed intent from stdin.

Both classes fire an on_trigger callback with a TriggerEvent when an intent
is ready. The orchestrator is blind to which path fired it.

Design notes
------------
The wake-phrase detector runs every 500 ms over a three-second rolling
buffer of audio. That is expensive on CPU because faster-whisper is not a
wake-word engine, but it keeps the dependency surface small. If CPU budget
ever becomes a problem, swap the wake step for picovoice/porcupine while
keeping the intent-capture + transcribe step here.

Imports for the heavy audio and whisper deps are deferred so this module
can be imported on a machine without them installed. Constructors raise
ImportError with install hints when the dep is actually needed.

Environment overrides
---------------------
  SINEW_WAKE_PHRASE     default "hey sinew"
  SINEW_WHISPER_MODEL   default "base"
  SINEW_WHISPER_DEVICE  default "cpu"
  SINEW_VAD_THRESHOLD   default 500
  SINEW_AUDIO_INPUT     default None (system default input). Accepts either
                        an integer device index or a substring of the
                        device name. See `python -m app.voice --list-devices`.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Optional

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.state import TriggerEvent

logger = logging.getLogger(__name__)

# Audio constants. 16 kHz mono int16 matches what faster-whisper expects
# and what most USB mics deliver without resampling.
SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_MS = 500
CHUNK_SAMPLES = SAMPLE_RATE * CHUNK_MS // 1000
ROLLING_WINDOW_CHUNKS = 6  # 3 s of audio for wake detection
VAD_WINDOW_MS = 100
VAD_WINDOW_SAMPLES = SAMPLE_RATE * VAD_WINDOW_MS // 1000
SILENCE_TAIL_S = 1.5  # end intent capture after this much continuous silence
INTENT_MAX_S = 6.0    # hard cap on intent capture length


TriggerCallback = Callable[[TriggerEvent], None]


def rms_int16(pcm: bytes) -> int:
    """
    Root mean square of a raw int16 mono PCM window.

    Numerically equivalent to stdlib `audioop.rms(pcm, 2)` for int16 input.
    audioop was removed in Python 3.13 so we compute it directly. Existing
    SINEW_VAD_THRESHOLD values tuned against audioop continue to apply.

    The astype(float32) cast before squaring is required: int16 * int16
    overflows silently and gives wrong RMS values for loud signals.
    """
    if not pcm:
        return 0
    samples = np.frombuffer(pcm, dtype=np.int16)
    if samples.size == 0:
        return 0
    return int(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))


# =============================================================================
# VoiceListener
# =============================================================================


class VoiceListener:
    """
    Wake-phrase gated mic listener with VAD-terminated intent capture.

    Usage:
        listener = VoiceListener(on_trigger=lambda ev: print(ev.transcript))
        listener.start()
        ...
        listener.stop()
    """

    def __init__(
        self,
        wake_phrase: Optional[str] = None,
        model_size: Optional[str] = None,
        device: Optional[str] = None,
        vad_threshold: Optional[int] = None,
        input_device: Optional[Any] = None,
        on_trigger: Optional[TriggerCallback] = None,
    ) -> None:
        self.wake_phrase = (
            wake_phrase or os.environ.get("SINEW_WAKE_PHRASE", "hey sinew")
        ).lower().strip()
        self.model_size = model_size or os.environ.get("SINEW_WHISPER_MODEL", "base")
        self.device = device or os.environ.get("SINEW_WHISPER_DEVICE", "cpu")
        self.vad_threshold = (
            vad_threshold if vad_threshold is not None
            else int(os.environ.get("SINEW_VAD_THRESHOLD", "500"))
        )
        # Audio input selection. Priority: explicit arg > env var > system default (None).
        # sounddevice accepts int or string (substring match on device name).
        resolved_input = input_device
        if resolved_input is None:
            env_val = os.environ.get("SINEW_AUDIO_INPUT")
            if env_val is not None and env_val != "":
                # Prefer int if it parses cleanly, else use the string.
                try:
                    resolved_input = int(env_val)
                except ValueError:
                    resolved_input = env_val
        self.input_device = resolved_input
        self.on_trigger = on_trigger

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._model = None  # faster_whisper.WhisperModel, lazy
        self._sd = None     # sounddevice module handle, lazy

    # ------------------------ lifecycle ------------------------

    def start(self) -> None:
        if self._running:
            return
        self._load_deps()
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="voice-listener"
        )
        self._thread.start()
        logger.info(
            "VoiceListener started wake=%r model=%s device=%s vad=%d input=%r",
            self.wake_phrase, self.model_size, self.device, self.vad_threshold,
            self.input_device,
        )

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        logger.info("VoiceListener stopped")

    def _load_deps(self) -> None:
        try:
            import sounddevice as sd  # type: ignore
        except ImportError as e:
            raise ImportError(
                "sounddevice is required for VoiceListener. Install with "
                "`pip install sounddevice` and ensure PortAudio is available "
                "(Linux: apt install libportaudio2)."
            ) from e
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ImportError as e:
            raise ImportError(
                "faster-whisper is required for VoiceListener. Install with "
                "`pip install faster-whisper`."
            ) from e
        self._sd = sd
        compute_type = "int8" if self.device == "cpu" else "float16"
        logger.info("loading faster-whisper %s on %s compute_type=%s",
                    self.model_size, self.device, compute_type)
        self._model = WhisperModel(
            self.model_size, device=self.device, compute_type=compute_type
        )

    # ------------------------ listen loop ------------------------

    def _run(self) -> None:
        try:
            while self._running:
                if self._wait_for_wake():
                    self._capture_and_fire_intent()
        except Exception:
            logger.exception("VoiceListener crashed")

    def _wait_for_wake(self) -> bool:
        """
        Open an InputStream and slide a three-second window across it. Every
        500 ms, transcribe the window and check for the wake phrase.
        Returns True when detected, False if the listener was stopped.
        """
        assert self._sd is not None
        rolling: Deque[bytes] = deque(maxlen=ROLLING_WINDOW_CHUNKS)

        with self._sd.RawInputStream(
            samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16",
            blocksize=CHUNK_SAMPLES, device=self.input_device,
        ) as stream:
            while self._running:
                chunk, _ = stream.read(CHUNK_SAMPLES)
                rolling.append(bytes(chunk))
                if len(rolling) < 2:
                    continue  # need at least 1 s of audio to bother
                window_bytes = b"".join(rolling)
                text = self._transcribe_pcm16(window_bytes)
                if text:
                    logger.debug("rolling transcript: %s", text)
                    if self.wake_phrase in text.lower():
                        logger.info("wake detected in: %r", text)
                        return True
        return False

    def _capture_and_fire_intent(self) -> None:
        """Record until VAD silence or hard cap, transcribe, fire callback."""
        assert self._sd is not None
        logger.info("capturing intent audio (max %.1fs, silence tail %.1fs)",
                    INTENT_MAX_S, SILENCE_TAIL_S)
        buf = bytearray()
        silence_accum_s = 0.0
        t0 = time.monotonic()

        with self._sd.RawInputStream(
            samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16",
            blocksize=VAD_WINDOW_SAMPLES, device=self.input_device,
        ) as stream:
            while self._running:
                chunk, _ = stream.read(VAD_WINDOW_SAMPLES)
                chunk_bytes = bytes(chunk)
                buf.extend(chunk_bytes)

                rms = rms_int16(chunk_bytes)
                if rms < self.vad_threshold:
                    silence_accum_s += VAD_WINDOW_MS / 1000.0
                else:
                    silence_accum_s = 0.0

                elapsed = time.monotonic() - t0
                if silence_accum_s >= SILENCE_TAIL_S:
                    logger.info("VAD silence tail reached at %.2fs", elapsed)
                    break
                if elapsed >= INTENT_MAX_S:
                    logger.info("INTENT_MAX_S cap reached")
                    break

        transcript = self._transcribe_pcm16(bytes(buf))
        transcript_l = transcript.lower().strip()
        if not transcript_l:
            logger.info("intent transcript empty, ignoring")
            return
        if self.wake_phrase in transcript_l and len(transcript_l) <= len(self.wake_phrase) + 3:
            # Only heard the wake phrase with nothing after. Drop it.
            logger.info("intent was just the wake phrase, ignoring")
            return
        event = TriggerEvent(transcript=transcript.strip(), timestamp=time.time())
        logger.info("intent fired: %r", event.transcript)
        if self.on_trigger is not None:
            try:
                self.on_trigger(event)
            except Exception:
                logger.exception("on_trigger callback raised")

    # ------------------------ transcription ------------------------

    def _transcribe_pcm16(self, pcm: bytes) -> str:
        """Transcribe raw int16 mono PCM bytes at SAMPLE_RATE. Returns joined text."""
        if not pcm or self._model is None:
            return ""
        try:
            import numpy as np
            audio = np.frombuffer(pcm, dtype=np.int16).astype("float32") / 32768.0
            segments, _info = self._model.transcribe(
                audio, language="en", beam_size=1, vad_filter=False,
            )
            return " ".join(seg.text for seg in segments).strip()
        except Exception:
            logger.exception("whisper transcribe failed")
            return ""


# =============================================================================
# ManualTrigger
# =============================================================================


class ManualTrigger:
    """
    Fallback trigger for boxes where VoiceListener does not work. Prompts
    on stdin for an intent string and fires the callback when the user
    hits Enter. Loops until stop() is called.

    This replaces an older spacebar-watching implementation that used the
    `keyboard` library. That library needs root on Linux, which is not
    acceptable for this deployment. A plain blocking input() avoids the
    permissions problem and does not require an extra dep.
    """

    def __init__(self, on_trigger: Optional[TriggerCallback] = None) -> None:
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._on_trigger: Optional[TriggerCallback] = on_trigger

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="manual-trigger"
        )
        self._thread.start()
        logger.info("ManualTrigger started. Type an intent and press Enter.")

    def stop(self) -> None:
        """
        Signal the loop to exit. input() is blocking on a real terminal, so
        we print a hint asking the user to press Enter. The thread observes
        _running is False on its next iteration and returns.
        """
        if not self._running:
            return
        self._running = False
        print("ManualTrigger stopping, press Enter to unblock input()")
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        logger.info("ManualTrigger stopped")

    def _run(self) -> None:
        while self._running:
            try:
                line = input("Intent (or 'quit' to stop): ").strip()
            except EOFError:
                # User closed stdin. Treat as a graceful exit.
                self._running = False
                return
            if not self._running:
                return
            if not line or line.lower() == "quit":
                continue
            event = TriggerEvent(transcript=line, timestamp=time.time())
            if self._on_trigger is not None:
                try:
                    self._on_trigger(event)
                except Exception:
                    logger.exception("on_trigger callback raised")


# =============================================================================
# Smoke test
# =============================================================================


def _list_devices() -> int:
    """Print each audio device sounddevice can see. Used to pin SINEW_AUDIO_INPUT."""
    try:
        import sounddevice as sd  # type: ignore
    except ImportError as e:
        print(f"sounddevice not installed: {e}")
        print("Install with: pip install sounddevice (plus libportaudio2 on Linux)")
        return 1

    try:
        devices = sd.query_devices()
    except Exception as e:
        print(f"sounddevice.query_devices failed: {e}")
        return 1

    try:
        default_in, default_out = sd.default.device
    except Exception:
        default_in, default_out = None, None

    print(f"{'idx':>4}  {'in':>3}  {'out':>3}  name")
    print("-" * 72)
    for i, dev in enumerate(devices):
        marker = " *" if default_in == i else "  "
        in_ch = dev.get("max_input_channels", 0)
        out_ch = dev.get("max_output_channels", 0)
        name = dev.get("name", "?")
        print(f"{i:>4}{marker}  {in_ch:>3}  {out_ch:>3}  {name}")
    print()
    print(f"default input index:  {default_in}")
    print(f"default output index: {default_out}")
    print()
    print("To pin VoiceListener to a specific device, set SINEW_AUDIO_INPUT")
    print("to the index (e.g. 3) or a substring of the name (e.g. 'WO Mic').")
    return 0


def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Sinew voice listener")
    parser.add_argument(
        "--list-devices", action="store_true",
        help="Print all audio devices sounddevice can see, then exit.",
    )
    args = parser.parse_args()

    if args.list_devices:
        return _list_devices()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    def on_trigger(event: TriggerEvent) -> None:
        print(f"\n=== TRIGGERED at {event.timestamp:.2f}: {event.transcript!r} ===\n")

    wake = os.environ.get("SINEW_WAKE_PHRASE", "hey sinew")
    print(f"Say '{wake}' then your intent. Runs for 60 seconds.")

    listener = VoiceListener(on_trigger=on_trigger)
    try:
        listener.start()
    except ImportError as e:
        print(f"FAIL: {e}")
        return 1
    except Exception as e:
        print(f"FAIL starting listener: {type(e).__name__}: {e}")
        return 1

    try:
        time.sleep(60)
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        listener.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
