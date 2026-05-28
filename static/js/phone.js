/**
 * phone.js — ProComm browser softphone
 * =====================================
 * Flow:
 *   1. On load: read token from localStorage, connect socket.io
 *   2. Emit 'phone_register' → server returns {token, line_id}
 *   3. Server sends 'phone_offer' (SDP offer from aiortc) → we answer
 *   4. After WebRTC connected: dial pad + call / hangup work via socket events
 *   5. On tab close: socket disconnect releases the line automatically
 */

'use strict';

// ── Constants ─────────────────────────────────────────────────────────────
const TOKEN_KEY   = 'procomm_phone_token';
const REDIAL_KEY  = 'procomm_last_number';
const HISTORY_KEY = 'procomm_call_history';
const HISTORY_MAX = 10;
const IDLE_WARN_MS    = 2 * 60 * 1000;  // 2 min idle → show warning
const IDLE_RELEASE_MS = 30 * 1000;       // 30 s more → release line

// ── State ─────────────────────────────────────────────────────────────────
let socket = null;
let pc = null;          // RTCPeerConnection
let localStream = null; // getUserMedia stream
let lineId = null;
let token = null;
let callState = 'idle'; // idle | dialing | incall | hangingup
let callTimerInterval = null;
let callStartTime = null;
let isMuted = false;
let intentionalDisconnect = false; // suppresses generic "Disconnected" status on deliberate socket.disconnect()
let remoteRouteCtx  = null; // AudioContext used for earpiece routing (Web Audio trick)
let remoteAudioNode = null; // MediaStreamSource connected to remoteRouteCtx
let remoteStream    = null; // remote MediaStream received via pc.ontrack

// ── DOM refs ──────────────────────────────────────────────────────────────
const elStatusDot   = document.getElementById('status-dot');
const elStatusText  = document.getElementById('status-text');
const elLineBadge   = document.getElementById('line-badge');
const elLineNum     = document.getElementById('line-num');
const elNumberText  = document.getElementById('number-text');
const elBtnCall     = document.getElementById('btn-call');
const elBtnHangup   = document.getElementById('btn-hangup');
const elBtnMute        = document.getElementById('btn-mute');
const elBtnSpeaker     = document.getElementById('btn-speaker');
const elIncallWideRow  = document.getElementById('incall-wide-row');
const elBtnRedial   = document.getElementById('btn-redial');
const elBtnClear    = document.getElementById('btn-clear');
const elBtnBack     = document.getElementById('btn-back');
const elBtnPaste    = document.getElementById('btn-paste');
const elBtnContacts = document.getElementById('btn-contacts');
const elIncallOverlay = document.getElementById('incall-overlay');
const elIncallNumber  = document.getElementById('incall-number');
const elIncallTimer   = document.getElementById('incall-timer');
const elToast         = document.getElementById('toast');
const elDialpad       = document.getElementById('dialpad');

let dialedNumber = '';
let isSpeaker = false;
let audioRoutes = [];
let currentRouteIdx = 0;

// ── Wake lock ─────────────────────────────────────────────────────────────
let wakeLock = null;

async function acquireWakeLock() {
  if (!('wakeLock' in navigator)) return;
  try {
    wakeLock = await navigator.wakeLock.request('screen');
    wakeLock.addEventListener('release', () => { wakeLock = null; });
  } catch (e) { /* denied or unavailable */ }
}

function releaseWakeLock() {
  if (wakeLock) { wakeLock.release().catch(() => {}); wakeLock = null; }
}

// Re-acquire if page becomes visible again (iOS drops wake lock on tab switch)
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' &&
      (callState === 'dialing' || callState === 'incall')) {
    acquireWakeLock();
  }
});

// ── Haptic feedback ───────────────────────────────────────────────────────
function haptic(pattern = 8) {
  if (navigator.vibrate) navigator.vibrate(pattern);
}

// ── DTMF tones (Web Audio API) ────────────────────────────────────────────
const DTMF_FREQS = {
  '1': [697, 1209], '2': [697, 1336], '3': [697, 1477],
  '4': [770, 1209], '5': [770, 1336], '6': [770, 1477],
  '7': [852, 1209], '8': [852, 1336], '9': [852, 1477],
  '*': [941, 1209], '0': [941, 1336], '#': [941, 1477],
};
let audioCtx = null;

function playDTMF(digit) {
  const freqs = DTMF_FREQS[digit];
  if (!freqs) return;
  try {
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    // iOS suspends AudioContext when the tab is backgrounded; resume before playing
    if (audioCtx.state === 'suspended') audioCtx.resume();
    const t = audioCtx.currentTime;
    freqs.forEach(freq => {
      const osc  = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.type = 'sine';
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.12, t);
      gain.gain.exponentialRampToValueAtTime(0.001, t + 0.10);
      osc.connect(gain);
      gain.connect(audioCtx.destination);
      osc.start(t);
      osc.stop(t + 0.10);
    });
  } catch (e) { /* audio context blocked */ }
}

