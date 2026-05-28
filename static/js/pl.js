'use strict';

// ── Constants ─────────────────────────────────────────────────────────────
const PL_LINE_START = 29;
const PL_LINE_END   = 34;  // LINE 35 hidden by design

// ── State ─────────────────────────────────────────────────────────────────
let socket       = null;
let plPc         = null;   // RTCPeerConnection for PL audio
let plAudio      = null;   // <audio> element playing PL audio
let plMicStream  = null;   // getUserMedia stream
let plMicTrack   = null;   // audio track (enabled only during PTT)
let plLineId     = null;   // line I am currently ON (connected WebRTC)
let targetLineId = null;   // idle line selected as next dial target
let plIsDialer   = false;  // did I start this call?
// Per-line dialer memory: keeps track of which lines this session dialed.
// Prevents the Hang Up button from reverting to Leave when a cameraman
// switches between lines and the server's pl_role confirmation races
// the optimistic plIsDialer=false reset in onCardTap.
let dialerLines  = {};     // line_id → true  (lines this session dialed)
let isPtt        = false;
let lineStates   = {};     // line_id → {state, phone_number, caller_id}

// ── DOM refs ──────────────────────────────────────────────────────────────
const elStatusDot  = document.getElementById('status-dot');
const elStatusText = document.getElementById('status-text');
const elNumInput   = document.getElementById('num-input');
const elBtnAction  = document.getElementById('btn-action');
const elHintText   = document.getElementById('hint-text');
const elGrid       = document.getElementById('lines-grid');
const elBtnPtt     = document.getElementById('btn-ptt');
const elPttHint    = document.getElementById('ptt-hint');
const elToast      = document.getElementById('toast');

// ── Utility ───────────────────────────────────────────────────────────────
function setStatus(state, text) {
  elStatusDot.className = 'dot dot-' + state;
  elStatusText.textContent = text;
}

function showToast(msg, ms = 2800) {
  elToast.textContent = msg;
  elToast.classList.remove('hidden');
  clearTimeout(elToast._t);
  elToast._t = setTimeout(() => elToast.classList.add('hidden'), ms);
}

// ── Build line grid ───────────────────────────────────────────────────────
function buildGrid() {
  elGrid.innerHTML = '';
  for (let i = PL_LINE_START; i <= PL_LINE_END; i++) {
    const card = document.createElement('div');
    card.className = 'line-card';
    card.id = 'line-card-' + i;
    card.innerHTML = `
      <div class="card-line-num">Line ${i}</div>
      <div class="card-state-row">
        <span class="card-dot"></span>
        <span class="card-state-label">Idle</span>
      </div>
      <div class="card-caller">—</div>
      <span class="card-badge">✓</span>
    `;
    card.addEventListener('click', () => onCardTap(i));
    elGrid.appendChild(card);
  }
}

function updateCard(lineId) {
  const card = document.getElementById('line-card-' + lineId);
  if (!card) return;

  const info     = lineStates[lineId] || {};
  const rawState = (info.state || 'idle').toLowerCase();
  // 'dialing'/'ringing' lines are in-use — don't show as idle to other crew
  const isActive = rawState === 'connected' || rawState === 'dialing' || rawState === 'ringing';
  const isMe     = (plLineId === lineId);
  const isTarget = (targetLineId === lineId && plLineId === null);

  let stateClass = 'state-idle';
  if (isMe)           stateClass = 'state-selected';
  else if (isTarget)  stateClass = 'state-target';
  else if (isActive)  stateClass = 'state-active';

  card.className = 'line-card ' + stateClass;

  // State label
  const isCalling = rawState === 'dialing' || rawState === 'ringing';
  let label = 'Idle';
  if (isMe && isCalling)   label = 'Calling…';
  else if (isMe)           label = 'On Line';
  else if (isTarget)       label = 'Ready';
  else if (isCalling)      label = 'Calling…';
  else if (isActive)       label = 'Active';
  card.querySelector('.card-state-label').textContent = label;

  // Caller / number
  const callerText = info.caller_id || info.phone_number || '—';
  card.querySelector('.card-caller').textContent =
    (isActive || isMe) ? callerText : (isTarget ? 'Tap to change' : '—');

  // Badge
  const badge = card.querySelector('.card-badge');
  badge.textContent = isTarget ? '→' : '✓';
}

function updateAllCards() {
  for (let i = PL_LINE_START; i <= PL_LINE_END; i++) updateCard(i);
}

