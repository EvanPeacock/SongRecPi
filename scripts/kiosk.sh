#!/bin/bash
# kiosk.sh
#
# Starts Chromium in kiosk mode on the SongRecPi page.
#   Desktop: launched by ~/.config/autostart/songrecpi-kiosk.desktop
#   Lite:    launched by `startx /opt/songrecpi/kiosk.sh --lite` from ~/.bash_profile
#
# Waits for the Flask app before opening the page, and restarts Chromium if it exits.

set -u

URL=${KIOSK_URL:-http://127.0.0.1:5000}
LITE=0
[ "${1:-}" = "--lite" ] && LITE=1

LOG="$HOME/.cache/songrecpi-kiosk.log"
mkdir -p "$HOME/.cache"
exec >"$LOG" 2>&1
echo "kiosk.sh starting (lite=$LITE) at $(date)"

CHROMIUM=$(command -v chromium-browser || command -v chromium)
if [ -z "$CHROMIUM" ]; then
    echo "Chromium not found." >&2
    exit 1
fi

# Screen blanking / cursor (X11 only; on Wayland use raspi-config's Screen Blanking option)
if [ -z "${WAYLAND_DISPLAY:-}" ] && command -v xset >/dev/null; then
    xset s off
    xset s noblank
    xset -dpms
fi
if [ "$LITE" = 0 ] && [ -z "${WAYLAND_DISPLAY:-}" ] && command -v unclutter >/dev/null; then
    if ! pgrep -u "$USER" -x unclutter >/dev/null; then
        unclutter -idle 0.1 -root &
    fi
fi

# Without a window manager (Lite), Chromium needs to be told the screen size.
SIZE_ARGS=()
if [ "$LITE" = 1 ]; then
    size=$(xrandr --current 2>/dev/null | sed -n 's/.* current \([0-9]*\) x \([0-9]*\),.*/\1,\2/p')
    # shellcheck disable=SC2054  # the commas are part of the flag values
    SIZE_ARGS=(--window-position=0,0 "--window-size=${size:-480,320}")
fi

for _ in $(seq 1 90); do
    curl -sf -o /dev/null "$URL/current" && break
    sleep 1
done

PREFS="$HOME/.config/chromium/Default/Preferences"
while true; do
    # A killed Chromium otherwise shows a "Restore pages?" bubble on the next start.
    if [ -f "$PREFS" ]; then
        sed -i 's/"exited_cleanly":false/"exited_cleanly":true/; s/"exit_type":"[^"]*"/"exit_type":"Normal"/' "$PREFS"
    fi
    "$CHROMIUM" \
        --kiosk --app="$URL" \
        "${SIZE_ARGS[@]}" \
        --force-device-scale-factor=1 \
        --noerrdialogs --disable-infobars --no-first-run \
        --disable-session-crashed-bubble \
        --disable-features=Translate \
        --overscroll-history-navigation=0 --disable-pinch \
        --check-for-update-interval=31536000
    echo "Chromium exited ($?) at $(date); restarting in 3s"
    sleep 3
done
