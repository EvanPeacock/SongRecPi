#!/usr/bin/env python3
"""
SongRec Flask Kiosk App (kiosk screen + settings page + WiFi setup)

Features:
- Background thread running `songrec listen --json` (restarts with exponential backoff;
  the backoff resets after a run that lasted more than a minute).
- Mute: stops songrec entirely (the mic is not read) until unmuted. Toggled by tapping
  the screen or from the settings page; remembered across restarts.
- Recognition history with streaming links, kept in ~/.local/state/songrecpi.
- Fast shutdown: tracks subprocess; terminates on SIGINT/SIGTERM and exits.
- /kiosk: the on-screen UI (Chromium kiosk):
    * Centered translucent panel that grows with content but keeps >=5px margin to edges.
    * Multi-line wrapping with adaptive clamping & album art shrinking to fit vertically.
    * Dynamic background gradient from album art dominant color.
    * Flash animation on track change.
    * "Listening..." after IDLE_TIMEOUT without a new recognition; after QUIET_AFTER more,
      a small still "Listening..." at the bottom.
    * Muted: black screen with a small hint, and a gear that shows the settings page QR.
    * WiFi setup view (two QR codes + status) while the setup hotspot is running.
- /: settings page for phones/browsers on the home network (mute, history, WiFi).
  While the setup hotspot is up it is the WiFi setup (captive) portal instead.
Environment Variables:
    SONGREC_PATH              (default: ~/.cargo/bin/songrec)
    HOST                      (default: 0.0.0.0)
    PORT                      (default: 80)
    POLL_INTERVAL             (seconds, float, default 1.0)
    IDLE_TIMEOUT              (seconds, default 600; 0 disables the idle views)
    QUIET_AFTER               (seconds of "Listening..." before it goes small/still, default 300)
    SONGRECPI_CONF            (default: /etc/songrecpi.conf)
    SETUP_STATE_FILE          (default: /run/songrecpi/setup.json)
    DATA_DIR                  (default: ~/.local/state/songrecpi)
"""

import io
import os
import sys
import json
import time
import signal
import socket
import logging
import threading
import subprocess
from functools import lru_cache
from typing import Dict, Any, List, Optional
from urllib.parse import quote_plus, urlparse

from flask import (Flask, jsonify, Response, abort, flash, redirect, render_template,
                   request, url_for)

import wifi

# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------
SONGREC_PATH = os.environ.get("SONGREC_PATH", os.path.expanduser("~/.cargo/bin/songrec"))
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "1.0"))
POLL_INTERVAL_MS = int(POLL_INTERVAL * 1000)
IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT", "600"))
QUIET_AFTER = int(os.environ.get("QUIET_AFTER", "300"))
DATA_DIR = os.environ.get("DATA_DIR", os.path.expanduser("~/.local/state/songrecpi"))
PORTAL_URL = "http://10.42.0.1/"
HISTORY_MAX = 200
HISTORY_REPEAT_SECS = 30 * 60   # same song again within this window isn't logged twice

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

CONF = wifi.CONF

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
wake_event = threading.Event()   # set to wake the worker (unmute / shutdown)
proc_ref: Optional[subprocess.Popen] = None   # reference to songrec subprocess
history: List[Dict[str, Any]] = []
muted = False
listen_started = time.time()     # when listening (re)started: app start or unmute


def _load_json(name: str, default):
    try:
        with open(os.path.join(DATA_DIR, name), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def _save_json(name: str, data):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        path = os.path.join(DATA_DIR, name)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(path + ".tmp", path)
    except OSError:
        log.exception("Could not save %s", name)


def load_persisted_state():
    global muted, history
    muted = bool(_load_json("state.json", {}).get("muted"))
    loaded = _load_json("history.json", [])
    history = loaded[-HISTORY_MAX:] if isinstance(loaded, list) else []

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
    if not album_art.startswith("https://"):
        album_art = ""   # it ends up inside CSS url(); only accept plain https links
    shazam_url = track.get("url") or ""
    now = int(time.time())

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
            "updated": now
        })
        last = history[-1] if history else None
        # After a restart or unmute the song that was playing is recognized again.
        repeat = bool(last and last["title"] == title and last["artist"] == artist and
                      now - last["time"] < HISTORY_REPEAT_SECS)
        if changed and not repeat:
            history.append({"title": title, "artist": artist, "album_art": album_art,
                            "shazam": shazam_url, "time": now})
            del history[:-HISTORY_MAX]
            snapshot = list(history)

    if changed:
        log.info("Track: %s - %s", artist, title)
        if not repeat:
            _save_json("history.json", snapshot)


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
    """Run songrec until it exits (or we're stopping/muting), feeding its output to handle_line."""
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
        if stop_event.is_set() or muted:
            break
        try:
            handle_line(raw_line)
        except Exception:
            log.exception("Failed to handle songrec output line: %r", raw_line[:200])

    if stop_event.is_set() or muted:
        return
    rc = proc_ref.wait()
    log.warning("songrec exited (code=%s)", rc)


