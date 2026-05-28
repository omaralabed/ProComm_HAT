        // State management
        let lines = {};
        window.lines = lines;  // So updateIdleLineAvailabilityBadges() can update Idle -> Available when SIP registers
        let audioChannels = {}; // Maps channel number to line ID
        
        // ===============================
        // Custom Modal Confirm (no window.confirm)
        // ===============================
        let _confirmResolver = null;

        function showConfirmModal({ title = 'Confirm', message = '', okText = 'OK', cancelText = 'Cancel' } = {}) {
            const overlay = document.getElementById('confirm-modal');
            const titleEl = document.getElementById('confirm-title');
            const msgEl = document.getElementById('confirm-message');
            const cancelBtn = document.getElementById('confirm-cancel-btn');
            const okBtn = document.getElementById('confirm-ok-btn');

            if (!overlay || !titleEl || !msgEl || !cancelBtn || !okBtn) {
                // Fallback (should not happen): default to true
                return Promise.resolve(true);
            }

            // Prevent multiple modals stacking
            if (_confirmResolver) {
                return Promise.reject(new Error('A confirm dialog is already open.'));
            }

            titleEl.textContent = title;
            msgEl.innerHTML = message;  // Use innerHTML to render HTML tags like <br> and <strong>
            okBtn.textContent = okText;
            cancelBtn.textContent = cancelText;

            overlay.classList.add('show');
            overlay.setAttribute('aria-hidden', 'false');

            // Focus OK for keyboard users / PyQt
            setTimeout(() => okBtn.focus(), 0);

            return new Promise((resolve) => {
                _confirmResolver = resolve;

                const cleanup = () => {
                    overlay.classList.remove('show');
                    overlay.setAttribute('aria-hidden', 'true');
                    _confirmResolver = null;

                    // Remove listeners
                    cancelBtn.removeEventListener('click', onCancel);
                    okBtn.removeEventListener('click', onOk);
                    overlay.removeEventListener('click', onOverlay);
                    document.removeEventListener('keydown', onKeydown);
                };

                const onCancel = () => { cleanup(); resolve(false); };
                const onOk = () => { cleanup(); resolve(true); };

                // Click outside modal closes as cancel (nice UX)
                const onOverlay = (e) => {
                    if (e.target === overlay) onCancel();
                };

                const onKeydown = (e) => {
                    if (e.key === 'Escape') onCancel();
                    if (e.key === 'Enter') onOk();
                };

                cancelBtn.addEventListener('click', onCancel, { once: true });
                okBtn.addEventListener('click', onOk, { once: true });
                overlay.addEventListener('click', onOverlay);
                document.addEventListener('keydown', onKeydown);
            });
        }

        
        // Update system status indicator
        function updateSystemStatus(isConnected) {
            sipConnected = isConnected;
            const statusText = document.getElementById('system-status');
            const statusDot = document.querySelector('.status-dot');
            
            if (isConnected) {
                statusText.textContent = 'System Online';
                statusDot.classList.remove('offline');
            } else {
                statusText.textContent = 'System Offline';
                statusDot.classList.add('offline');
            }
            
            // Update SIP mode text to match system status
            setSipMode(isConnected ? 'LIVE' : 'POLLING');
            
            // Update idle line badges (Available when online, Idle when offline)
            updateIdleLineAvailabilityBadges();
        }
        
        async function refreshActiveCalls() {
            /**
             * Fetch current line state from backend and apply to UI.
             * Used on initial load (so refresh keeps last status) and after Socket reconnect.
             */
            try {
                const response = await apiFetch('/api/lines');
                if (!response || !response.ok) return;
                const data = await response.json();
                if (!data || !data.lines) return;
                for (const lineData of data.lines) {
                    const lineId = lineData.line_id;
                    if (!lines[lineId]) continue;
                    lines[lineId].state = lineData.state || 'idle';
                    lines[lineId].number = lineData.phone_number || '';
                    lines[lineId].remoteNumber = lineData.phone_number || '';
                    lines[lineId].contactName = lineData.caller_id || '';
                    lines[lineId].remoteName = lineData.caller_id || '';
                    lines[lineId].audioChannel = lineData.audio_channel !== undefined ? lineData.audio_channel : 0;
                    lines[lineId].duration = lineData.duration || 0;
                    if ((lineData.state === 'connected' || lineData.state === 'ringing' || lineData.state === 'dialing') && (lineData.duration || 0) > 0)
                        lines[lineId].callStart = Date.now() - (lineData.duration || 0) * 1000;
                    if (lineData.sip_registered) sipRegisteredLineIds.add(String(lineId));
                    else sipRegisteredLineIds.delete(String(lineId));
                }
                // Rebuild channel map so dropdowns show "In Use by Line X" correctly after refresh
                audioChannels = {};
                for (let i = 1; i <= 8; i++) {
                    const ch = lines[i] && lines[i].audioChannel;
                    if (ch > 0) audioChannels[ch] = i;
                }
                // Sync headset (Listen & Talk): the line with headset_listen_line has headset on (muted: false)
                const headsetLine = data.headset_listen_line;
                if (headsetLine != null && headsetLine >= 1 && headsetLine <= 8) {
                    for (let i = 1; i <= 8; i++) {
                        if (lines[i]) lines[i].muted = (i !== headsetLine);
                    }
                }
                renderLines();
                updateIdleLineAvailabilityBadges();
                console.log('Line states refreshed from backend');
            } catch (error) {
                console.error('Failed to refresh line states:', error);
            }
        }
        
        // Check SIP connection status
        // Backend should implement: GET /api/sip/status
        // Expected response: { "connected": true/false }
        
        // ===============================
        // API Fetch Helper (fixes file:// CORS issues in browsers / PyQt5 WebEngine)
        // If loaded via file://, relative apiFetch('/api/...') becomes file:///api/... and is blocked.
        // Set window.API_BASE from PyQt5 if needed (e.g., 'http://127.0.0.1:8080').
        // ===============================
        const API_BASE = (typeof window !== 'undefined' && window.API_BASE)
            ? String(window.API_BASE).replace(/\/+$/, '')
            : (location.protocol === 'file:' ? 'http://127.0.0.1:5000' : '');

        function apiUrl(path) {
            if (!path) return path;
            if (/^https?:\/\//i.test(path)) return path;
            const p = path.startsWith('/') ? path : '/' + path;
            return API_BASE + p;
        }

        async function apiFetch(path, options = {}) {
            const url = apiUrl(path);
            return fetch(url, {
                // for same-host APIs, cookies work when served over http(s)
                credentials: 'same-origin',
                ...options
            });
        }



        // ===============================
        // SIP Status UI + Realtime Updates (WebSocket primary, HTTP polling fallback)
        // ===============================
        const SIP_TOTAL_DEFAULT = 8;
        let sipState = {
            server: '—',
            registered: 0,
            total: SIP_TOTAL_DEFAULT,
            connected: false,
            mode: 'CONNECTING' // LIVE | POLLING | CONNECTING
        };

        // Registered line IDs (for per-line Availability UI)
        let sipRegisteredLineIds = new Set();

        
        function normalizeSipCallState(v) {
            const s = String(v ?? '').toLowerCase().trim();
            if (!s) return '';
            if (s === 'active') return 'connected';
            if (s === 'connected' || s === 'in_call' || s === 'incall' || s === 'talking') return 'connected';
            if (s === 'dial' || s === 'dialing' || s === 'outgoing') return 'dialing';
            if (s === 'ring' || s === 'ringing' || s === 'alerting') return 'ringing';
            if (s === 'idle' || s === 'available' || s === 'registered' || s === 'ready') return 'idle';
            return s; // fallback
        }

        function pickRemoteNumber(obj) {
            if (!obj) return '';
            return obj.remoteNumber ?? obj.remote ?? obj.callee ?? obj.to ?? obj.dest ?? obj.destination ?? obj.dialedNumber ?? obj.callNumber ?? obj.numberCalled ?? obj.target ?? '';
        }

        function pickCallStart(obj) {
            if (!obj) return 0;
            const v = obj.callStart ?? obj.callStartTs ?? obj.call_start ?? obj.connectedSince ?? obj.connected_since ?? obj.startTs ?? obj.start_ts ?? obj.startedAt ?? obj.started_at ?? obj.start ?? 0;
            const n = Number(v);
            if (!n || Number.isNaN(n)) return 0;
            // if seconds, convert to ms
            return n > 1e12 ? n : n * 1000;
        }

        // Returns map: lineId -> { state, remoteNumber, callStartMs }
        function extractSipLineCallUpdates(rawObj) {
            const map = {};
            try {
                // If it's a per-line event
                if (rawObj && typeof rawObj === 'object' && !Array.isArray(rawObj)) {
                    const singleId = rawObj.lineId ?? rawObj.line ?? rawObj.id ?? rawObj.line_id ?? null;
                    const singleState = rawObj.callState ?? rawObj.call_state ?? rawObj.state ?? rawObj.status ?? rawObj.call_status ?? null;
                    if (singleId != null && singleState != null && !Array.isArray(rawObj.lines)) {
                        const id = Number(singleId);
                        if (id && !Number.isNaN(id)) {
                            map[id] = {
                                state: normalizeSipCallState(singleState),
                                remoteNumber: String(pickRemoteNumber(rawObj) ?? ''),
                                callStartMs: pickCallStart(rawObj)
                            };
                            return map;
                        }
                    }
                }

                // Common: rawObj.lines array
                const arr = rawObj?.lines;
                if (Array.isArray(arr)) {
                    arr.forEach((l, idx) => {
                        if (!l) return;
                        const id = Number(l.id ?? l.lineId ?? l.line ?? l.line_id ?? (idx + 1));
                        if (!id || Number.isNaN(id)) return;
                        const rawState = (l.callState ?? l.call_state ?? l.state ?? l.status ?? l.call_status);
                        // If there is no call state field, this is likely a registration-only payload.
                        // Skip it to avoid forcing calls back to idle.
                        if (rawState === undefined || rawState === null || String(rawState).trim() === '') return;
                        const state = normalizeSipCallState(rawState);
                        const remoteNumber = String(pickRemoteNumber(l) ?? '');
                        const callStartMs = pickCallStart(l);
                        map[id] = { state, remoteNumber, callStartMs };
                    });
                }

                // Some backends send lineStates: { "1": {"state":"ringing","remote":"+1.."} }
                const dict = rawObj?.lineStates ?? rawObj?.line_states ?? rawObj?.lineState ?? rawObj?.line_state ?? null;
                if (dict && typeof dict === 'object' && !Array.isArray(dict)) {
                    Object.entries(dict).forEach(([k, v]) => {
                        const id = Number(k);
                        if (!id || Number.isNaN(id)) return;
                        const rawState = (v?.callState ?? v?.call_state ?? v?.state ?? v?.status ?? v?.call_status);
                        if (rawState === undefined || rawState === null || String(rawState).trim() === '') return;
                        const state = normalizeSipCallState(rawState);
                        const remoteNumber = String(pickRemoteNumber(v) ?? '');
                        const callStartMs = pickCallStart(v);
                        map[id] = { state, remoteNumber, callStartMs };
                    });
                }
            } catch (e) {
                console.warn('Failed to extract SIP line call updates', e);
            }
            return map;
        }

        function applySipLineCallUpdates(rawObj) {
            const updates = extractSipLineCallUpdates(rawObj);
            const ids = Object.keys(updates);
            if (!ids.length) return;

            ids.forEach((idStr) => {
                const lineId = Number(idStr);
                const upd = updates[lineId];
                if (!lines[lineId] || !upd) return;

                // If the update doesn't include an explicit call state, ignore it.
                // This prevents sip_status payloads (which include lines[] with only
                // registration info) from incorrectly resetting active calls to idle.
                if (!upd.state) return;

                const nextState = upd.state;
                const prevState = lines[lineId].state;

                // remote number + name
                const nextRemote = (upd.remoteNumber || '').toString().trim();
                const nextName = nextRemote ? findDirectoryNameByNumber(nextRemote) : '';

                let stateChanged = (nextState !== prevState);
                let remoteChanged = (nextRemote !== (lines[lineId].remoteNumber || '')) || (nextName !== (lines[lineId].remoteName || ''));

                // Call start: if backend provides, use it; otherwise set when transitioning into connected
                if (nextState === 'connected') {
                    const provided = upd.callStartMs || 0;
                    if (provided) {
                        if (lines[lineId].callStart !== provided) {
                            lines[lineId].callStart = provided;
                            remoteChanged = true;
                        }
                    } else if (prevState !== 'connected') {
                        lines[lineId].callStart = Date.now();
                        remoteChanged = true;
                    }
                } else {
                    // leaving connected => clear callStart
                    if (prevState === 'connected' && lines[lineId].callStart) {
                        lines[lineId].callStart = 0;
                        remoteChanged = true;
                    }
                }

                // Apply fields
                lines[lineId].remoteNumber = nextRemote;
                lines[lineId].remoteName = nextName;

                // Apply state (only if it is one of our known display states)
                if (['idle','dialing','ringing','connected'].includes(nextState)) {
                    lines[lineId].state = nextState;
                } else if (nextState === 'active') {
                    lines[lineId].state = 'connected';
                }

                if (stateChanged || remoteChanged) renderLine(lineId);
            });
        }

        function extractSipRegisteredLineIds(payload) {
            let obj = payload;
            try { if (typeof obj === 'string') obj = JSON.parse(obj); } catch (_) {}
            const set = new Set();
            if (!obj || typeof obj !== 'object') return set;

            // Optional direct lists
            const direct = obj.registeredLines ?? obj.registered_lines ?? obj.registeredLineIds ?? obj.registered_line_ids ?? obj.lines_registered_ids;
            if (Array.isArray(direct)) {
                direct.forEach(v => { if (v !== null && v !== undefined) set.add(String(v)); });
                return set;
            }

            // lines can be array or object
            const linesData = obj.lines ?? obj.lineStatus ?? obj.line_status;
            if (Array.isArray(linesData)) {
                linesData.forEach((l, idx) => {
                    if (!l) return;
                    const reg = (
                        l.registered === true || l.registered === 1 || l.registered === 'true' ||
                        l.status === 'registered' || l.state === 'registered' || l.ok === true || l.online === true
                    );
                    if (!reg) return;
                    const id = l.id ?? l.lineId ?? l.line_id ?? l.line ?? l.index ?? (idx + 1);
                    if (id !== null && id !== undefined) set.add(String(id));
                });
                return set;
            }
            if (linesData && typeof linesData === 'object') {
                Object.entries(linesData).forEach(([k, v]) => {
                    if (!v) return;
                    const reg = (
                        v.registered === true || v.registered === 1 || v.registered === 'true' ||
                        v.status === 'registered' || v.state === 'registered' || v.ok === true || v.online === true
                    );
                    if (reg) set.add(String(k));
                });
            }
            return set;
        }

        function isLineSipRegistered(lineId) {
            return sipRegisteredLineIds.has(String(lineId));
        }

        function updateIdleLineAvailabilityBadges() {
            try {
                if (!window.lines || typeof window.lines !== 'object') return;
                Object.keys(window.lines).forEach((id) => {
                    const line = window.lines[id];
                    if (!line || line.state !== 'idle') return;

                    const badge = document.querySelector(`#line-${id} .line-status`);
                    if (!badge) return;

                    // Show "Available" when system is online (SIP server reachable), "Idle" when offline
                    if (sipConnected) {
                        if (!badge.classList.contains('status-available') || badge.textContent.trim().toLowerCase() !== 'available') {
                            badge.classList.remove('status-idle');
                            badge.classList.add('status-available');
                            badge.textContent = 'Available';
                        }
                    } else {
                        if (!badge.classList.contains('status-idle') || badge.textContent.trim().toLowerCase() !== 'idle') {
                            badge.classList.remove('status-available');
                            badge.classList.add('status-idle');
                            badge.textContent = 'Idle';
                        }
                    }
                });
            } catch (_) {}
        }


        // ===============================
        // SIP Status: Socket.IO Connection (replaces raw WebSocket)
        // ===============================
        let socket = null;
        let sipConnected = false;
        let lastSipUpdateTime = Date.now();
        let sipPollTimer = null;

        function connectSipSocketIO() {
            if (socket) {
                socket.close();
            }

            setSipMode('CONNECTING');
            
            // Connect to Socket.IO (use current host for both local and remote access)
            const socketUrl = window.location.origin || 'http://localhost:5000';
            socket = io(socketUrl, {
                path: '/socket.io',
                transports: ['polling', 'websocket'],  // Start with polling (more reliable), upgrade to websocket
                reconnection: true,
                reconnectionDelay: 1000,
                reconnectionAttempts: Infinity,
                timeout: 20000
            });
            window.socket = socket;  // Expose for mobile audio monitor IIFE

            // Connection established
            socket.on('connected', (data) => {
                console.log('Socket.IO connected:', data);
                sipConnected = true;
                lastSipUpdateTime = Date.now();
                setSipMode('LIVE');
                stopSipPolling();
                // Do NOT set System Online here — wait for sip_status with real reachability
                
                // Subscribe to updates
                socket.emit('subscribe', { lines: [1, 2, 3, 4, 5, 6, 7, 8] });
            });

            // Connection events
            socket.on('connect', () => {
                console.log('Socket.IO transport connected');
                sipConnected = true;
                lastSipUpdateTime = Date.now();
                setSipMode('LIVE');
                stopSipPolling();
                // Do NOT set System Online here — sip_status (emitted every 2s) will
                // reflect actual internet/SIP reachability and update the indicator
                
                // After reconnection, check for active calls and refresh them
                setTimeout(() => {
                    refreshActiveCalls();
                }, 1000);
            });

            socket.on('disconnect', () => {
                console.log('Socket.IO disconnected');
                sipConnected = false;
                setSipMode('POLLING');
                startSipPolling();
                updateSystemStatus(false);
            });

            socket.on('connect_error', (error) => {
                console.error('Socket.IO connection error:', error);
                sipConnected = false;
                setSipMode('POLLING');
                startSipPolling();
                updateSystemStatus(false);
            });

            // Listen for SIP status updates (real-time)
            socket.on('sip_status', (data) => {
                console.log('SIP status update:', data);
                lastSipUpdateTime = Date.now();
                updateSipUI(data);
                updateSystemStatus(data.connected || false);
            });

            // Listen for line status updates
            socket.on('line_status', (data) => {
                console.log('Line status update:', data);
                lastSipUpdateTime = Date.now();
                handleLineStatusUpdate(data);
            });

            // Listen for audio channel changes
            socket.on('audio_channel_change', (data) => {
                console.log('Audio channel change:', data);
                lastSipUpdateTime = Date.now();
                handleAudioChannelChange(data);
            });

            // Listen for audio level meter updates
            socket.on('audio_levels', (data) => {
                handleAudioLevels(data);
            });

            // Sync phone directory across all connected clients (web + touchscreen)
            socket.on('directory_updated', () => {
                loadDirectory();
            });

            // Listen for headset listen (which line is Listen & Talk)
            // Note: headset_listen event listener removed - each client maintains independent headset state
            // If you need synchronized headset state across clients, uncomment this:
            // socket.on('headset_listen', (data) => {
            //     const lineId = data?.line_id;
            //     if (lineId == null) return;
            //     for (let i = 1; i <= 8; i++) {
            //         if (lines[i]) lines[i].muted = (i !== lineId);
            //     }
            //     renderLines();
            // });
        }

        // Handle line status update from Socket.IO
        function handleLineStatusUpdate(data) {
            const lineId = data.line_id || data.lineId;
            if (!lines[lineId]) return;
            
            // Map backend field names to UI field names
            let state = data.state || data.callState || 'idle';
            const currentState = lines[lineId].state;
            // Ignore stale error only when we're in an active call (the error is from a previous state)
            if (state === 'error' && (
                currentState === 'dialing' || currentState === 'ringing' || currentState === 'connected'
            )) {
                return;
            }
            const remoteNumber = data.phone_number || data.remoteNumber || '';
            const remoteName = data.caller_id || data.remoteName || '';
            const duration = data.duration;
            
            // Handle audio channel if provided
            if (data.audio_channel !== undefined) {
                const oldCh = lines[lineId].audioChannel;
                if (oldCh > 0 && audioChannels[oldCh] === lineId) {
                    delete audioChannels[oldCh];
                }
                lines[lineId].audioChannel = data.audio_channel;
                if (data.audio_channel > 0) {
                    audioChannels[data.audio_channel] = lineId;
                }
            }
            
            // Update line state (always trust backend state, except stale error already ignored above)
            lines[lineId].state = state;
            
            // Update numbers based on state
            if (state === 'dialing' || state === 'ringing' || state === 'connected') {
                // During active call states, show the remote number.
                // BUT: if the frontend already returned to idle (user hung up and may be typing
                // a new number), ignore this stale event — don't overwrite the new number.
                if (currentState !== 'idle') {
                    lines[lineId].number = remoteNumber;
                    lines[lineId].remoteNumber = remoteNumber;
                }
            } else if (state === 'idle') {
                // When returning to idle, only clear if we were in an active call
                if (currentState === 'dialing' || currentState === 'ringing' || currentState === 'connected') {
                    lines[lineId].number = '';
                    lines[lineId].remoteNumber = '';
                    lines[lineId].remoteName = '';
                    lines[lineId].contactName = '';
                    lines[lineId].callStart = null;
                }
                // If already idle, don't touch anything (preserves typed number)
            }
            
            if (remoteName) {
                lines[lineId].contactName = remoteName;
                lines[lineId].remoteName = remoteName;
            }
            
            // Handle call timing
            if (duration !== undefined) {
                // Backend sends duration in seconds - convert to callStart timestamp
                lines[lineId].callStart = Date.now() - (duration * 1000);
            } else if (data.callStartTs || data.callStart || data.connectedSince) {
                // Backend sends start timestamp
                const ts = data.callStartTs || data.callStart || data.connectedSince;
                // Handle both seconds and milliseconds
                lines[lineId].callStart = ts > 1e10 ? ts : ts * 1000;
            } else if (state === 'connected' && !lines[lineId].callStart) {
                // No timing provided, use current time
                lines[lineId].callStart = Date.now();
            }
            
            renderLine(lineId);
            
            // Try to apply SIP call updates if the function exists
            if (typeof applySipLineCallUpdates === 'function') {
                applySipLineCallUpdates({ lines: [data] });
            }
        }

        // Handle audio channel change from Socket.IO
        function handleAudioChannelChange(data) {
            const lineId = data.line_id;
            const channel = data.channel;
            if (lines[lineId]) {
                // Remove old channel from audioChannels map
                const oldCh = lines[lineId].audioChannel;
                if (oldCh > 0 && audioChannels[oldCh] === lineId) {
                    delete audioChannels[oldCh];
                }
                // Assign new channel
                lines[lineId].audioChannel = channel;
                if (channel > 0) {
                    audioChannels[channel] = lineId;
                }
                renderLines();  // Re-render ALL lines so other dropdowns update too
            }
        }

        // ── Audio level meters ──────────────────────────────────────────
        // Convert dBFS (-60..0) to fill fraction (0..1) with log scaling
        function dbToFraction(db) {
            const clamped = Math.max(-60, Math.min(0, db));
            return (clamped + 60) / 60;  // linear mapping: -60→0, 0→1
        }

        // Convert dBFS to CSS color: green safe, yellow loud, red clipping
        function dbToColor(db, isActive) {
            if (!isActive) return '#4a5568';  // grey when idle
            if (db > -6)  return '#ef4444';   // red — clipping
            if (db > -12) return '#f59e0b';   // yellow — loud
            return '#22c55e';                  // green — safe
        }

        function handleAudioLevels(data) {
            const ch = data.channel;
            const inDb  = data.in_db;
            const outDb = data.out_db;

            // Find which line is using this channel
            const lineId = audioChannels[ch];
            if (!lineId) return;

            const line = lines[lineId];
            if (!line) return;

            const isActive = (line.state === 'connected' || line.state === 'dialing' || line.state === 'ringing');

            // Update IN meter
            const inBar  = document.getElementById(`vu-in-bar-${lineId}`);
            const inVal  = document.getElementById(`vu-in-db-${lineId}`);
            if (inBar) {
                const frac = dbToFraction(inDb);
                inBar.style.height = (frac * 100) + '%';
                inBar.style.background = dbToColor(inDb, isActive);
            }
            if (inVal) inVal.textContent = inDb.toFixed(1) + ' dB';

            // Update OUT meter
            const outBar = document.getElementById(`vu-out-bar-${lineId}`);
            const outVal = document.getElementById(`vu-out-db-${lineId}`);
            if (outBar) {
                const frac = dbToFraction(outDb);
                outBar.style.height = (frac * 100) + '%';
                outBar.style.background = dbToColor(outDb, isActive);
            }
            if (outVal) outVal.textContent = outDb.toFixed(1) + ' dB';
        }

        // Start/Stop polling
        function stopSipPolling() {
            if (sipPollTimer) {
                clearInterval(sipPollTimer);
                sipPollTimer = null;
            }
        }

        function startSipPolling() {
            stopSipPolling();
            setSipMode('POLLING');
            checkSIPStatus();
            sipPollTimer = setInterval(checkSIPStatus, 2000);
        }

        // Staleness check: fallback to polling if no updates for >8s
        setInterval(() => {
            const timeSinceUpdate = Date.now() - lastSipUpdateTime;
            if (sipConnected && timeSinceUpdate > 8000) {
                console.log('Socket.IO stale (>8s), switching to polling');
                startSipPolling();
            }
        }, 2000);

        function initSipStatusRealtime() {
            connectSipSocketIO();
        }

        // Helper functions
        function normalizeSipPayload(data) {
            let obj = data;
            try {
                if (typeof obj === 'string') obj = JSON.parse(obj);
            } catch (_) {
                obj = {};
            }
            if (!obj || typeof obj !== 'object') obj = {};

            const total = Number(
                obj.totalLines ?? obj.total_lines ?? obj.total ?? obj.linesTotal ?? obj.total_lines_count ?? SIP_TOTAL_DEFAULT
            ) || SIP_TOTAL_DEFAULT;

            let registered = obj.registeredCount ?? obj.registered_count ?? obj.lines_registered ?? obj.registered ?? obj.linesRegistered;
            if (registered === undefined && Array.isArray(obj.lines)) {
                registered = obj.lines.filter(l => l && (l.registered === true || l.status === 'registered' || l.state === 'registered' || l.ok === true)).length;
            }
            registered = Number(registered ?? 0) || 0;

            const server = (obj.server ?? obj.host ?? obj.sipServer ?? obj.domain ?? '—');
            const connected = Boolean(obj.connected ?? obj.online ?? obj.sipConnected ?? false);

            return { server: String(server), registered, total, connected };
        }

        function setSipMode(mode) {
            // Only track mode for internal logic; do NOT write to #sip-mode here —
            // updateSystemStatus() is the single owner of that element's text.
            sipState.mode = mode;
        }

        function updateSipUI(payload) {
            let rawObj = payload;
            try { if (typeof rawObj === 'string') rawObj = JSON.parse(rawObj); } catch (_) {}
            const p = normalizeSipPayload(rawObj);
            sipState = { ...sipState, ...p };

            // Update registered line IDs (used to show Available vs Idle on each line)
            sipRegisteredLineIds = extractSipRegisteredLineIds(rawObj);
            updateIdleLineAvailabilityBadges();
            // Update per-line call state ONLY when the payload actually contains call state fields.
            // Our backend's sip_status payload includes lines[] entries with only {line_id, registered},
            // and treating those as call updates would reset active calls back to idle.
            try {
                const maybeLines = rawObj?.lines;
                const hasCallState = Array.isArray(maybeLines) && maybeLines.some((l) => {
                    if (!l) return false;
                    const v = (l.callState ?? l.call_state ?? l.state ?? l.status ?? l.call_status);
                    return v !== undefined && v !== null && String(v).trim() !== '';
                });
                if (hasCallState) applySipLineCallUpdates(rawObj);
            } catch (_) {}

            const serverEl = document.getElementById('sip-server');
            const linesEl = document.getElementById('sip-lines');
            const pillEl = document.getElementById('sip-pill-text');
            const dotEl = document.getElementById('sip-dot');
            const sipModeEl = document.getElementById('sip-mode');

            if (serverEl) serverEl.textContent = sipState.server || '—';

            // In outgoing-only mode REGISTER is never sent — hide all registration UI
            const isOutgoingOnly = !!(rawObj && rawObj.outgoing_only);

            if (isOutgoingOnly) {
                // Hide registration fraction, pill and dot — irrelevant in outgoing-only mode
                if (linesEl) linesEl.closest('.sip-row') && (linesEl.closest('.sip-row').style.display = 'none');
                const pillParent = pillEl && pillEl.closest('.status-indicator');
                if (pillParent) pillParent.style.display = 'none';
                if (dotEl) dotEl.closest('.status-indicator') && (dotEl.closest('.status-indicator').style.display = 'none');
                if (sipModeEl) sipModeEl.textContent = 'Outbound-Only';
                return;
            }

            const frac = `${sipState.registered} / ${sipState.total}`;

            if (linesEl) {
                linesEl.textContent = frac;
                linesEl.classList.remove('sip-strong', 'sip-warn', 'sip-bad');
                if (!sipState.connected) linesEl.classList.add('sip-bad');
                else if (sipState.registered >= sipState.total) linesEl.classList.add('sip-strong');
                else if (sipState.registered > 0) linesEl.classList.add('sip-warn');
                else linesEl.classList.add('sip-bad');
            }

            if (pillEl) pillEl.textContent = `SIP ${sipState.registered}/${sipState.total}`;

            if (dotEl) {
                dotEl.classList.remove('offline', 'warn', 'bad');
                if (!sipState.connected) dotEl.classList.add('offline');
                else if (sipState.registered >= sipState.total) {
                    // keep default green
                } else if (sipState.registered > 0) {
                    dotEl.classList.add('warn');
                } else {
                    dotEl.classList.add('bad');
                }
            }
        }


        async function checkSIPStatus() {
            try {
                const response = await apiFetch('/api/sip/status');
                const data = await response.json();
                updateSystemStatus(data.connected || false);
                updateSipUI(data);
            } catch (error) {
                console.error('Failed to check SIP status:', error);
                updateSystemStatus(false);
            }
        }


        // ===============================
        // Custom Modal Alert (no window.alert)
        // ===============================
        function showAlertModal(messageOrOptions) {
            const overlay = document.getElementById('alert-modal');
            const titleEl = document.getElementById('alert-title');
            const msgEl = document.getElementById('alert-message');
            const okBtn = document.getElementById('alert-ok-btn');

            if (!overlay || !titleEl || !msgEl || !okBtn) {
                // Ultimate fallback
                console.warn('Alert modal not found:', messageOrOptions);
                return;
            }

            const opts = (typeof messageOrOptions === 'object' && messageOrOptions !== null)
                ? messageOrOptions
                : { message: String(messageOrOptions ?? '') };

            const title = opts.title ?? 'Notice';
            const message = opts.message ?? '';

            titleEl.textContent = title;
            msgEl.innerHTML = message;  // Use innerHTML to render HTML tags like <br> and <strong>

            overlay.classList.add('show');
            overlay.setAttribute('aria-hidden', 'false');

            const cleanup = () => {
                overlay.classList.remove('show');
                overlay.setAttribute('aria-hidden', 'true');
                okBtn.removeEventListener('click', onOk);
                overlay.removeEventListener('click', onOverlay);
                document.removeEventListener('keydown', onKeydown);
            };

            const onOk = () => cleanup();
            const onOverlay = (e) => { if (e.target === overlay) cleanup(); };
            const onKeydown = (e) => { if (e.key === 'Escape' || e.key === 'Enter') cleanup(); };

            okBtn.addEventListener('click', onOk);
            overlay.addEventListener('click', onOverlay);
            document.addEventListener('keydown', onKeydown);

            setTimeout(() => okBtn.focus(), 0);
        }
        
        // Initialize
        
        // ===============================
        // Phone Directory (IFB / PL) - groups + branches, editable names
        // ===============================
        const DIRECTORY_STORAGE_KEY = 'procomm_phone_directory_v1';

        let directoryTab = 'IFB';    // fixed categories: IFB, PL
        let pickerTab = 'IFB';
        let _pickerLineId = null;

        // editor context
        let _editorContext = null; // { type: 'group'|'branch'|'addGroup'|'addBranch', category, groupId, branchId }

        function defaultDirectoryData() {
            return { IFB: [], PL: [], prefixes: [], defaultPrefixId: null };
        }

        function cryptoRandomId() {
            // Works in modern browsers / QWebEngine; fallback to Date if needed.
            try {
                return (crypto && crypto.randomUUID) ? crypto.randomUUID() : ('id-' + Date.now() + '-' + Math.random().toString(16).slice(2));
            } catch (_) {
                return ('id-' + Date.now() + '-' + Math.random().toString(16).slice(2));
            }
        }


        // Fast lookup: phone number -> contact name (built from Phone Directory)
        let directoryNumberIndex = new Map();

        function normalizePhoneDigits(v) {
            if (v === null || v === undefined) return '';
            const s = String(v).trim();
            // keep digits only for matching
            const digits = s.replace(/\D+/g, '');
            return digits;
        }

        function buildDirectoryNumberIndex() {
            try {
                directoryNumberIndex = new Map();
                if (!phoneDirectory) return;

                const addFromCategory = (cat) => {
                    const groups = Array.isArray(phoneDirectory[cat]) ? phoneDirectory[cat] : [];
                    groups.forEach(g => {
                        (g.branches || []).forEach(b => {
                            const num = b?.number ?? '';
                            const name = (b?.name ?? '').toString().trim();
                            const key = normalizePhoneDigits(num);
                            if (!key) return;
                            // Prefer first non-empty name; keep earliest entry if duplicates
                            if (!directoryNumberIndex.has(key) && name) directoryNumberIndex.set(key, name);
                        });
                    });
                };

                addFromCategory('IFB');
                addFromCategory('PL');
            } catch (e) {
                console.warn('Failed to build directory index', e);
            }
            refreshLineRemoteNamesFromDirectory();
        }


        function refreshLineRemoteNamesFromDirectory() {
            try {
                if (!window.lines) return;
                for (let i = 1; i <= 8; i++) {
                    const line = window.lines[i];
                    if (!line) continue;
                    if (!line.remoteNumber) continue;
                    const newName = findDirectoryNameByNumber(line.remoteNumber);
                    if ((line.remoteName || '') !== (newName || '')) {
                        line.remoteName = newName || '';
                        // only rerender if call info is visible for this line
                        if (['dialing','ringing','connected'].includes(line.state)) renderLine(i);
                    }
                }
            } catch (_) {}
        }


        function findDirectoryNameByNumber(number) {
            const n = normalizePhoneDigits(number);
            if (!n) return '';
            // exact match
            if (directoryNumberIndex.has(n)) return directoryNumberIndex.get(n);

            // fallback: match by last 7-10 digits (helps when SIP includes country code)
            let best = '';
            let bestLen = 0;
            for (const [k, v] of directoryNumberIndex.entries()) {
                if (!k || k.length < 7 || n.length < 7) continue;
                // Compare actual trailing digit overlap
                const maxSuffix = Math.min(10, k.length, n.length);
                for (let suffixLen = maxSuffix; suffixLen >= 7; suffixLen--) {
                    if (k.slice(-suffixLen) === n.slice(-suffixLen)) {
                        if (suffixLen > bestLen) { bestLen = suffixLen; best = v; }
                        break; // longest match for this entry found
                    }
                }
            }
            return best || '';
        }

        let phoneDirectory = defaultDirectoryData();

        async function loadDirectory() {
            try {
                const response = await fetch('/api/directory');
                if (response.ok) {
                    const data = await response.json();
                    if (data && typeof data === 'object') {
                        if (Array.isArray(data.IFB)) phoneDirectory.IFB = data.IFB;
                        if (Array.isArray(data.PL)) phoneDirectory.PL = data.PL;
                        if (Array.isArray(data.prefixes)) phoneDirectory.prefixes = data.prefixes;
                        if (typeof data.defaultPrefixId === 'string' || data.defaultPrefixId === null) {
                            phoneDirectory.defaultPrefixId = data.defaultPrefixId;
                        }
                        try { localStorage.setItem(DIRECTORY_STORAGE_KEY, JSON.stringify(phoneDirectory)); } catch (_) {}
                    }
                }
            } catch (e) {
                try {
                    const raw = localStorage.getItem(DIRECTORY_STORAGE_KEY);
                    if (raw) {
                        const parsed = JSON.parse(raw);
                        if (parsed && typeof parsed === 'object') {
                            if (Array.isArray(parsed.IFB)) phoneDirectory.IFB = parsed.IFB;
                            if (Array.isArray(parsed.PL)) phoneDirectory.PL = parsed.PL;
                            if (Array.isArray(parsed.prefixes)) phoneDirectory.prefixes = parsed.prefixes;
                            if (typeof parsed.defaultPrefixId === 'string' || parsed.defaultPrefixId === null) {
                                phoneDirectory.defaultPrefixId = parsed.defaultPrefixId;
                            }
                        }
                    }
                } catch (e2) { console.warn('Failed to load directory from localStorage:', e2); }
            }
            buildDirectoryNumberIndex();
            renderPhoneDirectory();
            renderPrefixList();
            renderLines();  // re-render keypads so prefix button appears if defaultPrefixId was loaded
        }

        async function saveDirectory() {
            try {
                localStorage.setItem(DIRECTORY_STORAGE_KEY, JSON.stringify(phoneDirectory));
                try {
                    const response = await fetch('/api/directory', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(phoneDirectory)
                    });
                    if (!response.ok) console.warn('Server save directory failed:', response.status);
                } catch (e) { console.warn('Failed to save directory to server:', e); }
                buildDirectoryNumberIndex();
                window.dispatchEvent(new Event('directory-updated'));
            } catch (e) {
                console.warn('Failed to save directory:', e);
            }
        }

        function setDirectoryTab(tab) {
            if (tab !== 'IFB' && tab !== 'PL') return;
            directoryTab = tab;
            const ifb = document.getElementById('dir-tab-ifb');
            const pl = document.getElementById('dir-tab-pl');
            ifb?.classList.toggle('active', tab === 'IFB');
            pl?.classList.toggle('active', tab === 'PL');
            ifb?.setAttribute('aria-selected', tab === 'IFB' ? 'true' : 'false');
            pl?.setAttribute('aria-selected', tab === 'PL' ? 'true' : 'false');
            renderPhoneDirectory();
        }

        function renderPhoneDirectory() {
            const container = document.getElementById('dir-groups');
            if (!container) return;

            const q = (document.getElementById('dir-search')?.value || '').trim().toLowerCase();
            const groups = phoneDirectory[directoryTab] || [];

            const groupHtml = groups.map(g => {
                const groupName = escapeHTML(g.name);
                const branches = Array.isArray(g.branches) ? g.branches : [];
                const branchCount = branches.length;

                // Filter logic
                const matchesGroup = !q || g.name.toLowerCase().includes(q);
                const matchingBranches = !q ? branches : branches.filter(b =>
                    (b.name || '').toLowerCase().includes(q) ||
                    (b.number || '').toLowerCase().includes(q)
                );

                const showGroup = matchesGroup || matchingBranches.length > 0;
                if (!showGroup) return '';

                const metaText = `${branchCount} contact${branchCount === 1 ? '' : 's'}`;
                const branchesHtml = matchingBranches.map(b => {
                    const bName = escapeHTML(b.name);
                    const bNumber = escapeHTML(b.number);
                    return `
                        <div class="dir-branch">
                            <div class="dir-branch-main" onclick="openPhonePickerForLine(null, '${directoryTab}', '${g.id}', '${b.id}')" style="cursor:pointer;">
                                <div class="dir-branch-name">${bName}</div>
                                <div class="dir-branch-number">${bNumber}</div>
                            </div>
                            <div class="dir-branch-actions">
<button class="dir-icon-btn" title="Rename" onclick="(typeof event!=='undefined' && event.stopPropagation()); openRenameBranch('${directoryTab}', '${g.id}', '${b.id}')">✎</button>
                                <button class="dir-icon-btn danger" title="Delete" onclick="(typeof event!=='undefined' && event.stopPropagation()); deleteBranch('${directoryTab}', '${g.id}', '${b.id}')">🗑</button>
                            </div>
                        </div>
                    `;
                }).join('');

                return `
                    <div class="dir-group" id="dir-group-${g.id}">
                        <div class="dir-group-header" onclick="toggleGroupOpen('${g.id}')">
                            <div class="dir-group-title">
                                <div class="dir-group-name">${groupName}</div>
                                <div class="dir-group-meta">${metaText}</div>
                            </div>
                            <div class="dir-chip" onclick="(typeof event!=='undefined' && event.stopPropagation());">${escapeHTML(directoryTab)}</div>
                            <button class="dir-icon-btn" title="Rename group" onclick="(typeof event!=='undefined' && event.stopPropagation()); openRenameGroup('${directoryTab}', '${g.id}')">✎</button>
                            <button class="dir-icon-btn danger" title="Delete group" onclick="(typeof event!=='undefined' && event.stopPropagation()); deleteGroup('${directoryTab}', '${g.id}')">🗑</button>
                            <button class="dir-icon-btn" title="Add contact" onclick="(typeof event!=='undefined' && event.stopPropagation()); openAddBranch('${directoryTab}', '${g.id}')">＋</button>
                            <div class="dir-caret">▾</div>
                        </div>
                        <div class="dir-group-body">
                            <div class="dir-branches">
                                ${branchesHtml || '<div class="section-subtitle" style="margin: 10px 2px 0 2px;">No contacts yet. Tap ＋ to add one.</div>'}
                            </div>
                        </div>
                    </div>
                `;
            }).join('');

            container.innerHTML = groupHtml || `<div class="section-subtitle" style="margin: 8px 2px;">No groups yet. Tap <b>+ Group</b> to create one.</div>`;
        }

        function toggleGroupOpen(groupId) {
            const el = document.getElementById(`dir-group-${groupId}`);
            if (!el) return;
            el.classList.toggle('open');
        }

        function openAddGroup() {
            _editorContext = { type: 'addGroup', category: directoryTab };
            openDirEditor({
                title: `Add Group (${directoryTab})`,
                subtitle: 'Create a new group name. You can rename it later.',
                fields: [
                    { id: 'edit-group-name', label: 'Group Name', value: '', keyboard: true }
                ]
            });
        }

        function openRenameGroup(category, groupId) {
            const group = (phoneDirectory[category] || []).find(g => g.id === groupId);
            if (!group) return;
            _editorContext = { type: 'group', category, groupId };
            openDirEditor({
                title: `Rename Group (${category})`,
                subtitle: 'Enter a new group name.',
                fields: [
                    { id: 'edit-group-name', label: 'Group Name', value: group.name || '', keyboard: true }
                ]
            });
        }

        function openAddBranch(category, groupId) {
            const group = (phoneDirectory[category] || []).find(g => g.id === groupId);
            if (!group) return;
            _editorContext = { type: 'addBranch', category, groupId };
            openDirEditor({
                title: `Add Contact (${category})`,
                subtitle: `Add a contact under "${group.name || 'Group'}".`,
                fields: [
                    { id: 'edit-branch-name', label: 'Contact Name', value: '', keyboard: true },
                    { id: 'edit-branch-number', label: 'Phone Number', value: '', keyboard: true, numeric: true }
                ]
            });
        }

        function openRenameBranch(category, groupId, branchId) {
            const group = (phoneDirectory[category] || []).find(g => g.id === groupId);
            if (!group) return;
            const branch = (group.branches || []).find(b => b.id === branchId);
            if (!branch) return;
            _editorContext = { type: 'branch', category, groupId, branchId };
            openDirEditor({
                title: `Edit Contact (${category})`,
                subtitle: `Update name and number.`,
                fields: [
                    { id: 'edit-branch-name', label: 'Contact Name', value: branch.name || '', keyboard: true },
                    { id: 'edit-branch-number', label: 'Phone Number', value: branch.number || '', keyboard: true, numeric: true }
                ]
            });
        }

        
        async function deleteGroup(category, groupId) {
            const groups = phoneDirectory[category] || [];
            const group = groups.find(g => g.id === groupId);
            if (!group) return;

            const branchCount = (group.branches || []).length;
            const confirmed = await showConfirmModal({
                title: 'Delete Group',
                message: `Delete "${group.name || 'Group'}" and its ${branchCount} contact${branchCount === 1 ? '' : 's'}?`,
                okText: 'Delete',
                cancelText: 'Cancel'
            });

            if (!confirmed) return;

            phoneDirectory[category] = groups.filter(g => g.id !== groupId);
            saveDirectory();
            renderPhoneDirectory();
        }


        async function deleteBranch(category, groupId, branchId) {
            const group = (phoneDirectory[category] || []).find(g => g.id === groupId);
            if (!group) return;
            const branch = (group.branches || []).find(b => b.id === branchId);
            if (!branch) return;

            const confirmed = await showConfirmModal({
                title: 'Delete Contact',
                message: `Delete "${branch.name || 'Contact'}" (${branch.number || ''})?`,
                okText: 'Delete',
                cancelText: 'Cancel'
            });

            if (!confirmed) return;

            group.branches = (group.branches || []).filter(b => b.id !== branchId);
            saveDirectory();
            renderPhoneDirectory();
        }

        function openDirEditor({ title, subtitle, fields }) {
            const overlay = document.getElementById('dir-editor-modal');
            const titleEl = document.getElementById('dir-editor-title');
            const subtitleEl = document.getElementById('dir-editor-subtitle');
            const form = document.getElementById('dir-editor-form');

            if (!overlay || !titleEl || !subtitleEl || !form) return;

            titleEl.textContent = title || 'Edit';
            subtitleEl.textContent = subtitle || '';

            const formHtml = (fields || []).map(f => {
                const safeLabel = escapeHTML(f.label || '');
                const value = escapeHTML(f.value || '');
                const inputId = f.id;
                const isNumeric = !!f.numeric;

                // readonly to force virtual keyboard on touch; desktop users can still click and type if you remove readonly
                return `
                    <div class="edit-field">
                        <label>${safeLabel}</label>
                        <input id="${inputId}" type="text" value="${value}" readonly
                               onclick="showVirtualKeyboard('${inputId}')"
                               inputmode="${isNumeric ? 'numeric' : 'text'}"
                               autocomplete="off" />
                    </div>
                `;
            }).join('');

            form.innerHTML = formHtml;

            overlay.classList.add('show');
            overlay.setAttribute('aria-hidden', 'false');

            // Focus first field (keyboard button shows on tap)
            setTimeout(() => {
                const first = form.querySelector('input');
                first && first.focus();
            }, 0);
        }

        function closeDirEditor() {
            const overlay = document.getElementById('dir-editor-modal');
            if (!overlay) return;
            overlay.classList.remove('show');
            overlay.setAttribute('aria-hidden', 'true');
            _editorContext = null;
        }

        function saveDirEditor() {
            if (!_editorContext) return;

            const ctx = _editorContext;
            const cat = ctx.category;

            const getVal = (id) => (document.getElementById(id)?.value || '').trim();

            // Quick Dial Prefixes — handled separately
            if (ctx.type === 'prefix') {
                if (savePrefixFromEditor()) {
                    closeDirEditor();
                }
                return;
            }

            if (ctx.type === 'addGroup' || ctx.type === 'group') {
                const name = getVal('edit-group-name');
                if (!name) {
                    showAlertModal({ title: 'Required', message: 'Please enter a group name.' });
                    return;
                }

                if (ctx.type === 'addGroup') {
                    phoneDirectory[cat] = phoneDirectory[cat] || [];
                    phoneDirectory[cat].push({ id: cryptoRandomId(), name, branches: [] });
                } else {
                    const group = (phoneDirectory[cat] || []).find(g => g.id === ctx.groupId);
                    if (group) group.name = name;
                }

                saveDirectory();
                closeDirEditor();
                renderPhoneDirectory();
                return;
            }

            // add/edit branch
            const bName = getVal('edit-branch-name');
            const bNumber = getVal('edit-branch-number');

            if (!bName || !bNumber) {
                showAlertModal({ title: 'Required', message: 'Please fill in Contact Name and Phone Number.' });
                return;
            }

            const group = (phoneDirectory[cat] || []).find(g => g.id === ctx.groupId);
            if (!group) return;

            group.branches = group.branches || [];

            if (ctx.type === 'addBranch') {
                group.branches.push({ id: cryptoRandomId(), name: bName, number: bNumber });
            } else if (ctx.type === 'branch') {
                const branch = group.branches.find(b => b.id === ctx.branchId);
                if (branch) {
                    branch.name = bName;
                    branch.number = bNumber;
                }
            }

            saveDirectory();
            closeDirEditor();
            renderPhoneDirectory();
        }

        // ===============================
        // Quick Dial Prefixes
        // ===============================

        function renderPrefixList() {
            const container = document.getElementById('prefix-list');
            if (!container) return;
            const prefixes = (phoneDirectory && Array.isArray(phoneDirectory.prefixes)) ? phoneDirectory.prefixes : [];
            const defaultId = phoneDirectory ? phoneDirectory.defaultPrefixId : null;

            if (prefixes.length === 0) {
                container.innerHTML = `<div class="section-subtitle" style="margin: 8px 2px;">No prefixes yet. Tap <b>+ Add Prefix</b> to create one.</div>`;
                return;
            }

            container.innerHTML = prefixes.map(p => `
                <div class="dir-branch" style="display:flex; align-items:center; gap:10px;">
                    <span class="prefix-toggle ${p.id === defaultId ? 'on' : ''}" onclick="setDefaultPrefix('${p.id}')" title="${p.id === defaultId ? 'Remove default' : 'Set as default'}">
                        <span class="prefix-toggle-track"></span>
                        <span class="prefix-toggle-label">Default</span>
                    </span>
                    <div style="flex:1;">
                        <div class="dir-branch-name">${escapeHTML(p.label || 'Unnamed')}</div>
                        <div class="dir-branch-number">${escapeHTML(p.value || '')}</div>
                    </div>
                    <button class="dir-icon-btn" title="Edit prefix" onclick="openEditPrefix('${p.id}')">✎</button>
                    <button class="dir-icon-btn" title="Delete prefix" onclick="deletePrefix('${p.id}')">🗑</button>
                </div>
            `).join('');
        }

        function setDefaultPrefix(prefixId) {
            if (!phoneDirectory) return;
            // Tapping the already-selected radio toggles it off (no default).
            phoneDirectory.defaultPrefixId = (phoneDirectory.defaultPrefixId === prefixId) ? null : prefixId;
            saveDirectory();
            renderPrefixList();
            renderLines();   // refresh keypads to show/hide/update prefix button
        }

        function openAddPrefix() {
            openPrefixEditor(null);
        }

        function openEditPrefix(prefixId) {
            openPrefixEditor(prefixId);
        }

        function deletePrefix(prefixId) {
            if (!phoneDirectory || !Array.isArray(phoneDirectory.prefixes)) return;
            phoneDirectory.prefixes = phoneDirectory.prefixes.filter(p => p.id !== prefixId);
            // If we just deleted the default, clear the default reference.
            if (phoneDirectory.defaultPrefixId === prefixId) {
                phoneDirectory.defaultPrefixId = null;
            }
            saveDirectory();
            renderPrefixList();
            renderLines();
        }

        function openPrefixEditor(prefixId /* null when adding */) {
            const existing = prefixId
                ? (phoneDirectory.prefixes || []).find(p => p.id === prefixId)
                : null;

            const overlay = document.getElementById('dir-editor-modal');
            const titleEl = document.getElementById('dir-editor-title');
            const subtitleEl = document.getElementById('dir-editor-subtitle');
            const form = document.getElementById('dir-editor-form');
            if (!overlay || !titleEl || !subtitleEl || !form) return;

            titleEl.textContent = existing ? 'Edit Prefix' : 'Add Prefix';
            subtitleEl.textContent = 'Label is shown on the keypad button. Prefix value is what gets dropped into the dial field.';

            const labelVal = escapeHTML(existing ? existing.label : '');
            const prefixVal = escapeHTML(existing ? existing.value : '');

            // Same pattern as openDirEditor: always readonly + virtual keyboard on tap.
            // This works correctly on both the touchscreen (localhost) and desktop.
            form.innerHTML = `
                <div class="edit-field">
                    <label>Label</label>
                    <input id="edit-prefix-label" type="text" value="${labelVal}" readonly
                           onclick="showVirtualKeyboard('edit-prefix-label')"
                           inputmode="text" autocomplete="off" />
                </div>
                <div class="edit-field">
                    <label>Prefix value</label>
                    <input id="edit-prefix-value" type="text" value="${prefixVal}" readonly
                           onclick="showVirtualKeyboard('edit-prefix-value')"
                           inputmode="numeric" autocomplete="off" />
                </div>
            `;

            // Stash the editor context so the Save button (saveDirEditor) knows what to do.
            _editorContext = { type: 'prefix', prefixId: prefixId };

            overlay.classList.add('show');
            overlay.setAttribute('aria-hidden', 'false');

            setTimeout(() => {
                const first = form.querySelector('input');
                if (first) first.focus();
            }, 0);
        }

        function savePrefixFromEditor() {
            const labelEl = document.getElementById('edit-prefix-label');
            const valueEl = document.getElementById('edit-prefix-value');
            if (!labelEl || !valueEl) return false;
            const label = (labelEl.value || '').trim();
            const value = (valueEl.value || '').trim();
            if (!label || !value) {
                showAlertModal({ title: 'Required', message: 'Both label and prefix value are required.' });
                return false;
            }
            if (!Array.isArray(phoneDirectory.prefixes)) phoneDirectory.prefixes = [];

            const ctx = _editorContext || {};
            if (ctx.prefixId) {
                const existing = phoneDirectory.prefixes.find(p => p.id === ctx.prefixId);
                if (existing) {
                    existing.label = label;
                    existing.value = value;
                }
            } else {
                phoneDirectory.prefixes.push({ id: cryptoRandomId(), label: label, value: value });
            }
            saveDirectory();
            renderPrefixList();
            renderLines();
            return true;
        }

        function applyPrefix(lineId) {
            if (!window.lines || !window.lines[lineId]) return;
            if (!phoneDirectory || !phoneDirectory.defaultPrefixId) return;
            const prefix = (phoneDirectory.prefixes || []).find(p => p.id === phoneDirectory.defaultPrefixId);
            if (!prefix) return;
            window.lines[lineId].number = String(prefix.value || '');
            renderLine(lineId);
        }

        // ===============================
        // Phone Picker (insert number into a line)
        // ===============================
        
        function openPhonePickerForLine(lineId, category, groupId, branchId) {
            // When clicking "Use" from settings list, open the picker is unnecessary; just insert into an idle line.
            // If you want to target a specific line, pass lineId from your UI.
            const group = (phoneDirectory[category] || []).find(g => g.id === groupId);
            const branch = (group?.branches || []).find(b => b.id === branchId);
            if (!branch) return;
            _pickerLineId = lineId; // may be null
            pickNumber(branch.number);
        }


        function openPhonePicker() {
            const overlay = document.getElementById('phone-picker-modal');
            if (!overlay) return;

            // default to current directoryTab
            pickerTab = directoryTab || 'IFB';
            setPickerTab(pickerTab);

            document.getElementById('picker-search').value = '';
            renderPickerList();

            overlay.classList.add('show');
            overlay.setAttribute('aria-hidden', 'false');
        }

        function closePhonePicker() {
            const overlay = document.getElementById('phone-picker-modal');
            if (!overlay) return;
            overlay.classList.remove('show');
            overlay.setAttribute('aria-hidden', 'true');
            _pickerLineId = null;
        }

        function setPickerTab(tab) {
            if (tab !== 'IFB' && tab !== 'PL') return;
            pickerTab = tab;
            const ifb = document.getElementById('picker-tab-ifb');
            const pl = document.getElementById('picker-tab-pl');
            ifb?.classList.toggle('active', tab === 'IFB');
            pl?.classList.toggle('active', tab === 'PL');
            renderPickerList();
        }

        function listAllBranches(category) {
            const groups = phoneDirectory[category] || [];
            const items = [];
            groups.forEach(g => {
                (g.branches || []).forEach(b => {
                    items.push({
                        category,
                        groupId: g.id,
                        groupName: g.name,
                        branchId: b.id,
                        name: b.name,
                        number: b.number
                    });
                });
            });
            return items;
        }

        function renderPickerList() {
            const list = document.getElementById('picker-list');
            if (!list) return;

            const q = (document.getElementById('picker-search')?.value || '').trim().toLowerCase();
            const items = listAllBranches(pickerTab)
                .filter(it => !q ||
                    (it.name || '').toLowerCase().includes(q) ||
                    (it.number || '').toLowerCase().includes(q) ||
                    (it.groupName || '').toLowerCase().includes(q)
                );

            if (items.length === 0) {
                list.innerHTML = `<div class="section-subtitle" style="margin: 10px 2px;">No matching contacts.</div>`;
                return;
            }

            list.innerHTML = items.map(it => {
                return `
                    <div class="dir-branch">
                        <div class="dir-branch-main">
                            <div class="dir-branch-name">${escapeHTML(it.name)} <span class="dir-chip" style="margin-left: 8px;">${escapeHTML(it.groupName)}</span></div>
                            <div class="dir-branch-number">${escapeHTML(it.number)}</div>
                        </div>
                        <div class="dir-branch-actions">
                            <button class="dir-select-btn" data-pick-number="${escapeHTML(it.number)}">Insert</button>
                        </div>
                    </div>
                `;
            }).join('');

            // Delegated click handler for pick buttons (avoids inline JS injection)
            list.querySelectorAll('[data-pick-number]').forEach(btn => {
                btn.addEventListener('click', () => pickNumber(btn.dataset.pickNumber));
            });
        }

        function pickNumber(number) {
            if (_pickerLineId == null) {
                // If called from settings "Use" button (without a target line),
                // just copy into the first idle line as a helpful default.
                const firstIdle = Object.keys(lines).map(x => parseInt(x)).find(id => lines[id].state === 'idle');
                _pickerLineId = (firstIdle != null) ? firstIdle : 1;
            }

            const lineId = parseInt(_pickerLineId);
            if (!lines[lineId]) return;

            // Insert number into line
            lines[lineId].number = String(number || '').trim();
            renderLine(lineId);

            closePhonePicker();
        }


        function init() {
            const numLines = 8; // Fixed to 8 lines
            
            // Initialize lines
            for (let i = 1; i <= numLines; i++) {
                lines[i] = {
                    state: 'idle',
                    number: '',
                    contactName: '',
                    audioChannel: 0,
                    duration: 0,
                    lastDialed: '',
                    muted: true
                };
            }
            
            renderLines();
            startActiveCallTimer();
            loadDirectory();
            renderPhoneDirectory();
            updateNetworkInfo();
            
            // SIP Status: WebSocket primary, HTTP polling fallback
            initSipStatusRealtime();
            loadLargeKeysPref();
            enableDragScrollAnywhere();

            // Restore last status from backend so refresh keeps line state (connected, dialing, etc.)
            refreshActiveCalls();
        }
        
        // Render all lines
        function renderLines() {
            const container = document.getElementById('lines-container');
            container.innerHTML = '';
            
            Object.keys(lines).forEach(lineId => {
                const lineElement = createLineElement(parseInt(lineId));
                container.appendChild(lineElement);
            });
        }
        
        // Render single line
        function renderLine(lineId) {
            const lineElement = document.getElementById(`line-${lineId}`);
            if (lineElement) {
                const newElement = createLineElement(lineId);
                lineElement.replaceWith(newElement);
            }
        }
        
        // Create line element
        function createLineElement(lineId) {
            const line = lines[lineId];
            const safeNumber = (line.number ?? '').toString();
            const safeNumberDisplay = safeNumber ? escapeHTML(safeNumber) : '—';
            const div = document.createElement('div');
            div.id = `line-${lineId}`;
            div.className = `phone-line ${line.state}`;
            
            const isIdle = line.state === 'idle';
            const isError = line.state === 'error';
            const isAvailable = isIdle && sipConnected;  // Available when idle AND system online
            const statusClass = isAvailable ? 'status-available' : `status-${line.state}`;
            const statusText = isAvailable ? 'Available' : (line.state === 'connected' ? 'Active' : (line.state === 'error' ? 'Error' : (line.state.charAt(0).toUpperCase() + line.state.slice(1))));
            const isRinging = line.state === 'ringing';       // outgoing ringback
            const isConnected = line.state === 'connected';
            const isDialing = line.state === 'dialing';
            const showCallInfo = (isDialing || isRinging || isConnected) && !!(line.remoteNumber || line.number);
            const remoteNum = (line.remoteNumber || line.number || '').toString();
            const remoteName = (line.remoteName || (remoteNum ? findDirectoryNameByNumber(remoteNum) : '') || '').toString();
            const callNameHTML = remoteName ? `<span class="line-call-name">${escapeHTML(remoteName)}</span>` : '';
            const callNumHTML = remoteNum ? `<span class="line-call-number">${escapeHTML(remoteNum)}</span>` : '';
            const timeId = `call-time-${lineId}`;
            const callTimeHTML = isConnected ? `<span class="line-call-time" id="${timeId}">${formatCallDuration(line.callStart || 0)}</span>` : '';
            const callInfoClass = showCallInfo ? 'line-call-info show' : 'line-call-info';

            // Quick Dial Prefix — full-width button below the dialpad if a default is set
            const _defaultPrefix = (phoneDirectory && phoneDirectory.defaultPrefixId)
                ? (phoneDirectory.prefixes || []).find(p => p.id === phoneDirectory.defaultPrefixId)
                : null;

            div.innerHTML = `
                <div class="line-header">
                    <div class="line-number">Line ${lineId}</div>
                    <div class="line-status ${statusClass}">${statusText}</div>
                </div>
                
                <div class="audio-selector">
                    <label>Audio Channel</label>
                    <div class="audio-selector-row">
                        <div class="custom-dropdown" id="dropdown-${lineId}">
                            <div class="custom-dropdown-selected ${line.audioChannel > 0 ? 'has-channel' : ''}" onclick="toggleDropdown(${lineId})">
                                <span>${line.audioChannel === 0 ? 'No Output' : `Channel ${line.audioChannel}`}</span>
                                <span class="custom-dropdown-arrow">▼</span>
                            </div>
                            <div class="custom-dropdown-list">
                                ${line.audioChannel !== 0 ? `<div class="custom-dropdown-option" onclick="selectChannel(event, ${lineId}, 0)">
                                    No Output
                                </div>` : ''}
                                ${[1,2,3,4,5,6,7,8]
                                    .filter(ch => ch !== line.audioChannel) // Don't show currently selected channel
                                    .map(ch => {
                                        const usingLineId = audioChannels[ch];
                                        const inUse = usingLineId && usingLineId !== lineId;
                                        const selected = line.audioChannel === ch ? 'selected' : '';
                                        const colorClass = inUse ? 'channel-in-use' : 'channel-available';
                                        const statusText = inUse ? `(In Use by Line ${usingLineId})` : '(Available)';
                                        return `<div class="custom-dropdown-option ${selected} ${colorClass}" onclick="selectChannel(event, ${lineId}, ${ch})">
                                            Channel ${ch} ${statusText}
                                        </div>`;
                                    }).join('')}
                                </div>
                            </div>
                        <div class="headset-controls">
                            <button class="headset-toggle ${!line.muted ? 'active' : ''}" onclick="toggleMute(${lineId})" title="${!line.muted ? 'Headset on (Listen & Talk) - Click to mute' : 'Listen & Talk - Click to use headset on this line'}">
                                <svg viewBox="0 0 48 48">
                                    <!-- Headband -->
                                    <path d="M 12 20 Q 24 8, 36 20" stroke-width="3"/>
                                    <!-- Left ear cup -->
                                    <rect x="8" y="20" width="8" height="12" rx="4" stroke-width="2.5"/>
                                    <!-- Right ear cup -->
                                    <rect x="32" y="20" width="8" height="12" rx="4" stroke-width="2.5"/>
                                    <!-- Mic boom -->
                                    <path d="M 10 32 Q 10 38, 16 40" stroke-width="2.5"/>
                                    <!-- Mic capsule -->
                                    <rect x="16" y="38" width="6" height="4" rx="2" stroke-width="2"/>
                                </svg>
                            </button>
                        </div>
                    </div>
                </div>
                
                <div class="phone-display">
                    ${line.contactName ? `<div class="contact-name-display">${escapeHTML(line.contactName)}</div>` : ``}
                    <div class="phone-number ${line.state}" style="font-size: ${getPhoneNumberFontSize(line.number.length)}; letter-spacing: ${getLetterSpacing(line.number.length)}">${safeNumberDisplay}</div>
                </div>
                
                ${(isIdle || isError) ? `
                <div class="dialpad-vu-row">
                    <div class="vu-meter-wrap">
                        <span class="vu-db-label" id="vu-in-db-${lineId}">— dB</span>
                        <div class="vu-meter-track">
                            <div class="vu-meter-bar" id="vu-in-bar-${lineId}"></div>
                        </div>
                        <span class="vu-label">🎤</span>
                    </div>
                    <div class="dialpad">
                        ${['1','2','3','4','5','6','7','8','9','+','0','#'].map(d => 
                            `<button onclick="addDigit(${lineId}, '${d}')">${d}</button>`
                        ).join('')}
                        <button onclick="deleteDigit(${lineId})">⌫</button>
                        <button onclick="redial(${lineId})">↻</button>
                        <button onclick="showPhoneList(${lineId})"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="22" height="22"><rect x="25" y="8" rx="6" ry="6" width="60" height="84" fill="#7cb9e8" stroke="#2c3e50" stroke-width="4"/><rect x="18" y="22" width="12" height="5" rx="2" fill="#b87333"/><rect x="18" y="37" width="12" height="5" rx="2" fill="#b87333"/><rect x="18" y="52" width="12" height="5" rx="2" fill="#b87333"/><rect x="18" y="67" width="12" height="5" rx="2" fill="#b87333"/><path d="M62 38c0-4-3-7-7-7s-7 3-7 7 3 6 7 6 7-2 7-6z" fill="#e8a65d" stroke="#2c3e50" stroke-width="2"/><path d="M50 42c-1 1-2 3-1 4l2 3c1 1 2 1 3 0l3-3c1-1 1-2 0-3l-2-3c0-1-1-1-2 0" fill="#e8a65d" stroke="#2c3e50" stroke-width="1.5"/><line x1="42" y1="62" x2="68" y2="62" stroke="#2c3e50" stroke-width="3" stroke-linecap="round"/><line x1="42" y1="72" x2="62" y2="72" stroke="#2c3e50" stroke-width="3" stroke-linecap="round"/></svg></button>
                        ${_defaultPrefix ? `<button class="dialpad-prefix" style="grid-column: span 3; font-weight:500;" onclick="applyPrefix(${lineId})" title="Apply prefix: ${escapeHTML(_defaultPrefix.value)}">${escapeHTML(_defaultPrefix.label)}</button>` : ''}
                    </div>
                    <div class="vu-meter-wrap">
                        <span class="vu-db-label" id="vu-out-db-${lineId}">— dB</span>
                        <div class="vu-meter-track">
                            <div class="vu-meter-bar" id="vu-out-bar-${lineId}"></div>
                        </div>
                        <span class="vu-label">🔊</span>
                    </div>
                </div>
                ` : `
                <div class="dialpad-vu-row dialpad-vu-row--active">
                    <div class="vu-meter-wrap">
                        <span class="vu-db-label" id="vu-in-db-${lineId}">— dB</span>
                        <div class="vu-meter-track">
                            <div class="vu-meter-bar" id="vu-in-bar-${lineId}"></div>
                        </div>
                        <span class="vu-label">🎤</span>
                    </div>
                    <div class="vu-spacer"></div>
                    <div class="vu-meter-wrap">
                        <span class="vu-db-label" id="vu-out-db-${lineId}">— dB</span>
                        <div class="vu-meter-track">
                            <div class="vu-meter-bar" id="vu-out-bar-${lineId}"></div>
                        </div>
                        <span class="vu-label">🔊</span>
                    </div>
                </div>
                `}
                
                <div class="action-buttons">
                    ${isIdle ? `
                        <button class="btn btn-call" onclick="makeCall(${lineId})" ${!line.number ? 'disabled' : ''}>
                            📞 Call
                        </button>
                        <button class="btn btn-clear" onclick="clearNumber(${lineId})">
                            ✕ Clear
                        </button>
                    ` : ''}
                    ${isError ? `
                        <button class="btn btn-hangup" onclick="hangupCall(${lineId})">
                            ✕ Clear error
                        </button>
                        <button class="btn btn-call" onclick="makeCall(${lineId})" ${!line.number ? 'disabled' : ''}>
                            📞 Call again
                        </button>
                    ` : ''}
                    ${(isConnected || isDialing || isRinging) ? `
                        <button class="btn btn-hangup" onclick="confirmHangup(${lineId})" style="grid-column: span 2">
                            📵 Hang Up
                        </button>
                    ` : ''}
                </div>
                ${showCallInfo ? `<div class="line-call-info-under"><div class="line-call-info show">${callNameHTML}${callNumHTML}${callTimeHTML}</div></div>` : ''}
            `;
            
            return div;
        }
        
        // Change audio channel
        async function changeAudioChannel(lineId, newChannel) {
            console.log(`changeAudioChannel called: lineId=${lineId}, newChannel=${newChannel}`);
            
            newChannel = parseInt(newChannel);
            lineId = parseInt(lineId);
            
            const currentChannel = lines[lineId].audioChannel || 0;  // normalize null/undefined → 0
            
            // Check if new channel is already in use by another line
            if (newChannel > 0) {
                const oldLineId = audioChannels[newChannel];
                console.log(`Channel ${newChannel} currently used by: ${oldLineId}`);
                
                // If channel is used by another line (not this line)
                if (oldLineId && oldLineId !== lineId) {
                    // Show warning when channel is in use (custom modal)
                    const confirmed = await showConfirmModal({
                        title: 'Audio Channel In Use',
                        message: `Channel ${newChannel} is selected already for Line ${oldLineId}.\n\n` +
                        `Are you sure you want to switch it to Line ${lineId}?`,
                        okText: 'Switch',
                        cancelText: 'Keep Current'
                    });

                    console.log(`User clicked: $OK`);

                    if (!confirmed) {
                        // User clicked Cancel - close dropdown and do nothing
                        document.querySelectorAll('.custom-dropdown').forEach(dd => {
                            dd.classList.remove('open');
                    }
);
                        return;
                    }
                    
                    // User clicked OK - Remove channel from old line
                    console.log(`Removing channel ${newChannel} from Line ${oldLineId}`);
                    delete audioChannels[newChannel];
                    lines[oldLineId].audioChannel = 0;
                    
                    // Update backend for old line
                    try {
                        await apiFetch(`/api/lines/${oldLineId}/audio`, {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({audio_channel: 0})
                        });
                    } catch (error) {
                        console.error('Failed to update old line:', error);
                    }
                }
            }
            
            // Clear current line's old channel (if any)
            if (currentChannel > 0) {
                console.log(`Clearing old channel ${currentChannel} from line ${lineId}`);
                delete audioChannels[currentChannel];
            }
            
            // Assign new channel to current line
            if (newChannel > 0) {
                console.log(`Assigning channel ${newChannel} to line ${lineId}`);
                audioChannels[newChannel] = lineId;
            }
            
            // Update line state
            lines[lineId].audioChannel = newChannel;
            console.log(`Line ${lineId} audioChannel set to ${newChannel}`);
            console.log('Current audioChannels:', JSON.stringify(audioChannels));
            console.log('Current lines channels:', Object.keys(lines).map(l => `Line ${l}: Ch${lines[l].audioChannel}`));
            
            // Close ALL dropdowns BEFORE renderLines (important!)
            document.querySelectorAll('.custom-dropdown').forEach(dd => {
                dd.classList.remove('open');
            });
            
            // Small delay to ensure dropdown closes before re-rendering
            await new Promise(resolve => setTimeout(resolve, 50));
            
            // Re-render ALL lines to update dropdown status everywhere
            console.log('Calling renderLines()...');
            renderLines();
            console.log('renderLines() completed');
            
            // Update backend for current line
            try {
                await apiFetch(`/api/lines/${lineId}/audio`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({audio_channel: newChannel})
                });
            } catch (error) {
                console.error('Failed to update audio channel:', error);
            }
        }
        
        // Toggle mute state (headset Listen & Talk: only one line can have headset on)
        async function toggleMute(lineId) {
            lines[lineId].muted = !lines[lineId].muted;
            if (!lines[lineId].muted) {
                // Unmuting this line: turn off headset for all other lines so only one shows on
                for (let i = 1; i <= 8; i++) {
                    if (i !== lineId && lines[i]) lines[i].muted = true;
                }
            }
            renderLines();
            try {
                await apiFetch(`/api/lines/${lineId}/mute`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({muted: lines[lineId].muted})
                });
            } catch (error) {
                console.error('Failed to update mute state:', error);
            }
        }
        
        // Custom dropdown functions
        // We teleport the dropdown list to <body> so it escapes
        // the backdrop-filter stacking context on .phone-line cards.
        let _activeDropdownLineId = null;

        function _closeAllDropdowns() {
            document.querySelectorAll('.custom-dropdown').forEach(dd => {
                dd.classList.remove('open');
            });
            // Return any teleported list back to its dropdown parent
            const floater = document.getElementById('dropdown-floater');
            if (floater && floater._sourceDropdown) {
                floater._sourceDropdown.appendChild(floater._listEl);
                floater._listEl.style.display = '';
                floater._listEl = null;
                floater._sourceDropdown = null;
            }
            if (floater) floater.style.display = 'none';
            _activeDropdownLineId = null;
        }

        function toggleDropdown(lineId) {
            const dropdown = document.getElementById(`dropdown-${lineId}`);
            if (dropdown.dataset.disabled === 'true') return;

            // If already open, just close
            if (_activeDropdownLineId === lineId) {
                _closeAllDropdowns();
                return;
            }

            // Close any other open dropdown first
            _closeAllDropdowns();

            // Get or create the floating container on <body>
            let floater = document.getElementById('dropdown-floater');
            if (!floater) {
                floater = document.createElement('div');
                floater.id = 'dropdown-floater';
                floater.style.cssText = 'position:fixed;z-index:99999;display:none;';
                document.body.appendChild(floater);
            }

            const selected = dropdown.querySelector('.custom-dropdown-selected');
            const list = dropdown.querySelector('.custom-dropdown-list');
            const rect = selected.getBoundingClientRect();
            const gap = 4;
            const margin = 12;

            // Move list into the floater on <body>
            floater._sourceDropdown = dropdown;
            floater._listEl = list;
            floater.appendChild(list);
            floater.style.display = 'block';

            // Measure natural height
            list.style.display = 'block';
            list.style.maxHeight = 'none';
            list.style.position = 'static';
            const listHeight = list.scrollHeight;

            // Now set fixed positioning on floater
            floater.style.left = rect.left + 'px';
            floater.style.width = rect.width + 'px';

            const spaceBelow = window.innerHeight - rect.bottom - gap - margin;
            const spaceAbove = rect.top - gap - margin;

            if (listHeight <= spaceBelow || spaceBelow >= spaceAbove) {
                // Open downward
                floater.style.top = (rect.bottom + gap) + 'px';
                floater.style.bottom = '';
                list.style.maxHeight = spaceBelow + 'px';
            } else {
                // Open upward
                floater.style.top = '';
                floater.style.bottom = (window.innerHeight - rect.top + gap) + 'px';
                list.style.maxHeight = spaceAbove + 'px';
            }

            list.style.position = '';
            list.style.display = 'block';
            dropdown.classList.add('open');
            _activeDropdownLineId = lineId;
        }
        
        function selectChannel(event, lineId, channel) {
            // Stop event propagation to prevent dropdown from reopening
            if (event) {
                event.stopPropagation();
                event.preventDefault();
            }
            // Close the dropdown immediately
            _closeAllDropdowns();
            changeAudioChannel(lineId, channel);
        }
        
        // Close dropdowns when clicking outside
        document.addEventListener('click', (e) => {
            if (!e.target.closest('.custom-dropdown') && !e.target.closest('#dropdown-floater')) {
                _closeAllDropdowns();
            }
        });

        // Reposition floater on scroll so it tracks the dropdown button
        document.addEventListener('scroll', () => {
            if (_activeDropdownLineId === null) return;
            const dropdown = document.getElementById(`dropdown-${_activeDropdownLineId}`);
            const floater = document.getElementById('dropdown-floater');
            if (!dropdown || !floater) return;
            const selected = dropdown.querySelector('.custom-dropdown-selected');
            if (!selected) return;
            const rect = selected.getBoundingClientRect();
            const gap = 4;
            const margin = 12;
            const list = floater._listEl;
            const listHeight = list ? list.scrollHeight : 0;
            floater.style.left = rect.left + 'px';
            floater.style.width = rect.width + 'px';
            const spaceBelow = window.innerHeight - rect.bottom - gap - margin;
            const spaceAbove = rect.top - gap - margin;
            if (listHeight <= spaceBelow || spaceBelow >= spaceAbove) {
                floater.style.top = (rect.bottom + gap) + 'px';
                floater.style.bottom = '';
            } else {
                floater.style.top = '';
                floater.style.bottom = (window.innerHeight - rect.top + gap) + 'px';
            }
        }, true); // capture phase so it fires for any scrollable container
        
        // Add digit to number
        function addDigit(lineId, digit) {
            if (lines[lineId].state === 'idle' || lines[lineId].state === 'error') {
                if (lines[lineId].state === 'error') {
                    lines[lineId].state = 'idle'; // Clear error on new input
                }
                lines[lineId].number += digit;
            lines[lineId].contactName = '';
                renderLine(lineId);
            }
        }
        
        // Delete last digit
        function deleteDigit(lineId) {
            if ((lines[lineId].state === 'idle' || lines[lineId].state === 'error') && lines[lineId].number.length > 0) {
                if (lines[lineId].state === 'error') {
                    lines[lineId].state = 'idle';
                }
                lines[lineId].number = lines[lineId].number.slice(0, -1);
                renderLine(lineId);
            }
        }
        
        // Redial last number
        function redial(lineId) {
            if ((lines[lineId].state === 'idle' || lines[lineId].state === 'error') && lines[lineId].lastDialed) {
                if (lines[lineId].state === 'error') {
                    lines[lineId].state = 'idle';
                }
                lines[lineId].number = lines[lineId].lastDialed;
                renderLine(lineId);
            }
        }
        
        // Show phone list

        
        // Clear number and contact name (e.g. after selecting from phone list)
        function clearNumber(lineId) {
            if (lines[lineId].state === 'idle' || lines[lineId].state === 'error') {
                if (lines[lineId].state === 'error') {
                    lines[lineId].state = 'idle';
                }
                lines[lineId].number = '';
                lines[lineId].contactName = '';
                renderLine(lineId);
            }
        }
        
        // Make call
        async function makeCall(lineId) {
            const number = lines[lineId].number;
            if (!number) return;
            
            // Immediately set to dialing to prevent race condition with Socket.IO
            lines[lineId].lastDialed = number;
            lines[lineId].state = 'dialing';
            renderLine(lineId);
            
            try {
                const response = await apiFetch(`/api/lines/${lineId}/dial`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({phone_number: number})
                });
                
                if (!response.ok) {
                    // If dial failed, revert to idle
                    console.error('Dial API failed with status:', response.status);
                    lines[lineId].state = 'idle';
                    renderLine(lineId);
                }
                // If successful, backend will emit proper state via Socket.IO
            } catch (error) {
                console.error('Failed to make call:', error);
                // Revert to idle on error
                lines[lineId].state = 'idle';
                renderLine(lineId);
            }
        }
        
        // Hangup call (called after user confirms)
        async function hangupCall(lineId) {
            try {
                await apiFetch(`/api/lines/${lineId}/hangup`, {method: 'POST'});
                lines[lineId].state = 'idle';
                lines[lineId].number = '';
                lines[lineId].duration = 0;
                renderLine(lineId);
            } catch (error) {
                console.error('Failed to hangup:', error);
            }
        }

        // Confirm hangup dialog (web + GUI) – "Are you sure you want to hang up?"
        async function confirmHangup(lineId) {
            const confirmed = await showConfirmModal({
                title: 'Confirm Hangup',
                message: 'Are you sure you want to hang up?',
                okText: 'Yes',
                cancelText: 'Cancel'
            });
            if (confirmed) await hangupCall(lineId);
        }
        
        // Get dynamic font size for phone number based on length
        function getPhoneNumberFontSize(numberLength) {
            if (numberLength <= 10) return '1.6em';
            if (numberLength <= 15) return '1.3em';
            if (numberLength <= 20) return '1.0em';
            if (numberLength <= 25) return '0.85em';
            return '0.7em';
        }
        
        // Get dynamic letter spacing based on number length
        function getLetterSpacing(numberLength) {
            if (numberLength <= 10) return '2px';
            if (numberLength <= 15) return '1.5px';
            if (numberLength <= 20) return '1px';
            return '0.5px';
        }
        
        // Network settings
        // Settings Dropdown Toggle
        
        function collapseSettingsSubs() {
            ['settings-sub-net', 'settings-sub-dir'].forEach(id => {
                const el = document.getElementById(id);
                if (el) el.classList.remove('open');
            });
        }

        function toggleSettingsSub(id) {
            const el = document.getElementById(id);
            if (!el) return;
            el.classList.toggle('open');
        }

