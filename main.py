"""
ProComm Phone System - GUI Launcher
Embeds the web interface in a native Qt window.
Starts app.py automatically if the Flask server is not already running.
"""

import os
import sys
import time
import signal
import subprocess
import threading
import json

# Project root (directory containing main.py and app.py)
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

# Subprocess for app.py when we start it (so we can kill it on exit)
_app_process = None

# Watchdog state — set True during intentional shutdown so watchdog stops
_shutting_down = False
# Crash-restart throttle: if app crashes too many times in a short window,
# pause restart attempts so we don't loop forever on a broken install.
_restart_history = []  # list of timestamps of recent restarts
_MAX_RESTARTS_PER_WINDOW = 5      # max restarts in window before pause
_RESTART_WINDOW_SEC = 120         # 2-minute window
_RESTART_BACKOFF_SEC = 30         # pause between restarts after crash


def flask_is_ready():
    """Return True if Flask server responds."""
    import urllib.request
    try:
        urllib.request.urlopen('http://localhost:5000/api/system/status', timeout=1)
        return True
    except Exception:
        return False


def start_flask_server():
    """Start app.py in a subprocess. Returns True if process was started."""
    global _app_process
    app_py = os.path.join(PROJECT_ROOT, 'app.py')
    if not os.path.exists(app_py):
        print(f"ERROR: app.py not found at {app_py}")
        return False
    # Prefer venv if present
    venv_python = os.path.join(PROJECT_ROOT, 'venv_with_system', 'bin', 'python')
    if not os.path.exists(venv_python):
        venv_python = os.path.join(PROJECT_ROOT, 'venv', 'bin', 'python')
    python_exe = venv_python if os.path.exists(venv_python) else sys.executable
    # Use writable log file if possible; otherwise discard (e.g. when run as procomm, app.log may be root-owned)
    log_path = os.path.join(PROJECT_ROOT, 'app.log')
    try:
        log_file = open(log_path, 'a')
        log_file.write(f"\n--- Started by main.py at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        log_file.flush()
        out_err = log_file
    except (PermissionError, OSError):
        try:
            log_path = os.path.join(os.environ.get('TMPDIR', '/tmp'), 'procomm_app.log')
            log_file = open(log_path, 'a')
            log_file.write(f"\n--- Started by main.py at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            log_file.flush()
            out_err = log_file
        except (PermissionError, OSError):
            out_err = subprocess.DEVNULL
            log_path = '(none)'
    print(f"Starting Flask server: {python_exe} app.py (logs: {log_path})")
    _app_process = subprocess.Popen(
        [python_exe, 'app.py'],
        cwd=PROJECT_ROOT,
        stdout=out_err,
        stderr=subprocess.STDOUT if out_err != subprocess.DEVNULL else subprocess.DEVNULL,
        env=dict(os.environ),
    )
    return True


def wait_for_flask(timeout=30, start_if_missing=True):
    """Wait for Flask server to be ready. If start_if_missing, start app.py when not running."""
    if flask_is_ready():
        print("Flask server is already running.")
        return True

    # Check if app.py process is already running (might be starting up)
    try:
        result = subprocess.run(['pgrep', '-f', 'python.*app.py'],
                              capture_output=True, text=True, timeout=1)
        if result.returncode == 0:
            print("Flask server process detected, waiting for it to be ready...")
            # Poll until ready or timeout (Flask can take several seconds after process start)
            wait_start = time.time()
            while time.time() - wait_start < 15:
                if flask_is_ready():
                    print("Flask server is ready!")
                    return True
                time.sleep(0.5)
    except Exception:
        pass  # pgrep not available or failed, continue

    if start_if_missing:
        start_flask_server()
        time.sleep(1)

    print("Waiting for Flask server at http://localhost:5000 ...")
    start_time = time.time()
    while time.time() - start_time < timeout:
        if flask_is_ready():
            print("Flask server is ready!")
            return True
        time.sleep(0.5)

    print("\nERROR: Flask server not responding!")
    log_path = os.path.join(PROJECT_ROOT, 'app.log')
    if os.path.exists(log_path):
        with open(log_path, 'r') as f:
            tail = f.read()
        if 'Traceback' in tail or 'Error' in tail:
            print("Check app.log for errors. Common fix: install dependencies:")
            print(f"  cd {PROJECT_ROOT}")
            print("  python3 -m venv venv && source venv/bin/activate")
            print("  pip install -r requirements.txt")
            print("  python app.py")
    print("\nOr start the server manually in another terminal:")
    print(f"  cd {PROJECT_ROOT}")
    print("  python app.py   # or: ./start_app.sh")
    return False


class _ReloadSignal:
    """Carries a pyqtSignal so the background thread can trigger view.reload() thread-safely."""
    _instance = None

    @staticmethod
    def create():
        from PyQt5.QtCore import QObject, pyqtSignal
        class _Sig(QObject):
            reload = pyqtSignal()
        sig = _Sig()
        _ReloadSignal._instance = sig
        return sig


def _version_watch_loop(sig):
    """Background thread: reload the WebView whenever app.py restarts (version changes)."""
    import urllib.request
    known_version = None
    while not _shutting_down:
        time.sleep(5)
        if _shutting_down:
            break
        try:
            resp = urllib.request.urlopen(
                'http://localhost:5000/api/system/status', timeout=2)
            data = json.loads(resp.read())
            ver = data.get('app_version')
            if ver is None:
                continue
            if known_version is None:
                known_version = ver
                continue
            if ver != known_version:
                known_version = ver
                sys.stdout.write(f"[version-watch] app.py restarted (v{ver}), reloading WebView\n")
                sys.stdout.flush()
                sig.reload.emit()
        except Exception:
            pass


def start_gui():
    """Start the PyQt5 GUI"""
    from PyQt5.QtWidgets import QApplication
    from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEngineSettings, QWebEngineProfile
    from PyQt5.QtCore import QUrl, Qt
    import shutil

    # Disable Qt scaling — we control zoom via CSS viewport
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, False)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, False)
    os.environ['QT_AUTO_SCREEN_SCALE_FACTOR'] = '0'
    os.environ['QT_SCALE_FACTOR'] = '1'

    app = QApplication(sys.argv)
    app.setApplicationName("ProComm Phone System")

    # Create web view
    view = QWebEngineView()

    # Configure settings
    settings = view.settings()
    settings.setAttribute(QWebEngineSettings.LocalStorageEnabled, True)
    settings.setAttribute(QWebEngineSettings.JavascriptEnabled, True)
    settings.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
    settings.setAttribute(QWebEngineSettings.ErrorPageEnabled, True)
    settings.setAttribute(QWebEngineSettings.ShowScrollBars, False)

    # Disable HTTP cache and wipe any stale disk cache so CSS/JS always loads fresh
    profile = QWebEngineProfile.defaultProfile()
    profile.setHttpCacheType(QWebEngineProfile.NoCache)
    try:
        cache_path = profile.cachePath()
        if cache_path and os.path.exists(cache_path):
            shutil.rmtree(cache_path, ignore_errors=True)
            sys.stdout.write(f"Cleared WebEngine disk cache: {cache_path}\n")
        else:
            sys.stdout.write(f"WebEngine cache path: '{cache_path}' (nothing to clear)\n")
        sys.stdout.flush()
    except Exception as e:
        sys.stdout.write(f"Cache clear warning: {e}\n")
        sys.stdout.flush()

    # Watch for app.py restarts and reload the page automatically (thread-safe signal)
    sig = _ReloadSignal.create()
    sig.reload.connect(view.reload)
    watcher = threading.Thread(
        target=_version_watch_loop, args=(sig,), daemon=True, name="version-watch")
    watcher.start()

    def _inject_touchscreen_css(ok):
        if not ok:
            return
        # PyQt5-only overrides — web UI is unaffected.
        # 1. Constrain qr-card/sip-status-card to 800px centered (match web UI layout).
        # 2. Fix phone-monitor grid so cells are square (older WebEngine ignores aspect-ratio:1).
        css = (
            ".settings-section.settings-card.qr-card,"
            ".settings-section.settings-card.sip-status-card {"
            "  max-width: 800px !important;"
            "  width:     800px !important;"
            "  margin-left: auto !important;"
            "  margin-right: auto !important;"
            "}"
            ".qr-slot-left {"
            "  max-width: 520px !important;"
            "  flex: 3 !important;"
            "}"
            # Disable aspect-ratio so our JS-set grid-auto-rows is not fought by CSS
            ".pm-cell { aspect-ratio: auto !important; }"
        )
        js = r"""
(function() {
    // Inject layout CSS
    var el = document.getElementById('_ts_overrides');
    if (el) el.remove();
    var s = document.createElement('style');
    s.id = '_ts_overrides';
    s.textContent = CSS_PLACEHOLDER;
    document.head.appendChild(s);

    // Make pm-cells square by setting grid-auto-rows = computed column width.
    // This is more reliable than per-cell height when aspect-ratio:1 is unsupported.
    function makeGridSquare() {
        var grid = document.querySelector('.phone-monitor-grid');
        if (!grid) return;
        var cells = grid.querySelectorAll('.pm-cell');
        if (!cells.length) return;
        var gridW = grid.getBoundingClientRect().width;
        if (gridW <= 0) return;
        var gap = 6, cols = 5;
        var cellW = (gridW - gap * (cols - 1)) / cols;
        grid.style.gridAutoRows = Math.ceil(cellW) + 'px';
    }

    // Run after CSS settles, and again later when cells load
    setTimeout(makeGridSquare, 300);
    setTimeout(makeGridSquare, 800);
    setTimeout(makeGridSquare, 2000);

    // Re-run whenever grid children change (cells added/removed dynamically)
    var grid = document.querySelector('.phone-monitor-grid');
    if (grid) {
        var obs = new MutationObserver(function() { setTimeout(makeGridSquare, 60); });
        obs.observe(grid, { childList: true });
    }
})();
""".replace('CSS_PLACEHOLDER', repr(css))
        view.page().runJavaScript(js)

    view.loadFinished.connect(_inject_touchscreen_css)

    view.setWindowTitle("ProComm Phone System")
    view.load(QUrl("http://localhost:5000"))

    # Scale to match Safari retina rendering (2x display shows ~1.5x larger)
    view.setZoomFactor(1.5)
    view.showFullScreen()

    # Run the Qt event loop
    return app.exec_()




