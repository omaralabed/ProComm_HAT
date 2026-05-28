"""
Event System for Smart SIP Engine
Provides async callbacks for GUI updates
"""

from enum import Enum, auto
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional
import threading
import queue
import logging

logger = logging.getLogger(__name__)


class EventType(Enum):
    """Types of events the SIP engine can emit"""
    # Registration events
    REGISTERED = auto()
    UNREGISTERED = auto()
    REGISTRATION_FAILED = auto()
    
    # Call state events
    INCOMING_CALL = auto()
    OUTGOING_CALL = auto()
    CALL_RINGING = auto()
    CALL_ANSWERED = auto()
    CALL_ENDED = auto()
    CALL_FAILED = auto()
    
    # Audio events
    AUDIO_STARTED = auto()
    AUDIO_STOPPED = auto()
    DTMF_RECEIVED = auto()
    
    # Line events
    LINE_STATE_CHANGED = auto()
    
    # System events
    ERROR = auto()
    WARNING = auto()


@dataclass
class Event:
    """Event data structure"""
    type: EventType
    line_id: Optional[int] = None
    data: Optional[Dict[str, Any]] = None
    timestamp: float = 0.0
    
    def __post_init__(self):
        if self.timestamp == 0.0:
            import time
            self.timestamp = time.time()


class EventEmitter:
    """
    Thread-safe event emitter for SIP engine events.
    Supports both sync and async callbacks.
    """
    
    def __init__(self):
        self._listeners: Dict[EventType, List[Callable]] = {}
        self._global_listeners: List[Callable] = []
        self._event_queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._running = False
        self._dispatch_thread: Optional[threading.Thread] = None
    
    def start(self):
        """Start the event dispatch thread"""
        self._running = True
        self._dispatch_thread = threading.Thread(
            target=self._dispatch_loop,
            daemon=True,
            name="EventDispatcher"
        )
        self._dispatch_thread.start()
        logger.debug("Event dispatcher started")
    
    def stop(self):
        """Stop the event dispatch thread"""
        self._running = False
        # Send sentinel to unblock queue
        self._event_queue.put(None)
        if self._dispatch_thread:
            self._dispatch_thread.join(timeout=2.0)
        logger.debug("Event dispatcher stopped")
    
    def on(self, event_type: EventType, callback: Callable):
        """Register a listener for a specific event type"""
        with self._lock:
            if event_type not in self._listeners:
                self._listeners[event_type] = []
            self._listeners[event_type].append(callback)
    
    def on_all(self, callback: Callable):
        """Register a listener for all events"""
        with self._lock:
            self._global_listeners.append(callback)
    
    def off(self, event_type: EventType, callback: Callable):
        """Remove a listener"""
        with self._lock:
            if event_type in self._listeners:
                try:
                    self._listeners[event_type].remove(callback)
                except ValueError:
                    pass
    
    def emit(self, event: Event):
        """Emit an event (queued for async dispatch)"""
        self._event_queue.put(event)
    
    def emit_sync(self, event: Event):
        """Emit an event synchronously (blocking)"""
        self._dispatch_event(event)
    
    def _dispatch_loop(self):
        """Background thread that dispatches events"""
        while self._running:
            try:
                event = self._event_queue.get(timeout=0.1)
                if event is None:  # Sentinel
                    break
                self._dispatch_event(event)
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Error in event dispatch: {e}")
    
    def _dispatch_event(self, event: Event):
        """Dispatch an event to all listeners"""
        with self._lock:
            # Type-specific listeners
            listeners = self._listeners.get(event.type, []).copy()
            # Global listeners
            global_listeners = self._global_listeners.copy()
        
        for callback in listeners + global_listeners:
            try:
                callback(event)
            except Exception as e:
                logger.error(f"Error in event callback: {e}")