// ── Call history ──────────────────────────────────────────────────────────
function getCallHistory() {
  try { return JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]'); }
  catch { return []; }
}

function saveCallToHistory(number, durationSecs) {
  const history = getCallHistory();
  history.unshift({ number, duration: durationSecs, time: Date.now() });
  if (history.length > HISTORY_MAX) history.pop();
  localStorage.setItem(HISTORY_KEY, JSON.stringify(history));
}

function renderHistory() {
  const list = document.getElementById('history-list');
  const history = getCallHistory();
  if (history.length === 0) {
    list.innerHTML = '<div class="history-empty">No recent calls</div>';
    return;
  }
  list.innerHTML = history.map((item, i) => {
    const d    = new Date(item.time);
    const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    const date = d.toLocaleDateString([], { month: 'short', day: 'numeric' });
    const dur  = item.duration > 0 ? formatTimer(item.duration) : 'No answer';
    return `<div class="history-item" data-number="${item.number}">
      <div class="history-dial-btn">
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">
          <path d="M6.6 10.8c1.4 2.8 3.8 5.1 6.6 6.6l2.2-2.2c.3-.3.7-.4 1-.2 1.1.4 2.3.6 3.6.6.6 0 1 .4 1 1V20c0 .6-.4 1-1 1-9.4 0-17-7.6-17-17 0-.6.4-1 1-1h3.5c.6 0 1 .4 1 1 0 1.3.2 2.5.6 3.6.1.3 0 .7-.2 1L6.6 10.8z"/>
        </svg>
      </div>
      <div class="history-item-number">${item.number}</div>
      <div class="history-item-meta">${date} ${time}<br>${dur}</div>
    </div>`;
  }).join('');

  list.querySelectorAll('.history-item').forEach(el => {
    el.addEventListener('click', () => {
      dialedNumber = el.dataset.number;
      updateDisplay();
      closeHistoryPanel();
      haptic(6);
    });
  });
}

function openHistoryPanel() {
  renderHistory();
  show(document.getElementById('history-backdrop'));
  show(document.getElementById('history-panel'));
  haptic(6);
}

function closeHistoryPanel() {
  hide(document.getElementById('history-backdrop'));
  hide(document.getElementById('history-panel'));
}

document.getElementById('btn-history').addEventListener('click', openHistoryPanel);
document.getElementById('btn-history-close').addEventListener('click', closeHistoryPanel);
document.getElementById('history-backdrop').addEventListener('click', closeHistoryPanel);
document.getElementById('btn-history-clear-all').addEventListener('click', () => {
  localStorage.removeItem(HISTORY_KEY);
  renderHistory();
  haptic(6);
});

// ── Idle screen timeout ───────────────────────────────────────────────────
let idleWarnTimer    = null;
let idleCountdownInt = null;
let idleReleaseSecs  = 0;

const elIdleOverlay  = document.getElementById('idle-overlay');
const elIdleCountdown = document.getElementById('idle-countdown');

function resetIdleTimer() {
  clearTimeout(idleWarnTimer);
  clearInterval(idleCountdownInt);
  hide(elIdleOverlay);
  if (callState !== 'idle') return;
  idleWarnTimer = setTimeout(showIdleWarning, IDLE_WARN_MS);
}

function showIdleWarning() {
  idleReleaseSecs = Math.round(IDLE_RELEASE_MS / 1000);
  elIdleCountdown.textContent = idleReleaseSecs;
  show(elIdleOverlay);
  haptic([50, 80, 50]);
  idleCountdownInt = setInterval(() => {
    idleReleaseSecs--;
    elIdleCountdown.textContent = idleReleaseSecs;
    if (idleReleaseSecs <= 0) {
      clearInterval(idleCountdownInt);
      localStorage.removeItem(TOKEN_KEY);
      token  = null;
      lineId = null;
      hide(elIdleOverlay);
      hide(elLineBadge);
      setStatus('error', 'Session timed out');
      showToast('Idle timeout — scan QR to reconnect', 6000);
      // Disconnect the socket so the server's disconnect_by_session handler
      // fires and releases the line via its 30-second auto-release timer.
      // Set flag first so the 'disconnect' handler doesn't overwrite our status.
      intentionalDisconnect = true;
      if (socket) socket.disconnect();
    }
  }, 1000);
}

document.getElementById('btn-idle-dismiss').addEventListener('click', () => {
  clearInterval(idleCountdownInt);
  hide(elIdleOverlay);
  resetIdleTimer();
  haptic(8);
});

// Any interaction resets idle timer
['click', 'pointerdown', 'keydown'].forEach(ev =>
  document.addEventListener(ev, () => {
    if (callState === 'idle') resetIdleTimer();
  }, { passive: true })
);

