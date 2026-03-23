#!/usr/bin/env python3
"""
SongRec Flask Kiosk App (centered adaptive panel + auto display power management)

Features:
- Background thread running `songrec listen --json` (restarts with exponential backoff).
- Fast shutdown: tracks subprocess; terminates on SIGINT/SIGTERM.
- /current JSON endpoint.
- Root (/) serves kiosk UI with:
    * Centered translucent panel that grows with content but keeps >=5px margin to edges.
    * Multi-line wrapping with adaptive clamping & album art shrinking to fit vertically.
    * Dynamic background gradient from album art dominant color.
    * Flash animation on track change.
    * Hidden cursor.
Environment Variables:
    SONGREC_PATH              (default: /home/pi/.cargo/bin/songrec)
    HOST                      (default: 0.0.0.0)
    PORT                      (default: 5000)
    POLL_INTERVAL             (seconds, float, default 1.0)
"""

import os
import sys
import json
import time
import signal
import logging
import threading
import subprocess
from typing import Dict, Any, Optional

from flask import Flask, jsonify, Response

# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------
SONGREC_PATH = os.environ.get("SONGREC_PATH", "/home/pi/.cargo/bin/songrec")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "1.0"))
POLL_INTERVAL_MS = int(POLL_INTERVAL * 1000)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("songrec-app")

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
def songrec_worker():
    global proc_ref
    backoff = 3
    max_backoff = 60
    time.sleep(2)  # allow audio stack to settle

    while not stop_event.is_set():
        if not os.path.isfile(SONGREC_PATH):
            log.error("songrec not found at %s", SONGREC_PATH)
            time.sleep(min(backoff, 30))
            continue

        cmd = ["/usr/bin/stdbuf", "-oL", SONGREC_PATH, "listen", "--json"]
        log.info("Starting songrec: %s", " ".join(cmd))
        try:
            proc_ref = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
        except Exception:
            log.exception("Failed to start songrec")
            time.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
            continue

        if proc_ref.stdout is None:
            log.error("songrec started without a stdout pipe; aborting this run.")
            try:
                proc_ref.terminate()
            except Exception:
                pass
            proc_ref = None
            time.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
            continue

        for raw_line in proc_ref.stdout:
            if stop_event.is_set():
                break
            line = raw_line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "track" in data:
                track = data["track"]
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

        if stop_event.is_set():
            break

        rc = proc_ref.wait()
        log.warning("songrec exited (code=%s). Restarting in %ss", rc, backoff)
        time.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)

    if proc_ref and proc_ref.poll() is None:
        try:
            proc_ref.terminate()
            proc_ref.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc_ref.kill()
    log.info("songrec_worker exiting.")

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

html, body, body * {
    cursor: none !important;
}

* {
    -webkit-user-select:none;
    user-select:none;
}
</style>
</head>
<body>
  <div id="panel">
      <div id="albumArt"></div>
      <div class="track-info">
          <div id="trackTitle" class="track-title clamp4"></div>
          <div id="trackArtist" class="track-artist clamp2"></div>
      </div>
  </div>

<script>
const POLL_INTERVAL = __POLL_INTERVAL_MS__;
let lastId = "";
let lastArtURL = "";

async function fetchTrack() {
    try {
        const res = await fetch('/current?ts=' + Date.now());
        if(!res.ok) return;
        const data = await res.json();
        const title = data.title || '-';
        const artist = data.artist || '-';
        const art = data.album_art || '';
        const newId = title + '|' + artist + '|' + art;
        if (newId !== lastId) {
            const panel = document.getElementById('panel');
            panel.classList.remove('flash');
            void panel.offsetWidth;
            panel.classList.add('flash');

            const titleEl = document.getElementById('trackTitle');
            const artistEl = document.getElementById('trackArtist');
            titleEl.textContent = title;
            artistEl.textContent = artist;

            const artDiv = document.getElementById('albumArt');
            if (art) {
                artDiv.style.backgroundImage = 'url(' + art + ')';
                if (art !== lastArtURL) extractAndApplyDominantColor(art);
            } else {
                artDiv.style.backgroundImage = 'none';
            }
            lastArtURL = art;

            resetLayoutAllowances();
            fitPanel();
            lastId = newId;
        }
    } catch(e) {
        console.error(e);
    }
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
    const img = new Image();
    img.crossOrigin = "anonymous";
    img.referrerPolicy = "no-referrer";
    img.onload = () => {
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

setInterval(fetchTrack, POLL_INTERVAL);
fetchTrack();
window.addEventListener('resize', fitPanel);
</script>
</body>
</html>
"""

HTML_PAGE = HTML_PAGE.replace("__POLL_INTERVAL_MS__", str(POLL_INTERVAL_MS))

# --------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------
@app.route("/")
def index():
    return Response(HTML_PAGE, mimetype="text/html")

@app.route("/current")
def current():
    with track_lock:
        return jsonify(current_track)

# --------------------------------------------------------------------------------------
# Shutdown Handling
# --------------------------------------------------------------------------------------
def shutdown_signal(signum, frame):
    log.info("Signal %s received, initiating shutdown...", signum)
    stop_event.set()
    global proc_ref
    if proc_ref and proc_ref.poll() is None:
        log.info("Terminating songrec subprocess...")
        try:
            proc_ref.terminate()
            proc_ref.wait(timeout=3)
        except subprocess.TimeoutExpired:
            log.warning("songrec did not terminate; killing.")
            proc_ref.kill()

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
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    log.info("Starting Flask on %s:%s", host, port)
    app.run(host=host, port=port, threaded=True)

if __name__ == "__main__":
    main()
