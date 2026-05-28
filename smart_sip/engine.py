"""
Smart SIP Engine - Main Engine Class
Pure Python SIP/RTP implementation for 8-line phone system
"""

import socket
import ssl
import threading
import time
import json
import os
import re
import urllib.request
from typing import Optional, Dict, List, Callable
import logging

from .line import Line, LineState
from .events import EventEmitter, EventType, Event
from .protocol import SIPParser, SIPBuilder, SIPMessage, DigestAuth, parse_sdp
from .rtp import RTPStream, PAYLOAD_PCMU, PAYLOAD_PCMA, PAYLOAD_G722
from .audio_usb_dongles import USBDongleAudioManager
from .stun import get_mapped_address_try_servers

logger = logging.getLogger(__name__)

def get_local_ip() -> str:
    """Auto-detect local IP address by connecting to external server"""
    try:
        # Create a socket and connect to external server (doesn't actually send data)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        return local_ip
    except Exception as e:
        logger.warning(f"Could not auto-detect IP: {e}, using 127.0.0.1")
        return "127.0.0.1"



class SIPClient:
    """
    Handles SIP signaling for one line.
    Manages registration, call setup, and teardown.
    """
    
    def __init__(self, line: Line, server_ip: str, server_port: int,
                 local_ip: str, local_port: int, username: str, password: str,
                 domain: str):
        self.line = line
        self.server_ip = server_ip
        self.server_port = server_port
        self.local_ip = local_ip
        self.local_port = local_port
        self.username = username
        self.password = password
        self.domain = domain
        
        # SIP socket
        self.socket: Optional[socket.socket] = None
        self._use_tls: bool = (server_port == 5061)
        
        # SIP state
        self._register_call_id = ""
        self._register_tag = ""
        self._register_cseq = 0
        self._invite_cseq = 0
        self._invite_in_progress = False  # Prevent overlapping INVITE; clearable from any thread (timeout)
        self._invite_auth_attempts = 0  # Track auth attempts to prevent infinite loops
        self._invite_timeout_timer: Optional[threading.Timer] = None  # INVITE no-response timeout (32s)
        self._last_auth_cseq = 0  # CSeq of last 407/401 we processed (ignore retransmissions)
        
        # Remote Contact URI and Route from 200 OK (for BYE routing per RFC 3261)
        self._remote_contact_uri: Optional[str] = None
        self._record_route: Optional[str] = None
        
        # Session timer (RFC 4028)
        self._session_timer: Optional[threading.Timer] = None
        self._session_expires: int = 0
        
        # Auto-reset from ERROR to IDLE after a delay so the line does not stay stuck
        self._error_reset_timer: Optional[threading.Timer] = None
        
        # Message builder
        self.builder: Optional[SIPBuilder] = None
        
        # Current invite message (for ACK)
        self._current_invite: Optional[SIPMessage] = None
        
        # Incoming invite message (for answering)
        self._incoming_invite: Optional[SIPMessage] = None
        
        # RTP
        self.rtp_stream: Optional[RTPStream] = None
        self._rtp_port = 0
        
        # Store for re-INVITE with auth
        self._current_phone_number: Optional[str] = None
        self._current_caller_id: Optional[str] = None
        
        # Callbacks
        self.on_incoming_call: Optional[Callable[[str, str], None]] = None
        self.on_call_answered: Optional[Callable[[str, int, str], None]] = None
        self.on_call_ended: Optional[Callable[[], None]] = None
        self.on_registered: Optional[Callable[[bool], None]] = None

        # Optional monitor callback: fn(line_id, pcm_data) — called for every
        # incoming RTP audio packet. Used to forward audio to phone browsers.
        self._audio_monitor_callback: Optional[Callable[[int, bytes], None]] = None

        # Audio manager reference (set by SIPEngine after creation)
        self.audio = None
    
    def start(self, rtp_base_port: int) -> bool:
        """Initialize SIP client"""
        try:
            if self._use_tls:
                # TLS transport: TCP socket wrapped with SSL (port 5061)
                raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                raw_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                raw_sock.settimeout(10)
                raw_sock.connect((self.server_ip, self.server_port))
                ssl_ctx = ssl.create_default_context()
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
                self.socket = ssl_ctx.wrap_socket(raw_sock, server_hostname=self.server_ip)
                self.socket.settimeout(0.5)
                logger.info(f"SIP client {self.line.line_id}: TLS connected to {self.server_ip}:{self.server_port}")
            else:
                # UDP transport (port 5060)
                self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.socket.bind((self.local_ip, self.local_port))
                self.socket.settimeout(0.5)
            
            self._rtp_port = rtp_base_port + (self.line.line_id * 2)
            
            # STUN: discover public (IP, port) so remote can reach us without port forwarding
            # STUN only works on UDP sockets; skip for TLS connections
            # But if use_local_ip_in_sdp is set, we want to use local IP for SDP (NAT will handle it)
            use_local_for_sdp = getattr(self, "use_local_ip_in_sdp", False)
            if not self._use_tls and getattr(self, "use_stun", False) and getattr(self, "advertise_port", None) is None:
                stun_result = get_mapped_address_try_servers(self.socket)
                if stun_result:
                    if not use_local_for_sdp:
                        self.advertise_ip = stun_result[0]
                        self.advertise_port = stun_result[1]
                        logger.info(f"Line {self.line.line_id}: STUN mapped {self.local_ip}:{self.local_port} -> {self.advertise_ip}:{self.advertise_port}")
                    else:
                        # STUN successful but not using for SDP (use_local_ip_in_sdp=true)
                        logger.info(f"Line {self.line.line_id}: STUN mapped {self.local_ip}:{self.local_port} -> {stun_result[0]}:{stun_result[1]} (not used for SDP)")
            
            advertise_ip = getattr(self, "advertise_ip", None) or self.local_ip
            advertise_port = getattr(self, "advertise_port", None)  # from STUN or None -> builder uses local_port
            self.builder = SIPBuilder(
                self.local_ip, self.local_port,
                self.server_ip, self.server_port,
                advertise_ip=advertise_ip,
                advertise_port=advertise_port,
                transport="TLS" if self._use_tls else "UDP"
            )
            
            self._register_call_id = SIPBuilder.generate_call_id()
            self._register_tag = SIPBuilder.generate_tag()
            
            logger.info(f"SIP client {self.line.line_id} started ({'TLS' if self._use_tls else 'UDP'} :{self.local_port if not self._use_tls else self.server_port})")
            return True
            
        except Exception as e:
            logger.error(f"Failed to start SIP client: {e}")
            return False

    def _select_audio_codec(self, remote_codecs: Optional[List[int]]) -> int:
        """
        Select the codec to use for RECEIVING (decoding incoming RTP).

        We always SEND as PCMU (hardcoded in RTPStream.send_codec).
        For receive, we prefer G.722 (wideband) > PCMU > PCMA.
        The SDP answer/offer lists PCMU first so the provider knows
        that is our sending codec, while G.722 is listed as receivable.
        """
        supported = [PAYLOAD_G722, PAYLOAD_PCMU, PAYLOAD_PCMA]  # receive preference: 9, 0, 8
        if not remote_codecs:
            return PAYLOAD_PCMU  # Default to PCMU if no negotiation info
        for pt in supported:
            if pt in remote_codecs:
                return pt
        return PAYLOAD_PCMU  # Fallback

    @staticmethod
    def _codec_name(pt: int) -> str:
        return {PAYLOAD_PCMU: "PCMU", PAYLOAD_PCMA: "PCMA", PAYLOAD_G722: "G722"}.get(pt, str(pt))
    
    def _setup_rtp_stream(self, local_port: int, remote_ip: str, remote_port: int, codec: int, label: str = ""):
        """Create and start an RTP stream with audio routing and network monitoring.
        
        Consolidates the duplicated RTP setup logic used in answer(), 183 early media,
        and 200 OK handler into a single method.
        """
        self.rtp_stream = RTPStream(
            local_port=local_port,
            remote_ip=remote_ip,
            remote_port=remote_port,
            codec=codec
        )
        
        # Route received RTP audio to audio output queue via the audio manager
        if hasattr(self, 'audio') and self.audio is not None:
            audio_count = [0]
            line_id = self.line.line_id
            audio_mgr = self.audio  # reference to USBDongleAudioManager
            def route_received_audio(pcm_data):
                try:
                    audio_mgr.send_audio(line_id, pcm_data)
                    audio_count[0] += 1
                    if audio_count[0] % 50 == 0:
                        logger.info(f"🔊 Line {line_id}: [{label or 'RTP'}] Routed {audio_count[0]} audio packets")
                except Exception as e:
                    if audio_count[0] == 0:
                        logger.warning(f"⚠️ Line {line_id}: [{label or 'RTP'}] Audio routing error: {e}")
                # Forward to phone browser monitors (listen-only, non-blocking)
                try:
                    if self._audio_monitor_callback:
                        self._audio_monitor_callback(line_id, pcm_data)
                except Exception:
                    pass
            
            self.rtp_stream.on_audio_received = route_received_audio
            logger.info(f"Line {line_id}: [{label or 'RTP'}] Audio callback registered via audio manager")
        
        # Network state monitoring — log only, never touch the SIP dialog.
        # Sending a re-INVITE while the network is down causes Telnyx to reply
        # with 481 Call Does Not Exist → call drops.  RTP resumes naturally
        # when packets flow again without any re-INVITE needed.
        def on_network_state_changed(is_up: bool):
            if is_up:
                logger.info(f"Line {self.line.line_id}: RTP network recovered — packets flowing again")
            else:
                logger.warning(f"Line {self.line.line_id}: RTP network down — keeping call alive, waiting for packets")
        
        self.rtp_stream.on_network_state_changed = on_network_state_changed
        
        self.rtp_stream.start()
        logger.info(f"Line {self.line.line_id}: [{label or 'RTP'}] Stream started on port {local_port} to {remote_ip}:{remote_port} codec={self._codec_name(codec)}")
    
    def _cancel_invite_timeout(self):
        """Cancel INVITE timeout timer if running"""
        if self._invite_timeout_timer:
            self._invite_timeout_timer.cancel()
            self._invite_timeout_timer = None

    def stop(self):
        """Stop SIP client"""
        self._cancel_invite_timeout()
        if self._error_reset_timer:
            self._error_reset_timer.cancel()
            self._error_reset_timer = None
        if self.rtp_stream:
            self.rtp_stream.stop()
            self.rtp_stream = None
        
        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass
            self.socket = None
    
    def _reconnect_tls(self) -> bool:
        """Re-establish a dropped TLS connection. Returns True on success."""
        try:
            if self.socket:
                try:
                    self.socket.close()
                except Exception:
                    pass
                self.socket = None
            
            raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            raw_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            raw_sock.settimeout(10)
            raw_sock.connect((self.server_ip, self.server_port))
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
            self.socket = ssl_ctx.wrap_socket(raw_sock, server_hostname=self.server_ip)
            self.socket.settimeout(0.5)
            logger.info(f"Line {self.line.line_id}: TLS reconnected to {self.server_ip}:{self.server_port}")
            return True
        except Exception as e:
            logger.error(f"Line {self.line.line_id}: TLS reconnect failed: {e}")
            return False
    
    def register(self, expires: int = 300) -> bool:
        """Send REGISTER request"""
        self._register_cseq += 1
        
        msg = self.builder.build_register(
            username=self.username,
            domain=self.domain,
            call_id=self._register_call_id,
            cseq=self._register_cseq,
            from_tag=self._register_tag,
            expires=expires
        )
        
        # Debug: log the REGISTER message
        logger.info(f"Line {self.line.line_id}: Sending REGISTER:\n{msg[:500]}")
        
        return self._send(msg)
    
    def register_with_auth(self, www_auth: str, expires: int = 300, auth_type: int = 401) -> bool:
        """Send REGISTER with authentication
        
        Args:
            auth_type: 401 for Authorization header, 407 for Proxy-Authorization header
        """
        self._register_cseq += 1
        
        # Parse challenge
        challenge = DigestAuth.parse_challenge(www_auth)
        realm = challenge.get('realm', self.domain)
        nonce = challenge.get('nonce', '')
        qop = challenge.get('qop', None)
        algorithm = challenge.get('algorithm', 'MD5')
        opaque = challenge.get('opaque', None)
        
        # Build auth response
        uri = f"sip:{self.domain}"
        auth_header = DigestAuth.build_response(
            username=self.username,
            password=self.password,
            realm=realm,
            nonce=nonce,
            uri=uri,
            method="REGISTER",
            algorithm=algorithm,
            qop=qop,
            opaque=opaque
        )
        
        msg = self.builder.build_register(
            username=self.username,
            domain=self.domain,
            call_id=self._register_call_id,
            cseq=self._register_cseq,
            from_tag=self._register_tag,
            expires=expires,
            auth_header=auth_header,
            auth_type=auth_type
        )
        
        return self._send(msg)
    
    def invite(self, phone_number: str, caller_id: str = None) -> bool:
        """Send INVITE to make a call"""
        logger.info(f"Line {self.line.line_id}: invite() called for {phone_number}")
        
        if self._invite_in_progress:
            logger.warning(f"Line {self.line.line_id}: INVITE already in progress, ignoring duplicate request")
            return False
        
        if not self.line.can_dial:
            logger.warning(f"Line {self.line.line_id} cannot dial")
            return False
        
        self._invite_in_progress = True
        self._invite_auth_attempts = 0
        self._last_auth_cseq = 0
        
        call_id = SIPBuilder.generate_call_id()
        self.line.start_outgoing_call(phone_number, call_id)
        
        self._invite_cseq += 1
        self.line.local_tag = SIPBuilder.generate_tag()
        self.line.call_id = call_id
        
        msg_text = self.builder.build_invite(
            from_user=self.username,
            to_number=phone_number,
            domain=self.domain,
            call_id=call_id,
            cseq=self._invite_cseq,
            from_tag=self.line.local_tag,
            local_rtp_port=self._rtp_port,
            caller_id=caller_id
        )
        
        # Log the INVITE for debugging
        logger.info(f"Line {self.line.line_id}: Sending INVITE:\n{msg_text[:600]}")
        
        # Store for ACK and for re-INVITE with auth
        self._current_invite = SIPParser.parse(msg_text.encode())
        if self._current_invite:
            self._current_invite.uri = f"sip:{phone_number}@{self.domain}"
        self._current_phone_number = phone_number
        self._current_caller_id = caller_id
        
        # INVITE timeout: if no final response in 32s, treat as failure so line becomes usable again
        self._cancel_invite_timeout()
        def on_invite_timeout():
            self._invite_timeout_timer = None
            if not self._invite_in_progress:
                return
            logger.warning(f"Line {self.line.line_id}: INVITE timeout (no response), resetting line")
            self._invite_in_progress = False
            self.line.on_error("Timeout")
            if self.on_call_ended:
                self.on_call_ended()
            self._schedule_error_reset()
        self._invite_timeout_timer = threading.Timer(32.0, on_invite_timeout)
        self._invite_timeout_timer.daemon = True
        self._invite_timeout_timer.start()
        
        if not self._send(msg_text):
            self._cancel_invite_timeout()
            self._invite_in_progress = False
            return False
        return True
    
    def _invite_with_auth(self, www_auth: str, auth_type: int = 407) -> bool:
        """Re-send INVITE with authentication
        
        Args:
            www_auth: The WWW-Authenticate or Proxy-Authenticate header value
            auth_type: 401 for Authorization header, 407 for Proxy-Authorization header
        """
        logger.info(f"Line {self.line.line_id}: _invite_with_auth() called (auth_type={auth_type})")
        
        if not self._current_phone_number:
            return False
        
        # Check if we've tried auth too many times (prevent infinite loop)
        self._invite_auth_attempts += 1
        logger.info(f"Line {self.line.line_id}: Auth attempt #{self._invite_auth_attempts}")
        if self._invite_auth_attempts > 2:
            logger.warning(f"Line {self.line.line_id}: Too many auth attempts ({self._invite_auth_attempts}), stopping retransmissions")
            # Don't send more auth attempts, but don't error out - 
            # the successful response may already be in flight
            return False
        
        self._invite_cseq += 1
        
        # Parse challenge
        challenge = DigestAuth.parse_challenge(www_auth)
        realm = challenge.get('realm', self.domain)
        nonce = challenge.get('nonce', '')
        qop = challenge.get('qop', None)
        algorithm = challenge.get('algorithm', 'MD5')
        opaque = challenge.get('opaque', None)  # Telnyx requires this!
        
        logger.info(f"Line {self.line.line_id}: Parsed auth - realm={realm}, qop={qop}, algorithm={algorithm}, nonce={nonce[:20]}...")
        
        # Build auth response
        # CRITICAL: auth URI must match the request URI EXACTLY (including + if present)
        uri = f"sip:{self._current_phone_number}@{self.domain}"
        logger.info(f"Line {self.line.line_id}: Auth URI: {uri}")
        auth_header = DigestAuth.build_response(
            username=self.username,
            password=self.password,
            realm=realm,
            nonce=nonce,
            uri=uri,
            method="INVITE",
            algorithm=algorithm,
            qop=qop,
            opaque=opaque
        )
        
        msg_text = self.builder.build_invite(
            from_user=self.username,
            to_number=self._current_phone_number,
            domain=self.domain,
            call_id=self.line.call_id,
            cseq=self._invite_cseq,
            from_tag=self.line.local_tag,
            local_rtp_port=self._rtp_port,
            caller_id=self._current_caller_id,
            auth_header=auth_header,
            auth_type=auth_type
        )
        
        logger.info(f"Line {self.line.line_id}: Sending authenticated INVITE with header: {auth_header[:150]}")
        # Debug: log the full INVITE message for Telnyx troubleshooting
        logger.info(f"Line {self.line.line_id}: Full authenticated INVITE:\n{msg_text[:800]}")
        
        # Update stored invite for ACK
        self._current_invite = SIPParser.parse(msg_text.encode())
        if self._current_invite:
            self._current_invite.uri = uri
        
        return self._send(msg_text)
    
    def _send_reinvite(self) -> bool:
        """Send re-INVITE to refresh media session (for network recovery)"""
        logger.info(f"Line {self.line.line_id}: Sending re-INVITE for media recovery")
        
        if not self.line.call_info or not self.line.call_id:
            logger.warning(f"Line {self.line.line_id}: Cannot send re-INVITE - no active call")
            return False
        
        # Increment CSeq for re-INVITE
        self._invite_cseq += 1
        
        # Build re-INVITE with same call-id and tags, new CSeq
        # Must include Route from the original 200 OK dialog (RFC 3261 §12.2)
        # Without this, Telnyx rejects the re-INVITE with 403 Forbidden P01
        phone_number = self.line.call_info.phone_number
        msg_text = self.builder.build_invite(
            from_user=self.username,
            to_number=phone_number,
            domain=self.domain,
            call_id=self.line.call_id,
            cseq=self._invite_cseq,
            from_tag=self.line.local_tag,
            to_tag=self.line.remote_tag,  # Include remote tag for re-INVITE
            local_rtp_port=self._rtp_port,
            caller_id=self._current_caller_id,
            route=self._record_route       # Dialog route set — required for mid-dialog requests
        )
        
        logger.info(f"Line {self.line.line_id}: re-INVITE sent with CSeq {self._invite_cseq}")
        return self._send(msg_text)
    
    def hangup(self) -> bool:
        """Send CANCEL (if dialing/ringing), 603 Decline (if incoming), or BYE (if connected) to end call"""
        if self.line.state in (LineState.DIALING, LineState.RINGING):
            # No dialog yet - abort the INVITE with CANCEL
            self.send_cancel()
        elif self.line.state == LineState.INCOMING:
            # Incoming call not yet answered - reject with 603 Decline
            if self._incoming_invite:
                try:
                    response = self.builder.build_response(603, "Decline", self._incoming_invite)
                    self._send(response)
                    logger.info(f"Line {self.line.line_id}: Sent 603 Decline for incoming call")
                except Exception as e:
                    logger.warning(f"Line {self.line.line_id}: Error sending 603 Decline: {e}")
                self._incoming_invite = None
            else:
                logger.warning(f"Line {self.line.line_id}: No stored INVITE to decline")
        elif self.line.call_info and self.line.call_info.phone_number and self.line.call_id:
            # Dialog established - send BYE
            try:
                self._invite_cseq += 1
                to_header = f"<sip:{self.line.call_info.phone_number}@{self.domain}>"
                if self.line.remote_tag:
                    to_header += f";tag={self.line.remote_tag}"
                
                # From header MUST match the INVITE's From header (RFC 3261 dialog matching)
                # INVITE uses SIP username in URI, caller_id only as display name
                display_name = self._current_caller_id if self._current_caller_id else self.username
                from_header = f"\"{display_name}\" <sip:{self.username}@{self.domain}>;tag={self.line.local_tag}"
                
                # Per RFC 3261: BYE must be sent to the Contact URI from 200 OK (dialog routing)
                # Fall back to original Request-URI if no Contact was stored
                bye_uri = self._remote_contact_uri if self._remote_contact_uri else f"sip:{self.line.call_info.phone_number}@{self.domain}"
                
                # Build our Contact header (same as in INVITE)
                our_contact = f"<sip:{self.username}@{self.builder._sip_host}:{self.builder._sip_port}>"
                
                logger.info(f"Line {self.line.line_id}: Sending BYE to {bye_uri}")
                
                msg = self.builder.build_bye(
                    call_id=self.line.call_id,
                    from_header=from_header,
                    to_header=to_header,
                    cseq=self._invite_cseq,
                    to_uri=bye_uri,
                    contact=our_contact,
                    route=self._record_route  # Add Route header from Record-Route
                )
                
                # Log full BYE for debugging
                logger.info(f"Line {self.line.line_id}: BYE message:\n{msg}")
                
                self._send(msg)
                
                # Clear remote contact URI and route
                self._remote_contact_uri = None
                self._record_route = None
            except Exception as e:
                logger.warning(f"Line {self.line.line_id}: Error sending BYE: {e}")
        
        # Cancel session timer
        self._stop_session_timer()
        
        # Always cleanup RTP
        if self.rtp_stream:
            self.rtp_stream.stop()
            self.rtp_stream = None
        # Reset "no RTP stream" log flag so the warning fires again on the next call
        self._logged_no_rtp = False
        
        # Flush stale audio from output queue to prevent old call audio leaking into next call
        if hasattr(self, 'audio') and self.audio is not None:
            try:
                self.audio.flush_output(self.line.line_id)
            except Exception as e:
                logger.warning(f"Line {self.line.line_id}: Error flushing audio: {e}")
        
        # Clear stale dialog state
        self._current_invite = None
        self._current_phone_number = None
        self._current_caller_id = None
        
        # CRITICAL FIX: Transition line state back to IDLE
        self.line.on_hangup()
        
        return True
    
    def send_cancel(self) -> bool:
        """Send CANCEL to abort in-progress INVITE (stops remote ringing).
        
        Per RFC 3261: CANCEL must use the same CSeq number as the INVITE.
        """
        if not self._current_invite or not self.line.call_id:
            return False
        try:
            # CANCEL uses the SAME CSeq as the INVITE (not incremented)
            cancel_msg = self.builder.build_cancel(self._current_invite, self._invite_cseq)
            self._send(cancel_msg)
            logger.info(f"Line {self.line.line_id}: Sent CANCEL to stop remote ringing")
            self._cancel_invite_timeout()
            self._invite_in_progress = False
            return True
        except Exception as e:
            logger.warning(f"Line {self.line.line_id}: Error sending CANCEL: {e}")
            return False
    
    def _start_session_timer(self, interval: int):
        """Start session refresh timer (RFC 4028)"""
        self._stop_session_timer()
        
        logger.info(f"Line {self.line.line_id}: Starting session timer, refresh in {interval}s")
        self._session_timer = threading.Timer(interval, self._refresh_session)
        self._session_timer.daemon = True
        self._session_timer.start()
    
    def _stop_session_timer(self):
        """Stop session refresh timer"""
        if self._session_timer:
            self._session_timer.cancel()
            self._session_timer = None
    
    def _refresh_session(self):
        """Send UPDATE or re-INVITE to refresh session"""
        if self.line.state != LineState.CONNECTED:
            logger.debug(f"Line {self.line.line_id}: Not refreshing - not connected")
            return
        
        logger.info(f"Line {self.line.line_id}: Refreshing session with UPDATE")
        
        try:
            self._invite_cseq += 1
            
            # Build UPDATE message (simpler than re-INVITE, no SDP change)
            to_header = f"<sip:{self.line.call_info.phone_number}@{self.domain}>"
            if self.line.remote_tag:
                to_header += f";tag={self.line.remote_tag}"
            
            from_header = f"<sip:{self.username}@{self.domain}>;tag={self.line.local_tag}"
            
            # Build UPDATE request
            uri = f"sip:{self.line.call_info.phone_number}@{self.domain}"
            branch = SIPBuilder.generate_branch()
            
            headers = {
                "Via": f"SIP/2.0/{self.builder.transport} {self.builder._sip_host}:{self.builder._sip_port};rport;branch={branch}",
                "From": from_header,
                "To": to_header,
                "Call-ID": self.line.call_id,
                "CSeq": f"{self._invite_cseq} UPDATE",
                "Contact": f"<sip:{self.username}@{self.builder._sip_host}:{self.builder._sip_port}>",
                "Supported": "timer",
                "Session-Expires": f"{self._session_expires};refresher=uac",
                "Max-Forwards": "70",
                "User-Agent": "SmartSIP/1.0"
            }
            
            msg = self.builder.build_request("UPDATE", uri, headers)
            self._send(msg)
            
            # Restart timer for next refresh
            refresh_interval = self._session_expires // 2
            self._start_session_timer(refresh_interval)
            
        except Exception as e:
            logger.error(f"Line {self.line.line_id}: Failed to refresh session: {e}")
    
    def answer(self, local_rtp_port: int = None) -> bool:
        """Answer incoming call with 200 OK"""
        if self.line.state != LineState.INCOMING:
            return False
        
        if not self._incoming_invite:
            logger.error(f"Line {self.line.line_id}: No stored INVITE to answer")
            return False
        
        # Use provided RTP port or default
        rtp_port = local_rtp_port or self._rtp_port
        
        # Parse SDP from incoming INVITE to get remote RTP info
        remote_ip = self.server_ip
        remote_port = 10000
        remote_codecs: List[int] = []
        if self._incoming_invite.body:
            sdp = parse_sdp(self._incoming_invite.body)
            remote_ip = sdp.get("ip", self.server_ip)
            remote_port = sdp.get("port", 10000)
            remote_codecs = sdp.get("codecs", []) or []

        selected_codec = self._select_audio_codec(remote_codecs)
        
        # Build SDP for our answer.
        # We always SEND as PCMU (G.711 μ-law) regardless of what the provider negotiates.
        # We can RECEIVE G.722 (decoded in RTPStream) or PCMU.
        # Listing PCMU first tells the provider: "send me anything in this list; I'll send you PCMU."
        # Most providers that support asymmetric codecs will honour this.
        sdp_codecs = [PAYLOAD_PCMU, PAYLOAD_G722, 101] if selected_codec == PAYLOAD_G722 else [selected_codec, 101]
        sdp_body = self.builder.build_sdp(rtp_port, codecs=sdp_codecs)
        
        # Build 200 OK response with To tag
        to_header = self._incoming_invite.to_header
        if ";tag=" not in to_header:
            to_header = f"{to_header};tag={self.line.local_tag}"
        
        # Build response manually with SDP
        lines = [
            "SIP/2.0 200 OK",
            f"Via: {self._incoming_invite.via}",
            f"From: {self._incoming_invite.from_header}",
            f"To: {to_header}",
            f"Call-ID: {self._incoming_invite.call_id}",
            f"CSeq: {self._incoming_invite.headers.get('CSeq', '')}",
            f"Contact: <sip:{self.username}@{self.builder._sip_host}:{self.builder._sip_port}>",
            "Content-Type: application/sdp",
            f"Content-Length: {len(sdp_body)}",
            "",
            sdp_body
        ]
        response = "\r\n".join(lines)
        
        if not self._send(response):
            logger.error(f"Line {self.line.line_id}: Failed to send 200 OK")
            return False
        
        # Start RTP stream
        self._setup_rtp_stream(rtp_port, remote_ip, remote_port, selected_codec, label="Answer")
        
        # Update line state
        self.line.on_answer(remote_ip, remote_port, self._codec_name(selected_codec))
        
        # Clear stored invite
        self._incoming_invite = None
        
        if self.on_call_answered:
            self.on_call_answered(remote_ip, remote_port, self._codec_name(selected_codec))
        
        logger.info(
            f"Line {self.line.line_id}: Answered call, RTP to {remote_ip}:{remote_port} codec={self._codec_name(selected_codec)}"
        )
        return True
    
    def send_dtmf(self, digit: str):
        """Send DTMF tone"""
        if self.rtp_stream:
            self.rtp_stream.send_dtmf(digit)
    
    def process_message(self, data: bytes, addr: tuple):
        """Process received SIP message"""
        msg = SIPParser.parse(data)
        if not msg:
            return
        
        if msg.is_request:
            self._handle_request(msg, addr)
        else:
            self._handle_response(msg)
    
    def _ack_error_response(self, msg: SIPMessage):
        """Send ACK for a non-2xx final response (RFC 3261 §17.1.1.3).
        
        Works even when _current_invite has been cleared (stale responses
        arriving after hangup). Constructs ACK from the response's own headers.
        """
        try:
            # Extract To tag from the response
            to_tag = ""
            to_header = msg.headers.get("To", "")
            tag_match = re.search(r';tag=([^;>\s]+)', to_header)
            if tag_match:
                to_tag = tag_match.group(1)

            cseq = int(msg.headers.get("CSeq", "1").split()[0])

            if self._current_invite:
                # Best case: we still have the original INVITE
                ack = self.builder.build_ack(self._current_invite, to_tag, None, cseq)
            else:
                # Stale response after hangup — build ACK from response headers
                from_header = msg.headers.get("From", "")
                call_id = msg.call_id or ""
                # RFC 3261: ACK Request-URI should match the original INVITE Request-URI
                # For stale responses we don't have it, so use the To URI
                to_uri_match = re.search(r'<(sip:[^>]+)>', to_header)
                ack_uri = to_uri_match.group(1) if to_uri_match else f"sip:{self.domain}"

                branch = SIPBuilder.generate_branch()
                headers = {
                    "Via": f"SIP/2.0/{self.builder.transport} {self.builder._sip_host}:{self.builder._sip_port};rport;branch={branch}",
                    "Max-Forwards": "70",
                    "From": from_header,
                    "To": to_header,
                    "Call-ID": call_id,
                    "CSeq": f"{cseq} ACK",
                    "Content-Length": "0",
                }
                ack = self.builder.build_request("ACK", ack_uri, headers)

            self._send(ack)
            logger.debug(f"Line {self.line.line_id}: Sent ACK for stale {msg.status_code} response")
        except Exception as e:
            logger.warning(f"Line {self.line.line_id}: Failed to ACK stale {msg.status_code}: {e}")

    def _schedule_error_reset(self):
        """Schedule auto-reset from ERROR or BUSY to IDLE after 5s so the line does not stay stuck."""
        if self._error_reset_timer:
            self._error_reset_timer.cancel()
            self._error_reset_timer = None
        def do_reset():
            self._error_reset_timer = None
            current_state = self.line.state
            if current_state in (LineState.ERROR, LineState.BUSY):
                logger.info(f"Line {self.line.line_id}: Auto-reset {current_state.name} -> IDLE — sending BYE and stopping RTP")
                # Send BYE if dialog still exists, stop RTP, flush audio, then reset line
                self.hangup()
            else:
                logger.debug(f"Line {self.line.line_id}: Auto-reset timer fired but state is {current_state.name}, skipping reset")
        self._error_reset_timer = threading.Timer(5.0, do_reset)
        self._error_reset_timer.daemon = True
        self._error_reset_timer.start()
        logger.info(f"Line {self.line.line_id}: Scheduled auto-reset timer for {self.line.state.name} state")

    def _do_remote_hangup(self, msg: SIPMessage):
        """Common logic for remote hangup (BYE or CANCEL). Caller must send 200 OK first."""
        self._stop_session_timer()
        self._cancel_invite_timeout()
        # Cancel error auto-reset timer if pending (prevents stale reset after remote hangup)
        if self._error_reset_timer:
            self._error_reset_timer.cancel()
            self._error_reset_timer = None
        self._invite_in_progress = False
        self._remote_contact_uri = None
        self._record_route = None
        self._current_invite = None
        self._current_phone_number = None
        self._current_caller_id = None
        if self.rtp_stream:
            self.rtp_stream.stop()
            self.rtp_stream = None
        # Reset "no RTP stream" log flag so the warning fires again on the next call
        self._logged_no_rtp = False
        # Flush stale audio from output queue
        if hasattr(self, 'audio') and self.audio is not None:
            try:
                self.audio.flush_output(self.line.line_id)
            except Exception as e:
                logger.warning(f"Line {self.line.line_id}: Error flushing audio: {e}")
        self.line.on_hangup()
        if self.on_call_ended:
            self.on_call_ended()

    def _handle_request(self, msg: SIPMessage, addr: tuple):
        """Handle incoming SIP request"""
        method = msg.method
        
        if method == "INVITE":
            # Incoming call
            caller_id = self._extract_caller_id(msg.from_header)
            from_uri = msg.from_header
            
            if self.line.start_incoming_call(msg.call_id, from_uri, caller_id):
                # Store incoming INVITE for later answer
                self._incoming_invite = msg
                
                # Extract remote tag
                if ";tag=" in msg.from_header:
                    self.line.remote_tag = msg.from_header.split(";tag=")[1].split(";")[0]
                
                # Generate local tag for this call
                self.line.local_tag = SIPBuilder.generate_tag()
                
                # Send 180 Ringing
                response = self.builder.build_response(180, "Ringing", msg)
                self._send(response)
                
                if self.on_incoming_call:
                    self.on_incoming_call(caller_id, from_uri)
        
        elif method == "BYE":
            # Remote hangup
            logger.info(f"Line {self.line.line_id}: Received BYE from remote (call_id={(msg.call_id or '')[:32]}...)")
            response = self.builder.build_response(200, "OK", msg)
            self._send(response)
            if msg.call_id == self.line.call_id:
                logger.info(f"Line {self.line.line_id}: BYE matches current call_id, hanging up")
                self._do_remote_hangup(msg)
            else:
                logger.warning(f"Line {self.line.line_id}: BYE call_id mismatch (ours={(self.line.call_id or '')[:32]}...)")
        
        elif method == "CANCEL":
            # Remote cancelled (e.g. hangup before answer or server sends CANCEL)
            logger.info(f"Line {self.line.line_id}: Received CANCEL from remote (call_id={(msg.call_id or '')[:32]}...)")
            response = self.builder.build_response(200, "OK", msg)
            self._send(response)
            if self.line.is_active and msg.call_id == self.line.call_id:
                logger.info(f"Line {self.line.line_id}: CANCEL matches current call, hanging up")
                self._do_remote_hangup(msg)
        
        elif method == "ACK":
            # ACK for our 200 OK
            pass
        
        elif method == "OPTIONS":
            # Keepalive - respond 200 OK
            response = self.builder.build_response(200, "OK", msg)
            self._send(response)
    
    def _handle_response(self, msg: SIPMessage):
        """Handle SIP response"""
        cseq_num, cseq_method = msg.cseq
        status = msg.status_code
        
        logger.info(f"Line {self.line.line_id}: {cseq_method} -> {status} {msg.reason}")
        
        if cseq_method == "REGISTER":
            self._handle_register_response(msg)
        
        elif cseq_method == "INVITE":
            self._handle_invite_response(msg)
        
        elif cseq_method == "BYE":
            # BYE confirmed
            pass
        
        elif cseq_method == "UPDATE":
            # Session refresh response
            if status == 200:
                logger.info(f"Line {self.line.line_id}: Session refresh successful")
            elif status == 481:
                # Call/Transaction Does Not Exist - call was terminated remotely
                logger.warning(f"Line {self.line.line_id}: Session refresh failed (481) - call ended")
                self._stop_session_timer()
                if self.rtp_stream:
                    self.rtp_stream.stop()
                    self.rtp_stream = None
                self.line.on_hangup()
                if self.on_call_ended:
                    self.on_call_ended()
            else:
                logger.warning(f"Line {self.line.line_id}: Session refresh failed: {status} {msg.reason}")
    
    def _handle_register_response(self, msg: SIPMessage):
        """Handle REGISTER response"""
        status = msg.status_code
        
        if status == 200:
            self.line.registered = True
            expires = int(msg.headers.get("Expires", "300"))
            self.line.register_expires = expires
            logger.info(f"Line {self.line.line_id}: Registered (expires={expires}s)")
            if self.on_registered:
                self.on_registered(True)
        
        elif status == 401 or status == 407:
            # Need authentication
            www_auth = msg.headers.get("WWW-Authenticate", "") or msg.headers.get("Proxy-Authenticate", "")
            self.register_with_auth(www_auth, auth_type=status)
        
        else:
            logger.error(f"Line {self.line.line_id}: Registration failed: {status} {msg.reason}")
            if self.on_registered:
                self.on_registered(False)
    
    def _handle_invite_response(self, msg: SIPMessage):
        """Handle INVITE response"""
        status = msg.status_code
        
        # Ignore all INVITE responses when line is IDLE or ERROR - they are from a previous transaction
        # (stale 4xx/481/480/200/180 after wrong number or clear, causing ERROR->ERROR, IDLE->ERROR, ERROR->CONNECTED)
        if self.line.state in (LineState.IDLE, LineState.ERROR):
            logger.info(
                f"Line {self.line.line_id}: Ignoring INVITE {status} {getattr(msg, 'reason', '')} - "
                f"line is {self.line.state.name} (stale response from previous call)"
            )
            # RFC 3261: MUST ACK non-2xx final responses even if stale, to stop server retransmissions
            if status >= 300:
                self._ack_error_response(msg)
            return
        
        # CRITICAL: Validate Call-ID matches current call to prevent cross-call contamination
        # Old calls' responses (200 OK, 407, etc.) must not be processed by the new call
        if msg.call_id and self.line.call_id and msg.call_id != self.line.call_id:
            logger.warning(
                f"Line {self.line.line_id}: Ignoring INVITE {status} - Call-ID mismatch "
                f"(got {msg.call_id[:20]}..., expected {self.line.call_id[:20]}...)"
            )
            # RFC 3261: MUST ACK non-2xx final responses even if stale, to stop server retransmissions
            if status >= 300:
                self._ack_error_response(msg)
            return
        
        # Log all headers for debugging session timers
        if status == 200:
            logger.info(f"Line {self.line.line_id}: 200 OK headers: {msg.headers}")
        
        if status == 100:
            # Trying
            pass
        
        elif status == 180 or status == 183:
            # Ringing / Session Progress - reset auth counter (call is progressing)
            self._invite_auth_attempts = 0
            self.line.on_ringing()
            
            # Handle early media (183 Session Progress with SDP)
            if status == 183 and msg.body and not self.rtp_stream:
                logger.info(f"Line {self.line.line_id}: 183 Session Progress with SDP - starting early media")
                sdp = parse_sdp(msg.body)
                remote_ip = sdp.get("ip", self.server_ip)
                remote_port = sdp.get("port", 10000)
                selected_codec = self._select_audio_codec(sdp.get("codecs", []) or [])
                
                # Start RTP stream for early media (ringback tone)
                self._setup_rtp_stream(self._rtp_port, remote_ip, remote_port, selected_codec, label="Early Media")

        
        elif status == 200:
            # Answered! - Release lock and reset auth counter
            self._invite_auth_attempts = 0
            
            # DEBUG: Log body presence
            logger.info(f"Line {self.line.line_id}: 200 OK body present: {msg.body is not None}, body length: {len(msg.body) if msg.body else 0}")
            if msg.body:
                logger.info(f"Line {self.line.line_id}: 200 OK SDP body:\n{msg.body}")
            
            # Ignore stale 200 OK when line is already IDLE (out-of-order / from cleared call)
            if self.line.state == LineState.IDLE:
                logger.info(f"Line {self.line.line_id}: Ignoring 200 OK - line is IDLE (stale response)")
                return
            
            # Extract Contact URI from 200 OK for ACK (RFC 3261)
            contact_uri = None
            contact_header = msg.headers.get("Contact", "")
            if contact_header:
                match = re.search(r'<([^>]+)>', contact_header)
                if match:
                    contact_uri = match.group(1)
                    # Strip transport parameter - it's implicit in UDP
                    if ';transport=' in contact_uri.lower():
                        contact_uri = contact_uri.split(';transport=')[0]
            
            # Extract Record-Route from this response (for ACK)
            record_route = msg.headers.get("Record-Route", "")
            
            # Extract CSeq from 200 OK - this MUST match the INVITE that was answered
            response_cseq = msg.cseq[0]  # CSeq number from 200 OK
            
            # Check if we already processed this 200 OK (retransmission or forked response)
            # CRITICAL: Do NOT overwrite _remote_contact_uri / _record_route here!
            # The first 200 OK that established the dialog has the correct values.
            # Forked 200 OKs may have different Contact URIs from different endpoints.
            if self.line.state == LineState.CONNECTED:
                logger.info(f"Line {self.line.line_id}: Retransmitted/forked 200 OK (CSeq {response_cseq}) - resending ACK only")
                # Send ACK using the STORED dialog values (not the forked values)
                if self._current_invite:
                    ack = self.builder.build_ack(self._current_invite, self.line.remote_tag, self._remote_contact_uri, response_cseq, route=self._record_route)
                    self._send(ack)
                return
            
            # FIRST 200 OK (state is DIALING/RINGING) - store dialog parameters
            # These values define the dialog and must NOT be overwritten by retransmissions
            if contact_uri:
                self._remote_contact_uri = contact_uri
                logger.info(f"Line {self.line.line_id}: Stored remote Contact URI for BYE: {contact_uri}")
            
            if record_route:
                self._record_route = record_route
                logger.info(f"Line {self.line.line_id}: Stored Record-Route for BYE: {record_route}")
            else:
                logger.warning(f"Line {self.line.line_id}: No Record-Route found in 200 OK")
            
            # Handle Session-Expires header for session timers (RFC 4028)
            session_expires = msg.headers.get("Session-Expires", "")
            if session_expires:
                # Parse: "1800;refresher=uac" or just "1800"
                parts = session_expires.split(";")
                try:
                    expire_seconds = int(parts[0].strip())
                    self._session_expires = expire_seconds  # Store for refresh
                    refresher = "uac"  # Default
                    for part in parts[1:]:
                        if "refresher=" in part:
                            refresher = part.split("=")[1].strip()
                    
                    logger.info(f"Line {self.line.line_id}: Session-Expires={expire_seconds}s, refresher={refresher}")
                    
                    # Start session refresh timer if we're the refresher
                    if refresher == "uac":
                        # Refresh at 50% of session time (per RFC 4028 recommendation)
                        refresh_interval = expire_seconds // 2
                        self._start_session_timer(refresh_interval)
                except ValueError as e:
                    logger.warning(f"Line {self.line.line_id}: Failed to parse Session-Expires: {session_expires} ({e})")
            
            # Extract remote tag
            to_header = msg.to_header
            if ";tag=" in to_header:
                self.line.remote_tag = to_header.split(";tag=")[1].split(";")[0]
            
            # Parse SDP for RTP info
            if msg.body:
                logger.info(f"Line {self.line.line_id}: Parsing SDP from 200 OK...")
                sdp = parse_sdp(msg.body)
                logger.info(f"Line {self.line.line_id}: SDP parsed: {sdp}")
                remote_ip = sdp.get("ip", self.server_ip)
                remote_port = sdp.get("port", 10000)
                logger.info(f"Line {self.line.line_id}: Extracted RTP endpoint: {remote_ip}:{remote_port}")
                selected_codec = self._select_audio_codec(sdp.get("codecs", []) or [])
                logger.info(f"Line {self.line.line_id}: Selected codec: {self._codec_name(selected_codec)}")
                
                # Start RTP stream (or update if already started from early media)
                logger.info(f"Line {self.line.line_id}: Checking RTP stream - exists: {self.rtp_stream is not None}")
                if self.rtp_stream:
                    logger.info(f"Line {self.line.line_id}: Current RTP: {self.rtp_stream.remote_ip}:{self.rtp_stream.remote_port}, New: {remote_ip}:{remote_port}")
                    
                    # Always stop the early media stream and start a fresh one for the connected call.
                    # Simply updating remote_ip/port attributes is not enough — the send/receive threads
                    # keep state (ratecv, jitter buffer, sequence numbers) tied to the old stream.
                    logger.info(f"Line {self.line.line_id}: Stopping early media RTP stream, starting fresh Connected stream")
                    self.rtp_stream.stop()
                    self.rtp_stream = None
                    # Flush the channel output queue to remove any stale early-media audio
                    self.audio.flush_output(self.line.line_id)
                    self._setup_rtp_stream(self._rtp_port, remote_ip, remote_port, selected_codec, label="Connected")
                else:
                    # No early media - start RTP now
                    self._setup_rtp_stream(self._rtp_port, remote_ip, remote_port, selected_codec, label="Connected")

                
                self.line.on_answer(remote_ip, remote_port, self._codec_name(selected_codec))
                
                if self.on_call_answered:
                    self.on_call_answered(remote_ip, remote_port, self._codec_name(selected_codec))
            
            # Send ACK to Contact URI (not original INVITE URI) - contact_uri and response_cseq extracted earlier
            logger.info(f"Line {self.line.line_id}: 200 OK CSeq={response_cseq}, using for ACK")
            
            if self._current_invite:
                ack = self.builder.build_ack(self._current_invite, self.line.remote_tag, contact_uri, response_cseq, route=self._record_route)
                logger.info(f"Line {self.line.line_id}: Sending ACK for 200 OK to {contact_uri or 'original URI'} with CSeq {response_cseq} route={self._record_route}")
                # Log full ACK for debugging
                logger.debug(f"Line {self.line.line_id}: ACK message:\n{ack}")
                # Send ACK multiple times to improve delivery through NAT
                for i in range(3):
                    if self._send(ack):
                        logger.info(f"Line {self.line.line_id}: ACK sent successfully (attempt {i+1})")
                    else:
                        logger.error(f"Line {self.line.line_id}: ACK send FAILED (attempt {i+1})")
                    if i < 2:
                        time.sleep(0.05)  # 50ms between retries
            else:
                logger.warning(f"Line {self.line.line_id}: Cannot send ACK - no current invite stored")
            
            # Invite transaction done - allow new INVITE
            self._cancel_invite_timeout()
            self._invite_in_progress = False
        
        elif status == 401 or status == 407:
            # Authentication required for INVITE - retry with auth once
            response_cseq = msg.cseq[0]  # CSeq number from 401/407
            
            # Ignore retransmitted 407 for the same CSeq we already processed
            if hasattr(self, '_last_auth_cseq') and response_cseq == self._last_auth_cseq:
                logger.info(f"Line {self.line.line_id}: Ignoring retransmitted {status} for CSeq {response_cseq}")
                return
            
            # If we already sent authenticated INVITE and get 401/407 for the NEW CSeq, auth really failed
            if self._invite_auth_attempts >= 1 and response_cseq > getattr(self, '_last_auth_cseq', 0):
                logger.warning(f"Line {self.line.line_id}: INVITE auth failed (still {status} after authenticated INVITE, CSeq {response_cseq})")
                # Debug: log the full auth challenge
                auth_header = msg.headers.get("WWW-Authenticate", "") or msg.headers.get("Proxy-Authenticate", "")
                logger.error(f"Line {self.line.line_id}: Auth challenge that failed: {auth_header[:200]}")
                if self.line.on_error(f"Auth failed {status}"):
                    if self.on_call_ended:
                        self.on_call_ended()
                self._schedule_error_reset()
                self._cancel_invite_timeout()
                self._invite_in_progress = False
                return
            if self.line.state == LineState.CONNECTED:
                logger.info(f"Line {self.line.line_id}: Ignoring {status} - already connected")
                return
            
            # Store which CSeq we're handling auth for (to ignore retransmissions)
            self._last_auth_cseq = response_cseq
            
            auth_header = msg.headers.get("WWW-Authenticate", "") or msg.headers.get("Proxy-Authenticate", "")
            logger.info(f"Line {self.line.line_id}: Auth challenge received (CSeq {response_cseq}): {auth_header[:150]}")
            if auth_header and self._current_invite:
                # Pass auth_type: 401 = Authorization header, 407 = Proxy-Authorization header
                self._invite_with_auth(auth_header, auth_type=status)
            else:
                # Only emit call_ended if state actually changed
                if self.line.on_error(f"{status} {msg.reason}"):
                    if self.on_call_ended:
                        self.on_call_ended()
                    self._schedule_error_reset()
                self._cancel_invite_timeout()
                self._invite_in_progress = False
        
        elif status >= 400:
            reason = msg.reason
            logger.warning(f"Line {self.line.line_id}: INVITE failed with {status} {reason}")
            
            # Send ACK to stop server from retransmitting the error response (RFC 3261)
            if self._current_invite and msg.headers.get("To"):
                try:
                    # Extract To tag from the error response
                    to_tag = ""
                    to_header = msg.headers.get("To", "")
                    tag_match = re.search(r';tag=([^;>\s]+)', to_header)
                    if tag_match:
                        to_tag = tag_match.group(1)
                    
                    cseq = int(msg.headers.get("CSeq", "1").split()[0])
                    ack = self.builder.build_ack(self._current_invite, to_tag, None, cseq)
                    self._send(ack)
                    logger.debug(f"Line {self.line.line_id}: Sent ACK for {status} response")
                except Exception as e:
                    logger.warning(f"Line {self.line.line_id}: Failed to send ACK for {status}: {e}")
            
            # Ignore "500 Overlapping Requests" - this happens when multiple INVITEs are sent
            if status == 500 and "Overlapping" in reason:
                logger.info(f"Line {self.line.line_id}: Ignoring 500 Overlapping Requests - call may still succeed")
                return
            
            if status == 486:
                # Busy: show busy state, then auto-reset after 5s
                state_changed = self.line.on_busy()
                if state_changed and self.on_call_ended:
                    self.on_call_ended()
                self._schedule_error_reset()  # Reuse error reset timer to auto-clear BUSY state
            elif status in (401, 403, 487):
                # Auth failure (401/403) or request terminated (487): end call so line doesn't stay stuck on DIALING
                if self.line.on_error(f"{status} {reason}"):
                    if self.on_call_ended:
                        self.on_call_ended()
                self._schedule_error_reset()
            else:
                # Other 4xx/5xx (e.g. 404, 408, 480, 603): set ERROR so UI shows failure, auto-reset after 5s
                if self.line.on_error(f"{status} {reason}"):
                    if self.on_call_ended:
                        self.on_call_ended()
                self._schedule_error_reset()
            
            self._cancel_invite_timeout()
            self._invite_in_progress = False
    
    def _extract_caller_id(self, from_header: str) -> str:
        """Extract caller ID from From header"""
        # Try to get display name
        match = re.match(r'"([^"]+)"', from_header)
        if match:
            return match.group(1)
        
        # Try to get user part of URI
        match = re.search(r'sip:([^@]+)@', from_header)
        if match:
            return match.group(1)
        
        return from_header
    
    def _send(self, message: str, dest_ip: str = None, dest_port: int = None) -> bool:
        """Send SIP message"""
        try:
            target_ip = dest_ip or self.server_ip
            target_port = dest_port or self.server_port
            if self._use_tls:
                # TLS/TCP: send as framed bytes (no dest addr needed, already connected)
                self.socket.sendall(message.encode())
            else:
                self.socket.sendto(message.encode(), (target_ip, target_port))
            # Log first line of message (the request/response line)
            first_line = message.split('\r\n')[0] if '\r\n' in message else message.split('\n')[0]
            logger.debug(f"Line {self.line.line_id}: Sent to {target_ip}:{target_port}: {first_line}")
            return True
        except Exception as e:
            logger.error(f"SIP send error: {e}")
            # For TLS: if the connection broke, try to reconnect and resend once
            if self._use_tls:
                logger.info(f"Line {self.line.line_id}: Attempting TLS reconnect after send failure...")
                if self._reconnect_tls():
                    try:
                        self.socket.sendall(message.encode())
                        logger.info(f"Line {self.line.line_id}: Resend after TLS reconnect succeeded")
                        return True
                    except Exception as e2:
                        logger.error(f"Line {self.line.line_id}: Resend after TLS reconnect failed: {e2}")
            elif isinstance(e, OSError) and e.errno in (22, 101, 100, 99):
                # UDP socket broken after network change (Errno 22=Invalid argument,
                # 101=Network unreachable, 100=Network down, 99=Cannot assign address)
                # Recreate the socket bound to 0.0.0.0 and retry once
                logger.info(f"Line {self.line.line_id}: UDP socket broken (errno={e.errno}), recreating and retrying...")
                try:
                    if self.socket:
                        try:
                            self.socket.close()
                        except Exception:
                            pass
                    new_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    new_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    new_sock.bind(("0.0.0.0", self.local_port))
                    new_sock.settimeout(0.5)
                    self.socket = new_sock
                    self.socket.sendto(message.encode(), (target_ip, target_port))
                    logger.info(f"Line {self.line.line_id}: UDP socket recreated and send succeeded")
                    return True
                except Exception as e2:
                    logger.error(f"Line {self.line.line_id}: UDP socket recreate failed: {e2}")
            return False


