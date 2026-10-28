#!/usr/bin/env python3
"""
SongRecPi WiFi Setup Portal

When the Pi can't find a known WiFi network on boot, this script:
1. Reads a pre-scanned list of nearby networks from $STATE_DIR/networks.txt
2. Serves a captive portal web UI on 10.42.0.1:80
3. Handles captive portal detection for iOS, Android, and Windows
4. Connects to the chosen network via nmcli:
   - on success it exits, so port 80 isn't left open on the home network;
   - on failure it deletes the failed profile, brings the hotspot back and
     reports "wrong password" on this page and on the kiosk screen.

Progress is written to $STATE_DIR/setup.json, which the kiosk (app.py) shows.
This script is started by wifi-portal-start.sh, not directly.
"""

import html
import json
import os
import subprocess
import threading
import time
from flask import Flask, request, redirect

app = Flask(__name__)

PORTAL_URL = 'http://10.42.0.1/'
CONF_PATH = os.environ.get('SONGRECPI_CONF', '/etc/songrecpi.conf')
STATE_DIR = os.environ.get('STATE_DIR', '/run/songrecpi')
STATE_FILE = os.path.join(STATE_DIR, 'setup.json')
NETWORKS_FILE = os.path.join(STATE_DIR, 'networks.txt')
OTHER = '__other__'


def load_conf(path):
    """Read the shared KEY="value" config (also sourced by wifi-portal-start.sh)."""
    conf = {'HOTSPOT_SSID': 'SongRecPi', 'HOTSPOT_CON': 'SongRecPi-Hotspot', 'WIFI_IFACE': 'wlan0'}
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                conf[key.strip()] = value.strip().strip('"\'')
    except FileNotFoundError:
        pass
    return conf


CONF = load_conf(CONF_PATH)
connect_lock = threading.Lock()


def write_state(status, ssid=''):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'mode': 'setup', 'status': status, 'ssid': ssid}, f)
    os.chmod(tmp, 0o644)
    os.replace(tmp, STATE_FILE)


def read_state():
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def split_nmcli(line):
    """Split one line of `nmcli -t` output; fields escape ':' and '\\' with a backslash."""
    fields, cur, chars = [], [], iter(line)
    for ch in chars:
        if ch == '\\':
            cur.append(next(chars, ''))
        elif ch == ':':
            fields.append(''.join(cur))
            cur = []
        else:
            cur.append(ch)
    fields.append(''.join(cur))
    return fields


def get_networks():
    """Read pre-scanned network list cached before hotspot started."""
    networks = {}
    try:
        with open(NETWORKS_FILE, encoding='utf-8', errors='replace') as f:
            for line in f:
                parts = split_nmcli(line.rstrip('\n'))
                if len(parts) < 2:
                    continue
                ssid = parts[0]
                if not ssid or ssid == CONF['HOTSPOT_SSID'] or ssid in networks:
                    continue
                security = parts[2] if len(parts) > 2 else ''
                networks[ssid] = {
                    'ssid': ssid,
                    'signal': parts[1],
                    'open': security.strip() in ('', '--'),
                }
    except FileNotFoundError:
        pass
    return sorted(networks.values(), key=lambda n: -int(n['signal']) if n['signal'].isdigit() else 0)


def nmcli(*args, timeout=60):
    try:
        return subprocess.run(['nmcli'] + list(args), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 1, '', 'timed out')


def connection_uuids():
    result = nmcli('-t', '-f', 'UUID', 'connection', 'show')
    return set(result.stdout.split())


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


PAGE_STYLE = '''
        body { font-family: sans-serif; max-width: 400px; margin: 40px auto; padding: 20px; background: #111; color: #eee; }
        select, input, button { width: 100%; padding: 12px; margin: 10px 0; font-size: 16px; box-sizing: border-box; border-radius: 6px; border: none; }
        select, input { background: #222; color: #eee; }
        button { background: #1db954; color: white; cursor: pointer; font-weight: bold; }
        h2 { color: #1db954; }
        p { color: #aaa; font-size: 14px; }
        .error { color: #ff8a80; background: #2a1414; padding: 10px; border-radius: 6px; }
'''


