# ProComm HAT — Setup & Deployment Guide

## Hardware Requirements

| Component | Specification |
|---|---|
| **Raspberry Pi** | **Pi 5** |
| **HAT** | RaspiAudio 8xIN + 8xOUT (HiFiBerry DAC8x compatible) |
| **Jacks** | 🟢 Green = INPUTS (ADC/capture) — 🔴 Red = OUTPUTS (DAC/playback) |
| **Channels** | Channel 1 = Jack 1, Channel 2 = Jack 2, ... Channel 8 = Jack 8 |

---

## SD Card Configuration (`/boot/firmware/config.txt`)

The only HAT-specific line needed:

```ini
dtoverlay=hifiberry-dac8x
```

This overlay (shipped with Raspberry Pi OS) does three things at boot:
1. Loads the `snd-soc-rpi-simple-soundcard` driver with DAC8x config
2. Configures GPIO18-27 as I2S (TDM) pins via RP1 pinctrl
3. Registers ALSA sound card 1 (`sndrpihifiberry`) with 8ch S32_LE @ 48kHz

**Full working `config.txt` active lines:**
```ini
dtparam=audio=on
dtoverlay=hifiberry-dac8x
camera_auto_detect=1
display_auto_detect=1
auto_initramfs=1
dtoverlay=vc4-kms-v3d
max_framebuffers=2
disable_fw_kms_setup=1
arm_64bit=1
disable_overscan=1
arm_boost=1
[cm5]
dtoverlay=dwc2,dr_mode=host
[all]
enable_uart=1
```

---

## ALSA Hardware Details

| Parameter | Value |
|---|---|
| ALSA Card | `card 0` (auto-detected by name via `_find_hat_card()`) |
| ALSA Device | `plughw:<card>,0` (use `plughw` NOT `hw` — needed for 8ch TDM) |
| Format | S32_LE (32-bit little-endian) |
| Sample Rate | 48000 Hz |
| Channels | 8 |
| Period Size | 960 frames |
| Period Bytes | 30720 bytes (960 × 8ch × 4 bytes) |
| BCLK | 12.288 MHz (256 × 48kHz) |

**Why `plughw` and not `hw`?**  
`hw:<card>,0` reports `CHANNELS: 2` at the hardware level because the ALSA plugin layer
hasn't applied the TDM mapping yet. `plughw:<card>,0` goes through the ALSA plugin layer
which enables full 8-channel TDM access.

---

## Software Architecture

### Key File: `smart_sip/audio_i2s_hat.py`

Replaces `audio_usb_dongles.py` — same public API, HAT-specific implementation.

**Audio flow (inbound — what you hear):**
```
SIP call → RTP packets → rtp.py → engine.py → audio_i2s_hat.send_audio()
→ channel output queue → _output_worker()
→ ratecv 8kHz→48kHz → pack 8ch S32_LE → ALSA plughw:0,0 → Red jack N
```

**Audio flow (outbound — what you say):**
```
Green jack N → ALSA plughw:0,0 → _input_worker()
→ S32_LE >>16 → int16 → ratecv 48kHz→8kHz → MIC_GAIN ×3.0
→ input callback → rtp.py → SIP call
```

**Channel mapping:**
- Channel N (1-indexed, UI) = TDM slot N-1 (0-indexed array column)
- Jack 1 Left = Channel 1 = slot 0
- Jack 2 = Channel 2 = slot 1 ... etc.

### Key File: `smart_sip/engine.py`

Import line (line 20):
```python
from .audio_i2s_hat import HATAudioManager as USBDongleAudioManager
```

---

## Operator Headset (USB)

The operator headset is a **USB audio device** that is completely separate from
the HAT. It plugs into any USB port and lets the operator listen to and talk on
**one** SIP line at a time, independent of the 8 HAT channels.

**Auto-detection.** The headset card is found at startup by `_find_headset_card()`
in `audio_i2s_hat.py`. It returns the first ALSA card that is **not** the HAT
(`hifiberry`/`dac8x`) and **not** HDMI (`hdmi`/`vc4`) — i.e. the only remaining USB
sound card. Set `"headset_card": -1` in `smart_sip_config.json` to enable
auto-detect (recommended). Returns `-1` / disabled if no USB headset is present.

```jsonc
// smart_sip_config.json
"headset_card": -1   // -1 = auto-detect the USB headset on any port
```

