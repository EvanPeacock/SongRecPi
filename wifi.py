"""
WiFi helpers for SongRecPi (NetworkManager via nmcli).

Used by app.py for:
- first-time setup over the hotspot (connect, and on failure delete the new
  profile and bring the hotspot back so the user can retry);
- the settings page (list / add / forget saved networks).

Read-only nmcli calls run as the app user; changes go through `sudo -n nmcli`,
which install.sh allows with /etc/sudoers.d/songrecpi.
"""

import json
import os
import subprocess
import threading
import time
from typing import Dict, List, Optional

STATE_DIR = os.environ.get("STATE_DIR", "/run/songrecpi")
STATE_FILE = os.environ.get("SETUP_STATE_FILE", os.path.join(STATE_DIR, "setup.json"))
NETWORKS_FILE = os.path.join(os.path.dirname(STATE_FILE), "networks.txt")
NMCLI = os.environ.get("NMCLI", "nmcli")
SUDO = [] if os.geteuid() == 0 or os.environ.get("NMCLI_NO_SUDO") else ["sudo", "-n"]

connect_lock = threading.Lock()


def load_conf(path: str) -> Dict[str, str]:
    """Read the shared KEY="value" config (also sourced by wifi-portal-start.sh)."""
    conf = {"HOTSPOT_SSID": "SongRecPi", "HOTSPOT_PASSWORD": "songrecpi",
            "HOTSPOT_CON": "SongRecPi-Hotspot", "WIFI_IFACE": "wlan0"}
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


CONF = load_conf(os.environ.get("SONGRECPI_CONF", "/etc/songrecpi.conf"))

# --------------------------------------------------------------------------------------
# nmcli plumbing
# --------------------------------------------------------------------------------------
def nmcli(*args, sudo=False, timeout=60) -> subprocess.CompletedProcess:
    cmd = (SUDO if sudo else []) + [NMCLI] + list(args)
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def split_nmcli(line: str) -> List[str]:
    """Split one line of `nmcli -t` output; fields escape ':' and '\\' with a backslash."""
    fields, cur, chars = [], [], iter(line)
    for ch in chars:
        if ch == "\\":
            cur.append(next(chars, ""))
        elif ch == ":":
            fields.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    fields.append("".join(cur))
    return fields


def parse_networks(text: str) -> List[Dict]:
    """Parse `nmcli -t -f SSID,SIGNAL,SECURITY[,IN-USE] dev wifi list` output."""
    networks: Dict[str, Dict] = {}
    for line in text.splitlines():
        parts = split_nmcli(line)
        if len(parts) < 2:
            continue
        ssid = parts[0]
        if not ssid or ssid == CONF["HOTSPOT_SSID"] or ssid in networks:
            continue
        security = parts[2] if len(parts) > 2 else ""
        networks[ssid] = {
            "ssid": ssid,
            "signal": parts[1],
            "open": security.strip() in ("", "--"),
        }
    return sorted(networks.values(),
                  key=lambda n: -int(n["signal"]) if n["signal"].isdigit() else 0)


def scanned_networks() -> List[Dict]:
    """Networks scanned by wifi-portal-start.sh just before the hotspot came up."""
    try:
        with open(NETWORKS_FILE, encoding="utf-8", errors="replace") as f:
            return parse_networks(f.read())
    except FileNotFoundError:
        return []


def visible_networks() -> List[Dict]:
    """Live scan results (cached by NetworkManager; no rescan, so it's quick)."""
    return parse_networks(nmcli("-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list",
                                timeout=15).stdout)


def saved_networks() -> List[Dict]:
    """Saved WiFi client profiles (not access-point ones like the setup hotspot)."""
    result = nmcli("-t", "-f", "NAME,UUID,TYPE,ACTIVE", "connection", "show", timeout=15)
    saved = []
    for line in result.stdout.splitlines():
        parts = split_nmcli(line)
        if len(parts) < 4 or parts[2] != "802-11-wireless":
            continue
        mode = nmcli("-g", "802-11-wireless.mode", "connection", "show", parts[1],
                     timeout=15).stdout.strip()
        if mode == "ap":
            continue
        saved.append({"name": parts[0], "uuid": parts[1], "active": parts[3] == "yes"})
    return saved


