"""
Audio Manager for Smart SIP Engine - RaspiAudio 8xIN+8xOUT HAT Mode

Hardware (confirmed on Pi 5):
  ALSA card 1:  snd_rpi_hifiberry_dac8x  (hw:1,0)
  dtoverlay:    hifiberry-dac8x
  8-channel TDM, S32_LE, 48000 Hz, I2S
  8 inputs  (green jacks)  +  8 outputs (red jacks)  — same hw:1,0

Why alsaaudio and NOT sounddevice:
  PortAudio probes pcm5102a codec which hard-limits channels_max=2.
  Opening hw:1,0 with 8 channels via sounddevice fails:
    "Invalid number of channels [PaErrorCode -9998]"
  Direct ALSA (alsaaudio) bypasses PortAudio and opens hw:1,0 at 8ch S32_LE
  correctly — confirmed with arecord/aplay and alsaaudio on the target Pi 5.

Architecture:
  ONE 8-channel alsaaudio PCM_CAPTURE stream — input worker thread reads
    960-frame S32_LE blocks, demuxes 8 channel slices, resamples each
    48kHz→8kHz, fans out to the line assigned to each channel.
  ONE 8-channel alsaaudio PCM_PLAYBACK stream — output worker thread assembles
    per-channel RTP audio (8kHz→48kHz), packs into 8-slot S32_LE frame,
    writes to ALSA.

  Channel N (1-indexed, UI) maps to TDM slot N-1 (0-indexed array column).
  S32_LE stores audio in the upper 16 bits; lower 16 are zero-padded.

Public API is identical to USBDongleAudioManager so engine.py needs only
the one-line import change already applied.

Headset: separate USB dongle via alsaaudio (same as USB dongle version).
"""

import struct
import math
import alsaaudio
import threading
import queue
import time
import audioop
import numpy as np
from typing import Optional, Dict, Set, Callable
import logging

logger = logging.getLogger(__name__)

# ── HAT hardware constants ─────────────────────────────────────────────────────
def _find_hat_card() -> int:
    """Auto-detect the ALSA card number for the HiFiBerry DAC8x (works on Pi 4 and Pi 5)."""
    try:
        import alsaaudio as _a
        for i in _a.card_indexes():
            name, _ = _a.card_name(i)
            if "hifiberry" in name.lower() or "dac8x" in name.lower():
                return i
    except Exception:
        pass
    return 0  # fallback


def _find_headset_card() -> int:
    """Auto-detect the operator headset USB sound card.

    The HAT replaced all USB audio dongles, so the ONLY remaining USB audio
    device on the Pi is the headset. Return the first ALSA card that is
    neither the HiFiBerry HAT nor an HDMI audio output — whatever USB port
    it's plugged into. Returns -1 if none is present (headset feature stays
    disabled until one is connected).
    """
    try:
        import alsaaudio as _a
        for i in _a.card_indexes():
            name, longname = _a.card_name(i)
            low = f"{name} {longname}".lower()
            if "hifiberry" in low or "dac8x" in low:
                continue  # the HAT itself
            if "hdmi" in low or "vc4" in low:
                continue  # HDMI audio outputs
            return i   # the USB headset
    except Exception:
        pass
    return -1  # no headset found → feature disabled

HAT_CARD        = _find_hat_card()   # Auto-detected: 0 on Pi 5, 1 on Pi 4
HAT_DEVICE      = f"plughw:{HAT_CARD},0"  # plughw enables 8-ch TDM via ALSA plugin layer
HAT_CHANNELS    = 8
HAT_SAMPLE_RATE = 48000
HAT_PERIOD      = 960         # frames per period — 20 ms at 48 kHz
# S32_LE: 4 bytes per sample, 8 channels interleaved
HAT_BYTES_PER_FRAME = HAT_CHANNELS * 4   # 32 bytes per interleaved frame
HAT_PERIOD_BYTES    = HAT_PERIOD * HAT_BYTES_PER_FRAME  # 30720 bytes

# ── RTP / processing constants ─────────────────────────────────────────────────
RTP_SAMPLE_RATE = 8000
SAMPLE_WIDTH    = 2          # 16-bit PCM throughout
RTP_CHUNK_SIZE  = 160        # 20 ms at 8 kHz
HW_SAMPLE_RATE  = 48000      # alias used in headset section
HW_CHUNK_SIZE   = 960        # headset dongle period

MIC_GAIN = 3.0               # base linear gain applied to captured mic audio (at default fader)

# ── Physical jack → TDM slot mapping ──────────────────────────────────────────
# RaspiAudio 8xIN+8xOUT HAT: each 3.5mm jack carries 2 TDM channels (stereo).
# Hardware TDM slot order (verified empirically):
#   Jack 1 (TDM slots 0,1) → UI channels 1,2
#   Jack 2 (TDM slots 6,7) → UI channels 3,4
#   Jack 3 (TDM slots 4,5) → UI channels 5,6
#   Jack 4 (TDM slots 2,3) → UI channels 7,8
# UI channel N → TDM slot (0-indexed column in the interleaved S32_LE frame):
_CH_TO_SLOT: dict = {1: 0, 2: 1, 3: 6, 4: 7, 5: 4, 6: 5, 7: 2, 8: 3}

# ── Per-channel software volume (web UI faders) ─────────────────────────────────
# The HAT is a single ALSA card with NO per-channel hardware mixer, so the IN/OUT
# faders are applied as software gain multipliers inside the audio workers.
# DEFAULT_VOLUME (85) maps to "calibrated unity" so the default preserves the
# exact behaviour validated during bring-up:
#   • IN  at 85 → MIC_GAIN (3.0)   • OUT at 85 → 1.0 (raw passthrough)
# The fader scales linearly from there: effective = BASE × (value / DEFAULT_VOLUME).
DEFAULT_VOLUME = 85          # UI fader value (0-100) that maps to calibrated unity
OUT_BASE_GAIN  = 1.0         # base linear gain for speaker output at the default fader