**Two hard rules keep the headset and HAT from interfering:**

1. **The headset never opens the HAT card.** `start_headset()` refuses any
   `card_number == HAT_CARD` (and `_find_headset_card()` skips the HAT). Opening
   the HAT card at 2-channel while it runs 8-channel TDM corrupts the codec clock
   and needs a **cold power-cycle** to recover (see Troubleshooting). This guard
   is belt-and-suspenders so it can never happen again.

2. **One line = one microphone (talk priority).** When the headset is monitoring
   a line, the HAT input worker **skips that one line's channel mic** so the
   headset mic is the sole talker — two mics on one outgoing SIP stream garble the
   far end. Only the monitored line's channel steps aside; the other 7 channels
   keep running. This matches the original USB-dongle behavior in `ProComm_1`:
   ```python
   for lid in lines_on_ch:
       if self._headset_listen_line == lid:
           continue          # headset mic has priority on this line
       cb = self._input_callbacks.get(lid)
       ...
   ```
   The instant the headset is turned off that line, its HAT channel mic resumes.

**Listen** (far end → headset speaker) is a parallel mirror in `send_audio()` and
never affects the HAT. **Talk** (headset mic → SIP line) runs on the separate USB
card in `_headset_input_worker()`.

---

## Python Dependencies

A fresh Raspberry Pi OS install has **no pip** and is missing the ALSA build
headers. Install everything in this order:

```bash
# 1. System packages (apt)
sudo apt update
sudo apt install -y python3-pip libasound2-dev

# 2. App Python packages
cd /home/procomm/ProComm_HAT
pip3 install -r requirements.txt --break-system-packages

# 3. Python 3.13 removed the built-in audioop module — install the shim
pip3 install audioop-lts --break-system-packages
```

`requirements.txt` pulls in: `flask`, `flask-cors`, `flask-socketio`,
`python-socketio`, `python-engineio`, `pyalsaaudio`, `numpy`, `soxr`, `g722`,
`qrcode`, `pillow`, `werkzeug`, `simple-websocket`.

> ⚠️ `pyalsaaudio` builds from source and **requires `libasound2-dev`** first,
> otherwise the wheel build fails with a missing `alsa/asoundlib.h` error.

**Why `audioop-lts`?**  
Python 3.13 removed the built-in `audioop` module. `audioop-lts` is a drop-in replacement.
The import in `rtp.py` / `audio_i2s_hat.py` handles this automatically:
```python
try:
    import audioop
except ImportError:
    import audioop_lts as audioop
```

---

## Pi 5 First-Time Setup (fresh OS / new unit)

These are the **one-time** steps to turn a stock Raspberry Pi OS image on a Pi 5
into a working ProComm unit. (When you clone the golden master SD card, these are
already done.)

### 1. Sync the app from a dev machine
```bash
rsync -av --exclude='.git' --exclude='__pycache__' \
  ./ProComm_HAT/ procomm@<pi-ip>:/home/procomm/ProComm_HAT/
```

### 2. Install Python deps
See **Python Dependencies** above.

### 3. Install the touchscreen GUI stack
```bash
sudo apt install -y xinit openbox unclutter x11-xserver-utils \
    python3-pyqt5 python3-pyqt5.qtwebengine
```

### 4. Enable console autologin on tty1
```bash
sudo mkdir -p /etc/systemd/system/getty@tty1.service.d
sudo cp /home/procomm/ProComm_HAT/config/getty-autologin.conf \
        /etc/systemd/system/getty@tty1.service.d/autologin.conf
sudo systemctl daemon-reload
```

### 5. Install the boot scripts
```bash
cp /home/procomm/ProComm_HAT/config/.bash_profile /home/procomm/.bash_profile
cp /home/procomm/ProComm_HAT/config/.xinitrc       /home/procomm/.xinitrc
```
- `.bash_profile` → runs `startx` automatically on tty1 login
- `.xinitrc` → launches `main.py` (which starts `app.py`) under openbox

### 6. Fix the Pi 5 display driver (CRITICAL — see below)
```bash
sudo tee /usr/share/X11/xorg.conf.d/99-vc4.conf > /dev/null << 'EOF'
Section "OutputClass"
    Identifier "vc4"
    MatchDriver "vc4"
    Driver "modesetting"
    Option "PrimaryGPU" "true"
EndSection
EOF
```

