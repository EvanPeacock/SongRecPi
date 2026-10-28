#!/usr/bin/env python3
"""
SongRec Flask Kiosk App (centered adaptive panel + on-screen WiFi setup)

Features:
- Background thread running `songrec listen --json` (restarts with exponential backoff;
  the backoff resets after a run that lasted more than a minute).
- Fast shutdown: tracks subprocess; terminates on SIGINT/SIGTERM and exits.
- /current JSON endpoint (track info + WiFi setup state).
- /qr/wifi.svg and /qr/portal.svg: offline QR codes shown during WiFi setup.
- Root (/) serves kiosk UI with:
    * Centered translucent panel that grows with content but keeps >=5px margin to edges.
    * Multi-line wrapping with adaptive clamping & album art shrinking to fit vertically.
    * Dynamic background gradient from album art dominant color.
    * Flash animation on track change.
    * "Listening..." idle view after IDLE_TIMEOUT seconds without a new recognition.
    * WiFi setup view (two QR codes + status) while the setup hotspot is running.
    * Hidden cursor.
Environment Variables:
    SONGREC_PATH              (default: ~/.cargo/bin/songrec)
    HOST                      (default: 127.0.0.1)
    PORT                      (default: 5000)
    POLL_INTERVAL             (seconds, float, default 1.0)
    IDLE_TIMEOUT              (seconds, default 600; 0 disables the idle view)
    SONGRECPI_CONF            (default: /etc/songrecpi.conf)
    SETUP_STATE_FILE          (default: /run/songrecpi/setup.json)
"""

import io
import os
import sys
import json
import time
import signal
import logging
import threading
import subprocess
from functools import lru_cache
from typing import Dict, Any, Optional

from flask import Flask, jsonify, Response, abort

# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------
SONGREC_PATH = os.environ.get("SONGREC_PATH", os.path.expanduser("~/.cargo/bin/songrec"))
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "1.0"))
POLL_INTERVAL_MS = int(POLL_INTERVAL * 1000)
IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT", "600"))
CONF_PATH = os.environ.get("SONGRECPI_CONF", "/etc/songrecpi.conf")
SETUP_STATE_FILE = os.environ.get("SETUP_STATE_FILE", "/run/songrecpi/setup.json")
PORTAL_URL = "http://10.42.0.1"

BACKOFF_MIN = 3
BACKOFF_MAX = 60
STABLE_RUN_SECS = 60   # a run longer than this resets the backoff

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("songrec-app")
# The kiosk polls /current every second; don't log each request.
logging.getLogger("werkzeug").setLevel(logging.WARNING)


def load_conf(path: str) -> Dict[str, str]:
    """Read the shared KEY="value" config (also sourced by wifi-portal-start.sh)."""
    conf = {"HOTSPOT_SSID": "SongRecPi", "HOTSPOT_PASSWORD": "songrecpi"}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                conf[key.strip()] = value.strip().strip("\"'")
    except FileNotFoundError:
        pass
    return conf


CONF = load_conf(CONF_PATH)

# --------------------------------------------------------------------------------------
# Shared State
# --------------------------------------------------------------------------------------
current_track: Dict[str, Any] = {
    "title": "-",
    "artist": "-",
    "album_art": "",
    "updated": 0   # unix timestamp last time a track update was applied
}
track_lock = threading.Lock()
stop_event = threading.Event()
proc_ref: Optional[subprocess.Popen] = None   # reference to songrec subprocess

# --------------------------------------------------------------------------------------
# SongRec Worker
# --------------------------------------------------------------------------------------
def handle_line(raw_line: str):
    line = raw_line.strip()
    if not line or not line.startswith("{"):
        return
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return
    track = data.get("track") if isinstance(data, dict) else None
    if not isinstance(track, dict):
        return
    title = track.get("title") or "-"
    artist = track.get("subtitle") or "-"
    images = track.get("images") or {}
    album_art = images.get("coverart") or images.get("coverarthq") or ""

    with track_lock:
        changed = (
            title != current_track["title"] or
            artist != current_track["artist"] or
            album_art != current_track["album_art"]
        )
        current_track.update({
            "title": title,
            "artist": artist,
            "album_art": album_art,
            "updated": int(time.time())
        })

    if changed:
        log.info("Track: %s - %s", artist, title)


