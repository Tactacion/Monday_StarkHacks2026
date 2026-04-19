"""
tests/test_voice_rms.py

Sanity check for app.voice.rms_int16. Builds three synthetic int16 PCM
windows at 16 kHz mono, runs them through the RMS function, and asserts
silence < moderate < loud. No microphone, no whisper, no sounddevice.

This exists because voice.py replaced stdlib audioop.rms (removed in
Python 3.13) with a numpy-based RMS. The replacement must produce values
in the same order of magnitude so the existing SINEW_VAD_THRESHOLD tuning
still works.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from app.voice import rms_int16, SAMPLE_RATE

WINDOW_S = 0.1  # 100 ms, the VAD window size used in voice.py
N = int(SAMPLE_RATE * WINDOW_S)


def make_sine(amplitude: int, freq_hz: float = 440.0) -> bytes:
    t = np.arange(N, dtype=np.float32) / SAMPLE_RATE
    wave = amplitude * np.sin(2.0 * np.pi * freq_hz * t)
    return wave.astype(np.int16).tobytes()


def test_silence_moderate_loud_ordering() -> None:
    silence = (np.zeros(N, dtype=np.int16)).tobytes()
    moderate = make_sine(1500)   # plausible "quiet speech" amplitude
    loud = make_sine(15000)      # near full scale

    rms_silence = rms_int16(silence)
    rms_moderate = rms_int16(moderate)
    rms_loud = rms_int16(loud)

    print(f"silence={rms_silence}  moderate={rms_moderate}  loud={rms_loud}")

    assert rms_silence == 0, f"silence RMS should be 0, got {rms_silence}"
    assert rms_silence < rms_moderate, "silence must score below moderate"
    assert rms_moderate < rms_loud, "moderate must score below loud"

    # Pure sine at amplitude A has RMS = A / sqrt(2). Verify our function
    # tracks that formula for the two non-silent cases, within rounding.
    expected_moderate = int(1500 / np.sqrt(2))
    expected_loud = int(15000 / np.sqrt(2))
    assert abs(rms_moderate - expected_moderate) <= 2, (
        f"moderate RMS {rms_moderate} diverges from expected {expected_moderate}"
    )
    assert abs(rms_loud - expected_loud) <= 2, (
        f"loud RMS {rms_loud} diverges from expected {expected_loud}"
    )


def test_empty_input() -> None:
    assert rms_int16(b"") == 0


def test_default_vad_threshold_lands_between() -> None:
    """The default SINEW_VAD_THRESHOLD (500) should split silence and moderate speech."""
    from app.voice import rms_int16 as _rms_int16  # noqa: F401

    default_threshold = 500
    silence = np.zeros(N, dtype=np.int16).tobytes()
    moderate = make_sine(1500)
    assert rms_int16(silence) < default_threshold
    assert rms_int16(moderate) > default_threshold


if __name__ == "__main__":
    test_silence_moderate_loud_ordering()
    test_empty_input()
    test_default_vad_threshold_lands_between()
    print("OK")
