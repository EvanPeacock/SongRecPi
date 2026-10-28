#!/bin/bash
# wifi-portal-start.sh
#
# Called by wifi-portal.service on boot.
# Waits (up to ~60s) for NetworkManager to connect, and only launches the
# hotspot + captive portal if no connection is found.
#
# While the hotspot is up, /run/songrecpi/setup.json tells the kiosk to show the
# WiFi setup QR codes. The portal removes it again once the Pi is connected.

set -u

CONF=${SONGRECPI_CONF:-/etc/songrecpi.conf}
HOTSPOT_SSID="SongRecPi"
HOTSPOT_PASSWORD="songrecpi"
HOTSPOT_CON="SongRecPi-Hotspot"
WIFI_IFACE="wlan0"
# shellcheck source=SCRIPTDIR/../config/songrecpi.conf
[ -r "$CONF" ] && . "$CONF"

STATE_DIR=${STATE_DIR:-/run/songrecpi}
STATE_FILE="$STATE_DIR/setup.json"
NETWORKS_FILE="$STATE_DIR/networks.txt"
PORTAL=${PORTAL:-/opt/songrecpi/wifi-portal.py}
WAIT_SECS=${WAIT_SECS:-60}

is_connected() {
    # "connected", "connected (site only)" and "connected (local only)" all count
    nmcli -t -f STATE general 2>/dev/null | grep -q '^connected'
}

write_state() {
    mkdir -p "$STATE_DIR"
    chmod 755 "$STATE_DIR"
    printf '{"mode":"setup","status":"%s","ssid":""}\n' "$1" > "$STATE_FILE.tmp"
    chmod 644 "$STATE_FILE.tmp"
    mv "$STATE_FILE.tmp" "$STATE_FILE"
}

waited=0
while [ "$waited" -lt "$WAIT_SECS" ]; do
    if is_connected; then
        echo "Already connected to a network, skipping WiFi portal."
        rm -f "$STATE_FILE"
        exit 0
    fi
    sleep 2
    waited=$((waited + 2))
done

echo "No network connection after ${WAIT_SECS}s. Scanning for nearby networks..."
mkdir -p "$STATE_DIR"
nmcli dev wifi rescan ifname "$WIFI_IFACE" 2>/dev/null
sleep 4
nmcli -t -f SSID,SIGNAL,SECURITY dev wifi list ifname "$WIFI_IFACE" > "$NETWORKS_FILE"

echo "Starting hotspot: $HOTSPOT_SSID"
if ! nmcli dev wifi hotspot ifname "$WIFI_IFACE" con-name "$HOTSPOT_CON" \
        ssid "$HOTSPOT_SSID" password "$HOTSPOT_PASSWORD"; then
    echo "Failed to start the hotspot; giving up." >&2
    rm -f "$STATE_FILE"
    exit 1
fi
# Never let the hotspot profile auto-activate instead of the home network.
nmcli con modify "$HOTSPOT_CON" connection.autoconnect no
write_state waiting
sleep 3

echo "Starting captive portal on 10.42.0.1..."
export SONGRECPI_CONF="$CONF" STATE_DIR
exec python3 "$PORTAL"
