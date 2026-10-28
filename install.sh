#!/bin/bash
# install.sh
#
# Install script for SongRecPi on Raspberry Pi OS Bullseye or Trixie,
# desktop or Lite (32- or 64-bit).
# Tested on a GeeekPi 3.5" TFT LCD (tft35a driver, 320x480).
#
# Run as your normal user from the repo clone (the script will sudo where needed):
#   bash install.sh [--desktop | --lite] [--songrec]
#
#   --desktop / --lite   Kiosk mode; auto-detected (desktop if the Pi boots to a
#                        graphical session, otherwise Lite).
#   --songrec            Rebuild songrec even if a binary already exists.
#   --update             Deploy only (used by update.sh): copy files, render units,
#                        migrate an old install, restart the app and kiosk. Skips
#                        the LCD overlay and boot-behaviour changes.
#
# DRY_RUN=1 prints the privileged / state-changing commands instead of running them.

set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR=/opt/songrecpi
DRY_RUN=${DRY_RUN:-0}

MODE=""
UPDATE=0
BUILD_SONGREC=0
for arg in "$@"; do
    case "$arg" in
        --desktop) MODE=desktop ;;
        --lite)    MODE=lite ;;
        --update)  UPDATE=1 ;;
        --songrec) BUILD_SONGREC=1 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as your normal user (e.g. pi), not with sudo; it uses sudo itself." >&2
    exit 1
fi
RUN_USER=$(id -un)
RUN_HOME=$(getent passwd "$RUN_USER" | cut -d: -f6)
RUN_UID=$(id -u "$RUN_USER")

# ── helpers ───────────────────────────────────────────────────────────────────
run() {
    if [ "$DRY_RUN" = 1 ]; then echo "  + $*"; else "$@"; fi
}
as_root() { run sudo "$@"; }
# put_file DEST MODE OWNER: write stdin to DEST (only if the content changed).
# Returns 0 if the file was (or would be) changed, 1 if it was already up to date.
put_file() {
    local dest=$1 mode=$2 owner=$3 tmp
    tmp=$(mktemp)
    cat > "$tmp"
    if [ -f "$dest" ] && cmp -s "$tmp" "$dest"; then
        rm -f "$tmp"
        return 1
    fi
    if [ "$DRY_RUN" = 1 ]; then
        echo "  + write $dest ($mode $owner)"
    elif [ "$owner" = root ]; then
        sudo install -D -m "$mode" -o root -g root "$tmp" "$dest"
    else
        install -D -m "$mode" "$tmp" "$dest"
    fi
    rm -f "$tmp"
}
step() { echo ""; echo "[$1] $2"; }
avail_mb() { df -Pm / | awk 'NR==2 {print $4}'; }
pkg_installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"; }
has_candidate() { apt-cache policy "$1" 2>/dev/null | grep -q 'Candidate: [0-9]'; }

# ── detection ─────────────────────────────────────────────────────────────────
if [ -z "$MODE" ]; then
    if grep -qs 'songrecpi kiosk' "$RUN_HOME/.bash_profile"; then
        MODE=lite
    elif [ "$(systemctl get-default 2>/dev/null)" = graphical.target ] ||
         [ -e /etc/systemd/system/display-manager.service ]; then
        MODE=desktop
    else
        MODE=lite
    fi
fi
if [ -f /boot/firmware/config.txt ]; then
    BOOT_CONFIG=/boot/firmware/config.txt   # Bookworm / Trixie
else
    BOOT_CONFIG=/boot/config.txt            # Bullseye and older
fi
# shellcheck disable=SC1091
. /etc/os-release 2>/dev/null || true

echo "================================================"
echo "  SongRecPi $([ "$UPDATE" = 1 ] && echo Update || echo Installer)"
echo "  OS: ${PRETTY_NAME:-unknown}   mode: $MODE   user: $RUN_USER"
echo "  Free space on /: $(avail_mb) MB"
[ "$DRY_RUN" = 1 ] && echo "  DRY RUN: nothing will be changed"
echo "================================================"

# ── 1. System packages ────────────────────────────────────────────────────────
step 1/9 "System packages"
PKGS=(python3-flask python3-qrcode network-manager dnsmasq-base curl git)
if ! command -v chromium-browser >/dev/null && ! command -v chromium >/dev/null; then
    if has_candidate chromium-browser; then PKGS+=(chromium-browser); else PKGS+=(chromium); fi
fi
if [ "$MODE" = lite ]; then
    PKGS+=(xserver-xorg xserver-xorg-video-fbdev xinit x11-xserver-utils)
