"""
RTP Handler for Smart SIP Engine
Handles real-time audio streaming with jitter buffer
"""

import socket
import struct
import threading
import queue
import time
import audioop
import random
from typing import Optional, Callable
from dataclasses import dataclass
import logging

try:
    import G722
    _g722_codec = G722.G722(16000, 64000)
    G722_AVAILABLE = True
except (ImportError, Exception):
    G722_AVAILABLE = False
    _g722_codec = None
    logging.warning("g722 library not available - G.722 codec will fall back to PCMU")

logger = logging.getLogger(__name__)

# RTP payload types
PAYLOAD_PCMU = 0    # G.711 μ-law
PAYLOAD_PCMA = 8    # G.711 A-law
PAYLOAD_G722 = 9    # G.722 wideband (16kHz sampled, 7kHz bandwidth)
PAYLOAD_DTMF = 101  # RFC 2833 telephone-event


@dataclass
class RTPPacket:
    """Parsed RTP packet"""
    version: int
    padding: bool
    extension: bool
    csrc_count: int
    marker: bool
    payload_type: int
    sequence: int
    timestamp: int
    ssrc: int
    payload: bytes
    
    @staticmethod
    def parse(data: bytes) -> Optional['RTPPacket']:
        """Parse RTP packet from bytes"""
        if len(data) < 12:
            return None
        
        try:
            # First 2 bytes
            b0, b1 = data[0], data[1]
            version = (b0 >> 6) & 0x03
            padding = bool((b0 >> 5) & 0x01)
            extension = bool((b0 >> 4) & 0x01)
            csrc_count = b0 & 0x0F
            marker = bool((b1 >> 7) & 0x01)
            payload_type = b1 & 0x7F
            
            # Sequence number (2 bytes)
            sequence = struct.unpack('!H', data[2:4])[0]
            
            # Timestamp (4 bytes)
            timestamp = struct.unpack('!I', data[4:8])[0]
            
            # SSRC (4 bytes)
            ssrc = struct.unpack('!I', data[8:12])[0]
            
            # Skip CSRC list
            header_len = 12 + (csrc_count * 4)
            
            # Payload
            payload = data[header_len:]
            
            return RTPPacket(
                version=version,
                padding=padding,
                extension=extension,
                csrc_count=csrc_count,
                marker=marker,
                payload_type=payload_type,
                sequence=sequence,
                timestamp=timestamp,
                ssrc=ssrc,
                payload=payload
            )
        except Exception as e:
            logger.error(f"RTP parse error: {e}")
            return None
    
    @staticmethod
    def build(payload_type: int, sequence: int, timestamp: int, 
              ssrc: int, payload: bytes, marker: bool = False) -> bytes:
        """Build RTP packet"""
        # Version=2, no padding, no extension, no CSRC
        b0 = 0x80  # Version 2
        b1 = (0x80 if marker else 0x00) | (payload_type & 0x7F)
        
        header = struct.pack('!BBHII',
            b0, b1, sequence & 0xFFFF, timestamp & 0xFFFFFFFF, ssrc
        )
        
        return header + payload