def songrec_worker():
    backoff = BACKOFF_MIN
    if stop_event.wait(2):  # allow audio stack to settle
        return

    while not stop_event.is_set():
        if muted:
            log.info("Muted: microphone off.")
            while muted and not stop_event.is_set():
                wake_event.wait()
                wake_event.clear()
            backoff = BACKOFF_MIN
            continue
        started = time.monotonic()
        try:
            run_songrec_once()
        except Exception:
            log.exception("songrec run failed")
        if stop_event.is_set():
            break
        if muted:
            continue
        if time.monotonic() - started > STABLE_RUN_SECS:
            backoff = BACKOFF_MIN
        log.warning("Restarting songrec in %ss", backoff)
        if stop_event.wait(backoff):
            break
        backoff = min(backoff * 2, BACKOFF_MAX)

    terminate_songrec()
    log.info("songrec_worker exiting.")


def set_muted(value: bool):
    global muted, listen_started
    if value == muted:
        return
    muted = value
    _save_json("state.json", {"muted": muted})
    log.info("Mic %s", "muted" if muted else "unmuted")
    if muted:
        terminate_songrec()
    else:
        listen_started = time.time()   # show "Listening..." rather than the old song
        wake_event.set()

# --------------------------------------------------------------------------------------
# Views, links and QR codes
# --------------------------------------------------------------------------------------
def kiosk_view(now: float) -> str:
    """What the screen should show: setup | checking | muted | song | listening | quiet."""
    setup = wifi.read_state()
    if setup:
        return "setup" if setup.get("mode") == "setup" else "checking"
    if muted:
        return "muted"
    with track_lock:
        updated = current_track["updated"]
    fresh = updated > 0 and updated >= int(listen_started)   # `updated` is whole seconds
    if fresh and (IDLE_TIMEOUT <= 0 or now - updated <= IDLE_TIMEOUT):
        return "song"
    if IDLE_TIMEOUT <= 0:
        return "listening"
    quiet_start = updated + IDLE_TIMEOUT if fresh else listen_started
    if QUIET_AFTER > 0 and now - quiet_start > QUIET_AFTER:
        return "quiet"
    return "listening"


def streaming_links(item: Dict[str, Any]) -> List[Dict[str, str]]:
    q = quote_plus("%s %s" % (item.get("title", ""), item.get("artist", "")))
    links = [
        {"name": "Spotify", "url": "https://open.spotify.com/search/" + q},
        {"name": "Apple Music", "url": "https://music.apple.com/search?term=" + q},
        {"name": "YouTube Music", "url": "https://music.youtube.com/search?q=" + q},
    ]
    if item.get("shazam", "").startswith("https://"):
        links.append({"name": "Shazam", "url": item["shazam"]})
    return links


def local_ip() -> Optional[str]:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))   # no packet is sent; just picks the outgoing address
            return s.getsockname()[0]
    except OSError:
        return None


def settings_urls() -> Dict[str, str]:
    port = os.environ.get("PORT", "80")
    suffix = "" if port == "80" else ":" + port
    ip = local_ip()
    return {
        "name": "http://%s.local%s" % (socket.gethostname(), suffix),
        "ip": "http://%s%s" % (ip, suffix) if ip else "",
    }


def wifi_qr_escape(value: str) -> str:
    for ch in ('\\', ';', ',', ':', '"'):
        value = value.replace(ch, '\\' + ch)
    return value


@lru_cache(maxsize=8)
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
# Flask App
# --------------------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.urandom(16)   # only for flash() messages; sessions needn't survive restarts


@app.before_request
def same_origin_posts():
    """Refuse cross-site form posts (a page elsewhere can't mute the Pi or change WiFi)."""
    if request.method != "POST":
        return None
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    if origin and urlparse(origin).netloc != request.host:
        abort(403)
    return None


@app.template_filter("clock")
def clock_filter(ts):
    return time.strftime("%a %d %b, %H:%M", time.localtime(ts))


@app.route("/kiosk")
def kiosk():
    return render_template(
        "kiosk.html",
        poll_ms=POLL_INTERVAL_MS,
        hotspot_info="%s / %s" % (CONF["HOTSPOT_SSID"], CONF["HOTSPOT_PASSWORD"]),
        portal_url=PORTAL_URL.replace("http://", "").rstrip("/"),
    )


@app.route("/current")
def current():
    now = time.time()
    with track_lock:
        data = dict(current_track)
    data["now"] = int(now)
    data["view"] = kiosk_view(now)
    data["muted"] = muted
    data["setup"] = wifi.read_state()
    return jsonify(data)