// ── Action button state ───────────────────────────────────────────────────
function refreshActionBtn() {
  const num = elNumInput.value.trim();

  if (plLineId !== null) {
    // On a line — show Hang Up or Leave
    elBtnAction.textContent = plIsDialer ? 'Hang Up' : 'Leave';
    elBtnAction.className   = plIsDialer ? 'hangup' : 'leave';
    elNumInput.disabled     = true;
    return;
  }

  // Validate targetLineId is still idle — clear it if the line was grabbed
  if (targetLineId !== null) {
    const tState = ((lineStates[targetLineId] || {}).state || 'idle').toLowerCase();
    if (tState !== 'idle') targetLineId = null;
  }

  if (targetLineId !== null && num.length > 0) {
    elBtnAction.textContent = 'Dial Line ' + targetLineId;
    elBtnAction.className   = '';
    elNumInput.disabled     = false;
    return;
  }

  if (num.length > 0) {
    // Number entered but no target line — find next free PL line
    const freeLine = getFreeLineId();
    if (freeLine) {
      elBtnAction.textContent = 'Dial Line ' + freeLine;
      elBtnAction.className   = '';
    } else {
      elBtnAction.textContent = 'No free lines';
      elBtnAction.className   = 'disabled';
    }
    elNumInput.disabled = false;
    return;
  }

  elBtnAction.textContent = 'Dial';
  elBtnAction.className   = 'disabled';
  elNumInput.disabled     = false;
}

function getFreeLineId() {
  for (let i = PL_LINE_START; i <= PL_LINE_END; i++) {
    const s = (lineStates[i] || {}).state || 'idle';
    if (s.toLowerCase() === 'idle') return i;
  }
  return null;
}

// ── PTT button state ──────────────────────────────────────────────────────
const PTT_WAVE = '<svg style="flex-shrink:0;margin-right:6px" width="20" height="16" viewBox="0 0 20 16" fill="currentColor"><rect x="0" y="5" width="2.5" height="6" rx="1.2"/><rect x="4.5" y="2" width="2.5" height="12" rx="1.2"/><rect x="9" y="0" width="2.5" height="16" rx="1.2"/><rect x="13.5" y="2" width="2.5" height="12" rx="1.2"/><rect x="18" y="5" width="2" height="6" rx="1.2"/></svg>';

function refreshPttBtn() {
  if (plLineId === null) {
    elBtnPtt.className   = 'ptt-idle';
    elBtnPtt.innerHTML   = PTT_WAVE + 'Hold to Talk';
    elPttHint.textContent = 'Select a line first';
    elPttHint.className  = '';
    return;
  }
  const webrtcLive = plPc && plPc.connectionState === 'connected';
  if (webrtcLive && isPtt) {
    elBtnPtt.className    = 'ptt-talking';
    elBtnPtt.innerHTML    = '<span class="ptt-tx-dot"></span>Talking…';
    elPttHint.textContent = 'Release to stop talking';
    elPttHint.className   = 'active';
    setStatus('transmit', 'Line ' + plLineId + ' · transmitting…');
  } else if (webrtcLive) {
    elBtnPtt.className    = 'ptt-ready';
    elBtnPtt.innerHTML    = PTT_WAVE + 'Hold to Talk';
    elPttHint.textContent = 'Line ' + plLineId + ' · listening';
    elPttHint.className   = 'active';
    setStatus('ready', 'Line ' + plLineId + ' · listening');
  } else {
    elBtnPtt.className    = 'ptt-idle';
    elBtnPtt.innerHTML    = PTT_WAVE + 'Hold to Talk';
    elPttHint.textContent = 'Line ' + plLineId + ' · connecting…';
    elPttHint.className   = 'active';
    setStatus('connecting', 'Line ' + plLineId + ' · connecting…');
  }
}

// ── PTT mic control ───────────────────────────────────────────────────────
//
// Toggling plMicTrack.enabled mutes the browser-side mic content (zeroed
// samples), but the WebRTC track keeps shipping ~50 fps of silence frames
// either way.  Without a server-side gate, those silence frames race a peer's
// real audio into the shared SIP RTP TX queue → studio cuts in/out.  So we
// ALSO signal the server, which has its own PTT gate on WebRTCPLBridge.
function setPtt(active) {
  if (plLineId === null) return;
  // Idempotency guard: avoid spamming the server with repeated identical
  // events (pointerleave + pointerup both fire when a touch is released).
  if (isPtt === active) return;
  isPtt = active;
  // NOTE: We deliberately do NOT mute plAudio here.
  //
  // The previous attempt muted the playback element during PTT to "help" the
  // browser's AEC.  On iOS Safari that flips AVAudioSession into record-only
  // mode, which engages aggressive AGC + noise gating on the mic — raw frame
  // peaks dropped from ~12000 to ~150 the moment PTT engaged, and the studio
  // heard digital silence instead of voice.  The studio operator does not
  // route the PL operator's own voice back to them, so there is no echo loop
  // to worry about; keeping playback active maintains full-duplex audio
  // session and normal mic gain.
  if (socket && socket.connected) {
    try { socket.emit('pl_ptt', { active: active }); } catch (_) {}
  }
  refreshPttBtn();
}

