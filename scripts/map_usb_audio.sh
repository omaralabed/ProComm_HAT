#!/bin/bash
# =============================================================================
# map_usb_audio.sh — Map USB audio dongles to stable channel numbers
# =============================================================================
#
# Reads /proc/asound/cards and maps each C-Media USB dongle to a channel
# by its physical USB port path (e.g. usb-xhci-hcd.0-1.1.1).
# No udev card renaming needed — works reliably at boot on any kernel.
#
# Port wiring (physical USB hub):
#   1.1.1 → CH1    1.1.2 → CH2    1.1.3 → CH3    1.1.4 → CH4
#   1.2.1 → CH5    1.2.2 → CH6    1.2.3 → CH7    1.2.4 → CH8
#   1.3.1 → Headset
#
# Output (stdout): JSON channel map + HEADSET_CARD line
#   { "1": 3, "2": 5, ... }
#   HEADSET_CARD=7
#
# Called by:
#   - App startup (engine.py) to discover current ALSA card numbers
#
# Usage:
#   map_usb_audio.sh [scan]
# =============================================================================

MAP_FILE="/run/procomm/usb_audio_map.json"
LOG_FILE="/run/procomm/usb_audio_map.log"

mkdir -p /run/procomm 2>/dev/null
chmod 777 /run/procomm 2>/dev/null || true

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG_FILE" 2>/dev/null
}

# Map USB port suffix → channel number
# The port path appears in /proc/asound/cards as e.g. "usb-xhci-hcd.0-1.1.1"
port_to_channel() {
    case "$1" in
        *-1.1.1) echo "1" ;;
        *-1.1.2) echo "2" ;;
        *-1.1.3) echo "3" ;;
        *-1.1.4) echo "4" ;;
        *-1.2.1) echo "5" ;;
        *-1.2.2) echo "6" ;;
        *-1.2.3) echo "7" ;;
        *-1.2.4) echo "8" ;;
        *-1.3.1) echo "headset" ;;
        *) echo "" ;;
    esac
}

scan_and_map() {
    log "Scanning USB audio devices from /proc/asound/cards..."

    local json="{"
    local first=true
    local headset_card=-1
    local card_num=""
    local in_usb_card=false

    # /proc/asound/cards has pairs of lines:
    #  N [name   ]: driver - short_name
    #               long description including USB path
    while IFS= read -r line; do
        # Line 1: " N [name]: ..."
        if [[ "$line" =~ ^[[:space:]]*([0-9]+)[[:space:]]*\[ ]]; then
            card_num="${BASH_REMATCH[1]}"
            # Only care about USB Audio cards (C-Media dongles)
            if echo "$line" | grep -q "USB-Audio"; then
                in_usb_card=true
            else
                in_usb_card=false
            fi
        # Line 2: description with USB port path
        elif [ "$in_usb_card" = true ] && [ -n "$card_num" ]; then
            # Extract the USB port token e.g. "usb-xhci-hcd.0-1.1.1,"
            usb_port=$(echo "$line" | grep -oE 'usb-[^ ,]+' | head -1)
            if [ -n "$usb_port" ]; then
                ch=$(port_to_channel "$usb_port")
                if [ "$ch" = "headset" ]; then
                    headset_card=$card_num
                    log "  Headset: Card $card_num (port $usb_port)"
                elif [ -n "$ch" ]; then
                    log "  Channel $ch: Card $card_num (port $usb_port)"
                    if [ "$first" = true ]; then
                        first=false
                    else
                        json+=", "
                    fi
                    json+="\"${ch}\": ${card_num}"
                fi
            fi
            in_usb_card=false
            card_num=""
        fi
    done < /proc/asound/cards

    json+="}"

    # Write to files
    echo "$json" > "$MAP_FILE" 2>/dev/null && chmod 644 "$MAP_FILE" 2>/dev/null || true
    echo "$headset_card" > "/run/procomm/headset_card.txt" 2>/dev/null && \
        chmod 644 "/run/procomm/headset_card.txt" 2>/dev/null || true
    log "Channel map: $json  Headset card: $headset_card"

    # Print to stdout (read by engine.py)
    echo "$json"
    echo "HEADSET_CARD=$headset_card"
}

# ── Main ──
ACTION="${1:-scan}"

case "$ACTION" in
    add|remove|scan)
        log "Action: $ACTION ${2:-}"
        scan_and_map
        ;;
    *)
        echo "Usage: $0 [scan]"
        exit 1
        ;;
esac
