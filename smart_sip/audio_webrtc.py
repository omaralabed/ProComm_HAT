"""
audio_webrtc.py — WebRTC ↔ RTP audio bridge for browser phone lines
=====================================================================
One WebRTCBridge per active browser call.

  RTP in  → push_output(pcm_8k) → BrowserOutputTrack → browser speaker
  Browser mic → _on_frame()     → input_callback()   → RTP send

start_webrtc_for_line() is the single public entry point; call it when a
browser SIP line goes CONNECTED.  It fills the browser_lines entry dict with
the keys already expected by the existing Socket.IO handlers:
  entry['webrtc']        — the WebRTCBridge instance
  entry['loop']          — asyncio event loop running the bridge
  entry['answer_future'] — asyncio.Future resolved with the SDP answer
  entry['ice_queue']     — queue.Queue for browser ICE candidate dicts
"""

import asyncio
import audioop
import fractions
import logging
import queue
import threading
import time
from typing import Callable, Dict, Optional

import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
from aiortc.rtcicetransport import RTCIceCandidate as AiortcIceCandidate

logger = logging.getLogger(__name__)

SAMPLE_RATE  = 8000
SAMPLE_WIDTH = 2          # 16-bit PCM
FRAME_BYTES  = 160 * SAMPLE_WIDTH   # 20 ms @ 8 kHz
WEBRTC_RATE  = 48000


def _mix_two(a: bytes, b: bytes) -> bytes:
    """Sum two int16 PCM byte strings with int32 headroom and clip to int16.
    If lengths differ, only the overlapping region is mixed; the longer tail
    is discarded (both frames are nominally the same 320-byte / 20 ms size).
    Returns `a` unchanged if `b` is empty or length is zero after alignment."""
    length = min(len(a), len(b)) & ~1   # align to 2-byte (int16) boundary
    if length == 0:
        return a
    sa = np.frombuffer(a[:length], dtype=np.int16).astype(np.int32)
    sb = np.frombuffer(b[:length], dtype=np.int16).astype(np.int32)
    return np.clip(sa + sb, -32768, 32767).astype(np.int16).tobytes()


# ── Outgoing audio track (Pi → browser) ──────────────────────────────────────

class BrowserOutputTrack(MediaStreamTrack):
    """
    aiortc MediaStreamTrack that reads 8 kHz mono PCM from a queue,
    upsamples to 48 kHz, and delivers AudioFrames to the browser.
    """

    kind = 'audio'

    def __init__(self, output_queue: queue.Queue,
                 member_queue: Optional[queue.Queue] = None):
        super().__init__()          # must call parent to register the track
        self._q        = output_queue
        self._member_q = member_queue   # optional second queue: member→member mix
        self._rs       = None           # audioop.ratecv state
        self._pts      = 0

    async def recv(self):
        from av import AudioFrame
        loop = asyncio.get_event_loop()

        try:
            pcm_8k = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: self._q.get(timeout=0.04)),
                timeout=0.05,
            )
        except Exception:
            pcm_8k = b'\x00' * FRAME_BYTES

        # Mix in peer member audio if available (member→member path).
        # get_nowait() never blocks; on Empty we simply skip the mix and
        # deliver studio audio only — the existing path is fully preserved.
        if self._member_q is not None:
            try:
                member_pcm = self._member_q.get_nowait()
                pcm_8k = _mix_two(pcm_8k, member_pcm)
            except queue.Empty:
                pass

        pcm_48k, self._rs = audioop.ratecv(
            pcm_8k, SAMPLE_WIDTH, 1, SAMPLE_RATE, WEBRTC_RATE, self._rs
        )

        samples = np.frombuffer(pcm_48k, dtype=np.int16)
        frame = AudioFrame.from_ndarray(
            samples.reshape(1, -1), format='s16', layout='mono'
        )
        frame.pts = self._pts
        frame.sample_rate = WEBRTC_RATE
        frame.time_base = fractions.Fraction(1, WEBRTC_RATE)
        self._pts += len(samples)
        return frame


# ── ICE candidate parser ──────────────────────────────────────────────────────

def _parse_ice_candidate(cand_dict: dict) -> Optional[AiortcIceCandidate]:
    """Convert a JS RTCIceCandidateInit dict into an aiortc RTCIceCandidate."""
    try:
        import aioice
        sdp = cand_dict.get('candidate', '')
        if sdp.startswith('candidate:'):
            sdp = sdp[10:]
        if not sdp:
            return None
        ac = aioice.Candidate.from_sdp(sdp)
        return AiortcIceCandidate(
            component=ac.component,
            foundation=ac.foundation,
            ip=ac.host,
            port=ac.port,
            priority=ac.priority,
            protocol=ac.transport,
            type=ac.type,
            sdpMid=cand_dict.get('sdpMid'),
            sdpMLineIndex=cand_dict.get('sdpMLineIndex'),
        )
    except Exception as e:
        logger.debug(f"ICE parse error: {e}")
        return None


# ── Bridge ────────────────────────────────────────────────────────────────────

