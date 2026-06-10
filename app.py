"""
PhoneSystem-Web - Flask Application with Smart SIP Engine
Web-based 8-line phone system for broadcast production
"""

import os
import sys
import json
import logging
import subprocess
import time
import threading
import socket
import re
_re = re  # alias used in netplan helpers
import atexit
import traceback
from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit
from flask_cors import CORS

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from smart_sip.engine import SIPEngine
from smart_sip.line import LineState

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = 'procomm-secret-key'
_APP_START_TIME = str(int(time.time()))

# Disable static file caching so browser always gets latest JS/CSS
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

@app.after_request
def add_no_cache_headers(response):
    """Prevent browser caching of static files (JS, CSS, HTML)."""
    if request.path.startswith('/static/') or request.path in ('/', '/phone', '/ifb', '/pl'):
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response

# Enable CORS for all routes
CORS(app, resources={r"/*": {"origins": "*"}})

# Initialize SocketIO with threading mode (logger=False in production to reduce I/O)
socketio = SocketIO(
    app, 
    cors_allowed_origins="*",
    async_mode='threading',
    ping_timeout=60,
    ping_interval=25,
    logger=False,
    engineio_logger=False
)

# Phone system components
sip_engine = None
sip_status_task_started = False  # Guard flag to prevent double-start
sip_server_reachable = False  # Track SIP server connectivity
boot_timestamp = time.time()  # Timestamp when app started - used for boot modal

# PID file for instance locking
PID_FILE = '/tmp/procomm_app.pid'


def acquire_lock():
    """
    Acquire exclusive lock via PID file.
    Returns True if lock acquired, False if another instance is running.
    """
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, 'r') as f:
                old_pid = int(f.read().strip())
            
            # Check if process is still running
            try:
                os.kill(old_pid, 0)  # Signal 0 checks if process exists
                logger.error(f"Another instance is already running (PID {old_pid})")
                logger.error("Stop the other instance first: sudo systemctl stop procomm-app")
                return False
            except OSError:
                # Process doesn't exist, stale PID file
                logger.warning(f"Removing stale PID file (PID {old_pid} not running)")
                os.remove(PID_FILE)
        except Exception as e:
            logger.warning(f"Error reading PID file: {e}, removing it")
            try:
                os.remove(PID_FILE)
            except Exception:
                pass
    
    # Write our PID
    try:
        with open(PID_FILE, 'w') as f:
            f.write(str(os.getpid()))
        logger.info(f"Acquired lock, PID {os.getpid()} written to {PID_FILE}")
        return True
    except Exception as e:
        logger.error(f"Failed to write PID file: {e}")
        return False


def release_lock():
    """Release the PID file lock"""
    try:
        if os.path.exists(PID_FILE):
            with open(PID_FILE, 'r') as f:
                pid = int(f.read().strip())
            if pid == os.getpid():
                os.remove(PID_FILE)
                logger.info(f"Released lock, removed {PID_FILE}")
    except Exception as e:
        logger.warning(f"Error releasing lock: {e}")


def cleanup_on_exit():
    """Cleanup handler for graceful shutdown"""
    logger.info("Shutting down ProComm...")
    release_lock()
    if sip_engine:
        try:
            sip_engine.stop()
        except Exception as e:
            logger.error(f"Error stopping SIP engine: {e}")


def signal_handler(signum, frame):
    """Handle SIGTERM/SIGINT for graceful shutdown"""
    logger.info(f"Received signal {signum}, shutting down...")
    release_lock()
    # Don't call sys.exit() here — it corrupts Flask-SocketIO threading.
    # Let the main thread handle shutdown naturally.
    raise SystemExit(0)


def check_sip_server_reachable(server_ip, server_port, timeout=2):
    """
    Check if SIP server is reachable by sending a minimal SIP OPTIONS probe.
    Uses UDP for port 5060, TLS/TCP for port 5061.
    Returns True if reachable, False otherwise.
    """
    try:
        use_tls = (int(server_port) == 5061)
        transport = "TLS" if use_tls else "UDP"
        probe = (
            f"OPTIONS sip:{server_ip}:{server_port} SIP/2.0\r\n"
            f"Via: SIP/2.0/{transport} 0.0.0.0:5060;branch=z9hG4bKping\r\n"
            f"From: <sip:ping@0.0.0.0>;tag=ping\r\n"
            f"To: <sip:{server_ip}>\r\n"
            f"Call-ID: ping@procomm\r\n"
            f"CSeq: 1 OPTIONS\r\n"
            f"Max-Forwards: 70\r\n"
            f"Content-Length: 0\r\n\r\n"
        )
        sock = None
        if use_tls:
            import ssl
            raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            raw_sock.settimeout(timeout)
            raw_sock.connect((server_ip, int(server_port)))
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
            sock = ssl_ctx.wrap_socket(raw_sock, server_hostname=server_ip)
            sock.sendall(probe.encode())
            data = sock.recv(2048)
            sock.close()
        else:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(timeout)
            sock.sendto(probe.encode(), (server_ip, int(server_port)))
            data, _ = sock.recvfrom(2048)
            sock.close()
        return data is not None and len(data) > 0
    except socket.timeout:
        if sock:
            try:
                sock.close()
            except Exception:
                pass
        return False
    except Exception as e:
        logger.debug(f"SIP server reachability check failed: {e}")
        if sock:
            try:
                sock.close()
            except Exception:
                pass
        return False


def _save_line_assignments():
    """Save per-line audio channel assignments to config so they survive reboots."""
    try:
        config_path = os.path.join(os.path.dirname(__file__), 'smart_sip_config.json')
        with open(config_path) as f:
            cfg = json.load(f)
        assignments = {}
        for line_id in range(1, 9):
            line = sip_engine.get_line(line_id)
            if line and line.audio_channel is not None:
                assignments[str(line_id)] = line.audio_channel
        cfg['line_channel_assignments'] = assignments
        with open(config_path, 'w') as f:
            json.dump(cfg, f, indent=2)
        logger.info(f"Saved line channel assignments: {assignments}")
    except Exception as e:
        logger.error(f"Failed to save line assignments: {e}")


def _restore_line_assignments():
    """Re-apply saved per-line audio channel assignments after engine start."""
    try:
        config_path = os.path.join(os.path.dirname(__file__), 'smart_sip_config.json')
        with open(config_path) as f:
            cfg = json.load(f)
        assignments = cfg.get('line_channel_assignments', {})
        for line_id_str, channel in assignments.items():
            line_id = int(line_id_str)
            line = sip_engine.get_line(line_id)
            if line and channel:
                line.set_audio_channel(channel)
                if hasattr(sip_engine.audio, 'set_line_channel'):
                    sip_engine.audio.set_line_channel(line_id, channel)
                logger.info(f"Restored: Line {line_id} -> Channel {channel}")
        if assignments:
            logger.info(f"Line channel assignments restored: {assignments}")
    except Exception as e:
        logger.error(f"Failed to restore line assignments: {e}")


def init_phone_system():
    """Initialize Smart SIP Engine (non-blocking - web server will start regardless)"""
    global sip_engine
    
    try:
        # Find config file
        config_path = os.path.join(os.path.dirname(__file__), 'smart_sip_config.json')
        
        if not os.path.exists(config_path):
            logger.error(f"Config file not found: {config_path}")
            return False
        
        # Initialize SIP engine
        # Lines 1-8: USB dongle lines (physical audio)
        # Lines 9-28: Browser phone lines (WebRTC audio, no USB card)
        logger.info("Initializing Smart SIP Engine...")
        sip_engine = SIPEngine(num_lines=35, config_path=config_path)
        
        # Load configuration
        sip_engine.load_config()
        
        # Register callback for state changes
        sip_engine.on_state_change = on_line_state_change
        # Note: on_registration_update is not set — outgoing_only=true skips REGISTER entirely

        # ── Audio monitor WebRTC callback (listen-only for phone browsers) ──
        threading.Thread(target=_register_audio_monitor_callback, daemon=True,
                         name='audio-monitor-reg').start()

        # ── Audio level meter polling (DECOUPLED from audio threads) ──────
        # CRITICAL: Do NOT call socketio.emit() from the audio worker thread —
        # blocking I/O there causes ALSA write delays and re-introduces the
        # channel-switch drift bug. Instead, the worker just updates an
        # in-memory dict, and we poll it here at a fixed rate.
        def _audio_level_emitter():
            import time as _t
            # Wait for the engine + audio manager to come up
            while True:
                am = getattr(sip_engine, 'audio', None)
                if am and hasattr(am, 'get_levels'):
                    break
                _t.sleep(0.5)
            logger.info("Audio level meter emitter started ✓")
            while True:
                try:
                    levels = am.get_levels()
                    for ch, (in_db, out_db) in levels.items():
                        socketio.emit('audio_levels', {
                            'channel': ch,
                            'in_db': round(in_db, 1),
                            'out_db': round(out_db, 1)
                        })
                except Exception as _e:
                    logger.debug(f"audio level emit error: {_e}")
                _t.sleep(0.2)  # 5 Hz update rate

        threading.Thread(target=_audio_level_emitter, daemon=True,
                         name="AudioLevelEmitter").start()

        # Start the engine in a background thread so web server starts immediately
        # This way, even if network/gateway is wrong, GUI is still accessible
        def start_sip_engine_background():
            try:
                logger.info("Starting SIP engine in background...")
                if not sip_engine.start():
                    logger.error("Failed to start SIP engine - will retry on config save")
                else:
                    logger.info("Smart SIP Engine started successfully")
            except Exception as e:
                logger.error(f"Error starting SIP engine in background: {e}")
        
        # Start in background thread with timeout protection
        sip_thread = threading.Thread(target=start_sip_engine_background, daemon=True)
        sip_thread.start()
        
        logger.info("Smart SIP Engine initialization started (non-blocking)")
        return True  # Always return True so web server starts
        
    except Exception as e:
        logger.error(f"Failed to initialize phone system: {e}")
        traceback.print_exc()
        return True  # Still return True so web server starts - user can fix via GUI


def _build_sip_status_payload():
    """Build the SIP status payload dict used by emit/polling/API.
    
    Returns (payload_dict, is_reachable) or (None, False) if engine is down.
    Also updates the global sip_server_reachable cache.
    """
    global sip_server_reachable
    if not sip_engine:
        return None, False
    
    all_status = sip_engine.get_all_status()
    line_statuses = [{'line_id': i + 1, 'registered': all_status[i].get('registered', False)} for i in range(min(8, len(all_status)))]
    # Pad to 8 lines so UI always gets 8 entries
    while len(line_statuses) < 8:
        line_statuses.append({'line_id': len(line_statuses) + 1, 'registered': False})
    
    config = sip_engine.config if hasattr(sip_engine, 'config') else {}
    sip_server = f"{config.get('server_ip', 'unknown')}:{config.get('server_port', 5060)}"
    outgoing_only = config.get('outgoing_only', False)

    server_ip = config.get('server_ip', 'newyork1.voip.ms')
    server_port = config.get('server_port', 5060)
    is_reachable = check_sip_server_reachable(server_ip, server_port)
    sip_server_reachable = is_reachable

    # In outgoing-only mode REGISTER is never sent so registered_count is always 0.
    # Report total=0 so the UI knows not to display a "0/8 registered" fraction.
    if outgoing_only:
        registered_count = 0
        total_lines = 0
    else:
        registered_count = sum(1 for s in all_status if s.get('registered', False))
        total_lines = 8

    return {
        'connected': is_reachable,
        'registered': registered_count,
        'total': total_lines,
        'outgoing_only': outgoing_only,
        'server': sip_server,
        'lines': line_statuses
    }, is_reachable


def emit_sip_status_once():
    """Build and emit sip_status once (e.g. on each registration so UI shows 1/8..8/8)."""
    try:
        payload, _ = _build_sip_status_payload()
        if payload is None:
            return
        socketio.emit('sip_status', payload)
        logger.debug(f"Emitted sip_status (registration update): {payload['registered']}/8, reachable: {payload['connected']}")
    except Exception as e:
        logger.warning(f"emit_sip_status_once failed: {e}")