def cleanup(signum=None, frame=None):
    """Cleanup on exit: kill app.py if we started it."""
    global _app_process, _shutting_down
    _shutting_down = True  # tell watchdog to stop trying to restart
    print("\nShutting down GUI...")
    if _app_process is not None and _app_process.poll() is None:
        _app_process.terminate()
        try:
            _app_process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _app_process.kill()
        _app_process = None
    sys.exit(0)


def _watchdog_loop():
    """Background thread: if app.py crashes, restart it automatically.

    Throttled so a broken install can't restart-loop forever.
    Only restarts processes WE started (i.e., _app_process is not None).
    """
    global _app_process, _restart_history
    while not _shutting_down:
        time.sleep(5)  # check every 5 seconds
        if _shutting_down:
            break
        # Only watch the process we started ourselves
        proc = _app_process
        if proc is None:
            continue
        # poll() returns None if still running, exit code if dead
        if proc.poll() is None:
            continue

        exit_code = proc.returncode
        print(f"[watchdog] app.py exited with code {exit_code} — preparing restart")

        # Throttle: drop timestamps outside the window
        now = time.time()
        _restart_history = [t for t in _restart_history if now - t < _RESTART_WINDOW_SEC]
        if len(_restart_history) >= _MAX_RESTARTS_PER_WINDOW:
            print(f"[watchdog] app crashed {_MAX_RESTARTS_PER_WINDOW} times in "
                  f"{_RESTART_WINDOW_SEC}s — pausing restarts for {_RESTART_WINDOW_SEC}s")
            time.sleep(_RESTART_WINDOW_SEC)
            _restart_history = []
            continue

        # Wait a bit for the OS to release the port and any audio devices
        time.sleep(_RESTART_BACKOFF_SEC if _restart_history else 3)
        if _shutting_down:
            break
        _restart_history.append(time.time())
        print("[watchdog] restarting app.py ...")
        _app_process = None  # clear before starting fresh
        if start_flask_server():
            print("[watchdog] app.py relaunched")
        else:
            print("[watchdog] failed to relaunch app.py")


def main():
    """Main entry point"""
    # Handle Ctrl+C gracefully
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    print("=" * 50)
    print("ProComm Phone System - GUI Launcher")
    print("=" * 50)

    # Wait for Flask to be ready
    if not wait_for_flask():
        return 1

    # Start watchdog (auto-restart app.py if it crashes)
    watchdog = threading.Thread(target=_watchdog_loop, daemon=True, name="app-watchdog")
    watchdog.start()
    print("Watchdog started (auto-restart app.py on crash)")

    # Start GUI
    print("\nLaunching GUI...")
    try:
        result = start_gui()
    finally:
        cleanup()

    return result


if __name__ == "__main__":
    main()
