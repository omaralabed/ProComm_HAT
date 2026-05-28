#!/bin/bash

set -e  # Exit on error

# ── Colors ──────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_step()  { echo ""; echo -e "${BLUE}═══════════════════════════════════════${NC}"; echo -e "${BLUE}  $1${NC}"; echo -e "${BLUE}═══════════════════════════════════════${NC}"; }

# ── Pre-flight checks ──────────────────────────────────────────────────
if [ "$EUID" -eq 0 ]; then
    log_error "Do not run as root. Run as the 'procomm' user."
    exit 1
fi

INSTALL_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
if [ ! -f "$INSTALL_DIR/app.py" ]; then
    log_error "app.py not found in $INSTALL_DIR — run this script from the ProComm_1 folder."
    exit 1
fi

TOTAL_STEPS=9
log_step "ProComm Deployment Starting..."
log_info "Install dir : $INSTALL_DIR"
log_info "User        : $(whoami)"
log_info "OS          : $(lsb_release -ds 2>/dev/null || head -1 /etc/os-release 2>/dev/null)"

# ── Parse flags ─────────────────────────────────────────────────────────
INSTALL_GUI=true

for arg in "$@"; do
    case "$arg" in
        --no-gui) INSTALL_GUI=false ;;
    esac
done

log_info "Audio mode  : usb_dongles"
log_info "Kiosk GUI   : $INSTALL_GUI"

# ════════════════════════════════════════════════════════════════════════
# Step 1: System packages
# ════════════════════════════════════════════════════════════════════════
log_step "Step 1/$TOTAL_STEPS — Installing system packages"

sudo apt update -qq

# Remove PipeWire if present (conflicts with direct ALSA access for USB dongles)
if dpkg -l 2>/dev/null | grep -q pipewire; then
    log_info "Removing PipeWire (conflicts with direct ALSA)..."
    sudo apt remove -y pipewire pipewire-alsa pipewire-jack pipewire-pulse wireplumber 2>/dev/null || true
fi

sudo apt install -y \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    libasound2-dev \
    alsa-utils \
    git \
    usbutils \
    curl

log_info "System packages installed ✓"

# ════════════════════════════════════════════════════════════════════════
# Step 2: Python virtual environment + dependencies
# ════════════════════════════════════════════════════════════════════════
log_step "Step 2/$TOTAL_STEPS — Python environment & dependencies"

cd "$INSTALL_DIR"

if [ -d "venv_with_system" ]; then
    log_info "Virtual environment already exists — upgrading packages..."
else
    log_info "Creating virtual environment..."
    python3 -m venv --system-site-packages venv_with_system
fi

source venv_with_system/bin/activate
pip install --upgrade pip -q

log_info "Installing Python dependencies..."
pip install -r requirements.txt -q

# Verify critical imports
python -c "import alsaaudio; print('  ✓ alsaaudio (direct ALSA)')"
python -c "import flask; print('  ✓ Flask')"
python -c "import flask_socketio; print('  ✓ Flask-SocketIO')"
python -c "import soxr; print('  ✓ soxr (high-quality resampling)')"
python -c "import numpy; print('  ✓ numpy')"

log_info "Python environment ready ✓"

# ════════════════════════════════════════════════════════════════════════
# Step 3: Configuration file
# ════════════════════════════════════════════════════════════════════════
log_step "Step 3/$TOTAL_STEPS — SIP configuration"

if [ -f "$INSTALL_DIR/smart_sip_config.json" ]; then
    log_info "smart_sip_config.json already exists — keeping current config"
elif [ -f "$INSTALL_DIR/smart_sip_config.example.json" ]; then
    cp "$INSTALL_DIR/smart_sip_config.example.json" "$INSTALL_DIR/smart_sip_config.json"
    log_warn "Created smart_sip_config.json from template"
    log_warn ">>> You MUST edit it with your SIP credentials before calling! <<<"
else
    log_error "No config template found! Create smart_sip_config.json manually."
fi

# ════════════════════════════════════════════════════════════════════════
# Step 4: USB audio dongle detection
# ════════════════════════════════════════════════════════════════════════
log_step "Step 4/$TOTAL_STEPS — Audio hardware setup (USB dongles)"

sleep 1  # Let ALSA settle

log_info "Detecting USB audio dongles..."

USB_CARDS=$(aplay -l 2>/dev/null | grep -i "usb\|C-Media\|AB13X" | grep -oP 'card \K[0-9]+' || true)