def terminate_songrec():
    proc = proc_ref
    if proc and proc.poll() is None:
        log.info("Terminating songrec subprocess...")
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            log.warning("songrec did not terminate; killing.")
            proc.kill()


def run_songrec_once():
    """Run songrec until it exits (or we're stopping), feeding its output to handle_line."""
    global proc_ref
    if not os.path.isfile(SONGREC_PATH):
        log.error("songrec not found at %s", SONGREC_PATH)
        return

    cmd = [SONGREC_PATH, "listen", "--json", "--disable-mpris"]
    if os.path.exists("/usr/bin/stdbuf"):
        cmd = ["/usr/bin/stdbuf", "-oL"] + cmd
    log.info("Starting songrec: %s", " ".join(cmd))
    proc_ref = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        bufsize=1
    )
    if proc_ref.stdout is None:
        log.error("songrec started without a stdout pipe; aborting this run.")
        terminate_songrec()
        return

    for raw_line in proc_ref.stdout:
        if stop_event.is_set():
            break
        try:
            handle_line(raw_line)
        except Exception:
            log.exception("Failed to handle songrec output line: %r", raw_line[:200])

    if stop_event.is_set():
        return
    rc = proc_ref.wait()
    log.warning("songrec exited (code=%s)", rc)


def songrec_worker():
    backoff = BACKOFF_MIN
    if stop_event.wait(2):  # allow audio stack to settle
        return

    while not stop_event.is_set():
        started = time.monotonic()
        try:
            run_songrec_once()
        except Exception:
            log.exception("songrec run failed")
        if stop_event.is_set():
            break
        if time.monotonic() - started > STABLE_RUN_SECS:
            backoff = BACKOFF_MIN
        log.warning("Restarting songrec in %ss", backoff)
        if stop_event.wait(backoff):
            break
        backoff = min(backoff * 2, BACKOFF_MAX)

    terminate_songrec()
    log.info("songrec_worker exiting.")

# --------------------------------------------------------------------------------------
# WiFi setup state + QR codes
# --------------------------------------------------------------------------------------
def read_setup_state() -> Optional[Dict[str, Any]]:
    """Written by the WiFi portal at boot ("checking", then "setup"); absent otherwise."""
    try:
        with open(SETUP_STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, PermissionError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def wifi_qr_escape(value: str) -> str:
    for ch in ('\\', ';', ',', ':', '"'):
        value = value.replace(ch, '\\' + ch)
    return value


@lru_cache(maxsize=4)
def qr_svg(payload: str) -> Optional[bytes]:
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:
        log.warning("python3-qrcode not installed; QR codes unavailable.")
        return None
    img = qrcode.make(payload, image_factory=qrcode.image.svg.SvgPathFillImage, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue()

# --------------------------------------------------------------------------------------
# Flask App / HTML
# --------------------------------------------------------------------------------------
app = Flask(__name__)

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<title>SongRec Pi</title>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,user-scalable=no">

<style>
:root {
    --panel-bg: rgba(20,20,20,0.55);
    --fallback-bg-1: #202020;
    --fallback-bg-2: #111111;
    --art-size: 180px;
}

html,body {
    margin:0;
    padding:0;
    width:100vw;
    height:100vh;
    overflow:hidden;
    font-family:Arial,sans-serif;
    background:linear-gradient(135deg,var(--fallback-bg-1),var(--fallback-bg-2));
    transition:background 0.9s ease;
}

body {
    display:flex;
    align-items:center;
    justify-content:center;
    box-sizing:border-box;
    padding:5px;
}

#panel {
    display:flex;
    flex-direction:row;
    align-items:center;
    gap:18px;
    padding:16px 18px;
    background:var(--panel-bg);
    border-radius:14px;
    backdrop-filter: blur(4px);
    -webkit-backdrop-filter: blur(4px);
    box-sizing:border-box;
    max-width:calc(100vw - 10px);
    max-height:calc(100vh - 10px);
    overflow:visible;
    position:relative;
    transition:opacity 1.5s ease;
}

#albumArt {
    width: calc(min(var(--art-size), 40vmin, 260px));
    aspect-ratio: 1 / 1;
    background:#333 center/cover no-repeat;
    border-radius:12px;
    box-shadow:0 4px 14px rgba(0,0,0,0.5);
    flex-shrink:0;
    transition:box-shadow .4s ease, transform .4s ease, width .25s ease;
}

