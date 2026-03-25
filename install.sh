#!/bin/bash
# install.sh
#
# Fresh install script for SongRecPi on Raspberry Pi OS Trixie (64-bit).
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
echo "[1/8] Installing system packages..."
sudo apt update
sudo apt install -y \
    python3-pip \
    python3-flask \
    network-manager \
    libasound2-dev \
    libssl-dev \
    curl \
    git \
    chromium \
    unclutter

# ── 2. Python dependencies ────────────────────────────────────────────────────
echo ""
echo "[2/8] Installing Python dependencies..."
# Try apt-installed flask first (preferred on Trixie), fall back to pip
if ! python3 -c "import flask" 2>/dev/null; then
    pip3 install -r "$REPO_DIR/requirements.txt" --break-system-packages
else
    echo "Flask already available via system packages, skipping pip."
fi

# ── 3. Rust + SongRec ─────────────────────────────────────────────────────────
echo ""
echo "[3/8] Installing Rust and SongRec..."
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
echo "[4/8] Configuring LCD display (tft35a, rotate=270)..."
# On Trixie the boot config moved to /boot/firmware/config.txt
if [ -f /boot/firmware/config.txt ]; then
    BOOT_CONFIG=/boot/firmware/config.txt
else
    BOOT_CONFIG=/boot/config.txt
fi

if ! grep -q "dtoverlay=tft35a" "$BOOT_CONFIG"; then
    echo "dtoverlay=tft35a:rotate=270" | sudo tee -a "$BOOT_CONFIG"
    echo "LCD overlay added to $BOOT_CONFIG"
else
    echo "LCD overlay already present in $BOOT_CONFIG, skipping."
fi

# ── 5. App files ──────────────────────────────────────────────────────────────
echo ""
echo "[5/8] Installing app files..."
cp "$REPO_DIR/app.py" /home/pi/app.py

sudo cp "$REPO_DIR/scripts/wifi-portal.py" /usr/local/bin/wifi-portal.py
sudo cp "$REPO_DIR/scripts/wifi-portal-start.sh" /usr/local/bin/wifi-portal-start.sh
sudo chmod +x /usr/local/bin/wifi-portal.py
sudo chmod +x /usr/local/bin/wifi-portal-start.sh

# ── 6. Systemd services ───────────────────────────────────────────────────────
echo ""
echo "[6/8] Installing and enabling systemd services..."
sudo cp "$REPO_DIR/services/songrec.service" /etc/systemd/system/songrec.service
sudo cp "$REPO_DIR/services/wifi-portal.service" /etc/systemd/system/wifi-portal.service
sudo systemctl daemon-reload
sudo systemctl enable songrec
sudo systemctl enable wifi-portal

# ── 7. Storage management ─────────────────────────────────────────────────────
echo ""
echo "[7/8] Configuring storage management..."

# Cap systemd journal to 50MB total and 10MB per file
sudo mkdir -p /etc/systemd/journald.conf.d
sudo tee /etc/systemd/journald.conf.d/size.conf > /dev/null <<EOF
[Journal]
SystemMaxUse=50M
SystemMaxFileSize=10M
RuntimeMaxUse=20M
EOF
sudo systemctl restart systemd-journald

# Logrotate config for songrec service logs
sudo tee /etc/logrotate.d/songrec > /dev/null <<EOF
/var/log/songrec/*.log {
    daily
    rotate 3
    compress
    missingok
    notifempty
    create 0640 pi pi
}
EOF

# Weekly cron to clean apt cache and temp files
(crontab -l 2>/dev/null; echo "0 3 * * 0 sudo apt-get clean && sudo rm -rf /tmp/*.wav /tmp/wifi-networks.txt") | crontab -

# Disable swap to reduce SD card wear on always-on device
if systemctl is-enabled dphys-swapfile &>/dev/null; then
    sudo dphys-swapfile swapoff
    sudo dphys-swapfile uninstall
    sudo systemctl disable dphys-swapfile
else
    echo "dphys-swapfile not present, skipping."
fi

# ── 8. Kiosk autostart ────────────────────────────────────────────────────────
echo ""
echo "[8/8] Configuring kiosk autostart..."
mkdir -p /home/pi/.config/autostart
cat > /home/pi/.config/autostart/kiosk.desktop <<EOF
[Desktop Entry]
Type=Application
Name=SongRecPi Kiosk
Exec=chromium --kiosk --noerrdialogs --disable-infobars --no-first-run --disable-translate --check-for-update-interval=31536000 http://localhost:5000
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