class WebRTCBridge:
    """
    Bridges one SIP/RTP call ↔ one browser WebRTC session.
    Do not instantiate directly — use start_webrtc_for_line().
    """

    def __init__(self, token: str, line_id: int, socketio, session_id: str):
        self.token    = token
        self.line_id  = line_id
        self._sio     = socketio
        self._sid     = session_id
        self._out_q: queue.Queue = queue.Queue(maxsize=10)  # 200 ms max buffer
        self._input_cb: Optional[Callable] = None
        self._pc: Optional[RTCPeerConnection] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._rs_in = None      # audioop.ratecv state for 48 kHz → 8 kHz

    # ── Public API (called from Flask / non-async threads) ───────────────

    def push_output(self, pcm_8k: bytes):
        """Queue 8 kHz mono PCM from RTP decoder → browser speaker."""
        try:
            self._out_q.put_nowait(pcm_8k)
        except queue.Full:
            # Drop oldest to keep audio current (prevent stale-audio delay)
            try:
                self._out_q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._out_q.put_nowait(pcm_8k)
            except queue.Full:
                pass

    def set_input_callback(self, cb: Callable):
        """Register cb(pcm_bytes: bytes) for browser mic audio → RTP send."""
        self._input_cb = cb

    def close(self):
        """Signal the bridge to shut down (call on hangup / call end)."""
        if self._pc and self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._pc.close(), self._loop)

    # ── Internal async methods ────────────────────────────────────────────

    async def _run(self, answer_future: asyncio.Future, ice_queue: queue.Queue):
        pc = RTCPeerConnection()
        self._pc = pc

        # Outgoing: RTP audio → browser
        pc.addTrack(BrowserOutputTrack(self._out_q))

        # Incoming: browser mic → RTP
        @pc.on('track')
        def on_track(track):
            logger.info(f"WebRTC line {self.line_id}: on_track fired kind={track.kind}")
            if track.kind == 'audio':
                asyncio.ensure_future(self._read_browser_audio(track))

        @pc.on('connectionstatechange')
        def on_conn_state():
            logger.info(f"WebRTC line {self.line_id}: connectionState={pc.connectionState}")

        # Build offer, then wait for all local ICE candidates so the offer
        # SDP is self-contained (no trickle needed from our side).
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        await self._wait_ice_complete(pc)

        self._sio.emit('phone_offer', {
            'token': self.token,
            'sdp': pc.localDescription.sdp,
        }, to=self._sid)

        # Wait for the browser's SDP answer (delivered via phone_answer event)
        try:
            sdp_answer = await asyncio.wait_for(answer_future, timeout=30.0)
        except asyncio.TimeoutError:
            logger.warning(f"WebRTC line {self.line_id}: no answer in 30 s")
            await pc.close()
            return

        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=sdp_answer, type='answer')
        )

        # Flush audio that buffered during WebRTC negotiation so the
        # browser doesn't hear a stale burst of audio on connect.
        while not self._out_q.empty():
            try:
                self._out_q.get_nowait()
            except queue.Empty:
                break

        # Process browser ICE candidates that arrive via phone_ice_candidate
        asyncio.ensure_future(self._drain_ice(pc, ice_queue))

        # Keep loop alive until connection closes
        while pc.connectionState not in ('closed', 'failed'):
            await asyncio.sleep(1.0)

    @staticmethod
    async def _wait_ice_complete(pc: RTCPeerConnection, timeout: float = 5.0):
        """Wait until local ICE gathering finishes (or timeout)."""
        if pc.iceGatheringState == 'complete':
            return
        done = asyncio.Event()

        @pc.on('icegatheringstatechange')
        def _ch():
            if pc.iceGatheringState == 'complete':
                done.set()

        try:
            await asyncio.wait_for(done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.debug(f"ICE gathering timeout — using partial candidates")

    @staticmethod
    async def _drain_ice(pc: RTCPeerConnection, ice_queue: queue.Queue):
        """Continuously pull browser ICE candidates from the thread-safe queue."""
        while pc.connectionState not in ('closed', 'failed'):
            await asyncio.sleep(0.05)
            while not ice_queue.empty():
                cand = _parse_ice_candidate(ice_queue.get_nowait())
                if cand:
                    try:
                        await pc.addIceCandidate(cand)
                    except Exception as e:
                        logger.debug(f"addIceCandidate: {e}")

    async def _read_browser_audio(self, track):
        """Drain incoming audio frames from the browser mic track."""
        frame_count = 0
        logger.info(f"WebRTC line {self.line_id}: _read_browser_audio started")
        while True:
            try:
                frame = await track.recv()
                frame_count += 1
                if frame_count == 1:
                    logger.info(
                        f"WebRTC line {self.line_id}: first browser audio frame "
                        f"fmt={frame.format.name} rate={frame.sample_rate} "
                        f"samples={frame.samples} layout={frame.layout.name}"
                    )
                elif frame_count % 500 == 0:
                    logger.info(f"WebRTC line {self.line_id}: {frame_count} browser audio frames received")
                self._on_frame(frame)
            except Exception as e:
                logger.warning(f"WebRTC line {self.line_id}: _read_browser_audio exit after {frame_count} frames: {e}")
                break

    def _on_frame(self, frame):
        """Convert a browser AudioFrame → 8 kHz PCM chunks → RTP."""
        try:
            src_rate = frame.sample_rate or WEBRTC_RATE
            fmt      = frame.format.name
            n_ch     = len(frame.layout.channels)

            if fmt == 's16':
                # Packed interleaved s16 — PyAV to_ndarray() returns (1, samples*channels)
                # which wrongly flattens stereo into fake-mono.  Use raw bytes instead.
                pcm_src = bytes(frame.planes[0])
                if n_ch >= 2:
                    # audioop.tomono correctly handles interleaved L,R,L,R... stereo
                    pcm_src = audioop.tomono(pcm_src, SAMPLE_WIDTH, 0.5, 0.5)
            else:
                # Planar (s16p, fltp, etc.) — to_ndarray() returns (channels, samples)
                arr = frame.to_ndarray()
                if arr.dtype == np.float32 or arr.dtype == np.float64:
                    arr = np.clip(arr, -1.0, 1.0)
                    arr = (arr * 32767).astype(np.int16)
                elif arr.dtype != np.int16:
                    arr = arr.astype(np.int16)
                # Packed non-s16 edge case: (1, N*ch) with interleaved content
                if n_ch >= 2 and arr.shape[0] == 1:
                    arr = arr.flatten().reshape(-1, n_ch).T
                if arr.shape[0] > 1:
                    arr = arr.mean(axis=0, keepdims=True).astype(np.int16)
                pcm_src = arr.flatten().tobytes()

            pcm_8k, self._rs_in = audioop.ratecv(
                pcm_src, SAMPLE_WIDTH, 1, src_rate, SAMPLE_RATE, self._rs_in
            )
            sent = 0
            while len(pcm_8k) >= FRAME_BYTES:
                chunk, pcm_8k = pcm_8k[:FRAME_BYTES], pcm_8k[FRAME_BYTES:]
                if self._input_cb:
                    try:
                        self._input_cb(chunk)
                        sent += 1
                    except Exception:
                        pass
            if not hasattr(self, '_frame_log_count'):
                self._frame_log_count = 0
            self._frame_log_count += 1
            if self._frame_log_count == 1:
                logger.info(f"WebRTC line {self.line_id}: first _on_frame "
                            f"fmt={frame.format.name} rate={src_rate} ch={n_ch} "
                            f"src_bytes={len(pcm_src)} → sent {sent} chunks to RTP")
        except Exception as e:
            logger.warning(f"WebRTC line {self.line_id}: _on_frame error: {e}", exc_info=True)


# ── Public entry point ────────────────────────────────────────────────────────

def start_webrtc_for_line(
    token: str,
    line_id: int,
    socketio,
    session_id: str,
    entry: dict,
) -> WebRTCBridge:
    """
    Start a WebRTC bridge for a browser phone line in a background thread.

    Fills entry['webrtc'], ['loop'], ['answer_future'], ['ice_queue'] so the
    existing Socket.IO handlers (phone_answer, phone_ice_candidate) just work.

    Returns the bridge immediately (negotiation happens asynchronously).
    """
    bridge: WebRTCBridge = WebRTCBridge(token, line_id, socketio, session_id)
    ice_q: queue.Queue   = queue.Queue()

    def _thread():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        bridge._loop  = loop
        answer_future = loop.create_future()

        # Populate entry before the loop starts so Flask event handlers
        # can deliver phone_answer / phone_ice_candidate immediately.
        entry['webrtc']        = bridge
        entry['loop']          = loop
        entry['answer_future'] = answer_future
        entry['ice_queue']     = ice_q

        try:
            loop.run_until_complete(bridge._run(answer_future, ice_q))
        except Exception as e:
            logger.error(f"WebRTC bridge line {line_id}: {e}")
        finally:
            loop.close()
            for k in ('webrtc', 'loop', 'answer_future', 'ice_queue'):
                entry.pop(k, None)
            logger.info(f"WebRTC bridge line {line_id} stopped")

    threading.Thread(
        target=_thread, daemon=True, name=f"WebRTC-{line_id}"
    ).start()
    logger.info(f"WebRTC bridge line {line_id} starting → {session_id[:8]}…")
    return bridge


# ── Listen-only monitor bridge (phone browser hears USB line audio) ───────────

class WebRTCMonitor:
    """
    One-way WebRTC bridge: streams incoming RTP audio from a USB line
    to a phone browser (listen only — no mic, no RTP send path touched).

    Usage:
        mon = WebRTCMonitor(line_id, socketio, session_id)
        mon.start()                   # begins WebRTC negotiation
        mon.push_audio(pcm_8k)        # call from _audio_monitor_callback
        mon.close()                   # call on unsubscribe / disconnect
    """

    def __init__(self, line_id: int, socketio, session_id: str):
        self.line_id  = line_id
        self._sio     = socketio
        self._sid     = session_id
        self._out_q: queue.Queue = queue.Queue(maxsize=10)  # ~200 ms buffer
        self._pc: Optional[RTCPeerConnection] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._answer_future: Optional[asyncio.Future] = None
        self._ice_queue: queue.Queue = queue.Queue()
        self._closed: bool = False

    # ── Public API ────────────────────────────────────────────────────────

    def push_audio(self, pcm_8k: bytes):
        """Feed 8 kHz mono PCM from RTP into the outgoing WebRTC track."""
        try:
            self._out_q.put_nowait(pcm_8k)
        except queue.Full:
            try:
                self._out_q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._out_q.put_nowait(pcm_8k)
            except queue.Full:
                pass

    def deliver_answer(self, sdp: str):
        """Deliver the browser's SDP answer (called from Socket.IO thread)."""
        fut = self._answer_future
        loop = self._loop
        if loop and fut and not loop.is_closed():
            def _resolve(f=fut, s=sdp):
                if not f.done():
                    f.set_result(s)
            loop.call_soon_threadsafe(_resolve)

    def add_ice_candidate(self, cand_dict: dict):
        """Queue a browser ICE candidate (called from Socket.IO thread)."""
        self._ice_queue.put_nowait(cand_dict)

    def close(self):
        """Shut down the WebRTC connection."""
        self._closed = True
        pc, loop = self._pc, self._loop
        if pc and loop and not loop.is_closed():
            asyncio.run_coroutine_threadsafe(pc.close(), loop)

    def start(self):
        """Start the WebRTC negotiation in a background thread."""
        def _thread():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._answer_future = loop.create_future()
            try:
                loop.run_until_complete(self._run())
            except Exception as e:
                logger.error(f"WebRTC monitor line {self.line_id}: {e}")
            finally:
                loop.close()
                logger.info(f"WebRTC monitor line {self.line_id} stopped")

        threading.Thread(
            target=_thread, daemon=True,
            name=f"WebRTCMon-{self.line_id}-{self._sid[:6]}"
        ).start()
        logger.info(f"WebRTC monitor line {self.line_id} starting → {self._sid[:8]}…")

    # ── Internal async ────────────────────────────────────────────────────

    async def _run(self):
        if self._closed:
            return
        pc = RTCPeerConnection()
        self._pc = pc

        # Add outgoing audio track (RTP → browser speaker, listen only)
        pc.addTrack(BrowserOutputTrack(self._out_q))

        @pc.on('connectionstatechange')
        def on_conn_state():
            logger.info(f"WebRTC monitor line {self.line_id}: state={pc.connectionState}")

        # Create offer, wait for ICE gathering
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        await WebRTCBridge._wait_ice_complete(pc)

        # Send offer to phone browser
        self._sio.emit('monitor_offer', {
            'line': self.line_id,
            'sdp': pc.localDescription.sdp,
        }, to=self._sid)

        # Wait for browser SDP answer
        try:
            sdp_answer = await asyncio.wait_for(self._answer_future, timeout=30.0)
        except asyncio.TimeoutError:
            logger.warning(f"WebRTC monitor line {self.line_id}: no answer in 30s")
            await pc.close()
            return

        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=sdp_answer, type='answer')
        )

        # Flush stale audio buffered during negotiation
        while not self._out_q.empty():
            try:
                self._out_q.get_nowait()
            except queue.Empty:
                break

        # Process ICE candidates from browser
        asyncio.ensure_future(WebRTCBridge._drain_ice(pc, self._ice_queue))

        # Keep alive until connection closes
        while pc.connectionState not in ('closed', 'failed') and not self._closed:
            await asyncio.sleep(1.0)


