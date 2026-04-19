# Sinew Calibration Log

One entry per session. Append new sessions at the bottom. Fill in during calibration, not after. Photos live alongside this file in `calibration_photos/`, named `YYYY-MM-DD_<finger>.jpg` or similar.

Template block below. Copy it and paste at the end of the file for each session.

---

## Session template

### Session metadata

- Date:
- Operator name:
- Subject name or identifier:
- Location:
- Ambient temperature:

### Belifu EMS unit

- Model:
- Mode used (e.g. TENS, EMS, massage program):
- Pulse width setting if adjustable:
- Frequency setting if adjustable:

### Starting intensity per channel

Baseline dial position when the session started. Record even if you planned to change it.

| Channel | Dial at start | Dial locked final | Notes |
|---------|---------------|-------------------|-------|
| Index   |               |                   |       |
| Middle  |               |                   |       |
| Pinky   |               |                   |       |

### Electrode positions

Describe placement in words and point to the photo filename. Photos go in `calibration_photos/`.

- Index: position description. Photo: `calibration_photos/`
- Middle: position description. Photo: `calibration_photos/`
- Pinky: position description. Photo: `calibration_photos/`
- Shared ground: position description. Photo: `calibration_photos/`

### Per-finger verification

For each finger, record the duration that produced a clean twitch, plus any notes on isolation and crosstalk.

| Finger | Duration verified (ms) | Twitch quality | Crosstalk to other fingers | Notes |
|--------|------------------------|----------------|----------------------------|-------|
| Index  |                        |                |                            |       |
| Middle |                        |                |                            |       |
| Pinky  |                        |                |                            |       |

Twitch quality shorthand: clean, weak, painful, delayed, irregular.

### Sequence test

Ran the Sequence Test button in stimGUI. Record any finger that felt off.

- Result:
- Notes:

### Verify Safety run

Ran the Verify Safety button. Pass or fail, and the `/stop` latency printed in the log.

- Result:
- /stop latency (ms):
- Notes:

### Anomalies

Anything the subject reported, anything the operator noticed, any hardware oddity. Skin redness, equipment warmth, unexpected sensation, relay behavior.

-
-

### Session end

- End time:
- Total on-time per finger if tracked:
- Kill switch tested at end of session: yes or no
- Equipment condition on unplug:

---

## Sessions

<!-- Paste filled-in session blocks below this line, newest at the bottom. -->