// ── Card tap ──────────────────────────────────────────────────────────────
function onCardTap(lineId) {
  const info      = lineStates[lineId] || {};
  const rawState  = (info.state || 'idle').toLowerCase();
  // Only a fully connected line can be joined — not one still dialing/ringing
  const isActive  = rawState === 'connected';
  const isInUse   = rawState === 'dialing' || rawState === 'ringing';

  // Already on this line — do nothing
  if (plLineId === lineId) return;

  // If I am connected to a different line, can't switch to a dialing/ringing one
  if (plLineId !== null && plLineId !== lineId) {
    if (isActive) {
      // Switch to this connected line
      doLeave();
      setTimeout(() => {
        socket.emit('pl_join', { line: lineId });
        plLineId = lineId;
        plIsDialer = !!dialerLines[lineId];  // restore if we originally dialed this line
        targetLineId = null;
        updateAllCards();
        refreshActionBtn();
        refreshPttBtn();
        showToast('Joining Line ' + lineId + '…');
      }, 300);
    } else if (isInUse) {
      showToast('Line ' + lineId + ' is still connecting…');
    } else {
      showToast('Line ' + lineId + ' is not active');
    }
    return;
  }

  if (isActive) {
    // Tap connected line → join it
    socket.emit('pl_join', { line: lineId });
    plLineId = lineId;
    plIsDialer = !!dialerLines[lineId];  // restore if we originally dialed this line
    targetLineId = null;
    updateAllCards();
    refreshActionBtn();
    refreshPttBtn();
    showToast('Joining Line ' + lineId + '…');
    return;
  }

  if (isInUse) {
    // Line is dialing/ringing — can't join yet
    showToast('Line ' + lineId + ' is connecting, wait…');
    return;
  }

  // Tap idle line → select as dial target (toggle)
  if (plLineId === null) {
    targetLineId = (targetLineId === lineId) ? null : lineId;
    updateAllCards();
    refreshActionBtn();
  }
}

// ── Dial ──────────────────────────────────────────────────────────────────
function doDial() {
  const num = elNumInput.value.trim();
  if (!num) { showToast('Enter a number first'); return; }

  // Re-validate targetLineId at dial time — it could have been grabbed since tap
  if (targetLineId !== null) {
    const tState = ((lineStates[targetLineId] || {}).state || 'idle').toLowerCase();
    if (tState !== 'idle') targetLineId = null;
  }

  const line = targetLineId || getFreeLineId();
  if (!line) { showToast('No free PL lines available'); return; }

  // Set plLineId NOW so handlePlOffer's guard check (data.line !== plLineId)
  // passes when the server sends the offer after the SIP call connects.
  plLineId     = line;
  plIsDialer   = true;
  dialerLines[line] = true;   // remember this session dialed this line
  targetLineId = null;

  socket.emit('pl_dial', { line: line, number: num });
  showToast('Dialing Line ' + line + '…');
  updateAllCards();
  refreshActionBtn();
  refreshPttBtn();
}

// ── Hang Up / Leave ───────────────────────────────────────────────────────
function doHangupOrLeave() {
  if (plLineId === null) return;
  const lineForToast = plLineId;  // save before cleanupPl() nulls it
  if (plIsDialer) {
    delete dialerLines[lineForToast];  // hung up — no longer the dialer
    socket.emit('pl_hangup', { line: lineForToast });
    cleanupPl();
    showToast('Hanging up Line ' + lineForToast);
  } else {
    if (socket) socket.emit('pl_leave', {});
    cleanupPl();
    showToast('Left Line ' + lineForToast);
  }
}

function doLeave() {
  if (socket) socket.emit('pl_leave', {});
  cleanupPl();
}