@app.route("/api/mute", methods=["POST"])
def api_mute():
    payload = request.get_json(silent=True) or {}
    value = payload.get("muted", request.form.get("muted"))
    set_muted(value in (True, "1", "true", "on"))
    if request.is_json:
        return jsonify({"muted": muted})
    return redirect(url_for("index"), code=303)


@app.route("/api/settings-url")
def api_settings_url():
    return jsonify(settings_urls())


@app.route("/qr/<name>.svg")
def qr(name):
    if name == "wifi":
        payload = "WIFI:T:WPA;S:%s;P:%s;;" % (wifi_qr_escape(CONF["HOTSPOT_SSID"]),
                                               wifi_qr_escape(CONF["HOTSPOT_PASSWORD"]))
    elif name == "portal":
        payload = PORTAL_URL
    elif name == "settings":
        urls = settings_urls()
        payload = urls["ip"] or urls["name"]
    else:
        abort(404)
    svg = qr_svg(payload)
    if svg is None:
        abort(503)
    cache = "no-cache" if name == "settings" else "max-age=3600"
    return Response(svg, mimetype="image/svg+xml", headers={"Cache-Control": cache})


@app.route("/")
def index():
    if wifi.in_setup():
        return setup_page()
    with track_lock:
        track = dict(current_track)
        items = [dict(h) for h in reversed(history)]
    for item in items:
        item["links"] = streaming_links(item)
    return render_template(
        "settings.html",
        track=track, view=kiosk_view(time.time()), muted=muted, history=items,
        saved=wifi.saved_networks(), visible=wifi.visible_networks(),
        urls=settings_urls(),
    )


@app.route("/wifi/add", methods=["POST"])
def wifi_add():
    error = wifi.add_network(request.form.get("ssid", ""), request.form.get("password", ""),
                             request.form.get("hidden") == "on")
    flash(error or "Saved. The Pi will use it whenever it's in range.", "error" if error else "ok")
    return redirect(url_for("index") + "#wifi", code=303)


@app.route("/wifi/forget", methods=["POST"])
def wifi_forget():
    error = wifi.forget_network(request.form.get("uuid", ""))
    flash(error or "Network forgotten.", "error" if error else "ok")
    return redirect(url_for("index") + "#wifi", code=303)

# ----- WiFi setup portal (only while the setup hotspot is up) --------------------------
OTHER = "__other__"


def setup_page():
    state = wifi.read_state() or {}
    return render_template("setup.html", networks=wifi.scanned_networks(), other=OTHER,
                           failed=state.get("status") == "failed", failed_ssid=state.get("ssid"))


# Captive portal detection endpoints
# Returning a redirect triggers the popup on iOS, Android, and Windows
@app.route("/generate_204")              # Android
@app.route("/gen_204")                   # Android fallback
@app.route("/hotspot-detect.html")       # Apple
@app.route("/library/test/success.html") # Apple fallback
@app.route("/ncsi.txt")                  # Windows
@app.route("/connecttest.txt")           # Windows 10+
@app.route("/redirect")
def captive_detect():
    if not wifi.in_setup():
        abort(404)
    return redirect(PORTAL_URL, code=302)


@app.errorhandler(404)
def not_found(e):
    # During setup, every unknown URL (the DNS sends all names here) leads to the portal.
    if wifi.in_setup():
        return redirect(PORTAL_URL, code=302)
    return e


@app.route("/connect", methods=["POST"])
def connect():
    if not wifi.in_setup():
        return redirect(url_for("index"), code=303)
    ssid = request.form.get("ssid") or ""
    hidden = ssid == OTHER
    if hidden:
        ssid = (request.form.get("hidden_ssid") or "").strip()
    if not ssid:
        return redirect(PORTAL_URL, code=303)

    password = request.form.get("password") or ""
    if not hidden and any(n["ssid"] == ssid and n["open"] for n in wifi.scanned_networks()):
        password = ""

    if wifi.connect_lock.locked():
        return redirect(PORTAL_URL, code=303)
    threading.Thread(target=wifi.setup_connect, args=(ssid, password, hidden, log.info),
                     daemon=True).start()
    return render_template("connecting.html", ssid=ssid, hotspot=CONF["HOTSPOT_SSID"],
                           settings=settings_urls()["name"])

# --------------------------------------------------------------------------------------
# Shutdown Handling
# --------------------------------------------------------------------------------------
def shutdown_signal(signum, frame):
    log.info("Signal %s received, initiating shutdown...", signum)
    stop_event.set()
    wake_event.set()
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
    load_persisted_state()
    start_background_threads()
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "80"))
    log.info("Starting Flask on %s:%s (muted=%s, %d history items)", host, port, muted, len(history))
    app.run(host=host, port=port, threaded=True)

if __name__ == "__main__":
    main()
