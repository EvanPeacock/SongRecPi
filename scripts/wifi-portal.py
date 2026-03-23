#!/usr/bin/env python3
"""
SongRecPi WiFi Setup Portal

When the Pi can't find a known WiFi network on boot, this script:
1. Reads a pre-scanned list of nearby networks from /tmp/wifi-networks.txt
2. Serves a captive portal web UI on 10.42.0.1:80
3. Handles captive portal detection for iOS, Android, and Windows
4. Connects to the chosen network via nmcli, then shuts down the hotspot

This script is started by wifi-portal-start.sh, not directly.
"""

import subprocess
import time
import threading
from flask import Flask, request, redirect

app = Flask(__name__)

PORTAL_URL = 'http://10.42.0.1/'


def get_networks():
    """Read pre-scanned network list cached before hotspot started."""
    networks = []
    seen = set()
    try:
        with open('/tmp/wifi-networks.txt', 'r') as f:
            for line in f:
                parts = line.strip().split(':')
                if len(parts) >= 2 and parts[0] and parts[0] not in seen and parts[0] != 'SongRecPi':
                    seen.add(parts[0])
                    networks.append({'ssid': parts[0], 'signal': parts[1]})
    except FileNotFoundError:
        pass
    return networks


# Captive portal detection endpoints
# Returning a redirect triggers the popup on iOS, Android, and Windows
@app.route('/generate_204')              # Android
@app.route('/gen_204')                   # Android fallback
@app.route('/hotspot-detect.html')       # Apple
@app.route('/library/test/success.html') # Apple fallback
@app.route('/ncsi.txt')                  # Windows
@app.route('/connecttest.txt')           # Windows 10+
@app.route('/redirect')
def captive_detect():
    return redirect(PORTAL_URL, code=302)


@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def index(path=''):
    networks = get_networks()
    options = ''.join(
        f'<option value="{n["ssid"]}">{n["ssid"]} (signal: {n["signal"]})</option>'
        for n in networks
    )
    return f'''<!DOCTYPE html>
<html>
<head>
    <title>SongRecPi WiFi Setup</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {{ font-family: sans-serif; max-width: 400px; margin: 40px auto; padding: 20px; background: #111; color: #eee; }}
        select, input, button {{ width: 100%; padding: 12px; margin: 10px 0; font-size: 16px; box-sizing: border-box; border-radius: 6px; border: none; }}
        select, input {{ background: #222; color: #eee; }}
        button {{ background: #1db954; color: white; cursor: pointer; font-weight: bold; }}
        h2 {{ color: #1db954; }}
        p {{ color: #aaa; font-size: 14px; }}
    </style>
</head>
<body>
    <h2>🎵 SongRecPi Setup</h2>
    <p>Connect to your WiFi network to get started.</p>
    <form method="POST" action="/connect">
        <label>Select your network:</label>
        <select name="ssid">{options}</select>
        <label>Password:</label>
        <input type="password" name="password" placeholder="WiFi password">
        <button type="submit">Connect</button>
    </form>
</body>
</html>'''


@app.route('/connect', methods=['POST'])
def connect():
    ssid = request.form.get('ssid')
    password = request.form.get('password')

    def do_connect():
        time.sleep(1)
        # Find and bring down whichever hotspot connection is active
        result = subprocess.run(
            ['nmcli', '-t', '-f', 'NAME,TYPE', 'connection', 'show', '--active'],
            capture_output=True, text=True
        )
        for line in result.stdout.strip().split('\n'):
            parts = line.split(':')
            if len(parts) >= 2 and 'hotspot' in parts[0].lower():
                subprocess.run(['nmcli', 'connection', 'down', parts[0]])
                break
        # Give wlan0 time to fully release from AP mode before connecting
        time.sleep(6)
        subprocess.run(['nmcli', 'dev', 'wifi', 'rescan'])
        time.sleep(3)
        subprocess.run(['nmcli', 'dev', 'wifi', 'connect', ssid, 'password', password])

    threading.Thread(target=do_connect).start()

    return '''<!DOCTYPE html>
<html>
<head>
    <title>Connecting...</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family: sans-serif; max-width: 400px; margin: 40px auto; padding: 20px; background: #111; color: #eee; text-align: center; }
        h2 { color: #1db954; }
        p { color: #aaa; }
    </style>
</head>
<body>
    <h2>🎵 Connecting...</h2>
    <p>Connecting to your network now.</p>
    <p>This page will go offline in a moment — that means it worked!</p>
    <p>Give the Pi about 30 seconds to finish connecting.</p>
</body>
</html>'''


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=80)