// ─────────────────────────────────────────────────────────────────────────
// Utility
// ─────────────────────────────────────────────────────────────────────────
function setStatus(state, text) {
  elStatusDot.className = 'dot dot-' + state;
  elStatusText.textContent = text;
}

function showToast(msg, durationMs = 2800) {
  elToast.textContent = msg;
  elToast.classList.remove('hidden');
  clearTimeout(elToast._t);
  elToast._t = setTimeout(() => elToast.classList.add('hidden'), durationMs);
}

function show(el)  { el.classList.remove('hidden'); }
function hide(el)  { el.classList.add('hidden'); }

function updateDisplay() {
  const lastNumber = localStorage.getItem(REDIAL_KEY) || '';
  if (dialedNumber.length === 0) {
    elNumberText.textContent = 'Enter number';
    elNumberText.classList.add('placeholder');
    elBtnCall.disabled = true;
    hide(elBtnClear);
    if (callState === 'idle' && lastNumber) show(elBtnRedial);
    else hide(elBtnRedial);
  } else {
    elNumberText.textContent = dialedNumber;
    elNumberText.classList.remove('placeholder');
    elBtnCall.disabled = (callState !== 'idle');
    show(elBtnClear);
    hide(elBtnRedial);
  }
}

function formatTimer(seconds) {
  const m = String(Math.floor(seconds / 60)).padStart(2, '0');
  const s = String(seconds % 60).padStart(2, '0');
  return `${m}:${s}`;
}

// ─────────────────────────────────────────────────────────────────────────
// Dialpad input
// ─────────────────────────────────────────────────────────────────────────
// Long-press 0 → inserts '+' instead
// Uses a timestamp instead of a boolean so the flag self-expires:
// iOS doesn't fire 'click' after a long press, so a plain boolean flag
// would stay true and silently drop the next normal 0 tap.
let zeroLongPressTimer = null;
let zeroLongPressTime  = 0;   // ms timestamp of last long-press fire (0 = none)

document.querySelectorAll('.key').forEach(btn => {
  const digit = btn.dataset.digit;

  // Long-press on '0' → '+'
  if (digit === '0') {
    btn.addEventListener('pointerdown', () => {
      zeroLongPressTime  = 0;   // reset on each new press
      zeroLongPressTimer = setTimeout(() => {
        zeroLongPressTime = Date.now();
        if (callState === 'idle' && dialedNumber.length < 20) {
          dialedNumber += '+';
          updateDisplay();
          haptic([10, 30, 10]);
          playDTMF('0');
        }
      }, 600);
    });
    ['pointerup', 'pointercancel'].forEach(ev =>
      btn.addEventListener(ev, () => clearTimeout(zeroLongPressTimer))
    );
  }

  btn.addEventListener('click', () => {
    // Suppress click if it fired within 500 ms of a long-press (handles Android;
    // iOS usually doesn't fire click after long press at all, but this is safe either way)
    if (digit === '0' && zeroLongPressTime > 0 && Date.now() - zeroLongPressTime < 500) return;

    if (callState === 'incall') {
      // Send DTMF in-call
      playDTMF(digit);
      haptic(6);
      if (socket && token) socket.emit('phone_dtmf', { token, digit });
      return;
    }

    if (callState !== 'idle') return;
    if (dialedNumber.length < 20) {
      dialedNumber += digit;
      updateDisplay();
      haptic(6);
      playDTMF(digit);
    }
  });
});

elBtnClear.addEventListener('click', () => {
  if (callState !== 'idle') return;
  dialedNumber = '';
  updateDisplay();
});

elBtnBack.addEventListener('click', () => {
  if (callState !== 'idle') return;
  dialedNumber = dialedNumber.slice(0, -1);
  updateDisplay();
});

// Long-press backspace = clear all (idle only)
let backPressTimer = null;
elBtnBack.addEventListener('pointerdown', () => {
  backPressTimer = setTimeout(() => {
    if (callState !== 'idle') return;
    dialedNumber = '';
    updateDisplay();
  }, 700);
});
['pointerup', 'pointercancel'].forEach(ev =>
  elBtnBack.addEventListener(ev, () => clearTimeout(backPressTimer))
);

// ─────────────────────────────────────────────────────────────────────────
// Paste button (iPhone primary contact flow)
// ─────────────────────────────────────────────────────────────────────────
elBtnPaste.addEventListener('click', async () => {
  if (callState !== 'idle') return;  // don't overwrite dialedNumber during a call
  try {
    const text = await navigator.clipboard.readText();
    const digits = text.replace(/\D/g, '').slice(0, 20);
    if (digits.length > 0) {
      dialedNumber = digits;
      updateDisplay();
      showToast('Number pasted ✓');
    } else {
      showToast('No number found in clipboard');
    }
  } catch {
    showToast('Tap: copy a phone number first, then paste');
  }
});