class JitterBuffer:
    """
    Adaptive jitter buffer for smooth audio playback.
    Buffers packets and reorders based on sequence number.
    """
    
    def __init__(self, target_delay_ms: int = 0, max_delay_ms: int = 200):
        self.target_delay = target_delay_ms / 1000.0
        self.max_delay = max_delay_ms / 1000.0
        self._buffer: dict = {}  # sequence -> (packet, arrival_time)
        self._next_seq = -1
        self._lock = threading.Lock()
        self._last_output_time = 0

        # Stats
        self.packets_received = 0
        self.packets_late = 0
        self.packets_lost = 0
    
    def put(self, packet: RTPPacket):
        """Add packet to jitter buffer"""
        with self._lock:
            self.packets_received += 1
            
            if self._next_seq == -1:
                self._next_seq = packet.sequence
            
            # Check if packet is too late
            seq_diff = (packet.sequence - self._next_seq) & 0xFFFF
            if seq_diff > 32768:  # Wrapped around, packet is late
                self.packets_late += 1
                return
            
            self._buffer[packet.sequence] = (packet, time.time())
            
            # Limit buffer size - if overflowing, advance _next_seq to match
            if len(self._buffer) > 100:
                while len(self._buffer) > 50:
                    oldest = min(self._buffer.keys())
                    del self._buffer[oldest]
                # Advance _next_seq to the new oldest packet
                if self._buffer:
                    new_oldest = min(self._buffer.keys())
                    if (new_oldest - self._next_seq) & 0xFFFF < 32768:
                        self._next_seq = new_oldest
    
    def get(self) -> Optional[RTPPacket]:
        """Get next packet from buffer"""
        with self._lock:
            if self._next_seq in self._buffer:
                packet, arrival_time = self._buffer.pop(self._next_seq)
                self._next_seq = (self._next_seq + 1) & 0xFFFF
                self._last_output_time = time.time()
                return packet
            
            # If buffer has packets but we can't find _next_seq,
            # check how far behind we are and jump ahead if needed
            if len(self._buffer) > 0:
                min_seq = min(self._buffer.keys())
                seq_gap = (min_seq - self._next_seq) & 0xFFFF
                
                # If we're more than 10 packets behind, jump to earliest available
                if seq_gap > 10 and seq_gap < 32768:
                    logger.warning(f"⚠️ Jitter buffer: jumping from seq {self._next_seq} to {min_seq} (gap={seq_gap}, buffered={len(self._buffer)})")
                    self.packets_lost += seq_gap
                    self._next_seq = min_seq
                    # Now try to return the packet we jumped to
                    if self._next_seq in self._buffer:
                        packet, arrival_time = self._buffer.pop(self._next_seq)
                        self._next_seq = (self._next_seq + 1) & 0xFFFF
                        self._last_output_time = time.time()
                        return packet
            
            # Check if we should skip a single missing packet
            now = time.time()
            if self._last_output_time > 0:
                elapsed = now - self._last_output_time
                if elapsed > 0.04:  # 40ms - 2 packets worth
                    self.packets_lost += 1
                    self._next_seq = (self._next_seq + 1) & 0xFFFF
            
            self._last_output_time = now
            return None
    
    def clear(self):
        """Clear the buffer"""
        with self._lock:
            self._buffer.clear()
            self._next_seq = -1