# ── Party Line two-way bridge (PL crew member ↔ SIP conference line) ─────────

class WebRTCPLBridge:
    """
    Two-way WebRTC bridge for Party Line (PL) crew members.
    Sends SIP line RTP audio → browser (listen) and receives
    browser mic audio → RTP TX (PTT talk).
    Uses pl_offer / pl_answer / pl_ice_candidate socket events.
    """

    def __init__(self, line_id: int, socketio, session_id: str):
        self.line_id  = line_id
        self._sio     = socketio
        self._sid     = session_id
        self._out_q: queue.Queue = queue.Queue(maxsize=10)
        self._input_cb: Optional[Callable] = None
        self._pc: Optional[RTCPeerConnection] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._answer_future: Optional[asyncio.Future] = None
        self._ice_queue: queue.Queue = queue.Queue()
        self._closed: bool = False
        self._rs_in = None   # audioop.ratecv state for 48 kHz → 8 kHz
        # Server-side PTT gate.  The browser's mic track keeps shipping ~50 fps
        # of (silenced) frames even when the user releases the button, so the
        # gate has to live here too — otherwise silence frames race a peer's
        # real frames into the shared RTP TX queue and the studio cuts in/out.
        self._ptt_active: bool = False
        # Second output queue: receives mixed audio from peers on the same PL
        # line (member→member path).  Fed by PLConferenceMixer tick thread;
        # drained by BrowserOutputTrack.recv() and mixed with studio audio.
        self._member_q: queue.Queue = queue.Queue(maxsize=10)

    # ── Public API ────────────────────────────────────────────────────────

    def set_ptt(self, active: bool):
        """Open/close the server-side PTT gate (called from the socket handler)."""
        self._ptt_active = bool(active)

    def push_output(self, pcm_8k: bytes):
        """Feed 8 kHz PCM from SIP RTP → browser speaker."""
        try:
            self._out_q.put_nowait(pcm_8k)
        except queue.Full:
            try:
                self._out_q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._out_q.put_nowait(pcm_8k)
            except queue.Full:
                pass

    def set_input_callback(self, cb: Callable):
        """Register cb(pcm_bytes) called with browser mic audio for RTP TX."""
        self._input_cb = cb

    def deliver_answer(self, sdp: str):
        """Deliver browser SDP answer (called from Socket.IO thread)."""
        fut, loop = self._answer_future, self._loop
        if loop and fut and not loop.is_closed():
            def _resolve(f=fut, s=sdp):
                if not f.done():
                    f.set_result(s)
            loop.call_soon_threadsafe(_resolve)

    def add_ice_candidate(self, cand_dict: dict):
        """Queue browser ICE candidate (called from Socket.IO thread)."""
        self._ice_queue.put_nowait(cand_dict)

    def close(self):
        """Shut down WebRTC connection."""
        self._closed = True
        loop = self._loop
        if loop and not loop.is_closed():
            # Cancel the answer future immediately — prevents a 30-second thread
            # hang if close() is called while _run() is waiting for the browser
            # to send back its SDP answer (e.g. user leaves during getUserMedia).
            fut = self._answer_future
            if fut:
                def _cancel(f=fut):
                    if not f.done():
                        f.cancel()
                loop.call_soon_threadsafe(_cancel)
            pc = self._pc
            if pc:
                asyncio.run_coroutine_threadsafe(pc.close(), loop)

    def start(self):
        """Start WebRTC negotiation in a background thread."""
        def _thread():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._answer_future = loop.create_future()
            try:
                loop.run_until_complete(self._run())
            except Exception as e:
                logger.error(f"WebRTC PL bridge line {self.line_id}: {e}")
            finally:
                loop.close()
                logger.info(f"WebRTC PL bridge line {self.line_id} stopped")

        threading.Thread(
            target=_thread, daemon=True,
            name=f"WebRTCPL-{self.line_id}-{self._sid[:6]}"
        ).start()
        logger.info(f"WebRTC PL bridge line {self.line_id} starting → {self._sid[:8]}…")

    # ── Internal async ────────────────────────────────────────────────────

    async def _run(self):
        if self._closed:
            return
        pc = RTCPeerConnection()
        self._pc = pc

        # Outgoing: SIP RTP audio → browser speaker (studio mix)
        # _member_q carries peer member audio, mixed in by PLConferenceMixer
        pc.addTrack(BrowserOutputTrack(self._out_q, self._member_q))

        # Incoming: browser mic → RTP TX
        @pc.on('track')
        def on_track(track):
            if track.kind == 'audio':
                asyncio.ensure_future(self._read_browser_mic(track))

        @pc.on('connectionstatechange')
        def on_conn_state():
            logger.info(f"WebRTC PL line {self.line_id}: state={pc.connectionState}")

        # Create offer, wait for ICE gathering
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        await WebRTCBridge._wait_ice_complete(pc)

        # Send offer to browser
        self._sio.emit('pl_offer', {
            'line': self.line_id,
            'sdp': pc.localDescription.sdp,
        }, to=self._sid)

        # Wait for browser SDP answer
        try:
            sdp_answer = await asyncio.wait_for(self._answer_future, timeout=30.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # TimeoutError: browser never responded in 30s.
            # CancelledError: close() was called (user left) while waiting.
            logger.info(f"WebRTC PL line {self.line_id}: answer wait cancelled or timed out")
            await pc.close()
            return

        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=sdp_answer, type='answer')
        )

        # Flush stale audio buffered during negotiation
        while not self._out_q.empty():
            try:
                self._out_q.get_nowait()
            except queue.Empty:
                break

        asyncio.ensure_future(WebRTCBridge._drain_ice(pc, self._ice_queue))

        while pc.connectionState not in ('closed', 'failed') and not self._closed:
            await asyncio.sleep(1.0)

    async def _read_browser_mic(self, track):
        """Drain browser mic frames and forward to RTP TX."""
        frame_count = 0
        while True:
            try:
                frame = await track.recv()
                frame_count += 1
                if frame_count == 1:
                    logger.info(f"WebRTC PL line {self.line_id}: first mic frame received")
                self._process_mic_frame(frame)
            except Exception as e:
                logger.warning(f"WebRTC PL line {self.line_id}: mic ended after {frame_count} frames: {e}")
                break

    def _process_mic_frame(self, frame):
        """Convert browser AudioFrame → 8 kHz PCM chunks → RTP TX."""
        # Raw-frame diagnostic — runs regardless of PTT gate so we can see
        # if the browser is actually sending audio or silence.
        if not hasattr(self, '_diag_count'):
            self._diag_count = 0
        self._diag_count += 1
        if self._diag_count <= 5 or self._diag_count % 500 == 0:
            try:
                _fmt  = frame.format.name
                _rate = frame.sample_rate or WEBRTC_RATE
                _nch  = len(frame.layout.channels)
                if _fmt == 's16':
                    _raw = bytes(frame.planes[0])
                    _peak = int(np.frombuffer(_raw, dtype=np.int16).__abs__().max()) if _raw else 0
                else:
                    _arr = frame.to_ndarray().astype(np.float32)
                    _peak = int((np.abs(_arr) * 32767).max()) if _arr.size else 0
                logger.info(
                    f"WebRTC PL line {self.line_id}: raw frame #{self._diag_count} "
                    f"fmt={_fmt} ch={_nch} rate={_rate} peak={_peak} ptt={self._ptt_active}"
                )
            except Exception as _de:
                logger.info(f"WebRTC PL line {self.line_id}: diag err: {_de}")

        if not self._ptt_active:
            return
        if not hasattr(self, '_ptt_frame_count'):
            self._ptt_frame_count = 0
        self._ptt_frame_count += 1
        if self._ptt_frame_count == 1 or self._ptt_frame_count % 100 == 0:
            logger.info(f"WebRTC PL line {self.line_id}: PTT frame #{self._ptt_frame_count} cb={'yes' if self._input_cb else 'NO'}")
        try:
            src_rate = frame.sample_rate or WEBRTC_RATE
            fmt      = frame.format.name
            n_ch     = len(frame.layout.channels)

            # ── Step 1: extract mono int16 from whatever aiortc delivers ──────
            if fmt == 's16':
                pcm_src = bytes(frame.planes[0])
                arr_mono = np.frombuffer(pcm_src, dtype=np.int16)
                if n_ch >= 2:
                    # Stereo interleaved: take left channel (index 0, 2, 4, …)
                    arr_mono = arr_mono[0::2].copy()
            else:
                arr = frame.to_ndarray()
                if arr.dtype in (np.float32, np.float64):
                    arr = np.clip(arr, -1.0, 1.0)
                    arr = (arr * 32767).astype(np.int16)
                elif arr.dtype != np.int16:
                    arr = arr.astype(np.int16)
                # arr shape: (channels, samples) or (1, samples)
                if arr.ndim > 1 and arr.shape[0] > 1:
                    arr = arr.mean(axis=0, keepdims=True)
                arr_mono = arr.flatten().astype(np.int16)

            # ── Step 2: resample to 8 kHz using numpy (no audioop state) ──────
            # For the common case (48 kHz → 8 kHz) this is exact factor-of-6
            # decimation. For other rates we fall back to audioop.ratecv.
            ratio = src_rate // SAMPLE_RATE
            if src_rate == ratio * SAMPLE_RATE and ratio > 1:
                # Box-filter decimation: average each group of `ratio` samples.
                # Provides basic anti-aliasing and requires no persistent state.
                n_trim = (len(arr_mono) // ratio) * ratio
                arr_8k = arr_mono[:n_trim].astype(np.int32).reshape(-1, ratio).mean(axis=1).astype(np.int16)
                pcm_8k = arr_8k.tobytes()
            else:
                # Non-integer ratio: fall back to audioop.ratecv (e.g. 44100 Hz)
                pcm_8k, self._rs_in = audioop.ratecv(
                    arr_mono.tobytes(), SAMPLE_WIDTH, 1, src_rate, SAMPLE_RATE, self._rs_in
                )

            if self._ptt_frame_count <= 5 or self._ptt_frame_count % 100 == 0:
                peak_in  = int(np.abs(arr_mono).max()) if arr_mono.size else 0
                arr_out  = np.frombuffer(pcm_8k, dtype=np.int16)
                peak_out_pre = int(np.abs(arr_out).max()) if arr_out.size else 0
                logger.info(
                    f"WebRTC PL line {self.line_id}: PTT frame #{self._ptt_frame_count} "
                    f"src={src_rate}Hz ch={n_ch} mono_samples={len(arr_mono)} "
                    f"peak_in={peak_in} → 8k_bytes={len(pcm_8k)} peak_8k={peak_out_pre}"
                )

            chunks_sent = 0
            while len(pcm_8k) >= FRAME_BYTES:
                chunk, pcm_8k = pcm_8k[:FRAME_BYTES], pcm_8k[FRAME_BYTES:]
                chunks_sent += 1
                if self._input_cb:
                    try:
                        self._input_cb(chunk)
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"WebRTC PL line {self.line_id}: _process_mic_frame: {e}")


