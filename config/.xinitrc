#!/bin/bash
sleep 2

# Apply ProComm USB audio card names and wait until all rename events are done
sudo udevadm trigger --action=add --subsystem-match=sound
sudo udevadm settle --timeout=10

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
/home/procomm/ProComm_1/venv_with_system/bin/python /home/procomm/ProComm_1/main.py > /tmp/procomm_gui.log 2>&1 &

# Start window manager (keeps X alive)
exec /usr/bin/openbox-session