// ─────────────────────────────────────────────────────────────────────────
// Contact picker
// Android Chrome  → Contact Picker API (native picker sheet)
// iOS Safari/other → hidden <input type="tel"> trick:
//   focusing it makes iOS show the keyboard + QuickType contact bar
// ─────────────────────────────────────────────────────────────────────────
if ('contacts' in navigator && typeof navigator.contacts.select === 'function') {
  // ── Android Chrome ────────────────────────────────────────────────────
  show(elBtnContacts);
  elBtnContacts.addEventListener('click', async () => {
    if (callState !== 'idle') return;  // don't open picker during a call
    try {
      const results = await navigator.contacts.select(['tel'], { multiple: false });
      if (results && results.length > 0 && results[0].tel && results[0].tel.length > 0) {
        const digits = results[0].tel[0].replace(/\D/g, '').slice(0, 20);
        if (digits.length > 0) {
          dialedNumber = digits;
          updateDisplay();
          showToast('Contact selected ✓');
          haptic(6);
        }
      }
    } catch (e) {
      if (e.name !== 'AbortError') showToast('Contact picker failed');
    }
  });

} else {
  // ── iOS Safari + any browser without Contact Picker API ───────────────
  // Show the contacts button and wire it to the hidden <input type="tel">.
  // When focused, iOS presents its native keyboard with a QuickType row of
  // contact suggestions at the top — tap one to fill the number in.
  const elTelInput = document.getElementById('tel-contact-input');
  if (elTelInput) {
    show(elBtnContacts);

    elBtnContacts.addEventListener('click', () => {
      if (callState !== 'idle') return;  // don't open keyboard during a call
      elTelInput.value = '';
      elTelInput.focus();
      showToast('Pick a contact above the keyboard', 4000);
      haptic(6);
    });

    // Real-time update as user types or as QuickType fills digits.
    // Does NOT blur — let the user tap Done when ready.
    elTelInput.addEventListener('input', () => {
      const digits = elTelInput.value.replace(/[^\d+*#]/g, '').slice(0, 20);
      if (digits.length > 0) {
        dialedNumber = digits;
        updateDisplay();
      }
    });

    // Blur is the authoritative finalizer — fires when the user taps Done,
    // taps outside, or when a QuickType contact is picked (which auto-blurs
    // on some iOS versions without ever firing 'input').
    elTelInput.addEventListener('blur', () => {
      const digits = elTelInput.value.replace(/[^\d+*#]/g, '').slice(0, 20);
      elTelInput.value = '';  // clear regardless
      if (digits.length > 0) {
        // Fallback capture: QuickType filled the field but 'input' never fired
        if (digits !== dialedNumber) {
          dialedNumber = digits;
          updateDisplay();
        }
        showToast('Contact loaded ✓');
        haptic(6);
      }
      // If digits is empty the user dismissed without picking — silent clean-up
    });
  }
}

// ─────────────────────────────────────────────────────────────────────────
// WebRTC helpers
// ─────────────────────────────────────────────────────────────────────────
async function createPeerConnection() {
  if (pc) {
    pc.close();
    pc = null;
  }

  // No STUN needed — all traffic stays on the local LAN.
  // Host ICE candidates are sufficient for Pi ↔ phone on the same network.
  pc = new RTCPeerConnection({ iceServers: [] });

  // Get mic — works on http:// for .local mDNS addresses on Chrome/Android.
  // Firefox also allows getUserMedia on LAN origins.
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    showToast('Microphone not available — try Chrome on Android or Safari on iOS');
    setStatus('error', 'No mic API');
    throw new Error('getUserMedia not available on this browser/origin');
  }

  try {
    localStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        sampleRate: 48000,
        channelCount: 1
      },
      video: false
    });
    localStream.getTracks().forEach(t => pc.addTrack(t, localStream));
  } catch (e) {
    if (e.name === 'NotAllowedError') {
      showToast('Allow microphone access and try again', 4000);
    } else if (e.name === 'NotFoundError') {
      showToast('No microphone found on this device', 4000);
    } else {
      showToast('Microphone error: ' + e.message, 4000);
    }
    setStatus('error', 'Mic blocked');
    throw e;
  }

  // Remote audio → <audio> element
  // iOS Safari requires playsinline as an HTML attribute (setAttribute), not a JS property,
  // to correctly route audio to the earpiece instead of the loudspeaker.
  const remoteAudio = document.createElement('audio');
  remoteAudio.autoplay = true;
  remoteAudio.id = 'remote-audio';
  if (!isSpeaker) {
    remoteAudio.setAttribute('playsinline', '');
    remoteAudio.setAttribute('webkit-playsinline', '');
  }
  document.body.appendChild(remoteAudio);

  pc.ontrack = e => {
    if (e.streams && e.streams[0]) {
      remoteStream = e.streams[0];
      // Always look up the CURRENT element — applyAudioRoute may have replaced it.
      const ra = document.getElementById('remote-audio');
      if (ra) {
        ra.srcObject = remoteStream;
        ra.play().catch(() => {});
      }
    }
  };

  // ICE candidates → server
  pc.onicecandidate = e => {
    if (e.candidate) {
      socket.emit('phone_ice_candidate', {
        token: token,
        candidate: e.candidate.toJSON()
      });
    }
  };

  pc.onconnectionstatechange = () => {
    // pc may have been set to null (and closed) by the time this async event
    // fires — guard before touching it to prevent a TypeError.
    if (!pc) return;
    const s = pc.connectionState;
    if (s === 'connected') {
      setStatus('ready', 'Connected');
    } else if (s === 'failed' || s === 'disconnected') {
      if (callState === 'idle') return;  // phone_call_ended already handled this
      setStatus('error', 'Audio lost');
      showToast('Audio connection lost — try again');
      // Clean up pc and mic before resetting so nothing leaks
      const numWas = elIncallNumber.textContent || localStorage.getItem(REDIAL_KEY) || '';
      pc.close(); pc = null;
      if (localStream) { localStream.getTracks().forEach(t => t.stop()); localStream = null; }
      const ra = document.getElementById('remote-audio');
      if (ra) ra.remove();
      // Save to history only if we had an active call (not just dialing)
      resetToIdle(callState === 'incall', numWas);
    }
  };

  return pc;
}

