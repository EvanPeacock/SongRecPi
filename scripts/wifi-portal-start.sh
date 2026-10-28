#!/bin/bash
# wifi-portal-start.sh
#
# Called by wifi-portal.service on boot.
# Waits for NetworkManager to connect, and only launches the hotspot + captive
# portal if no connection is found. The wait is ~60s when a WiFi network is
# saved (to give it time to connect), but only ~15s when none is (e.g. a freshly
# gifted Pi), which is just long enough for a plugged-in ethernet cable.
#
# /run/songrecpi/setup.json tells the kiosk what to show:
#   mode "checking"  while waiting  -> "Connecting to Wi-Fi..."
#   mode "setup"     hotspot is up  -> the WiFi setup QR codes
# It is removed once the Pi is connected.

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
NO_SAVED_WAIT_SECS=${NO_SAVED_WAIT_SECS:-15}

is_connected() {
    # "connected", "connected (site only)" and "connected (local only)" all count
    nmcli -t -f STATE general 2>/dev/null | grep -q '^connected'
}

has_saved_wifi() {
    # Any saved WiFi client profile (access-point profiles like our hotspot don't count).
    local uuid type
    while IFS=: read -r uuid type; do
        [ "$type" = 802-11-wireless ] || continue
        [ "$(nmcli -g 802-11-wireless.mode connection show "$uuid" 2>/dev/null)" = ap ] && continue
        return 0
    done < <(nmcli -t -f UUID,TYPE connection show 2>/dev/null)
    return 1
}

write_state() {
    mkdir -p "$STATE_DIR"
    chmod 755 "$STATE_DIR"
    printf '{"mode":"%s","status":"%s","ssid":""}\n' "$1" "$2" > "$STATE_FILE.tmp"
    chmod 644 "$STATE_FILE.tmp"
    mv "$STATE_FILE.tmp" "$STATE_FILE"
}

if has_saved_wifi; then
    wait_secs=$WAIT_SECS
else
    wait_secs=$NO_SAVED_WAIT_SECS
    echo "No saved WiFi networks; waiting only ${wait_secs}s (for ethernet) before the hotspot."
fi

write_state checking waiting
waited=0
while [ "$waited" -lt "$wait_secs" ]; do
    if is_connected; then
        echo "Already connected to a network, skipping WiFi portal."
        rm -f "$STATE_FILE"
        exit 0
    fi
    sleep 2
    waited=$((waited + 2))
done

echo "No network connection after ${wait_secs}s. Scanning for nearby networks..."
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
write_state setup waiting
sleep 3

echo "Starting captive portal on 10.42.0.1..."
export SONGRECPI_CONF="$CONF" STATE_DIR
exec python3 "$PORTAL"