class RTPStream:
    """
    Handles bidirectional RTP stream for one call.
    Manages send/receive threads and codec conversion.
    """
    
    def __init__(self, local_port: int, remote_ip: str, remote_port: int,
                 codec: int = PAYLOAD_PCMU, sample_rate: int = 8000):
        self.local_port = local_port
        self.remote_ip = remote_ip
        self.remote_port = remote_port
        self.codec = codec  # Used for RECEIVE (decode incoming)
        self.send_codec = PAYLOAD_PCMU  # Always send as PCMU for maximum compatibility
        self.sample_rate = sample_rate
        
        # Socket
        self.socket: Optional[socket.socket] = None
        
        # Threading
        self._running = False
        self._recv_thread: Optional[threading.Thread] = None
        self._send_thread: Optional[threading.Thread] = None
        
        # Audio queues
        self._send_queue: queue.Queue = queue.Queue(maxsize=50)
        self._recv_queue: queue.Queue = queue.Queue(maxsize=50)
        
        # Jitter buffer for received audio
        self.jitter_buffer = JitterBuffer()
        
        # RTP state
        self._sequence = 0
        self._timestamp = 0
        self._ssrc = 0
        
        # Resampler state (maintain phase continuity across packets)
        # _decode_ratecv_state removed: G.722 decoded at native 16kHz, no downsample needed
        self._encode_ratecv_state = None  # Used when encoding G.722 send (upsamples 8k→16k)
        
        # Network resilience (PJSIP-inspired)
        self._last_packet_received = time.time()
        self._last_packet_sent = time.time()
        self._keepalive_interval = 0.02  # Send comfort noise every 20ms (same rate as real audio)
        self._network_timeout = 10.0  # Log a warning after 10s of no packets (informational only)
        self._is_network_up = True
        
        # Callbacks
        self.on_audio_received: Optional[Callable[[bytes], None]] = None
        self.on_dtmf_received: Optional[Callable[[str], None]] = None
        self.on_network_state_changed: Optional[Callable[[bool], None]] = None  # Callback for network up/down
        
        # Stats
        self.packets_sent = 0
        self.packets_received = 0
        self.bytes_sent = 0
        self.bytes_received = 0
    
    def start(self) -> bool:
        """Start RTP stream"""
        try:
            # Create UDP socket
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind(('0.0.0.0', self.local_port))
            self.socket.settimeout(0.1)
            
            # Initialize RTP state
            self._sequence = 0
            self._timestamp = 0
            self._ssrc = random.getrandbits(32)
            
            self._running = True
            
            # Start receive thread
            self._recv_thread = threading.Thread(
                target=self._receive_loop,
                daemon=True,
                name=f"RTP-Recv-{self.local_port}"
            )
            self._recv_thread.start()
            
            # Start send thread  
            self._send_thread = threading.Thread(
                target=self._send_loop,
                daemon=True,
                name=f"RTP-Send-{self.local_port}"
            )
            self._send_thread.start()
            
            logger.info(f"RTP stream started: :{self.local_port} <-> {self.remote_ip}:{self.remote_port}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to start RTP stream: {e}")
            return False
    
    def stop(self):
        """Stop RTP stream"""
        self._running = False
        
        if self._recv_thread:
            self._recv_thread.join(timeout=1.0)
        if self._send_thread:
            self._send_thread.join(timeout=1.0)
        
        if self.socket:
            self.socket.close()
            self.socket = None
        
        self.jitter_buffer.clear()
        logger.info(f"RTP stream stopped: port {self.local_port}")
    
    def send_audio(self, pcm_data: bytes):
        """
        Send audio (linear PCM) - will be encoded to codec.
        PCM format: 16-bit signed, mono, 8000Hz
        """
        try:
            self._send_queue.put_nowait(pcm_data)
        except queue.Full:
            # Drop if queue full, but log it (rate-limited) so the operator
            # can tell when mic audio is being lost — previously silent.
            if not hasattr(self, '_send_drop_count'):
                self._send_drop_count = 0
            self._send_drop_count += 1
            if self._send_drop_count == 1 or self._send_drop_count % 50 == 0:
                logger.warning(
                    f"RTP send queue full on port {self.local_port}, "
                    f"dropped {self._send_drop_count} mic packets total"
                )
    
    def get_audio(self) -> Optional[bytes]:
        """
        Get received audio as linear PCM.
        Returns None if no audio available.
        """
        try:
            return self._recv_queue.get_nowait()
        except queue.Empty:
            return None
    
    def send_dtmf(self, digit: str, duration_ms: int = 160):
        """Send DTMF digit via RFC 2833"""
        dtmf_map = {
            '0': 0, '1': 1, '2': 2, '3': 3, '4': 4,
            '5': 5, '6': 6, '7': 7, '8': 8, '9': 9,
            '*': 10, '#': 11, 'A': 12, 'B': 13, 'C': 14, 'D': 15
        }
        
        if digit not in dtmf_map:
            return
        
        event_id = dtmf_map[digit]
        samples = int(duration_ms * 8)  # 8 samples per ms at 8kHz
        
        # Send 3 packets: start, middle, end
        for i, end in enumerate([False, False, True]):
            # RFC 2833 payload: event(1) + E+R+volume(1) + duration(2)
            flags = 0x80 if end else 0x00  # E bit for end
            flags |= 10  # Volume = 10
            
            payload = struct.pack('!BBH', event_id, flags, samples)
            
            packet = RTPPacket.build(
                payload_type=PAYLOAD_DTMF,
                sequence=self._sequence,
                timestamp=self._timestamp,
                ssrc=self._ssrc,
                payload=payload,
                marker=(i == 0)
            )
            
            self.socket.sendto(packet, (self.remote_ip, self.remote_port))
            self._sequence = (self._sequence + 1) & 0xFFFF
            
            if not end:
                time.sleep(0.02)
        
        self._timestamp += samples
    
    def _receive_loop(self):
        """Receive RTP packets with network state monitoring"""
        logger.info(f"RTP receive loop started, socket={self.socket.fileno()}, local_port={self.local_port}")
        loop_count = 0
        while self._running:
            loop_count += 1
            if loop_count % 500 == 0:
                logger.info(f"RTP receive loop alive: {loop_count} iterations, packets_received={self.packets_received}")
            try:
                data, addr = self.socket.recvfrom(2048)
                self.packets_received += 1
                self.bytes_received += len(data)
                
                # Track receive time and check network state
                now = time.time()
                self._last_packet_received = now
                
                # Network came back up
                if not self._is_network_up:
                    logger.info("RTP: Network recovered, packets flowing again")
                    self._is_network_up = True
                    if self.on_network_state_changed:
                        self.on_network_state_changed(True)
                
                packet = RTPPacket.parse(data)
                if not packet:
                    continue
                
                if packet.payload_type == PAYLOAD_DTMF:
                    self._handle_dtmf(packet)
                elif packet.payload_type in (PAYLOAD_PCMU, PAYLOAD_PCMA, PAYLOAD_G722):
                    self._route_packet_direct(packet)
                    
            except socket.timeout:
                # Log a warning if RTP has been silent for a long time,
                # but do NOT change call state or send re-INVITE.
                # The call must survive network outages of any length.
                # Recovery happens naturally when packets start flowing again.
                if self._is_network_up:
                    time_since_last = time.time() - self._last_packet_received
                    if time_since_last > self._network_timeout:
                        logger.warning(f"RTP: No packets received for {time_since_last:.1f}s — network may be down, call kept alive")
                        self._is_network_up = False
                        # Do NOT fire on_network_state_changed(False) — no re-INVITE,
                        # no call teardown.  Just wait silently for packets to return.
                continue
            except Exception as e:
                if self._running:
                    logger.error(f"RTP receive error: {e}")
    
    def _process_jitter_buffer(self):
        """Process packets from jitter buffer — bypass jitter buffer, decode directly."""
        # NOTE: Jitter buffer disabled — decode and route every packet immediately.
        # SIP provider packets arrive in-order; buffering only adds latency and drop risk.
        pass

    def _route_packet_direct(self, packet: RTPPacket):
        """Decode and route a single RTP packet directly, bypassing the jitter buffer."""
        pcm = self._decode_audio(packet.payload, packet.payload_type)

        if not hasattr(self, '_rtp_recv_count'):
            self._rtp_recv_count = 0
        self._rtp_recv_count += 1
        if self._rtp_recv_count % 500 == 0:
            logger.info(f"RTP received: {self._rtp_recv_count} packets, decoded {len(pcm)} bytes PCM")

        if self.on_audio_received:
            try:
                self.on_audio_received(pcm)
            except Exception as e:
                logger.error(f"❌ RTP: Exception in on_audio_received callback: {e}", exc_info=True)

        try:
            self._recv_queue.put_nowait(pcm)
        except queue.Full:
            pass
    
    def _build_and_send_rtp(self, encoded: bytes, samples_per_packet: int = 160):
        """Build an RTP packet from encoded audio, send it, and update counters."""
        packet = RTPPacket.build(
            payload_type=self.send_codec,
            sequence=self._sequence,
            timestamp=self._timestamp,
            ssrc=self._ssrc,
            payload=encoded
        )
        
        self.socket.sendto(packet, (self.remote_ip, self.remote_port))
        self._last_packet_sent = time.time()
        
        self.packets_sent += 1
        self.bytes_sent += len(packet)
        self._sequence = (self._sequence + 1) & 0xFFFF
        self._timestamp += samples_per_packet
        return packet
    
    def _send_loop(self):
        """Send exactly one RTP packet per 20ms tick regardless of queue depth.

        Using a fixed ticker prevents bursting: if WebRTC (or any async source)
        delivers multiple PCM chunks at once the loop drains them one-per-tick,
        keeping the RTP stream evenly spaced at 50 pps.  Without this, a burst
        of 3 back-to-back packets followed by silence causes the remote jitter
        buffer to discard the extras and play silence — audible as chirping.
        """
        samples_per_packet = 160  # 20 ms at 8 kHz
        audio_count = 0
        silence_count = 0
        next_tick = time.monotonic() + 0.02

        while self._running:
            # Sleep until the next 20 ms tick
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            # Advance the tick; if we fell behind by more than one period
            # reset to now so we don't try to catch up with a burst.
            next_tick += 0.02
            if next_tick < time.monotonic():
                next_tick = time.monotonic()

            try:
                pcm = self._send_queue.get_nowait()
                encoded = self._encode_audio(pcm, self.send_codec)
                packet = self._build_and_send_rtp(encoded, samples_per_packet)
                audio_count += 1
                if audio_count % 50 == 0:
                    logger.info(f"📤 RTP: Sent {audio_count} audio packets, {silence_count} silence packets "
                                f"(PCM: {len(pcm)}B → Encoded: {len(encoded)}B, Packet: {len(packet)}B)")
            except queue.Empty:
                # No real audio — send comfort noise to defeat VAD timeouts
                comfort_noise = bytes([random.randint(0, 3) for _ in range(320)])
                encoded = self._encode_audio(comfort_noise, self.send_codec)
                self._build_and_send_rtp(encoded, samples_per_packet)
                silence_count += 1
                if silence_count % 50 == 0:
                    logger.info(f"📤 RTP: Sent {audio_count} audio packets, {silence_count} comfort noise packets (keepalive)")
            except Exception as e:
                if self._running:
                    logger.error(f"RTP send error: {e}")
    
    def _encode_audio(self, pcm: bytes, codec: int) -> bytes:
        """Encode PCM to RTP codec
        
        Note: Input PCM is 8kHz from audio pipeline.
        - G.722: Upsamples 8kHz → 16kHz, then encodes (wideband)
        - G.711 (PCMU/PCMA): Uses 8kHz directly (narrowband)
        """
        if codec == PAYLOAD_PCMU:
            return audioop.lin2ulaw(pcm, 2)
        elif codec == PAYLOAD_PCMA:
            return audioop.lin2alaw(pcm, 2)
        elif codec == PAYLOAD_G722:
            if G722_AVAILABLE and _g722_codec:
                try:
                    # Upsample 8kHz → 16kHz for G.722 encoder
                    pcm_16k, self._encode_ratecv_state = audioop.ratecv(pcm, 2, 1, 8000, 16000, self._encode_ratecv_state)
                    encoded = _g722_codec.encode(pcm_16k)
                    return encoded
                except Exception as e:
                    logger.warning(f"G.722 encode failed, falling back to PCMU: {e}")
                    return audioop.lin2ulaw(pcm, 2)
            else:
                logger.warning("G.722 not available, falling back to PCMU")
                return audioop.lin2ulaw(pcm, 2)
        return pcm
    
    def _decode_audio(self, data: bytes, codec: int) -> bytes:
        """Decode RTP codec to PCM.

        Output sample rate depends on codec:
        - G.722: returns 16kHz PCM (320 samples × 2 bytes = 640 bytes per 20ms packet)
        - G.711 PCMU/PCMA: returns 8kHz PCM (160 samples × 2 bytes = 320 bytes per 20ms packet)

        The audio pipeline (audio_usb_dongles.py) detects the rate from the buffer
        size and resamples directly to 48kHz — no lossy 16kHz→8kHz→48kHz round trip.
        """
        if codec == PAYLOAD_PCMU:
            return audioop.ulaw2lin(data, 2)
        elif codec == PAYLOAD_PCMA:
            return audioop.alaw2lin(data, 2)
        elif codec == PAYLOAD_G722:
            if G722_AVAILABLE and _g722_codec:
                try:
                    # G.722 decoder outputs 16kHz PCM — return it as-is (no downsample)
                    # Output worker will resample 16kHz → 48kHz directly, preserving 4–8kHz range
                    return _g722_codec.decode(data)
                except Exception as e:
                    logger.warning(f"G.722 decode failed, treating as PCMU: {e}")
                    return audioop.ulaw2lin(data, 2)
            else:
                logger.warning("G.722 not available, treating as PCMU")
                return audioop.ulaw2lin(data, 2)
        return data
    
    def _handle_dtmf(self, packet: RTPPacket):
        """Handle incoming DTMF"""
        if len(packet.payload) < 4:
            return
        
        event_id = packet.payload[0]
        end = bool(packet.payload[1] & 0x80)
        
        if end and self.on_dtmf_received:
            dtmf_chars = "0123456789*#ABCD"
            if event_id < len(dtmf_chars):
                self.on_dtmf_received(dtmf_chars[event_id])
    
    def get_stats(self) -> dict:
        """Get stream statistics"""
        return {
            "packets_sent": self.packets_sent,
            "packets_received": self.packets_received,
            "bytes_sent": self.bytes_sent,
            "bytes_received": self.bytes_received,
            "jitter_lost": self.jitter_buffer.packets_lost,
            "jitter_late": self.jitter_buffer.packets_late,
        }
