'use strict';

// ── Per-channel WebRTC state ──────────────────────────────────────────────
// Each channel that is active gets an entry: { pc, audio }
// pc    = RTCPeerConnection
// audio = <audio> element playing the stream
const chState = {};   // ch (int) → { pc, audio } | undefined

// ── Socket.IO ────────────────────────────────────────────────────────────
let socket = null;

// ── DOM refs ──────────────────────────────────────────────────────────────
const elStatusDot  = document.getElementById('status-dot');
const elStatusText = document.getElementById('status-text');
const elGrid       = document.getElementById('channel-grid');
const elHint       = document.getElementById('hint-text');
const elActiveBar  = document.getElementById('active-bar');
const elActiveText = document.getElementById('active-bar-text');
const elBtnStopAll = document.getElementById('btn-stop-all');
const elToast      = document.getElementById('toast');

// ── Jack label helper ─────────────────────────────────────────────────────
// CH 1,2 → Jack 1 | CH 3,4 → Jack 2 | CH 5,6 → Jack 3 | CH 7,8 → Jack 4
function jackLabel(ch) {
  return 'Jack ' + Math.ceil(ch / 2);
}

// ── Utility ───────────────────────────────────────────────────────────────
function setStatus(state, text) {
  elStatusDot.className = 'dot dot-' + state;
  elStatusText.textContent = text;
}

function showToast(msg, ms) {
  ms = ms || 2500;
  elToast.textContent = msg;
  elToast.classList.remove('hidden');
  clearTimeout(elToast._t);
  elToast._t = setTimeout(function () { elToast.classList.add('hidden'); }, ms);
}

// ── Active bar ────────────────────────────────────────────────────────────
function refreshActiveBar() {
  const count = Object.keys(chState).length;
  if (count === 0) {
    elActiveBar.classList.add('hidden');
  } else {
    elActiveBar.classList.remove('hidden');
    elActiveText.textContent = 'Monitoring ' + count + ' channel' + (count === 1 ? '' : 's');
  }
}

// ── Build grid ────────────────────────────────────────────────────────────
function buildGrid() {
  elGrid.innerHTML = '';
  for (let ch = 1; ch <= 8; ch++) {
    const card = document.createElement('div');
    card.className = 'ch-card state-idle';
    card.id = 'ch-card-' + ch;
    card.innerHTML =
      '<div class="card-pulse"></div>' +
      '<svg class="card-icon" viewBox="0 0 24 24" fill="none"' +
      ' stroke="currentColor" stroke-width="2"' +
      ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M3 18v-6a9 9 0 0 1 18 0v6"/>' +
      '<path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3z"/>' +
      '<path d="M3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z"/>' +
      '</svg>' +
      '<div class="card-ch-num">CH ' + ch + '</div>' +
      '<div class="card-jack-label">' + jackLabel(ch) + '</div>' +
      '<div class="card-status">Idle</div>';
    card.addEventListener('click', function () { onCardTap(ch); });
    elGrid.appendChild(card);
  }
}

function setCardState(ch, state) {
  // state: 'idle' | 'connecting' | 'active'
  const card = document.getElementById('ch-card-' + ch);
  if (!card) return;
  card.className = 'ch-card state-' + state;
  const label = card.querySelector('.card-status');
  if (!label) return;
  if (state === 'idle')       label.textContent = 'Idle';
  else if (state === 'connecting') label.textContent = 'Connecting…';
  else if (state === 'active')     label.textContent = 'Listening';
}

// ── Start monitoring a channel ────────────────────────────────────────────
function startChannel(ch) {
  if (chState[ch]) return;   // already active
  if (!socket || !socket.connected) {
    showToast('Not connected — please wait');
    return;
  }

  setCardState(ch, 'connecting');

  const pc = new RTCPeerConnection({ iceServers: [] });
  // We only receive audio; add a receive-only transceiver so the offer
  // that the server creates includes an audio m-line we can accept.
  pc.addTransceiver('audio', { direction: 'recvonly' });

  const audio = document.createElement('audio');
  audio.autoplay = true;
  audio.playsInline = true;
  // Keep the element in the DOM (off-screen) so Safari doesn't stop playback
  audio.style.cssText = 'position:fixed;top:-999px;left:-999px;width:0;height:0;';
  document.body.appendChild(audio);

  chState[ch] = { pc: pc, audio: audio };

  pc.ontrack = function (evt) {
    if (evt.streams && evt.streams[0]) {
      audio.srcObject = evt.streams[0];
    } else {
      // Fallback: wrap the track in a new MediaStream
      const ms = new MediaStream([evt.track]);
      audio.srcObject = ms;
    }
    // iOS requires a user-gesture-triggered play(); we call it here
    // since this fires inside the tap handler chain.
    audio.play().catch(function (e) {
      console.warn('CH ' + ch + ': audio.play():', e);
    });
    setCardState(ch, 'active');
    refreshActiveBar();
  };

  pc.onconnectionstatechange = function () {
    const s = pc.connectionState;
    console.log('CH ' + ch + ': WebRTC state =', s);
    if (s === 'failed' || s === 'closed' || s === 'disconnected') {
      _cleanupChannel(ch, false);
    }
  };

  pc.onicecandidate = function (evt) {
    if (evt.candidate && socket) {
      socket.emit('ch_monitor_ice', {
        ch:        ch,
        candidate: evt.candidate.toJSON(),
      });
    }
  };

  // Ask the server to start sending audio for this channel
  socket.emit('ch_monitor_subscribe', { ch: ch });
  refreshActiveBar();
}

