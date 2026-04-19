# Sinew — AI-guided EMS finger control

An AI sees through a USB webcam, hears user intent via a wake word, and electrically stimulates specific fingers via EMS to perform physical tasks. Primary demo: "grab the object" — all three fingers curl to grasp something in view.

## Hardware BOM

| Item | Description |
|------|-------------|
| TENS/EMS unit | Belifu dual-channel TENS/EMS device, Mode 15 |
| Relay modules | 3× 5V single-channel relay modules (active-LOW, opto-isolated) from AZKO 8-pack |
| Microcontroller | Arduino Uno or Arduino Micro |
| Edge computer | Raspberry Pi 4 or 5 |
| USB webcam | Any UVC-compatible webcam, 640×480 minimum |
| Electrode pads | 3× 1×3cm active pads + 1× 3×3cm ground pad, cut from stock gel pads |
| Kill switch | SPST toggle switch, wired inline with Belifu positive output |
| Jumper wires | Male-to-male and male-to-female for Arduino-to-relay connections |
| USB cables | USB-A to USB-B (Arduino), USB-A to micro/USB-C (webcam) |

## First-time setup

### Arduino

1. Open `firmware/sinew_ems/sinew_ems.ino` in Arduino IDE.
2. Select board (Arduino Uno or Micro) and port.
3. Upload. Open serial monitor at 115200 baud — you should see `READY`.
4. Verify: type `PING`, expect `PONG`. Type `FINGER:INDEX:ON`, hear a relay click, then wait 3 seconds for `WATCHDOG` to auto-release.

### Raspberry Pi

```bash
# Install system OpenCV (avoids long pip compile)
sudo apt update && sudo apt install -y python3-opencv

# Install Python dependencies
cd pi/
pip3 install -r requirements.txt

# Install as systemd services (auto-start on boot)
bash install_systemd.sh
```

### Laptop

```bash
# Create conda environment
conda env create -f laptop/environment.yml
conda activate sinew-laptop

# Copy and fill in your API key
cp laptop/.env_empty laptop/.env
# Edit laptop/.env and set ANTHROPIC_API_KEY

# macOS: pyaudio requires portaudio
brew install portaudio
```

## Running a session

1. **Power on Arduino** — plug USB into Pi. Relays should click once on boot.
2. **Start Pi services** (if not using systemd):
   ```bash
   # On the Pi, in two terminals:
   cd pi/ && python3 receiver.py
   cd pi/ && python3 frame_server.py
   ```
3. **Verify Pi is reachable** from laptop:
   ```bash
   curl http://sinew.local:5001/health
   curl http://sinew.local:5002/health
   ```
4. **Start the laptop app:**
   ```bash
   cd laptop/
   conda activate sinew-laptop
   python3 main.py
   ```
5. **Say "hey operator"**, wait for the beep, then speak your intent (e.g., "grab the ball").

### Hotkeys (in the OpenCV window)

- `SPACE` — manual intent capture (skip wake word)
- `ESC` — emergency stop (all relays off)
- `Q` — quit

## Troubleshooting

### Arduino not detected by Pi
- Check `ls /dev/ttyACM* /dev/ttyUSB*` on the Pi.
- Try unplugging and replugging USB.
- Override with: `SINEW_ARDUINO_PORT=/dev/ttyACM0 python3 receiver.py`

### Pi frame_server returns 503
- The webcam may not be connected or is in use by another process.
- Check: `ls /dev/video*` — if empty, the camera isn't recognized.
- Override camera index: `SINEW_CAMERA_INDEX=1 python3 frame_server.py`

### Claude API key missing
- Ensure `laptop/.env` exists and contains a valid `ANTHROPIC_API_KEY`.
- Test: `python3 -c "from dotenv import load_dotenv; load_dotenv(); import os; print(os.getenv('ANTHROPIC_API_KEY')[:8])"`

### Relays not clicking
- Verify Arduino is powered (onboard LED on).
- Check wiring: Arduino 5V → relay VCC, Arduino GND → relay GND, D4/D5/D6 → relay IN.
- Open serial monitor and send `FINGER:INDEX:ON` manually.

### Voice not recognizing wake phrase
- Ensure microphone is working: `python3 -c "import speech_recognition as sr; print(sr.Microphone.list_microphone_names())"`
- Check internet connection (Google speech recognition requires it).
- Try in a quieter environment or adjust microphone gain.

### Watchdog keeps firing during operation
- The Pi must send commands or PINGs within every 3-second window.
- Check network latency between laptop and Pi.
- Review `pi/logs/serial.log` for timing gaps.
# Monday_StarkHacks2026
