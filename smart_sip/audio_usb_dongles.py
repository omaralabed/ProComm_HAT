"""
Audio Manager for Smart SIP Engine - USB Dongle Mode (Direct ALSA)

Channels (1-8)  = physical USB audio dongles (each mapped to an ALSA card).
Lines    (1-8)  = virtual SIP phone lines.

The UI lets the user assign any line to any channel.  Each channel is
exclusive — only one line at a time.  A channel's mic feeds the line
assigned to it; received RTP audio for a line is played out the channel
it's assigned to.

Config key "line_audio_cards" maps *channel* number -> ALSA card number:
    { "1": 3, "2": 4, ... }

Uses alsaaudio (pyalsaaudio) for direct ALSA access — no PortAudio/PyAudio
middleman.  Dongles are opened via hw:X,0 directly.
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

# Audio constants
RTP_SAMPLE_RATE = 8000   # RTP uses 8kHz (G.711 standard)
HW_SAMPLE_RATE = 48000   # USB dongles use 48kHz
SAMPLE_WIDTH = 2         # 16-bit
RTP_CHUNK_SIZE = 160     # 20ms at 8kHz (matches RTP packet size)
HW_CHUNK_SIZE = 960      # 20ms at 48kHz (samples per channel)

# Default channel -> ALSA card mapping
# Modify this based on your `aplay -l` output
DEFAULT_CHANNEL_TO_CARD = {
    1: 3,  # Channel 1 uses card 3 (AB13X USB Audio / C-Media)
    2: 4,  # Channel 2 uses card 4
    3: 5,
    4: 6,
    5: 7,
}

# Per-channel speaker volume override (0-100).
# Default for all channels is 85%. Add an entry here to boost a specific
# channel only (e.g. a dongle whose physical output is quieter than the rest).
# UI will reflect the new ALSA value; only the listed channels are affected.
SPEAKER_VOLUME_OVERRIDE = {
    4: 100,  # CH4 dongle has weaker output — push to max
}
DEFAULT_SPEAKER_VOLUME = 85


class USBDongleAudioManager:
    """
    Manages audio for multiple phone lines using separate USB audio dongles.
    Dongles are identified by *channel* number (1-8).
    Lines are dynamically routed to channels via set_line_channel().
    Uses direct ALSA access via alsaaudio — no PortAudio/PyAudio.

    Headset support:
    A separate USB dongle (the operator's headset) can be opened via
    start_headset(card_number).  When set_headset_listen_line(line_id) is
    called the headset speaker mirrors that line's incoming RTP audio, and
    the headset mic feeds back into that line's RTP send path.
    Only one line can be listened to at a time.
    """

    def __init__(self, max_lines: int = 8, line_to_card: dict = None,
                 headset_card: int = -1):
        self._running = False
        self.max_lines = max_lines

        # channel_to_card: channel_number -> ALSA card number
        self.channel_to_card: Dict[int, int] = {}
        if line_to_card:
            self.channel_to_card = {int(k): int(v) for k, v in line_to_card.items()}
        else:
            self.channel_to_card = dict(DEFAULT_CHANNEL_TO_CARD)

        # ── Per-CHANNEL (dongle) resources ──────────────────────────────
        self._ch_input_streams: Dict[int, alsaaudio.PCM] = {}
        self._ch_output_streams: Dict[int, alsaaudio.PCM] = {}
        self._ch_output_queues: Dict[int, queue.Queue] = {}
        self._ch_input_channels: Dict[int, int] = {}   # mic channel count
        self._ch_output_channels: Dict[int, int] = {}   # speaker channel count
        self._ch_input_threads: Dict[int, threading.Thread] = {}
        self._ch_output_threads: Dict[int, threading.Thread] = {}

        # ── Routing: line <-> channel ───────────────────────────────────
        self._line_to_channel: Dict[int, int] = {}  # int for USB channel number
        self._channel_to_lines: Dict[int, Set[int]] = {}

        # ── Per-LINE callbacks (registered by engine.py) ────────────────
        self._input_callbacks: Dict[int, Callable[[bytes], None]] = {}

        # ── Audio Mixing (channel mic + headset mic) ────────────────────
        # Per-line buffers to hold latest audio from each source
        self._line_channel_mic_buffer: Dict[int, Optional[bytes]] = {}
        self._line_headset_mic_buffer: Dict[int, Optional[bytes]] = {}
        self._line_mix_locks: Dict[int, threading.Lock] = {}

        # ── Headset (operator monitor dongle) ───────────────────────────
        self._headset_card: int = headset_card        # ALSA card number (-1 = none)
        self._headset_listen_line: Optional[int] = None  # which line we're monitoring
        self._headset_output_queue: Optional[queue.Queue] = None
        self._headset_input_stream: Optional[alsaaudio.PCM] = None
        self._headset_output_stream: Optional[alsaaudio.PCM] = None
        self._headset_input_thread: Optional[threading.Thread] = None
        self._headset_output_thread: Optional[threading.Thread] = None
        self._headset_input_channels: int = 1
        self._headset_output_channels: int = 2
        self._headset_running: bool = False

        # Threading
        self._lock = threading.Lock()

        # ── Audio level meters ───────────────────────────────────────────
        # Callback: fn(channel, in_level_db, out_level_db) — called ~5x/sec
        self._level_callback: Optional[Callable] = None
        # Per-channel smoothed levels (0.0–1.0)
        self._ch_in_level: Dict[int, float] = {}
        self._ch_out_level: Dict[int, float] = {}
        # Packet counters for throttling emissions
        self._ch_in_pkt: Dict[int, int] = {}
        self._ch_out_pkt: Dict[int, int] = {}

    # ── helpers ─────────────────────────────────────────────────────────

    def _rebuild_reverse_map(self):
        """Rebuild _channel_to_lines from _line_to_channel."""
        rev: Dict[int, Set[int]] = {}
        for lid, ch in self._line_to_channel.items():
            if isinstance(ch, int):  # Only map USB channels
                rev.setdefault(ch, set()).add(lid)
        self._channel_to_lines = rev

    def set_level_callback(self, callback: Callable):
        """DEPRECATED: kept for back-compat but no longer used.
        Use get_levels() polled from a background task instead.
        Calling socket.emit() from the audio worker thread caused
        ALSA write delays / drift on channel switch."""
        self._level_callback = callback

    def get_levels(self) -> Dict[int, tuple]:
        """Return snapshot of current per-channel audio levels.
        Returns: {channel_number: (in_db, out_db)} for all active channels.
        Safe to call from any thread - no locking needed (dict reads are atomic
        in CPython, and stale-by-a-frame data is fine for a VU meter)."""
        levels = {}
        # Union of channels seen on either side
        all_chs = set(self._ch_in_level.keys()) | set(self._ch_out_level.keys())
        for ch in all_chs:
            in_db = self._ch_in_level.get(ch, -60.0)
            out_db = self._ch_out_level.get(ch, -60.0)
            levels[ch] = (in_db, out_db)
        return levels

    def _available_channels(self):
        """Return sorted list of channel numbers whose dongles are active."""
        return sorted(self._ch_output_queues.keys())

    # ── Routing API ─────────────────────────────────────────────────────

    def set_line_channel(self, line_id: int, channel):
        """Assign a line to a channel (dongle).  channel is 1-based integer.
        Pass channel=0 or None to unassign.
        Channels are EXCLUSIVE — only one line per channel.
        If another line already owns the channel, it gets evicted."""
        with self._lock:
            old_ch = self._line_to_channel.get(line_id)
            
            if channel and channel > 0:
                # Evict any other line currently on this channel
                evicted = [lid for lid, ch in self._line_to_channel.items()
                           if ch == channel and lid != line_id]
                for lid in evicted:
                    del self._line_to_channel[lid]
                    logger.info(f"🔗 Routing: Line {lid} evicted from Channel {channel}"
                                f" (claimed by Line {line_id})")
                self._line_to_channel[line_id] = channel
                logger.info(f"🔗 Routing: Line {line_id} → Channel {channel}"
                            + (f" (was Channel {old_ch})" if old_ch else ""))
                # Flush stale silence from the output queue so the ALSA buffer
                # doesn't have to drain before real audio is heard (fixes
                # intermittent silence on first call after channel assignment)
                output_queue = self._ch_output_queues.get(channel)
                if output_queue:
                    flushed = 0
                    while not output_queue.empty():
                        try:
                            output_queue.get_nowait()
                            flushed += 1
                        except Exception:
                            break
                    if flushed:
                        logger.info(f"🧹 Channel {channel}: Flushed {flushed} stale frames on channel assign")
                # Also drop any audio buffered inside the ALSA hardware buffer
                # of the new channel.  When the dongle has been idle, the worker
                # writes silence into ALSA which builds up latency.  Without this
                # drop, the first incoming RTP packets land *behind* that silence
                # and create a persistent delay/drift that lasts the whole call.
                # stream.drop() resets the hardware buffer to empty so audio
                # plays immediately at minimum latency.
                out_stream = self._ch_output_streams.get(channel)
                if out_stream is not None:
                    try:
                        out_stream.drop()
                    except Exception as _drop_err:
                        # drop() can fail on some ALSA states — non-fatal,
                        # latency will just be slightly higher this call
                        logger.debug(f"Channel {channel}: ALSA drop() skipped ({_drop_err})")
                # Same fix on the capture (mic) side: ALSA's capture buffer can
                # accumulate samples on channels that aren't actively being
                # consumed (e.g. when no line is assigned, or when CPU pressure
                # makes the input worker fall slightly behind).  When a line is
                # then assigned to that channel, the next reads return OLD
                # samples first → outgoing audio is delayed.  drop() flushes
                # the capture buffer so reads start from "now".
                in_stream = self._ch_input_streams.get(channel)
                if in_stream is not None:
                    try:
                        in_stream.drop()
                    except Exception as _drop_err:
                        logger.debug(f"Channel {channel}: ALSA input drop() skipped ({_drop_err})")
            else:
                self._line_to_channel.pop(line_id, None)
                logger.info(f"🔗 Routing: Line {line_id} unassigned"
                            + (f" (was Channel {old_ch})" if old_ch else ""))
            self._rebuild_reverse_map()

    def get_line_channel(self, line_id: int) -> Optional[int]:
        """Return the channel number a line is currently routed to, or None if unassigned."""
        return self._line_to_channel.get(line_id)

    # ── Initialization ──────────────────────────────────────────────────

    def initialize(self) -> bool:
        """Initialize audio system (ALSA — no setup needed)."""
        try:
            cards = alsaaudio.card_indexes()
            logger.info(f"ALSA initialized — {len(cards)} sound cards detected")
            for idx in cards:
                name, longname = alsaaudio.card_name(idx)
                logger.info(f"  Card {idx}: {name}")
            return True
        except Exception as e:
            logger.error(f"Failed to query ALSA cards: {e}")
            return False
    
    def list_devices(self):
        """List all available ALSA audio devices."""
        try:
            for idx in alsaaudio.card_indexes():
                name, longname = alsaaudio.card_name(idx)
                logger.info(f"Card {idx}: {name} | {longname}")
        except Exception as e:
            logger.error(f"Error listing ALSA devices: {e}")

    # ── Channel (dongle) lifecycle ──────────────────────────────────────

    def _configure_mixer(self, card_number: int, channel: int):
        """Set ALSA mixer controls for optimal audio quality on USB dongle.
        - Mic capture: 85% with GAIN=3.0 for optimal audio
        - Speaker: reasonable volume
        - Auto Gain Control: OFF (causes pumping artifacts)"""
        try:
            mixers = alsaaudio.mixers(cardindex=card_number)
            logger.info(f"Channel {channel} (card {card_number}): mixer controls = {mixers}")

            # Set Mic capture level
            if 'Mic' in mixers:
                mic = alsaaudio.Mixer('Mic', cardindex=card_number)
                mic.setvolume(85, pcmtype=alsaaudio.PCM_CAPTURE)
                # Playback (sidetone) moderate
                mic.setvolume(50, pcmtype=alsaaudio.PCM_PLAYBACK)
                logger.info(f"Channel {channel}: Mic capture=85%, playback=50%")

            # Disable Auto Gain Control (causes pumping/distortion)
            if 'Auto Gain Control' in mixers:
                agc = alsaaudio.Mixer('Auto Gain Control', cardindex=card_number)
                agc.setmute(1)
                logger.info(f"Channel {channel}: Auto Gain Control disabled")

            # Set Speaker volume (per-channel override if listed, else default)
            if 'Speaker' in mixers:
                spk = alsaaudio.Mixer('Speaker', cardindex=card_number)
                spk_vol = SPEAKER_VOLUME_OVERRIDE.get(channel, DEFAULT_SPEAKER_VOLUME)
                spk.setvolume(spk_vol)
                spk.setmute(0)  # ensure unmuted
                logger.info(f"Channel {channel}: Speaker volume={spk_vol}%")

        except Exception as e:
            logger.warning(f"Channel {channel}: Could not configure mixer (card {card_number}): {e}")

    # ── Mixer volume get/set (for web UI) ───────────────────────────────

    def get_mixer_volumes(self) -> dict:
        """Return current mixer volumes for all configured channels.
        Returns: { channel: { 'input': int, 'output': int }, ... }
        """
        result = {}
        for channel, card_number in self.channel_to_card.items():
            vol = {'input': 85, 'output': 85}  # defaults
            try:
                mixers = alsaaudio.mixers(cardindex=card_number)
                if 'Mic' in mixers:
                    mic = alsaaudio.Mixer('Mic', cardindex=card_number)
                    vols = mic.getvolume(pcmtype=alsaaudio.PCM_CAPTURE)
                    vol['input'] = vols[0] if vols else 85
                if 'Speaker' in mixers:
                    spk = alsaaudio.Mixer('Speaker', cardindex=card_number)
                    vols = spk.getvolume()
                    vol['output'] = vols[0] if vols else 85
            except Exception as e:
                logger.warning(f"Channel {channel}: Could not read mixer volumes: {e}")
            result[channel] = vol
        return result

    def set_mixer_volume(self, channel: int, vol_type: str, value: int) -> bool:
        """Set mixer volume for a specific channel.
        vol_type: 'input' (Mic capture) or 'output' (Speaker playback)
        value: 0-100
        Returns True on success.
        """
        card_number = self.channel_to_card.get(channel)
        if card_number is None:
            logger.warning(f"set_mixer_volume: channel {channel} not mapped to any card")
            return False
        value = max(0, min(100, int(value)))
        try:
            mixers = alsaaudio.mixers(cardindex=card_number)
            if vol_type == 'input' and 'Mic' in mixers:
                mic = alsaaudio.Mixer('Mic', cardindex=card_number)
                mic.setvolume(value, pcmtype=alsaaudio.PCM_CAPTURE)
                logger.info(f"Channel {channel}: Mic capture set to {value}%")
                return True
            elif vol_type == 'output' and 'Speaker' in mixers:
                spk = alsaaudio.Mixer('Speaker', cardindex=card_number)
                spk.setvolume(value)
                logger.info(f"Channel {channel}: Speaker set to {value}%")
                return True
            else:
                logger.warning(f"Channel {channel}: mixer '{vol_type}' not available")
                return False
        except Exception as e:
            logger.error(f"Channel {channel}: set_mixer_volume failed: {e}")
            return False

    def _probe_channels(self, card_number: int, pcm_type: int) -> int:
        """Probe how many channels a card supports for capture/playback.
        Capture: try mono first (preferred for mic input).
        Playback: try stereo first — USB dongles typically need stereo
        output even though ALSA may accept a mono open."""
        device = f"hw:{card_number},0"
        if pcm_type == alsaaudio.PCM_PLAYBACK:
            order = [2, 1]   # stereo first for playback
        else:
            order = [1, 2]   # mono first for capture
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

    def start_channel(self, channel: int) -> bool:
        """Open ALSA streams for a physical channel (dongle)."""
        if channel not in self.channel_to_card:
            logger.error(f"Channel {channel} not mapped to any USB card")
            return False

        card_number = self.channel_to_card[channel]
        device = f"hw:{card_number},0"

        # Check card exists
        if card_number not in alsaaudio.card_indexes():
            logger.error(f"ALSA card {card_number} not found for Channel {channel}")
            return False

        card_name, _ = alsaaudio.card_name(card_number)
        logger.info(f"Starting audio for Channel {channel}: card {card_number} ({card_name}) → {device}")

        try:
            # Probe channel counts
            in_ch = self._probe_channels(card_number, alsaaudio.PCM_CAPTURE)
            out_ch = self._probe_channels(card_number, alsaaudio.PCM_PLAYBACK)
            self._ch_input_channels[channel] = in_ch
            self._ch_output_channels[channel] = out_ch
            logger.info(f"Channel {channel}: capture_ch={in_ch}, playback_ch={out_ch}")

            # Configure ALSA mixer for optimal audio quality
            self._configure_mixer(card_number, channel)

            self._ch_output_queues[channel] = queue.Queue(maxsize=10)

            # Open capture (mic)
            self._ch_input_streams[channel] = alsaaudio.PCM(
                type=alsaaudio.PCM_CAPTURE, device=device,
                channels=in_ch, rate=HW_SAMPLE_RATE,
                format=alsaaudio.PCM_FORMAT_S16_LE,
                periodsize=HW_CHUNK_SIZE
            )

            # Open playback (speaker)
            self._ch_output_streams[channel] = alsaaudio.PCM(
                type=alsaaudio.PCM_PLAYBACK, device=device,
                channels=out_ch, rate=HW_SAMPLE_RATE,
                format=alsaaudio.PCM_FORMAT_S16_LE,
                periodsize=HW_CHUNK_SIZE
            )

            self._running = True

            t_in = threading.Thread(target=self._input_worker, args=(channel,),
                                    name=f"AudioInput-Ch{channel}", daemon=True)
            t_in.start()
            self._ch_input_threads[channel] = t_in

            t_out = threading.Thread(target=self._output_worker, args=(channel,),
                                     name=f"AudioOutput-Ch{channel}", daemon=True)
            t_out.start()
            self._ch_output_threads[channel] = t_out

            logger.info(f"Channel {channel} ALSA streams started ✓")
            return True

        except Exception as e:
            logger.error(f"Failed to start audio for Channel {channel}: {e}")
            self._cleanup_channel(channel)
            return False

    def restart_dead_channels(self) -> dict:
        """Re-run USB audio mapping and restart any channel whose ALSA streams
        are dead.  Called by the UI 'Re-apply Names' button.
        Returns {'restarted': [...], 'failed': [...], 'already_ok': [...]}"""
        import subprocess, json as _json, os as _os

        # 1. Re-run the USB mapping script so channel_to_card is up to date
        script = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                               '..', 'scripts', 'map_usb_audio.sh')
        try:
            result = subprocess.run(['bash', script], capture_output=True, text=True, timeout=15)
            new_map = {}
            headset_card = -1
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.startswith('HEADSET_CARD='):
                    try:
                        headset_card = int(line.split('=', 1)[1])
                    except ValueError:
                        pass
                elif line.startswith('{'):
                    try:
                        new_map = _json.loads(line)
                    except Exception:
                        pass
            if new_map:
                # Update channel_to_card with any newly discovered channels
                for ch_str, card_num in new_map.items():
                    ch = int(ch_str)
                    self.channel_to_card[ch] = card_num
                logger.info(f"restart_dead_channels: updated card map: {self.channel_to_card}")
        except Exception as e:
            logger.warning(f"restart_dead_channels: map script error: {e}")

        restarted, failed, already_ok = [], [], []
        present_cards = set(alsaaudio.card_indexes())

        for ch in sorted(self.channel_to_card.keys()):
            workers_alive = (ch in self._ch_input_streams and ch in self._ch_output_streams)
            if workers_alive:
                already_ok.append(ch)
                continue
            card_num = self.channel_to_card[ch]
            if card_num not in present_cards:
                logger.warning(f"restart_dead_channels: Channel {ch} card {card_num} not present — skipping")
                failed.append(ch)
                continue
            logger.info(f"restart_dead_channels: Restarting Channel {ch} (card {card_num})")
            self._cleanup_channel(ch)
            ok = self.start_channel(ch)
            if ok:
                restarted.append(ch)
                logger.info(f"restart_dead_channels: Channel {ch} restarted ✓")
            else:
                failed.append(ch)
                logger.warning(f"restart_dead_channels: Channel {ch} restart failed")

        return {'restarted': restarted, 'failed': failed, 'already_ok': already_ok}

    def stop_channel(self, channel: int):
        """Stop audio streams for a physical channel."""
        logger.info(f"Stopping audio for Channel {channel}")
        self._cleanup_channel(channel)

    def _cleanup_channel(self, channel: int):
        """Clean up resources for a channel."""
        if channel in self._ch_input_streams:
            try:
                self._ch_input_streams[channel].close()
            except Exception:
                pass
            del self._ch_input_streams[channel]

        if channel in self._ch_output_streams:
            try:
                self._ch_output_streams[channel].close()
            except Exception:
                pass
            del self._ch_output_streams[channel]

        self._ch_output_queues.pop(channel, None)
        self._ch_input_channels.pop(channel, None)
        self._ch_output_channels.pop(channel, None)
        self._ch_input_threads.pop(channel, None)
        self._ch_output_threads.pop(channel, None)

    # ── Audio workers ───────────────────────────────────────────────────

    def _input_worker(self, channel: int):
        """Read mic audio from a channel's dongle and fan out to every line
        currently assigned to this channel.
        Uses audioop.ratecv for stateful resampling with no buffering."""
        stream = self._ch_input_streams.get(channel)
        if not stream:
            return

        in_ch = self._ch_input_channels.get(channel, 1)
        logger.info(f"Input worker started for Channel {channel} (input_channels={in_ch})")

        packet_count = 0
        GAIN = 3.0  # mic gain — boosts captured audio before sending via RTP
        logger.info(f"Channel {channel}: Using gain={GAIN} (input_channels={in_ch})")
        
        # Use audioop.ratecv() for stateful resampling without buffering issues.
        # - soxr.ResampleStream: buffers 7-8 frames then bursts → underwater sound
        # - soxr.resample(): no buffering but loses filter state → hiss
        # - audioop.ratecv(): maintains state, consistent output, no hiss ✓
        ratecv_state = None

        while self._running and channel in self._ch_input_streams:
            try:
                length, hw_audio = stream.read()

                if length <= 0:
                    time.sleep(0.001)
                    continue

                # Convert to mono if stereo
                if in_ch >= 2:
                    mono_audio = audioop.tomono(hw_audio, SAMPLE_WIDTH, 1.0, 0.0)
                else:
                    mono_audio = hw_audio

                # Resample 48kHz → 8kHz using audioop.ratecv (stateful, no buffering)
                # Produces exactly 160 samples from 960 input every time
                rtp_audio, ratecv_state = audioop.ratecv(
                    mono_audio, SAMPLE_WIDTH, 1, HW_SAMPLE_RATE, RTP_SAMPLE_RATE, ratecv_state
                )

                # Apply gain with proper clipping to prevent distortion
                if GAIN != 1.0:
                    samples = np.frombuffer(rtp_audio, dtype=np.int16)
                    samples = np.clip(samples.astype(np.float32) * GAIN, -32768, 32767).astype(np.int16)
                    rtp_audio = samples.tobytes()

                # ── Audio level meter (IN) ──────────────────────────────
                # Throttle: only compute RMS every 10 packets (~200ms at 20ms/pkt).
                # NEVER call socket.emit from this realtime thread - just update
                # the in-memory dict. A background task in app.py polls get_levels()
                # and emits to clients. This keeps the ALSA write loop unblocked
                # so we don't get drift/delay on channel switch.
                pkt_n = self._ch_in_pkt.get(channel, 0) + 1
                self._ch_in_pkt[channel] = pkt_n
                if pkt_n % 10 == 0:
                    rms = audioop.rms(rtp_audio, SAMPLE_WIDTH)
                    db = max(-60.0, 20.0 * math.log10(rms / 32768.0)) if rms > 0 else -60.0
                    # Smooth: 70% old + 30% new
                    old_in = self._ch_in_level.get(channel, -60.0)
                    self._ch_in_level[channel] = old_in * 0.7 + db * 0.3

                # Fan-out to every line routed to this channel
                lines_on_ch = self._channel_to_lines.get(channel, set())
                sent = False
                for lid in lines_on_ch:
                    # Only send if headset is NOT active on this line
                    # When headset is on, headset mic has priority
                    if self._headset_listen_line == lid:
                        continue  # Skip - headset mic will send instead
                    
                    cb = self._input_callbacks.get(lid)
                    if cb:
                        cb(rtp_audio)
                        sent = True

                if sent:
                    packet_count += 1
                    if packet_count % 250 == 0:
                        headset_status = f" (headset on Line {self._headset_listen_line})" if self._headset_listen_line else ""
                        post_rms = audioop.rms(rtp_audio, SAMPLE_WIDTH)
                        post_max = audioop.max(rtp_audio, SAMPLE_WIDTH)
                        logger.info(f"🎤 Channel {channel}: pkt={packet_count} rms={post_rms} max={post_max}"
                                    f"  → lines {sorted(lines_on_ch)}{headset_status}")
                else:
                    if packet_count == 0:
                        logger.warning(f"Channel {channel}: No lines assigned — mic audio discarded")
                        packet_count = -1

            except Exception as e:
                if self._running:
                    err_str = str(e)
                    # Transient ALSA errors — retry briefly (do NOT kill the worker)
                    if any(x in err_str for x in ("Input/output error", "Device or resource busy",
                                                   "File descriptor in bad state", "Invalid argument")):
                        logger.warning(f"Channel {channel}: transient ALSA capture error ({e}), retrying in 200ms")
                        time.sleep(0.2)
                        continue
                    # Fatal: device physically removed — stop this worker
                    if "No such device" in err_str:
                        logger.warning(f"Channel {channel}: dongle removed ({e}), input worker stopping")
                        self._ch_input_streams.pop(channel, None)
                        break
                    logger.error(f"Error in input worker for Channel {channel}: {e}")
                time.sleep(0.01)

        logger.info(f"Input worker stopped for Channel {channel}")

    def _output_worker(self, channel: int):
        """Read RTP audio from the channel's output queue and play it.
        Uses audioop.ratecv for stateful resampling with no buffering.
        Writes silence when no audio is available to keep ALSA buffer in sync.

        Supports both 8kHz PCM (G.711, 320 bytes/packet) and 16kHz PCM (G.722,
        640 bytes/packet).  The source rate is detected per-packet from buffer
        length so there is no lossy 16kHz→8kHz→48kHz double-conversion."""
        stream = self._ch_output_streams.get(channel)
        output_queue = self._ch_output_queues.get(channel)
        if not stream or not output_queue:
            return

        out_ch = self._ch_output_channels.get(channel, 2)
        logger.info(f"Output worker started for Channel {channel} (output_channels={out_ch})")

        silence_48k = b'\x00' * HW_CHUNK_SIZE * SAMPLE_WIDTH * out_ch
        audio_played_count = 0
        silence_played_count = 0

        # Separate ratecv states for each possible source rate so filter memory
        # is not corrupted when the codec changes mid-call (e.g. re-INVITE).
        ratecv_state_8k  = None   # G.711 path:  8 000 Hz → 48 000 Hz
        ratecv_state_16k = None   # G.722 path: 16 000 Hz → 48 000 Hz

        while self._running and channel in self._ch_output_streams:
            try:
                try:
                    # Use 60ms timeout — tolerates up to 3x jitter before playing silence
                    rtp_audio = output_queue.get(timeout=0.060)
                except queue.Empty:
                    # Packet truly late/lost — play silence
                    stream.write(silence_48k)
                    silence_played_count += 1
                    if silence_played_count % 50 == 0:
                        logger.warning(f"🔇 Channel {channel}: Silence {silence_played_count},"
                                       f" audio {audio_played_count} — queue starved!")
                    # ── Decay OUT meter to silent during starvation ──
                    # Without this, _ch_out_level stays frozen at the last
                    # call's level forever after hangup. Decay 70/30 toward
                    # -60dB so the bar drops to grey within ~1 second.
                    old_out = self._ch_out_level.get(channel, -60.0)
                    self._ch_out_level[channel] = old_out * 0.7 + (-60.0) * 0.3
                    continue

                # Detect source sample rate from buffer size:
                #   320 bytes = 160 samples × 2 bytes = 20ms @ 8 kHz  (G.711)
                #   640 bytes = 320 samples × 2 bytes = 20ms @ 16 kHz (G.722)
                # Resample directly to 48kHz — no intermediate step.
                samples_in = len(rtp_audio) // SAMPLE_WIDTH
                if samples_in >= 300:
                    # 16kHz source (G.722 decoded) → resample 16kHz → 48kHz
                    hw_audio, ratecv_state_16k = audioop.ratecv(
                        rtp_audio, SAMPLE_WIDTH, 1, 16000, HW_SAMPLE_RATE, ratecv_state_16k
                    )
                else:
                    # 8kHz source (G.711 PCMU/PCMA) → resample 8kHz → 48kHz
                    hw_audio, ratecv_state_8k = audioop.ratecv(
                        rtp_audio, SAMPLE_WIDTH, 1, RTP_SAMPLE_RATE, HW_SAMPLE_RATE, ratecv_state_8k
                    )

                if out_ch >= 2:
                    out_audio = audioop.tostereo(hw_audio, SAMPLE_WIDTH, 1, 1)
                else:
                    out_audio = hw_audio

                t_write = time.perf_counter()
                stream.write(out_audio)
                write_ms = (time.perf_counter() - t_write) * 1000
                if write_ms > 30:
                    logger.warning(f"⚠️ Channel {channel}: ALSA write blocked {write_ms:.0f}ms!")
                audio_played_count += 1

                # ── Audio level meter (OUT) ─────────────────────────────
                # Throttle: only compute RMS every 10 packets (~200ms).
                # NEVER call socket.emit from this realtime thread - app.py polls
                # get_levels() in a background task. This keeps stream.write()
                # unblocked so channel-switch drop() actually clears latency.
                pkt_n = self._ch_out_pkt.get(channel, 0) + 1
                self._ch_out_pkt[channel] = pkt_n
                if pkt_n % 10 == 0:
                    rms = audioop.rms(rtp_audio, SAMPLE_WIDTH)
                    db = max(-60.0, 20.0 * math.log10(rms / 32768.0)) if rms > 0 else -60.0
                    old_out = self._ch_out_level.get(channel, -60.0)
                    self._ch_out_level[channel] = old_out * 0.7 + db * 0.3

            except Exception as e:
                if self._running:
                    err_str = str(e)
                    # Transient ALSA errors — wait briefly and retry.
                    # Do NOT break out of the loop; the stream usually recovers.
                    if any(x in err_str for x in ("Input/output error", "Device or resource busy",
                                                   "File descriptor in bad state", "Invalid argument")):
                        logger.warning(f"Channel {channel}: transient ALSA error ({e}), retrying in 200ms")
                        time.sleep(0.2)
                        continue
                    # Fatal: device physically removed — stop this worker
                    if "No such device" in err_str:
                        logger.warning(f"Channel {channel}: dongle removed ({e}), output worker stopping")
                        self._ch_output_streams.pop(channel, None)
                        break
                    logger.error(f"Error in output worker for Channel {channel}: {e}")
                time.sleep(0.01)

        logger.info(f"Output worker stopped for Channel {channel}")

    # ── Line callbacks & send_audio ─────────────────────────────────────

    def set_input_callback(self, line_id: int, callback: Callable[[bytes], None]):
        """Register the RTP-send callback for a line (called by engine)."""
        self._input_callbacks[line_id] = callback

    def register_input_callback(self, line_id: int, callback: Callable[[bytes], None]):
        """Alias for set_input_callback — used by UnifiedAudioManager."""
        self._input_callbacks[line_id] = callback

    def unregister_input_callback(self, line_id: int):
        """Remove the RTP-send callback for a line — used by UnifiedAudioManager."""
        self._input_callbacks.pop(line_id, None)

    def send_audio(self, line_id: int, audio_data: bytes):
        """Route received RTP audio to the channel dongle (speaker) for this line.
        Also feeds the headset speaker if the operator has this line active on headset."""
        import time as _t
        t0 = _t.perf_counter()
        
        # Channel dongle — primary audio out path
        ch = self._line_to_channel.get(line_id)
        if ch is not None:
            output_queue = self._ch_output_queues.get(ch)
            if output_queue:
                try:
                    output_queue.put_nowait(audio_data)
                    # Log queue depth occasionally to see if packets are piling up
                    if hasattr(self, '_send_count'):
                        self._send_count += 1
                    else:
                        self._send_count = 1
                    if self._send_count % 100 == 0:
                        depth = output_queue.qsize()
                        logger.info(f"📊 Channel {ch}: Queue depth = {depth} (sent {self._send_count} total)")
                except queue.Full:
                    logger.warning(f"Output queue full for Channel {ch} (Line {line_id}), dropping audio")

        # Headset speaker — only if operator has this line selected on headset
        if self._headset_listen_line == line_id:
            self.send_audio_to_headset(line_id, audio_data)
        
        elapsed_ms = (_t.perf_counter() - t0) * 1000
        if elapsed_ms > 5:
            logger.warning(f"⚠️ send_audio took {elapsed_ms:.1f}ms! (headset={'ON' if self._headset_listen_line == line_id else 'OFF'})")

    def flush_output(self, line_id: int):
        """Flush the output queue for a line's channel. Called on hangup to prevent
        stale audio from the previous call leaking into the next call."""
        ch = self._line_to_channel.get(line_id)
        if ch is None:
            return
        output_queue = self._ch_output_queues.get(ch)
        if output_queue:
            flushed = 0
            while not output_queue.empty():
                try:
                    output_queue.get_nowait()
                    flushed += 1
                except queue.Empty:
                    break
            logger.info(f"🧹 Channel {ch} (Line {line_id}): Flushed {flushed} stale audio frames")

    def _mix_and_send_audio(self, line_id: int, source: str, audio_data: bytes):
        """Mix audio from channel mic and headset mic, then send to RTP.
        
        Args:
            line_id: The line ID
            source: Either "channel" or "headset"
            audio_data: 160 bytes of 8kHz mono PCM audio
        """
        # Initialize mixing structures for this line if needed
        if line_id not in self._line_mix_locks:
            self._line_mix_locks[line_id] = threading.Lock()
            self._line_channel_mic_buffer[line_id] = None
            self._line_headset_mic_buffer[line_id] = None

        with self._line_mix_locks[line_id]:
            # Store this source's audio
            if source == "channel":
                self._line_channel_mic_buffer[line_id] = audio_data
            elif source == "headset":
                self._line_headset_mic_buffer[line_id] = audio_data

            # Get both sources
            channel_audio = self._line_channel_mic_buffer[line_id]
            headset_audio = self._line_headset_mic_buffer[line_id]

            # Decide what to send:
            # - If only one source has audio, send it directly
            # - If both have audio, mix them (average to prevent clipping)
            if channel_audio and headset_audio:
                # Mix: average the two signals
                ch_samples = np.frombuffer(channel_audio, dtype=np.int16)
                hs_samples = np.frombuffer(headset_audio, dtype=np.int16)
                mixed = ((ch_samples.astype(np.int32) + hs_samples.astype(np.int32)) // 2).astype(np.int16)
                final_audio = mixed.tobytes()
            elif channel_audio:
                final_audio = channel_audio
            elif headset_audio:
                final_audio = headset_audio
            else:
                return  # No audio from either source

            # Send the mixed audio to RTP
            cb = self._input_callbacks.get(line_id)
            if cb:
                cb(final_audio)

    # ── Engine-compatible API ───────────────────────────────────────────

    def register_line(self, line_id: int, send_callback: Callable[[bytes], None]):
        """Engine-compatible: register a phone line.

        Stores the mic callback only.  No channel is assigned —
        the user picks the channel from the UI via set_line_channel().
        """
        self.set_input_callback(line_id, send_callback)
        # No auto-assign — user decides channel via UI
        ch = self._line_to_channel.get(line_id)
        return self._ch_output_queues.get(ch) if ch else None

    def initialize_channels(self):
        """Start all configured channels (dongles).
        Called once from engine.py after initialize()."""
        for ch in sorted(self.channel_to_card.keys()):
            self.start_channel(ch)
        active = self._available_channels()
        logger.info(f"Channels started: {active}")

        # Start the dead-channel watchdog (Layer 2 protection)
        self._watchdog_thread = threading.Thread(
            target=self._channel_watchdog,
            name="USBAudioWatchdog",
            daemon=True
        )
        self._watchdog_thread.start()
        logger.info("USB dead-channel watchdog started")

    def _channel_watchdog(self):
        """Background watchdog — checks every 30s if any channel worker has fully stopped.
        Only restarts workers that are completely dead AND whose ALSA card is still present.
        Does NOT interfere with workers that are alive and retrying transient errors (Layer 1).
        """
        CHECK_INTERVAL = 30.0   # check every 30 seconds
        RESTART_DELAY  =  3.0   # grace period after card detected before opening

        logger.info("USB dead-channel watchdog running (checks every 30s)")

        while self._running:
            time.sleep(CHECK_INTERVAL)
            if not self._running:
                break

            present_cards = set(alsaaudio.card_indexes())

            for ch, card_num in list(self.channel_to_card.items()):
                # Check if BOTH workers are alive (thread object exists and is_alive)
                in_thread  = self._ch_input_threads.get(ch)
                out_thread = self._ch_output_threads.get(ch)
                in_alive   = in_thread  is not None and in_thread.is_alive()
                out_alive  = out_thread is not None and out_thread.is_alive()

                if in_alive and out_alive:
                    continue  # healthy — nothing to do

                # Worker(s) dead — only restart if ALSA card is still physically present
                if card_num not in present_cards:
                    logger.debug(f"Watchdog: Channel {ch} card {card_num} not present — skipping")
                    continue

                logger.warning(
                    f"Watchdog: Channel {ch} (card {card_num}) — "
                    f"input={'alive' if in_alive else 'DEAD'}, "
                    f"output={'alive' if out_alive else 'DEAD'} — restarting..."
                )

                # Brief grace period so ALSA finishes any internal cleanup
                time.sleep(RESTART_DELAY)

                self._cleanup_channel(ch)
                ok = self.start_channel(ch)
                if ok:
                    # Rebuild the channel→lines reverse map so the restarted
                    # input worker knows which lines to fan mic audio out to.
                    # Without this, _channel_to_lines is stale and the worker
                    # logs "No lines assigned" and discards all mic audio.
                    with self._lock:
                        self._rebuild_reverse_map()
                    logger.info(f"Watchdog: Channel {ch} restarted ✓")
                else:
                    logger.warning(f"Watchdog: Channel {ch} restart failed — will retry next cycle")

        logger.info("USB dead-channel watchdog stopped")

    def start(self) -> bool:
        """Engine-compatible: called after all lines registered."""
        logger.info(f"USBDongleAudioManager.start() - channels running: {self._available_channels()}")
        return True

    # Stubs for legacy engine calls
    def set_devices_by_name(self, name_pattern: str, device_type: str = "both") -> bool:
        return True
    def set_devices(self, input_index: int, output_index: int):
        pass
    def set_headset_mode(self, enable: bool):
        pass
    def get_default_devices(self) -> tuple:
        return (None, None)

    def stop(self):
        """Engine-compatible: stop audio processing (delegates to shutdown)"""
        logger.info("USBDongleAudioManager.stop() -> calling shutdown()")
        self.shutdown()

    def set_active_line(self, line_id):
        pass
    def get_active_line(self):
        return None

    # ── Headset dongle (operator monitor) ──────────────────────────────

    def start_headset(self, card_number: int) -> bool:
        """Open ALSA streams for the operator headset dongle.
        Called once from engine.py after audio channels are started."""
        if card_number < 0:
            logger.info("Headset: no card configured (headset_card=-1), headset disabled")
            return False
        if card_number not in alsaaudio.card_indexes():
            logger.error(f"Headset: ALSA card {card_number} not found")
            return False

        self._headset_card = card_number
        device = f"hw:{card_number},0"
        card_name, _ = alsaaudio.card_name(card_number)
        logger.info(f"Starting headset dongle: card {card_number} ({card_name}) → {device}")

        try:
            in_ch = self._probe_channels(card_number, alsaaudio.PCM_CAPTURE)
            out_ch = self._probe_channels(card_number, alsaaudio.PCM_PLAYBACK)
            self._headset_input_channels = in_ch
            self._headset_output_channels = out_ch
            self._configure_mixer(card_number, channel=0)   # channel=0 = headset

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

            logger.info(f"Headset ALSA streams started ✓ (card {card_number})")
            return True

        except Exception as e:
            logger.error(f"Failed to start headset dongle (card {card_number}): {e}")
            self._headset_running = False
            return False

    def _headset_input_worker(self):
        """Read mic audio from the headset dongle.
        When a line is being monitored, feed audio into that line's RTP send path."""
        stream = self._headset_input_stream
        if not stream:
            return

        in_ch = self._headset_input_channels
        GAIN = 3.0
        ratecv_state = None
        _transient_errors = 0     # consecutive transient error count
        logger.info(f"Headset input worker started (input_channels={in_ch})")

        while self._headset_running and self._headset_input_stream:
            try:
                # If no line is being monitored, sleep instead of reading ALSA
                # to avoid GIL contention with channel workers.
                if self._headset_listen_line is None:
                    time.sleep(0.020)
                    continue

                length, hw_audio = stream.read()
                if length <= 0:
                    time.sleep(0.001)
                    continue

                # Convert to mono if stereo
                if in_ch >= 2:
                    mono_audio = audioop.tomono(hw_audio, SAMPLE_WIDTH, 1.0, 0.0)
                else:
                    mono_audio = hw_audio

                # Resample 48kHz → 8kHz
                rtp_audio, ratecv_state = audioop.ratecv(
                    mono_audio, SAMPLE_WIDTH, 1, HW_SAMPLE_RATE, RTP_SAMPLE_RATE, ratecv_state
                )

                # Apply gain with clipping to prevent distortion on loud audio
                if GAIN != 1.0:
                    samples = np.frombuffer(rtp_audio, dtype=np.int16)
                    samples = np.clip(samples.astype(np.float32) * GAIN, -32768, 32767).astype(np.int16)
                    rtp_audio = samples.tobytes()

                # Send headset mic audio directly to RTP (replaces channel mic when active)
                cb = self._input_callbacks.get(self._headset_listen_line)
                if cb:
                    cb(rtp_audio)
                _transient_errors = 0   # successful read — reset error counter

            except Exception as e:
                if self._headset_running:
                    err_str = str(e)
                    # Fatal: physically removed
                    if "No such device" in err_str:
                        logger.warning(f"Headset: dongle removed ({e}), input worker stopping")
                        self._headset_input_stream = None
                        break
                    # Transient — allow a few retries, then reopen the stream
                    if any(x in err_str for x in ("Input/output error", "Device or resource busy",
                                                   "File descriptor in bad state", "Invalid argument")):
                        _transient_errors += 1
                        if _transient_errors < 5:
                            logger.warning(f"Headset input: transient ALSA error #{_transient_errors} ({e}), retrying in 200ms")
                            time.sleep(0.2)
                            continue
                        # Too many consecutive errors — close and reopen the ALSA stream
                        logger.warning(f"Headset input: {_transient_errors} consecutive errors, reopening ALSA stream")
                        try:
                            self._headset_input_stream.close()
                        except Exception:
                            pass
                        time.sleep(0.5)
                        try:
                            card = self._headset_card
                            new_stream = alsaaudio.PCM(
                                type=alsaaudio.PCM_CAPTURE,
                                device=f"hw:{card},0",
                                channels=in_ch, rate=HW_SAMPLE_RATE,
                                format=alsaaudio.PCM_FORMAT_S16_LE,
                                periodsize=HW_CHUNK_SIZE
                            )
                            self._headset_input_stream = new_stream
                            stream = new_stream
                            ratecv_state = None
                            _transient_errors = 0
                            logger.info("Headset input: ALSA stream reopened successfully ✓")
                        except Exception as reopen_err:
                            logger.error(f"Headset input: reopen failed ({reopen_err}), stopping")
                            self._headset_input_stream = None
                            break
                        continue
                    logger.error(f"Headset input worker error: {e}")
                time.sleep(0.01)

        logger.info("Headset input worker stopped")

    def _headset_output_worker(self):
        """Play audio from the headset output queue.
        Receives mirrored RTP frames when a line is being monitored.
        When idle (no line being monitored), sleeps instead of writing silence
        to avoid GIL contention with the channel output workers."""
        stream = self._headset_output_stream
        output_queue = self._headset_output_queue
        if not stream or not output_queue:
            return

        out_ch = self._headset_output_channels
        silence_48k = b'\x00' * HW_CHUNK_SIZE * SAMPLE_WIDTH * out_ch
        ratecv_state_8k  = None   # G.711 path:  8 000 Hz → 48 000 Hz
        ratecv_state_16k = None   # G.722 path: 16 000 Hz → 48 000 Hz
        _transient_errors = 0     # consecutive transient error count
        logger.info(f"Headset output worker started (output_channels={out_ch})")

        while self._headset_running and self._headset_output_stream:
            try:
                # If no line is being monitored, don't write anything to ALSA —
                # just sleep so we don't steal the GIL from channel workers.
                if self._headset_listen_line is None:
                    time.sleep(0.020)
                    continue

                try:
                    rtp_audio = output_queue.get(timeout=0.060)
                except queue.Empty:
                    # Line is monitored but packet is late — write silence to keep ALSA clock
                    stream.write(silence_48k)
                    continue

                # Detect source rate: 640 bytes = 16kHz (G.722), 320 bytes = 8kHz (G.711)
                samples_in = len(rtp_audio) // SAMPLE_WIDTH
                if samples_in >= 300:
                    hw_audio, ratecv_state_16k = audioop.ratecv(
                        rtp_audio, SAMPLE_WIDTH, 1, 16000, HW_SAMPLE_RATE, ratecv_state_16k
                    )
                else:
                    hw_audio, ratecv_state_8k = audioop.ratecv(
                        rtp_audio, SAMPLE_WIDTH, 1, RTP_SAMPLE_RATE, HW_SAMPLE_RATE, ratecv_state_8k
                    )

                if out_ch >= 2:
                    out_audio = audioop.tostereo(hw_audio, SAMPLE_WIDTH, 1, 1)
                else:
                    out_audio = hw_audio

                stream.write(out_audio)
                _transient_errors = 0   # successful write — reset error counter

            except Exception as e:
                if self._headset_running:
                    err_str = str(e)
                    # Fatal: physically removed
                    if "No such device" in err_str:
                        logger.warning(f"Headset: dongle removed ({e}), output worker stopping")
                        self._headset_output_stream = None
                        break
                    # Transient — allow a few retries, then reopen the stream
                    if any(x in err_str for x in ("Input/output error", "Device or resource busy",
                                                   "File descriptor in bad state", "Invalid argument")):
                        _transient_errors += 1
                        if _transient_errors < 5:
                            logger.warning(f"Headset output: transient ALSA error #{_transient_errors} ({e}), retrying in 200ms")
                            time.sleep(0.2)
                            continue
                        # Too many consecutive errors — close and reopen the ALSA stream
                        logger.warning(f"Headset output: {_transient_errors} consecutive errors, reopening ALSA stream")
                        try:
                            self._headset_output_stream.close()
                        except Exception:
                            pass
                        time.sleep(0.5)
                        try:
                            card = self._headset_card
                            new_stream = alsaaudio.PCM(
                                type=alsaaudio.PCM_PLAYBACK,
                                device=f"hw:{card},0",
                                channels=out_ch, rate=HW_SAMPLE_RATE,
                                format=alsaaudio.PCM_FORMAT_S16_LE,
                                periodsize=HW_CHUNK_SIZE
                            )
                            self._headset_output_stream = new_stream
                            stream = new_stream
                            ratecv_state_8k  = None
                            ratecv_state_16k = None
                            _transient_errors = 0
                            logger.info("Headset output: ALSA stream reopened successfully ✓")
                        except Exception as reopen_err:
                            logger.error(f"Headset output: reopen failed ({reopen_err}), stopping")
                            self._headset_output_stream = None
                            break
                        continue
                    logger.error(f"Headset output worker error: {e}")
                time.sleep(0.01)

        logger.info("Headset output worker stopped")

    def mute_line_and_clear_headset_if(self, line_id: int):
        """Called when operator mutes/deactivates headset on a line.
        If this line was the monitored one, stop the headset listen."""
        import time as _time
        t0 = _time.perf_counter()
        with self._lock:
            if self._headset_listen_line == line_id:
                self._headset_listen_line = None
                if self._headset_output_queue:
                    while not self._headset_output_queue.empty():
                        try:
                            self._headset_output_queue.get_nowait()
                        except queue.Empty:
                            break
                logger.info(f"Headset: stopped monitoring Line {line_id} (muted) in {(_time.perf_counter()-t0)*1000:.1f}ms")

    def set_headset_line_only(self, line_id: int):
        """Called when operator activates headset on a line (unmutes).
        Only one line can have headset active at a time."""
        import time as _time
        t0 = _time.perf_counter()
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
                            + (f" (was Line {old})" if old else "")
                            + f" in {(_time.perf_counter()-t0)*1000:.1f}ms")

    def set_headset_listen_line(self, line_id: Optional[int]):
        """Set which line's audio plays through the headset speaker.
        None or 0 = silence (no monitoring)."""
        with self._lock:
            old = self._headset_listen_line
            self._headset_listen_line = line_id if (line_id and line_id > 0) else None
            logger.info(f"Headset listen line: {old} → {self._headset_listen_line}")

    def get_headset_listen_line(self) -> Optional[int]:
        """Return the line currently being monitored through the headset."""
        return self._headset_listen_line

    def send_audio_to_headset(self, line_id: int, audio_data: bytes):
        """Mirror incoming RTP audio for a line to the headset output queue.
        Called from send_audio() when a line has the headset active."""
        if self._headset_output_queue is None or not self._headset_running:
            return
        try:
            self._headset_output_queue.put_nowait(audio_data)
        except queue.Full:
            pass  # Drop silently — headset queue is best-effort

    # ── Test Tone ──────────────────────────────────────────────────────

    def play_test_tone(self, channel: int, freq: int = 440, volume: float = 0.5):
        """Generate a sine-wave test tone on the given *channel* (dongle)."""
        self.stop_test_tone()

        output_queue = self._ch_output_queues.get(channel)
        if not output_queue:
            logger.error(f"Cannot play test tone: Channel {channel} not active")
            return False

        self._test_tone_stop = threading.Event()
        self._test_tone_channel = channel

        def _tone_worker():
            sample_rate = RTP_SAMPLE_RATE
            chunk_samples = RTP_CHUNK_SIZE
            amplitude = max(0.0, min(1.0, volume)) * 32000
            phase = 0.0
            phase_inc = 2.0 * math.pi * freq / sample_rate

            logger.info(f"🔊 Test tone started: Channel {channel}, {freq} Hz, vol={volume:.1f}")
            while not self._test_tone_stop.is_set():
                samples = []
                for _ in range(chunk_samples):
                    samples.append(int(amplitude * math.sin(phase)))
                    phase += phase_inc
                if phase > 2.0 * math.pi:
                    phase -= 2.0 * math.pi

                pcm = struct.pack(f'<{chunk_samples}h', *samples)
                try:
                    output_queue.put(pcm, timeout=0.05)
                except queue.Full:
                    pass
                time.sleep(0.018)

            logger.info(f"🔇 Test tone stopped: Channel {channel}")

        t = threading.Thread(target=_tone_worker, name=f"TestTone-Ch{channel}", daemon=True)
        t.start()
        self._test_tone_thread = t
        return True

    def stop_test_tone(self):
        """Stop any running test tone."""
        evt = getattr(self, '_test_tone_stop', None)
        if evt is not None:
            evt.set()
        t = getattr(self, '_test_tone_thread', None)
        if t is not None:
            t.join(timeout=1.0)
        self._test_tone_stop = None
        self._test_tone_thread = None
        self._test_tone_channel = None

    # ── Shutdown ───────────────────────────────────────────────────────

    def shutdown(self):
        """Shutdown all audio"""
        logger.info("Shutting down USB Dongle Audio Manager")
        self.stop_test_tone()

        self._running = False

        # Stop headset streams
        self._headset_running = False
        self._headset_listen_line = None
        for stream in [self._headset_input_stream, self._headset_output_stream]:
            if stream:
                try:
                    stream.close()
                except Exception:
                    pass
        self._headset_input_stream = None
        self._headset_output_stream = None
        if self._headset_output_queue is not None:
            try:
                self._headset_output_queue.put_nowait(None)  # unblock output worker
            except Exception:
                pass
        for t in [self._headset_input_thread, self._headset_output_thread]:
            if t and t.is_alive():
                t.join(timeout=2.0)
        self._headset_input_thread = None
        self._headset_output_thread = None
        self._headset_output_queue = None

        for ch in list(self._ch_input_streams.keys()):
            self._cleanup_channel(ch)

        time.sleep(0.1)
        logger.info("USB Dongle Audio Manager shut down")


# For backward compatibility
AudioManager = USBDongleAudioManager