function cleanupPl() {
  // Stop mic
  setPtt(false);
  if (plMicStream) {
    plMicStream.getTracks().forEach(t => t.stop());
    plMicStream = null;
    plMicTrack  = null;
  }
  // Close WebRTC
  if (plPc) {
    try { plPc.close(); } catch (_) {}
    plPc = null;
  }
  if (plAudio) {
    plAudio.pause();
    plAudio.srcObject = null;
    plAudio.remove();
    plAudio = null;
  }
  const prev = plLineId;
  plLineId   = null;
  plIsDialer = false;
  isPtt      = false;

  if (prev !== null) updateCard(prev);
  updateAllCards();
  elNumInput.disabled = false;
  elNumInput.value    = '';
  refreshActionBtn();
  refreshPttBtn();
  setStatus('ready', 'Connected');
}

// ── WebRTC: handle pl_offer from server ──────────────────────────────────
async function handlePlOffer(data) {
  if (data.line !== plLineId) return; // stale offer

  // Clean up any existing PC
  if (plPc) {
    try { plPc.close(); } catch (_) {}
    plPc = null;
  }
  if (plAudio) {
    plAudio.pause();
    plAudio.srcObject = null;
    plAudio.remove();
    plAudio = null;
  }

  // Request mic access (PTT starts muted)
  try {
    plMicStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
    plMicTrack  = plMicStream.getAudioTracks()[0];
  } catch (err) {
    console.warn('PL: mic access denied:', err);
    plMicStream = null;
    plMicTrack  = null;
  }

  // Stale-check: user may have pressed Leave while the mic prompt was open.
  // If so, stop any acquired mic tracks and bail out cleanly.
  if (data.line !== plLineId) {
    if (plMicStream) {
      plMicStream.getTracks().forEach(t => t.stop());
      plMicStream = null;
      plMicTrack  = null;
    }
    return;
  }

  // Create peer connection
  plPc = new RTCPeerConnection({ iceServers: [] });

  // Add mic track (even if null, the server expects bidirectional)
  if (plMicStream) {
    plMicStream.getTracks().forEach(t => plPc.addTrack(t, plMicStream));
  }

  // Receive SIP line audio
  plAudio = document.createElement('audio');
  plAudio.id        = 'pl-audio';
  plAudio.autoplay  = true;
  plAudio.playsInline = true;
  document.body.appendChild(plAudio);

  plPc.ontrack = (e) => {
    if (e.streams && e.streams[0]) {
      plAudio.srcObject = e.streams[0];
      plAudio.play().catch(() => {});
    }
  };

  plPc.onicecandidate = (e) => {
    if (e.candidate) {
      socket.emit('pl_ice_candidate', { candidate: e.candidate.toJSON() });
    }
  };

  plPc.onconnectionstatechange = () => {
    const s = plPc ? plPc.connectionState : null;
    if (s === 'connected') {
      showToast('Connected to Line ' + plLineId);
      // The server bridge starts with its PTT gate closed.  Re-assert the
      // current state in case the user was already holding the button while
      // the WebRTC handshake was finishing (rare, but otherwise their first
      // press of the session would go nowhere).
      if (socket && socket.connected) {
        try { socket.emit('pl_ptt', { active: isPtt }); } catch (_) {}
      }
      refreshPttBtn();
    } else if (s === 'failed' || s === 'disconnected') {
      showToast('PL connection lost');
      cleanupPl();
    }
  };

  try {
    await plPc.setRemoteDescription({ type: 'offer', sdp: data.sdp });
    const answer = await plPc.createAnswer();
    await plPc.setLocalDescription(answer);
    // Final stale-check before sending answer — cleanupPl() may have run
    // during the async SDP negotiation above.
    if (data.line !== plLineId) return;
    socket.emit('pl_answer', { sdp: answer.sdp });
  } catch (err) {
    console.error('PL offer handling failed:', err);
    cleanupPl();
    showToast('PL setup failed');
  }
}

// ── Fetch initial PL line states ──────────────────────────────────────────
function fetchPlLines() {
  fetch('/api/pl/lines')
    .then(r => r.json())
    .then(data => {
      (data.lines || []).forEach(l => {
        lineStates[l.line_id] = {
          state:        l.state,
          caller_id:    l.caller_id,
          phone_number: l.phone_number
        };
      });
      // If we are on a PL line but the server says it is now idle/busy/error,
      // clean up — this catches hangups that happened while the socket was
      // briefly disconnected and whose events were never delivered.
      if (plLineId !== null) {
        const info = lineStates[plLineId] || {};
        const s = (info.state || '').toLowerCase();
        if (s === 'idle' || s === 'busy' || s === 'error') {
          showToast('Line ' + plLineId + ' ended');
          cleanupPl();
          return;
        }
      }
      updateAllCards();
    })
    .catch(() => {});
}