// ── Stop monitoring a channel ─────────────────────────────────────────────
function stopChannel(ch, silent) {
  if (!chState[ch]) return;
  if (!silent && socket) {
    socket.emit('ch_monitor_stop', { ch: ch });
  }
  _cleanupChannel(ch, true);
}

function _cleanupChannel(ch, alreadyNotified) {
  const entry = chState[ch];
  if (!entry) return;
  delete chState[ch];

  if (entry.pc) {
    try { entry.pc.close(); } catch (_) {}
  }
  if (entry.audio) {
    entry.audio.pause();
    entry.audio.srcObject = null;
    try { entry.audio.remove(); } catch (_) {}
  }

  setCardState(ch, 'idle');
  refreshActiveBar();
}

// ── Card tap handler ──────────────────────────────────────────────────────
function onCardTap(ch) {
  if (chState[ch]) {
    stopChannel(ch, false);
    showToast('CH ' + ch + ' stopped');
  } else {
    startChannel(ch);
    showToast('CH ' + ch + ' connecting…');
  }
}

// ── Stop All ──────────────────────────────────────────────────────────────
elBtnStopAll.addEventListener('click', function () {
  Object.keys(chState).forEach(function (ch) {
    stopChannel(parseInt(ch, 10), false);
  });
  showToast('All channels stopped');
});

// ── Socket.IO setup ───────────────────────────────────────────────────────
function initSocket() {
  socket = io({ transports: ['websocket'], reconnectionDelay: 2000 });

  socket.on('connect', function () {
    setStatus('connected', 'Connected');
  });

  socket.on('disconnect', function () {
    setStatus('connecting', 'Reconnecting…');
    // Clean up all active channels locally — the server has lost state
    Object.keys(chState).forEach(function (ch) {
      _cleanupChannel(parseInt(ch, 10), true);
    });
    refreshActiveBar();
  });

  // Server sends us the WebRTC offer for a channel
  socket.on('ch_monitor_offer', function (data) {
    const ch = data.ch;
    const entry = chState[ch];
    if (!entry) return;   // channel was stopped before offer arrived

    entry.pc.setRemoteDescription({ type: 'offer', sdp: data.sdp })
      .then(function () { return entry.pc.createAnswer(); })
      .then(function (answer) { return entry.pc.setLocalDescription(answer); })
      .then(function () {
        socket.emit('ch_monitor_answer', {
          ch:  ch,
          sdp: entry.pc.localDescription.sdp,
        });
      })
      .catch(function (e) {
        console.error('CH ' + ch + ': WebRTC offer/answer error:', e);
        showToast('CH ' + ch + ' WebRTC error');
        _cleanupChannel(ch, false);
      });
  });

  // Server sends us an ICE candidate (we relay back via ch_monitor_ice above)
  // — in the current design the server gathers all ICE before sending the
  // offer, so this event may never fire, but handle it defensively.
  socket.on('ch_monitor_server_ice', function (data) {
    const ch = data.ch;
    const entry = chState[ch];
    if (!entry || !data.candidate) return;
    entry.pc.addIceCandidate(data.candidate).catch(function (e) {
      console.warn('CH ' + ch + ': addIceCandidate:', e);
    });
  });

  socket.on('ch_monitor_error', function (data) {
    const ch = (data || {}).ch;
    const msg = (data || {}).error || 'Unknown error';
    if (ch) _cleanupChannel(ch, true);
    showToast('Error' + (ch ? ' CH ' + ch : '') + ': ' + msg);
  });
}

// ── Boot ──────────────────────────────────────────────────────────────────
buildGrid();
initSocket();