def on_line_state_change(line_id, old_state, new_state):
    """Callback for line state changes - broadcast to all clients"""
    try:
        state_name = new_state.name.lower() if hasattr(new_state, 'name') else str(new_state).lower()
        status = sip_engine.get_line_status(line_id) if sip_engine else {}
        socketio.emit('line_status', {
            'line_id': line_id,
            'state': state_name,
            'phone_number': status.get('phone_number', ''),
            'caller_id': status.get('caller_id', ''),
            'duration': status.get('duration', 0)
        })

        # Notify browser phone clients assigned to this line
        try:
            from smart_sip import browser_lines as _bl
            token = _bl._line_to_token.get(line_id)
            if token:
                entry = _bl.get_entry(token)
                sid = entry.get('session_id') if entry else None
                if sid:
                    if state_name in ('active', 'connected'):
                        socketio.emit('phone_call_connected', {
                            'token': token,
                            'number': status.get('phone_number', '')
                        }, to=sid)
                        # Start WebRTC bridge for browser lines (9+)
                        if line_id >= 9 and entry and 'webrtc' not in entry:
                            from smart_sip.audio_webrtc import start_webrtc_for_line
                            bridge = start_webrtc_for_line(
                                token, line_id, socketio, sid, entry
                            )
                            # Find client by line_id (safe if any earlier lines failed)
                            client = next(
                                (c for c in sip_engine.clients
                                 if c.line.line_id == line_id), None
                            )
                            if client:
                                if client.rtp_stream:
                                    client.rtp_stream.on_audio_received = bridge.push_output
                                def _mic_to_rtp(pcm, _c=client):
                                    if _c.rtp_stream:
                                        _c.rtp_stream.send_audio(pcm)
                                bridge.set_input_callback(_mic_to_rtp)
                    elif state_name == 'idle':
                        socketio.emit('phone_call_ended', {
                            'token': token
                        }, to=sid)
                        # Close WebRTC bridge if one is running
                        if line_id >= 9 and entry and 'webrtc' in entry:
                            entry['webrtc'].close()
        except Exception as be:
            logger.debug(f"Browser phone state notify error: {be}")

        # ── PL line state handling (lines 29-35) ─────────────────────────
        try:
            if PL_LINE_START <= line_id <= PL_LINE_END:
                if state_name == 'connected':
                    # Set up RTP RX fan-out: SIP audio → all crew WebRTC bridges
                    client = next(
                        (c for c in sip_engine.clients if c.line.line_id == line_id), None
                    )
                    if client and client.rtp_stream:
                        def _pl_fan_out(pcm, _lid=line_id):
                            with _pl_lock:
                                bridges = [v['bridge'] for v in _pl_bridges.values()
                                           if v['line_id'] == _lid]
                            for b in bridges:
                                try:
                                    b.push_output(pcm)
                                except Exception:
                                    pass
                        client.rtp_stream.on_audio_received = _pl_fan_out

                    # Start WebRTC for any crew members who already dialed this line
                    with _pl_pending_lock:
                        pending_sids = [sid for sid, lid in list(_pl_pending.items())
                                        if lid == line_id]
                        for sid in pending_sids:
                            del _pl_pending[sid]
                    for sid in pending_sids:
                        _start_pl_webrtc(line_id, sid, is_dialer=True)

                elif state_name in ('idle', 'error', 'busy'):
                    # Close all crew WebRTC bridges for this line.
                    # Handles idle (normal end), error (bad number/network),
                    # and busy (far-end busy) — don't wait for auto-reset to idle.
                    with _pl_lock:
                        to_close = [(sid, v['bridge'])
                                    for sid, v in list(_pl_bridges.items())
                                    if v['line_id'] == line_id]
                        for sid, _ in to_close:
                            del _pl_bridges[sid]
                    for sid, bridge in to_close:
                        try:
                            bridge.close()
                        except Exception:
                            pass
                        socketio.emit('pl_call_ended', {'line_id': line_id}, to=sid)
                    # Broadcast to ALL clients as a reliable fallback — pl.js
                    # ignores it if data.line_id !== plLineId, so this is safe.
                    # This covers members whose bridge sids are stale/missing
                    # (e.g. page reload, pending dial before SIP connected,
                    # or reconnect after the dialer already removed their entry).
                    socketio.emit('pl_call_ended', {'line_id': line_id})
                    # Tear down the mixer once all bridges are gone — its tick
                    # thread has nothing to mix without them.
                    _stop_pl_mixer(line_id)
                    _stop_conf_mixer(line_id)
                    # Clear any pending dials for this line
                    with _pl_pending_lock:
                        for sid in [s for s, l in list(_pl_pending.items()) if l == line_id]:
                            del _pl_pending[sid]
                    # Drop the dialer registration — the call is over, so the
                    # next person to dial this line becomes its new dialer.
                    with _pl_lock:
                        _pl_dialer_by_line.pop(line_id, None)
        except Exception as ple:
            logger.debug(f"PL state notify error: {ple}")

    except Exception as e:
        logger.error(f"Error in state change callback: {e}")
        traceback.print_exc()


# ============================================================================
# WEB ROUTES
# ============================================================================

@app.route('/')
def index():
    """Main phone system page"""
    return render_template('index.html')


@app.route('/debug')
def debug():
    """Debug console page"""
    return render_template('debug.html')


@app.route('/sip-settings')
def sip_settings():
    """SIP configuration page"""
    return render_template('sip_settings.html')


@app.route('/network-settings')
def network_settings():
    """Network configuration page"""
    return render_template('network_settings.html')


@app.route('/api/device/info', methods=['GET'])
def api_device_info():
    """Get device information including serial number (MAC address)"""
    try:
        import uuid
        # Get MAC address of the primary interface (eth0 or wlan0)
        mac = ':'.join(['{:02x}'.format((uuid.getnode() >> i) & 0xff) for i in range(0,8*6,8)][::-1])
        serial = f"PC-{mac.replace(':', '').upper()[-6:]}"
        
        return jsonify({
            'product': 'ProComm / SIP Engine',
            'model': 'ProComm-8L',
            'version': '1.0.0',
            'serial': serial,
            'mac': mac.upper()
        })
    except Exception as e:
        logger.error(f"Error getting device info: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/boot/check', methods=['GET'])
def api_boot_check():
    """Return boot timestamp so each client can decide if they've seen the modal"""
    global boot_timestamp
    return jsonify({
        'boot_timestamp': boot_timestamp,
        'current_time': time.time()
    })


@app.route('/qr')
def qr_page():
    """QR code image for the /phone page — points to <hostname>.local:5443/phone"""
    try:
        import qrcode
        import io
        from flask import send_file
        url = f'http://{socket.gethostname()}.local:5443/phone'
        qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M,
                            box_size=8, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color='black', back_color='white')
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        return send_file(buf, mimetype='image/png', max_age=0)
    except Exception as e:
        logger.error(f"QR generation failed: {e}")
        return f"QR error: {e}", 500


@app.route('/qr_ui')
def qr_ui_page():
    """QR code image for the main UI — points to http://<hostname>.local:5443"""
    try:
        import qrcode
        import io
        from flask import send_file
        url = f'http://{socket.gethostname()}.local:5443'
        qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M,
                            box_size=8, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color='black', back_color='white')
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        return send_file(buf, mimetype='image/png', max_age=0)
    except Exception as e:
        logger.error(f"QR UI generation failed: {e}")
        return f"QR error: {e}", 500


@app.route('/qr_ifb')
def qr_ifb_page():
    """QR code image for the /ifb page — points to procomm.local:5443/ifb"""
    try:
        import qrcode
        import io
        from flask import send_file
        url = 'https://procomm.local:5443/ifb'
        qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M,
                            box_size=8, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color='black', back_color='white')
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        return send_file(buf, mimetype='image/png', max_age=0)
    except Exception as e:
        logger.error(f"QR IFB generation failed: {e}")
        return f"QR error: {e}", 500


@app.route('/phone')
def phone_page():
    """Browser softphone page — served to users who scan the QR code"""
    return render_template('phone.html')


@app.route('/ifb')
def ifb_page():
    """IFB monitor page — producers scan QR to listen to any active line"""
    return render_template('ifb.html')


@app.route('/qr_pl')
def qr_pl_page():
    """QR code image for the /pl page — points to procomm.local:5443/pl"""
    try:
        import qrcode
        import io
        from flask import send_file
        url = 'https://procomm.local:5443/pl'
        qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M,
                            box_size=8, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color='black', back_color='white')
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        return send_file(buf, mimetype='image/png', max_age=0)
    except Exception as e:
        logger.error(f"QR PL generation failed: {e}")
        return f"QR error: {e}", 500


@app.route('/pl')
def pl_page():
    """Party Line page — crew members join PL conference lines"""
    return render_template('pl.html')