// ── Socket.io ─────────────────────────────────────────────────────────────

// Watchdog: poll line state every 5 s while on a PL line.
// Catches hangups whose Socket.IO events were dropped (brief disconnect,
// network hiccup) without waiting for a full socket reconnect.
setInterval(() => {
  if (plLineId === null) return;
  fetchPlLines();
}, 5000);

function initSocket() {
  socket = io({ transports: ['websocket'] });

  socket.on('connect', () => {
    setStatus('ready', 'Connected');
    fetchPlLines();
  });

  socket.on('disconnect', () => {
    // cleanupPl() first — it calls setStatus('ready','Connected') internally.
    // Then override with the real disconnected status so it's not overwritten.
    if (plLineId !== null) cleanupPl();
    setStatus('error', 'Disconnected');
  });

  // Line state updates — listen for PL lines 29-35
  socket.on('line_status', (data) => {
    const lid = data.line_id;
    if (lid < PL_LINE_START || lid > PL_LINE_END) return;

    lineStates[lid] = {
      state:        data.state,
      caller_id:    data.caller_id    || '',
      phone_number: data.phone_number || ''
    };

    const rawState = (data.state || '').toLowerCase();

    // Only clean up when the call is truly finished — NOT during dialing/ringing
    const isDone = rawState === 'idle' || rawState === 'busy' || rawState === 'error';
    if (plLineId === lid && isDone) {
      showToast('Line ' + lid + ' ended');
      cleanupPl();
      return;
    }

    updateCard(lid);
    refreshActionBtn();
  });

  // WebRTC offer from server for PL audio
  socket.on('pl_offer', (data) => {
    handlePlOffer(data).catch((err) => {
      console.error('pl_offer error:', err);
      showToast('PL error — try again');
      cleanupPl();
    });
  });

  // Server tells us our role on a PL line (dialer vs joiner).  This is the
  // source of truth — onCardTap optimistically sets plIsDialer=false on every
  // switch, and only this event flips it back to true for someone returning
  // to a line they originally dialed.  Without it the action button would be
  // stuck on "Leave" and the original dialer couldn't hang up their own call.
  socket.on('pl_role', (data) => {
    if (!data || data.line !== plLineId) return;
    const next = !!data.is_dialer;
    // Persist dialer status so switching away and back preserves Hang Up button
    if (next) {
      dialerLines[data.line] = true;
    } else {
      delete dialerLines[data.line];
    }
    if (plIsDialer === next) return;
    plIsDialer = next;
    refreshActionBtn();
  });

  // Call ended by server (dialer hung up or line dropped)
  socket.on('pl_call_ended', (data) => {
    if (data.line_id === plLineId) {
      showToast('Line ' + data.line_id + ' ended');
      delete dialerLines[data.line_id];  // line is gone — clear dialer memory
      cleanupPl();
    }
  });

  // Server-side error
  socket.on('pl_error', (data) => {
    showToast('PL error: ' + (data.error || 'unknown'));
    // Always full reset — plLineId may have been set optimistically by doDial()
    // but the server rejected the request, so tear everything down cleanly.
    cleanupPl();
  });
}

// ── Action button click ───────────────────────────────────────────────────
elBtnAction.addEventListener('click', () => {
  if (plLineId !== null) {
    doHangupOrLeave();
  } else {
    doDial();
  }
});

// ── Number input — update button on every keystroke ───────────────────────
elNumInput.addEventListener('input', () => refreshActionBtn());

// ── PTT — pointer events (works for mouse and touch) ─────────────────────
elBtnPtt.addEventListener('pointerdown', (e) => {
  if (plLineId === null) return;
  e.preventDefault();
  elBtnPtt.setPointerCapture(e.pointerId);
  setPtt(true);
});
elBtnPtt.addEventListener('pointerup',     () => setPtt(false));
elBtnPtt.addEventListener('pointercancel', () => setPtt(false));
elBtnPtt.addEventListener('pointerleave',  () => { if (isPtt) setPtt(false); });

// ── Release audio on tab close ────────────────────────────────────────────
window.addEventListener('beforeunload', () => {
  if (plAudio) plAudio.srcObject = null;
  if (plMicStream) plMicStream.getTracks().forEach(t => t.stop());
});

// ── Boot ──────────────────────────────────────────────────────────────────
setStatus('connecting', 'Connecting…');
buildGrid();
refreshActionBtn();
refreshPttBtn();
initSocket();