class HATAudioManager:
    """
    Manages audio for up to 8 phone lines using the RaspiAudio 8xIN+8xOUT HAT.

    TDM slot N-1 (0-indexed) <-> channel N (1-indexed, matches UI).
    Uses direct ALSA (alsaaudio) at hw:1,0 with 8ch S32_LE — the only path
    that correctly opens this device at 8 channels on the Pi 5.

    Public API is identical to USBDongleAudioManager.
    """

    def __init__(self, max_lines: int = 8, line_to_card: dict = None,
                 headset_card: int = -1):
        self._running  = False
        self.max_lines = max_lines

        # channel_to_card: kept for API compat (not used for HAT routing)
        self.channel_to_card: Dict[int, int] = {}
        if line_to_card:
            self.channel_to_card = {int(k): int(v) for k, v in line_to_card.items()}

        # ── HAT ALSA streams ───────────────────────────────────────────────
        self._hat_input_stream:  Optional[alsaaudio.PCM] = None
        self._hat_output_stream: Optional[alsaaudio.PCM] = None

        # ── Per-channel output queues (channels 1-8) ───────────────────────
        self._ch_output_queues: Dict[int, queue.Queue] = {
            ch: queue.Queue(maxsize=10) for ch in range(1, 9)
        }

        # ── Routing: line <-> channel ──────────────────────────────────────
        self._line_to_channel:  Dict[int, int]      = {}
        self._channel_to_lines: Dict[int, Set[int]] = {}

        # ── Per-line RTP-send callbacks ────────────────────────────────────
        self._input_callbacks: Dict[int, Callable[[bytes], None]] = {}

        # ── Audio mixing (channel mic + headset mic) ───────────────────────
        self._line_channel_mic_buffer: Dict[int, Optional[bytes]] = {}
        self._line_headset_mic_buffer: Dict[int, Optional[bytes]] = {}
        self._line_mix_locks:          Dict[int, threading.Lock]  = {}

        # ── Headset USB dongle (alsaaudio) ─────────────────────────────────
        # Resolve the headset card robustly. The HAT replaced all USB dongles,
        # so the headset is simply "the one USB audio card". We auto-detect it
        # when the configured value is unusable:
        #   • headset_card < 0      → "auto" sentinel
        #   • headset_card == HAT   → stale config left over from USB-dongle days
        #     (card 0 used to be a dongle; on the HAT, card 0/HAT_CARD is the HAT)
        if headset_card is None or headset_card < 0 or headset_card == HAT_CARD:
            detected = _find_headset_card()
            if detected >= 0:
                if headset_card == HAT_CARD:
                    logger.info(f"Headset: configured card {headset_card} is the HAT — "
                                f"auto-detected USB headset on card {detected} instead")
                else:
                    logger.info(f"Headset: auto-detected USB headset on card {detected}")
                headset_card = detected
            else:
                logger.info("Headset: no USB headset detected — feature disabled")
                headset_card = -1
        self._headset_card:            int                    = headset_card
        self._headset_listen_line:     Optional[int]          = None
        self._headset_output_queue:    Optional[queue.Queue]  = None
        self._headset_input_stream:    Optional[alsaaudio.PCM] = None
        self._headset_output_stream:   Optional[alsaaudio.PCM] = None
        self._headset_input_thread:    Optional[threading.Thread] = None
        self._headset_output_thread:   Optional[threading.Thread] = None
        self._headset_input_channels:  int  = 1
        self._headset_output_channels: int  = 2
        self._headset_running:         bool = False

        # ── Threading ──────────────────────────────────────────────────────
        self._lock             = threading.Lock()
        self._input_thread:  Optional[threading.Thread] = None
        self._output_thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None

        # ── Per-channel stateful ratecv state ──────────────────────────────
        self._ratecv_in:      Dict[int, object] = {ch: None for ch in range(1, 9)}
        self._ratecv_out_8k:  Dict[int, object] = {ch: None for ch in range(1, 9)}
        self._ratecv_out_16k: Dict[int, object] = {ch: None for ch in range(1, 9)}

        # ── Level meters ───────────────────────────────────────────────────
        self._level_callback: Optional[Callable] = None
        self._ch_in_level:  Dict[int, float] = {}
        self._ch_out_level: Dict[int, float] = {}
        self._ch_in_pkt:    Dict[int, int]   = {}
        self._ch_out_pkt:   Dict[int, int]   = {}

        # ── Per-channel software volume faders (web UI, values 0-100) ──────
        # Applied as gain multipliers in the audio workers (no HW mixer on HAT).
        # 85 = calibrated unity (preserves validated default behaviour).
        self._ch_in_vol:  Dict[int, int] = {ch: DEFAULT_VOLUME for ch in range(1, 9)}
        self._ch_out_vol: Dict[int, int] = {ch: DEFAULT_VOLUME for ch in range(1, 9)}

        # ── Test tone ──────────────────────────────────────────────────────
        self._test_tone_stop:    Optional[threading.Event]  = None
        self._test_tone_thread:  Optional[threading.Thread] = None
        self._test_tone_channel: Optional[int]              = None

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _rebuild_reverse_map(self):
        rev: Dict[int, Set[int]] = {}
        for lid, ch in self._line_to_channel.items():
            if isinstance(ch, int):
                rev.setdefault(ch, set()).add(lid)
        self._channel_to_lines = rev

    def set_level_callback(self, callback: Callable):
        """Kept for back-compat — use get_levels() instead."""
        self._level_callback = callback

    def get_levels(self) -> Dict[int, tuple]:
        """Return {channel: (in_db, out_db)} snapshot. Thread-safe."""
        all_chs = set(self._ch_in_level.keys()) | set(self._ch_out_level.keys())
        return {ch: (self._ch_in_level.get(ch, -60.0),
                     self._ch_out_level.get(ch, -60.0))
                for ch in all_chs}

    def _in_gain(self, ch: int) -> float:
        """Per-channel input (mic) gain multiplier from the IN fader.
        85 → MIC_GAIN (calibrated unity), scales linearly, 0 → mute."""
        return MIC_GAIN * (self._ch_in_vol.get(ch, DEFAULT_VOLUME) / DEFAULT_VOLUME)

    def _out_gain(self, ch: int) -> float:
        """Per-channel output (speaker) gain multiplier from the OUT fader.
        85 → 1.0 (raw passthrough), scales linearly, 0 → mute."""
        return OUT_BASE_GAIN * (self._ch_out_vol.get(ch, DEFAULT_VOLUME) / DEFAULT_VOLUME)

    def _available_channels(self):
        return list(range(1, self.max_lines + 1))

    # ── Routing API ────────────────────────────────────────────────────────────

    def set_line_channel(self, line_id: int, channel):
        """Assign a line to a HAT channel (1-8). Channels are exclusive."""
        with self._lock:
            old_ch = self._line_to_channel.get(line_id)
            if channel and int(channel) > 0:
                channel = int(channel)
                # Evict any other line on this channel
                evicted = [lid for lid, ch in self._line_to_channel.items()
                           if ch == channel and lid != line_id]
                for lid in evicted:
                    del self._line_to_channel[lid]
                    logger.info(f"🔗 Routing: Line {lid} evicted from Channel {channel}"
                                f" (claimed by Line {line_id})")
                self._line_to_channel[line_id] = channel
                logger.info(f"🔗 Routing: Line {line_id} -> Channel {channel}"
                            + (f" (was Channel {old_ch})" if old_ch else ""))
                # Flush stale audio from the channel's output queue
                q = self._ch_output_queues.get(channel)
                if q:
                    flushed = 0
                    while not q.empty():
                        try:
                            q.get_nowait()
                            flushed += 1
                        except Exception:
                            break
                    if flushed:
                        logger.info(f"🧹 Channel {channel}: flushed {flushed} stale frames on assign")
                # Reset ratecv states so stale filter memory doesn't bleed into new call
                self._ratecv_in[channel]      = None
                self._ratecv_out_8k[channel]  = None
                self._ratecv_out_16k[channel] = None
            else:
                self._line_to_channel.pop(line_id, None)
                logger.info(f"🔗 Routing: Line {line_id} unassigned"
                            + (f" (was Channel {old_ch})" if old_ch else ""))
            self._rebuild_reverse_map()

    def get_line_channel(self, line_id: int) -> Optional[int]:
        return self._line_to_channel.get(line_id)

    # ── Initialization ─────────────────────────────────────────────────────────

    def initialize(self) -> bool:
        """Verify the HAT card is present."""
        try:
            present = alsaaudio.card_indexes()
            if HAT_CARD not in present:
                logger.error(f"HAT ALSA card {HAT_CARD} not found. Present: {present}")
                return False
            name, _ = alsaaudio.card_name(HAT_CARD)
            logger.info(f"HAT card found: card {HAT_CARD} = '{name}' -> {HAT_DEVICE}")
            return True
        except Exception as e:
            logger.error(f"initialize() failed: {e}")
            return False

    def list_devices(self):
        """Log all ALSA cards."""
        for i in alsaaudio.card_indexes():
            name, longname = alsaaudio.card_name(i)
            logger.info(f"  Card {i}: {name} | {longname}")

    # ── HAT stream open/close ──────────────────────────────────────────────────

    def _open_hat_streams(self) -> bool:
        """Open 8-channel S32_LE ALSA streams for the HAT."""
        if HAT_CARD not in alsaaudio.card_indexes():
            logger.error(f"HAT card {HAT_CARD} not present — cannot open streams")
            return False
        try:
            logger.info(f"Opening HAT capture: {HAT_DEVICE} 8ch S32_LE @{HAT_SAMPLE_RATE}Hz "
                        f"period={HAT_PERIOD}")
            self._hat_input_stream = alsaaudio.PCM(
                type=alsaaudio.PCM_CAPTURE,
                device=HAT_DEVICE,
                channels=HAT_CHANNELS,
                rate=HAT_SAMPLE_RATE,
                format=alsaaudio.PCM_FORMAT_S32_LE,
                periodsize=HAT_PERIOD,
            )
            logger.info(f"Opening HAT playback: {HAT_DEVICE} 8ch S32_LE @{HAT_SAMPLE_RATE}Hz "
                        f"period={HAT_PERIOD}")
            self._hat_output_stream = alsaaudio.PCM(
                type=alsaaudio.PCM_PLAYBACK,
                device=HAT_DEVICE,
                channels=HAT_CHANNELS,
                rate=HAT_SAMPLE_RATE,
                format=alsaaudio.PCM_FORMAT_S32_LE,
                periodsize=HAT_PERIOD,
            )
            logger.info("HAT ALSA streams opened ✓")
            return True
        except Exception as e:
            logger.error(f"Failed to open HAT streams: {e}")
            self._close_hat_streams()
            return False

    def _close_hat_streams(self):
        """Close the HAT ALSA streams."""
        for attr in ("_hat_input_stream", "_hat_output_stream"):
            stream = getattr(self, attr, None)
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
                setattr(self, attr, None)

    # ── Worker threads ─────────────────────────────────────────────────────────

    def _input_worker(self):
        """
        Read 8-channel S32_LE frames from the HAT and demux to lines.

        Each period: 960 frames x 8 ch x 4 bytes = 30720 bytes (S32_LE).
        Audio lives in the upper 16 bits of each 32-bit word — shift >> 16
        to get int16 PCM for audioop resampling.
        """
        stream = self._hat_input_stream
        if not stream:
            return
        logger.info("HAT input worker started (8ch S32_LE @48kHz)")

        while self._running and self._hat_input_stream is not None:
            try:
                length, raw = stream.read()
                if length <= 0:
                    time.sleep(0.001)
                    continue

                # Reshape to (frames, 8) int32 little-endian
                samples_i32 = np.frombuffer(raw, dtype="<i4")
                if len(samples_i32) != HAT_PERIOD * HAT_CHANNELS:
                    continue  # unexpected period size
                frames_i32 = samples_i32.reshape(HAT_PERIOD, HAT_CHANNELS)

                # S32_LE -> int16: audio is in upper 16 bits
                frames_i16 = (frames_i32 >> 16).astype(np.int16)

                for ch in range(1, HAT_CHANNELS + 1):
                    slot = _CH_TO_SLOT.get(ch, ch - 1)
                    lines_on_ch = self._channel_to_lines.get(ch)
                    if not lines_on_ch:
                        continue

                    # Extract channel slice as contiguous bytes (1920 bytes)
                    hw_bytes = frames_i16[:, slot].copy().tobytes()

                    # Resample 48kHz -> 8kHz (stateful per-channel)
                    rtp_audio, self._ratecv_in[ch] = audioop.ratecv(
                        hw_bytes, SAMPLE_WIDTH, 1,
                        HAT_SAMPLE_RATE, RTP_SAMPLE_RATE,
                        self._ratecv_in[ch]
                    )

                    # Apply per-channel mic gain (IN fader) with clipping
                    in_gain = self._in_gain(ch)
                    if in_gain != 1.0:
                        s = np.frombuffer(rtp_audio, dtype=np.int16)
                        s = np.clip(
                            s.astype(np.float32) * in_gain, -32768, 32767
                        ).astype(np.int16)
                        rtp_audio = s.tobytes()

                    # ── Level meter (IN) ───────────────────────────────
                    pkt_n = self._ch_in_pkt.get(ch, 0) + 1
                    self._ch_in_pkt[ch] = pkt_n
                    if pkt_n % 10 == 0:
                        rms = audioop.rms(rtp_audio, SAMPLE_WIDTH)
                        db  = (max(-60.0, 20.0 * math.log10(rms / 32768.0))
                               if rms > 0 else -60.0)
                        old = self._ch_in_level.get(ch, -60.0)
                        self._ch_in_level[ch] = old * 0.7 + db * 0.3

                    # Fan-out to assigned lines.
                    # If the operator headset is monitoring this line, the
                    # headset mic is the sole talker for it — skip THIS line's
                    # HAT channel mic so the two don't both feed the same
                    # outgoing SIP stream (which garbles the far end). Only the
                    # one monitored line's channel steps aside; the other
                    # channels keep running normally.
                    for lid in lines_on_ch:
                        if self._headset_listen_line == lid:
                            continue  # headset mic has priority on this line
                        cb = self._input_callbacks.get(lid)
                        if cb:
                            try:
                                cb(rtp_audio)
                            except Exception as e:
                                logger.error(f"Input callback error line {lid}: {e}")

            except Exception as e:
                if self._running:
                    err = str(e)
                    if any(x in err for x in ("Input/output error", "Device or resource busy",
                                               "File descriptor in bad state", "Invalid argument")):
                        logger.warning(f"HAT input: transient ALSA error ({e}), retrying in 200ms")
                        time.sleep(0.2)
                        continue
                    if "No such device" in err:
                        logger.error(f"HAT input: device removed ({e}), stopping")
                        self._hat_input_stream = None
                        break
                    logger.error(f"HAT input worker error: {e}")
                time.sleep(0.01)

        logger.info("HAT input worker stopped")

    def _output_worker(self):
        """
        Assemble per-channel RTP audio into 8-channel S32_LE frames and
        write to the HAT playback stream.

        For each 20ms period:
          - Pull RTP packets from each channel's queue until we have
            HAT_PERIOD (960) resampled samples, or pad with silence.
          - Pack all 8 channels interleaved: int16 << 16 -> int32 (S32_LE).
          - Write one 30720-byte block to ALSA.
        """
        stream = self._hat_output_stream
        if not stream:
            return
        logger.info("HAT output worker started (8ch S32_LE @48kHz)")

        # Per-channel leftover resampled samples (carry across periods)
        ch_buf: Dict[int, np.ndarray] = {ch: np.array([], dtype=np.int16)
                                         for ch in range(1, 9)}
        _dbg_period = 0

        while self._running and self._hat_output_stream is not None:
            try:
                _dbg_period += 1
                # Assemble 8-channel frame: (HAT_PERIOD, 8) int16
                out_i16 = np.zeros((HAT_PERIOD, HAT_CHANNELS), dtype=np.int16)

                for ch in range(1, HAT_CHANNELS + 1):
                    slot = _CH_TO_SLOT.get(ch, ch - 1)
                    q    = self._ch_output_queues[ch]
                    buf  = ch_buf[ch]

                    # Drain the queue, resampling each packet into buf
                    deadline = time.perf_counter() + 0.040  # 40ms budget
                    while len(buf) < HAT_PERIOD and time.perf_counter() < deadline:
                        try:
                            rtp_audio = q.get_nowait()
                        except queue.Empty:
                            break

                        # Detect source rate: >=300 samples = 16kHz (G.722)
                        samples_in = len(rtp_audio) // SAMPLE_WIDTH
                        if samples_in >= 300:
                            hw_audio, self._ratecv_out_16k[ch] = audioop.ratecv(
                                rtp_audio, SAMPLE_WIDTH, 1,
                                16000, HAT_SAMPLE_RATE,
                                self._ratecv_out_16k[ch]
                            )
                        else:
                            hw_audio, self._ratecv_out_8k[ch] = audioop.ratecv(
                                rtp_audio, SAMPLE_WIDTH, 1,
                                RTP_SAMPLE_RATE, HAT_SAMPLE_RATE,
                                self._ratecv_out_8k[ch]
                            )
                        buf = np.concatenate([buf, np.frombuffer(hw_audio, dtype=np.int16)])

                    # Take exactly HAT_PERIOD samples; carry remainder
                    if len(buf) >= HAT_PERIOD:
                        slot_data  = buf[:HAT_PERIOD]
                        ch_buf[ch] = buf[HAT_PERIOD:]
                    else:
                        pad        = np.zeros(HAT_PERIOD - len(buf), dtype=np.int16)
                        slot_data  = np.concatenate([buf, pad])
                        ch_buf[ch] = np.array([], dtype=np.int16)

                    # Apply per-channel speaker gain (OUT fader) with clipping.
                    # Done before the level meter so the meter reflects the
                    # signal actually sent to the jack.
                    out_gain = self._out_gain(ch)
                    if out_gain != 1.0:
                        slot_data = np.clip(
                            slot_data.astype(np.float32) * out_gain, -32768, 32767
                        ).astype(np.int16)

                    out_i16[:, slot] = slot_data

                    # ── Level meter (OUT) ──────────────────────────────
                    pkt_n = self._ch_out_pkt.get(ch, 0) + 1
                    self._ch_out_pkt[ch] = pkt_n
                    if pkt_n % 10 == 0:
                        rms = audioop.rms(slot_data.tobytes(), SAMPLE_WIDTH)
                        db  = (max(-60.0, 20.0 * math.log10(rms / 32768.0))
                               if rms > 0 else -60.0)
                        old = self._ch_out_level.get(ch, -60.0)
                        if rms == 0 and q.empty():
                            self._ch_out_level[ch] = old * 0.7 + (-60.0) * 0.3
                        else:
                            self._ch_out_level[ch] = old * 0.7 + db * 0.3

                # int16 -> S32_LE: shift left 16 bits into int32
                out_i32  = out_i16.astype(np.int32) << 16
                raw_out  = out_i32.astype("<i4").tobytes()  # 30720 bytes

                # Debug: log RMS of ch1 every 100 periods (~2s)
                if _dbg_period % 100 == 0:
                    ch1_rms = audioop.rms(out_i16[:, 0].tobytes(), SAMPLE_WIDTH)
                    assigned_chs = list(self._line_to_channel.values())
                    q_sizes = {ch: self._ch_output_queues[ch].qsize()
                               for ch in assigned_chs if ch in self._ch_output_queues}
                    logger.info(f"🔈 Output worker period {_dbg_period}: "
                                f"ch1_rms={ch1_rms} assigned={assigned_chs} "
                                f"qsizes={q_sizes}")

                t_write  = time.perf_counter()
                stream.write(raw_out)
                write_ms = (time.perf_counter() - t_write) * 1000
                if write_ms > 30:
                    logger.warning(f"HAT output: ALSA write blocked {write_ms:.0f}ms!")

            except Exception as e:
                if self._running:
                    err = str(e)
                    if any(x in err for x in ("Input/output error", "Device or resource busy",
                                               "File descriptor in bad state", "Invalid argument")):
                        logger.warning(f"HAT output: transient ALSA error ({e}), retrying in 200ms")
                        time.sleep(0.2)
                        continue
                    if "No such device" in err:
                        logger.error(f"HAT output: device removed ({e}), stopping")
                        self._hat_output_stream = None
                        break
                    logger.error(f"HAT output worker error: {e}")
                time.sleep(0.01)

        logger.info("HAT output worker stopped")

    # ── Channel lifecycle (API compatibility) ──────────────────────────────────

    def start_channel(self, channel: int) -> bool:
        """No-op — all 8 channels share one stream pair opened in initialize_channels()."""
        logger.debug(f"start_channel({channel}) — HAT uses shared stream, no-op")
        return True

    def stop_channel(self, channel: int):
        logger.debug(f"stop_channel({channel}) — no-op on HAT")

    def restart_dead_channels(self) -> dict:
        """Reopen HAT streams if they have died."""
        in_alive  = (self._hat_input_stream  is not None and
                     self._input_thread  is not None and self._input_thread.is_alive())
        out_alive = (self._hat_output_stream is not None and
                     self._output_thread is not None and self._output_thread.is_alive())
        if in_alive and out_alive:
            return {'restarted': [], 'failed': [], 'already_ok': list(range(1, 9))}
        logger.info("restart_dead_channels: reopening HAT streams")
        self._close_hat_streams()
        ok = self._open_hat_streams()
        if ok:
            self._start_worker_threads()
            return {'restarted': list(range(1, 9)), 'failed': [], 'already_ok': []}
        return {'restarted': [], 'failed': list(range(1, 9)), 'already_ok': []}

    def _start_worker_threads(self):
        self._input_thread = threading.Thread(
            target=self._input_worker, name="HATInput", daemon=True)
        self._input_thread.start()
        self._output_thread = threading.Thread(
            target=self._output_worker, name="HATOutput", daemon=True)
        self._output_thread.start()

    # ── Line callbacks & send_audio ────────────────────────────────────────────

    def set_input_callback(self, line_id: int, callback: Callable[[bytes], None]):
        self._input_callbacks[line_id] = callback

    def register_input_callback(self, line_id: int, callback: Callable[[bytes], None]):
        self._input_callbacks[line_id] = callback

    def unregister_input_callback(self, line_id: int):
        self._input_callbacks.pop(line_id, None)

    def send_audio(self, line_id: int, audio_data: bytes):
        """Route received RTP audio to the HAT output for this line.
        Also mirrors to headset if operator has this line selected."""
        ch = self._line_to_channel.get(line_id)
        if ch is not None:
            q = self._ch_output_queues.get(ch)
            if q:
                try:
                    q.put_nowait(audio_data)
                    # Debug: log first few successful enqueues per channel
                    dbg_key = f"_dbg_enqueue_{ch}"
                    cnt = getattr(self, dbg_key, 0) + 1
                    setattr(self, dbg_key, cnt)
                    if cnt <= 5 or cnt % 500 == 0:
                        rms = audioop.rms(audio_data, SAMPLE_WIDTH) if audio_data else 0
                        logger.info(f"📬 send_audio: Line {line_id} -> Ch {ch} queue "
                                    f"#{cnt} len={len(audio_data)} rms={rms}")
                except queue.Full:
                    logger.warning(f"Output queue full for Channel {ch} (Line {line_id}), dropping")
        else:
            # Debug: warn if line has no channel assigned
            dbg_key = f"_dbg_noch_{line_id}"
            cnt = getattr(self, dbg_key, 0) + 1
            setattr(self, dbg_key, cnt)
            if cnt <= 3 or cnt % 1000 == 0:
                logger.warning(f"⚠️ send_audio: Line {line_id} has NO channel assigned "
                               f"(#{cnt}) — audio dropped. _line_to_channel={self._line_to_channel}")

        if self._headset_listen_line == line_id:
            self.send_audio_to_headset(line_id, audio_data)

    def flush_output(self, line_id: int):
        """Flush stale audio from a line's channel queue (call on hangup)."""
        ch = self._line_to_channel.get(line_id)
        if ch is None:
            return
        q = self._ch_output_queues.get(ch)
        if q:
            flushed = 0
            while not q.empty():
                try:
                    q.get_nowait()
                    flushed += 1
                except queue.Empty:
                    break
            if flushed:
                logger.info(f"🧹 Channel {ch} (Line {line_id}): flushed {flushed} stale audio frames")

    def _mix_and_send_audio(self, line_id: int, source: str, audio_data: bytes):
        """Mix channel mic + headset mic then fire input callback."""
        if line_id not in self._line_mix_locks:
            self._line_mix_locks[line_id]          = threading.Lock()
            self._line_channel_mic_buffer[line_id] = None
            self._line_headset_mic_buffer[line_id] = None

        with self._line_mix_locks[line_id]:
            if source == "channel":
                self._line_channel_mic_buffer[line_id] = audio_data
            elif source == "headset":
                self._line_headset_mic_buffer[line_id] = audio_data

            ch_audio = self._line_channel_mic_buffer[line_id]
            hs_audio = self._line_headset_mic_buffer[line_id]

            if ch_audio and hs_audio:
                ch_s  = np.frombuffer(ch_audio, dtype=np.int16)
                hs_s  = np.frombuffer(hs_audio, dtype=np.int16)
                mixed = ((ch_s.astype(np.int32) + hs_s.astype(np.int32)) // 2).astype(np.int16)
                final = mixed.tobytes()
            elif ch_audio:
                final = ch_audio
            elif hs_audio:
                final = hs_audio
            else:
                return

            cb = self._input_callbacks.get(line_id)
            if cb:
                cb(final)

    # ── Engine-compatible API ──────────────────────────────────────────────────

    def register_line(self, line_id: int, send_callback: Callable[[bytes], None]):
        """Engine-compatible: register a phone line."""
        self.set_input_callback(line_id, send_callback)
        ch = self._line_to_channel.get(line_id)
        return self._ch_output_queues.get(ch) if ch else None

    def initialize_channels(self):
        """Open HAT streams and launch worker threads + watchdog.
        Called once from engine.py after initialize()."""
        self._running = True
        ok = self._open_hat_streams()
        if ok:
            self._start_worker_threads()
            logger.info("HAT streams and workers started (all 8 channels active)")
        else:
            logger.error("HAT streams failed to open — audio unavailable")

        self._watchdog_thread = threading.Thread(
            target=self._hat_watchdog, name="HATWatchdog", daemon=True)
        self._watchdog_thread.start()
        logger.info("HAT watchdog started")

    def _hat_watchdog(self):
        """Reopen HAT streams and restart workers if they die unexpectedly."""
        CHECK_INTERVAL = 30.0
        logger.info("HAT watchdog running (checks every 30s)")
        while self._running:
            time.sleep(CHECK_INTERVAL)
            if not self._running:
                break
            in_alive  = (self._input_thread  is not None and self._input_thread.is_alive())
            out_alive = (self._output_thread is not None and self._output_thread.is_alive())
            if in_alive and out_alive:
                continue
            logger.warning(
                f"HAT watchdog: input={'alive' if in_alive else 'DEAD'}, "
                f"output={'alive' if out_alive else 'DEAD'} — restarting..."
            )
            self._close_hat_streams()
            time.sleep(2.0)
            ok = self._open_hat_streams()
            if ok:
                self._start_worker_threads()
                with self._lock:
                    self._rebuild_reverse_map()
                logger.info("HAT watchdog: streams and workers restarted ✓")
            else:
                logger.warning("HAT watchdog: reopen failed — will retry next cycle")
        logger.info("HAT watchdog stopped")

    def start(self) -> bool:
        """Engine-compatible: called after all lines registered."""
        logger.info("HATAudioManager.start()")
        return True

    # ── Mixer API (software gain — no per-channel ALSA mixer on HAT) ───────────

    def get_mixer_volumes(self) -> dict:
        """Return current per-channel software fader values (UI 0-100).
        Shape matches the USB dongle manager: { ch: {'input': v, 'output': v} }."""
        return {ch: {'input':  int(self._ch_in_vol.get(ch, DEFAULT_VOLUME)),
                     'output': int(self._ch_out_vol.get(ch, DEFAULT_VOLUME))}
                for ch in range(1, 9)}

    def set_mixer_volume(self, channel: int, vol_type: str, value: int) -> bool:
        """Set a per-channel software fader. vol_type 'input'|'output', value 0-100.
        Applied as a gain multiplier in the audio workers (no HW mixer on the HAT).
        Stored in memory; reads from the worker threads are atomic (GIL)."""
        ch = int(channel)
        if ch < 1 or ch > 8:
            logger.warning(f"set_mixer_volume: invalid channel {channel}")
            return False
        value = max(0, min(100, int(value)))
        if vol_type == 'input':
            self._ch_in_vol[ch] = value
            gain = self._in_gain(ch)
        elif vol_type == 'output':
            self._ch_out_vol[ch] = value
            gain = self._out_gain(ch)
        else:
            logger.warning(f"set_mixer_volume: unknown type '{vol_type}'")
            return False
        logger.info(f"Channel {ch}: {vol_type} fader = {value}% (gain ×{gain:.2f})")
        return True

    def _configure_mixer(self, card_number: int, channel: int):
        pass

    # ── Stubs for legacy engine calls ─────────────────────────────────────────

    def set_devices_by_name(self, name_pattern: str, device_type: str = "both") -> bool:
        return True
    def set_devices(self, input_index: int, output_index: int):
        pass
    def set_headset_mode(self, enable: bool):
        pass
    def get_default_devices(self) -> tuple:
        return (None, None)
    def stop(self):
        logger.info("HATAudioManager.stop() -> calling shutdown()")
        self.shutdown()
    def set_active_line(self, line_id):
        pass
    def get_active_line(self):
        return None

    # ── Headset dongle (operator monitor — USB dongle via alsaaudio) ───────────

    def _probe_headset_channels(self, card_number: int, pcm_type: int) -> int:
        device = f"hw:{card_number},0"
        order  = [2, 1] if pcm_type == alsaaudio.PCM_PLAYBACK else [1, 2]
        for ch in order:
            try:
                pcm = alsaaudio.PCM(
                    type=pcm_type, device=device,
                    channels=ch, rate=HW_SAMPLE_RATE,
                    format=alsaaudio.PCM_FORMAT_S16_LE,
                    periodsize=HW_CHUNK_SIZE
                )
                pcm.close()
                return ch
            except alsaaudio.ALSAAudioError:
                continue
        return 2 if pcm_type == alsaaudio.PCM_PLAYBACK else 1

    def _configure_headset_mixer(self, card_number: int):
        try:
            mixers = alsaaudio.mixers(cardindex=card_number)
            if 'Mic' in mixers:
                mic = alsaaudio.Mixer('Mic', cardindex=card_number)
                mic.setvolume(85, pcmtype=alsaaudio.PCM_CAPTURE)
                mic.setvolume(50, pcmtype=alsaaudio.PCM_PLAYBACK)
            if 'Auto Gain Control' in mixers:
                alsaaudio.Mixer('Auto Gain Control', cardindex=card_number).setmute(1)
            if 'Speaker' in mixers:
                spk = alsaaudio.Mixer('Speaker', cardindex=card_number)
                spk.setvolume(85)
                spk.setmute(0)
        except Exception as e:
            logger.warning(f"Headset: could not configure mixer (card {card_number}): {e}")

    def start_headset(self, card_number: int) -> bool:
        """Open ALSA streams for the operator headset USB dongle."""
        if card_number < 0:
            logger.info("Headset: disabled (headset_card=-1)")
            return False
        if card_number == HAT_CARD:
            # Hard safety guard: the headset must NEVER open the HAT card.
            # Opening the HAT at 2ch while it runs 8ch TDM corrupts the codec
            # clock state and requires a COLD power-cycle to recover. This is
            # belt-and-suspenders on top of the __init__/auto-detect protection.
            logger.error(f"Headset: refusing to open HAT card {card_number} — "
                         "would corrupt the 8-channel TDM stream")
            return False
        if card_number not in alsaaudio.card_indexes():
            logger.error(f"Headset: ALSA card {card_number} not found")
            return False

        self._headset_card = card_number
        device = f"hw:{card_number},0"
        card_name, _ = alsaaudio.card_name(card_number)
        logger.info(f"Starting headset dongle: card {card_number} ({card_name})")

        try:
            in_ch  = self._probe_headset_channels(card_number, alsaaudio.PCM_CAPTURE)
            out_ch = self._probe_headset_channels(card_number, alsaaudio.PCM_PLAYBACK)
            self._headset_input_channels  = in_ch
            self._headset_output_channels = out_ch
            self._configure_headset_mixer(card_number)

            self._headset_output_queue = queue.Queue(maxsize=10)

            self._headset_input_stream = alsaaudio.PCM(
                type=alsaaudio.PCM_CAPTURE, device=device,
                channels=in_ch, rate=HW_SAMPLE_RATE,
                format=alsaaudio.PCM_FORMAT_S16_LE,
                periodsize=HW_CHUNK_SIZE
            )
            self._headset_output_stream = alsaaudio.PCM(
                type=alsaaudio.PCM_PLAYBACK, device=device,
                channels=out_ch, rate=HW_SAMPLE_RATE,
                format=alsaaudio.PCM_FORMAT_S16_LE,
                periodsize=HW_CHUNK_SIZE
            )
            self._headset_running = True

            t_in = threading.Thread(target=self._headset_input_worker,
                                    name="HeadsetInput", daemon=True)
            t_in.start()
            self._headset_input_thread = t_in

            t_out = threading.Thread(target=self._headset_output_worker,
                                     name="HeadsetOutput", daemon=True)
            t_out.start()
            self._headset_output_thread = t_out

            logger.info(f"Headset ALSA streams started (card {card_number}) ✓")
            return True

        except Exception as e:
            logger.error(f"Failed to start headset dongle (card {card_number}): {e}")
            self._headset_running = False
            return False

    def _headset_input_worker(self):
        stream       = self._headset_input_stream
        if not stream:
            return
        in_ch        = self._headset_input_channels
        GAIN         = 3.0
        ratecv_state = None
        _transient   = 0
        logger.info(f"Headset input worker started (channels={in_ch})")

        while self._headset_running and self._headset_input_stream:
            try:
                if self._headset_listen_line is None:
                    time.sleep(0.020)
                    continue

                length, hw_audio = stream.read()
                if length <= 0:
                    time.sleep(0.001)
                    continue

                if in_ch >= 2:
                    mono = audioop.tomono(hw_audio, SAMPLE_WIDTH, 1.0, 0.0)
                else:
                    mono = hw_audio

                rtp_audio, ratecv_state = audioop.ratecv(
                    mono, SAMPLE_WIDTH, 1, HW_SAMPLE_RATE, RTP_SAMPLE_RATE, ratecv_state)

                if GAIN != 1.0:
                    s = np.frombuffer(rtp_audio, dtype=np.int16)
                    s = np.clip(s.astype(np.float32) * GAIN, -32768, 32767).astype(np.int16)
                    rtp_audio = s.tobytes()

                cb = self._input_callbacks.get(self._headset_listen_line)
                if cb:
                    cb(rtp_audio)
                _transient = 0

            except Exception as e:
                if self._headset_running:
                    err = str(e)
                    if "No such device" in err:
                        logger.warning("Headset input: dongle removed, stopping")
                        self._headset_input_stream = None
                        break
                    if any(x in err for x in ("Input/output error", "Device or resource busy",
                                               "File descriptor in bad state", "Invalid argument")):
                        _transient += 1
                        if _transient < 5:
                            time.sleep(0.2)
                            continue
                        logger.warning("Headset input: too many errors, reopening stream")
                        try:
                            self._headset_input_stream.close()
                        except Exception:
                            pass
                        time.sleep(0.5)
                        try:
                            new_stream = alsaaudio.PCM(
                                type=alsaaudio.PCM_CAPTURE,
                                device=f"hw:{self._headset_card},0",
                                channels=in_ch, rate=HW_SAMPLE_RATE,
                                format=alsaaudio.PCM_FORMAT_S16_LE,
                                periodsize=HW_CHUNK_SIZE
                            )
                            self._headset_input_stream = new_stream
                            stream       = new_stream
                            ratecv_state = None
                            _transient   = 0
                            logger.info("Headset input: stream reopened ✓")
                        except Exception as reopen_err:
                            logger.error(f"Headset input: reopen failed ({reopen_err}), stopping")
                            self._headset_input_stream = None
                            break
                        continue
                    logger.error(f"Headset input worker error: {e}")
                time.sleep(0.01)

        logger.info("Headset input worker stopped")

    def _headset_output_worker(self):
        stream           = self._headset_output_stream
        output_queue     = self._headset_output_queue
        if not stream or not output_queue:
            return

        out_ch           = self._headset_output_channels
        silence_48k      = b'\x00' * HW_CHUNK_SIZE * SAMPLE_WIDTH * out_ch
        ratecv_state_8k  = None
        ratecv_state_16k = None
        _transient       = 0
        logger.info(f"Headset output worker started (channels={out_ch})")

        while self._headset_running and self._headset_output_stream:
            try:
                if self._headset_listen_line is None:
                    time.sleep(0.020)
                    continue

                try:
                    rtp_audio = output_queue.get(timeout=0.060)
                except queue.Empty:
                    stream.write(silence_48k)
                    continue

                samples_in = len(rtp_audio) // SAMPLE_WIDTH
                if samples_in >= 300:
                    hw_audio, ratecv_state_16k = audioop.ratecv(
                        rtp_audio, SAMPLE_WIDTH, 1, 16000, HW_SAMPLE_RATE, ratecv_state_16k)
                else:
                    hw_audio, ratecv_state_8k = audioop.ratecv(
                        rtp_audio, SAMPLE_WIDTH, 1, RTP_SAMPLE_RATE, HW_SAMPLE_RATE, ratecv_state_8k)

                if out_ch >= 2:
                    out_audio = audioop.tostereo(hw_audio, SAMPLE_WIDTH, 1, 1)
                else:
                    out_audio = hw_audio

                stream.write(out_audio)
                _transient = 0

            except Exception as e:
                if self._headset_running:
                    err = str(e)
                    if "No such device" in err:
                        logger.warning("Headset output: dongle removed, stopping")
                        self._headset_output_stream = None
                        break
                    if any(x in err for x in ("Input/output error", "Device or resource busy",
                                               "File descriptor in bad state", "Invalid argument")):
                        _transient += 1
                        if _transient < 5:
                            time.sleep(0.2)
                            continue
                        logger.warning("Headset output: too many errors, reopening stream")
                        try:
                            self._headset_output_stream.close()
                        except Exception:
                            pass
                        time.sleep(0.5)
                        try:
                            new_stream = alsaaudio.PCM(
                                type=alsaaudio.PCM_PLAYBACK,
                                device=f"hw:{self._headset_card},0",
                                channels=out_ch, rate=HW_SAMPLE_RATE,
                                format=alsaaudio.PCM_FORMAT_S16_LE,
                                periodsize=HW_CHUNK_SIZE
                            )
                            self._headset_output_stream = new_stream
                            stream           = new_stream
                            ratecv_state_8k  = None
                            ratecv_state_16k = None
                            _transient       = 0
                            logger.info("Headset output: stream reopened ✓")
                        except Exception as reopen_err:
                            logger.error(f"Headset output: reopen failed ({reopen_err}), stopping")
                            self._headset_output_stream = None
                            break
                        continue
                    logger.error(f"Headset output worker error: {e}")
                time.sleep(0.01)

        logger.info("Headset output worker stopped")

    def mute_line_and_clear_headset_if(self, line_id: int):
        with self._lock:
            if self._headset_listen_line == line_id:
                self._headset_listen_line = None
                if self._headset_output_queue:
                    while not self._headset_output_queue.empty():
                        try:
                            self._headset_output_queue.get_nowait()
                        except queue.Empty:
                            break
                logger.info(f"Headset: stopped monitoring Line {line_id} (muted)")

    def set_headset_line_only(self, line_id: int):
        with self._lock:
            old = self._headset_listen_line
            self._headset_listen_line = line_id
            if self._headset_output_queue and old != line_id:
                while not self._headset_output_queue.empty():
                    try:
                        self._headset_output_queue.get_nowait()
                    except queue.Empty:
                        break
            if old != line_id:
                logger.info(f"Headset: now monitoring Line {line_id}"
                            + (f" (was Line {old})" if old else ""))

    def set_headset_listen_line(self, line_id: Optional[int]):
        with self._lock:
            old = self._headset_listen_line
            self._headset_listen_line = line_id if (line_id and line_id > 0) else None
            logger.info(f"Headset listen line: {old} -> {self._headset_listen_line}")

    def get_headset_listen_line(self) -> Optional[int]:
        return self._headset_listen_line

    def send_audio_to_headset(self, line_id: int, audio_data: bytes):
        if self._headset_output_queue is None or not self._headset_running:
            return
        try:
            self._headset_output_queue.put_nowait(audio_data)
        except queue.Full:
            pass

    # ── Test Tone ──────────────────────────────────────────────────────────────

    def play_test_tone(self, channel: int, freq: int = 440, volume: float = 0.5):
        """Generate a sine-wave test tone on the given channel (1-8)."""
        self.stop_test_tone()
        q = self._ch_output_queues.get(channel)
        if not q:
            logger.error(f"Cannot play test tone: Channel {channel} not valid (1-8)")
            return False

        self._test_tone_stop    = threading.Event()
        self._test_tone_channel = channel

        def _tone_worker():
            amplitude = max(0.0, min(1.0, volume)) * 32000
            phase     = 0.0
            phase_inc = 2.0 * math.pi * freq / RTP_SAMPLE_RATE
            logger.info(f"Test tone: Channel {channel}, {freq} Hz, vol={volume:.1f}")
            while not self._test_tone_stop.is_set():
                samples = []
                for _ in range(RTP_CHUNK_SIZE):
                    samples.append(int(amplitude * math.sin(phase)))
                    phase += phase_inc
                if phase > 2.0 * math.pi:
                    phase -= 2.0 * math.pi
                pcm = struct.pack(f'<{RTP_CHUNK_SIZE}h', *samples)
                try:
                    q.put(pcm, timeout=0.05)
                except queue.Full:
                    pass
                time.sleep(0.018)
            logger.info(f"Test tone stopped: Channel {channel}")

        t = threading.Thread(target=_tone_worker, name=f"TestTone-Ch{channel}", daemon=True)
        t.start()
        self._test_tone_thread = t
        return True

    def stop_test_tone(self):
        evt = getattr(self, '_test_tone_stop', None)
        if evt is not None:
            evt.set()
        t = getattr(self, '_test_tone_thread', None)
        if t is not None:
            t.join(timeout=1.0)
        self._test_tone_stop    = None
        self._test_tone_thread  = None
        self._test_tone_channel = None

    # ── Shutdown ───────────────────────────────────────────────────────────────

    def shutdown(self):
        """Shutdown all audio."""
        logger.info("Shutting down HATAudioManager")
        self.stop_test_tone()
        self._running = False

        # Headset
        self._headset_running     = False
        self._headset_listen_line = None
        for stream in (self._headset_input_stream, self._headset_output_stream):
            if stream:
                try:
                    stream.close()
                except Exception:
                    pass
        self._headset_input_stream  = None
        self._headset_output_stream = None
        if self._headset_output_queue is not None:
            try:
                self._headset_output_queue.put_nowait(None)
            except Exception:
                pass
        for t in (self._headset_input_thread, self._headset_output_thread):
            if t and t.is_alive():
                t.join(timeout=2.0)
        self._headset_input_thread  = None
        self._headset_output_thread = None
        self._headset_output_queue  = None

        # HAT streams + workers
        self._close_hat_streams()
        for t in (self._input_thread, self._output_thread):
            if t and t.is_alive():
                t.join(timeout=2.0)
        self._input_thread  = None
        self._output_thread = None

        time.sleep(0.1)
        logger.info("HATAudioManager shut down")


# ── Module alias ───────────────────────────────────────────────────────────────
AudioManager = HATAudioManager