function toggleSettingsDropdown() {
            const dropdown = document.getElementById('settings-dropdown');
            const arrow = document.getElementById('settings-arrow');
            
            if (dropdown.style.display === 'none') {
                dropdown.style.display = 'block';
                collapseSettingsSubs();
                dropdown.classList.add('open');
                arrow.classList.add('open');
                updateNetworkInfo();
                loadExtraInterfaces();
                _startNetPoll();
            } else {
                dropdown.classList.remove('open');
                arrow.classList.remove('open');
                _stopNetPoll();
                setTimeout(() => {
                    dropdown.style.display = 'none';
                }, 300); // Wait for animation to complete
            }
        }

        // ── Network poll timer (eth0 + eth1) ──────────────────────────
        let _netPollTimer = null;
        function _startNetPoll() {
            _stopNetPoll();
            _netPollTimer = setInterval(function() {
                updateNetworkInfo();
                loadExtraInterfaces();
            }, 5000);
        }
        function _stopNetPoll() {
            if (_netPollTimer) { clearInterval(_netPollTimer); _netPollTimer = null; }
        }

        // ── eth1 (Dante) inline helpers ───────────────────────────────────
        let pendingEth1Mode = null; // Track user's pending toggle change for eth1
        
        async function loadExtraInterfaces() {
            try {
                const r      = await apiFetch('/api/network/interfaces');
                const data   = await r.json();
                const ifaces = Array.isArray(data) ? data : (data.interfaces || []);
                const eth1Iface = ifaces.find(i => i.name !== 'eth0');
                const eth1Sec = document.getElementById('net-eth1-section');
                if (eth1Sec) {
                    eth1Sec.style.display = 'none';
                    if (eth1Iface) {
                        document.getElementById('eth1-dhcp-ip').textContent = eth1Iface.ip || '—';
                        document.getElementById('eth1-dhcp-subnet').textContent = eth1Iface.subnet || '—';
                        document.getElementById('eth1-dhcp-gateway').textContent = eth1Iface.gateway || '—';
                        document.getElementById('eth1-status').textContent  = eth1Iface.connected ? `Connected (${eth1Iface.name})` : `No link (${eth1Iface.name})`;
                        const isStatic = eth1Iface.mode === 'static';
                        // FIXED: Don't overwrite toggle if user is actively changing it
                        if (pendingEth1Mode === null) {
                            document.getElementById('eth1-mode-switch').checked = isStatic;
                        }
                        // Use toggle state (not backend) to control display
                        const toggleIsStatic = document.getElementById('eth1-mode-switch').checked;
                        document.getElementById('eth1-dhcp-info').style.display    = toggleIsStatic ? 'none'  : 'block';
                        document.getElementById('eth1-static-info').style.display  = toggleIsStatic ? 'block' : 'none';
                        document.getElementById('eth1-dhcp-label').classList.toggle('active', !toggleIsStatic);
                        document.getElementById('eth1-manual-label').classList.toggle('active', toggleIsStatic);
                        if (isStatic) {
                            // Only update if user is NOT currently editing (FIXED)
                            const eth1IpInput = document.getElementById('eth1-ip');
                            const eth1SubnetInput = document.getElementById('eth1-subnet');
                            // Only overwrite if: user not editing AND backend has data AND value changed
                            if (document.activeElement !== eth1IpInput && eth1Iface.ip && eth1IpInput.value !== eth1Iface.ip) {
                                eth1IpInput.value = eth1Iface.ip;
                            }
                            if (document.activeElement !== eth1SubnetInput && eth1Iface.subnet && eth1SubnetInput.value !== eth1Iface.subnet) {
                                eth1SubnetInput.value = eth1Iface.subnet;
                            }
                        }
                        eth1Sec.dataset.iface = eth1Iface.name;
                    } else {
                        document.getElementById('eth1-dhcp-ip').textContent = '—';
                        document.getElementById('eth1-dhcp-subnet').textContent = '—';
                        document.getElementById('eth1-dhcp-gateway').textContent = '—';
                        document.getElementById('eth1-status').textContent  = 'Dongle not connected';
                        eth1Sec.dataset.iface = 'eth1';
                    }
                }
            } catch(e) {
                console.warn('loadExtraInterfaces:', e);
            }
        }

        function eth1ModeChange() {
            const isStatic = document.getElementById('eth1-mode-switch').checked;
            pendingEth1Mode = isStatic ? 'static' : 'dhcp';
            document.getElementById('eth1-dhcp-info').style.display   = isStatic ? 'none'  : 'block';
            document.getElementById('eth1-static-info').style.display = isStatic ? 'block' : 'none';
            document.getElementById('eth1-dhcp-label').classList.toggle('active', !isStatic);
            document.getElementById('eth1-manual-label').classList.toggle('active', isStatic);

            // When switching to DHCP, apply immediately (no need to fill in an IP)
            if (!isStatic) {
                const ifaceName = document.getElementById('net-eth1-section').dataset.iface || 'eth1';
                apiFetch(`/api/network/interface/${ifaceName}/set`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ mode: 'dhcp' })
                }).then(r => r.json()).then(d => {
                    if (d.error) {
                        showAlertModal({ title: 'Error', message: d.error });
                        // Revert toggle on failure
                        document.getElementById('eth1-mode-switch').checked = true;
                        pendingEth1Mode = 'static';
                        document.getElementById('eth1-dhcp-info').style.display   = 'none';
                        document.getElementById('eth1-static-info').style.display = 'block';
                        document.getElementById('eth1-dhcp-label').classList.toggle('active', false);
                        document.getElementById('eth1-manual-label').classList.toggle('active', true);
                        return;
                    }
                    pendingEth1Mode = null; // Allow polling to take over
                    setTimeout(loadExtraInterfaces, 1500);
                }).catch(e => {
                    showAlertModal({ title: 'Error', message: String(e) });
                });
            }
        }

        async function applyEth1Static() {
            const ip        = (document.getElementById('eth1-ip').value     || '').trim();
            const subnet    = (document.getElementById('eth1-subnet').value || '255.255.0.0').trim();
            const isStatic  = document.getElementById('eth1-mode-switch').checked;
            const mode      = isStatic ? 'static' : 'dhcp';
            const ifaceName = document.getElementById('net-eth1-section').dataset.iface || 'eth1';
            if (mode === 'static' && !ip) {
                showAlertModal({ title: 'Attention', message: 'IP address is required for static mode.' });
                return;
            }
            try {
                const r = await apiFetch(`/api/network/interface/${ifaceName}/set`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ mode, ip, subnet })
                });
                const d = await r.json();
                // FIXED: Do not auto-reload after save to prevent overwriting user input
                if (d.error) { showAlertModal({ title: 'Error', message: d.error }); return; }
                pendingEth1Mode = null; // Clear pending state after successful save
                showAlertModal({ title: 'Dante Applied', message: `${ifaceName} set to ${mode.toUpperCase()}${ip ? ' — ' + ip : ''}.` });
                setTimeout(loadExtraInterfaces, 1500);
            } catch(e) {
                showAlertModal({ title: 'Error', message: String(e) });
            }
        }

        // Virtual Keyboard
        let vkeyboardTargetId = null;
        
        
        // ===============================
        // LARGE key mode (kiosk) - saved preference
        // ===============================
        const VK_LARGE_PREF_KEY = 'vk_large_mode_v1';
        let vkeyboardLarge = false;

        function loadLargeKeysPref() {
            try {
                vkeyboardLarge = (localStorage.getItem(VK_LARGE_PREF_KEY) === '1');
            } catch (e) { vkeyboardLarge = false; }
        }

        function applyLargeKeysUI() {
            const overlay = document.getElementById('vkeyboard-overlay');
            overlay?.classList.toggle('vk-large', !!vkeyboardLarge);

            const btn = document.getElementById('vkey-mode-large');
            btn?.classList.toggle('active', !!vkeyboardLarge);
            if (btn) btn.textContent = vkeyboardLarge ? 'SMALL' : 'LARGE';
        }

        function setLargeKeys(on) {
            vkeyboardLarge = !!on;
            applyLargeKeysUI();
            try { localStorage.setItem(VK_LARGE_PREF_KEY, vkeyboardLarge ? '1' : '0'); } catch (e) {}
        }

        function toggleLargeKeys() {
            setLargeKeys(!vkeyboardLarge);
        }

        // ===============================
        // Drag-to-scroll-anywhere (PyQt5 touch friendly)
        // - scrolls the nearest scrollable container under your finger,
        //   otherwise scrolls the page
        // ===============================
        function enableDragScrollAnywhere() {
            const state = { down:false, dragging:false, sx:0, sy:0, st:0, el:null };

            const isOverlayOpen = () => {
                const vk = document.getElementById('vkeyboard-overlay');
                if (vk && vk.style.display && vk.style.display !== 'none') return true;
                // any modal overlay that is visible
                const openModal = document.querySelector('.modal-overlay.show');
                return !!openModal;
            };

            const ignoreSelector = [
                'button', 'a', 'input', 'textarea', 'select',
                '.custom-dropdown-selected', '.custom-dropdown-option',
                '.vkey-btn', '.virtual-keyboard', '.modal-box',
                '.dir-icon-btn', '.dir-danger-btn', '.dir-btn',
                '.settings-toggle-btn', '.switch', '.slider'
            ].join(',');

            function findScrollable(startEl) {
                let el = startEl;
                while (el && el !== document.body && el !== document.documentElement) {
                    if (el.nodeType !== 1) { el = el.parentElement; continue; }
                    const cs = getComputedStyle(el);
                    const oy = cs.overflowY;
                    if ((oy === 'auto' || oy === 'scroll') && el.scrollHeight > el.clientHeight + 2) {
                        return el;
                    }
                    el = el.parentElement;
                }
                // fallback to page scrolling element
                return document.scrollingElement || document.documentElement;
            }

            document.addEventListener('pointerdown', (e) => {
                if (isOverlayOpen()) return;
                if (e.pointerType === 'mouse' && e.button !== 0) return;
                if (e.target && e.target.closest && e.target.closest(ignoreSelector)) return;

                state.down = true;
                state.dragging = false;
                state.sx = e.clientX;
                state.sy = e.clientY;
                state.el = findScrollable(e.target);
                state.st = state.el.scrollTop;

                document.body.classList.add('drag-scroll-active');
            }, { passive: true });

            document.addEventListener('pointermove', (e) => {
                if (!state.down || !state.el) return;

                const dx = e.clientX - state.sx;
                const dy = e.clientY - state.sy;

                // threshold to avoid killing taps
                if (!state.dragging && Math.hypot(dx, dy) < 8) return;

                state.dragging = true;
                // vertical scroll only; rAF syncs update to next frame (faster load-up on Pi)
                const scrollDy = dy;
                const scrollEl = state.el;
                const scrollBase = state.st;
                requestAnimationFrame(() => {
                    if (!state.down || !scrollEl) return;
                    scrollEl.scrollTop = scrollBase - scrollDy;
                });

                e.preventDefault();
            }, { passive: false });

            function endDrag() {
                state.down = false;
                document.body.classList.remove('drag-scroll-active');
                // keep dragging flag briefly to suppress click
                setTimeout(() => { state.dragging = false; }, 0);
            }

            document.addEventListener('pointerup', endDrag, { passive: true });
            document.addEventListener('pointercancel', endDrag, { passive: true });

            // Prevent accidental clicks after dragging
            document.addEventListener('click', (e) => {
                if (state.dragging) {
                    e.stopPropagation();
                    e.preventDefault();
                }
            }, true);
        }