async function handleOffer(sdpOffer) {
  try {
    await pc.setRemoteDescription({ type: 'offer', sdp: sdpOffer });
    const answer = await pc.createAnswer();
    await pc.setLocalDescription(answer);
    socket.emit('phone_answer', {
      token: token,
      sdp: answer.sdp
    });
  } catch (e) {
    console.error('SDP negotiation failed:', e);
    showToast('Call setup failed');
    if (pc) { pc.close(); pc = null; }
    resetToIdle();
  }
}

// ─────────────────────────────────────────────────────────────────────────
// Call state machine
// ─────────────────────────────────────────────────────────────────────────
function enterDialing(number) {
  callState = 'dialing';
  clearTimeout(idleWarnTimer);
  clearInterval(idleCountdownInt);
  hide(elIdleOverlay);
  hide(elBtnCall);
  show(elBtnHangup);
  show(elIncallWideRow);
  show(elIncallOverlay);
  elIncallNumber.textContent = number;
  elIncallTimer.textContent = '00:00';
  setStatus('incall', 'Calling…');
  elDialpad.classList.add('ring-active');
  acquireWakeLock();
  haptic([10, 40, 10]);
}

function enterIncall() {
  callState = 'incall';
  clearInterval(callTimerInterval);   // defensive: prevent stacked timers
  hide(elBtnCall);
  show(elBtnHangup);
  show(elIncallWideRow);
  show(elIncallOverlay);
  callStartTime = Date.now();
  callTimerInterval = setInterval(() => {
    const secs = Math.floor((Date.now() - callStartTime) / 1000);
    elIncallTimer.textContent = formatTimer(secs);
  }, 1000);
  setStatus('incall', 'In call');
  elDialpad.classList.add('ring-active');
  acquireWakeLock();
  haptic([10, 60, 10]);
  // Detect Bluetooth/AirPods now that mic permission is granted (labels become visible)
  buildAudioRoutes(true); // apply initial route (earpiece or speaker)
}

function resetToIdle(saveHistory = false, calledNumber = '') {
  // Save call to history before clearing state
  if (saveHistory && calledNumber) {
    const durationSecs = callStartTime
      ? Math.floor((Date.now() - callStartTime) / 1000)
      : 0;
    saveCallToHistory(calledNumber, durationSecs);
  }

  callState = 'idle';
  isMuted = false;
  audioRoutes = [];
  currentRouteIdx = 0;
  // Clean up Web Audio earpiece routing and remote stream reference
  if (remoteAudioNode) { remoteAudioNode.disconnect(); remoteAudioNode = null; }
  if (remoteRouteCtx)  { remoteRouteCtx.close().catch(() => {}); remoteRouteCtx = null; }
  remoteStream = null;
  elDialpad.classList.remove('ring-active');
  if (elBtnSpeaker) { const span = elBtnSpeaker.querySelector('span'); if (span) span.textContent = 'Speaker'; }
  clearInterval(callTimerInterval);
  callTimerInterval = null;
  callStartTime = null;
  show(elBtnCall);
  hide(elBtnHangup);
  hide(elIncallWideRow);
  hide(elIncallOverlay);
  elBtnMute.classList.remove('muted');
  if (localStream) {
    localStream.getTracks().forEach(t => t.enabled = true);
  }
  releaseWakeLock();
  haptic([8, 30, 8]);
  setStatus('ready', `Line ${lineId} ready`);
  updateDisplay();
  // Show redial if a last number is saved
  if (localStorage.getItem(REDIAL_KEY) && dialedNumber.length === 0) show(elBtnRedial);
  // Start idle countdown
  resetIdleTimer();
}