@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def index(path=''):
    networks = get_networks()
    options = ''.join(
        '<option value="{v}" data-open="{o}">{v}{lock} (signal: {s})</option>'.format(
            v=html.escape(n['ssid']), o='1' if n['open'] else '0',
            lock='' if n['open'] else ' 🔒', s=html.escape(n['signal']))
        for n in networks
    )
    options += '<option value="%s" data-open="0">Other (hidden network)…</option>' % OTHER

    state = read_state()
    error = ''
    if state.get('status') == 'failed':
        error = ('<p class="error">Couldn’t connect to <b>%s</b>. Check the password and try again.</p>'
                 % html.escape(state.get('ssid') or 'that network'))

    return f'''<!DOCTYPE html>
<html>
<head>
    <title>SongRecPi WiFi Setup</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>{PAGE_STYLE}</style>
</head>
<body>
    <h2>🎵 SongRecPi Setup</h2>
    <p>Connect to your WiFi network to get started.</p>
    {error}
    <form method="POST" action="/connect">
        <label for="ssid">Select your network:</label>
        <select name="ssid" id="ssid">{options}</select>
        <div id="hiddenBox">
            <label for="hidden_ssid">Hidden network name:</label>
            <input type="text" name="hidden_ssid" id="hidden_ssid" autocapitalize="none" autocorrect="off">
        </div>
        <div id="pwBox">
            <label for="password">Password:</label>
            <input type="password" name="password" id="password" placeholder="WiFi password">
        </div>
        <button type="submit">Connect</button>
    </form>
    <script>
        var sel = document.getElementById('ssid');
        function sync() {{
            var opt = sel.options[sel.selectedIndex];
            document.getElementById('hiddenBox').style.display = sel.value === '{OTHER}' ? '' : 'none';
            document.getElementById('pwBox').style.display = opt && opt.dataset.open === '1' ? 'none' : '';
        }}
        sel.addEventListener('change', sync);
        sync();
    </script>
</body>
</html>'''


def do_connect(ssid, password, hidden):
    with connect_lock:
        write_state('connecting', ssid)
        time.sleep(1)  # let the "Connecting..." page reach the phone
        before = connection_uuids()
        nmcli('connection', 'down', CONF['HOTSPOT_CON'])
        # Give wlan0 time to fully release from AP mode before connecting
        time.sleep(4)
        nmcli('dev', 'wifi', 'rescan', 'ifname', CONF['WIFI_IFACE'])
        time.sleep(3)

        cmd = ['--wait', '45', 'dev', 'wifi', 'connect', ssid, 'ifname', CONF['WIFI_IFACE']]
        if password:
            cmd += ['password', password]
        if hidden:
            cmd += ['hidden', 'yes']
        result = nmcli(*cmd, timeout=60)

        if result.returncode == 0:
            print('Connected to %r; shutting down the portal.' % ssid, flush=True)
            try:
                os.remove(STATE_FILE)
            except FileNotFoundError:
                pass
            os._exit(0)

        print('Connecting to %r failed: %s' % (ssid, (result.stderr or result.stdout).strip()), flush=True)
        # nmcli saves a profile even when activation fails; delete only profiles it just
        # created, so an existing saved network with the same name is never touched.
        for uuid in connection_uuids() - before:
            nmcli('connection', 'delete', 'uuid', uuid)
        up = nmcli('connection', 'up', CONF['HOTSPOT_CON'])
        if up.returncode != 0:
            print('Failed to restart the hotspot: %s' % up.stderr.strip(), flush=True)
        write_state('failed', ssid)


@app.route('/connect', methods=['POST'])
def connect():
    ssid = request.form.get('ssid') or ''
    hidden = ssid == OTHER
    if hidden:
        ssid = (request.form.get('hidden_ssid') or '').strip()
    if not ssid:
        return redirect(PORTAL_URL, code=303)

    password = request.form.get('password') or ''
    if not hidden and any(n['ssid'] == ssid and n['open'] for n in get_networks()):
        password = ''

    if connect_lock.locked():
        return redirect(PORTAL_URL, code=303)
    threading.Thread(target=do_connect, args=(ssid, password, hidden), daemon=True).start()

    return f'''<!DOCTYPE html>
<html>
<head>
    <title>Connecting...</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>{PAGE_STYLE} body {{ text-align: center; }}</style>
</head>
<body>
    <h2>🎵 Connecting...</h2>
    <p>Connecting to <b>{html.escape(ssid)}</b> now.</p>
    <p>This page will go offline in a moment — that's expected.</p>
    <p>If the SongRecPi screen shows your song view, it worked. If it says the password
       was wrong, rejoin the <b>{html.escape(CONF['HOTSPOT_SSID'])}</b> network and try again.</p>
</body>
</html>'''


if __name__ == '__main__':
    app.run(host=os.environ.get('PORTAL_HOST', '0.0.0.0'), port=int(os.environ.get('PORTAL_PORT', '80')))
