"""
browser_lines.py — Dynamic browser phone line manager
======================================================
Manages the pool of browser-based SIP lines (lines 9-28).
Each user who opens /phone gets a token → line mapping.
Lines are released when the browser disconnects.
All state is in-memory only — resets on Pi reboot.
Thread-safe using a single lock.
"""

import threading
import uuid
import logging
import time

logger = logging.getLogger(__name__)

# Lines 1-8 are reserved for USB dongle lines.
# Lines 9-28 are the 20 browser phone lines.
BROWSER_LINE_START = 9
BROWSER_LINE_END   = 28   # inclusive — max 20 concurrent browser phones
BROWSER_LINE_MAX   = BROWSER_LINE_END - BROWSER_LINE_START + 1  # 20

_lock = threading.Lock()

# token (str UUID) → {
#   'line_id': int,
#   'session_id': str,   # socket.io session id when connected
#   'connected': bool,
#   'assigned_at': float,
#   'last_seen': float,
# }
_tokens: dict = {}

# line_id → token (reverse map for fast lookup)
_line_to_token: dict = {}

# Counter for next line number to try
_next_line = BROWSER_LINE_START


def _next_free_line() -> int | None:
    """Return the next available line number, or None if all 20 lines are taken.
    Must be called with _lock held."""
    global _next_line
    used = set(_line_to_token.keys())
    # Search the full valid range (handles slots freed by disconnected users)
    for candidate in range(BROWSER_LINE_START, BROWSER_LINE_END + 1):
        if candidate not in used:
            _next_line = candidate + 1
            return candidate
    return None  # all 20 lines occupied


def register(token: str = None) -> dict:
    """
    Register a browser client and assign it a line.
    If token is provided and valid, restore the existing line.
    If token is unknown/None, create a new one.
    Returns: {'token': str, 'line_id': int, 'is_new': bool}
             or {'error': str} when all 20 lines are occupied.
    """
    with _lock:
        # Try to restore existing session
        if token and token in _tokens:
            entry = _tokens[token]
            entry['last_seen'] = time.time()
            entry['connected'] = True
            logger.info(f"Browser phone: restored line {entry['line_id']} for token {token[:8]}…")
            return {'token': token, 'line_id': entry['line_id'], 'is_new': False}

        # New token — check capacity
        line_id = _next_free_line()
        if line_id is None:
            logger.warning(f"Browser phone: all {BROWSER_LINE_MAX} lines occupied, rejecting new registration")
            return {'error': 'all_lines_busy', 'max_lines': BROWSER_LINE_MAX}

        new_token = str(uuid.uuid4())
        _tokens[new_token] = {
            'line_id': line_id,
            'session_id': None,
            'connected': True,
            'assigned_at': time.time(),
            'last_seen': time.time(),
        }
        _line_to_token[line_id] = new_token
        logger.info(f"Browser phone: new line {line_id} assigned, token {new_token[:8]}… ({len(_tokens)}/{BROWSER_LINE_MAX} lines used)")
        return {'token': new_token, 'line_id': line_id, 'is_new': True}


def set_session(token: str, session_id: str):
    """Associate a socket.io session ID with a token."""
    with _lock:
        if token in _tokens:
            _tokens[token]['session_id'] = session_id
            _tokens[token]['connected'] = True
            _tokens[token]['last_seen'] = time.time()


def disconnect(token: str):
    """Mark a token as disconnected (browser closed tab)."""
    with _lock:
        if token in _tokens:
            _tokens[token]['connected'] = False
            _tokens[token]['session_id'] = None
            line_id = _tokens[token]['line_id']
            logger.info(f"Browser phone: line {line_id} disconnected (token {token[:8]}…)")


def release(token: str):
    """Fully release a token and free its line."""
    with _lock:
        if token in _tokens:
            line_id = _tokens[token]['line_id']
            del _tokens[token]
            _line_to_token.pop(line_id, None)
            logger.info(f"Browser phone: line {line_id} released (token {token[:8]}…)")


def reset_all() -> int:
    """Release ALL browser lines — called by operator when moving Pi to new location."""
    global _next_line
    with _lock:
        count = len(_tokens)
        _tokens.clear()
        _line_to_token.clear()
        _next_line = BROWSER_LINE_START
        logger.info(f"Browser phone: all {count} lines reset by operator")
        return count


def get_line_id(token: str):
    """Return line_id for a token, or None if not found."""
    with _lock:
        entry = _tokens.get(token)
        return entry['line_id'] if entry else None


def get_entry(token: str):
    """Return the full entry dict for a token (live reference), or None."""
    with _lock:
        return _tokens.get(token)


def disconnect_by_session(session_id: str):
    """Mark disconnected by socket.io session ID (called on socket disconnect)."""
    with _lock:
        for token, entry in _tokens.items():
            if entry.get('session_id') == session_id:
                entry['connected'] = False
                entry['session_id'] = None
                logger.info(f"Browser phone: line {entry['line_id']} socket disconnected (sid={session_id[:8]}…)")
                break


def get_all() -> list:
    """Return a snapshot of all current browser lines (for debugging)."""
    with _lock:
        return [
            {
                'token': t[:8] + '…',
                'line_id': v['line_id'],
                'connected': v['connected'],
                'age_s': round(time.time() - v['assigned_at']),
            }
            for t, v in _tokens.items()
        ]