# ── PL line mixer (per-line, mixes mic from all crew members → RTP TX) ───────

class PLLineMixer:
    """
    Mixes mic audio from all WebRTCPLBridge members on one PL line into a
    single 20 ms cadence stream feeding rtp_stream.send_audio().

    Why this exists
    ----------------
    The SIP RTP send loop drains its queue at exactly 50 packets/s.  If every
    PL bridge wrote directly into that queue, N members would push at 50·N fps,
    overflowing the queue (maxsize=50) within a second and dropping mic frames.
    Even before overflow, frames from a non-talking member would interleave
    with the active talker's frames on the same single-producer path, so the
    studio would hear chopped or silent audio — the exact symptom the operator
    reported.

    Each bridge feeds 20 ms 8 kHz mono frames into its own per-sid slot via
    push(); the mixer ticks at strict 20 ms and emits ONE summed frame per
    tick (silence is contributed by the RTP send loop's comfort noise path,
    so a tick with no input is simply skipped here).
    """

    SAMPLE_WIDTH  = 2
    FRAME_SAMPLES = 160               # 20 ms @ 8 kHz mono
    FRAME_BYTES   = FRAME_SAMPLES * SAMPLE_WIDTH
    TICK_PERIOD   = 0.020             # 20 ms

    def __init__(self, line_id: int, send_fn: Callable[[bytes], None]):
        self.line_id   = line_id
        self._send_fn  = send_fn      # rtp_stream.send_audio
        # Per-sid frame queue.  maxsize=2 absorbs sub-tick jitter without
        # building latency; oldest frame is dropped on overflow inside push().
        self._queues: Dict[str, queue.Queue] = {}
        self._lock     = threading.Lock()
        self._running  = True
        # Peak-amplitude diagnostic.  Reports the loudest sample sent in the
        # last ~1 s window so the operator can see whether audio is actually
        # live or is silence riding through the pipeline.
        self._peak_window_max  = 0
        self._peak_window_count = 0
        self._thread   = threading.Thread(
            target=self._loop, daemon=True, name=f"PLMixer-{line_id}"
        )
        self._thread.start()
        logger.info(f"PL mixer line {line_id} started")

    # ── Public API ────────────────────────────────────────────────────────

    def add_member(self, sid: str):
        with self._lock:
            if sid not in self._queues:
                # maxsize=12: holds 2 full aiortc callback bursts (each callback
                # delivers a ~120ms block = 6 × 20ms chunks at once).  The old
                # maxsize=2 caused the audio chunk (always chunk #1 of 6) to be
                # overwritten by the trailing 5 silence chunks before the mixer
                # tick could drain it.
                self._queues[sid] = queue.Queue(maxsize=12)
                logger.info(f"PL mixer line {self.line_id}: + sid={sid[:8]} (members={len(self._queues)})")

    def remove_member(self, sid: str):
        with self._lock:
            if self._queues.pop(sid, None) is not None:
                logger.info(f"PL mixer line {self.line_id}: – sid={sid[:8]} (members={len(self._queues)})")

    def member_count(self) -> int:
        with self._lock:
            return len(self._queues)

    def push(self, sid: str, pcm_20ms: bytes):
        """Push one 20 ms 8 kHz mono PCM frame from a single PL bridge."""
        with self._lock:
            q = self._queues.get(sid)
        if q is None:
            # sid not registered — log once to catch mismatched-sid bugs
            if not hasattr(self, '_push_miss_logged'):
                self._push_miss_logged = True
                logger.warning(
                    f"PL mixer line {self.line_id}: push() sid={sid[:8]} NOT IN queues "
                    f"(registered: {[s[:8] for s in self._queues]})"
                )
            return
        # First-call diagnostic: log peak of first 3 pushes to confirm data arrives
        if not hasattr(self, '_push_count'):
            self._push_count = 0
        self._push_count += 1
        if self._push_count <= 3:
            try:
                peak = int(np.abs(np.frombuffer(pcm_20ms, dtype=np.int16)).max())
                logger.info(
                    f"PL mixer line {self.line_id}: push #{self._push_count} "
                    f"sid={sid[:8]} peak={peak} len={len(pcm_20ms)}"
                )
            except Exception:
                pass
        try:
            q.put_nowait(pcm_20ms)
        except queue.Full:
            # Drop oldest, keep latest audio current.
            try:
                q.get_nowait()
            except queue.Empty:
                pass
            try:
                q.put_nowait(pcm_20ms)
            except queue.Full:
                pass

    def stop(self):
        """Signal the mix loop to exit (idempotent)."""
        if not self._running:
            return
        self._running = False
        logger.info(f"PL mixer line {self.line_id} stopping")

    # ── Mix loop ──────────────────────────────────────────────────────────

    def _loop(self):
        next_tick = time.monotonic() + self.TICK_PERIOD
        while self._running:
            now = time.monotonic()
            delay = next_tick - now
            if delay > 0:
                time.sleep(delay)
            next_tick += self.TICK_PERIOD
            # Drift correction: if we fell behind by more than one period
            # (GC pause, OS schedule hiccup) reset rather than catch up with a
            # burst, mirroring the strategy in rtp.RTPStream._send_loop.
            if next_tick < time.monotonic() - self.TICK_PERIOD:
                next_tick = time.monotonic() + self.TICK_PERIOD

            # Snapshot queues under the lock; pop frames outside it.
            with self._lock:
                qs = list(self._queues.values())

            frames = []
            for q in qs:
                try:
                    frames.append(q.get_nowait())
                except queue.Empty:
                    pass

            if not frames:
                # No PTT-gated audio this tick.  Skipping send is correct:
                # the RTP send loop emits its own comfort noise when its
                # queue is empty.
                continue

            try:
                mixed = self._mix(frames)
                if mixed:
                    # Peak-amplitude monitor (rolling 1 s = 50 ticks).
                    # Logs the loudest int16 sample so the operator can tell
                    # at a glance whether real audio is going out or silence.
                    try:
                        arr = np.frombuffer(mixed, dtype=np.int16)
                        if arr.size:
                            peak = int(np.abs(arr).max())
                            if peak > self._peak_window_max:
                                self._peak_window_max = peak
                            self._peak_window_count += 1
                            if self._peak_window_count >= 50:
                                logger.info(
                                    f"PL mixer line {self.line_id}: peak "
                                    f"amplitude over last 1 s = "
                                    f"{self._peak_window_max} (0=silence, "
                                    f"~32767=clipping)"
                                )
                                self._peak_window_max = 0
                                self._peak_window_count = 0
                    except Exception:
                        pass
                    self._send_fn(mixed)
            except Exception as e:
                logger.warning(f"PL mixer line {self.line_id}: send error: {e}")

        logger.info(f"PL mixer line {self.line_id} stopped")

    @staticmethod
    def _mix(frames):
        """Sum N int16 PCM frames with clip-to-int16. Returns bytes or b''."""
        if not frames:
            return b''
        if len(frames) == 1:
            return frames[0]
        length = min(len(f) for f in frames)
        if length <= 0:
            return b''
        # Pad length down to an even byte count for int16 view safety.
        length &= ~1
        if length == 0:
            return b''
        n_samples = length // 2
        summed = np.zeros(n_samples, dtype=np.int32)
        for f in frames:
            summed += np.frombuffer(f[:length], dtype=np.int16).astype(np.int32)
        np.clip(summed, -32768, 32767, out=summed)
        return summed.astype(np.int16).tobytes()