.track-info {
    display:flex;
    flex-direction:column;
    justify-content:center;
    min-width:0;
}

.track-title, .track-artist {
    margin:0;
    color:#fff;
    word-break:break-word;
    white-space:normal;
    overflow:hidden;
    display:-webkit-box;
    -webkit-box-orient:vertical;
}

.track-title {
    font-size:2rem;
    font-weight:600;
    line-height:1.18;
}
.track-title.clamp1 { -webkit-line-clamp:1; }
.track-title.clamp2 { -webkit-line-clamp:2; }
.track-title.clamp3 { -webkit-line-clamp:3; }
.track-title.clamp4 { -webkit-line-clamp:4; }

.track-artist {
    font-size:1.05rem;
    line-height:1.2;
    margin-top:8px;
}
.track-artist.clamp1 { -webkit-line-clamp:1; }
.track-artist.clamp2 { -webkit-line-clamp:2; }

.flash {
    animation:flash-bg 0.6s ease;
}
@keyframes flash-bg {
    0%   { background-color:rgba(255,255,255,0.12); }
    100% { background-color:var(--panel-bg); }
}

/* Idle: fade the song panel out and show a quiet "Listening..." label */
#idle {
    position:fixed;
    inset:0;
    display:flex;
    align-items:center;
    justify-content:center;
    color:rgba(255,255,255,0.55);
    font-size:1.6rem;
    letter-spacing:0.04em;
    opacity:0;
    pointer-events:none;
    transition:opacity 1.5s ease;
}
body.idle #panel { opacity:0; }
body.idle #idle { opacity:1; animation:breathe 4s ease-in-out infinite; }
@keyframes breathe {
    0%,100% { color:rgba(255,255,255,0.35); }
    50%     { color:rgba(255,255,255,0.7); }
}

/* WiFi setup view, sized for a 480x320 panel */
#setup {
    display:none;
    flex-direction:column;
    align-items:center;
    justify-content:center;
    gap:2vh;
    width:100%;
    height:100%;
    color:#fff;
    text-align:center;
}
body.setup #panel, body.setup #idle { display:none; }
body.setup #setup { display:flex; }
#setup h1 { margin:0; font-size:1.25rem; font-weight:600; }
.qr-row { display:flex; gap:16vw; justify-content:center; }
.qr-cell { display:flex; flex-direction:column; align-items:center; gap:1vh; }
.qr-cell img {
    width:min(30vw, 52vh);
    height:min(30vw, 52vh);
    background:#fff;
    border-radius:8px;
    image-rendering:pixelated;
}
.qr-cell .cap { font-size:0.95rem; font-weight:600; }
.qr-cell .sub { font-size:0.75rem; color:rgba(255,255,255,0.7); }
#setupStatus { font-size:0.95rem; min-height:1.2em; color:#ffd479; }
#setupStatus.ok { color:#9be89b; }

html, body, body * {
    cursor: none !important;
}

* {
    -webkit-user-select:none;
    user-select:none;
}
</style>
</head>
<body class="idle">
  <div id="panel">
      <div id="albumArt"></div>
      <div class="track-info">
          <div id="trackTitle" class="track-title clamp4"></div>
          <div id="trackArtist" class="track-artist clamp2"></div>
      </div>
  </div>
  <div id="idle">Listening&hellip;</div>
  <div id="setup">
      <h1>Wi-Fi setup</h1>
      <div class="qr-row">
          <div class="qr-cell">
              <img src="/qr/wifi.svg" alt="">
              <div class="cap">1. Scan to join</div>
              <div class="sub" id="hotspotInfo">__HOTSPOT_INFO__</div>
          </div>
          <div class="qr-cell">
              <img src="/qr/portal.svg" alt="">
              <div class="cap">2. Scan to set up</div>
              <div class="sub">__PORTAL_URL__</div>
          </div>
      </div>
      <div id="setupStatus"></div>
  </div>

<script>
const POLL_INTERVAL = __POLL_INTERVAL_MS__;
const IDLE_TIMEOUT = __IDLE_TIMEOUT__;
const FETCH_TIMEOUT = 4000;
let lastId = "";
let lastArtURL = "";
let colorToken = 0;