// ─────────────────────────────────────────────────────────────────────────
// Call / hangup buttons
// ─────────────────────────────────────────────────────────────────────────
async function startCall(number) {
  if (callState !== 'idle' || !number) return;
  if (!lineId) { showToast('Not connected — wait a moment'); return; }

  try {
    await createPeerConnection();
    localStorage.setItem(REDIAL_KEY, number);
    hide(elBtnRedial);
    enterDialing(number);
    socket.emit('phone_dial', { token, number });
  } catch (e) {
    // createPeerConnection already created an RTCPeerConnection before throwing —
    // close it here so it doesn't leak
    if (pc) { pc.close(); pc = null; }
    if (localStream) { localStream.getTracks().forEach(t => t.stop()); localStream = null; }
    resetToIdle();
  }
}

elBtnCall.addEventListener('click', () => {
  if (dialedNumber.length === 0) return;
  startCall(dialedNumber);
});

elBtnRedial.addEventListener('click', () => {
  const last = localStorage.getItem(REDIAL_KEY);
  if (!last) return;
  dialedNumber = last;
  updateDisplay();
});

elBtnHangup.addEventListener('click', () => {
  // Guard against rapid double-taps — second tap would create a spurious
  // zero-duration history entry and call resetToIdle a second time.
  if (callState === 'idle') return;
  const numWas = elIncallNumber.textContent || localStorage.getItem(REDIAL_KEY) || '';
  socket.emit('phone_hangup', { token: token });
  if (pc) { pc.close(); pc = null; }
  if (localStream) { localStream.getTracks().forEach(t => t.stop()); localStream = null; }
  const ra = document.getElementById('remote-audio');
  if (ra) ra.remove();
  haptic([10, 80, 20]);
  resetToIdle(true, numWas);
  showToast('Call ended');
});

elBtnMute.addEventListener('click', () => {
  if (!localStream) return;
  isMuted = !isMuted;
  localStream.getTracks().forEach(t => {
    if (t.kind === 'audio') t.enabled = !isMuted;
  });
  elBtnMute.classList.toggle('muted', isMuted);
  showToast(isMuted ? 'Muted' : 'Unmuted');
});

// ── Audio output routing ──────────────────────────────────────────────────────
// Builds a list of available audio routes: Earpiece, any Bluetooth/AirPods
// devices detected via enumerateDevices(), and Speaker. Tapping the Speaker
// button cycles through the list. On Chrome/Android setSinkId() is used for
// precise device switching. On iOS Safari the playsinline attribute controls
// whether the <audio> element routes to earpiece or loudspeaker.

async function buildAudioRoutes(applyInitial = false) {
  const prevId = audioRoutes[currentRouteIdx]?.id || null;

  audioRoutes = [{ id: 'earpiece', label: 'Earpiece' }];

  try {
    const devices = await navigator.mediaDevices.enumerateDevices();
    devices
      .filter(d => d.kind === 'audiooutput' &&
                   /airpod|bluetooth|wireless|headphone|headset|buds/i.test(d.label))
      .forEach(d => {
        const label = d.label.replace(/\s*\(.*\)$/, '').trim().split(' ').slice(0, 2).join(' ') || 'Bluetooth';
        audioRoutes.push({ id: d.deviceId, label });
      });
  } catch (e) {}

  audioRoutes.push({ id: 'speaker', label: 'Speaker' });

  if (prevId) {
    const sameIdx = audioRoutes.findIndex(r => r.id === prevId);
    if (sameIdx >= 0) {
      currentRouteIdx = sameIdx;
    } else {
      applyInitial = true;
      currentRouteIdx = isSpeaker ? audioRoutes.findIndex(r => r.id === 'speaker') : 0;
      if (currentRouteIdx < 0) currentRouteIdx = 0;
    }
  } else {
    if (isSpeaker) {
      currentRouteIdx = audioRoutes.findIndex(r => r.id === 'speaker');
      if (currentRouteIdx < 0) currentRouteIdx = audioRoutes.length - 1;
    } else {
      currentRouteIdx = 0;
    }
  }

  if (applyInitial) {
    await applyAudioRoute(audioRoutes[currentRouteIdx]).catch(() => {});
  }

  updateSpeakerBtn();
}

function updateSpeakerBtn() {
  const route = audioRoutes[currentRouteIdx] || { id: 'speaker', label: 'Speaker' };
  isSpeaker = (route.id === 'speaker');
  if (elBtnSpeaker) {
    elBtnSpeaker.classList.toggle('speaker-on', isSpeaker);
    const span = elBtnSpeaker.querySelector('span');
    if (span) span.textContent = route.label;
  }
}

