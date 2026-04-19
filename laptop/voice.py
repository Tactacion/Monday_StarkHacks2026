# Sinew Voice Trigger — wake word detection and intent capture.
#
# Uses Google's free speech recognition API (requires internet).
# For offline use, swap in whisper or pocketsphinx (out of scope for initial build).
#
# macOS: brew install portaudio  (required before pip install pyaudio)

import logging
import sys
import threading
import time
from typing import Callable, Optional

import speech_recognition as sr

logger = logging.getLogger("voice")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)


class VoiceTrigger:
    def __init__(self, wake_phrase: str = "hey operator", intent_seconds: int = 4):
        self.wake_phrase = wake_phrase.lower().strip()
        self.intent_seconds = intent_seconds
        self.recognizer = sr.Recognizer()
        self._thread = None
        self._running = False
        self._on_intent = None
        self._on_stop = None

    def start(self, on_intent: Callable[[str], None], on_stop: Callable[[], None]):
        """Begin listening in a background thread. Non-blocking."""
        self._on_intent = on_intent
        self._on_stop = on_stop
        self._running = True
        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()
        logger.info(f"Voice trigger started (wake phrase: '{self.wake_phrase}')")

    def stop(self):
        """Stop listening and release the microphone."""
        self._running = False
        logger.info("Voice trigger stopped")

    def _listen_loop(self):
        mic = None
        while self._running:
            # Try to get a microphone
            if mic is None:
                try:
                    mic = sr.Microphone()
                    logger.info("Microphone opened")
                except Exception as e:
                    logger.warning(f"Microphone not available: {e}, retrying in 2s")
                    time.sleep(2)
                    continue

            try:
                with mic as source:
                    # Adjust for ambient noise on first use
                    self.recognizer.adjust_for_ambient_noise(source, duration=0.5)

                    while self._running:
                        try:
                            audio = self.recognizer.listen(source, timeout=2, phrase_time_limit=3)
                        except sr.WaitTimeoutError:
                            continue

                        # Recognize in background to not block
                        try:
                            text = self.recognizer.recognize_google(audio).lower().strip()
                            logger.debug(f"Heard: '{text}'")
                        except sr.UnknownValueError:
                            continue
                        except sr.RequestError as e:
                            logger.warning(f"Speech API error: {e}")
                            continue

                        # Check for stop phrase
                        if "stop stop stop" in text:
                            logger.info("Stop phrase detected!")
                            if self._on_stop:
                                self._on_stop()
                            continue

                        # Check for wake phrase
                        if self.wake_phrase in text:
                            logger.info("Wake phrase detected!")
                            print("\nListening for intent...", file=sys.stderr)

                            # Capture intent
                            try:
                                intent_audio = self.recognizer.listen(
                                    source,
                                    timeout=self.intent_seconds + 1,
                                    phrase_time_limit=self.intent_seconds,
                                )
                                intent_text = self.recognizer.recognize_google(intent_audio).strip()
                                if intent_text:
                                    logger.info(f"Intent captured: '{intent_text}'")
                                    if self._on_intent:
                                        self._on_intent(intent_text)
                            except sr.WaitTimeoutError:
                                logger.info("No intent heard within timeout")
                            except sr.UnknownValueError:
                                logger.info("Could not understand intent")
                            except sr.RequestError as e:
                                logger.warning(f"Speech API error during intent: {e}")

            except Exception as e:
                logger.error(f"Microphone error: {e}, retrying in 2s")
                mic = None
                time.sleep(2)


if __name__ == "__main__":
    print("Say 'hey operator' then an intent. Say 'stop stop stop' to test stop.", file=sys.stderr)
    print("Press Enter to exit.", file=sys.stderr)

    trigger = VoiceTrigger()
    trigger.start(
        on_intent=lambda s: print(f"INTENT: {s}"),
        on_stop=lambda: print("STOP"),
    )

    try:
        input()
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        trigger.stop()
