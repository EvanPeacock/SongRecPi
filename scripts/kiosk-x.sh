#!/bin/bash
# kiosk-x.sh
#
# Lite only: started from ~/.bash_profile on tty1 (console autologin). Runs a bare
# X server (no desktop, no window manager) whose only client is kiosk.sh.
#
# Picks the framebuffer at boot rather than install time, because the SPI panel's
# /dev/fbN only exists once its overlay is loaded:
#   - fbcp running/configured: it mirrors fb0 to the panel, so use Xorg's default.
#   - an fbtft panel (name "fb_*") found: use /etc/X11/songrecpi-fbN.conf for it.
#   - otherwise: Xorg's default (modesetting / HDMI).

set -u

server_args=(-nocursor)
if ! pgrep -x fbcp >/dev/null && ! grep -qs '^[^#]*fbcp' /etc/rc.local; then
    for fb in /sys/class/graphics/fb*; do
        [ -r "$fb/name" ] || continue
        case "$(cat "$fb/name")" in
            fb_*)
                conf="songrecpi-${fb##*/}.conf"
                [ -r "/etc/X11/$conf" ] && server_args+=(-config "$conf")
                break
                ;;
        esac
    done
fi

exec startx /opt/songrecpi/kiosk.sh --lite -- "${server_args[@]}"