### 7. Reboot
```bash
sudo reboot
```
After reboot the touchscreen shows the ProComm UI and `app.py` is running.

---

## Troubleshooting

### No audio on any red jack after boot
```bash
# Check GPIO18-27 are claimed as I2S:
cat /sys/kernel/debug/pinctrl/*/pinmux-pins | grep "pin 18"
# Expected: pin 18 (gpio18): ... function i2s0 ...
```

### App won't start — "Another instance is already running"
The GUI (`main.py`) runs a watchdog that auto-restarts `app.py`, so killing
`app.py` alone isn't enough — the watchdog respawns it. To run `app.py` manually:
```bash
# Stop the GUI + its watchdog first:
pkill -f "main.py"
pkill -f "app.py"
# Then start ProComm_HAT manually:
cd /home/procomm/ProComm_HAT && python3 app.py
```
The lock file is `/tmp/procomm_app.pid` — a stale one is removed automatically on
next start if the PID is no longer running.

### HAT card not found (ALSA card missing)
```bash
# Check if overlay loaded:
dmesg | grep -i "hifiberry\|dac8x\|snd-rpi"
# Check config.txt has: dtoverlay=hifiberry-dac8x
```

### HAT stuck: "Failed to open HAT streams: Invalid argument [plughw:0,0]"
**Symptom:** The HAT enumerates fine at boot (`dmesg` shows `ADC8x detected`) and
nothing is holding the card, yet **every** open fails with `Invalid argument` —
even standalone ALSA tools at any channel count:
```bash
arecord -D hw:0,0 -c 2 -f S16_LE -r 48000 -d 1 /tmp/x.wav
# arecord: audio open error: Invalid argument
```
The app's HAT workers go `input=DEAD, output=DEAD` and the watchdog retries every
30 s without success.

**Cause:** The DAC8x/ADC8x codec clock is in a corrupted state — usually from
something opening the HAT card with a conflicting config (e.g. the headset feature
opening `card 0` at 2-channel while the HAT runs 8-channel TDM). The codec chips
stay powered across a warm reboot, so **`sudo reboot` does NOT fix it.**

**Fix — a full cold power-cycle:**
1. **Unplug the Pi's USB-C power** (full power off — not `reboot`)
2. **Wait ~10 seconds** (lets the HAT regulators fully discharge)
3. **Plug power back in**

After boot, confirm both cards came up:
```bash
grep -E "HAT ALSA streams opened|Headset ALSA streams started" \
    /home/procomm/ProComm_HAT/app.log | tail -2
```
This is now prevented in code: `start_headset()` refuses to open the HAT card
(see **Operator Headset** above), so the only fix you should ever need is the
one-time cold boot if a unit is already stuck.

### Headset: talk to the far end is intermittent (must mute/unmute 2–3×)
**Symptom:** Tapping the headset icon on a line, the operator's talk only reaches
the remote sometimes; toggling mute a few times eventually makes it work.

**Cause:** Two microphones feeding one line's outgoing SIP stream — the HAT channel
mic **and** the headset mic — at 2× packet rate, which garbles/drops the far end.
This happens if the headset-on-a-line does **not** suppress that line's HAT channel
mic.

**Fix (already in code):** The HAT input worker skips the monitored line's channel
mic so the headset is the sole talker (one line = one mic). Verify the `continue`
guard is present:
```bash
grep -n "headset mic has priority" \
    /home/procomm/ProComm_HAT/smart_sip/audio_i2s_hat.py
```
If talk is still doubled, confirm the monitored line isn't being fed by two paths:
```bash
grep -E "now monitoring Line|send_audio: Line .* -> Ch" \
    /home/procomm/ProComm_HAT/app.log | tail -10
```

### Headset on the wrong card / no headset audio
The headset auto-detects on the only non-HAT, non-HDMI USB sound card. Check which
card it picked and that it's not the HAT (`card 0`):
```bash
cat /proc/asound/cards
grep -E "auto-detected USB headset|Headset ALSA streams started" \
    /home/procomm/ProComm_HAT/app.log | tail -3
```
With `"headset_card": -1` (auto-detect) the app skips the HAT and HDMI cards
automatically. If no USB headset is plugged in, the feature stays disabled.

### Black screen on the touchscreen (Pi 5) — X server won't start
**Symptom:** Pi boots, SSH works, `app.py` runs, but the HDMI/touchscreen is black.