async function applyAudioRoute(route) {
  if (remoteAudioNode) { remoteAudioNode.disconnect(); remoteAudioNode = null; }

  const ra = document.getElementById('remote-audio');

  if (route.id === 'earpiece') {
    if (ra) {
      ra.setAttribute('playsinline', '');
      ra.setAttribute('webkit-playsinline', '');
      ra.muted = false;
      if (!ra.srcObject && remoteStream) ra.srcObject = remoteStream;
      ra.play().catch(() => {});
    }
    return;
  }

  // Speaker / Bluetooth: remove playsinline so iOS routes to loudspeaker
  if (route.id === 'speaker' && ra) {
    ra.removeAttribute('playsinline');
    ra.removeAttribute('webkit-playsinline');
  }
  if (ra) {
    ra.muted = false;
    if (!ra.srcObject && remoteStream) ra.srcObject = remoteStream;
    ra.play().catch(() => {});
  }

  const hasSinkId = 'setSinkId' in HTMLAudioElement.prototype;
  if (hasSinkId && ra) {
    try {
      await ra.setSinkId(route.id === 'speaker' ? '' : route.id);
    } catch (e) {
      if (route.id !== 'speaker') {
        showToast('Could not switch to ' + route.label);
        currentRouteIdx = 0;
        updateSpeakerBtn();
      }
    }
  }
}

async function cycleAudioRoute() {
  if (audioRoutes.length === 0) await buildAudioRoutes(true);
  currentRouteIdx = (currentRouteIdx + 1) % audioRoutes.length;
  const route = audioRoutes[currentRouteIdx];
  await applyAudioRoute(route).catch(() => {});
  updateSpeakerBtn();
  showToast(route.label);
}

if (elBtnSpeaker) elBtnSpeaker.addEventListener('click', () => cycleAudioRoute());

// Re-detect audio outputs when Bluetooth devices connect / disconnect mid-call
if (navigator.mediaDevices) {
  navigator.mediaDevices.addEventListener('devicechange', async () => {
    if (callState !== 'incall') return;
    const prevLen = audioRoutes.length;
    await buildAudioRoutes(false);
    if (audioRoutes.length !== prevLen) {
      const added = audioRoutes.length > prevLen;
      const btDevice = audioRoutes.find(r => r.id !== 'earpiece' && r.id !== 'speaker');
      if (added && btDevice) showToast(btDevice.label + ' connected');
    }
  });
}

// ─────────────────────────────────────────────────────────────────────────
// Socket.io — connect + events
// ─────────────────────────────────────────────────────────────────────────
function initSocket() {
  // Force WebSocket transport only — no HTTP long-polling.
  // Polling keeps Safari's loading indicator spinning forever even after
  // the page is fully rendered, and is slower on a local LAN anyway.
  // Socket connects to same host:port the page loaded from (5443 for iPhone via HTTPS/WSS).
  socket = io({ transports: ['websocket'] });

  socket.on('connect', () => {
    setStatus('connecting', 'Registering line…');
    const savedToken = localStorage.getItem(TOKEN_KEY);
    socket.emit('phone_register', { token: savedToken || null });
  });

  socket.on('disconnect', () => {
    if (intentionalDisconnect) { intentionalDisconnect = false; return; }
    setStatus('error', 'Disconnected');
  });

  socket.on('reconnect', () => {
    const savedToken = localStorage.getItem(TOKEN_KEY);
    socket.emit('phone_register', { token: savedToken || null });
  });

  // Server confirmed line assignment
  socket.on('phone_registered', data => {
    if (data.error) {
      if (data.error === 'all_lines_busy') {
        setStatus('error', 'All lines busy');
        showToast(`All ${data.max_lines || 20} phone lines are in use — try again later`, 6000);
      } else {
        setStatus('error', 'Registration failed');
        showToast('Could not register phone line: ' + data.error, 5000);
      }
      return;
    }
    token  = data.token;
    lineId = data.line_id;
    localStorage.setItem(TOKEN_KEY, token);
    elLineNum.textContent = lineId;
    show(elLineBadge);
    setStatus('ready', `Line ${lineId} ready`);
    updateDisplay();
    resetIdleTimer();  // start 2-min idle clock once line is ready
  });

  // Server is sending us a WebRTC offer to bridge audio.
  // The offer arrives once at registration time (to pre-establish the audio
  // bridge), so callState is 'idle' here — do NOT guard on callState.
  socket.on('phone_offer', async data => {
    if (data.token !== token) return;
    try {
      // pc may already exist if dial was pressed before offer arrived
      if (!pc) await createPeerConnection();
      await handleOffer(data.sdp);
    } catch (e) {
      console.error('phone_offer handling failed:', e);
    }
  });

  // Server forwarded an ICE candidate from aiortc
  socket.on('phone_ice', data => {
    if (data.token !== token || !pc) return;
    pc.addIceCandidate(new RTCIceCandidate(data.candidate)).catch(() => {});
  });

  // Call connected on PBX side
  socket.on('phone_call_connected', data => {
    if (data.token !== token) return;
    if (callState !== 'dialing') return;  // ignore if user already hung up
    enterIncall();
    showToast('Connected');
  });

  // Call ended from remote/PBX side
  socket.on('phone_call_ended', data => {
    if (data.token !== token) return;
    if (callState === 'idle') return;  // stale/duplicate event — already reset
    const numWas = elIncallNumber.textContent || localStorage.getItem(REDIAL_KEY) || '';
    if (pc) { pc.close(); pc = null; }
    if (localStream) { localStream.getTracks().forEach(t => t.stop()); localStream = null; }
    const ra = document.getElementById('remote-audio');
    if (ra) ra.remove();
    resetToIdle(true, numWas);
    showToast(data.reason || 'Call ended');
  });

  // Call failed (busy, no answer, etc.)
  socket.on('phone_call_failed', data => {
    if (data.token !== token) return;
    if (callState === 'idle') return;  // stale/duplicate event — already reset
    const numWas = elIncallNumber.textContent || localStorage.getItem(REDIAL_KEY) || '';
    if (pc) { pc.close(); pc = null; }
    if (localStream) { localStream.getTracks().forEach(t => t.stop()); localStream = null; }
    const ra = document.getElementById('remote-audio');
    if (ra) ra.remove();
    resetToIdle(true, numWas);
    showToast(data.reason || 'Call failed');
  });

  // Operator hit "Reset All Phone Lines" (Pi is moving to a new location)
  socket.on('phone_lines_reset', () => {
    closeHistoryPanel();  // dismiss if open — can't dial from it after reset anyway
    localStorage.removeItem(TOKEN_KEY);
    token = null;
    lineId = null;
    if (pc) { pc.close(); pc = null; }
    if (localStream) { localStream.getTracks().forEach(t => t.stop()); localStream = null; }
    const ra = document.getElementById('remote-audio');
    if (ra) ra.remove();
    clearInterval(callTimerInterval);
    clearTimeout(idleWarnTimer);
    clearInterval(idleCountdownInt);
    callTimerInterval = null;
    callStartTime = null;
    callState = 'idle';
    elDialpad.classList.remove('ring-active');
    hide(elBtnHangup);
    hide(elIncallWideRow);
    hide(elIncallOverlay);
    hide(elIdleOverlay);
    show(elBtnCall);
    elBtnCall.disabled = true;
    hide(elLineBadge);
    releaseWakeLock();
    setStatus('error', 'Session ended');
    showToast('Phone lines reset — please scan the QR code again', 8000);
    // Do NOT auto re-register. User must scan the QR code at the new location.
  });
}