async function fetchTrack() {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT);
    try {
        const res = await fetch('/current?ts=' + Date.now(), {signal: ctrl.signal, cache: 'no-store'});
        if (res.ok) render(await res.json());
    } catch(e) {
        console.warn('fetch /current failed:', e);
    } finally {
        clearTimeout(timer);
        setTimeout(fetchTrack, POLL_INTERVAL);
    }
}

function render(data) {
    if (renderSetup(data.setup)) return;

    // While the boot script waits for a network, say so instead of "Listening..."
    const checking = !!(data.setup && data.setup.mode === 'checking');
    document.getElementById('idle').textContent = checking ? 'Connecting to Wi-Fi…' : 'Listening…';
    const idle = checking || (IDLE_TIMEOUT > 0 && (!data.updated || (data.now - data.updated) > IDLE_TIMEOUT));
    document.body.classList.toggle('idle', idle);

    const title = data.title || '-';
    const artist = data.artist || '-';
    const art = data.album_art || '';
    const newId = title + '|' + artist + '|' + art;
    if (newId === lastId) return;

    const panel = document.getElementById('panel');
    panel.classList.remove('flash');
    void panel.offsetWidth;
    panel.classList.add('flash');

    document.getElementById('trackTitle').textContent = title;
    document.getElementById('trackArtist').textContent = artist;

    const artDiv = document.getElementById('albumArt');
    if (art) {
        artDiv.style.backgroundImage = 'url("' + art.replace(/["\\\n]/g, '\\$&') + '")';
        if (art !== lastArtURL) extractAndApplyDominantColor(art);
    } else {
        artDiv.style.backgroundImage = 'none';
        colorToken++;                       // drop any in-flight color extraction
        document.body.style.background = '';  // back to the CSS fallback gradient
    }
    lastArtURL = art;

    resetLayoutAllowances();
    fitPanel();
    lastId = newId;
}

const SETUP_MESSAGES = {
    waiting:    '',
    connecting: 'Connecting to ',
    failed:     'Couldn’t connect — check the password and try again',
    connected:  'Connected!'
};

function renderSetup(setup) {
    const active = !!(setup && setup.mode === 'setup');
    document.body.classList.toggle('setup', active);
    if (!active) return false;
    const status = document.getElementById('setupStatus');
    let msg = SETUP_MESSAGES[setup.status] || '';
    if (setup.status === 'connecting') msg += (setup.ssid || 'network') + '…';
    status.textContent = msg;
    status.classList.toggle('ok', setup.status === 'connected');
    return true;
}

function resetLayoutAllowances() {
    const titleEl = document.getElementById('trackTitle');
    const artistEl = document.getElementById('trackArtist');
    titleEl.className = 'track-title clamp4';
    artistEl.className = 'track-artist clamp2';
    setArtSize(180);
}

function fitPanel() {
    const panel = document.getElementById('panel');
    const titleEl = document.getElementById('trackTitle');
    const artistEl = document.getElementById('trackArtist');
    const maxH = window.innerHeight - 10;
    let safety = 30;

    while (panel.scrollHeight > maxH && safety-- > 0) {
        if (titleEl.classList.contains('clamp4')) { titleEl.classList.replace('clamp4','clamp3'); continue; }
        if (titleEl.classList.contains('clamp3')) { titleEl.classList.replace('clamp3','clamp2'); continue; }
        if (titleEl.classList.contains('clamp2')) { titleEl.classList.replace('clamp2','clamp1'); continue; }
        if (artistEl.classList.contains('clamp2')) { artistEl.classList.replace('clamp2','clamp1'); continue; }
        const currentArt = getArtSize();
        if (currentArt > 125) { setArtSize(Math.max(125, currentArt - 15)); continue; }
        if (currentArt > 110) { setArtSize(Math.max(110, currentArt - 5)); continue; }
        break;
    }
}

function setArtSize(px) {
    document.documentElement.style.setProperty('--art-size', px + 'px');
}
function getArtSize() {
    const val = getComputedStyle(document.documentElement).getPropertyValue('--art-size').trim();
    return parseInt(val,10) || 180;
}

function extractAndApplyDominantColor(url) {
    const token = ++colorToken;
    const img = new Image();
    img.crossOrigin = "anonymous";
    img.referrerPolicy = "no-referrer";
    img.onload = () => {
        if (token !== colorToken) return;   // a newer track (or no art) superseded this load
        try {
            const canvas = document.createElement('canvas');
            const ctx = canvas.getContext('2d');
            const size = 64;
            canvas.width = size;
            canvas.height = size;
            ctx.drawImage(img, 0, 0, size, size);
            const data = ctx.getImageData(0,0,size,size).data;
            let r=0,g=0,b=0,count=0;
            for (let i=0;i<data.length;i+=4) {
                const R=data[i], G=data[i+1], B=data[i+2], A=data[i+3];
                if (A < 64) continue;
                const brightness = 0.299*R + 0.587*G + 0.114*B;
                if (brightness < 15 || brightness > 245) continue;
                r+=R; g+=G; b+=B; count++;
            }
            if (!count) return;
            r = Math.round(r/count); g = Math.round(g/count); b = Math.round(b/count);
            const factor = 0.85;
            const avg = (r+g+b)/3;
            r = Math.round(r*factor + avg*(1-factor));
            g = Math.round(g*factor + avg*(1-factor));
            b = Math.round(b*factor + avg*(1-factor));
            const darker = 'rgb(' + Math.max(0,r-40) + ',' + Math.max(0,g-40) + ',' + Math.max(0,b-40) + ')';
            const main   = 'rgb(' + r + ',' + g + ',' + b + ')';
            document.body.style.background = 'linear-gradient(135deg,' + darker + ',' + main + ')';
        } catch(err) {
            console.warn("Color extraction failed:", err);
        }
    };
    img.onerror = () => console.warn("Album art load error; skipping dominant color.");
    img.src = url;
}

fetchTrack();
window.addEventListener('resize', () => { resetLayoutAllowances(); fitPanel(); });
</script>
</body>
</html>
"""


def _html_escape(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;")
                 .replace(">", "&gt;").replace('"', "&quot;"))


HTML_PAGE = (HTML_PAGE
             .replace("__POLL_INTERVAL_MS__", str(POLL_INTERVAL_MS))
             .replace("__IDLE_TIMEOUT__", str(IDLE_TIMEOUT))
             .replace("__HOTSPOT_INFO__", _html_escape(
                 "%s / %s" % (CONF["HOTSPOT_SSID"], CONF["HOTSPOT_PASSWORD"])))
             .replace("__PORTAL_URL__", _html_escape(PORTAL_URL.replace("http://", ""))))

# --------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------
@app.route("/")
def index():
    return Response(HTML_PAGE, mimetype="text/html")

@app.route("/current")
def current():
    with track_lock:
        data = dict(current_track)
    data["now"] = int(time.time())
    data["setup"] = read_setup_state()
    return jsonify(data)

@app.route("/qr/<name>.svg")
def qr(name):
    if name == "wifi":
        payload = "WIFI:T:WPA;S:%s;P:%s;;" % (wifi_qr_escape(CONF["HOTSPOT_SSID"]),
                                               wifi_qr_escape(CONF["HOTSPOT_PASSWORD"]))
    elif name == "portal":
        payload = PORTAL_URL
    else:
        abort(404)
    svg = qr_svg(payload)
    if svg is None:
        abort(503)
    return Response(svg, mimetype="image/svg+xml",
                    headers={"Cache-Control": "max-age=3600"})

# --------------------------------------------------------------------------------------
# Shutdown Handling
# --------------------------------------------------------------------------------------
def shutdown_signal(signum, frame):
    log.info("Signal %s received, initiating shutdown...", signum)
    stop_event.set()
    terminate_songrec()
    sys.exit(0)  # raises SystemExit in the main thread, which stops the Flask server

signal.signal(signal.SIGTERM, shutdown_signal)
signal.signal(signal.SIGINT, shutdown_signal)

# --------------------------------------------------------------------------------------
# Thread Starters
# --------------------------------------------------------------------------------------
def start_background_threads():
    t1 = threading.Thread(target=songrec_worker, name="songrec-worker", daemon=True)
    t1.start()
    return [t1]

# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------
def main():
    start_background_threads()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    log.info("Starting Flask on %s:%s", host, port)
    app.run(host=host, port=port, threaded=True)

if __name__ == "__main__":
    main()
