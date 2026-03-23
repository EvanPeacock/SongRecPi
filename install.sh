#!/bin/bash
# install.sh
#
# Fresh install script for SongRecPi on Raspberry Pi OS Bullseye (32-bit).
# Tested on a GeeekPi 3.5" TFT LCD (tft35a driver, 320x480).
#
# Run as pi user (script will sudo where needed):
#   bash install.sh

set -e
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "================================================"
echo "  SongRecPi Installer"
echo "================================================"

# ── 1. System packages ────────────────────────────────────────────────────────
echo ""
echo "[1/7] Installing system packages..."
sudo apt update
sudo apt install -y \
    python3-pip \
    network-manager \
    libasound2-dev \
    libssl-dev \
    curl \
    git \
    chromium-browser \
    unclutter

# ── 2. Python dependencies ────────────────────────────────────────────────────
echo ""
echo "[2/7] Installing Python dependencies..."
pip3 install -r "$REPO_DIR/requirements.txt" --break-system-packages

# ── 3. Rust + SongRec ─────────────────────────────────────────────────────────
echo ""
echo "[3/7] Installing Rust and SongRec..."
if ! command -v cargo &>/dev/null; then
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
    source "$HOME/.cargo/env"
else
    echo "Rust already installed, skipping."
fi

if ! command -v songrec &>/dev/null; then
    cargo install songrec
else
    echo "SongRec already installed, skipping."
fi

# ── 4. LCD display driver ─────────────────────────────────────────────────────
echo ""
echo "[4/7] Configuring LCD display (tft35a, rotate=270)..."
# The tft35a overlay is what drives the GeeekPi 3.5" display.
# If you have a different display, adjust the overlay name and rotation below.
if ! grep -q "dtoverlay=tft35a" /boot/config.txt; then
    echo "dtoverlay=tft35a:rotate=270" | sudo tee -a /boot/config.txt
    echo "LCD overlay added to /boot/config.txt"
else
    echo "LCD overlay already present in /boot/config.txt, skipping."
fi

# ── 5. App files ──────────────────────────────────────────────────────────────
echo ""
echo "[5/7] Installing app files..."
cp "$REPO_DIR/app.py" /home/pi/app.py

sudo cp "$REPO_DIR/scripts/wifi-portal.py" /usr/local/bin/wifi-portal.py
sudo cp "$REPO_DIR/scripts/wifi-portal-start.sh" /usr/local/bin/wifi-portal-start.sh
sudo chmod +x /usr/local/bin/wifi-portal.py
sudo chmod +x /usr/local/bin/wifi-portal-start.sh

# ── 6. Systemd services ───────────────────────────────────────────────────────
echo ""
echo "[6/7] Installing and enabling systemd services..."
sudo cp "$REPO_DIR/services/songrec.service" /etc/systemd/system/songrec.service
sudo cp "$REPO_DIR/services/wifi-portal.service" /etc/systemd/system/wifi-portal.service
sudo systemctl daemon-reload
sudo systemctl enable songrec
sudo systemctl enable wifi-portal

# ── 7. Kiosk autostart ────────────────────────────────────────────────────────
echo ""
echo "[7/7] Configuring kiosk autostart..."
mkdir -p /home/pi/.config/autostart
cat > /home/pi/.config/autostart/kiosk.desktop <<EOF
[Desktop Entry]
Type=Application
Name=SongRecPi Kiosk
Exec=chromium-browser --kiosk --noerrdialogs --disable-infobars --no-first-run http://localhost:5000
EOF

# Hide cursor on idle
cat > /home/pi/.config/autostart/unclutter.desktop <<EOF
[Desktop Entry]
Type=Application
Name=Unclutter
Exec=unclutter -idle 0.1 -root
EOF

echo ""
echo "================================================"
echo "  Installation complete!"
echo ""
echo "  On first boot without a known WiFi network:"
echo "  1. Connect to hotspot: SongRecPi"
echo "  2. Password:           songrecpi"
echo "  3. Open browser to:    http://10.42.0.1"
echo "  4. Pick your network and enter your password"
echo ""
echo "  Reboot now to start everything:"
echo "    sudo reboot"
echo "================================================"