# ── PL conference mixer (member→member audio, mix-minus per member) ──────────

class PLConferenceMixer:
    """
    Distributes mic audio from every PL crew member to every OTHER member
    (mix-minus: you never hear your own mic).

    Audio flow (NEW — additive to existing paths):
        Member A mic → push_member('A', pcm)  ┐
        Member B mic → push_member('B', pcm)  ├→ tick → bridge._member_q
        Studio RTP   → (NOT handled here —    │         per member (minus self)
                         _pl_fan_out unchanged)┘

    The existing paths are completely untouched:
        PLLineMixer still sends member mics → studio RTP
        _pl_fan_out still sends studio RTP → each member's _out_q

    Only peer-to-peer member audio is new.
    """

    TICK_PERIOD = 0.020   # 20 ms — matches PLLineMixer and RTP cadence

    def __init__(self, line_id: int):
        self.line_id   = line_id
        # Per-member input queue.  Mirrors PLLineMixer's _queues design:
        # push_member() enqueues each 320-byte chunk (called 6× per aiortc
        # frame in rapid succession); the tick drains ONE per 20 ms tick so
        # peers receive audio at the correct 50 fps cadence.
        # maxsize=12 absorbs two full aiortc bursts without building latency.
        self._frame_queues: Dict[str, queue.Queue] = {}   # sid → Queue[bytes]
        self._member_qs: Dict[str, queue.Queue] = {}  # sid → bridge._member_q
        self._lock    = threading.Lock()
        self._running = True
        self._thread  = threading.Thread(
            target=self._loop, daemon=True, name=f"PLConfMixer-{line_id}"
        )
        self._thread.start()
        logger.info(f"PL conf mixer line {line_id} started")

    # ── Public API ────────────────────────────────────────────────────────

    def add_member(self, sid: str, member_queue: queue.Queue):
        """Register a crew member.  member_queue is bridge._member_q."""
        with self._lock:
            self._frame_queues[sid] = queue.Queue(maxsize=12)
            self._member_qs[sid]    = member_queue
        logger.info(f"PL conf mixer line {self.line_id}: + sid={sid[:8]} "
                    f"(members={len(self._member_qs)})")

    def remove_member(self, sid: str):
        """Deregister a crew member (called from _remove_pl_bridge)."""
        with self._lock:
            self._frame_queues.pop(sid, None)
            self._member_qs.pop(sid, None)
        logger.info(f"PL conf mixer line {self.line_id}: – sid={sid[:8]} "
                    f"(members={len(self._member_qs)})")

    def member_count(self) -> int:
        with self._lock:
            return len(self._member_qs)

    def push_member(self, sid: str, pcm_20ms: bytes):
        """Enqueue one 320-byte mic chunk for sid (called from _mic_to_both).
        Drop-oldest on overflow — same strategy as PLLineMixer.push()."""
        with self._lock:
            q = self._frame_queues.get(sid)
        if q is None:
            return
        try:
            q.put_nowait(pcm_20ms)
        except queue.Full:
            try:
                q.get_nowait()
            except queue.Empty:
                pass
            try:
                q.put_nowait(pcm_20ms)
            except queue.Full:
                pass

    def stop(self):
        """Signal tick thread to exit (idempotent)."""
        if not self._running:
            return
        self._running = False
        logger.info(f"PL conf mixer line {self.line_id} stopping")

    # ── Tick loop ─────────────────────────────────────────────────────────

    def _loop(self):
        next_tick = time.monotonic() + self.TICK_PERIOD
        while self._running:
            now   = time.monotonic()
            delay = next_tick - now
            if delay > 0:
                time.sleep(delay)
            next_tick += self.TICK_PERIOD
            # Drift correction — same strategy as PLLineMixer
            if next_tick < time.monotonic() - self.TICK_PERIOD:
                next_tick = time.monotonic() + self.TICK_PERIOD

            with self._lock:
                fqs = dict(self._frame_queues)  # sid → input Queue
                qs  = dict(self._member_qs)     # sid → output Queue

            # Need at least 2 members for anyone to hear anyone else
            if len(fqs) < 2:
                continue

            # Drain ONE frame per member (mirrors PLLineMixer tick strategy)
            frames_by_sid = {}
            for sid, fq in fqs.items():
                try:
                    frames_by_sid[sid] = fq.get_nowait()
                except queue.Empty:
                    pass

            if not frames_by_sid:
                continue

            for sid, out_q in qs.items():
                # Mix everyone EXCEPT this member (mix-minus)
                others = [f for s, f in frames_by_sid.items() if s != sid]
                if not others:
                    continue
                mixed = PLLineMixer._mix(others)
                if not mixed:
                    continue
                # Drop-oldest push — same pattern as push_output
                try:
                    out_q.put_nowait(mixed)
                except queue.Full:
                    try:
                        out_q.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        out_q.put_nowait(mixed)
                    except queue.Full:
                        pass

        logger.info(f"PL conf mixer line {self.line_id} stopped")


