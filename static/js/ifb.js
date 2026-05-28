'use strict';

// ── State ─────────────────────────────────────────────────────────────────
let socket        = null;
let monitorPc     = null;   // RTCPeerConnection for the monitor stream
let monitorAudio  = null;   // <audio> element playing IFB audio
let monitorLineId = null;   // line currently being monitored (null = none)
let lineStates    = {};     // line_id → { state, caller_id, phone_number }

// ── DOM refs ──────────────────────────────────────────────────────────────
const elStatusDot    = document.getElementById('status-dot');
const elStatusText   = document.getElementById('status-text');
const elMonitorBar   = document.getElementById('monitor-bar');
const elMonitorText  = document.getElementById('monitor-bar-text');
const elBtnStop      = document.getElementById('btn-stop');
const elHintText     = document.getElementById('hint-text');
const elGrid         = document.getElementById('lines-grid');
const elToast        = document.getElementById('toast');

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

// ── Build / update line cards ─────────────────────────────────────────────
function buildGrid() {
  elGrid.innerHTML = '';
  for (let i = 1; i <= 8; i++) {
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
      <svg class="card-headphones" viewBox="0 0 24 24" fill="none"
           stroke="currentColor" stroke-width="2"
           stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M3 18v-6a9 9 0 0 1 18 0v6"/>
        <path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3z"/>
        <path d="M3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z"/>
      </svg>
    `;
    card.addEventListener('click', () => onCardTap(i));
    elGrid.appendChild(card);
  }
}

function updateCard(lineId) {
  const card = document.getElementById('line-card-' + lineId);
  if (!card) return;

  const info = lineStates[lineId] || {};
  const isMonitoring = (monitorLineId === lineId);
  const rawState = (info.state || 'idle').toLowerCase();
  const isActive = rawState === 'active' || rawState === 'connected';

  // Determine card state class
  let stateClass = 'state-idle';
  if (isMonitoring)       stateClass = 'state-monitoring';
  else if (isActive)      stateClass = 'state-active';

  card.className = 'line-card ' + stateClass;

  // State label text
  let stateLabel = 'Idle';
  if (isMonitoring)  stateLabel = 'Listening';
  else if (isActive) stateLabel = 'Active';

  card.querySelector('.card-state-label').textContent = stateLabel;

  // Caller ID
  const callerText = info.caller_id || info.phone_number || '—';
  card.querySelector('.card-caller').textContent =
    (isActive || isMonitoring) ? callerText : '—';
}

function updateAllCards() {
  for (let i = 1; i <= 8; i++) updateCard(i);
}

// ── Monitor bar ───────────────────────────────────────────────────────────
function showMonitorBar(lineId) {
  elMonitorText.textContent = 'Monitoring Line ' + lineId;
  elMonitorBar.classList.remove('hidden');
  elHintText.classList.add('hidden');
}

function hideMonitorBar() {
  elMonitorBar.classList.add('hidden');
  elHintText.classList.remove('hidden');
}

// ── Stop monitoring ───────────────────────────────────────────────────────
function stopMonitor(silent) {
  if (socket) socket.emit('monitor_unsubscribe', {});

  if (monitorPc) {
    try { monitorPc.close(); } catch (_) {}
    monitorPc = null;
  }
  if (monitorAudio) {
    monitorAudio.pause();
    monitorAudio.srcObject = null;
    monitorAudio.remove();
    monitorAudio = null;
  }

  const prev = monitorLineId;
  monitorLineId = null;

  if (prev !== null) updateCard(prev);
  hideMonitorBar();
  if (!silent) showToast('Stopped listening');
}

// ── Start monitoring a line ───────────────────────────────────────────────
function startMonitor(lineId) {
  // If already on this line, stop it (toggle off)
  if (monitorLineId === lineId) {
    stopMonitor(false);
    return;
  }

  // Stop any existing monitor first (silent)
  if (monitorLineId !== null) stopMonitor(true);

  monitorLineId = lineId;
  updateCard(lineId);
  showMonitorBar(lineId);
  socket.emit('monitor_subscribe', { line: lineId });
  showToast('Connecting to Line ' + lineId + '…');
}

// ── Card tap handler ──────────────────────────────────────────────────────
function onCardTap(lineId) {
  const info = lineStates[lineId] || {};
  const rawState = (info.state || 'idle').toLowerCase();
  const isActive = rawState === 'active' || rawState === 'connected';

  // Allow tapping if active OR if already monitoring this line (to stop)
  if (!isActive && monitorLineId !== lineId) {
    showToast('Line ' + lineId + ' is not active');
    return;
  }

  startMonitor(lineId);
}

// ── Handle WebRTC monitor offer from server ───────────────────────────────
async function handleMonitorOffer(data) {
  // Ignore stale offers that arrived for a line we are no longer monitoring
  if (data.line !== monitorLineId) return;
  // Clean up any stale PC
  if (monitorPc) {
    try { monitorPc.close(); } catch (_) {}
    monitorPc = null;
  }
  if (monitorAudio) {
    monitorAudio.pause();
    monitorAudio.srcObject = null;
    monitorAudio.remove();
    monitorAudio = null;
  }

  // Receive-only peer connection — no mic needed
  monitorPc = new RTCPeerConnection({ iceServers: [] });

  // Create audio element to play the IFB stream
  monitorAudio = document.createElement('audio');
  monitorAudio.id = 'ifb-audio';
  monitorAudio.autoplay = true;
  monitorAudio.playsInline = true;
  document.body.appendChild(monitorAudio);

  monitorPc.ontrack = (e) => {
    if (e.streams && e.streams[0]) {
      monitorAudio.srcObject = e.streams[0];
      monitorAudio.play().catch(() => {});
    }
  };

  monitorPc.onicecandidate = (e) => {
    if (e.candidate) {
      socket.emit('monitor_ice_candidate', { candidate: e.candidate.toJSON() });
    }
  };

  monitorPc.onconnectionstatechange = () => {
    const s = monitorPc ? monitorPc.connectionState : null;
    if (s === 'connected') {
      showToast('Listening to Line ' + monitorLineId);
    } else if (s === 'failed' || s === 'disconnected') {
      showToast('IFB connection lost');
      stopMonitor(true);
      updateAllCards();
    }
  };

  try {
    await monitorPc.setRemoteDescription({ type: 'offer', sdp: data.sdp });
    const answer = await monitorPc.createAnswer();
    await monitorPc.setLocalDescription(answer);
    socket.emit('monitor_answer', { sdp: answer.sdp });
  } catch (err) {
    console.error('IFB offer handling failed:', err);
    stopMonitor(true);
    updateAllCards();
    showToast('IFB setup failed');
  }
}

// ── Fetch initial line states ─────────────────────────────────────────────
function fetchLines() {
  fetch('/api/lines')
    .then(r => r.json())
    .then(data => {
      (data.lines || []).forEach(l => {
        lineStates[l.line_id] = {
          state:        l.state,
          caller_id:    l.caller_id,
          phone_number: l.phone_number
        };
      });
      updateAllCards();
    })
    .catch(() => {});
}

// ── Socket.io ─────────────────────────────────────────────────────────────
function initSocket() {
  socket = io({ transports: ['websocket'] });

  socket.on('connect', () => {
    setStatus('ready', 'Connected');
    fetchLines();
  });

  socket.on('disconnect', () => {
    setStatus('error', 'Disconnected');
    // Clean up monitor state — WebRTC connection is gone
    if (monitorLineId !== null) {
      stopMonitor(true);
      updateAllCards();
    }
  });

  // Live line state updates from server
  socket.on('line_status', (data) => {
    const lid = data.line_id;
    if (lid < 1 || lid > 8) return;

    lineStates[lid] = {
      state:        data.state,
      caller_id:    data.caller_id    || '',
      phone_number: data.phone_number || ''
    };

    const rawState = (data.state || '').toLowerCase();
    const isActive = rawState === 'active' || rawState === 'connected';

    // If we were monitoring this line and it went idle, stop
    if (monitorLineId === lid && !isActive) {
      stopMonitor(true);
      showToast('Line ' + lid + ' ended');
    }

    updateCard(lid);
  });

  // WebRTC offer from server for the monitor stream
  socket.on('monitor_offer', (data) => {
    handleMonitorOffer(data).catch((err) => {
      console.error('monitor_offer error:', err);
      showToast('IFB error — try again');
      stopMonitor(true);
      updateAllCards();
    });
  });

  // Server-side monitor error
  socket.on('monitor_error', (data) => {
    showToast('IFB error: ' + (data.error || 'unknown'));
    stopMonitor(true);
    updateAllCards();
  });
}

// ── Stop button ───────────────────────────────────────────────────────────
elBtnStop.addEventListener('click', () => stopMonitor(false));

// ── Release audio on tab close ────────────────────────────────────────────
window.addEventListener('beforeunload', () => {
  if (monitorAudio) monitorAudio.srcObject = null;
});

// ── Boot ──────────────────────────────────────────────────────────────────
setStatus('connecting', 'Connecting…');
buildGrid();
initSocket();
