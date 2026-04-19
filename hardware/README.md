# Sinew Hardware Bridge

Flask process that owns the USB serial port to the Arduino firmware. Listens on `127.0.0.1:5001`. Everything above this layer talks to the hardware through HTTP, never by opening the serial port directly.

## Running

```
cd hardware
pip install flask pyserial
python receiver.py
```

Override port detection with an env var if auto-detect picks the wrong device:

```
SINEW_SERIAL_PORT=/dev/ttyACM0 python receiver.py
```

Logs land in `hardware/logs/receiver.log` (rotating, 5 MB per file, 3 backups) and also echo to stderr.

## Manual testing with curl

Health check. Always returns 200 even when the serial port is down.

```
curl -s http://127.0.0.1:5001/health
```

Status snapshot. Shows serial connection state, last ACK seen from firmware, and the watchdog countdown in milliseconds.

```
curl -s http://127.0.0.1:5001/status
```

Fire the index finger relay for 500 ms. The bridge sends `FINGER:INDEX:ON`, sleeps, then sends `FINGER:INDEX:OFF`.

```
curl -s -X POST http://127.0.0.1:5001/stimulate \
  -H 'Content-Type: application/json' \
  -d '{"finger": "INDEX", "action": "ON", "duration_ms": 500}'
```

Latch a relay on without an auto-off. The firmware's 2 second per finger cap will turn it off if nothing else does.

```
curl -s -X POST http://127.0.0.1:5001/stimulate \
  -H 'Content-Type: application/json' \
  -d '{"finger": "MIDDLE", "action": "ON"}'
```

Turn it off explicitly.

```
curl -s -X POST http://127.0.0.1:5001/stimulate \
  -H 'Content-Type: application/json' \
  -d '{"finger": "MIDDLE", "action": "OFF"}'
```

Emergency stop. Sends `ALL:OFF` to the firmware.

```
curl -s -X POST http://127.0.0.1:5001/stop -H 'Content-Type: application/json' -d '{}'
```

Bad input gets 400 with a reason.

```
curl -s -i -X POST http://127.0.0.1:5001/stimulate \
  -H 'Content-Type: application/json' \
  -d '{"finger": "THUMB", "action": "ON"}'
```

A serial drop returns 503. To test it, unplug the Arduino after `python receiver.py` has connected, then hit `/stimulate`. The bridge will try to reconnect once. If the Arduino comes back, the retry succeeds. If not, 503.

## What this layer enforces

The firmware is the authoritative safety layer. This bridge adds:

- `duration_ms` capped at 1000 at the HTTP boundary.
- Strict enum check on `finger` and `action`. Unknown values return 400.
- One lock serializes every write and its ACK read so two requests cannot interleave commands.
- Auto reconnect on serial drop, retried once per request.

The bridge does NOT implement the watchdog itself. It only reports the watchdog countdown based on local send timestamps. The firmware runs its own 3 second watchdog and will force all relays OFF if this bridge ever goes silent.

## Calibration

The calibration GUI at [../calibration/stimGUI.py](../calibration/stimGUI.py) is the tool operators use during electrode placement and intensity tuning. Use it every time electrodes go on a new subject, or when the Belifu unit or electrode pads change.

### Launch

```
cd calibration
pip install PyQt5 requests
python stimGUI.py
```

Override the receiver URL with an env var if the bridge runs somewhere other than localhost:5001:

```
SINEW_RECEIVER_URL=http://localhost:5001 python stimGUI.py
```

The GUI assumes `hardware/receiver.py` is already running. Start the bridge first.

### What you see

- A big red STOP ALL button at the top. Always enabled even if the connection indicator goes red. Escape also triggers it.
- A connection dot, green when `/health` responds, red otherwise. Polls every 2 seconds.
- A duration slider from 50 to 1000 ms, snapping to 50 ms steps.
- Three finger buttons: Pinky, Middle, Index. Hotkeys 1, 2, 3 fire them.
- A Sequence Test button that pulses Pinky, Middle, Index in order with 500 ms gaps.
- A Verify Safety button (see below).
- A rolling log panel with every request, response, and latency.

### What to look for on each finger

Hold the subject's hand steady and watch the fingertip, not the dorsal side. A clean twitch is a single brief flex with no visible activity on the other two fingers. If the pinky fires when you hit the index channel, the electrodes are too close, the ground is placed wrong, or the Belifu intensity is too high. Start at the lowest intensity that produces any visible movement, then raise one click at a time.

Record the verified duration and any crosstalk notes in `hardware/calibration_log.md` for the session. Photograph each electrode placement before you remove anything.

### Verify Safety button

This is the user-facing end-to-end check for the priority-stop path. It runs four steps and logs pass or fail for each:

1. Send `/stimulate` with duration_ms 1000 on the index channel.
2. Wait 200 ms, send `/stop`, and time the response.
3. Assert `/stop` returned in under 100 ms. If not, FAIL is logged in red.
4. Poll `/status` and assert `watchdog_remaining_ms` is at least 2500 ms, confirming no pulse is still in flight from the bridge's perspective.

Run it before every session. If any step fails, do not proceed to subject work until the cause is identified. Common causes: bridge not running (step 1 fails with connection error), bridge running but without the priority-stop path (step 3 fails with latency at or above 1000 ms), pulse tail thread did not abort (step 4 fails because the bridge sent an auto-OFF after `/stop`, resetting the watchdog timestamp).

### Not in scope for this GUI

- Belifu intensity is set by hand on the physical dial. The GUI does not drive it.
- Electrode impedance is not measured. Watch for visible twitch and ask the subject.
- The GUI does not capture photos. Operators take photos on their own device and save filenames into `calibration_log.md`.