class SIPEngine:
    """
    Main SIP Engine managing multiple phone lines.
    Handles registration, calls, and audio routing.
    """
    
    def __init__(self, num_lines: int = 8, config_path: str = None):
        self.num_lines = num_lines
        self.config_path = config_path
        
        # Configuration
        self.config = {
            "server_ip": "127.0.0.1",
            "server_port": 5060,
            "local_ip": "0.0.0.0",
            "sip_port_base": 5100,
            "rtp_port_base": 10000,
            "domain": "localhost",
            "accounts": [],  # List of {username, password}
            "caller_id": "",
            "audio_device": "default",
        }
        
        # Components
        self.lines: List[Line] = []
        self.clients: List[SIPClient] = []
        self.events = EventEmitter()
        self.audio = None  # Created in start() based on audio_mode config
        
        # State
        self._running = False
        self._receive_threads: List[threading.Thread] = []
        self._register_thread: Optional[threading.Thread] = None
        self._recovery_thread: Optional[threading.Thread] = None  # Auto-retry failed clients after cold boot
        self._failed_clients: List[tuple] = []  # (client, rtp_base_port, send_audio_callback)
        
        # External callbacks
        self.on_state_change: Optional[Callable[[int, LineState, LineState], None]] = None
        self.on_registration_update: Optional[Callable[[], None]] = None  # Called when any line registers (for UI 1/8..8/8)
    
    def load_config(self, config_path: str = None):
        """Load configuration from JSON file"""
        path = config_path or self.config_path
        if not path:
            return
        
        try:
            with open(path, 'r') as f:
                loaded = json.load(f)
                self.config.update(loaded)
            logger.info(f"Config loaded from {path}")
        except FileNotFoundError:
            logger.warning(f"Config file not found: {path}")
        except Exception as e:
            logger.error(f"Error loading config: {e}")
    
    def configure(self, server_ip: str, server_port: int = 5060,
                  domain: str = None, accounts: List[Dict] = None,
                  caller_id: str = "", audio_device: str = "default"):
        """Configure engine programmatically"""
        self.config["server_ip"] = server_ip
        self.config["server_port"] = server_port
        self.config["domain"] = domain or server_ip
        self.config["caller_id"] = caller_id
        self.config["audio_device"] = audio_device
        
        if accounts:
            self.config["accounts"] = accounts
    
    def start(self) -> bool:
        """Start the SIP engine"""
        if self._running:
            return True
        
        logger.info("Starting Smart SIP Engine...")
        
        # Load configuration from file if path provided
        if self.config_path:
            self.load_config(self.config_path)
        
        # Detect local IP and advertise IP; retry if network is down at startup (common on Pi)
        max_network_retries = 60  # 60 * 10s = 10 minutes (covers slow Peplink boot)
        retry_delay_sec = 10
        advertise_ip_from_config = self.config.get("public_ip") or self.config.get("advertise_ip")
        use_local_ip_in_sdp = self.config.get("use_local_ip_in_sdp", False)
        
        for attempt in range(max_network_retries):
            local_ip = self._detect_local_ip()
            self.config["local_ip"] = local_ip
            logger.info(f"Local IP: {local_ip}")

            # Public/NAT IP to advertise in SIP/SDP: config overrides, else auto-detect
            # If use_local_ip_in_sdp is True, force local IP for better NAT traversal
            if use_local_ip_in_sdp:
                advertise_ip = local_ip
                logger.info(f"Using local IP in SDP (use_local_ip_in_sdp=true): {advertise_ip}")
            elif advertise_ip_from_config:
                advertise_ip = advertise_ip_from_config
                logger.info(f"Using configured advertise IP: {advertise_ip}")
            else:
                public = self._detect_public_ip()
                advertise_ip = public if public else get_local_ip()
                if public:
                    logger.info(f"Auto-detected public IP, advertising: {advertise_ip}")
                else:
                    logger.info(f"No public IP in config and auto-detect failed, using local: {advertise_ip}")
                logger.info(f"Advertising IP (from config): {advertise_ip}")
            
            # If network was down we get 0.0.0.0 / 127.0.0.1; retry after delay
            if local_ip not in ("0.0.0.0", "") and (advertise_ip_from_config or advertise_ip not in ("127.0.0.1", "")):
                break
            if attempt < max_network_retries - 1:
                logger.warning(f"Network unreachable at startup (attempt {attempt + 1}/{max_network_retries}), retrying in {retry_delay_sec}s...")
                time.sleep(retry_delay_sec)
        
        # Create audio manager based on audio_mode config
        audio_mode = self.config.get("audio_mode", "usb_dongles")

        if audio_mode == "usb_dongles":
            # USB audio dongles — separate card per line/channel
            # Run the mapping script to discover current USB dongle positions.
            # We read stdout directly to avoid /tmp file permission issues (udev
            # writes the file as root; we may not be able to overwrite it).
            line_audio_cards = {}
            map_file = os.path.join(os.path.dirname(__file__), '..', 'scripts', 'map_usb_audio.sh')
            # Expected = number of dongles defined in config (not total SIP lines)
            configured_cards = self.config.get("line_audio_cards", {})
            expected_channels = len(configured_cards) if configured_cards else self.num_lines
            # Initialize here so it's always defined even if map script is missing
            discovered_headset_card = -1
            
            if os.path.exists(map_file):
                import subprocess

                def _run_map_script():
                    """Run map_usb_audio.sh and return (channel_map_dict, headset_card_int).
                    headset_card_int is -1 if not found."""
                    channel_map = {}
                    headset_card = -1
                    try:
                        result = subprocess.run(
                            ["bash", map_file, "scan"], timeout=6,
                            capture_output=True, text=True, check=False
                        )
                        for line in result.stdout.strip().splitlines():
                            line = line.strip()
                            if line.startswith("HEADSET_CARD="):
                                try:
                                    headset_card = int(line.split("=", 1)[1])
                                except ValueError:
                                    pass
                            elif line.startswith("{"):
                                try:
                                    channel_map = json.loads(line)
                                except Exception:
                                    pass
                    except Exception as e:
                        logger.warning(f"Could not run USB audio mapping script: {e}")
                    return channel_map, headset_card

                # Retry for up to ~20 seconds to handle USB enumeration race at boot
                last_discovered = {}
                for attempt in range(20):
                    discovered, hc = _run_map_script()
                    if hc >= 0:
                        discovered_headset_card = hc
                    if discovered:
                        last_discovered = discovered
                    if len(discovered) >= expected_channels:
                        line_audio_cards = discovered
                        logger.info(f"Auto-discovered USB audio mapping (attempt {attempt+1}): {line_audio_cards}")
                        break
                    logger.debug(f"Partial USB map ({len(discovered)}/{expected_channels}): {discovered}, retrying... (attempt {attempt+1}/20)")
                    time.sleep(1)

                # If retries exhausted but we have a partial result, use it
                if not line_audio_cards:
                    if last_discovered:
                        line_audio_cards = last_discovered
                        logger.warning(f"USB map incomplete after retries, using partial: {line_audio_cards}")
                    else:
                        logger.warning("USB audio mapping script returned no results")
            
            # Fall back to config if auto-discovery found nothing
            if not line_audio_cards:
                line_audio_cards = self.config.get("line_audio_cards", {})
                logger.info(f"Using config USB audio mapping: {line_audio_cards}")

            # Resolve headset card: prefer script discovery, fall back to config
            headset_card = discovered_headset_card if discovered_headset_card >= 0 \
                else self.config.get("headset_card", -1)

            # USB-only audio manager
            self.audio = USBDongleAudioManager(
                max_lines=self.num_lines,
                line_to_card=line_audio_cards or None,
                headset_card=headset_card
            )
            logger.info(f"Using USB dongle audio mode, card mapping: {line_audio_cards}")
                
            if headset_card >= 0:
                logger.info(f"Headset card resolved to ALSA card {headset_card}")
            else:
                logger.info("No headset card configured — headset feature disabled")
        else:
            # Fallback: USB dongles with defaults
            line_audio_cards = self.config.get("line_audio_cards", {})
            headset_card = self.config.get("headset_card", -1)
            self.audio = USBDongleAudioManager(
                max_lines=self.num_lines,
                line_to_card=line_audio_cards or None,
                headset_card=headset_card
            )
            logger.info(f"Using USB dongle audio mode (fallback), card mapping: {line_audio_cards}")

        # Initialize audio
        if not self.audio.initialize():
            logger.error("Failed to initialize audio")
            return False

        # Start all configured channel (dongle) streams before registering lines
        self.audio.initialize_channels()

        # Start headset dongle if a card was configured
        if hasattr(self.audio, 'start_headset') and hasattr(self.audio, '_headset_card'):
            hc = self.audio._headset_card
            if hc >= 0:
                self.audio.start_headset(hc)
                logger.info(f"Headset audio started on ALSA card {hc}")
            else:
                logger.info("Headset audio not started (no card configured)")
        
        # Create lines and clients
        for i in range(self.num_lines):
            line_id = i + 1
            
            # Get account for this line (fall back to last account for overflow lines)
            accounts = self.config.get("accounts", [])
            if accounts:
                account = accounts[i] if i < len(accounts) else accounts[-1]
                username = account.get("username", f"100{line_id}")
                password = account.get("password", "")
            else:
                username = f"100{line_id}"
                password = ""
            
            # Create line
            line = Line(line_id=line_id, sip_account=username)
            line.on_state_change(self._on_line_state_change)
            self.lines.append(line)
            
            # Create SIP client
            client = SIPClient(
                line=line,
                server_ip=self.config["server_ip"],
                server_port=self.config["server_port"],
                local_ip=local_ip,
                local_port=self.config["sip_port_base"] + i,
                username=username,
                password=password,
                domain=self.config["domain"]
            )
            # Via/Contact/SDP: config or auto (STUN in start() when use_stun)
            client.advertise_ip = advertise_ip or local_ip
            # STUN only when no manual public_ip/advertise_ip (so we discover mapped port without port forwarding)
            client.use_stun = not (self.config.get("public_ip") or self.config.get("advertise_ip"))
            # Pass use_local_ip_in_sdp flag to client for SDP generation
            client.use_local_ip_in_sdp = use_local_ip_in_sdp
            
            # Set callbacks
            client.on_incoming_call = lambda cid, uri, lid=line_id: self._on_incoming(lid, cid, uri)
            client.on_call_answered = lambda ip, port, codec, lid=line_id: self._on_answered(lid, ip, port, codec)
            client.on_call_ended = lambda lid=line_id: self._on_ended(lid)
            client.on_registered = lambda success, lid=line_id: self._on_registered(lid, success)
            
            # Register audio routing for this line (before start, in case we need to retry later)
            def send_audio(data, c=client, lid=line_id):
                if c.rtp_stream:
                    c.rtp_stream.send_audio(data)
                else:
                    # Log once per call if RTP stream doesn't exist
                    if not hasattr(c, '_logged_no_rtp'):
                        logger.warning(f"📭 Line {lid}: Mic audio captured but no RTP stream to send it! Call may not be active.")
                        c._logged_no_rtp = True
            
            if not client.start(self.config["rtp_port_base"]):
                logger.error(f"Failed to start SIP client for line {line_id}")
                # Save for retry by network recovery watchdog (cold boot race with Peplink)
                self._failed_clients.append((client, self.config["rtp_port_base"], send_audio))
                continue
            
            self.clients.append(client)
            
            output_queue = self.audio.register_line(line_id, send_audio)
            
            # Store audio manager and output queue references on client
            client.audio = self.audio
            client._audio_output_queue = output_queue
        
        # Start event system
        self.events.start()
        
        # Start network recovery watchdog if any clients failed (cold boot race with Peplink)
        if self._failed_clients:
            logger.warning(f"⚠️  {len(self._failed_clients)} line(s) failed to start — network recovery watchdog starting...")
            self._recovery_thread = threading.Thread(
                target=self._network_recovery_loop,
                daemon=True,
                name="Network-Recovery"
            )
            self._recovery_thread.start()
        
        # Start receive threads for successfully started clients
        self._running = True
        for client in self.clients:
            thread = threading.Thread(
                target=self._receive_loop,
                args=(client,),
                daemon=True,
                name=f"SIP-Recv-{client.line.line_id}"
            )
            thread.start()
            self._receive_threads.append(thread)
        
        # Start registration thread only when not outgoing-only (skip REGISTER for outgoing-only mode)
        outgoing_only = self.config.get("outgoing_only", False)
        if outgoing_only:
            logger.info("Outgoing-only mode: skipping REGISTER (no incoming calls)")
        else:
            self._register_thread = threading.Thread(
                target=self._registration_loop,
                daemon=True,
                name="SIP-Register"
            )
            self._register_thread.start()
        
        # Start audio
        self.audio.start()
        
        logger.info(f"Smart SIP Engine started with {len(self.clients)} lines")
        return True
    
    def _network_recovery_loop(self):
        """Retry failed SIP clients after cold boot network race condition.
        
        When the Pi boots before the Peplink router is ready, SIP socket binding
        fails. This watchdog retries every 5 seconds for up to 60 seconds.
        """
        max_retries = 60  # 60 * 10s = 10 minutes (covers slow Peplink boot)
        retry_interval = 10.0
        
        for attempt in range(1, max_retries + 1):
            if not self._running and attempt > 1:
                logger.info("🛑 Network recovery: engine stopped, aborting retries")
                return
            
            time.sleep(retry_interval)
            
            if not self._failed_clients:
                logger.info("✅ Network recovery: all clients recovered!")
                return
            
            logger.info(f"🔄 Network recovery attempt {attempt}/{max_retries} — {len(self._failed_clients)} client(s) pending...")
            
            still_failed = []
            for client, rtp_base_port, send_audio in self._failed_clients:
                line_id = client.line.line_id
                
                # Try to start the client again
                if client.start(rtp_base_port):
                    logger.info(f"✅ Line {line_id}: recovered after {attempt * retry_interval:.0f}s!")
                    
                    # Register audio routing
                    self.clients.append(client)
                    output_queue = self.audio.register_line(line_id, send_audio)
                    client.audio = self.audio
                    client._audio_output_queue = output_queue
                    
                    # Start receive thread
                    thread = threading.Thread(
                        target=self._receive_loop,
                        args=(client,),
                        daemon=True,
                        name=f"SIP-Recv-{line_id}"
                    )
                    thread.start()
                    self._receive_threads.append(thread)
                    
                    # Emit UI state change
                    if self.events:
                        self.events.emit("line_state_changed", {
                            "line_id": line_id,
                            "state": "idle"
                        })
                else:
                    still_failed.append((client, rtp_base_port, send_audio))
            
            self._failed_clients = still_failed
        
        # Exhausted retries
        if self._failed_clients:
            failed_ids = [c.line.line_id for c, _, _ in self._failed_clients]
            logger.error(f"❌ Network recovery FAILED after {max_retries * retry_interval:.0f}s — lines still down: {failed_ids}")
            logger.error("Manual restart may be required once network is available")

    def stop(self):
        """Stop the SIP engine"""
        logger.info("Stopping Smart SIP Engine...")
        
        self._running = False
        
        # Hangup all active calls
        for client in self.clients:
            if client.line.is_active:
                client.hangup()
        
        # Wait for threads
        for thread in self._receive_threads:
            thread.join(timeout=1.0)
        self._receive_threads.clear()
        
        if self._register_thread:
            self._register_thread.join(timeout=2.0)
        
        # Stop clients
        for client in self.clients:
            client.stop()
        self.clients.clear()
        
        # Stop audio
        if self.audio:
            self.audio.shutdown()
        
        # Stop events
        self.events.stop()
        
        # Clear lines
        self.lines.clear()
        
        logger.info("Smart SIP Engine stopped")
    
    @staticmethod
    def normalize_dial_number(phone_number: str) -> str:
        """
        Dial assistance: normalize to E.164 for SIP.
        - US: 10 digits (646 383 XXXX) or 11 with leading 1 -> add +1.
        - International: already +44, +56, etc. -> keep. Or 011 44... -> strip 011, add +.
        """
        raw = (phone_number or '').strip()
        digits_plus = re.sub(r'[^\d+]', '', raw)
        if not digits_plus:
            return raw
        if digits_plus.startswith('+'):
            return digits_plus
        if digits_plus.startswith('011'):
            return '+' + digits_plus[3:]
        if len(digits_plus) == 10:
            return '+1' + digits_plus
        if len(digits_plus) == 11 and digits_plus.startswith('1'):
            return '+' + digits_plus
        return '+' + digits_plus

    def _get_client(self, line_id: int) -> Optional['SIPClient']:
        """Look up SIPClient by line_id (safe even after network recovery reordering)."""
        return next((c for c in self.clients if c.line.line_id == line_id), None)

    def make_call(self, line_id: int, phone_number: str) -> bool:
        """Make a call on specified line"""
        if line_id < 1 or line_id > self.num_lines:
            logger.error(f"Invalid line ID: {line_id}")
            return False
        
        client = self._get_client(line_id)
        if not client:
            logger.error(f"make_call: no client found for line {line_id}")
            return False
        
        # Prevent duplicate dial when line is already active
        if client.line.state in (LineState.DIALING, LineState.RINGING, LineState.CONNECTED):
            logger.warning(f"Line {line_id}: Cannot dial - line is already {client.line.state.name}")
            return False
        
        phone_number = self.normalize_dial_number(phone_number)
        logger.info(f"Line {line_id}: Calling {phone_number}")
        
        # If line is in ERROR (wrong number), clear it first so the new dial succeeds
        if client.line.state == LineState.ERROR:
            self.hangup_call(line_id)
            client = self._get_client(line_id)
        
        self.audio.set_active_line(line_id)
        return client.invite(phone_number, self.config.get("caller_id"))
    
    def set_line_mute(self, line_id: int, muted: bool) -> bool:
        """
        Mute/unmute headset for specified line (Listen & Talk).
        When muted: clear headset listen/active if this line had them.
        When unmuted: only this line has headset on (one lock in audio layer).
        """
        if line_id < 1 or line_id > len(self.lines):
            logger.error(f"Invalid line ID: {line_id}")
            return False
        if muted:
            self.audio.mute_line_and_clear_headset_if(line_id)
        else:
            self.audio.set_headset_line_only(line_id)
        
        return True
    
    def set_headset_listen_line(self, line_id: Optional[int]) -> bool:
        """
        Set which line's audio is sent to headset output.
        Used for "listen" toggle: dial tone, ringing, or call audio on that line.
        line_id 1-8 = listen to that line; 0 or None = listen to none.
        """
        if line_id is not None and (line_id < 0 or line_id > 8):
            return False
        self.audio.set_headset_listen_line(line_id if line_id else None)
        logger.info(f"Headset listen line set to {line_id}")
        return True
    
    def get_headset_listen_line(self) -> Optional[int]:
        """Get which line is currently sent to headset output."""
        if self.audio is None:
            return None
        return self.audio.get_headset_listen_line()

    def get_active_line(self) -> Optional[int]:
        """Get which line receives headset mic (talk)."""
        if self.audio is None:
            return None
        return self.audio.get_active_line()
    
    def hangup_call(self, line_id: int) -> bool:
        """Hangup call on specified line - always resets to IDLE. Clears error and stops remote ringing."""
        if line_id < 1 or line_id > self.num_lines:
            return False
        
        client = self._get_client(line_id)
        if not client:
            return False
        
        # Cancel error auto-reset timer so it does not fire after we clear
        if hasattr(client, '_error_reset_timer') and client._error_reset_timer:
            try:
                client._error_reset_timer.cancel()
            except Exception:
                pass
            client._error_reset_timer = None
        
        # Try to send BYE if there's an active call
        if client.line.is_active:
            try:
                client.hangup()
            except Exception as e:
                logger.warning(f"Line {line_id}: Error during hangup: {e}")
        elif client.line.state == LineState.ERROR:
            # Clear error: send CANCEL so remote stops ringing, then reset
            try:
                client.send_cancel()
            except Exception as e:
                logger.warning(f"Line {line_id}: Error sending CANCEL: {e}")
        
        # Reset to IDLE only if not already there (hangup() may have done it already;
        # calling reset() on an IDLE line fires an invalid IDLE->IDLE transition warning)
        if client.line.state != LineState.IDLE:
            client.line.reset()
        
        # Always clear invite-in-progress flag so next dial is not blocked
        client._invite_in_progress = False
        client._cancel_invite_timeout()
        
        # Stop RTP if running
        if client.rtp_stream:
            client.rtp_stream.stop()
            client.rtp_stream = None
        
        # Notify callback
        if client.on_call_ended:
            client.on_call_ended()
        
        return True
    
    def answer_call(self, line_id: int) -> bool:
        """Answer incoming call on specified line"""
        if line_id < 1 or line_id > self.num_lines:
            return False
        
        client = self._get_client(line_id)
        if not client:
            return False
        
        # Set this line as active for audio
        self.audio.set_active_line(line_id)
        
        return client.answer()
    
    def send_dtmf(self, line_id: int, digit: str):
        """Send DTMF digit on specified line"""
        if line_id < 1 or line_id > self.num_lines:
            return
        
        client = self._get_client(line_id)
        if not client:
            return
        client.send_dtmf(digit)
    
    def get_line(self, line_id: int) -> Optional[Line]:
        """Get Line object for specified line ID"""
        if line_id < 1 or line_id > len(self.lines):
            return None
        return self.lines[line_id - 1]
    
    def get_line_status(self, line_id: int) -> dict:
        """Get status of a specific line"""
        if line_id < 1 or line_id > len(self.lines):
            return {}
        return self.lines[line_id - 1].get_status()
    
    def get_all_status(self) -> List[dict]:
        """Get status of all lines"""
        return [line.get_status() for line in self.lines]
    
    def _receive_loop(self, client: SIPClient):
        """Receive SIP messages for a client"""
        if client._use_tls:
            self._receive_loop_tls(client)
        else:
            self._receive_loop_udp(client)
    
    def _receive_loop_udp(self, client: SIPClient):
        """Receive SIP messages over UDP (one datagram = one message)"""
        while self._running:
            try:
                data, addr = client.socket.recvfrom(4096)
                client.process_message(data, addr)
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    logger.error(f"Receive error on line {client.line.line_id}: {e}")
    
    def _receive_loop_tls(self, client: SIPClient):
        """Receive SIP messages over TLS/TCP with proper stream framing.
        
        TCP is stream-based, so we must buffer data and extract complete
        SIP messages using the Content-Length header to find message boundaries.
        Automatically reconnects if the server closes the connection.
        """
        buf = b""
        addr = (client.server_ip, client.server_port)
        
        while self._running:
            try:
                if client.socket is None:
                    # Socket not available (reconnect failed), wait and retry
                    time.sleep(5)
                    if self._running:
                        client._reconnect_tls()
                    continue
                
                chunk = client.socket.recv(8192)
                if not chunk:
                    # Connection closed by server — try to reconnect
                    if not self._running:
                        break
                    logger.warning(f"Line {client.line.line_id}: TLS connection closed by server, reconnecting...")
                    buf = b""  # discard partial data
                    if client._reconnect_tls():
                        continue
                    else:
                        # Reconnect failed, wait before retrying
                        time.sleep(5)
                        if self._running and client._reconnect_tls():
                            continue
                        elif not self._running:
                            break
                        logger.error(f"Line {client.line.line_id}: TLS reconnect failed after retry")
                        time.sleep(30)
                        if self._running:
                            client._reconnect_tls()
                        continue
                    
                buf += chunk
                
                # Extract complete SIP messages from the buffer
                while buf:
                    # Find the end of the SIP headers
                    header_end = buf.find(b"\r\n\r\n")
                    if header_end == -1:
                        break  # Don't have full headers yet, wait for more data
                    
                    headers_section = buf[:header_end].decode("utf-8", errors="replace")
                    
                    # Parse Content-Length to know how much body follows
                    # SIP allows "Content-Length:", "content-length:", or compact form "l:"
                    content_length = 0
                    for line in headers_section.split("\r\n"):
                        lower_line = line.lower().strip()
                        if lower_line.startswith("content-length:") or lower_line.startswith("l:"):
                            try:
                                content_length = int(line.split(":", 1)[1].strip())
                            except ValueError:
                                pass
                            break
                    
                    # Total message size: headers + \r\n\r\n + body
                    total_len = header_end + 4 + content_length
                    if len(buf) < total_len:
                        break  # Don't have the full body yet, wait for more data
                    
                    # Extract this complete message and process it
                    message_data = buf[:total_len]
                    buf = buf[total_len:]
                    client.process_message(message_data, addr)
                    
            except socket.timeout:
                continue
            except (ssl.SSLError, ConnectionError, BrokenPipeError, OSError) as e:
                if not self._running:
                    break
                logger.error(f"Line {client.line.line_id}: TLS connection error: {e}, reconnecting...")
                buf = b""
                time.sleep(2)
                client._reconnect_tls()
                continue
            except Exception as e:
                if self._running:
                    logger.error(f"TLS receive error on line {client.line.line_id}: {e}")
    
    def _registration_loop(self):
        """Periodic registration for all lines"""
        # Initial registration
        time.sleep(1)
        for client in self.clients:
            client.register()
            time.sleep(0.1)  # Stagger registrations
        
        # Track when each line last registered so we know when to re-register.
        # register_expires on the Line object is set from the server's 200 OK
        # but never decremented — so we track elapsed time ourselves.
        last_register_time = {i: time.time() for i in range(len(self.clients))}
        
        # Re-register periodically
        while self._running:
            time.sleep(60)  # Check every minute
            
            now = time.time()
            for idx, client in enumerate(self.clients):
                line = client.line
                elapsed = now - last_register_time.get(idx, 0)
                # Re-register if: not registered, or approaching expiry (within 120s of expires)
                expires = line.register_expires if line.register_expires > 0 else 300
                if not line.registered or elapsed >= (expires - 120):
                    client.register()
                    last_register_time[idx] = now
                    time.sleep(0.1)
    
    def _detect_local_ip(self) -> str:
        """Detect local IP address (delegates to module-level get_local_ip)"""
        return get_local_ip()

    def _detect_public_ip(self) -> Optional[str]:
        """Auto-detect public/NAT IP so we can advertise it in SIP without manual config."""
        services = [
            "https://api.ipify.org",
            "https://icanhazip.com",
            "https://ifconfig.me/ip",
        ]
        for url in services:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "ProComm-SmartSIP/1.0"})
                with urllib.request.urlopen(req, timeout=3) as resp:
                    ip = resp.read().decode("utf-8").strip()
                    if ip and re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip):
                        return ip
            except Exception as e:
                logger.debug(f"Public IP check {url} failed: {e}")
                continue
        return None
    
    def _on_line_state_change(self, line_id: int, old_state: LineState, new_state: LineState):
        """Handle line state changes"""
        logger.debug(f"Line {line_id} state: {old_state.name} -> {new_state.name}")
        self.events.emit(Event(
            type=EventType.LINE_STATE_CHANGED,
            line_id=line_id,
            data={"old_state": old_state.name, "new_state": new_state.name}
        ))
        if self.on_state_change:
            self.on_state_change(line_id, old_state, new_state)
    
    def _on_incoming(self, line_id: int, caller_id: str, from_uri: str):
        """Handle incoming call"""
        self.events.emit(Event(
            type=EventType.INCOMING_CALL,
            line_id=line_id,
            data={"caller_id": caller_id, "from_uri": from_uri}
        ))
    
    def _on_answered(self, line_id: int, remote_ip: str, remote_port: int, codec: str):
        """Handle call answered"""
        self.events.emit(Event(
            type=EventType.CALL_ANSWERED,
            line_id=line_id,
            data={"remote_ip": remote_ip, "remote_port": remote_port, "codec": codec}
        ))
    
    def _on_ended(self, line_id: int):
        """Handle call ended"""
        self.events.emit(Event(
            type=EventType.CALL_ENDED,
            line_id=line_id
        ))
    
    def _on_registered(self, line_id: int, success: bool):
        """Handle registration result"""
        # Update line's registered status
        if 1 <= line_id <= len(self.lines):
            self.lines[line_id - 1].registered = success
            logger.info(f"Line {line_id} registration updated: {success}")
        
        event_type = EventType.REGISTERED if success else EventType.REGISTRATION_FAILED
        self.events.emit(Event(
            type=event_type,
            line_id=line_id,
            data={"success": success}
        ))
        # Emit sip_status on each successful registration so UI shows 1/8, 2/8, ... 8/8
        if success and self.on_registration_update:
            try:
                self.on_registration_update()
            except Exception as e:
                logger.warning(f"on_registration_update callback failed: {e}")
