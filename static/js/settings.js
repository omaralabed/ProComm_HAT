/**
 * Settings Page Functionality
 * Handles SIP settings loading and saving for VoIP.ms format
 */

// ── Self-contained modal helpers (settings page has no access to index.js modals) ──

/** Show an alert modal (replaces window.alert) */
function settingsAlert(message, callback) {
    _settingsEnsureModalMarkup();
    const overlay = document.getElementById('settings-modal-overlay');
    const titleEl = document.getElementById('settings-modal-title');
    const msgEl   = document.getElementById('settings-modal-message');
    const okBtn   = document.getElementById('settings-modal-ok');
    const cancelBtn = document.getElementById('settings-modal-cancel');

    titleEl.textContent = 'Notice';
    msgEl.textContent   = message;
    cancelBtn.style.display = 'none';
    overlay.style.display = 'flex';

    const cleanup = () => {
        overlay.style.display = 'none';
        okBtn.removeEventListener('click', onOk);
        document.removeEventListener('keydown', onKey);
    };
    const onOk  = () => { cleanup(); if (callback) callback(); };
    const onKey = (e) => { if (e.key === 'Escape' || e.key === 'Enter') onOk(); };
    okBtn.addEventListener('click', onOk);
    document.addEventListener('keydown', onKey);
    setTimeout(() => okBtn.focus(), 0);
}

/** Show a confirm modal (replaces window.confirm). Calls onYes/onNo callbacks. */
function settingsConfirm(message, onYes, onNo) {
    _settingsEnsureModalMarkup();
    const overlay   = document.getElementById('settings-modal-overlay');
    const titleEl   = document.getElementById('settings-modal-title');
    const msgEl     = document.getElementById('settings-modal-message');
    const okBtn     = document.getElementById('settings-modal-ok');
    const cancelBtn = document.getElementById('settings-modal-cancel');

    titleEl.textContent = 'Confirm';
    msgEl.textContent   = message;
    cancelBtn.style.display = '';
    overlay.style.display = 'flex';

    const cleanup = () => {
        overlay.style.display = 'none';
        okBtn.removeEventListener('click', onOkFn);
        cancelBtn.removeEventListener('click', onCancelFn);
        document.removeEventListener('keydown', onKey);
    };
    const onOkFn     = () => { cleanup(); if (onYes) onYes(); };
    const onCancelFn = () => { cleanup(); if (onNo) onNo(); };
    const onKey = (e) => {
        if (e.key === 'Escape') onCancelFn();
        else if (e.key === 'Enter') onOkFn();
    };
    okBtn.addEventListener('click', onOkFn);
    cancelBtn.addEventListener('click', onCancelFn);
    document.addEventListener('keydown', onKey);
    setTimeout(() => okBtn.focus(), 0);
}

/** Lazily inject modal markup + styles once */
function _settingsEnsureModalMarkup() {
    if (document.getElementById('settings-modal-overlay')) return;

    const style = document.createElement('style');
    style.textContent = `
        #settings-modal-overlay {
            display:none; position:fixed; inset:0; z-index:9999;
            background:rgba(0,0,0,.55); align-items:center; justify-content:center;
        }
        .settings-modal-box {
            background:#1e293b; color:#e2e8f0; border-radius:12px; padding:28px 24px 20px;
            min-width:300px; max-width:420px; box-shadow:0 8px 32px rgba(0,0,0,.5); text-align:center;
        }
        .settings-modal-box h3 { margin:0 0 12px; font-size:1.1rem; }
        .settings-modal-box p  { margin:0 0 20px; font-size:.95rem; line-height:1.4; }
        .settings-modal-btns { display:flex; gap:10px; justify-content:center; }
        .settings-modal-btns button {
            padding:8px 22px; border:none; border-radius:6px; font-size:.95rem; cursor:pointer;
        }
        .settings-modal-ok  { background:#3b82f6; color:#fff; }
        .settings-modal-ok:hover  { background:#2563eb; }
        .settings-modal-cancel { background:#475569; color:#e2e8f0; }
        .settings-modal-cancel:hover { background:#334155; }
    `;
    document.head.appendChild(style);

    const overlay = document.createElement('div');
    overlay.id = 'settings-modal-overlay';
    overlay.innerHTML = `
        <div class="settings-modal-box">
            <h3 id="settings-modal-title">Notice</h3>
            <p id="settings-modal-message"></p>
            <div class="settings-modal-btns">
                <button class="settings-modal-cancel" id="settings-modal-cancel">Cancel</button>
                <button class="settings-modal-ok" id="settings-modal-ok">OK</button>
            </div>
        </div>
    `;
    document.body.appendChild(overlay);

    // Close on overlay click (outside the box)
    overlay.addEventListener('click', (e) => {
        if (e.target === overlay) {
            document.getElementById('settings-modal-cancel')?.click() ||
            document.getElementById('settings-modal-ok')?.click();
        }
    });
}