function showVirtualKeyboard(inputId) {
            // Web UI (accessed by IP) — skip virtual keyboard, use physical keyboard
            if (location.hostname !== 'localhost' && location.hostname !== '127.0.0.1') {
                const el = document.getElementById(inputId);
                if (el) { el.removeAttribute('readonly'); el.focus(); }
                return;
            }
            vkeyboardTargetId = inputId;
            const input = document.getElementById(inputId);
            const overlay = document.getElementById('vkeyboard-overlay');
            const vkeyInput = document.getElementById('vkeyboard-input');
            const title = document.getElementById('vkeyboard-title');

            if (!input || !overlay || !vkeyInput || !title) return;

            // Title logic:
            // - For manual network fields: fixed titles
            // - For everything else (Phone Directory): use data-vk-title or label text
            let t = '';
            if (inputId === 'manual-ip') t = 'Enter IP Address';
            else if (inputId === 'manual-subnet') t = 'Enter Subnet Mask';
            else if (inputId === 'manual-gateway') t = 'Enter Gateway';
            else if (input.dataset && input.dataset.vkTitle) t = input.dataset.vkTitle;
            else {
                // Try to infer from nearest label in editor
                const label = input.closest('.edit-field')?.querySelector('label')?.textContent || '';
                t = label ? `Edit ${label}` : 'Edit';
            }

            // User requested: default letters uppercase
            title.textContent = String(t || 'Edit').toUpperCase();

            // Load current value
            vkeyInput.value = input.value || '';

            // Choose keyboard mode based on input mode/type
            // - numeric for network / phone number
            // - alpha for names
            const im = (input.getAttribute('inputmode') || '').toLowerCase();
            const wantsNumeric = (inputId === 'manual-ip' || inputId === 'manual-subnet' || inputId === 'manual-gateway' || inputId === 'eth1-ip' || inputId === 'eth1-subnet' || im === 'numeric');
            vkeyboardShift = true; // default uppercase
            setKeyboardMode(wantsNumeric ? 'numeric' : 'alpha');

            // Show keyboard
            overlay.style.display = 'flex';
            applyLargeKeysUI();
        }
        
        
        // ===============================
        // Virtual Keyboard: Mode + Shift (default uppercase)
        // ===============================
        let vkeyboardMode = 'alpha';
        let vkeyboardShift = true; // default uppercase

        function setKeyboardMode(mode) {
            vkeyboardMode = (mode === 'numeric') ? 'numeric' : 'alpha';

            const alpha = document.getElementById('vkeyboard-alpha');
            const numeric = document.getElementById('vkeyboard-numeric');
            const btnABC = document.getElementById('vkey-mode-abc');
            const btn123 = document.getElementById('vkey-mode-123');

            if (alpha) alpha.style.display = (vkeyboardMode === 'alpha') ? 'grid' : 'none';
            if (numeric) numeric.style.display = (vkeyboardMode === 'numeric') ? 'grid' : 'none';

            btnABC?.classList.toggle('active', vkeyboardMode === 'alpha');
            btn123?.classList.toggle('active', vkeyboardMode === 'numeric');

            // Keep shift active by default for alpha
            if (vkeyboardMode === 'alpha') {
                updateAlphaKeyLabels();
                const shiftBtn = document.getElementById('vkey-shift');
                shiftBtn?.classList.toggle('active', vkeyboardShift);
            }
        }

        function toggleShift() {
            vkeyboardShift = !vkeyboardShift;
            const shiftBtn = document.getElementById('vkey-shift');
            shiftBtn?.classList.toggle('active', vkeyboardShift);
            updateAlphaKeyLabels();
        }

        function vkeyAlpha(letter) {
            const ch = vkeyboardShift ? String(letter).toUpperCase() : String(letter).toLowerCase();
            vkeyPress(ch);
        }


        
        function initAlphaKeysOnce() {
            const alpha = document.getElementById('vkeyboard-alpha');
            if (!alpha || alpha.dataset.inited === '1') return;

            alpha.querySelectorAll('button.vkey-btn').forEach(btn => {
                const t = (btn.textContent || '').trim();
                // Store base letter for alphabet keys only
                if (t.length === 1 && /[A-Z]/.test(t)) {
                    btn.dataset.baseLetter = t.toLowerCase();
                }
            });

            alpha.dataset.inited = '1';
        }

        function updateAlphaKeyLabels() {
            const alpha = document.getElementById('vkeyboard-alpha');
            if (!alpha) return;

            initAlphaKeysOnce();

            alpha.querySelectorAll('button.vkey-btn').forEach(btn => {
                const base = btn.dataset.baseLetter;
                if (!base) return;
                btn.textContent = vkeyboardShift ? base.toUpperCase() : base.toLowerCase();
            });
        }


        function vkeyPress(char) {
            const vkeyInput = document.getElementById('vkeyboard-input');
            vkeyInput.value += char;
        }
        
        function vkeyBackspace() {
            const vkeyInput = document.getElementById('vkeyboard-input');
            vkeyInput.value = vkeyInput.value.slice(0, -1);
        }
        
        function vkeyClear() {
            const vkeyInput = document.getElementById('vkeyboard-input');
            vkeyInput.value = '';
        }
        
        function vkeyDone() {
            const vkeyInput = document.getElementById('vkeyboard-input');
            const targetInput = document.getElementById(vkeyboardTargetId);
            targetInput.value = vkeyInput.value;
            closeVirtualKeyboard();
        }
        
        function closeVirtualKeyboard() {
            const overlay = document.getElementById('vkeyboard-overlay');
            overlay.style.display = 'none';
            vkeyboardTargetId = null;
        }
        
        // Network Configuration State
        let currentNetworkMode = 'dhcp'; // Current actual backend mode
        let pendingNetworkMode = null; // Mode user is editing (UI state)
        let savedManualConfig = { ip: '', subnet: '', gateway: '' }; // Last saved manual config
        let currentManualConfig = { ip: '', subnet: '', gateway: '' }; // Current input values
        
        // Refresh network mode from backend (without full update)
        async function refreshNetworkModeOnly() {
            try {
                const response = await apiFetch('/api/network/status');
                const data = await response.json();
                currentNetworkMode = data.mode || 'dhcp';
                return currentNetworkMode;
            } catch (error) {
                console.error('Failed to refresh network mode:', error);
                return currentNetworkMode;
            }
        }
        
        // Update Network Info from Backend
        async function updateNetworkInfo() {
            try {
                const response = await apiFetch('/api/network/status');
                const data = await response.json();
                
                // Update DHCP info display (shows current IP when on DHCP)
                document.getElementById('dhcp-ip-address').textContent = data.current_ip || data.ip_address || 'Unknown';
                document.getElementById('dhcp-subnet').textContent = data.subnet_mask || '255.255.255.0';
                document.getElementById('dhcp-gateway').textContent = data.gateway || 'Not set';
                document.getElementById('eth0-status').textContent = data.connected ? 'Connected (eth0)' : 'No link (eth0)';
                
                // Update current mode from backend
                currentNetworkMode = data.mode || 'dhcp';
                
                // If in manual mode, populate the manual fields with saved config
                if (currentNetworkMode === 'manual') {
                    savedManualConfig = {
                        ip: data.ip_address || '',
                        subnet: data.subnet_mask || '255.255.255.0',
                        gateway: data.gateway || ''
                    };
                    currentManualConfig = { ...savedManualConfig };
                    
                    document.getElementById('manual-ip').value = savedManualConfig.ip;
                    document.getElementById('manual-subnet').value = savedManualConfig.subnet;
                    document.getElementById('manual-gateway').value = savedManualConfig.gateway;
                }
                
                // Reset pending mode and update UI
                // FIXED: pendingNetworkMode = null; // Commented to prevent toggle from reverting
                updateNetworkModeUI();
            } catch (error) {
                console.error('Failed to fetch network info:', error);
                // Use current IP from URL if fetch fails
                document.getElementById('dhcp-ip-address').textContent = window.location.hostname || 'Unknown';
            }
        }
        
        // Update UI to reflect network mode (backend or pending edit mode)
        function updateNetworkModeUI() {
            const switchInput = document.getElementById('network-mode-switch');
            const dhcpLabel = document.getElementById('dhcp-label');
            const manualLabel = document.getElementById('manual-label');
            const dhcpInfo = document.getElementById('dhcp-info');
            const manualInfo = document.getElementById('manual-info');
            
            // Determine mode: use pendingNetworkMode if editing, otherwise backend mode
            const displayMode = pendingNetworkMode || currentNetworkMode;
            const isManual = displayMode === 'manual';
            
            // Update switch position
            switchInput.checked = isManual;
            
            // Update label colors
            dhcpLabel.classList.toggle('active', !isManual);
            manualLabel.classList.toggle('active', isManual);
            
            // Show/hide appropriate section
            if (isManual) {
                dhcpInfo.style.display = 'none';
                manualInfo.style.display = 'block';
            } else {
                dhcpInfo.style.display = 'block';
                manualInfo.style.display = 'none';
            }
        }
        
        // User requests to change network mode via toggle
        function requestNetworkModeChange() {
            console.log('⚠️ requestNetworkModeChange called for eth0!');
            const switchInput = document.getElementById('network-mode-switch');
            const requestedMode = switchInput.checked ? 'manual' : 'dhcp';
            
            // PC-like behavior:
            // - Toggling to Manual only changes the UI to let user enter static values.
            //   Backend switches only when user taps Save & Apply (with confirmation).
            // - Toggling to DHCP immediately asks confirmation *only if backend is currently manual*,
            //   then applies DHCP (reboot). If backend is already DHCP, just show DHCP UI.
            
            if (requestedMode === 'manual') {
                // Enter manual editing mode (UI only)
                pendingNetworkMode = 'manual';
                updateNetworkModeUI();
                
                // Restore last saved manual config (if any)
                if (savedManualConfig && savedManualConfig.ip) {
                    document.getElementById('manual-ip').value = savedManualConfig.ip;
                    document.getElementById('manual-subnet').value = savedManualConfig.subnet;
                    document.getElementById('manual-gateway').value = savedManualConfig.gateway;
                    currentManualConfig = { ...savedManualConfig };
                }
                return;
            }
            
            // requestedMode === 'dhcp'
            // If we were just editing manual (pending) but backend is DHCP, simply exit manual editing.
            if (currentNetworkMode !== 'manual') {
                pendingNetworkMode = null;
                updateNetworkModeUI();
                return;
            }
            
            // Backend mode might be stale; confirm with backend before prompting a reboot.
            refreshNetworkModeOnly().then((mode) => {
                if (mode !== 'manual') {
                    // No reboot needed because backend is already DHCP; just exit manual editing UI.
                    pendingNetworkMode = null;
                    updateNetworkModeUI();
                    return;
                }
                // Backend is manual -> confirm switching to DHCP (will reboot)
                pendingNetworkMode = 'dhcp';
                updateNetworkModeUI();
                showDHCPConfirmation();
            }).catch(() => {
                // Fallback to previous behavior if status check fails
                pendingNetworkMode = 'dhcp';
                updateNetworkModeUI();
                showDHCPConfirmation();
            });
            return;
        }
        
        // Show DHCP confirmation popup
        function showDHCPConfirmation() {
            const modal = document.getElementById('network-modal');
            document.getElementById('modal-title').textContent = 'Switch to DHCP';
            document.getElementById('modal-message').innerHTML = `
                Are you sure you want to switch to DHCP mode?
                <div class="modal-warning">⚠️ This will reboot the server.</div>
            `;
            document.getElementById('modal-confirm-btn').textContent = 'Apply DHCP';
            modal.classList.add('show');
        }
        
        // User clicks Save & Apply in Manual mode
        // Helper function to validate IP address (proper range check)
        function validateIP(ip) {
            const parts = ip.split('.');
            if (parts.length !== 4) return false;
            return parts.every(part => {
                const num = parseInt(part, 10);
                return !isNaN(num) && num >= 0 && num <= 255 && part === num.toString();
            });
        }
        
        // Helper function to safely escape HTML
        
        function formatCallDuration(startMs) {
            try {
                if (!startMs) return '00:00';
                const now = Date.now();
                const diff = Math.max(0, now - Number(startMs));
                const totalSec = Math.floor(diff / 1000);
                const mm = String(Math.floor(totalSec / 60)).padStart(2, '0');
                const ss = String(totalSec % 60).padStart(2, '0');
                return `${mm}:${ss}`;
            } catch (_) {
                return '00:00';
            }
        }

        let activeCallTimer = null;
        function startActiveCallTimer() {
            if (activeCallTimer) return;
            activeCallTimer = setInterval(() => {
                try {
                    for (let i = 1; i <= 8; i++) {
                        const line = lines[i];
                        if (!line) continue;
                        if (line.state !== 'connected' || !line.callStart) continue;
                        const el = document.getElementById(`call-time-${i}`);
                        if (el) el.textContent = formatCallDuration(line.callStart);
                    }
                } catch (_) {}
            }, 1000);
        }

        function escapeHTML(str) {
            const div = document.createElement('div');
            div.textContent = str;
            return div.innerHTML;
        }
        
        function requestManualNetworkSave() {
            const ip = document.getElementById('manual-ip').value.trim();
            const subnet = document.getElementById('manual-subnet').value.trim();
            const gateway = document.getElementById('manual-gateway').value.trim();
            
            // Update current config
            currentManualConfig = { ip, subnet, gateway };
            
            // Basic validation
            if (!ip || !subnet || !gateway) {
                showAlertModal({ title: 'Attention', message: 'Please fill in all required fields (IP Address, Subnet Mask, Gateway)' });
                return;
            }
            
            // Proper IP validation with range check (0-255)
            if (!validateIP(ip) || !validateIP(subnet) || !validateIP(gateway)) {
                showAlertModal({ title: 'Attention', message: 'Please enter valid IP addresses (0-255 for each octet)' });
                return;
            }
            
            // If already in manual mode and nothing changed, no need to save
            if (currentNetworkMode === 'manual') {
                if (ip === savedManualConfig.ip && 
                    subnet === savedManualConfig.subnet && 
                    gateway === savedManualConfig.gateway) {
                    showAlertModal({ title: 'Attention', message: 'No changes detected' });
                    return;
                }
            }
            
            // Show confirmation popup
            showManualConfirmation();
        }
        
        // Show Manual configuration confirmation popup
        function showManualConfirmation() {
            const modal = document.getElementById('network-modal');
            const ip = document.getElementById('manual-ip').value.trim();
            const subnet = document.getElementById('manual-subnet').value.trim();
            const gateway = document.getElementById('manual-gateway').value.trim();
            
            document.getElementById('modal-title').textContent = 'Save Static IP Configuration';
            // FIX: Use escapeHTML to prevent XSS
            document.getElementById('modal-message').innerHTML = `
                Apply the following static IP settings?<br><br>
                <strong>IP Address:</strong> ${escapeHTML(ip)}<br>
                <strong>Subnet Mask:</strong> ${escapeHTML(subnet)}<br>
                <strong>Gateway:</strong> ${escapeHTML(gateway)}
                <div class="modal-warning">⚠️ This will reboot the server.</div>
            `;
            document.getElementById('modal-confirm-btn').textContent = 'Save & Reboot';
            modal.classList.add('show');
            
            pendingNetworkMode = 'manual';
        }
        
        // User clicks Cancel in popup
        function cancelNetworkChange() {
            const modal = document.getElementById('network-modal');
            modal.classList.remove('show');
            
            // Revert switch to current mode
            updateNetworkModeUI();
            pendingNetworkMode = null;
        }
        
        // User clicks Confirm/Save in popup
        async function confirmNetworkChange() {
            const modal = document.getElementById('network-modal');
            modal.classList.remove('show');
            
            if (pendingNetworkMode === 'dhcp') {
                // Apply DHCP
                await applyDHCP();
            } else if (pendingNetworkMode === 'manual') {
                // Apply Manual IP
                await applyManualIP();
            }
            
            pendingNetworkMode = null;
        }
        
        // Apply DHCP to backend
        async function applyDHCP() {
            try {
                const response = await apiFetch('/api/network/set-dhcp', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'}
                });
                
                if (response.ok) {
                    console.log('DHCP applied successfully - server will reboot');
                    currentNetworkMode = 'dhcp';
                    updateNetworkModeUI();
                    
                    // Show reboot message
                    showAlertModal({ title: 'Attention', message: 'DHCP mode applied. Server is rebooting...' });
                }
            } catch (error) {
                console.error('Failed to apply DHCP:', error);
                showAlertModal({ title: 'Attention', message: 'Failed to apply DHCP settings' });
                updateNetworkModeUI();
            }
        }
        
        // Apply Manual IP to backend
        async function applyManualIP() {
            const ip = document.getElementById('manual-ip').value.trim();
            const subnet = document.getElementById('manual-subnet').value.trim();
            const gateway = document.getElementById('manual-gateway').value.trim();
            
            try {
                const response = await apiFetch('/api/network/set-static', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        ip_address: ip,
                        subnet_mask: subnet,
                        gateway: gateway
                    })
                });
                
                if (response.ok) {
                    console.log('Static IP applied successfully - server will reboot');
                    currentNetworkMode = 'manual';
                    
                    // Save the config that was applied
                    savedManualConfig = { ip, subnet, gateway };
                    currentManualConfig = { ...savedManualConfig };
                    
                    // Show reboot message
                    showAlertModal({ title: 'Attention', message: 'Static IP configuration saved. Server is rebooting...' });
                }
            } catch (error) {
                console.error('Failed to apply static IP:', error);
                showAlertModal({ title: 'Attention', message: 'Failed to apply static IP settings' });
            }
        }
        
        // ===============================
        // System Reboot
        // ===============================
        async function requestSystemReboot() {
            const confirmed = await showConfirmModal({
                title: '⚠️ Reboot System',
                message: 'Are you sure you want to reboot the system?<br><br><strong style="color: #ef4444;">Warning:</strong> All active calls will be disconnected and the system will be unavailable for about 30 seconds.',
                okText: 'Reboot Now',
                cancelText: 'Cancel'
            });
            
            if (!confirmed) return;
            
            // Show rebooting message immediately
            showAlertModal({ 
                title: '🔄 System Rebooting', 
                message: 'The system is rebooting now. The connection will be lost momentarily.<br><br>Please wait about 30 seconds, then refresh the page to reconnect.' 
            });
            
            // Send reboot command (connection will be lost, that's expected)
            try {
                await apiFetch('/api/system/reboot', {
                    method: 'POST'
                });
            } catch (error) {
                // Expected: connection will drop during reboot
                console.log('Reboot initiated, connection lost (expected behavior)');
            }
        }
        
        // ===============================
        // Audio test
        // ===============================
        // Test Channel Dropdown Functions
        let selectedTestChannel = 0;
        
        function toggleTestDropdown() {
            const dropdown = document.getElementById('test-channel-dropdown');
            const isOpen = dropdown.classList.contains('open');
            
            // Close all dropdowns first
            document.querySelectorAll('.custom-dropdown').forEach(dd => {
                dd.classList.remove('open');
            });
            
            // Toggle this one if it wasn't open
            if (!isOpen) {
                dropdown.classList.add('open');
            }
        }
        
        function selectTestChannel(channel) {
            selectedTestChannel = channel;
            const selectedSpan = document.getElementById('test-channel-selected');
            
            if (channel === 0) {
                selectedSpan.textContent = 'Select Channel...';
            } else {
                selectedSpan.textContent = `Channel ${channel}`;
            }
            
            // Close dropdown
            document.getElementById('test-channel-dropdown').classList.remove('open');
        }
        
        // Audio Test Functions
        async function startTest() {
            const channel = selectedTestChannel;
            const testBtn = document.getElementById('hold-test-btn');
            
            if (channel === 0) {
                console.log('No channel selected for test');
                return;
            }
            
            // Turn button green
            testBtn.classList.add('testing');
            
            console.log('Start audio test on channel', channel);
            try {
                await apiFetch('/api/audio/test/start', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({channel: parseInt(channel)})
                });
            } catch (error) {
                console.error('Failed to start audio test:', error);
            }
        }
        
        async function stopTest() {
            const testBtn = document.getElementById('hold-test-btn');
            
            // Turn button back to cyan
            testBtn.classList.remove('testing');
            
            console.log('Stop audio test');
            try {
                await apiFetch('/api/audio/test/stop', {
                    method: 'POST'
                });
            } catch (error) {
                console.error('Failed to stop audio test:', error);
            }
        }
        
        // Initialize on load - CONSOLIDATED
        document.addEventListener('DOMContentLoaded', function() {
            // Initialize the application
            init();
            
            // Fetch device info (serial number)
            fetch('/api/device/info')
                .then(res => res.json())
                .then(data => {
                    const serialEl = document.getElementById('device-serial');
                    if (serialEl && data.serial) {
                        serialEl.textContent = data.serial;
                    }
                })
                .catch(err => console.error('Failed to load device info:', err));
            
            // Fetch and update Pi status
            function updatePiStatus() {
                fetch('/api/system/pi-status')
                    .then(res => res.json())
                    .then(data => {
                        const tempEl = document.getElementById('pi-temperature');
                        const cpuEl = document.getElementById('pi-cpu');
                        const memEl = document.getElementById('pi-memory');
                        const uptimeEl = document.getElementById('pi-uptime');
                        
                        if (tempEl && data.temperature) tempEl.textContent = data.temperature;
                        if (cpuEl && data.cpu_usage) cpuEl.textContent = data.cpu_usage;
                        if (memEl && data.memory_used && data.memory_total) {
                            memEl.textContent = `${data.memory_used} / ${data.memory_total}`;
                        }
                        if (uptimeEl && data.uptime) uptimeEl.textContent = data.uptime;
                    })
                    .catch(err => console.error('Failed to load Pi status:', err));
            }
            
            // Update Pi status immediately and then every 5 seconds
            updatePiStatus();
            setInterval(updatePiStatus, 5000);

            // ── Phone lines monitor ──────────────────────────────────
            (function initPhoneMonitor() {
                const grid = document.getElementById('pm-grid');
                const countEl = document.getElementById('pm-connected');
                if (!grid) return;

                // Build 20 cells once
                for (let i = 0; i < 20; i++) {
                    const cell = document.createElement('div');
                    cell.className = 'pm-cell';
                    cell.textContent = 9 + i;
                    cell.id = 'pm-' + (9 + i);
                    grid.appendChild(cell);
                }

                function refreshPhoneMonitor() {
                    fetch('/api/phone/lines')
                        .then(r => r.ok ? r.json() : null)
                        .then(data => {
                            if (!data || !data.lines) return;
                            countEl.textContent = data.connected;
                            data.lines.forEach(l => {
                                const cell = document.getElementById('pm-' + l.line_id);
                                if (!cell) return;
                                cell.className = 'pm-cell';
                                if (l.call_state === 'incall')       cell.classList.add('pm-incall');
                                else if (l.call_state === 'dialing') cell.classList.add('pm-dialing');
                                else if (l.connected)                cell.classList.add('pm-connected');
                            });
                        })
                        .catch(() => {});
                }

                refreshPhoneMonitor();
                setInterval(refreshPhoneMonitor, 3000);
            })();
            
            // Setup virtual keyboard overlay click handler
            const overlay = document.getElementById('vkeyboard-overlay');
            if (overlay) {
                overlay.addEventListener('click', function(e) {
                    if (e.target === overlay) {
                        closeVirtualKeyboard();
                    }
                });
            }
        });
    
        // ===============================
        // Keypad Phone List (Read-only viewer of Phone Directory)
        // - Uses phoneDirectory (Settings editor) as source of truth.
        // - Selecting a branch loads name + number onto the keypad.
        // ===============================
        let keypadPickerLineId = null;
        let keypadDirectoryTab = 'IFB';
        const keypadOpenGroups = new Set();

        function showPhoneList(lineId) {
            openKeypadPhoneList(lineId);
        }

        async function openKeypadPhoneList(lineId) {
            keypadPickerLineId = parseInt(lineId);
            keypadDirectoryTab = 'IFB';
            keypadOpenGroups.clear();

            const overlay = document.getElementById('keypad-list-overlay');
            const pill = document.getElementById('kp-target-pill');
            if (pill) pill.textContent = `Line ${keypadPickerLineId}`;

            await loadDirectory();

            selectKeypadCategory('IFB', true);

            if (overlay) {
                overlay.style.display = 'flex';
                overlay.setAttribute('aria-hidden', 'false');
            }
            closeKeypadCategoryDropdown();
        }

        function closeKeypadPhoneList() {
            const overlay = document.getElementById('keypad-list-overlay');
            if (overlay) {
                overlay.style.display = 'none';
                overlay.setAttribute('aria-hidden', 'true');
            }
            closeKeypadCategoryDropdown();
        }

        function toggleKeypadCategoryDropdown() {
            const list = document.getElementById('kp-cat-list');
            if (!list) return;
            list.classList.toggle('show');
        }
        function closeKeypadCategoryDropdown() {
            const list = document.getElementById('kp-cat-list');
            if (!list) return;
            list.classList.remove('show');
        }

        function selectKeypadCategory(tab, skipClose = false) {
            if (tab !== 'IFB' && tab !== 'PL') return;
            keypadDirectoryTab = tab;

            const selected = document.getElementById('kp-cat-selected');
            if (selected) selected.textContent = tab;

            const opts = Array.from(document.querySelectorAll('#kp-cat-list .kp-cat-opt'));
            opts.forEach(o => o.classList.toggle('active', (o.textContent || '').trim() === tab));

            renderKeypadPhoneList();
            if (!skipClose) closeKeypadCategoryDropdown();
        }

        function toggleKeypadGroupOpen(groupId) {
            if (!groupId) return;
            if (keypadOpenGroups.has(groupId)) keypadOpenGroups.delete(groupId);
            else keypadOpenGroups.add(groupId);
            renderKeypadPhoneList();
        }

        function renderKeypadPhoneList() {
            const container = document.getElementById('keypad-list-content');
            if (!container) return;

            const groups = phoneDirectory[keypadDirectoryTab] || [];
            if (!Array.isArray(groups) || groups.length === 0) {
                container.innerHTML = `
                    <div style="padding:14px 6px; color: rgba(226,232,240,0.75); font-weight:700;">
                        No contacts in ${escapeHTML(keypadDirectoryTab)} yet. Add them in Settings → Phone Directory.
                    </div>
                `;
                return;
            }

            container.innerHTML = groups.map(g => {
                const groupName = escapeHTML(g.name || 'Unnamed Group');
                const branches = Array.isArray(g.branches) ? g.branches : [];
                const count = branches.length;
                const metaText = `${count} contact${count === 1 ? '' : 's'}`;
                const isOpen = keypadOpenGroups.has(g.id);

                const branchesHtml = branches.map(b => {
                    const bName = escapeHTML(b.name || '');
                    const bNumber = escapeHTML(b.number || '');
                    return `
                        <div class="dir-branch kp-contact-item" data-contact-name="${escapeHTML(b.name || '')}" data-contact-number="${escapeHTML(b.number || '')}">
                            <div class="dir-branch-main">
                                <div class="dir-branch-name">${bName}</div>
                                <div class="dir-branch-number">${bNumber}</div>
                            </div>
                        </div>
                    `;
                }).join('');

                return `
                    <div class="dir-group" id="kp-group-${g.id}">
                        <div class="dir-group-header" onclick="toggleKeypadGroupOpen('${g.id}')">
                            <div class="dir-group-title">
                                <div class="dir-group-name">${groupName}</div>
                                <div class="dir-group-meta">${metaText}</div>
                            </div>
                            <div class="dir-chip">${escapeHTML(keypadDirectoryTab)}</div>
                            <div class="dir-caret" style="transform:${isOpen ? 'rotate(180deg)' : 'rotate(0deg)'}">▾</div>
                        </div>
                        <div class="dir-group-body" style="display:${isOpen ? 'block' : 'none'}">
                            ${branchesHtml}
                        </div>
                    </div>
                `;
            }).join('');

            // Delegated click handlers for contact items (avoids inline JS injection)
            container.querySelectorAll('.kp-contact-item').forEach(el => {
                el.addEventListener('click', () => {
                    selectKeypadContact(el.dataset.contactName, el.dataset.contactNumber);
                });
            });
        }

        function selectKeypadContact(name, number) {
            const lineId = parseInt(keypadPickerLineId);
            if (!lines[lineId]) return;

            lines[lineId].number = String(number || '').trim();
            lines[lineId].contactName = String(name || '').trim();
            renderLine(lineId);
            closeKeypadPhoneList();
        }

        // Sync if admin edits directory while modal is open
        window.addEventListener('directory-updated', () => {
            const overlay = document.getElementById('keypad-list-overlay');
            if (overlay && overlay.style.display !== 'none') {
                loadDirectory();
                renderKeypadPhoneList();
            }
        });

        // Close modal on outside tap
        document.addEventListener('pointerdown', (e) => {
            const overlay = document.getElementById('keypad-list-overlay');
            if (!overlay || overlay.style.display === 'none') return;
            const modal = overlay.querySelector('.keypad-list-modal');
            if (modal && !modal.contains(e.target)) closeKeypadPhoneList();
        }, { passive: true });

        // Close dropdown on outside tap
        document.addEventListener('pointerdown', (e) => {
            const list = document.getElementById('kp-cat-list');
            const btn = document.querySelector('.kp-cat-btn');
            if (!list || !btn) return;
            if (!list.classList.contains('show')) return;
            if (btn.contains(e.target) || list.contains(e.target)) return;
            closeKeypadCategoryDropdown();
        }, { passive: true });



        // Prevent context menu on long-press (touch screen) and right-click
        document.addEventListener('contextmenu', function(e) {
            e.preventDefault();
        }, false);

        // Prevent keyboard zoom shortcuts (Ctrl+Plus, Ctrl+Minus, Ctrl+0)
        document.addEventListener('keydown', function(e) {
            // Prevent Ctrl+Plus (zoom in)
            if ((e.ctrlKey || e.metaKey) && (e.key === '+' || e.key === '=' || e.keyCode === 187 || e.keyCode === 61)) {
                e.preventDefault();
                return false;
            }
            // Prevent Ctrl+Minus (zoom out)
            if ((e.ctrlKey || e.metaKey) && (e.key === '-' || e.keyCode === 189 || e.keyCode === 173)) {
                e.preventDefault();
                return false;
            }
            // Prevent Ctrl+0 (reset zoom)
            if ((e.ctrlKey || e.metaKey) && (e.key === '0' || e.keyCode === 48)) {
                e.preventDefault();
                return false;
            }
        }, false);

        // Prevent mouse wheel zoom (Ctrl+Wheel)
        document.addEventListener('wheel', function(e) {
            if (e.ctrlKey || e.metaKey) {
                e.preventDefault();
                return false;
            }
        }, { passive: false });

        // ===============================
        // Audio Mixer Functions
        // ===============================
        function toggleMixerDropdown() {
            const dropdown = document.getElementById('mixer-dropdown');
            const arrow = document.getElementById('mixer-arrow');
            
            if (!dropdown || !arrow) return;
            
            if (dropdown.style.display === 'none') {
                dropdown.style.display = 'block';
                arrow.classList.add('open');
                // Load current mixer volumes from backend
                loadMixerVolumes();
            } else {
                dropdown.style.display = 'none';
                arrow.classList.remove('open');
            }
        }

        async function loadMixerVolumes() {
            try {
                const response = await apiFetch('/api/audio/mixer');
                if (!response.ok) {
                    console.warn('Failed to load mixer volumes');
                    return;
                }
                const data = await response.json();
                
                // data format: { "1": {"input": 85, "output": 85}, "2": {...}, ... }
                for (let ch = 1; ch <= 4; ch++) {
                    const chData = data[ch.toString()];
                    if (!chData) continue;
                    
                    // Update input slider and value
                    const inputSlider = document.getElementById(`mixer-input-${ch}`);
                    const inputVal = document.getElementById(`mixer-input-val-${ch}`);
                    if (inputSlider && chData.input !== undefined) {
                        inputSlider.value = chData.input;
                        if (inputVal) inputVal.textContent = chData.input;
                    }
                    
                    // Update output slider and value
                    const outputSlider = document.getElementById(`mixer-output-${ch}`);
                    const outputVal = document.getElementById(`mixer-output-val-${ch}`);
                    if (outputSlider && chData.output !== undefined) {
                        outputSlider.value = chData.output;
                        if (outputVal) outputVal.textContent = chData.output;
                    }
                }
            } catch (error) {
                console.error('Failed to load mixer volumes:', error);
            }
        }

        async function onMixerChange(slider) {
            if (!slider) return;
            
            const channel = slider.dataset.channel;
            const type = slider.dataset.type; // 'input' or 'output'
            const value = parseInt(slider.value);
            
            // Update displayed value
            const valSpan = document.getElementById(`mixer-${type}-val-${channel}`);
            if (valSpan) valSpan.textContent = value;
            
            // Send to backend
            await setMixerVolume(channel, type, value);
        }

        async function setMixerVolume(channel, type, value) {
            try {
                const response = await apiFetch('/api/audio/mixer', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        channel: parseInt(channel),
                        type: type,
                        value: parseInt(value)
                    })
                });
                
                if (!response.ok) {
                    console.error('Failed to set mixer volume');
                }
            } catch (error) {
                console.error('Failed to set mixer volume:', error);
            }
        }