# ── Headset two-way bridge (mobile operator ↔ USB SIP line) ─────────────────
class WebRTCHeadsetBridge:
    """
    Two-way WebRTC bridge for the mobile headset feature in index.html.
    Operator's phone browser can HEAR and SPEAK on a USB SIP line (1-8).

    Audio flow:
        SIP RTP audio  →  push_audio(pcm_8k)  →  browser speaker
        Browser mic    →  _on_mic_frame()      →  input_callback() → RTP TX

    Uses headset_offer / headset_answer / headset_ice_candidate socket events
    so it is completely independent from WebRTCMonitor (listen-only) and
    WebRTCPLBridge (party line).  Mic is always open — no PTT gate needed.
    """

    def __init__(self, line_id: int, socketio, session_id: str):
        self.line_id   = line_id
        self._sio      = socketio
        self._sid      = session_id
        self._out_q: queue.Queue = queue.Queue(maxsize=10)
        self._input_cb: Optional[Callable] = None
        self._pc: Optional[RTCPeerConnection] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._answer_future: Optional[asyncio.Future] = None
        self._ice_queue: queue.Queue = queue.Queue()
        self._closed: bool = False
        self._rs_in = None   # audioop.ratecv state for browser → 8 kHz

    # ── Public API ────────────────────────────────────────────────────────

    def push_audio(self, pcm_8k: bytes):
        """Feed 8 kHz mono PCM from RTP → browser speaker."""
        try:
            self._out_q.put_nowait(pcm_8k)
        except queue.Full:
            try:
                self._out_q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._out_q.put_nowait(pcm_8k)
            except queue.Full:
                pass

    def set_input_callback(self, cb: Callable):
        """Register cb(pcm_bytes) called with browser mic audio → RTP TX."""
        self._input_cb = cb

    def deliver_answer(self, sdp: str):
        """Deliver browser SDP answer (called from Socket.IO thread)."""
        fut, loop = self._answer_future, self._loop
        if loop and fut and not loop.is_closed():
            def _resolve(f=fut, s=sdp):
                if not f.done():
                    f.set_result(s)
            loop.call_soon_threadsafe(_resolve)

    def add_ice_candidate(self, cand_dict: dict):
        """Queue browser ICE candidate (called from Socket.IO thread)."""
        self._ice_queue.put_nowait(cand_dict)

    def close(self):
        """Shut down WebRTC connection."""
        self._closed = True
        loop = self._loop
        if loop and not loop.is_closed():
            fut = self._answer_future
            if fut:
                def _cancel(f=fut):
                    if not f.done():
                        f.cancel()
                loop.call_soon_threadsafe(_cancel)
            pc = self._pc
            if pc:
                asyncio.run_coroutine_threadsafe(pc.close(), loop)

    def start(self):
        """Start WebRTC negotiation in a background thread."""
        def _thread():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._answer_future = loop.create_future()
            try:
                loop.run_until_complete(self._run())
            except Exception as e:
                logger.error(f"WebRTC headset bridge line {self.line_id}: {e}")
            finally:
                loop.close()
                logger.info(f"WebRTC headset bridge line {self.line_id} stopped")

        threading.Thread(
            target=_thread, daemon=True,
            name=f"WebRTCHdst-{self.line_id}-{self._sid[:6]}"
        ).start()
        logger.info(f"WebRTC headset bridge line {self.line_id} starting → {self._sid[:8]}…")

    # ── Internal async ────────────────────────────────────────────────────

    async def _run(self):
        if self._closed:
            return
        pc = RTCPeerConnection()
        self._pc = pc

        # Outgoing: SIP RTP audio → browser speaker
        pc.addTrack(BrowserOutputTrack(self._out_q))

        # Incoming: browser mic → RTP TX
        @pc.on('track')
        def on_track(track):
            if track.kind == 'audio':
                asyncio.ensure_future(self._read_mic(track))

        @pc.on('connectionstatechange')
        def on_conn_state():
            logger.info(f"WebRTC headset line {self.line_id}: state={pc.connectionState}")

        # Create offer, wait for ICE gathering
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        await WebRTCBridge._wait_ice_complete(pc)

        # Send offer to phone browser
        self._sio.emit('headset_offer', {
            'line': self.line_id,
            'sdp': pc.localDescription.sdp,
        }, to=self._sid)

        # Wait for browser SDP answer
        try:
            sdp_answer = await asyncio.wait_for(self._answer_future, timeout=30.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            logger.info(f"WebRTC headset line {self.line_id}: answer wait cancelled or timed out")
            await pc.close()
            return

        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=sdp_answer, type='answer')
        )

        # Flush stale audio buffered during negotiation
        while not self._out_q.empty():
            try:
                self._out_q.get_nowait()
            except queue.Empty:
                break

        asyncio.ensure_future(WebRTCBridge._drain_ice(pc, self._ice_queue))

        while pc.connectionState not in ('closed', 'failed') and not self._closed:
            await asyncio.sleep(1.0)

    async def _read_mic(self, track):
        """Drain browser mic frames and forward to RTP TX."""
        frame_count = 0
        while True:
            try:
                frame = await track.recv()
                frame_count += 1
                if frame_count == 1:
                    logger.info(f"WebRTC headset line {self.line_id}: first mic frame received")
                self._on_mic_frame(frame)
            except Exception as e:
                logger.warning(
                    f"WebRTC headset line {self.line_id}: mic ended after {frame_count} frames: {e}"
                )
                break

    def _on_mic_frame(self, frame):
        """Convert browser AudioFrame → 8 kHz PCM chunks → RTP TX."""
        try:
            src_rate = frame.sample_rate or WEBRTC_RATE
            fmt      = frame.format.name
            n_ch     = len(frame.layout.channels)

            if fmt == 's16':
                pcm_src = bytes(frame.planes[0])
                arr_mono = np.frombuffer(pcm_src, dtype=np.int16)
                if n_ch >= 2:
                    arr_mono = arr_mono[0::2].copy()
            else:
                arr = frame.to_ndarray()
                if arr.dtype in (np.float32, np.float64):
                    arr = np.clip(arr, -1.0, 1.0)
                    arr = (arr * 32767).astype(np.int16)
                elif arr.dtype != np.int16:
                    arr = arr.astype(np.int16)
                if arr.ndim > 1 and arr.shape[0] > 1:
                    arr = arr.mean(axis=0, keepdims=True)
                arr_mono = arr.flatten().astype(np.int16)

            # Resample to 8 kHz
            ratio = src_rate // SAMPLE_RATE
            if src_rate == ratio * SAMPLE_RATE and ratio > 1:
                n_trim = (len(arr_mono) // ratio) * ratio
                arr_8k = arr_mono[:n_trim].astype(np.int32).reshape(-1, ratio).mean(axis=1).astype(np.int16)
                pcm_8k = arr_8k.tobytes()
            else:
                pcm_8k, self._rs_in = audioop.ratecv(
                    arr_mono.tobytes(), SAMPLE_WIDTH, 1, src_rate, SAMPLE_RATE, self._rs_in
                )

            while len(pcm_8k) >= FRAME_BYTES:
                chunk, pcm_8k = pcm_8k[:FRAME_BYTES], pcm_8k[FRAME_BYTES:]
                if self._input_cb:
                    try:
                        self._input_cb(chunk)
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"WebRTC headset line {self.line_id}: _on_mic_frame error: {e}")