else
    PKGS+=(unclutter)
fi
MISSING=()
for p in "${PKGS[@]}"; do pkg_installed "$p" || MISSING+=("$p"); done
if [ "${#MISSING[@]}" -gt 0 ]; then
    echo "Installing: ${MISSING[*]}"
    if [ "$(avail_mb)" -lt 300 ]; then
        echo "Only $(avail_mb) MB free on /; free some space first (need ~300 MB headroom)." >&2
        exit 1
    fi
    as_root apt-get update -q
    as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends "${MISSING[@]}"
    as_root apt-get clean
else
    echo "All packages present."
fi
if ! python3 -c "import flask" 2>/dev/null; then
    # Only reached if apt's python3-flask is unavailable.
    PIP_ARGS=()
    pip3 install --help 2>/dev/null | grep -q break-system-packages && PIP_ARGS=(--break-system-packages)
    run pip3 install --user "${PIP_ARGS[@]}" -r "$REPO_DIR/requirements.txt"
fi
if [ "$(systemctl is-active NetworkManager 2>/dev/null)" != active ]; then
    echo "WARNING: NetworkManager isn't running, so the WiFi setup hotspot won't work."
    echo "         On Bullseye switch to it with: sudo raspi-config → Advanced → Network Config."
fi

# ── 2. SongRec ────────────────────────────────────────────────────────────────
step 2/9 "SongRec"
SONGREC_PATH=""
for c in "${SONGREC_PATH_OVERRIDE:-}" "$RUN_HOME/.cargo/bin/songrec" /usr/local/bin/songrec "$(command -v songrec || true)"; do
    if [ -n "$c" ] && [ -x "$c" ]; then SONGREC_PATH=$c; break; fi
done
if [ -n "$SONGREC_PATH" ] && [ "$BUILD_SONGREC" = 0 ]; then
    echo "Using existing songrec: $SONGREC_PATH ($("$SONGREC_PATH" --version 2>/dev/null || echo '?'))"
else
    if [ "$(avail_mb)" -lt 1500 ]; then
        echo "Building songrec needs ~1.5 GB free on / (have $(avail_mb) MB)." >&2
        exit 1
    fi
    echo "Building songrec (CLI only) from source; this takes 20-40 minutes on a Pi..."
    as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends build-essential pkg-config \
        libasound2-dev libssl-dev libglib2.0-dev gettext
    if ! command -v cargo >/dev/null && [ ! -x "$RUN_HOME/.cargo/bin/cargo" ]; then
        if [ "$DRY_RUN" = 1 ]; then echo "  + install rustup"
        else curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal; fi
    fi
    run "$RUN_HOME/.cargo/bin/cargo" install --force --locked songrec --no-default-features
    SONGREC_PATH="$RUN_HOME/.cargo/bin/songrec"
fi

# ── 3. LCD display driver (fresh install only) ────────────────────────────────
step 3/9 "LCD display (tft35a, rotate=270)"
if [ "$UPDATE" = 1 ]; then
    echo "Update mode: leaving $BOOT_CONFIG alone."
elif grep -qs "dtoverlay=tft35a" "$BOOT_CONFIG"; then
    echo "LCD overlay already present in $BOOT_CONFIG, skipping."
else
    if [ "$DRY_RUN" = 1 ]; then echo "  + append dtoverlay=tft35a:rotate=270 to $BOOT_CONFIG"
    else echo "dtoverlay=tft35a:rotate=270" | sudo tee -a "$BOOT_CONFIG" >/dev/null; fi
    echo "LCD overlay added to $BOOT_CONFIG"
fi

# ── 4. App files ──────────────────────────────────────────────────────────────
step 4/9 "App files → $APP_DIR"
for f in app.py scripts/wifi-portal.py scripts/wifi-portal-start.sh scripts/kiosk.sh scripts/kiosk-x.sh; do
    put_file "$APP_DIR/$(basename "$f")" 755 root < "$REPO_DIR/$f" && echo "  updated $(basename "$f")" || true
done
if [ ! -f /etc/songrecpi.conf ]; then
    put_file /etc/songrecpi.conf 644 root < "$REPO_DIR/config/songrecpi.conf" || true
    echo "  created /etc/songrecpi.conf"
else
    echo "  keeping existing /etc/songrecpi.conf"
fi

# ── 5. Systemd services ───────────────────────────────────────────────────────
step 5/9 "Systemd services"
render() {
    sed -e "s|@USER@|$RUN_USER|g" -e "s|@HOME@|$RUN_HOME|g" \
        -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@SONGREC_PATH@|$SONGREC_PATH|g" "$1"
}
UNITS_CHANGED=0
for u in songrec wifi-portal; do
    if render "$REPO_DIR/services/$u.service" | put_file "/etc/systemd/system/$u.service" 644 root; then
        echo "  updated $u.service"; UNITS_CHANGED=1
    fi
