"""
Line State Machine for Smart SIP Engine
Handles call states and transitions
"""

from enum import Enum, auto
from dataclasses import dataclass
from typing import Optional
import threading
import time
import logging

logger = logging.getLogger(__name__)


class LineState(Enum):
    """Phone line states"""
    IDLE = auto()
    DIALING = auto()
    RINGING = auto()        # Outgoing call ringing
    INCOMING = auto()       # Incoming call ringing
    CONNECTED = auto()
    ON_HOLD = auto()
    BUSY = auto()
    ERROR = auto()


@dataclass
class CallInfo:
    """Information about an active call"""
    call_id: str = ""
    from_uri: str = ""
    to_uri: str = ""
    phone_number: str = ""
    caller_id: str = ""
    start_time: float = 0.0
    answer_time: float = 0.0
    local_rtp_port: int = 0
    remote_rtp_ip: str = ""
    remote_rtp_port: int = 0
    codec: str = "PCMU"
    
    def duration(self) -> float:
        """Get call duration in seconds"""
        if self.answer_time > 0:
            return time.time() - self.answer_time
        return 0.0


class Line:
    """
    Represents a single phone line with state machine.
    Thread-safe for concurrent access.
    """
    
    # Valid state transitions
    VALID_TRANSITIONS = {
        LineState.IDLE: [LineState.DIALING, LineState.INCOMING, LineState.ERROR],  # Late 4xx can set ERROR so user can clear
        LineState.DIALING: [LineState.RINGING, LineState.CONNECTED, LineState.BUSY, LineState.ERROR, LineState.IDLE],
        LineState.RINGING: [LineState.CONNECTED, LineState.BUSY, LineState.IDLE, LineState.ERROR],   # Late 4xx/5xx after 180 can set ERROR
        LineState.INCOMING: [LineState.CONNECTED, LineState.IDLE],                                   # outgoing_only: this state is never entered, but keep table consistent
        LineState.CONNECTED: [LineState.ON_HOLD, LineState.IDLE, LineState.ERROR],                   # Allow ERROR if mid-call signaling fails (e.g. re-INVITE timeout)
        LineState.ON_HOLD: [LineState.CONNECTED, LineState.IDLE],
        LineState.BUSY: [LineState.IDLE],
        LineState.ERROR: [LineState.IDLE, LineState.RINGING, LineState.CONNECTED],  # Allow recovery from errors
    }
    
    def __init__(self, line_id: int, sip_account: str = ""):
        self.line_id = line_id
        self.sip_account = sip_account  # e.g., "1001" for registration
        self._state = LineState.IDLE
        self._state_entered_at = time.monotonic()  # When current state was entered (for watchdog)
        self._call_info: Optional[CallInfo] = None
        self._lock = threading.RLock()
        self._state_callbacks = []
        
        # Audio routing - which channel this line uses (1-8 int, or None for unassigned)
        self.audio_channel: Optional[int] = None
        
        # SIP dialog info
        self.local_tag: str = ""
        self.remote_tag: str = ""
        self.call_id: str = ""
        self.cseq: int = 0
        self.branch: str = ""
        
        # Registration state
        self.registered: bool = False
        self.register_expires: int = 0
    
    @property
    def state(self) -> LineState:
        # Direct return - Python GIL makes this safe for reads
        return self._state
    
    @property
    def call_info(self) -> Optional[CallInfo]:
        with self._lock:
            return self._call_info
    
    @property
    def is_active(self) -> bool:
        """Check if line has an active call"""
        return self.state in [LineState.DIALING, LineState.RINGING, 
                              LineState.INCOMING, LineState.CONNECTED, LineState.ON_HOLD]
    
    @property
    def can_dial(self) -> bool:
        """Check if line can make a call (IDLE only; registration not required for outgoing-only)"""
        result = self.state == LineState.IDLE
        logger.debug(f"Line {self.line_id}: can_dial check - state={self.state}, result={result}")
        return result
    
    def set_state(self, new_state: LineState) -> bool:
        """
        Transition to a new state.
        Returns True if transition was valid, False otherwise.
        """
        with self._lock:
            old_state = self._state
            
            # Check if transition is valid
            valid_next = self.VALID_TRANSITIONS.get(old_state, [])
            if new_state not in valid_next:
                logger.warning(
                    f"Line {self.line_id}: Invalid state transition "
                    f"{old_state.name} -> {new_state.name}"
                )
                return False
            
            self._state = new_state
            self._state_entered_at = time.monotonic()
            logger.info(f"Line {self.line_id}: {old_state.name} -> {new_state.name}")
            
            # Handle state entry actions
            if new_state == LineState.IDLE:
                self._call_info = None
                self.local_tag = ""
                self.remote_tag = ""
                self.call_id = ""
            
            # Copy callbacks to call outside lock
            callbacks = list(self._state_callbacks)
        
        # Notify callbacks OUTSIDE the lock to prevent deadlocks
        for callback in callbacks:
            try:
                callback(self.line_id, old_state, new_state)
            except Exception as e:
                logger.error(f"State callback error: {e}")
        
        return True
    
    def reset(self):
        """Force reset to IDLE state"""
        logger.info(f"Line {self.line_id}: Force reset to IDLE")
        # Use set_state to trigger callbacks
        self.set_state(LineState.IDLE)
    
    def start_outgoing_call(self, phone_number: str, call_id: str) -> bool:
        """Start an outgoing call"""
        old_state = None
        with self._lock:
            if self._state != LineState.IDLE:
                logger.warning(f"Line {self.line_id}: Cannot dial, state is {self._state.name}")
                return False
            
            old_state = self._state
            self._call_info = CallInfo(
                call_id=call_id,
                phone_number=phone_number,
                start_time=time.time()
            )
            self._state = LineState.DIALING
            self.call_id = call_id
            logger.info(f"Line {self.line_id}: {old_state.name} -> DIALING")
            callbacks = list(self._state_callbacks)
        
        # Notify callbacks outside the lock
        for callback in callbacks:
            try:
                callback(self.line_id, old_state, LineState.DIALING)
            except Exception as e:
                logger.error(f"State callback error: {e}")
        return True
    
    def start_incoming_call(self, call_id: str, from_uri: str, caller_id: str) -> bool:
        """Handle an incoming call"""
        old_state = None
        with self._lock:
            if self._state != LineState.IDLE:
                logger.warning(f"Line {self.line_id}: Cannot accept incoming, state is {self._state.name}")
                return False
            
            old_state = self._state
            self._call_info = CallInfo(
                call_id=call_id,
                from_uri=from_uri,
                caller_id=caller_id,
                start_time=time.time()
            )
            self._state = LineState.INCOMING
            self.call_id = call_id
            logger.info(f"Line {self.line_id}: {old_state.name} -> INCOMING")
            callbacks = list(self._state_callbacks)
        
        # Notify callbacks outside the lock
        for callback in callbacks:
            try:
                callback(self.line_id, old_state, LineState.INCOMING)
            except Exception as e:
                logger.error(f"State callback error: {e}")
        return True
    
    def on_ringing(self):
        """Called when remote side is ringing"""
        self.set_state(LineState.RINGING)
    
    def on_answer(self, remote_rtp_ip: str, remote_rtp_port: int, codec: str = "PCMU"):
        """Called when call is answered"""
        with self._lock:
            if self._call_info:
                self._call_info.answer_time = time.time()
                self._call_info.remote_rtp_ip = remote_rtp_ip
                self._call_info.remote_rtp_port = remote_rtp_port
                self._call_info.codec = codec
        self.set_state(LineState.CONNECTED)
    
    def on_hangup(self):
        """Called when call ends"""
        self.set_state(LineState.IDLE)
    
    def on_busy(self):
        """Called when remote side is busy"""
        return self.set_state(LineState.BUSY)
    
    def on_error(self, reason: str = ""):
        """Called on call error"""
        logger.error(f"Line {self.line_id}: Call error - {reason}")
        return self.set_state(LineState.ERROR)
    
    def on_state_change(self, callback):
        """Register a state change callback"""
        self._state_callbacks.append(callback)
    
    def set_audio_channel(self, channel: Optional[int]):
        """Set which audio channel this line uses (1-8, or None/0 for unassigned)"""
        with self._lock:
            old_channel = self.audio_channel
            if isinstance(channel, int) and channel > 0:
                self.audio_channel = channel
            else:
                self.audio_channel = None
            logger.info(f"Line {self.line_id}: Audio channel changed from {old_channel} to {self.audio_channel}")
    
    def get_status(self) -> dict:
        """Get line status as dictionary"""
        # Avoid lock to prevent deadlocks - safe for reads
        call_info = self._call_info
        # audio_channel can be int (1-8), or None (unassigned → return 0 for JS compat)
        audio_ch = self.audio_channel if self.audio_channel is not None else 0
        return {
            "line_id": self.line_id,
            "state": self._state.name.lower(),
            "registered": self.registered,
            "sip_account": self.sip_account,
            "audio_channel": audio_ch,
            "phone_number": call_info.phone_number if call_info else "",
            "caller_id": call_info.caller_id if call_info else "",
            "duration": call_info.duration() if call_info else 0,
        }