**Cause 1 — wrong X driver.** Pi 5 has two GPU nodes (render + display). X grabs
the legacy `fbdev` driver and dies with:
```
(EE) Cannot run in framebuffer mode. Please specify busIDs for all framebuffer devices
```
Check with: `cat ~/.local/share/xorg/Xorg.0.log | grep EE`

**Fix:** Force the `modesetting` driver (see Pi 5 First-Time Setup step 6):
```bash
sudo tee /usr/share/X11/xorg.conf.d/99-vc4.conf > /dev/null << 'EOF'
Section "OutputClass"
    Identifier "vc4"
    MatchDriver "vc4"
    Driver "modesetting"
    Option "PrimaryGPU" "true"
EndSection
EOF
sudo systemctl reset-failed getty@tty1.service
sudo systemctl restart getty@tty1.service
```

**Cause 2 — `.xinitrc` hangs on `sudo`.** If `.xinitrc` calls `sudo` (e.g. the old
`sudo udevadm trigger` USB-audio lines) and the `procomm` user does **not** have
passwordless sudo, X starts but `.xinitrc` blocks forever waiting for a password →
GUI never launches, getty hits its 5× restart limit and gives up.

**Fix:** The Pi 5 HAT uses I2S, not USB dongles, so those lines are removed from
`config/.xinitrc`. Verify your `.xinitrc` has **no `sudo` calls**:
```bash
grep sudo ~/.xinitrc   # should print nothing
```

### getty@tty1 "start-limit-hit" / failed
X failed to start 5× in a row. Fix the underlying cause (the two above), then:
```bash
sudo systemctl reset-failed getty@tty1.service
sudo systemctl restart getty@tty1.service
```

### Audio routing not working (call connected but no sound)
Check the log for `send_audio` and `ch1_rms`:
```bash
grep -E "send_audio|ch1_rms|Routing" /home/procomm/ProComm_HAT/app.log | tail -20
```
- `assigned=[]` → Line has no channel assigned in UI — click a channel button
- `ch1_rms=0` with `assigned=[1]` → RTP not flowing or wrong codec

---

## Web UI Access & QR Code

The app serves **two** web endpoints:

| Protocol | Port | When available |
|---|---|---|
| **HTTP** | `5000` | **Always** — no certs needed |
| **HTTPS** | `5443` | Only if `certs/cert.pem` + `certs/key.pem` exist |

For this deployment (outbound-only, HAT audio from the physical jacks — the browser
never needs mic access) **HTTP port 5000 is all you need**:

### URL
```
http://procomm.local:5000          # via mDNS hostname
http://<pi-ip>:5000                # via IP, e.g. http://192.168.108.65:5000
```

- Works via mDNS (`.local`) — no IP address needed
- **Mac, iPhone, Android** — works natively
- **Windows** — requires Bonjour (installed automatically with iTunes/iCloud)

> 💡 HTTPS (5443) is only required if you want **microphone access from a browser**
> (iPhone/Android). Self-signed certs also trigger a browser security warning. The
> physical HAT jacks don't need it, so plain HTTP is simpler and warning-free.

### QR Code — auto-generated by the app
The app renders its own QR codes (no external tool needed):
- `http://<pi-ip>:5000/qr`     → QR for the `/phone` page
- `http://<pi-ip>:5000/qr_ui`  → QR for the main UI

These **auto-detect the unit's hostname** via `socket.gethostname()`, so each unit's
QR code automatically points to its own `.local` address with no manual editing.

### Multiple units on the same network
Give each unit a unique hostname (e.g. `procomm-01`, `procomm-02`):
```bash
sudo hostnamectl set-hostname procomm-01
sudo sed -i 's/\bprocomm\b/procomm-01/g' /etc/hosts
sudo reboot
```
Then each unit's URL (and its auto-generated QR code) becomes:
```
http://procomm-01.local:5000
http://procomm-02.local:5000
```

---

## Running as a Service (optional)

```bash
sudo cp /home/procomm/ProComm_HAT/procomm-app.service /etc/systemd/system/
sudo systemctl enable procomm-app
sudo systemctl start procomm-app
sudo journalctl -u procomm-app -f   # follow logs
```

---

*Last updated: June 9, 2026 — Pi 5 deployment verified: all 8 capture + 8 playback channels working, touchscreen GUI auto-starting, web UI on port 5000.*