if [ -n "$USB_CARDS" ]; then
    CARD_COUNT=$(echo "$USB_CARDS" | wc -l)
    log_info "Found $CARD_COUNT USB audio dongle(s):"
    while IFS= read -r card; do
        NAME=$(aplay -l | grep "card $card:" | sed 's/.*: //' | cut -d',' -f1)
        log_info "  Card $card — $NAME"
    done <<< "$USB_CARDS"

    CHANNEL=1
    MAPPING="{"
    while IFS= read -r card; do
        [ $CHANNEL -gt 1 ] && MAPPING+=", "
        MAPPING+="\"$CHANNEL\": $card"
        CHANNEL=$((CHANNEL + 1))
    done <<< "$USB_CARDS"
    MAPPING+="}"

    log_info "Channel→Card mapping: $MAPPING"

    if [ -f "$INSTALL_DIR/smart_sip_config.json" ]; then
        python3 << PYEOF
import json
with open('$INSTALL_DIR/smart_sip_config.json', 'r') as f:
    config = json.load(f)
config['audio_mode'] = 'usb_dongles'
config['line_audio_cards'] = $MAPPING
with open('$INSTALL_DIR/smart_sip_config.json', 'w') as f:
    json.dump(config, f, indent=2)
print("  Config updated: audio_mode=usb_dongles, cards=$MAPPING")
PYEOF
    fi
else
    log_warn "No USB audio dongles detected"
    log_warn "Plug in dongles and re-run, or edit smart_sip_config.json manually"
    aplay -l 2>/dev/null || true
fi

# ════════════════════════════════════════════════════════════════════════
# Step 5: Install systemd service (dynamically generated for this user/path)
# ════════════════════════════════════════════════════════════════════════
log_step "Step 5/$TOTAL_STEPS — Installing systemd service"

# Stop existing service if running
sudo systemctl stop procomm-app 2>/dev/null || true

CURRENT_USER=$(whoami)

# Generate service file dynamically so it works for any user/path
cat << SVCEOF | sudo tee /etc/systemd/system/procomm-app.service > /dev/null
[Unit]
Description=ProComm SIP Phone System
After=network.target
Wants=network.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv_with_system/bin/python $INSTALL_DIR/app.py
Restart=on-failure
RestartSec=10
StandardOutput=append:$INSTALL_DIR/app.log
StandardError=append:$INSTALL_DIR/app.log
Environment="PATH=$INSTALL_DIR/venv_with_system/bin:/usr/local/bin:/usr/bin:/bin"

[Install]
WantedBy=multi-user.target
SVCEOF

sudo systemctl daemon-reload
sudo systemctl enable procomm-app

log_info "procomm-app.service installed and enabled ✓"

# ════════════════════════════════════════════════════════════════════════
# Step 6: Disable conflicting autostart (if any)
# ════════════════════════════════════════════════════════════════════════
log_step "Step 6/$TOTAL_STEPS — Cleaning up conflicts"

# Disable desktop autostart for main.py (conflicts with systemd service on port 5000)
AUTOSTART_FILE="$HOME/.config/autostart/procomm-gui.desktop"
if [ -f "$AUTOSTART_FILE" ]; then
    mv "$AUTOSTART_FILE" "${AUTOSTART_FILE}.disabled"
    log_info "Disabled desktop autostart for main.py (would conflict with systemd service)"
fi

# Kill any stale process holding port 5000
if lsof -i :5000 2>/dev/null | grep -q python; then
    log_info "Killing stale process on port 5000..."
    sudo fuser -k 5000/tcp 2>/dev/null || true
    sleep 1
fi

log_info "Conflicts cleaned ✓"

# ════════════════════════════════════════════════════════════════════════
# Step 7: Log file & permissions
# ════════════════════════════════════════════════════════════════════════
log_step "Step 7/$TOTAL_STEPS — Log file & permissions"

touch "$INSTALL_DIR/app.log"
chmod 664 "$INSTALL_DIR/app.log"

# Ensure current user owns everything (CURRENT_USER set in Step 5)
sudo chown -R "$CURRENT_USER:$CURRENT_USER" "$INSTALL_DIR"

# Add user to audio group (needed for ALSA access)
sudo usermod -aG audio "$CURRENT_USER" 2>/dev/null || true

# Passwordless systemctl for procomm-app service
SUDOERS_FILE="/etc/sudoers.d/procomm"
if [ ! -f "$SUDOERS_FILE" ] || ! grep -qF "procomm-app" "$SUDOERS_FILE" 2>/dev/null; then
    echo "$CURRENT_USER ALL=(ALL) NOPASSWD: /bin/systemctl restart procomm-app, /bin/systemctl stop procomm-app, /bin/systemctl start procomm-app, /bin/systemctl status procomm-app" | sudo tee "$SUDOERS_FILE" > /dev/null
    sudo chmod 440 "$SUDOERS_FILE"
    log_info "Sudoers rule added — passwordless service control"