def connection_uuids() -> set:
    return set(nmcli("-t", "-f", "UUID", "connection", "show").stdout.split())


def add_network(ssid: str, password: str, hidden: bool) -> Optional[str]:
    """Save a network for later (used automatically when in range). Returns an error or None."""
    ssid = ssid.strip()
    if not ssid:
        return "Enter a network name."
    if password and len(password) < 8:
        return "WiFi passwords are at least 8 characters."
    args = ["connection", "add", "type", "wifi", "con-name", ssid, "ifname", CONF["WIFI_IFACE"],
            "ssid", ssid, "connection.autoconnect", "yes"]
    if password:
        args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
    if hidden:
        args += ["802-11-wireless.hidden", "yes"]
    result = nmcli(*args, sudo=True)
    if result.returncode != 0:
        return (result.stderr or result.stdout).strip() or "nmcli failed"
    return None


def forget_network(uuid: str) -> Optional[str]:
    match = [n for n in saved_networks() if n["uuid"] == uuid]
    if not match:
        return "That network isn't saved."
    if match[0]["active"]:
        return "That's the network the Pi is using right now, so it can't be forgotten from here."
    result = nmcli("connection", "delete", "uuid", uuid, sudo=True)
    if result.returncode != 0:
        return (result.stderr or result.stdout).strip() or "nmcli failed"
    return None

# --------------------------------------------------------------------------------------
# Setup state (shared with wifi-portal-start.sh and shown on the kiosk)
# --------------------------------------------------------------------------------------
def read_state() -> Optional[Dict]:
    """Written at boot ("checking", then "setup") and during setup; absent otherwise."""
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, PermissionError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def write_state(status: str, ssid: str = ""):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"mode": "setup", "status": status, "ssid": ssid}, f)
    os.chmod(tmp, 0o644)
    os.replace(tmp, STATE_FILE)


def clear_state():
    try:
        os.remove(STATE_FILE)
    except FileNotFoundError:
        pass


def in_setup() -> bool:
    state = read_state()
    return bool(state and state.get("mode") == "setup")


def setup_connect(ssid: str, password: str, hidden: bool, log=print):
    """Leave the hotspot and join `ssid`. On failure, restore the hotspot for a retry."""
    with connect_lock:
        write_state("connecting", ssid)
        time.sleep(1)  # let the "Connecting..." page reach the phone
        before = connection_uuids()
        nmcli("connection", "down", CONF["HOTSPOT_CON"], sudo=True)
        # Give wlan0 time to fully release from AP mode before connecting
        time.sleep(4)
        nmcli("dev", "wifi", "rescan", "ifname", CONF["WIFI_IFACE"], sudo=True)
        time.sleep(3)

        args = ["--wait", "45", "dev", "wifi", "connect", ssid, "ifname", CONF["WIFI_IFACE"]]
        if password:
            args += ["password", password]
        if hidden:
            args += ["hidden", "yes"]
        result = nmcli(*args, sudo=True, timeout=60)

        if result.returncode == 0:
            log("Connected to %r; leaving setup mode." % ssid)
            clear_state()
            return

        log("Connecting to %r failed: %s" % (ssid, (result.stderr or result.stdout).strip()))
        # nmcli saves a profile even when activation fails; delete only profiles it just
        # created, so an existing saved network with the same name is never touched.
        for uuid in connection_uuids() - before:
            nmcli("connection", "delete", "uuid", uuid, sudo=True)
        up = nmcli("connection", "up", CONF["HOTSPOT_CON"], sudo=True)
        if up.returncode != 0:
            log("Failed to restart the hotspot: %s" % up.stderr.strip())
        write_state("failed", ssid)
