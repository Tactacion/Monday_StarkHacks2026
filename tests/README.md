# Tests

## Mock receiver

Simulates Person A's hardware bridge on port 5001. Use this to develop and test
all Person B code without the Pi or Arduino.

    python tests/mock_receiver.py

The mock logs every request to stdout and returns `{"status": "ok"}` for all write
endpoints. It never touches serial or hardware.

## Brain evaluation harness

Runs 8 test cases against the real Claude API to validate the system prompt.

    python tests/test_brain.py

Set `ANTHROPIC_API_KEY` in your environment or in a `.env` file before running.
Results print to stdout. Iterate on `prompts/system_prompt.txt` until all 8 pass
consistently across 3 runs.

## Fixtures

`tests/fixtures/` contains six JPEG images used by the evaluation harness.

Current placeholders are solid-color images created with PIL.
See `tests/fixtures/TODO.md` for what real photos to take and how.

Replace the placeholders before running `test_brain.py` for meaningful results.
The prompt iteration loop is pointless against solid-color images.