@app.route('/api/pl/lines')
def api_pl_lines():
    """Return status of PL lines 29-35."""
    try:
        lines = []
        for line_id in range(PL_LINE_START, PL_LINE_END + 1):
            if sip_engine:
                try:
                    status = sip_engine.get_line_status(line_id)
                    lines.append({
                        'line_id':      line_id,
                        'state':        status.get('state', 'idle'),
                        'phone_number': status.get('phone_number', ''),
                        'caller_id':    status.get('caller_id', ''),
                    })
                except Exception:
                    lines.append({'line_id': line_id, 'state': 'idle',
                                  'phone_number': '', 'caller_id': ''})
            else:
                lines.append({'line_id': line_id, 'state': 'idle',
                              'phone_number': '', 'caller_id': ''})
        return jsonify({'lines': lines})
    except Exception as e:
        logger.error(f"api_pl_lines error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/phone/lines', methods=['GET'])
def api_phone_lines():
    """Return status of all 20 browser phone lines (9-28) for the operator monitor."""
    try:
        from smart_sip import browser_lines as _bl
        lines = []
        for line_id in range(_bl.BROWSER_LINE_START, _bl.BROWSER_LINE_END + 1):
            token = _bl._line_to_token.get(line_id)
            entry = _bl._tokens.get(token) if token else None
            call_state = 'idle'
            if sip_engine and entry:
                try:
                    st = sip_engine.get_line_status(line_id)
                    s = st.get('state', 'idle').lower()
                    if s in ('connected', 'active'):
                        call_state = 'incall'
                    elif s in ('dialing', 'ringing', 'calling'):
                        call_state = 'dialing'
                except Exception:
                    pass
            lines.append({
                'line_id':    line_id,
                'assigned':   entry is not None,
                'connected':  entry.get('connected', False) if entry else False,
                'call_state': call_state,
            })
        connected = sum(1 for l in lines if l['connected'])
        return jsonify({'lines': lines, 'connected': connected, 'max': _bl.BROWSER_LINE_MAX})
    except Exception as e:
        logger.error(f"api_phone_lines error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/phone/reset', methods=['POST'])
def api_phone_reset():
    """Reset all browser phone lines — called by operator when moving Pi to new location.
    Hangs up any active SIP calls on lines 9-28, closes WebRTC bridges, then clears all tokens.
    Connected browsers receive phone_lines_reset and must scan the QR code again.
    """
    try:
        from smart_sip import browser_lines as _bl

        # Hang up active calls and close WebRTC bridges on all browser lines
        if sip_engine:
            for line_id in range(_bl.BROWSER_LINE_START, _bl.BROWSER_LINE_END + 1):
                try:
                    entry = _bl.get_entry(_bl._line_to_token.get(line_id, ''))
                    if entry and 'webrtc' in entry:
                        entry['webrtc'].close()
                    sip_engine.hangup_call(line_id)
                except Exception:
                    pass

        count = _bl.reset_all()
        logger.info(f"Operator reset all browser phone lines ({count} cleared)")
        socketio.emit('phone_lines_reset', {'count': count})
        return jsonify({'ok': True, 'cleared': count})
    except Exception as e:
        logger.error(f"Phone reset failed: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/system/pi-status', methods=['GET'])
def api_pi_status():
    """Get Raspberry Pi system status (temperature, CPU, memory)"""
    try:
        status = {
            'temperature': '—',
            'cpu_usage': '—',
            'memory_used': '—',
            'memory_total': '—',
            'uptime': '—'
        }
        
        # Get CPU temperature
        try:
            temp_output = subprocess.run(['vcgencmd', 'measure_temp'], 
                                        capture_output=True, text=True, timeout=2)
            if temp_output.returncode == 0:
                # Extract temperature from "temp=58.2'C" format
                temp_str = temp_output.stdout.strip()
                if 'temp=' in temp_str:
                    temp_value = temp_str.split('temp=')[1].split("'")[0]
                    status['temperature'] = f"{temp_value}°C"
        except Exception as e:
            logger.debug(f"Failed to get temperature: {e}")
        
        # Get CPU usage
        try:
            cpu_output = subprocess.run(['top', '-bn1'], 
                                       capture_output=True, text=True, timeout=2)
            if cpu_output.returncode == 0:
                for line in cpu_output.stdout.splitlines():
                    if 'Cpu(s)' in line:
                        # Extract idle percentage and calculate usage
                        parts = line.split(',')
                        for part in parts:
                            if 'id' in part:
                                idle = float(part.split()[0])
                                usage = 100 - idle
                                status['cpu_usage'] = f"{usage:.1f}%"
                                break
                        break
        except Exception as e:
            logger.debug(f"Failed to get CPU usage: {e}")
        
        # Get memory usage
        try:
            mem_output = subprocess.run(['free', '-h'], 
                                       capture_output=True, text=True, timeout=2)
            if mem_output.returncode == 0:
                lines = mem_output.stdout.splitlines()
                if len(lines) >= 2:
                    mem_line = lines[1].split()
                    if len(mem_line) >= 3:
                        status['memory_total'] = mem_line[1]
                        status['memory_used'] = mem_line[2]
        except Exception as e:
            logger.debug(f"Failed to get memory usage: {e}")
        
        # Get uptime
        try:
            uptime_output = subprocess.run(['uptime', '-p'], 
                                          capture_output=True, text=True, timeout=2)
            if uptime_output.returncode == 0:
                uptime_str = uptime_output.stdout.strip()
                # Remove "up " prefix if present
                if uptime_str.startswith('up '):
                    uptime_str = uptime_str[3:]
                status['uptime'] = uptime_str
        except Exception as e:
            logger.debug(f"Failed to get uptime: {e}")
        
        return jsonify(status)
        
    except Exception as e:
        logger.error(f"Error getting Pi status: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/audio/status', methods=['GET'])
def api_audio_status():
    """Return per-channel dongle health by reading the map produced by map_usb_audio.sh."""
    try:
        # Read channel→card map written by map_usb_audio.sh at startup
        map_path = '/run/procomm/usb_audio_map.json'
        hs_path = '/run/procomm/headset_card.txt'

        ch_map = {}  # channel str -> card_num int
        if os.path.exists(map_path):
            with open(map_path) as f:
                ch_map = json.load(f)  # e.g. {"1": 3, "2": 5, ...}

        hs_card = -1
        if os.path.exists(hs_path):
            try:
                hs_card = int(open(hs_path).read().strip())
            except (ValueError, IOError):
                pass

        def card_id(card_num):
            id_path = f'/proc/asound/card{card_num}/id'
            try:
                return open(id_path).read().strip()
            except IOError:
                return 'USB Audio'

        channels = []
        for ch in range(1, 9):
            card = ch_map.get(str(ch), -1)
            ok = card != -1
            channels.append({
                'channel': ch,
                'card': card if ok else '—',
                'name': card_id(card) if ok else 'Not found',
                'ok': ok
            })

        hs_ok = hs_card != -1
        channels.append({
            'channel': 'headset',
            'card': hs_card if hs_ok else '—',
            'name': card_id(hs_card) if hs_ok else 'Not found',
            'ok': hs_ok
        })

        return jsonify({'channels': channels})
    except Exception as e:
        logger.error(f"Error getting audio status: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/audio/fix-names', methods=['POST'])
def api_audio_fix_names():
    """Restart dead audio channels and remap USB dongles."""
    try:
        if sip_engine and hasattr(sip_engine, 'audio') and sip_engine.audio:
            result = sip_engine.audio.restart_dead_channels()
            logger.info(f"restart_dead_channels result: {result}")
            # Restart headset dongle only if its worker is dead.
            # Use the audio manager's RESOLVED card (it auto-detects the USB
            # headset), not the raw config value which may be stale (e.g. 0 =
            # the HAT) on HAT-based units.
            headset_card = getattr(sip_engine.audio, '_headset_card', -1)
            headset_already_running = getattr(sip_engine.audio, '_headset_running', False)
            if headset_card >= 0 and not headset_already_running and hasattr(sip_engine.audio, 'start_headset'):
                try:
                    sip_engine.audio.start_headset(headset_card)
                    logger.info(f"Re-apply: headset restarted on card {headset_card}")
                except Exception as he:
                    logger.warning(f"Re-apply: headset restart failed: {he}")
            else:
                logger.info(f"Re-apply: headset already running (card {headset_card}), skipping restart")
            return jsonify({'status': 'ok', 'result': result})
        else:
            return jsonify({'error': 'Audio manager not ready'}), 503
    except Exception as e:
        logger.error(f"Error restarting audio channels: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================================
# API ENDPOINTS - Lines
# ============================================================================

@app.route('/api/lines', methods=['GET'])
def api_get_all_lines():
    """Get status of all 8 lines"""
    try:
        if sip_engine is None:
            logger.error("SIP engine not initialized")
            return jsonify({'error': 'SIP engine not initialized'}), 503
            
        lines_status = []
        for line_id in range(1, 9):
            status = sip_engine.get_line_status(line_id)
            lines_status.append({
                'line_id': line_id,
                'state': status.get('state', 'idle'),
                'phone_number': status.get('phone_number', ''),
                'caller_id': status.get('caller_id', ''),
                'duration': status.get('duration', 0),
                'audio_channel': status.get('audio_channel', 0),
                'sip_registered': status.get('registered', False)
            })
        out = {'lines': lines_status}
        headset_listen = sip_engine.get_headset_listen_line()
        if headset_listen is not None:
            out['headset_listen_line'] = headset_listen
        active_line = sip_engine.get_active_line()
        if active_line is not None:
            out['headset_talk_line'] = active_line
        return jsonify(out)
    except Exception as e:
        logger.error(f"Error getting lines status: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/lines/<int:line_id>', methods=['GET'])
def api_get_line(line_id):
    """Get status of specific line"""
    try:
        if sip_engine is None:
            return jsonify({'error': 'SIP engine not initialized'}), 503
        if not 1 <= line_id <= 8:
            return jsonify({'error': 'Invalid line ID'}), 400

        status = sip_engine.get_line_status(line_id)
        return jsonify({
            'line_id': line_id,
            'state': status.get('state', 'idle'),
            'phone_number': status.get('phone_number', ''),
            'caller_id': status.get('caller_id', ''),
            'duration': status.get('duration', 0),
            'audio_channel': status.get('audio_channel', 0),
            'sip_registered': status.get('registered', False)
        })
    except Exception as e:
        logger.error(f"Error getting line {line_id} status: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/lines/<int:line_id>/dial', methods=['POST'])
def api_dial(line_id):
    """Dial a number on specified line"""
    try:
        if sip_engine is None:
            logger.error("SIP engine not initialized")
            return jsonify({'error': 'SIP engine not initialized'}), 503
            
        if not 1 <= line_id <= 8:
            return jsonify({'error': 'Invalid line ID'}), 400
        
        data = request.get_json() or {}
        phone_number = (data.get('phone_number') or '').strip()
        
        if not phone_number:
            return jsonify({'error': 'Phone number required'}), 400
        
        logger.info(f"Dialing {phone_number} on line {line_id}")
        success = sip_engine.make_call(line_id, phone_number)
        
        if success:
            return jsonify({'status': 'dialing', 'phone_number': phone_number})
        else:
            return jsonify({'error': 'Failed to dial'}), 500
            
    except Exception as e:
        logger.error(f"Error dialing on line {line_id}: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/lines/<int:line_id>/hangup', methods=['POST'])
def api_hangup(line_id):
    """Hang up call on specified line"""
    try:
        if sip_engine is None:
            logger.error("SIP engine not initialized")
            return jsonify({'error': 'SIP engine not initialized'}), 503
            
        if not 1 <= line_id <= 28:
            return jsonify({'error': 'Invalid line ID'}), 400
        
        logger.info(f"Hanging up line {line_id}")
        success = sip_engine.hangup_call(line_id)
        
        if success:
            return jsonify({'status': 'idle'})
        else:
            return jsonify({'error': 'Failed to hangup'}), 500
            
    except Exception as e:
        logger.error(f"Error hanging up line {line_id}: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/lines/<int:line_id>/force_idle', methods=['POST'])
def api_force_idle(line_id):
    """Force a line to IDLE state (emergency reset)"""
    try:
        if sip_engine is None:
            logger.error("SIP engine not initialized")
            return jsonify({'error': 'SIP engine not initialized'}), 503
            
        if not 1 <= line_id <= 8:
            return jsonify({'error': 'Invalid line ID'}), 400
        
        logger.warning(f"FORCE RESET: Setting line {line_id} to IDLE")
        
        # Clean up SIPClient resources first (timers, RTP, flags)
        client = sip_engine._get_client(line_id)
        if not client:
            logger.error(f"api_force_idle: no client found for line {line_id}")
            return jsonify({'error': 'Client not found'}), 404
        client._invite_in_progress = False
        client._cancel_invite_timeout()
        client._stop_session_timer()
        if client._error_reset_timer:
            try:
                client._error_reset_timer.cancel()
            except Exception:
                pass
            client._error_reset_timer = None
        if client.rtp_stream:
            client.rtp_stream.stop()
            client.rtp_stream = None
        
        # Get the line object directly
        line = sip_engine.lines[line_id - 1]

        old_state = line._state
        # Force state to IDLE by bypassing validation
        line._state = LineState.IDLE
        line._call_info = None
        line.local_tag = ""
        line.remote_tag = ""
        line.call_id = ""
        
        logger.warning(f"Line {line_id} forcibly reset to IDLE")
        
        # Trigger state change callback manually (use actual previous state)
        if sip_engine.on_state_change:
            sip_engine.on_state_change(line_id, old_state, LineState.IDLE)
        
        return jsonify({'status': 'idle', 'message': 'Line forcibly reset'})
            
    except Exception as e:
        logger.error(f"Error force resetting line {line_id}: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/lines/<int:line_id>/audio', methods=['POST'])
@app.route('/api/lines/<int:line_id>/channel', methods=['POST'])  # Alias for main.js / index_old
def api_set_audio_channel(line_id):
    """Set audio channel for specified line"""
    try:
        if sip_engine is None:
            logger.error("SIP engine not initialized")
            return jsonify({'error': 'SIP engine not initialized'}), 503
            
        if not 1 <= line_id <= 8:
            return jsonify({'error': 'Invalid line ID'}), 400
        
        data = request.get_json() or {}
        # Support both 'channel' and 'audio_channel' field names
        channel = data.get('channel', data.get('audio_channel', 0))
        
        # Validate channel:
        # - 0 = unassigned
        # - 1-8 = USB audio channels
        if isinstance(channel, int):
            if channel < 0 or channel > 8:
                return jsonify({'error': 'Invalid channel (must be 0-8)'}), 400
            audio_channel = None if channel == 0 else channel
        else:
            return jsonify({'error': 'Invalid channel type (must be int)'}), 400
        
        # Get the line and set its audio channel
        line = sip_engine.get_line(line_id)
        if line:
            # Set the line's audio channel (stores 1-8, or None)
            line.set_audio_channel(audio_channel)
            
            # Also update the AudioManager's channel routing
            if hasattr(sip_engine.audio, 'set_line_channel'):
                if audio_channel is not None:
                    sip_engine.audio.set_line_channel(line_id, audio_channel)
                    logger.info(f"API: Line {line_id} -> Channel {audio_channel}")
                else:
                    sip_engine.audio.set_line_channel(line_id, 0)
                    logger.info(f"API: Line {line_id} channel unassigned")
            _save_line_assignments()
            
            # Broadcast channel change to connected clients (for GUI updates)
            # Use 0 (not None/null) for unassigned — JS audioChannel state expects 0
            emit_channel = audio_channel if audio_channel is not None else 0
            socketio.emit('audio_channel_change', {
                'line_id': line_id,
                'channel': emit_channel,
                'audio_channel': emit_channel
            })
            
            return jsonify({
                'status': 'success',
                'line_id': line_id,
                'audio_channel': emit_channel
            })
        else:
            return jsonify({'error': 'Line not found'}), 404
            
    except Exception as e:
        logger.error(f"Error setting audio channel for line {line_id}: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/lines/<int:line_id>/dtmf', methods=['POST'])
def api_send_dtmf(line_id):
    """Send DTMF on specified line"""
    try:
        if sip_engine is None:
            return jsonify({'error': 'SIP engine not initialized'}), 503
        if not 1 <= line_id <= 8:
            return jsonify({'error': 'Invalid line ID'}), 400

        data = request.get_json() or {}
        digit = (data.get('digit') or '')
        
        if not digit:
            return jsonify({'error': 'Digit required'}), 400
        
        sip_engine.send_dtmf(line_id, digit)
        return jsonify({'status': 'sent', 'digit': digit})
        
    except Exception as e:
        logger.error(f"Error sending DTMF on line {line_id}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/headset/listen', methods=['POST'])
def api_headset_listen():
    """Set which line's audio is sent to headset out. 0 = none, 1-8 = line."""
    try:
        if sip_engine is None:
            return jsonify({'error': 'SIP engine not initialized'}), 503
        data = request.get_json() or {}
        line_id = data.get('line_id')
        if line_id is not None:
            line_id = int(line_id)
        success = sip_engine.set_headset_listen_line(line_id)
        if success:
            socketio.emit('headset_listen', {'line_id': line_id})
            return jsonify({'status': 'ok', 'headset_listen_line': line_id})
        return jsonify({'error': 'Invalid line_id (0-8)'}), 400
    except Exception as e:
        logger.error(f"Error setting headset listen: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/lines/<int:line_id>/mute', methods=['POST'])
def api_mute_line(line_id):
    """Mute/unmute specified line"""
    try:
        if sip_engine is None:
            return jsonify({'error': 'SIP engine not initialized'}), 503
        if not 1 <= line_id <= 8:
            return jsonify({'error': 'Invalid line ID'}), 400

        data = request.get_json() or {}
        muted = data.get('muted', False)
        
        success = sip_engine.set_line_mute(line_id, muted)
        if not success:
            return jsonify({'error': 'Failed to set mute'}), 500
        
        logger.info(f"Line {line_id}: Headset (listen & talk) set to {'muted' if muted else 'on'}")
        
        # Broadcast mute state and headset listen/talk to all clients
        socketio.emit('line_mute', {
            'line_id': line_id,
            'muted': muted
        })
        headset_listen = sip_engine.get_headset_listen_line()
        if headset_listen is not None:
            socketio.emit('headset_listen', {'line_id': headset_listen})
        
        return jsonify({'status': 'success', 'line_id': line_id, 'muted': muted})
        
    except Exception as e:
        logger.error(f"Error muting line {line_id}: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================================
# API ENDPOINTS - Audio Test
# ============================================================================

# Global test tone state
_test_tone_active = False

@app.route('/api/audio/test', methods=['POST'])
@app.route('/api/audio/test/start', methods=['POST'])  # Alias for UI compatibility
def api_start_test_tone():
    """Start test tone on specified channel"""
    global _test_tone_active
    try:
        data = request.get_json()
        channel = data.get('channel', 1)
        
        if sip_engine and sip_engine.audio:
            sip_engine.audio.play_test_tone(int(channel))
        _test_tone_active = True
        logger.info(f"Test tone started on channel {channel}")
        
        return jsonify({'status': 'playing', 'channel': channel})
        
    except Exception as e:
        logger.error(f"Error starting test tone: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/audio/test', methods=['DELETE'])
@app.route('/api/audio/test/stop', methods=['POST'])  # Alias for UI compatibility
def api_stop_test_tone():
    """Stop test tone"""
    global _test_tone_active
    try:
        if sip_engine and sip_engine.audio:
            sip_engine.audio.stop_test_tone()
        _test_tone_active = False
        logger.info("Test tone stopped")
        
        return jsonify({'status': 'stopped'})
        
    except Exception as e:
        logger.error(f"Error stopping test tone: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================================
# API ENDPOINTS - Audio Mixer
# ============================================================================

@app.route('/api/audio/mixer', methods=['GET'])
def api_get_mixer():
    """Get current mixer volumes for all channels"""
    try:
        if sip_engine and sip_engine.audio:
            volumes = sip_engine.audio.get_mixer_volumes()
            # Convert int keys to strings for JSON
            return jsonify({str(k): v for k, v in volumes.items()})
        return jsonify({})
    except Exception as e:
        logger.error(f"Error getting mixer volumes: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/audio/mixer', methods=['POST'])
def api_set_mixer():
    """Set mixer volume for a channel.
    Body: { "channel": 1, "type": "input"|"output", "value": 0-100 }
    """
    try:
        data = request.get_json()
        channel = int(data.get('channel', 1))
        vol_type = data.get('type', 'output')  # 'input' or 'output'
        value = int(data.get('value', 85))

        if sip_engine and sip_engine.audio:
            ok = sip_engine.audio.set_mixer_volume(channel, vol_type, value)
            if ok:
                return jsonify({'status': 'ok', 'channel': channel, 'type': vol_type, 'value': value})
            else:
                return jsonify({'error': f'Channel {channel} mixer not available'}), 400
        return jsonify({'error': 'Audio engine not ready'}), 503
    except Exception as e:
        logger.error(f"Error setting mixer volume: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================================
# API ENDPOINTS - Configuration
# ============================================================================

@app.route('/api/config/sip', methods=['GET'])
def api_get_sip_config():
    """Get SIP configuration"""
    try:
        config_path = os.path.join(os.path.dirname(__file__), 'smart_sip_config.json')
        with open(config_path, 'r') as f:
            config = json.load(f)
        return jsonify(config)
    except Exception as e:
        logger.error(f"Error reading SIP config: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/config/sip', methods=['POST'])
def api_save_sip_config():
    """Save SIP configuration and reload SIP engine so new credentials take effect."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No JSON body'}), 400
        config_path = os.path.join(os.path.dirname(__file__), 'smart_sip_config.json')

        # Merge incoming data into existing config so audio/I2S fields are preserved
        existing_config = {}
        try:
            with open(config_path, 'r') as f:
                existing_config = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        existing_config.update(data)

        # Save merged config to file
        with open(config_path, 'w') as f:
            json.dump(existing_config, f, indent=2)

        logger.info("SIP configuration saved to file")

        # Reload SIP engine so it uses new config (password, server, etc.) without app restart
        if sip_engine is not None:
            try:
                sip_engine.stop()
                if sip_engine.start():  # start() loads config from file and re-registers
                    logger.info("SIP engine reloaded with new config")
                    return jsonify({'status': 'saved', 'message': 'Configuration saved. SIP engine reloaded with new settings.'})
                else:
                    logger.warning("SIP engine failed to start after config save")
                    return jsonify({'status': 'saved', 'message': 'Configuration saved. SIP engine failed to start—please restart the app.'})
            except Exception as reload_err:
                logger.error(f"Error reloading SIP engine after config save: {reload_err}", exc_info=True)
                return jsonify({'status': 'saved', 'message': 'Configuration saved. Could not reload SIP engine—please restart the app.'})

        return jsonify({'status': 'saved', 'message': 'Configuration saved. Restart required for SIP engine.'})

    except Exception as e:
        logger.error(f"Error saving SIP config: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================================
# API ENDPOINTS - Phone Directory (shared so GUI and web interface see same data)
# ============================================================================

DIRECTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'phone_directory.json')

@app.route('/api/directory', methods=['GET'])
def api_get_directory():
    """Get phone directory (IFB/PL). Same data for GUI and web interface."""
    try:
        if os.path.exists(DIRECTORY_FILE):
            with open(DIRECTORY_FILE, 'r') as f:
                data = json.load(f)
            if isinstance(data.get('IFB'), list) and isinstance(data.get('PL'), list):
                return jsonify(data)
        return jsonify({'IFB': [], 'PL': []})
    except Exception as e:
        logger.error(f"Error reading phone directory: {e}")
        return jsonify({'IFB': [], 'PL': []})


@app.route('/api/directory', methods=['POST'])
def api_save_directory():
    """Save phone directory. Persists on server so GUI and web match after reboot."""
    try:
        data = request.get_json()
        if data is None:
            return jsonify({'error': 'JSON body required'}), 400
        if not isinstance(data.get('IFB'), list):
            data['IFB'] = []
        if not isinstance(data.get('PL'), list):
            data['PL'] = []
        with open(DIRECTORY_FILE, 'w') as f:
            json.dump(data, f, indent=2)
        logger.info("Phone directory saved")
        socketio.emit('directory_updated', {})
        return jsonify({'status': 'saved'})
    except Exception as e:
        logger.error(f"Error saving phone directory: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================================
# API ENDPOINTS - Network
# ============================================================================

@app.route('/api/config/network', methods=['GET'])
def api_get_network_config():
    """Get network configuration"""
    try:
        # Get current IP address
        result = subprocess.run(['hostname', '-I'], capture_output=True, text=True)
        current_ip = result.stdout.strip().split()[0] if result.stdout else "Unknown"
        
        return jsonify({
            'mode': 'dhcp',
            'current_ip': current_ip,
            'ip_address': current_ip,
            'subnet_mask': '255.255.255.0',
            'gateway': '',
            'dns_server': '8.8.8.8'
        })
        
    except Exception as e:
        logger.error(f"Error reading network config: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/network/status', methods=['GET'])
def api_network_status():
    """Get current network status from netplan and system"""
    try:
        # Get eth0 IP specifically
        result = subprocess.run(
            ['ip', '-4', 'addr', 'show', 'eth0'],
            capture_output=True, text=True, timeout=5
        )
        current_ip = "Unknown"
        for ln in result.stdout.split('\n'):
            ln = ln.strip()
            if ln.startswith('inet '):
                current_ip = ln.split()[1].split('/')[0]
                break

        # Check eth0 link state
        try:
            with open('/sys/class/net/eth0/operstate') as _f:
                eth0_connected = _f.read().strip() == 'up'
        except Exception:
            eth0_connected = False
        
        # Read netplan config
        netplan_path = '/etc/netplan/50-cloud-init.yaml'
        is_dhcp = True
        static_ip = ''
        gateway = ''
        subnet_mask = '255.255.255.0'
        dns_server = '8.8.8.8'
        
        try:
            result = subprocess.run(
                ['sudo', 'cat', netplan_path],
                capture_output=True, text=True, timeout=5
            )
            config_text = result.stdout
            
            # Parse YAML manually (simple parsing) - FIXED: Only check eth0 section
            in_eth0_section = False
            for line in config_text.split('\n'):
                stripped = line.strip()
                # Detect eth0 section start (4 spaces + "eth0:")
                if '    eth0:' in line and stripped == 'eth0:':
                    in_eth0_section = True
                    continue
                # Exit eth0 section when we hit another top-level interface (4 spaces + name:)
                if in_eth0_section and line.startswith('    ') and ':' in stripped and stripped != 'eth0:' and not line.startswith('      '):
                    in_eth0_section = False
                # Check dhcp4 only within eth0 section  
                if in_eth0_section and ('dhcp4: false' in stripped or 'dhcp4: no' in stripped):
                    is_dhcp = False
            
            # Extract addresses - FIXED: Only from eth0 section
            in_eth0_section = False
            for line in config_text.split('\n'):
                stripped = line.strip()
                # Detect eth0 section start (4 spaces + "eth0:")
                if '    eth0:' in line and stripped == 'eth0:':
                    in_eth0_section = True
                    continue
                # Exit eth0 section when we hit another top-level interface (4 spaces + name:)
                if in_eth0_section and line.startswith('    ') and ':' in stripped and stripped != 'eth0:' and not line.startswith('      '):
                    in_eth0_section = False
                # Parse only in eth0 section
                if in_eth0_section and stripped.startswith('- ') and '/' in stripped and not stripped.startswith('- to:'):
                    # This is likely an IP address line like "- 192.168.1.100/24"
                    addr = stripped[2:].strip()
                    if '/' in addr:
                        static_ip = addr.split('/')[0]
                        cidr = addr.split('/')[1]
                        subnet_mask = {32:'255.255.255.255',31:'255.255.255.254',30:'255.255.255.252',29:'255.255.255.248',28:'255.255.255.240',27:'255.255.255.224',26:'255.255.255.192',25:'255.255.255.128',24:'255.255.255.0',23:'255.255.254.0',22:'255.255.252.0',21:'255.255.248.0',20:'255.255.240.0',16:'255.255.0.0',8:'255.0.0.0'}.get(int(cidr), '255.255.255.0')
                elif in_eth0_section and 'via:' in stripped:
                    # Gateway line like "via: 192.168.1.1"
                    gateway = stripped.split('via:')[1].strip()
            
            # Also look for nameservers
            in_nameservers = False
            for line in config_text.split('\n'):
                if 'nameservers:' in line:
                    in_nameservers = True
                elif in_nameservers and line.strip().startswith('- '):
                    dns_server = line.strip()[2:].strip()
                    break
                elif in_nameservers and not line.strip().startswith('-') and not line.strip().startswith('addresses'):
                    in_nameservers = False
                    
        except Exception as e:
            logger.error(f"Error reading netplan config: {e}")
        
        # If DHCP, get actual subnet mask and gateway from system
        if is_dhcp:
            try:
                # Get subnet mask from ip addr show
                addr_result = subprocess.run(
                    ['ip', 'addr', 'show', 'eth0'],
                    capture_output=True, text=True, timeout=5
                )
                for line in addr_result.stdout.split('\n'):
                    if 'inet ' in line and '/' in line:
                        # Line looks like: "inet 192.168.108.36/23 brd ..."
                        parts = line.strip().split()
                        for part in parts:
                            if '/' in part and '.' in part:
                                ip_cidr = part
                                cidr = ip_cidr.split('/')[1]
                                subnet_mask = {32:'255.255.255.255',31:'255.255.255.254',30:'255.255.255.252',29:'255.255.255.248',28:'255.255.255.240',27:'255.255.255.224',26:'255.255.255.192',25:'255.255.255.128',24:'255.255.255.0',23:'255.255.254.0',22:'255.255.252.0',21:'255.255.248.0',20:'255.255.240.0',16:'255.255.0.0',8:'255.0.0.0'}.get(int(cidr), '255.255.255.0')
                                break
            except Exception as e:
                logger.error(f"Error reading IP address: {e}")
            
            # Get gateway from routing table
            if not gateway:
                try:
                    route_result = subprocess.run(
                        ['ip', 'route', 'show', 'default'],
                        capture_output=True, text=True, timeout=5
                    )
                    for line in route_result.stdout.split('\n'):
                        if 'default via' in line:
                            parts = line.split()
                            if len(parts) >= 3 and parts[0] == 'default' and parts[1] == 'via':
                                gateway = parts[2]
                                break
                except Exception as e:
                    logger.error(f"Error reading default route: {e}")
        
        return jsonify({
            'mode': 'dhcp' if is_dhcp else 'manual',
            'current_ip': current_ip,
            'ip_address': static_ip if static_ip else current_ip,
            'subnet_mask': subnet_mask,
            'gateway': gateway,
            'dns_server': dns_server,
            'connected': eth0_connected
        })
            
    except Exception as e:
        logger.error(f"Error getting network status: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/network/set-dhcp', methods=['POST'])
def api_set_dhcp():
    """Set network to DHCP mode using netplan - FIXED: Preserves other interfaces"""
    try:
        logger.info("Setting network to DHCP mode")
        
        # Read existing netplan to preserve other interfaces (eth1, wlan, etc.)
        existing_netplan = _read_netplan()
        
        # Build new eth0 stanza
        eth0_stanza = """    eth0:
      optional: true
      dhcp4: true
      dhcp6: true
"""
        
        # Replace or add eth0 section while preserving everything else
        pattern = _re.compile(
            r'( {4}eth0:\n(?:([ ]{6,}[^\n]*\n|\n))*)',
            _re.MULTILINE
        )
        
        if pattern.search(existing_netplan):
            # Replace existing eth0 section
            netplan_config = pattern.sub(eth0_stanza, existing_netplan, count=1)
        else:
            # Add eth0 to ethernets section
            if 'ethernets:' in existing_netplan:
                # Insert after ethernets: line
                netplan_config = existing_netplan.replace('ethernets:\n', 'ethernets:\n' + eth0_stanza)
            else:
                # Create new ethernets section
                netplan_config = """network:
  version: 2
  ethernets:
""" + eth0_stanza
        
        # Write netplan config
        netplan_path = '/etc/netplan/50-cloud-init.yaml'
        result = subprocess.run(
            ['sudo', 'tee', netplan_path],
            input=netplan_config,
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode != 0:
            logger.error(f"Failed to write netplan config: {result.stderr}")
            return jsonify({'error': f'Failed to set DHCP: {result.stderr}'}), 500
        
        # Apply netplan
        result = subprocess.run(['sudo', 'netplan', 'apply'], capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            logger.error(f"Failed to apply netplan: {result.stderr}")
            return jsonify({'error': f'Failed to apply network config: {result.stderr}'}), 500
        
        logger.info("DHCP mode set successfully, rebooting system")
        
        # Schedule reboot after responding
        def delayed_reboot():
            time.sleep(2)
            subprocess.run(['sudo', 'reboot'], check=False)
        
        threading.Thread(target=delayed_reboot, daemon=True).start()
        
        return jsonify({'status': 'success', 'message': 'Network set to DHCP mode, system rebooting'})
        
    except Exception as e:
        logger.error(f"Error setting DHCP: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/network/set-static', methods=['POST'])
def api_set_static():
    """Set static IP address using netplan - FIXED: Preserves other interfaces"""
    try:
        data = request.get_json() or {}
        ip_address = (data.get('ip_address') or '').strip()
        subnet_mask = (data.get('subnet_mask') or '255.255.255.0').strip()
        gateway = (data.get('gateway') or '').strip()
        dns_server = (data.get('dns_server') or '8.8.8.8').strip()
        
        # Validate IP address
        if not ip_address:
            return jsonify({'error': 'IP address is required'}), 400
        
        if not gateway:
            return jsonify({'error': 'Gateway is required'}), 400
        
        # Convert subnet mask to CIDR (e.g., 255.255.255.0 -> /24)
        cidr = _subnet_to_cidr(subnet_mask)
        ip_with_cidr = f"{ip_address}/{cidr}"
        
        logger.info(f"Setting static IP: {ip_with_cidr}, gateway: {gateway}")
        
        # Read existing netplan to preserve other interfaces
        existing_netplan = _read_netplan()
        
        # Create eth0 static stanza
        eth0_stanza = f"""    eth0:
      optional: true
      dhcp4: false
      dhcp6: false
      addresses:
        - {ip_with_cidr}
      routes:
        - to: default
          via: {gateway}
      nameservers:
        addresses:
          - {dns_server}
"""
        
        # Replace or add eth0 section while preserving everything else
        pattern = _re.compile(
            r'( {4}eth0:\n(?:([ ]{6,}[^\n]*\n|\n))*)',
            _re.MULTILINE
        )
        
        if pattern.search(existing_netplan):
            # Replace existing eth0 section
            netplan_config = pattern.sub(eth0_stanza, existing_netplan, count=1)
        else:
            # Add eth0 to ethernets section
            if 'ethernets:' in existing_netplan:
                netplan_config = existing_netplan.replace('ethernets:\n', 'ethernets:\n' + eth0_stanza)
            else:
                netplan_config = """network:
  version: 2
  ethernets:
""" + eth0_stanza
        
        # Write netplan config
        netplan_path = '/etc/netplan/50-cloud-init.yaml'
        result = subprocess.run(
            ['sudo', 'tee', netplan_path],
            input=netplan_config,
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode != 0:
            logger.error(f"Failed to write netplan config: {result.stderr}")
            return jsonify({'error': f'Failed to set static IP: {result.stderr}'}), 500
        
        # Apply netplan
        result = subprocess.run(['sudo', 'netplan', 'apply'], capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            logger.error(f"Failed to apply netplan: {result.stderr}")
            return jsonify({'error': f'Failed to apply network config: {result.stderr}'}), 500
        
        logger.info(f"Static IP {ip_address} set successfully, rebooting system")
        
        # Schedule reboot after responding
        def delayed_reboot():
            time.sleep(2)
            subprocess.run(['sudo', 'reboot'], check=False)
        
        threading.Thread(target=delayed_reboot, daemon=True).start()
        
        return jsonify({
            'status': 'success',
            'message': f'Static IP {ip_address} configured, system rebooting',
            'ip_address': ip_address
        })
        
    except Exception as e:
        logger.error(f"Error setting static IP: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================================
# API ENDPOINTS - Network Interfaces (eth0 main LAN, eth1 Dante, WiFi)
# ============================================================================

def _subnet_to_cidr(subnet_mask):
    """Convert a subnet to a CIDR prefix length, accepting any valid form.

    Handles:
      • a dotted mask  → '255.255.255.0', '255.255.254.0', '255.240.0.0', …
      • a CIDR number  → '24', '/24', 24
    Computes the prefix algorithmically (not a fixed table) so ANY valid mask
    /0-/32 works. Falls back to 24 only if the input is unparseable.
    """
    if subnet_mask is None:
        return 24
    s = str(subnet_mask).strip().lstrip('/')
    # Already a CIDR number?
    if s.isdigit():
        n = int(s)
        return n if 0 <= n <= 32 else 24
    # Dotted mask → count the contiguous 1-bits
    try:
        octets = [int(o) for o in s.split('.')]
        if len(octets) != 4 or any(o < 0 or o > 255 for o in octets):
            return 24
        bits = ''.join(f'{o:08b}' for o in octets)
        # Valid masks are 1s followed by 0s; count the leading 1s either way.
        return bits.count('1')
    except Exception:
        return 24



def _read_netplan():
    """Read raw netplan YAML text, empty string on error."""
    try:
        r = subprocess.run(['sudo', 'cat', '/etc/netplan/50-cloud-init.yaml'],
                           capture_output=True, text=True, timeout=5)
        return r.stdout if r.returncode == 0 else ''
    except Exception:
        return ''


def _read_all_netplan():
    """Concatenate every /etc/netplan/*.yaml file. Used for read-only mode
    detection so interfaces configured in their own file (e.g. eth1 in
    70-procomm-eth1.yaml) are also seen."""
    try:
        r = subprocess.run('sudo cat /etc/netplan/*.yaml',
                           shell=True, capture_output=True, text=True, timeout=5)
        return r.stdout if r.returncode == 0 else ''
    except Exception:
        return ''


def _get_iface_info(name):
    """Return a live-status dict for one network interface."""
    info = {
        'name': name,
        'type': 'wifi' if name.startswith('wl') else 'ethernet',
        'enabled': True, 'connected': False,
        'ip': '', 'subnet': '255.255.255.0', 'gateway': '',
        'mac': '', 'mode': 'dhcp', 'ssid': '',
    }
    # Link state
    try:
        r = subprocess.run(['ip', 'link', 'show', name],
                           capture_output=True, text=True, timeout=4)
        if r.returncode != 0:
            info['enabled'] = False
            return info
        if 'state UP' in r.stdout or 'state UNKNOWN' in r.stdout:
            info['connected'] = True
    except Exception as e:
        logger.error(f"_get_iface_info link {name}: {e}")

    # IP + MAC
    try:
        r = subprocess.run(['ip', 'addr', 'show', name],
                           capture_output=True, text=True, timeout=4)
        cidr_map = {32: '255.255.255.255', 31: '255.255.255.254', 30: '255.255.255.252',
                    29: '255.255.255.248', 28: '255.255.255.240', 27: '255.255.255.224',
                    26: '255.255.255.192', 25: '255.255.255.128', 24: '255.255.255.0',
                    23: '255.255.254.0', 22: '255.255.252.0', 21: '255.255.248.0',
                    20: '255.255.240.0', 16: '255.255.0.0', 8: '255.0.0.0'}
        for line in r.stdout.splitlines():
            line = line.strip()
            if line.startswith('inet ') and '/' in line:
                parts = line.split()
                ip, cidr = parts[1].split('/')
                info['ip'] = ip
                info['subnet'] = cidr_map.get(int(cidr), '255.255.255.0')
                info['connected'] = True
            if 'link/ether' in line:
                info['mac'] = line.split()[1]
    except Exception as e:
        logger.error(f"_get_iface_info addr {name}: {e}")

    # Default gateway for this interface
    try:
        r = subprocess.run(['ip', 'route', 'show', 'dev', name],
                           capture_output=True, text=True, timeout=4)
        for line in r.stdout.splitlines():
            if 'default' in line and 'via' in line:
                parts = line.split()
                via_idx = parts.index('via')
                if via_idx + 1 < len(parts):
                    info['gateway'] = parts[via_idx + 1]
    except Exception as e:
        logger.error(f"_get_iface_info route {name}: {e}")

    # DHCP vs static from netplan (read ALL netplan files so interfaces
    # configured in their own file — e.g. eth1 in 70-procomm-eth1.yaml — are seen)
    netplan = _read_all_netplan()
    in_block = False
    for line in netplan.splitlines():
        stripped = line.strip().rstrip(':')
        # Detect when entering the interface block (4 spaces + interface name)
        if f'    {name}:' in line and stripped == name:
            in_block = True
        # Check for static mode within the block
        if in_block and ('dhcp4: false' in line or 'dhcp4: no' in line):
            info['mode'] = 'static'
            break
        # Exit block when hitting another top-level interface (4 spaces + colon, but not 6+ spaces)
        if in_block and line.startswith('    ') and ':' in line and not line.startswith('      ') and stripped != name:
            in_block = False

    # If interface has no live IP (e.g., NO-CARRIER), read configured values from netplan
    if not info['ip'] and info['mode'] == 'static':
        try:
            cidr_map = {32: '255.255.255.255', 31: '255.255.255.254', 30: '255.255.255.252',
                        29: '255.255.255.248', 28: '255.255.255.240', 27: '255.255.255.224',
                        26: '255.255.255.192', 25: '255.255.255.128', 24: '255.255.255.0',
                        23: '255.255.254.0', 22: '255.255.252.0', 21: '255.255.248.0',
                        20: '255.255.240.0', 16: '255.255.0.0', 8: '255.0.0.0'}
            # Parse netplan for configured IP/subnet
            in_block = False
            for line in netplan.splitlines():
                stripped = line.strip().rstrip(':')
                if stripped == name:
                    in_block = True
                    continue
                if in_block:
                    # Look for "- 10.12.6.11/24" format
                    match = _re.search(r'-\s+([\d.]+)/(\d+)', line)
                    if match:
                        info['ip'] = match.group(1)
                        cidr = int(match.group(2))
                        info['subnet'] = cidr_map.get(cidr, '255.255.255.0')
                    # Look for gateway
                    if 'via:' in line:
                        gw_match = _re.search(r'via:\s+([\d.]+)', line)
                        if gw_match:
                            info['gateway'] = gw_match.group(1)
                    # Exit block when we hit another interface or section
                    if line.strip() and not line.startswith(' ') and stripped != name:
                        break
        except Exception as e:
            logger.error(f"_get_iface_info netplan parse {name}: {e}")

    # WiFi SSID
    if info['type'] == 'wifi':
        try:
            r = subprocess.run(['iwgetid', name, '--raw'],
                               capture_output=True, text=True, timeout=4)
            info['ssid'] = r.stdout.strip()
        except Exception:
            pass

    return info


@app.route('/api/network/interfaces', methods=['GET'])
def api_network_interfaces():
    """Return live status of eth0, eth1 (Dante), wlan0 — only those that exist."""
    try:
        r = subprocess.run(['ip', 'link', 'show'], capture_output=True, text=True, timeout=5)
        existing = set()
        for line in r.stdout.splitlines():
            parts = line.split(':')
            if len(parts) >= 2 and parts[0].strip().isdigit():
                existing.add(parts[1].strip().split('@')[0])

        interfaces = [_get_iface_info(n) for n in ['eth0', 'eth1', 'wlan0', 'wlan1'] if n in existing]
        return jsonify({'interfaces': interfaces})
    except Exception as e:
        logger.error(f"Error listing interfaces: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/network/interface/<name>/enable', methods=['POST'])
def api_interface_enable(name):
    """Bring an interface up."""
    try:
        subprocess.run(['sudo', 'ip', 'link', 'set', name, 'up'], capture_output=True, timeout=5)
        subprocess.run(['sudo', 'netplan', 'apply'], capture_output=True, timeout=30)
        logger.info(f"Interface {name} enabled")
        return jsonify({'status': 'ok', 'message': f'{name} enabled'})
    except Exception as e:
        logger.error(f"Error enabling {name}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/network/interface/<name>/disable', methods=['POST'])
def api_interface_disable(name):
    """
    Bring an interface down.
    eth0 → reboots (disabling main LAN mid-session is destructive).
    eth1/wlan* → link down only, no reboot.
    """
    try:
        subprocess.run(['sudo', 'ip', 'link', 'set', name, 'down'], capture_output=True, timeout=5)
        if name == 'eth0':
            logger.info("eth0 disabled — rebooting")
            def delayed_reboot():
                time.sleep(2)
                subprocess.run(['sudo', 'reboot'], check=False)
            threading.Thread(target=delayed_reboot, daemon=True).start()
            return jsonify({'status': 'ok', 'message': 'eth0 disabled, system rebooting', 'reboot': True})
        logger.info(f"Interface {name} disabled")
        return jsonify({'status': 'ok', 'message': f'{name} disabled'})
    except Exception as e:
        logger.error(f"Error disabling {name}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/network/interface/<name>/set', methods=['POST'])
def api_interface_set(name):
    """
    Set DHCP or static IP for a specific interface.

    Reboot policy:
      eth0  → always reboots  (changing main LAN IP breaks the current session)
      eth1  → netplan apply only  (Dante audio-only, no routing impact)
      wlan* → netplan apply only  (reconnects automatically)
    """
    try:
        data   = request.get_json() or {}
        mode   = data.get('mode', 'dhcp')
        ip     = (data.get('ip')      or '').strip()
        subnet = (data.get('subnet')  or '255.255.255.0').strip()
        gw     = (data.get('gateway') or '').strip()
        is_eth1 = (name == 'eth1')
        is_wlan = name.startswith('wlan')
        if mode == 'static' and not ip:
            return jsonify({'error': 'IP address is required for static mode'}), 400

        # ── eth1 (Dante) — dedicated clean netplan file ────────────────────────
        # eth1 gets its OWN complete, valid netplan file instead of being merged
        # into 50-cloud-init.yaml (which is fragile and lost its `network:` header,
        # causing "unknown key 'ethernets'"). No gateway/DNS so the Dante network
        # stays isolated and eth0 remains the only default route. chmod 600 avoids
        # the "permissions too open" warning.
        if name == 'eth1':
            if mode == 'dhcp':
                body = (
                    "network:\n"
                    "  version: 2\n"
                    "  renderer: NetworkManager\n"
                    "  ethernets:\n"
                    "    eth1:\n"
                    "      optional: true\n"
                    "      dhcp4: true\n"
                    "      dhcp6: false\n"
                )
            else:
                cidr = _subnet_to_cidr(subnet)
                body = (
                    "network:\n"
                    "  version: 2\n"
                    "  renderer: NetworkManager\n"
                    "  ethernets:\n"
                    "    eth1:\n"
                    "      optional: true\n"
                    "      dhcp4: false\n"
                    "      dhcp6: false\n"
                    "      addresses:\n"
                    f"        - {ip}/{cidr}\n"
                )
            path = '/etc/netplan/70-procomm-eth1.yaml'
            wr = subprocess.run(['sudo', 'tee', path],
                                input=body, capture_output=True, text=True, timeout=10)
            if wr.returncode != 0:
                return jsonify({'error': f'Failed to write netplan: {wr.stderr}'}), 500
            subprocess.run(['sudo', 'chmod', '600', path], capture_output=True, timeout=5)
            ar = subprocess.run(['sudo', 'netplan', 'apply'],
                                capture_output=True, text=True, timeout=30)
            if ar.returncode != 0:
                logger.error(f"netplan apply error (eth1): {ar.stderr}")
                return jsonify({'error': f'netplan apply failed: {ar.stderr.strip()}'}), 500
            logger.info(f"eth1 (Dante) configured: mode={mode} ip={ip or 'dhcp'}")
            return jsonify({'status': 'ok',
                            'message': f'eth1 configured: {mode}{" · " + ip if ip else ""}',
                            'reboot': False})

        # Build netplan stanza for this interface
        if mode == 'dhcp':
            stanza = (
                f"    {name}:\n"
                "      optional: true\n"
                "      dhcp4: true\n"
                "      dhcp6: false\n"
            )
        else:
            cidr = _subnet_to_cidr(subnet)
            stanza = (
                f"    {name}:\n"
                "      optional: true\n"
                "      dhcp4: false\n"
                "      dhcp6: false\n"
                f"      addresses:\n"
                f"        - {ip}/{cidr}\n"
            )
            if gw:
                stanza += (
                    "      routes:\n"
                    "        - to: default\n"
                    f"          via: {gw}\n"
                )
            if not is_eth1:
                stanza += (
                    "      nameservers:\n"
                    "        addresses:\n"
                    "          - 8.8.8.8\n"
                    "          - 8.8.4.4\n"
                )

        # Merge into existing netplan
        netplan_path = '/etc/netplan/50-cloud-init.yaml'
        existing = _read_netplan()
        pattern = _re.compile(
            r'( {4}' + _re.escape(name) + r':\n(?:([ ]{6,}[^\n]*\n|\n))*)',
            _re.MULTILINE
        )
        if pattern.search(existing):
            new_yaml = pattern.sub(stanza, existing, count=1)
        else:
            section = 'wifis' if is_wlan else 'ethernets'
            if section + ':' in existing:
                new_yaml = existing.rstrip() + '\n' + stanza
            else:
                new_yaml = existing.rstrip() + '\n  ' + section + ':\n' + stanza


        wr = subprocess.run(['sudo', 'tee', netplan_path],
                            input=new_yaml, capture_output=True, text=True, timeout=10)
        if wr.returncode != 0:
            return jsonify({'error': f'Failed to write netplan: {wr.stderr}'}), 500

        ar = subprocess.run(['sudo', 'netplan', 'apply'], capture_output=True, text=True, timeout=30)
        if ar.returncode != 0:
            logger.error(f"netplan apply error: {ar.stderr}")
            return jsonify({'error': f'netplan apply failed: {ar.stderr}'}), 500

        logger.info(f"Interface {name} configured: mode={mode} ip={ip or 'dhcp'}")

        # eth0 always reboots; eth1 and wlan just apply
        if name == 'eth0':
            def delayed_reboot():
                time.sleep(2)
                subprocess.run(['sudo', 'reboot'], check=False)
            threading.Thread(target=delayed_reboot, daemon=True).start()
            return jsonify({'status': 'ok',
                            'message': f'eth0 configured ({mode}), system rebooting',
                            'reboot': True})

        return jsonify({'status': 'ok',
                        'message': f'{name} configured: {mode}{" · " + ip if ip else ""}',
                        'reboot': False})

    except Exception as e:
        logger.error(f"Error configuring {name}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/network/wifi/connect', methods=['POST'])
def api_wifi_connect():
    """
    Connect wlan0 to a new SSID via wpa_supplicant + netplan.
    No reboot needed — wpa_cli reconfigure reconnects automatically.
    """
    try:
        data     = request.get_json() or {}
        ssid     = (data.get('ssid')     or '').strip()
        password = (data.get('password') or '').strip()

        if not ssid:
            return jsonify({'error': 'SSID is required'}), 400

        # Write wpa_supplicant config
        if password:
            wpa_conf = (
                'ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\n'
                'update_config=1\ncountry=US\n\nnetwork={\n'
                f'    ssid="{ssid}"\n    psk="{password}"\n    key_mgmt=WPA-PSK\n}}\n'
            )
        else:
            wpa_conf = (
                'ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\n'
                'update_config=1\ncountry=US\n\nnetwork={\n'
                f'    ssid="{ssid}"\n    key_mgmt=NONE\n}}\n'
            )

        wr = subprocess.run(
            ['sudo', 'tee', '/etc/wpa_supplicant/wpa_supplicant.conf'],
            input=wpa_conf, capture_output=True, text=True, timeout=10
        )
        if wr.returncode != 0:
            return jsonify({'error': f'Failed to write WiFi config: {wr.stderr}'}), 500

        subprocess.run(['sudo', 'wpa_cli', '-i', 'wlan0', 'reconfigure'],
                       capture_output=True, timeout=10)

        # Save SSID into netplan for persistence across reboots
        netplan_path = '/etc/netplan/50-cloud-init.yaml'
        existing = _read_netplan()
        if password:
            wifi_stanza = (
                '    wlan0:\n'
                '      optional: true\n'
                '      dhcp4: true\n'
                '      access-points:\n'
                f'        "{ssid}":\n          password: "{password}"\n'
            )
        else:
            wifi_stanza = (
                '    wlan0:\n'
                '      optional: true\n'
                '      dhcp4: true\n'
                '      access-points:\n'
                f'        "{ssid}": {{}}\n'
            )

        pattern = _re.compile(
            r'( {4}wlan0:\n(?:([ ]{6,}[^\n]*\n|\n))*)',
            _re.MULTILINE
        )
        if pattern.search(existing):
            new_yaml = pattern.sub(wifi_stanza, existing, count=1)
        elif 'wifis:' in existing:
            new_yaml = existing.rstrip() + '\n' + wifi_stanza
        else:
            new_yaml = existing.rstrip() + '\n  wifis:\n' + wifi_stanza

        subprocess.run(['sudo', 'tee', netplan_path],
                       input=new_yaml, capture_output=True, text=True, timeout=10)
        subprocess.run(['sudo', 'netplan', 'apply'], capture_output=True, timeout=30)

        logger.info(f"WiFi connecting to SSID: {ssid}")
        return jsonify({'status': 'ok',
                        'message': f'Connecting to "{ssid}" — check status in a few seconds',
                        'reboot': False})

    except Exception as e:
        logger.error(f"Error connecting WiFi: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================================
# API ENDPOINTS - System
# ============================================================================

@app.route('/api/system/status', methods=['GET'])
def api_system_status():
    """Get system health status"""
    try:
        if not sip_engine:
            return jsonify({'status': 'stopped', 'sip_engine': 'stopped', 'lines_registered': 0})
        all_status = sip_engine.get_all_status()
        registered = sum(1 for s in all_status if s.get('registered', False))
        return jsonify({
            'status': 'running',
            'sip_engine': 'running',
            'lines_registered': registered,
            'app_version': _APP_START_TIME
        })
    except Exception as e:
        logger.error(f"Error getting system status: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/sip/status', methods=['GET'])
def api_sip_status():
    """Get detailed SIP registration status for all lines - always returns valid JSON"""
    try:
        # Safe defaults if engine is down
        if not sip_engine:
            logger.warning("SIP engine not initialized, returning safe defaults")
            return jsonify({
                'connected': False,
                'registered': 0,
                'registered_ok': False,
                'total_lines': 8,
                'registered_lines': 0,
                'lines_registered': 0,
                'server': '—',
                'lines': [{'line_id': i, 'registered': False} for i in range(1, 9)]
            }), 200
        
        payload, _ = _build_sip_status_payload()
        if payload is None:
            return jsonify({
                'connected': False,
                'registered': 0,
                'registered_ok': False,
                'total_lines': 8,
                'registered_lines': 0,
                'lines_registered': 0,
                'server': '—',
                'lines': [{'line_id': i, 'registered': False} for i in range(1, 9)]
            }), 200
        
        # Add extra compatibility fields for REST API consumers
        payload['registered_ok'] = payload['registered'] > 0
        payload['total_lines'] = 8
        payload['registered_lines'] = payload['registered']
        payload['lines_registered'] = payload['registered']
        
        return jsonify(payload), 200
        
    except Exception as e:
        logger.error(f"Error getting SIP status: {e}")
        # Return safe defaults on ANY error, never fail with 500
        return jsonify({
            'connected': False,
            'registered': 0,
            'registered_ok': False,
            'total_lines': 8,
            'registered_lines': 0,
            'lines_registered': 0,
            'server': '—',
            'lines': [{'line_id': i, 'registered': False} for i in range(1, 9)]
        }), 200


@app.route('/api/system/restart', methods=['POST'])
def api_restart_system():
    """Restart phone system service"""
    try:
        # Try systemd service first (procomm-app or procomm), fall back to app process
        for svc in ('procomm-app', 'procomm'):
            result = subprocess.run(['sudo', 'systemctl', 'restart', svc],
                                   timeout=5, capture_output=True)
            if result.returncode == 0:
                break
        else:
            logger.info("Service not found, restarting via process...")
            subprocess.Popen(['sudo', 'pkill', '-f', 'app\\.py'])
        return jsonify({'status': 'restarting'})
    except Exception as e:
        logger.error(f"Error restarting system: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/system/reboot', methods=['POST'])
def api_reboot_pi():
    """Reboot Raspberry Pi"""
    try:
        subprocess.Popen(['sudo', 'reboot'])
        return jsonify({'status': 'rebooting'})
    except Exception as e:
        logger.error(f"Error rebooting: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================================
# WEBSOCKET EVENTS
# ============================================================================

@socketio.on('connect')
def handle_connect():
    """Handle client connection"""
    logger.info(f"Client connected: {request.sid}")
    emit('connected', {'status': 'connected'})


@socketio.on('subscribe')
def handle_subscribe(data):
    """Subscribe to line updates"""
    lines = data.get('lines', [])
    logger.info(f"Client {request.sid} subscribed to lines: {lines}")


# ============================================================================
# AUDIO MONITOR — WebRTC listen-only (phone browser hears USB line audio)
# ============================================================================
# _monitor_sessions: sid → WebRTCMonitor  (one per connected phone browser)
_monitor_sessions: dict = {}
_monitor_lock = threading.Lock()

# ── PL (Party Line) sessions ────────────────────────────────────────────────
# Lines 29-35 are dedicated PL lines.
# _pl_bridges:  sid → {'line_id': int, 'bridge': WebRTCPLBridge, 'is_dialer': bool}
# _pl_pending:  sid → line_id  (crew member waiting for their dialed line to connect)
PL_LINE_START = 29
PL_LINE_END   = 35
_pl_bridges: dict = {}
_pl_lock     = threading.Lock()
_pl_pending: dict = {}
_pl_pending_lock = threading.Lock()
# _pl_mixers:  line_id → PLLineMixer  (one per active PL line)
# Mixes mic audio from every crew member's WebRTCPLBridge on the line and
# emits exactly one 20 ms PCM frame to the shared rtp_stream.send_audio()
# per tick.  Without this, two PL members on the same line would push 50 fps
# each into a queue drained at 50 pps → overflow → cut-outs at the studio.
_pl_mixers: dict = {}
_pl_mixers_lock = threading.Lock()
# _pl_conf_mixers:  line_id → PLConferenceMixer  (member→member audio)
_pl_conf_mixers: dict = {}
_pl_conf_mixers_lock = threading.Lock()
# _pl_dialer_by_line:  line_id → sid of the user who initiated this call.
# Persists across bridge teardowns (pl_leave / disconnect) so switching away
# from your own dialed line and back still shows "Hang Up" instead of "Leave".
# Cleared only when the SIP call actually ends (state → idle/error/busy).
_pl_dialer_by_line: dict = {}


def _get_or_create_monitor(line_id: int, sid: str):
    """Create a fresh monitor for line_id/sid, closing any existing one first."""
    from smart_sip.audio_webrtc import WebRTCMonitor
    with _monitor_lock:
        mon = _monitor_sessions.get(sid)
        if mon:
            try:
                mon.close()
            except Exception:
                pass
        mon = WebRTCMonitor(line_id, socketio, sid)
        _monitor_sessions[sid] = mon
    return mon


def _remove_monitor(sid: str):
    """Close and remove monitor for a disconnected/unsubscribed client."""
    with _monitor_lock:
        mon = _monitor_sessions.pop(sid, None)
    if mon:
        try:
            mon.close()
        except Exception:
            pass


def _get_or_create_pl_mixer(line_id: int):
    """Return the PLLineMixer for line_id, creating one if needed.

    Returns None when:
      - sip_engine isn't initialised yet
      - the SIP client doesn't exist or its RTP stream isn't ready
      - the PLLineMixer class isn't importable (audio_webrtc.py on the Pi
        is older than the matching app.py revision — caller falls back to
        a direct send path so audio still flows)

    Failures here are logged loudly because they were previously hidden in
    the PL state callback's DEBUG-only try/except, which made a stale deploy
    look like "no audio at all" with no obvious cause in the log.
    """
    if sip_engine is None:
        logger.warning(f"PL mixer line {line_id}: sip_engine not ready")
        return None

    with _pl_mixers_lock:
        mixer = _pl_mixers.get(line_id)
        if mixer is not None:
            return mixer

        client = next(
            (c for c in sip_engine.clients if c.line.line_id == line_id), None
        )
        if not client or not client.rtp_stream:
            logger.warning(
                f"PL mixer line {line_id}: client/rtp_stream not available "
                f"(client={'yes' if client else 'no'})"
            )
            return None

        try:
            from smart_sip.audio_webrtc import PLLineMixer
        except ImportError as ie:
            # audio_webrtc.py on the Pi may be older than the deployed app.py
            # — surface the mismatch instead of letting it silently drop audio.
            logger.error(
                f"PL mixer line {line_id}: PLLineMixer import failed "
                f"(audio_webrtc.py likely stale): {ie}"
            )
            return None

        # Late binding: look up client.rtp_stream on every send so a re-INVITE
        # that swaps the stream mid-call doesn't leave us writing into a
        # stopped one.  Capture the *client*, not the stream.
        def _send(pcm, _client=client):
            rtp = _client.rtp_stream
            if rtp is None:
                return
            try:
                rtp.send_audio(pcm)
            except Exception:
                pass

        try:
            mixer = PLLineMixer(line_id, _send)
        except Exception as me:
            logger.error(f"PL mixer line {line_id}: instantiation failed: {me}")
            return None
        _pl_mixers[line_id] = mixer
        return mixer


def _stop_pl_mixer(line_id: int):
    """Stop and drop the mixer for line_id (no-op if absent)."""
    with _pl_mixers_lock:
        mixer = _pl_mixers.pop(line_id, None)
    if mixer:
        try:
            mixer.stop()
        except Exception:
            pass


def _get_or_create_conf_mixer(line_id: int):
    """Return the PLConferenceMixer for line_id, creating one lazily if needed."""
    with _pl_conf_mixers_lock:
        mixer = _pl_conf_mixers.get(line_id)
        if mixer is not None:
            return mixer
        try:
            from smart_sip.audio_webrtc import PLConferenceMixer
        except ImportError as ie:
            logger.error(f"PL conf mixer line {line_id}: import failed: {ie}")
            return None
        try:
            mixer = PLConferenceMixer(line_id)
        except Exception as me:
            logger.error(f"PL conf mixer line {line_id}: instantiation failed: {me}")
            return None
        _pl_conf_mixers[line_id] = mixer
        return mixer


def _stop_conf_mixer(line_id: int):
    """Stop and drop the conf mixer for line_id (no-op if absent)."""
    with _pl_conf_mixers_lock:
        mixer = _pl_conf_mixers.pop(line_id, None)
    if mixer:
        try:
            mixer.stop()
        except Exception:
            pass


def _start_pl_webrtc(line_id: int, sid: str, is_dialer: bool = False):
    """Create and start a WebRTCPLBridge for a PL crew member."""
    # Close any existing bridge for this sid first — prevents double-bridge on
    # rapid re-join or retry (old thread would keep running and send a second offer).
    _remove_pl_bridge(sid)

    from smart_sip.audio_webrtc import WebRTCPLBridge
    bridge = WebRTCPLBridge(line_id, socketio, sid)

    # Route browser mic → per-line mixer (NOT directly into RTP).  The mixer
    # paces frames at 20 ms and sums audio across crew members, so two people
    # talking at once reach the studio cleanly instead of overrunning the
    # shared RTP TX queue.
    mixer = _get_or_create_pl_mixer(line_id)
    conf_mixer = _get_or_create_conf_mixer(line_id)
    if mixer is not None:
        mixer.add_member(sid)
        if conf_mixer is not None:
            conf_mixer.add_member(sid, bridge._member_q)
            def _mic_to_both(pcm, _m=mixer, _cm=conf_mixer, _sid=sid):
                _m.push(_sid, pcm)           # → studio (existing, unchanged)
                _cm.push_member(_sid, pcm)   # → peers (new)
            bridge.set_input_callback(_mic_to_both)
        else:
            # conf mixer unavailable — fall back to studio-only (existing behaviour)
            def _mic_to_mixer(pcm, _m=mixer, _sid=sid):
                _m.push(_sid, pcm)
            bridge.set_input_callback(_mic_to_mixer)
    else:
        # FALLBACK: mixer unavailable (stale audio_webrtc.py, late SIP setup,
        # etc.).  Send straight to the RTP stream so audio still reaches the
        # studio — at the cost of the old intermittent-cut-out behaviour when
        # more than one PL member is on the line.  Better than total silence.
        # If the bridge has a server-side PTT gate (newer audio_webrtc.py),
        # _process_mic_frame respects it; otherwise mic flows continuously.
        logger.warning(
            f"PL: mixer unavailable for line {line_id} sid={sid[:8]} "
            f"— falling back to direct RTP send"
        )
        client = next(
            (c for c in sip_engine.clients if c.line.line_id == line_id), None
        ) if sip_engine else None
        if client:
            def _mic_to_rtp_direct(pcm, _c=client):
                rtp = _c.rtp_stream
                if rtp is None:
                    return
                try:
                    rtp.send_audio(pcm)
                except Exception:
                    pass
            bridge.set_input_callback(_mic_to_rtp_direct)
        else:
            logger.error(
                f"PL: no SIP client found for line {line_id} — mic will be silent"
            )

    with _pl_lock:
        _pl_bridges[sid] = {'line_id': line_id, 'bridge': bridge, 'is_dialer': is_dialer}

    # Tell the client what role it has on this line.  This is the source of
    # truth — the browser sets plIsDialer=false optimistically when switching
    # lines, and only this event corrects it for someone returning to the
    # line they originally dialed.
    try:
        socketio.emit('pl_role', {'line': line_id, 'is_dialer': bool(is_dialer)}, to=sid)
    except Exception as ex:
        logger.debug(f"pl_role emit failed for sid={sid[:8]}: {ex}")

    bridge.start()
    logger.info(f"PL WebRTC bridge started: line {line_id} sid={sid[:8]} dialer={is_dialer}")


def _remove_pl_bridge(sid: str):
    """Close and remove PL bridge for a disconnected/leaving crew member."""
    with _pl_lock:
        entry = _pl_bridges.pop(sid, None)
    if entry:
        try:
            entry['bridge'].close()
        except Exception:
            pass
        # Pull this sid out of its line's mixer; if it was the last member on
        # the line, retire the mixer too so the tick thread exits.  A new
        # pl_join later will lazily recreate it.
        line_id = entry.get('line_id')
        if line_id is not None:
            with _pl_mixers_lock:
                mixer = _pl_mixers.get(line_id)
            if mixer:
                try:
                    mixer.remove_member(sid)
                except Exception:
                    pass
                if mixer.member_count() == 0:
                    _stop_pl_mixer(line_id)
            # Also clean up the conf mixer
            with _pl_conf_mixers_lock:
                conf_mixer = _pl_conf_mixers.get(line_id)
            if conf_mixer:
                try:
                    conf_mixer.remove_member(sid)
                except Exception:
                    pass
                if conf_mixer.member_count() == 0:
                    _stop_conf_mixer(line_id)
    with _pl_pending_lock:
        _pl_pending.pop(sid, None)


def _register_audio_monitor_callback():
    """Register _audio_monitor_callback on SIP clients 1-8 (USB lines)."""
    import time as _t
    while True:
        clients = getattr(sip_engine, 'clients', []) if sip_engine else []
        if clients:
            def _on_audio(line_id: int, pcm_data: bytes):
                # Fan-out to listen-only monitors (IFB / mobile monitor)
                with _monitor_lock:
                    monitors = [m for m in _monitor_sessions.values()
                                if m.line_id == line_id]
                for m in monitors:
                    try:
                        m.push_audio(pcm_data)
                    except Exception:
                        pass
                # Fan-out to two-way headset bridges
                with _headset_lock:
                    headsets = [b for b in _headset_sessions.values()
                                if b.line_id == line_id]
                for b in headsets:
                    try:
                        b.push_audio(pcm_data)
                    except Exception:
                        pass
            for client in clients:
                if getattr(client, 'line', None) and client.line.line_id <= 8:
                    client._audio_monitor_callback = _on_audio
            logger.info("Audio monitor callback registered on lines 1-8")
            break
        _t.sleep(0.5)


@socketio.on('monitor_subscribe')
def handle_monitor_subscribe(data):
    """Phone browser requests to listen to a USB line via WebRTC."""
    try:
        line_id = int((data or {}).get('line', 0))
        if line_id < 1 or line_id > 8:
            emit('monitor_error', {'error': 'Invalid line'})
            return
        mon = _get_or_create_monitor(line_id, request.sid)
        mon.start()
        logger.info(f"Audio monitor: sid={request.sid} subscribed to line {line_id}")
    except Exception as e:
        logger.error(f"monitor_subscribe error: {e}")
        emit('monitor_error', {'error': str(e)})


@socketio.on('monitor_answer')
def handle_monitor_answer(data):
    """Phone browser sends SDP answer for monitor WebRTC offer."""
    try:
        sdp = (data or {}).get('sdp')
        if not sdp:
            return
        with _monitor_lock:
            mon = _monitor_sessions.get(request.sid)
        if mon:
            mon.deliver_answer(sdp)
    except Exception as e:
        logger.error(f"monitor_answer error: {e}")


@socketio.on('monitor_ice_candidate')
def handle_monitor_ice_candidate(data):
    """Phone browser sends ICE candidate for monitor connection."""
    try:
        cand = (data or {}).get('candidate')
        if not cand:
            return
        with _monitor_lock:
            mon = _monitor_sessions.get(request.sid)
        if mon:
            mon.add_ice_candidate(cand)
    except Exception as e:
        logger.debug(f"monitor_ice_candidate error: {e}")


@socketio.on('monitor_unsubscribe')
def handle_monitor_unsubscribe(data):
    """Phone browser stops listening."""
    _remove_monitor(request.sid)
    logger.info(f"Audio monitor: sid={request.sid} unsubscribed")


# ============================================================================
# HEADSET BRIDGE — WebRTC two-way (mobile operator talks + listens on USB line)
# ============================================================================
# _headset_sessions: sid → WebRTCHeadsetBridge  (one per connected mobile operator)
_headset_sessions: dict = {}
_headset_lock = threading.Lock()


def _get_or_create_headset(line_id: int, sid: str):
    """Create a fresh headset bridge for line_id/sid, closing any existing one."""
    from smart_sip.audio_webrtc import WebRTCHeadsetBridge
    with _headset_lock:
        existing = _headset_sessions.get(sid)
        if existing:
            try:
                existing.close()
            except Exception:
                pass
        bridge = WebRTCHeadsetBridge(line_id, socketio, sid)
        _headset_sessions[sid] = bridge

    # Wire bridge mic → RTP TX of the target USB line
    if sip_engine:
        client = next(
            (c for c in sip_engine.clients if c.line.line_id == line_id), None
        )
        if client:
            def _mic_to_rtp(pcm, _c=client):
                rtp = _c.rtp_stream
                if rtp is None:
                    return
                try:
                    rtp.send_audio(pcm)
                except Exception:
                    pass
            bridge.set_input_callback(_mic_to_rtp)
        else:
            logger.warning(f"Headset bridge line {line_id}: no SIP client found — mic will be silent")

    # Wire RTP audio monitor → headset bridge speaker (reuse existing callback)
    # The _register_audio_monitor_callback already fans out to _monitor_sessions;
    # we piggy-back on the same callback path via _headset_audio_fan.
    return bridge


def _remove_headset(sid: str):
    """Close and remove headset bridge for a disconnected client."""
    with _headset_lock:
        bridge = _headset_sessions.pop(sid, None)
    if bridge:
        try:
            bridge.close()
        except Exception:
            pass


@socketio.on('headset_subscribe')
def handle_headset_subscribe(data):
    """Mobile operator requests two-way audio on a USB line."""
    try:
        line_id = int((data or {}).get('line', 0))
        if line_id < 1 or line_id > 8:
            emit('headset_error', {'error': 'Invalid line'})
            return
        bridge = _get_or_create_headset(line_id, request.sid)
        bridge.start()
        logger.info(f"Headset bridge: sid={request.sid} subscribed to line {line_id}")
    except Exception as e:
        logger.error(f"headset_subscribe error: {e}")
        emit('headset_error', {'error': str(e)})


@socketio.on('headset_answer')
def handle_headset_answer(data):
    """Mobile operator sends SDP answer for headset WebRTC offer."""
    try:
        sdp = (data or {}).get('sdp')
        if not sdp:
            return
        with _headset_lock:
            bridge = _headset_sessions.get(request.sid)
        if bridge:
            bridge.deliver_answer(sdp)
    except Exception as e:
        logger.error(f"headset_answer error: {e}")


@socketio.on('headset_ice_candidate')
def handle_headset_ice_candidate(data):
    """Mobile operator sends ICE candidate for headset connection."""
    try:
        cand = (data or {}).get('candidate')
        if not cand:
            return
        with _headset_lock:
            bridge = _headset_sessions.get(request.sid)
        if bridge:
            bridge.add_ice_candidate(cand)
    except Exception as e:
        logger.debug(f"headset_ice_candidate error: {e}")


@socketio.on('headset_unsubscribe')
def handle_headset_unsubscribe(data):
    """Mobile operator stops headset session."""
    _remove_headset(request.sid)
    logger.info(f"Headset bridge: sid={request.sid} unsubscribed")


# ============================================================================
# PARTY LINE (PL) — WebRTC socket events  (lines 29-35)
# ============================================================================

@socketio.on('pl_dial')
def handle_pl_dial(data):
    """Crew member dials a conference number on a specific PL line."""
    try:
        line_id = int((data or {}).get('line', 0))
        number  = ((data or {}).get('number') or '').strip()
        if not (PL_LINE_START <= line_id <= PL_LINE_END) or not number:
            emit('pl_error', {'error': 'Invalid line or number'})
            return
        if sip_engine is None:
            emit('pl_error', {'error': 'SIP engine not ready'})
            return
        # Only allow dial if line is currently idle
        status = sip_engine.get_line_status(line_id)
        if (status.get('state') or 'idle').lower() not in ('idle',):
            emit('pl_error', {'error': f'Line {line_id} is already in use'})
            return
        # Mark this sid as pending for this line
        with _pl_pending_lock:
            _pl_pending[request.sid] = line_id
        # Register this sid as the dialer for this line.  This survives the
        # bridge being torn down on pl_leave so the user keeps their "Hang Up"
        # button after switching to another PL line and back.  Cleared when
        # the SIP call actually ends (state → idle/error/busy).
        with _pl_lock:
            _pl_dialer_by_line[line_id] = request.sid
        logger.info(f"PL dial: sid={request.sid[:8]} line={line_id} → {number}")
        sid = request.sid
        def _do_dial():
            try:
                ok = sip_engine.make_call(line_id, number)
                if not ok:
                    # make_call returned False without raising — clean up pending
                    # and notify the client so the UI doesn't get stuck.
                    logger.error(f"PL dial: make_call returned False for line {line_id}")
                    with _pl_pending_lock:
                        _pl_pending.pop(sid, None)
                    # Roll back the dialer registration so a stale sid doesn't
                    # confuse pl_join in the rare case the line never moves to
                    # an error/idle state.
                    with _pl_lock:
                        if _pl_dialer_by_line.get(line_id) == sid:
                            _pl_dialer_by_line.pop(line_id, None)
                    socketio.emit('pl_error', {'error': 'Failed to start call on line ' + str(line_id)}, to=sid)
            except Exception as ex:
                logger.error(f"PL dial engine error: {ex}")
                with _pl_pending_lock:
                    _pl_pending.pop(sid, None)
                with _pl_lock:
                    if _pl_dialer_by_line.get(line_id) == sid:
                        _pl_dialer_by_line.pop(line_id, None)
                socketio.emit('pl_error', {'error': str(ex)}, to=sid)
        threading.Thread(target=_do_dial, daemon=True).start()
    except Exception as e:
        logger.error(f"pl_dial error: {e}")
        emit('pl_error', {'error': str(e)})


@socketio.on('pl_join')
def handle_pl_join(data):
    """Crew member joins an already-active PL line (listen + PTT)."""
    try:
        line_id = int((data or {}).get('line', 0))
        if not (PL_LINE_START <= line_id <= PL_LINE_END):
            emit('pl_error', {'error': 'Invalid line'})
            return
        if sip_engine is None:
            emit('pl_error', {'error': 'SIP engine not ready'})
            return
        status = sip_engine.get_line_status(line_id)
        state  = (status.get('state') or 'idle').lower()
        if state != 'connected':
            emit('pl_error', {'error': f'Line {line_id} is not active'})
            return
        # If this sid was the original dialer of this line they retain their
        # "Hang Up" rights — they're just rejoining their own call after a
        # detour to another line.
        with _pl_lock:
            is_dialer = (_pl_dialer_by_line.get(line_id) == request.sid)
        _start_pl_webrtc(line_id, request.sid, is_dialer=is_dialer)
        logger.info(f"PL join: sid={request.sid[:8]} line={line_id} dialer={is_dialer}")
    except Exception as e:
        logger.error(f"pl_join error: {e}")
        emit('pl_error', {'error': str(e)})


@socketio.on('pl_answer')
def handle_pl_answer(data):
    """Browser sends SDP answer for PL WebRTC offer."""
    try:
        sdp = (data or {}).get('sdp')
        if not sdp:
            return
        with _pl_lock:
            entry = _pl_bridges.get(request.sid)
        if entry:
            entry['bridge'].deliver_answer(sdp)
    except Exception as e:
        logger.error(f"pl_answer error: {e}")


@socketio.on('pl_ice_candidate')
def handle_pl_ice_candidate(data):
    """Browser sends ICE candidate for PL WebRTC."""
    try:
        cand = (data or {}).get('candidate')
        if not cand:
            return
        with _pl_lock:
            entry = _pl_bridges.get(request.sid)
        if entry:
            entry['bridge'].add_ice_candidate(cand)
    except Exception as e:
        logger.debug(f"pl_ice_candidate error: {e}")


@socketio.on('pl_ptt')
def handle_pl_ptt(data):
    """Crew member presses or releases the PTT button.

    Browser sends {active: bool} on every transition.  The server-side gate
    is what actually stops a non-talking member's silenced mic frames from
    racing the active talker's frames into RTP — see WebRTCPLBridge.set_ptt
    and PLLineMixer for the full path.
    """
    try:
        active = bool((data or {}).get('active', False))
        with _pl_lock:
            entry = _pl_bridges.get(request.sid)
        logger.info(f"pl_ptt: sid={request.sid[:8]} active={active} bridge={'yes' if entry else 'NO'}")
        if entry:
            bridge = entry['bridge']
            set_ptt = getattr(bridge, 'set_ptt', None)
            if callable(set_ptt):
                try:
                    set_ptt(active)
                except Exception as ex:
                    logger.warning(f"pl_ptt set_ptt error: {ex}")
            else:
                # Old WebRTCPLBridge without server-side PTT gate — log once so
                # the operator knows audio_webrtc.py needs redeploying.  Audio
                # will still flow (the direct-send fallback in _start_pl_webrtc
                # doesn't depend on this gate), just without silence-frame
                # suppression — same as the pre-fix behaviour.
                if not getattr(handle_pl_ptt, '_warned_no_set_ptt', False):
                    logger.warning(
                        "pl_ptt: bridge has no set_ptt() — audio_webrtc.py "
                        "on the Pi appears older than app.py.  Redeploy "
                        "smart_sip/audio_webrtc.py to enable PTT gating."
                    )
                    handle_pl_ptt._warned_no_set_ptt = True
        # If there's no bridge for this sid yet (PTT pressed before negotiation
        # completes), silently ignore — the user can press again once joined.
    except Exception as e:
        logger.debug(f"pl_ptt error: {e}")


@socketio.on('pl_leave')
def handle_pl_leave(data):
    """Crew member leaves a PL line without hanging up the call."""
    _remove_pl_bridge(request.sid)
    logger.info(f"PL leave: sid={request.sid[:8]}")


@socketio.on('pl_hangup')
def handle_pl_hangup(data):
    """Crew member (dialer) hangs up the PL line — ends the SIP call."""
    try:
        line_id = int((data or {}).get('line', 0))
        _remove_pl_bridge(request.sid)
        if sip_engine and PL_LINE_START <= line_id <= PL_LINE_END:
            sip_engine.hangup_call(line_id)
        logger.info(f"PL hangup: sid={request.sid[:8]} line={line_id}")
    except Exception as e:
        logger.error(f"pl_hangup error: {e}")


# ============================================================================
# BROWSER PHONE — WebRTC signaling socket events
# ============================================================================

@socketio.on('phone_register')
def handle_phone_register(data):
    """Browser requests a phone line. Returns {token, line_id}."""
    try:
        from smart_sip import browser_lines as _bl
        saved_token = (data or {}).get('token')
        result = _bl.register(token=saved_token)
        if 'error' in result:
            logger.warning(f"Browser phone rejected — {result['error']}")
            emit('phone_registered', {'error': result['error'], 'max_lines': result.get('max_lines', 20)})
            return
        _bl.set_session(result['token'], request.sid)
        line_id = result['line_id']
        logger.info(f"Browser phone registered: line {line_id} sid={request.sid} new={result['is_new']}")
        emit('phone_registered', {'token': result['token'], 'line_id': line_id})

        # If the line is already connected (e.g. page reload mid-call), notify immediately
        if sip_engine:
            try:
                status = sip_engine.get_line_status(line_id)
                state = status.get('state', '').lower()
                if state in ('connected', 'active'):
                    number = status.get('phone_number', '')
                    logger.info(f"Browser phone re-registered on active line {line_id} — sending phone_call_connected")
                    emit('phone_call_connected', {'token': result['token'], 'number': number})
            except Exception as se:
                logger.debug(f"phone_register state check error: {se}")
    except Exception as e:
        logger.error(f"phone_register error: {e}")
        emit('phone_registered', {'error': str(e)})


@socketio.on('phone_answer')
def handle_phone_answer(data):
    """Browser sends SDP answer back to our aiortc offer."""
    try:
        from smart_sip import browser_lines as _bl
        token = (data or {}).get('token')
        sdp   = (data or {}).get('sdp')
        if not token or not sdp:
            return
        entry = _bl.get_entry(token)
        if entry and 'webrtc' in entry:
            # Deliver answer to the async WebRTC loop
            loop = entry.get('loop')
            answer_future = entry.get('answer_future')
            if loop and answer_future:
                loop.call_soon_threadsafe(answer_future.set_result, sdp)
    except Exception as e:
        logger.error(f"phone_answer error: {e}")


@socketio.on('phone_ice_candidate')
def handle_phone_ice_candidate(data):
    """Browser sends an ICE candidate."""
    try:
        from smart_sip import browser_lines as _bl
        token = (data or {}).get('token')
        cand  = (data or {}).get('candidate')
        if not token or not cand:
            return
        entry = _bl.get_entry(token)
        if entry:
            ice_queue = entry.get('ice_queue')
            if ice_queue is not None:
                ice_queue.put_nowait(cand)
    except Exception as e:
        logger.debug(f"phone_ice_candidate error: {e}")


@socketio.on('phone_dial')
def handle_phone_dial(data):
    """Browser requests an outgoing call."""
    try:
        from smart_sip import browser_lines as _bl
        token  = (data or {}).get('token')
        number = (data or {}).get('number', '').strip()
        if not token or not number:
            return
        line_id = _bl.get_line_id(token)
        if line_id is None:
            emit('phone_call_failed', {'token': token, 'reason': 'No line assigned'})
            return
        logger.info(f"Browser phone dial: line {line_id} → {number}")
        # Delegate to SIP engine (non-blocking)
        sid = request.sid
        def _do_dial():
            try:
                sip_engine.make_call(line_id, number)
            except Exception as ex:
                logger.error(f"phone_dial engine error: {ex}")
                socketio.emit('phone_call_failed', {'token': token, 'reason': str(ex)}, to=sid)
        threading.Thread(target=_do_dial, daemon=True).start()
    except Exception as e:
        logger.error(f"phone_dial error: {e}")


@socketio.on('phone_hangup')
def handle_phone_hangup(data):
    """Browser requests hangup."""
    try:
        from smart_sip import browser_lines as _bl
        token = (data or {}).get('token')
        if not token:
            return
        line_id = _bl.get_line_id(token)
        if line_id is not None:
            logger.info(f"Browser phone hangup: line {line_id}")
            try:
                sip_engine.hangup_call(line_id)
            except Exception as ex:
                logger.debug(f"phone_hangup engine error: {ex}")
    except Exception as e:
        logger.error(f"phone_hangup error: {e}")


@socketio.on('disconnect')
def handle_phone_disconnect_browser():
    """On any disconnect: release browser line if this sid owned one."""
    try:
        from smart_sip import browser_lines as _bl
        _bl.disconnect_by_session(request.sid)
    except Exception:
        pass
    # Clean up any audio monitor session
    _remove_monitor(request.sid)
    # Clean up any headset bridge session
    _remove_headset(request.sid)
    # Clean up any PL bridge session
    _remove_pl_bridge(request.sid)


# ============================================================================
# BACKGROUND TASKS
# ============================================================================
# NOTE: This background task pattern works for single-worker deployments.
# For multi-worker setups (gunicorn -w N), consider:
#   1) Start background task only in a "leader" process (check env var)
#   2) Move SIP status emission to a separate service
#   3) Use message queue (Redis) + proper SocketIO room/namespace pattern
# ============================================================================

def emit_sip_status_periodically():
    """Background task to emit SIP status every 2 seconds using Flask-SocketIO"""
    # Give registration time to complete before first emission (avoid showing 8/8 before it's true)
    socketio.sleep(3)
    while True:
        try:
            socketio.sleep(2)  # Use socketio.sleep instead of time.sleep
            
            payload, _ = _build_sip_status_payload()
            if payload is None:
                continue
            
            socketio.emit('sip_status', payload)
            logger.debug(f"Emitted sip_status: {payload['registered']}/8 lines registered, server reachable: {payload['connected']}")
            
        except Exception as e:
            logger.error(f"Error in SIP status background task: {e}")
            traceback.print_exc()


# ============================================================================
# MAIN
# ============================================================================

if __name__ == '__main__':
    # Register cleanup handler for PID file (atexit runs on normal exit)
    atexit.register(release_lock)
    
    # Acquire exclusive lock (prevent duplicate instances)
    if not acquire_lock():
        logger.error("=" * 70)
        logger.error("FATAL: Another instance of ProComm is already running!")
        logger.error("To stop the other instance, run:")
        logger.error("  sudo systemctl stop procomm-app")
        logger.error("  OR")
        logger.error("  pkill -f 'python.*app.py'")
        logger.error("=" * 70)
        sys.exit(1)
    
    # Initialize phone system
    if not init_phone_system():
        logger.error("Failed to initialize phone system - exiting")
        release_lock()
        exit(1)
    
    # Start background task for SIP status emission using Flask-SocketIO (only once)
    if not sip_status_task_started:
        socketio.start_background_task(emit_sip_status_periodically)
        sip_status_task_started = True
        logger.info("Started SIP status background task")
    
    # Run Flask app with SocketIO
    # NOTE: Don't register signal handlers — socketio.run() handles SIGINT/SIGTERM
    # gracefully with its threading mode. Custom handlers break WebSocket connections.
    #
    # ── Dual-port setup ──────────────────────────────────────────────────
    # Port 5000 = plain HTTP  — for kiosk touchscreen and operator web UI
    # Port 5443 = HTTPS + WSS — for browser softphone (iOS Safari requires
    #                           HTTPS for getUserMedia / microphone access,
    #                           and wss:// for SocketIO from an HTTPS page)
    # Both use the same Flask+SocketIO instance — shared state.
    cert_dir  = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'certs')
    cert_file = os.path.join(cert_dir, 'cert.pem')
    key_file  = os.path.join(cert_dir, 'key.pem')

    if os.path.exists(cert_file) and os.path.exists(key_file):
        # Run HTTPS+WSS SocketIO on port 5443 in a background thread
        def _run_https_socketio():
            try:
                logger.info("HTTPS+WSS SocketIO listening on https://0.0.0.0:5443 (browser phone)")
                socketio.run(
                    app,
                    host='0.0.0.0',
                    port=5443,
                    debug=False,
                    use_reloader=False,
                    allow_unsafe_werkzeug=True,
                    ssl_context=(cert_file, key_file),
                )
            except Exception as ex:
                logger.error(f"HTTPS SocketIO server failed: {ex}")
        threading.Thread(target=_run_https_socketio, daemon=True, name='https-socketio-5443').start()
    else:
        logger.warning("No TLS cert in ./certs/ — HTTPS:5443 disabled (iPhone mic will not work)")

    logger.info("Starting web server on http://0.0.0.0:5000")
    try:
        socketio.run(
            app, 
            host='0.0.0.0', 
            port=5000, 
            debug=False,              # No debug mode
            use_reloader=False,       # No auto-reloader (prevents duplicate processes)
            allow_unsafe_werkzeug=True
        )
    finally:
        release_lock()