fi

log_info "Permissions set ✓"

# ════════════════════════════════════════════════════════════════════════
# Step 8: Touchscreen Kiosk GUI (PyQt5 WebEngine)
# ════════════════════════════════════════════════════════════════════════
if [ "$INSTALL_GUI" == "true" ]; then
log_step "Step 8/$TOTAL_STEPS — Touchscreen Kiosk GUI"

log_info "Installing X server, PyQt5 WebEngine, and Openbox..."
sudo apt install -y \
    xserver-xorg \
    xinit \
    x11-xserver-utils \
    openbox \
    python3-pyqt5 \
    python3-pyqt5.qtwebengine \
    unclutter

log_info "X server + PyQt5 WebEngine installed ✓"

# ── Create .xinitrc (kiosk startup) ────────────────────────────────────
cat > "$HOME/.xinitrc" << 'XINITEOF'
#!/bin/bash
sleep 2

# DISPLAY is already set by xinit/startx — do NOT hardcode
# Export it for child processes (main.py)
export DISPLAY

# Force 1080p resolution on HDMI
xrandr --output HDMI-A-1 --mode 1920x1080 2>/dev/null || \
xrandr --output HDMI-1 --mode 1920x1080 2>/dev/null || true

# Disable screen blanking / power saving
xset s off
xset -dpms
xset s noblank

# Hide mouse cursor after 3 seconds of inactivity
unclutter -idle 3 -root &

# ProComm backend is managed by systemd (procomm-app.service)
# Wait for backend to be ready
echo "Waiting for ProComm backend on port 5000..."
for i in $(seq 1 30); do
    if curl -s http://localhost:5000/ > /dev/null 2>&1; then
        echo "Backend ready!"
        break
    fi
    sleep 1
done

# Start ProComm GUI (PyQt5 WebEngine - production-grade secure kiosk)
cd /home/procomm/ProComm_1
python3 main.py > /tmp/procomm_gui.log 2>&1 &

# Start window manager (keeps X alive)
exec /usr/bin/openbox-session
XINITEOF
chmod +x "$HOME/.xinitrc"
log_info ".xinitrc created — PyQt5 kiosk ✓"

# ── Auto-login on tty1 ─────────────────────────────────────────────────
log_info "Configuring auto-login on tty1..."
sudo mkdir -p /etc/systemd/system/getty@tty1.service.d
cat << LOGINEOF | sudo tee /etc/systemd/system/getty@tty1.service.d/autologin.conf > /dev/null
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin $CURRENT_USER --noclear %I \$TERM
LOGINEOF
log_info "Auto-login configured for '$CURRENT_USER' on tty1 ✓"

# ── Auto-start X on login (via .bash_profile) ──────────────────────────
# Only start X if on tty1 and no display is active
BASH_PROFILE="$HOME/.bash_profile"
STARTX_BLOCK='# ProComm kiosk: auto-start X on tty1
if [ -z "$DISPLAY" ] && [ "$(tty)" = "/dev/tty1" ]; then
    exec startx -- -nocursor 2>/dev/null
fi'

if [ -f "$BASH_PROFILE" ] && grep -q "ProComm kiosk" "$BASH_PROFILE" 2>/dev/null; then
    log_info "startx auto-launch already in .bash_profile ✓"
else
    echo "" >> "$BASH_PROFILE"
    echo "$STARTX_BLOCK" >> "$BASH_PROFILE"
    log_info "Added startx auto-launch to .bash_profile ✓"
fi

# ── HDMI + Xorg config for KMS on Pi 5 ─────────────────────────────────
BOOT_CONFIG="/boot/firmware/config.txt"
if [ ! -f "$BOOT_CONFIG" ]; then
    BOOT_CONFIG="/boot/config.txt"
fi