// ── SIP Configuration ──

// Load SIP configuration
function loadSIPConfig() {
    fetch('/api/config/sip')
        .then(response => response.json())
        .then(config => {
            // Global settings - VoIP.ms format
            document.getElementById('sip-server').value = config.server_ip || '';
            document.getElementById('sip-domain').value = config.domain || '';
            document.getElementById('sip-port').value = config.server_port || '';
            document.getElementById('caller-id').value = config.caller_id || '';
            const outgoingOnlyEl = document.getElementById('outgoing-only');
            if (outgoingOnlyEl) outgoingOnlyEl.checked = config.outgoing_only !== false;
            
            // Account credentials (use first account if available)
            if (config.accounts && config.accounts.length > 0) {
                document.getElementById('account-username').value = config.accounts[0].username || '';
                document.getElementById('account-password').value = config.accounts[0].password || '';
            }
        })
        .catch(error => {
            console.error('Error loading SIP config:', error);
            settingsAlert('Error loading SIP configuration');
        });
}

// Save SIP settings in VoIP.ms format
function saveSIPSettings() {
    // Gather settings
    const serverIp = document.getElementById('sip-server').value.trim();
    const sipDomain = document.getElementById('sip-domain').value.trim();
    const serverPort = parseInt(document.getElementById('sip-port').value) || 5060;
    const callerId = document.getElementById('caller-id').value.trim();
    const username = document.getElementById('account-username').value.trim();
    const password = document.getElementById('account-password').value;
    
    // Validate
    if (!serverIp) {
        settingsAlert('Please enter SIP server');
        return;
    }
    
    if (!sipDomain) {
        settingsAlert('Please enter SIP domain');
        return;
    }
    
    if (!username) {
        settingsAlert('Please enter username');
        return;
    }
    
    if (!password) {
        settingsAlert('Please enter password');
        return;
    }
    
    // Confirm — the rest of the save logic continues inside the onYes callback
    settingsConfirm('Save SIP settings? The SIP engine will reload and use the new settings immediately.', () => {
        _doSaveSIPSettings(serverIp, sipDomain, serverPort, callerId, username, password);
    });
}

/** Actually perform the save (called after user confirms) */
function _doSaveSIPSettings(serverIp, sipDomain, serverPort, callerId, username, password) {
    // Build VoIP.ms format config
    const outgoingOnlyEl = document.getElementById('outgoing-only');
    const outgoingOnly = outgoingOnlyEl ? outgoingOnlyEl.checked : true;
    const config = {
        server_ip: serverIp,
        server_port: serverPort,
        domain: sipDomain,
        sip_port_base: 5100,
        rtp_port_base: 10000,
        caller_id: callerId,
        audio_device: "",
        use_per_line_channels: true,
        outgoing_only: outgoingOnly,
        accounts: []
    };
    
    // Create 8 accounts with same credentials
    for (let i = 0; i < 8; i++) {
        config.accounts.push({
            username: username,
            password: password
        });
    }
    
    // Save
    fetch('/api/config/sip', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify(config)
    })
    .then(response => response.json())
    .then(data => {
        if (data.error) {
            settingsAlert('Error saving: ' + data.error);
        } else {
            settingsAlert(data.message || 'SIP settings saved. SIP engine is using the new settings.', () => {
                window.location.href = '/';
            });
        }
    })
    .catch(error => {
        console.error('Error saving SIP config:', error);
        settingsAlert('Error saving SIP configuration');
    });
}

// Load config when page loads
document.addEventListener('DOMContentLoaded', () => {
    if (document.getElementById('sip-server')) {
        loadSIPConfig();
    }
});
