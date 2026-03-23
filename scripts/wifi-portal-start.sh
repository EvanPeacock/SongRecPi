#!/bin/bash
# wifi-portal-start.sh
#
# Called by wifi-portal.service on boot.
# Waits for NetworkManager to settle, checks if already connected,
# and only launches the hotspot + captive portal if no connection is found.

sleep 20

if nmcli -t -f STATE general | grep -q "^connected$"; then
    echo "Already connected to a network, skipping WiFi portal."
    exit 0
fi

echo "No network connection found. Scanning for nearby networks..."
nmcli dev wifi rescan
sleep 4
nmcli -t -f SSID,SIGNAL,SECURITY dev wifi list > /tmp/wifi-networks.txt

echo "Starting hotspot: SongRecPi"
nmcli dev wifi hotspot ifname wlan0 ssid "SongRecPi" password "songrecpi"
sleep 3

echo "Starting captive portal on 10.42.0.1..."
python3 /usr/local/bin/wifi-portal.py
