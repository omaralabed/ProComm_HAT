#!/usr/bin/env python3
"""
Setup AES67 daemon with 8 TX sources and 8 RX sinks for ProComm
This creates the virtual ALSA devices that ProComm will use for Dante audio
"""

import requests
import json
import time

DAEMON_URL = "http://192.168.108.39:8080/api"
BASE_MCAST = "239.69.1."

def add_source(line_num):
    """Add a TX source (ProComm -> Dante)"""
    source_config = {
        "id": f"ProComm_Line_{line_num}_TX",
        "enabled": True,
        "name": f"ProComm Line {line_num} TX",
        "io": "Audio Device",
        "max_samples_per_packet": 48,
        "codec": "L24",
        "address": f"{BASE_MCAST}{line_num}",
        "ttl": 15,
        "payload_type": 98,
        "dscp": 34,
        "refclk_ptp_domain": 0,
        "map": [0, 1]  # Stereo
    }
    
    try:
        resp = requests.post(f"{DAEMON_URL}/source/add", json=source_config, timeout=5)
        if resp.status_code == 200:
            print(f"✓ Added source: Line {line_num} TX")
            return True
        else:
            print(f"✗ Failed to add source Line {line_num}: {resp.status_code}")
            return False
    except Exception as e:
        print(f"✗ Error adding source Line {line_num}: {e}")
        return False

def add_sink(line_num):
    """Add an RX sink (Dante -> ProComm)"""
    sink_config = {
        "id": f"ProComm_Line_{line_num}_RX",
        "enabled": True,
        "name": f"ProComm Line {line_num} RX",
        "io": "Audio Device",
        "max_samples_per_packet": 48,
        "codec": "L24",
        "address": f"{BASE_MCAST}{100 + line_num}",  # Use different multicast addresses
        "use_sdp": False,
        "sdp": "",
        "refclk_ptp_domain": 0,
        "map": [0, 1]  # Stereo
    }
    
    try:
        resp = requests.post(f"{DAEMON_URL}/sink/add", json=sink_config, timeout=5)
        if resp.status_code == 200:
            print(f"✓ Added sink: Line {line_num} RX")
            return True
        else:
            print(f"✗ Failed to add sink Line {line_num}: {resp.status_code}")
            return False
    except Exception as e:
        print(f"✗ Error adding sink Line {line_num}: {e}")
        return False

def main():
    print("Setting up AES67 streams for ProComm Dante integration...")
    print()
    
    # Add 8 sources (TX)
    print("Creating TX sources (ProComm → Dante):")
    for i in range(1, 9):
        add_source(i)
        time.sleep(0.2)
    
    print()
    
    # Add 8 sinks (RX)
    print("Creating RX sinks (Dante → ProComm):")
    for i in range(1, 9):
        add_sink(i)
        time.sleep(0.2)
    
    print()
    print("✓ AES67 stream setup complete!")
    print()
    print("ALSA devices should now be available:")
    print("  TX: aes67_source_0 through aes67_source_7")
    print("  RX: aes67_sink_0 through aes67_sink_7")

if __name__ == "__main__":
    main()