# ── Interview Monitor — one-way HAT-input → browser (per channel) ─────────────

class WebRTCChannelMonitor:
    """
    One-way WebRTC bridge: streams raw HAT input audio for a single channel
    to a browser for the Interview Monitor feature.

    Listen-only — no browser mic, no SIP involvement.
    Audio arrives via push_audio() called from HATAudioManager's
    _input_worker fan-out (registered via register_ch_input_monitor).

    Uses socket events:
        Server → browser : ch_monitor_offer   {ch, sdp}
        Browser → server : ch_monitor_answer  {ch, sdp}
        Browser → server : ch_monitor_ice     {ch, candidate}

    One instance per (session_id, channel).
    """

    def __init__(self, ch: int, socketio, session_id: str):
        self.ch       = ch
        self._sio     = socketio
        self._sid     = session_id
        self._out_q: queue.Queue = queue.Queue(maxsize=10)  # ~200 ms
        self._pc: Optional[RTCPeerConnection]       = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._answer_future: Optional[asyncio.Future]   = None
        self._ice_queue: queue.Queue = queue.Queue()
        self._closed: bool = False

    # ── Public API ────────────────────────────────────────────────────────

    def push_audio(self, pcm_8k: bytes):
        """Feed 8 kHz mono PCM from HAT input → browser earpiece."""
        try:
            self._out_q.put_nowait(pcm_8k)
        except queue.Full:
            try:
                self._out_q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._out_q.put_nowait(pcm_8k)
            except queue.Full:
                pass

    def deliver_answer(self, sdp: str):
        """Deliver the browser's SDP answer (called from Socket.IO thread)."""
        fut, loop = self._answer_future, self._loop
        if loop and fut and not loop.is_closed():
            def _resolve(f=fut, s=sdp):
                if not f.done():
                    f.set_result(s)
            loop.call_soon_threadsafe(_resolve)

    def add_ice_candidate(self, cand_dict: dict):
        """Queue a browser ICE candidate (called from Socket.IO thread)."""
        self._ice_queue.put_nowait(cand_dict)

    def close(self):
        """Shut down the WebRTC connection."""
        self._closed = True
        loop = self._loop
        if loop and not loop.is_closed():
            fut = self._answer_future
            if fut:
                def _cancel(f=fut):
                    if not f.done():
                        f.cancel()
                loop.call_soon_threadsafe(_cancel)
            pc = self._pc
            if pc:
                asyncio.run_coroutine_threadsafe(pc.close(), loop)

    def start(self):
        """Start WebRTC negotiation in a background thread."""
        def _thread():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._answer_future = loop.create_future()
            try:
                loop.run_until_complete(self._run())
            except Exception as e:
                logger.error(f"WebRTCChannelMonitor ch={self.ch}: {e}")
            finally:
                loop.close()
                logger.info(f"WebRTCChannelMonitor ch={self.ch} stopped")

        threading.Thread(
            target=_thread, daemon=True,
            name=f"WebRTCChMon-{self.ch}-{self._sid[:6]}"
        ).start()
        logger.info(f"WebRTCChannelMonitor ch={self.ch} starting → {self._sid[:8]}…")

    # ── Internal async ────────────────────────────────────────────────────

    async def _run(self):
        if self._closed:
            return
        pc = RTCPeerConnection()
        self._pc = pc

        # Outgoing: raw HAT input → browser earpiece (listen only)
        pc.addTrack(BrowserOutputTrack(self._out_q))

        @pc.on('connectionstatechange')
        def on_conn_state():
            logger.info(f"WebRTCChannelMonitor ch={self.ch}: state={pc.connectionState}")

        # Create offer, wait for ICE gathering
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        await WebRTCBridge._wait_ice_complete(pc)

        # Send offer to the Interview Monitor browser
        self._sio.emit('ch_monitor_offer', {
            'ch':  self.ch,
            'sdp': pc.localDescription.sdp,
        }, to=self._sid)

        # Wait for browser SDP answer
        try:
            sdp_answer = await asyncio.wait_for(self._answer_future, timeout=30.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            logger.info(f"WebRTCChannelMonitor ch={self.ch}: answer wait cancelled/timeout")
            await pc.close()
            return

        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=sdp_answer, type='answer')
        )

        # Flush any audio that buffered during negotiation
        while not self._out_q.empty():
            try:
                self._out_q.get_nowait()
            except queue.Empty:
                break

        asyncio.ensure_future(WebRTCBridge._drain_ice(pc, self._ice_queue))

        # Stay alive until the connection closes or close() is called
        while pc.connectionState not in ('closed', 'failed') and not self._closed:
            await asyncio.sleep(1.0)
