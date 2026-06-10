#!/bin/bash
sleep 2

# Force 1080p resolution on HDMI
xrandr --output HDMI-A-1 --mode 1920x1080 2>/dev/null || \
xrandr --output HDMI-1 --mode 1920x1080 2>/dev/null || true

# Disable screen blanking / power saving
xset s off
xset -dpms
xset s noblank

# Hide mouse cursor after 3 seconds of inactivity
unclutter -idle 3 -root &

# Launch ProComm GUI (PyQt5 WebEngine — starts app.py automatically)
python3 /home/procomm/ProComm_HAT/main.py > /tmp/procomm_gui.log 2>&1 &

# Start window manager (keeps X alive)
exec /usr/bin/openbox-session