// ─────────────────────────────────────────────────────────────────────────
// PWA service worker — only registers on HTTPS or localhost
// (Service workers are blocked by browsers on plain HTTP origins)
// ─────────────────────────────────────────────────────────────────────────
if ('serviceWorker' in navigator && (location.protocol === 'https:' || location.hostname === 'localhost')) {
  navigator.serviceWorker.register('/static/sw.js').catch(() => {});
}

// ─────────────────────────────────────────────────────────────────────────
// Portrait lock — Android uses screen.orientation.lock(); iOS Safari uses
// a JS body-transform since CSS transforms on <html> are unreliable in WebKit.
// ─────────────────────────────────────────────────────────────────────────
try {
  if (screen.orientation && typeof screen.orientation.lock === 'function') {
    screen.orientation.lock('portrait').catch(() => {});
  }
} catch(e) {}

(function() {
  const PROPS = ['position','top','left','width','height','transform','transform-origin','max-width','margin'];
  function doLock() {
    if (window.innerWidth > window.innerHeight) {
      const w = window.innerWidth, h = window.innerHeight;
      const s = document.body.style;
      s.setProperty('position', 'fixed', 'important');
      s.setProperty('top',    h + 'px',  'important');
      s.setProperty('left',   '0',        'important');
      s.setProperty('width',  h + 'px',  'important');
      s.setProperty('height', w + 'px',  'important');
      s.setProperty('transform', 'rotate(-90deg)', 'important');
      s.setProperty('transform-origin', 'top left', 'important');
      s.setProperty('max-width', 'none', 'important');
      s.setProperty('margin',  '0',      'important');
    } else {
      PROPS.forEach(p => document.body.style.removeProperty(p));
    }
  }
  function lockPortrait() { setTimeout(doLock, 50); setTimeout(doLock, 300); }
  window.addEventListener('resize', lockPortrait);
  window.addEventListener('orientationchange', lockPortrait);
  doLock();
}());

// ─────────────────────────────────────────────────────────────────────────
// Boot
// ─────────────────────────────────────────────────────────────────────────
setStatus('connecting', 'Connecting…');
updateDisplay();
initSocket();

// Release mic if user closes tab
window.addEventListener('beforeunload', () => {
  if (localStream) localStream.getTracks().forEach(t => t.stop());
});