done
[ "$UNITS_CHANGED" = 1 ] && as_root systemctl daemon-reload
as_root systemctl enable --quiet songrec wifi-portal

# ── 6. Captive portal DNS ─────────────────────────────────────────────────────
step 6/9 "Captive portal DNS"
DNSMASQ_DIR=/etc/NetworkManager/dnsmasq-shared.d
echo "address=/#/10.42.0.1" | put_file "$DNSMASQ_DIR/songrecpi-captive.conf" 644 root || true
# An older hand-made copy of the same rule; drop it so there's a single source.
if [ "$(cat "$DNSMASQ_DIR/captive-portal.conf" 2>/dev/null)" = "address=/#/10.42.0.1" ]; then
    as_root rm -f "$DNSMASQ_DIR/captive-portal.conf"
    echo "  removed duplicate $DNSMASQ_DIR/captive-portal.conf"
fi

# ── 7. Storage management ─────────────────────────────────────────────────────
step 7/9 "Storage management"
# Cap systemd journal to 50MB total and 10MB per file
if put_file /etc/systemd/journald.conf.d/size.conf 644 root <<EOF
[Journal]
SystemMaxUse=50M
SystemMaxFileSize=10M
RuntimeMaxUse=20M
EOF
then echo "  capped the journal at 50 MB"; as_root systemctl restart systemd-journald; fi