if [ -f "$BOOT_CONFIG" ]; then
    # Disable disable_fw_kms_setup (let firmware pass display info to KMS)
    if grep -q "^disable_fw_kms_setup=1" "$BOOT_CONFIG"; then
        sudo sed -i 's/^disable_fw_kms_setup=1/#disable_fw_kms_setup=1/' "$BOOT_CONFIG"
        log_info "Disabled disable_fw_kms_setup (required for KMS display)"
        NEEDS_REBOOT=true
    fi

    # Comment out legacy HDMI settings (incompatible with KMS on Pi 5)
    for setting in hdmi_force_hotplug hdmi_group hdmi_mode; do
        if grep -q "^${setting}=" "$BOOT_CONFIG"; then
            sudo sed -i "s/^${setting}=/#${setting}=/" "$BOOT_CONFIG"
            log_info "Commented out legacy $setting (not needed with KMS)"
        fi
    done
fi

# Add KMS video= parameter to kernel cmdline to force HDMI 1080p
CMDLINE_FILE="/boot/firmware/cmdline.txt"
if [ -f "$CMDLINE_FILE" ]; then
    if ! grep -q "video=HDMI-A-1" "$CMDLINE_FILE"; then
        sudo sed -i 's|rootwait|rootwait video=HDMI-A-1:1920x1080@60D|' "$CMDLINE_FILE"
        log_info "Added video=HDMI-A-1:1920x1080@60D to kernel cmdline ✓"
        NEEDS_REBOOT=true
    else
        log_info "HDMI video= already in kernel cmdline ✓"
    fi
fi

# Xorg config: use modesetting driver on card1 (vc4-drm GPU)
sudo mkdir -p /etc/X11/xorg.conf.d
cat << XORGEOF | sudo tee /etc/X11/xorg.conf.d/10-modesetting.conf > /dev/null
Section "Device"
    Identifier "vc4"
    Driver "modesetting"
    Option "kmsdev" "/dev/dri/card1"
EndSection
XORGEOF
log_info "Xorg config for KMS modesetting on card1 ✓"

# Allow non-root Xorg with KMS
cat << WRAPEOF | sudo tee /etc/X11/Xwrapper.config > /dev/null
allowed_users=anybody
needs_root_rights=yes
WRAPEOF
log_info "Xwrapper.config: non-root X allowed ✓"

# Add user to video + render groups for DRM/GPU access
sudo usermod -aG video "$CURRENT_USER" 2>/dev/null || true
sudo usermod -aG render "$CURRENT_USER" 2>/dev/null || true

log_info "Kiosk GUI setup complete ✓"
log_info "On next boot: auto-login → startx → PyQt5 GUI → http://localhost:5000"

else
    log_info "Skipping kiosk GUI install (--no-gui flag)"
fi

# ════════════════════════════════════════════════════════════════════════
# Step 9: Start service & verify
# ════════════════════════════════════════════════════════════════════════
log_step "Step 9/$TOTAL_STEPS — Starting ProComm service"

sudo systemctl start procomm-app
sleep 3

if systemctl is-active --quiet procomm-app; then
    log_info "procomm-app service is RUNNING ✓"
else
    log_error "Service failed to start! Check logs:"
    log_error "  tail -50 $INSTALL_DIR/app.log"
    log_error "  sudo journalctl -u procomm-app --no-pager -n 30"
fi

# Get IP for the web UI URL
LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
LOCAL_IP=${LOCAL_IP:-"<pi-ip>"}

# ════════════════════════════════════════════════════════════════════════
# Summary
# ════════════════════════════════════════════════════════════════════════
echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║                                                          ║"
echo "║           ProComm Deployment Complete! ✓                 ║"
echo "║                                                          ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
echo "  Audio:     USB dongles"
echo "  Web UI:    http://${LOCAL_IP}:5000"
if [ "$INSTALL_GUI" == "true" ]; then
echo "  Kiosk:     PyQt5 WebEngine GUI on HDMI (auto-boot)"
fi
echo "  Service:   sudo systemctl status procomm-app"
echo "  Logs:      tail -f $INSTALL_DIR/app.log"
echo ""
echo "  ┌─────────────────────────────────────────────────────┐"
echo "  │ NEXT STEPS:                                          │"
echo "  │                                                      │"
echo "  │ 1. Set SIP credentials from the Web UI:              │"
echo "  │    http://${LOCAL_IP}:5000  → Settings → SIP          │"
echo "  │                                                      │"
echo "  │ 2. Assign USB dongles to lines from the UI           │"
echo "  │                                                      │"
echo "  │ Service starts automatically on every boot.          │"
echo "  └─────────────────────────────────────────────────────┘"
echo ""

# Offer to edit config now
read -p "Edit SIP credentials now via terminal? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    nano "$INSTALL_DIR/smart_sip_config.json" || vi "$INSTALL_DIR/smart_sip_config.json"
    sudo systemctl restart procomm-app
    log_info "Service restarted with new config"
fi

exit 0