# Weekly cleanup of the apt cache and temp files
put_file /etc/cron.weekly/songrecpi 755 root <<'EOF' && echo "  wrote /etc/cron.weekly/songrecpi" || true
#!/bin/sh
# SongRecPi weekly cleanup (installed by install.sh)
apt-get clean
rm -f /tmp/*.wav /tmp/wifi-networks.txt
EOF
[ -e /etc/logrotate.d/songrec ] && as_root rm -f /etc/logrotate.d/songrec

# ── 8. Migrate an older install ───────────────────────────────────────────────
step 8/9 "Migrating old install (if any)"
if crontab -l 2>/dev/null | grep -q 'wifi-networks.txt'; then
    if [ "$DRY_RUN" = 1 ]; then echo "  + remove old cleanup line from user crontab"
    else crontab -l | grep -v 'wifi-networks.txt' | crontab -; fi
    echo "  removed old weekly line from the user crontab"
fi
if [ -f "$RUN_HOME/app.py" ] && [ ! -L "$RUN_HOME/app.py" ]; then
    run mv "$RUN_HOME/app.py" "$RUN_HOME/app.py.old"
    echo "  moved ~/app.py to ~/app.py.old (now runs from $APP_DIR)"
fi
for old in /usr/local/bin/wifi-portal.py /usr/local/bin/wifi-portal-start.sh; do
    [ -e "$old" ] && as_root rm -f "$old" && echo "  removed $old"
done

# ── 9. Kiosk autostart ────────────────────────────────────────────────────────
step 9/9 "Kiosk autostart ($MODE)"
AUTOSTART="$RUN_HOME/.config/autostart"
PROFILE="$RUN_HOME/.bash_profile"
MARK_BEGIN="# >>> songrecpi kiosk >>>"
MARK_END="# <<< songrecpi kiosk <<<"

# Retire older kiosk/unclutter autostart entries (kept as *.desktop.old).
for f in "$AUTOSTART"/*.desktop; do
    [ -f "$f" ] || continue
    [ "$(basename "$f")" = songrecpi-kiosk.desktop ] && continue
    if grep -qE 'localhost:5000|127\.0\.0\.1:5000|^Exec=unclutter' "$f"; then
        run mv "$f" "$f.old"
        echo "  retired $(basename "$f")"
    fi
done

strip_profile_block() {
    [ -f "$PROFILE" ] && grep -qF "$MARK_BEGIN" "$PROFILE" || return 0
    if [ "$DRY_RUN" = 1 ]; then echo "  + remove kiosk block from $PROFILE"; return 0; fi
    sed -i "/$MARK_BEGIN/,/$MARK_END/d" "$PROFILE"
}

if [ "$MODE" = desktop ]; then
    strip_profile_block
    put_file "$AUTOSTART/songrecpi-kiosk.desktop" 644 "$RUN_USER" <<EOF && echo "  wrote songrecpi-kiosk.desktop" || true
[Desktop Entry]
Type=Application
Name=SongRecPi Kiosk
Exec=$APP_DIR/kiosk.sh
StartupNotify=false
Terminal=false
X-GNOME-Autostart-enabled=true
EOF
else
    [ -f "$AUTOSTART/songrecpi-kiosk.desktop" ] && run rm -f "$AUTOSTART/songrecpi-kiosk.desktop"
    # Xorg configs for an fbtft SPI panel; kiosk-x.sh picks the right one at boot.
    for n in 0 1 2; do
        put_file "/etc/X11/songrecpi-fb$n.conf" 644 root <<EOF || true
# SongRecPi: drive the SPI LCD (/dev/fb$n) with Xorg's fbdev driver (see kiosk-x.sh)
Section "Device"
    Identifier "SongRecPi LCD"
    Driver "fbdev"
    Option "fbdev" "/dev/fb$n"
EndSection
EOF
    done
    if ! grep -qsF "$MARK_BEGIN" "$PROFILE"; then
        if [ "$DRY_RUN" = 1 ]; then echo "  + add kiosk block to $PROFILE"
        else
            # A new ~/.bash_profile stops bash reading ~/.profile, so source it.
            [ -f "$PROFILE" ] || echo '[ -f ~/.profile ] && . ~/.profile' > "$PROFILE"
            cat >> "$PROFILE" <<EOF
$MARK_BEGIN
if [ -z "\${DISPLAY:-}" ] && [ "\$(tty)" = /dev/tty1 ]; then
    exec $APP_DIR/kiosk-x.sh
fi
$MARK_END
EOF
        fi
        echo "  added kiosk launcher to ~/.bash_profile (tty1 only)"
    fi
    if [ "$UPDATE" = 0 ] && command -v raspi-config >/dev/null; then
        as_root raspi-config nonint do_boot_behaviour B2   # console autologin
    fi
fi

# ── Restart (update mode) ─────────────────────────────────────────────────────
if [ "$UPDATE" = 1 ]; then
    echo ""
    echo "Restarting songrec..."
    as_root systemctl restart songrec
    if [ "$DRY_RUN" = 0 ]; then
        for _ in $(seq 1 20); do curl -sf -o /dev/null http://127.0.0.1:5000/current && break; sleep 1; done
        if curl -sf -o /dev/null http://127.0.0.1:5000/current; then echo "  app is up"
        else echo "  WARNING: app not answering yet; check: journalctl -u songrec -n 50" >&2; fi
    fi

    echo "Restarting kiosk..."
    if [ "$MODE" = lite ]; then
        as_root systemctl restart getty@tty1
    elif pgrep -u "$RUN_USER" -f 'lxsession|labwc|wayfire' >/dev/null; then
        KIOSK_ENV=(DISPLAY=:0 XAUTHORITY="$RUN_HOME/.Xauthority")
        if pgrep -u "$RUN_USER" -x 'labwc|wayfire' >/dev/null; then
            KIOSK_ENV+=(WAYLAND_DISPLAY=wayland-0 XDG_RUNTIME_DIR="/run/user/$RUN_UID")
        fi
        # Patterns are anchored to the executable so they can't match some other
        # shell whose command line merely mentions kiosk.sh or Chromium.
        run pkill -u "$RUN_USER" -f "^(/usr)?/bin/bash $APP_DIR/kiosk\.sh" || true
        run pkill -u "$RUN_USER" -f '^[^ ]*chromium[^ ]* .*--app=http://(localhost|127\.0\.0\.1):5000' || true
        [ "$DRY_RUN" = 0 ] && sleep 2
        if [ "$DRY_RUN" = 1 ]; then echo "  + ${KIOSK_ENV[*]} setsid $APP_DIR/kiosk.sh &"
        else env "${KIOSK_ENV[@]}" setsid "$APP_DIR/kiosk.sh" </dev/null >/dev/null 2>&1 & fi
        echo "  kiosk relaunched"
    else
        echo "  no desktop session running; the kiosk starts on next login."
    fi
    echo ""
    echo "Update complete."
    exit 0
fi

echo ""
echo "================================================"
echo "  Installation complete!"
echo ""
echo "  On first boot without a known WiFi network the screen shows two"
echo "  QR codes: scan the first to join the setup hotspot, the second to"
# shellcheck disable=SC1091
echo "  open the setup page (or join $(. /etc/songrecpi.conf 2>/dev/null; echo "${HOTSPOT_SSID:-SongRecPi}") and open http://10.42.0.1)."
echo ""
echo "  Reboot now to start everything:"
echo "    sudo reboot"
echo "================================================"
