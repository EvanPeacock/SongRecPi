# SongRecPi — Fix review findings + Lite support, on-screen WiFi QR, update script

## Context
After reviewing the code, the user asked for (a) all review fixes, (b) support for Raspberry Pi OS **Lite**/headless (they currently run the full desktop), (c) a **QR code on the screen** during WiFi setup, and (d) an **update script** for their existing Pi. Work goes on a feature branch, gets committed and pushed; no PR unless asked.

## 1. Review fixes (as previously listed)
**app.py**
- Signal handler ends with `sys.exit(0)` so Flask stops (today: 10s hang → SIGKILL).
- Worker robustness: per-line try/except, `encoding="utf-8", errors="replace"`, outer try/except so the thread can't die silently; backoff resets after a run > 60s; `stop_event.wait()` instead of `time.sleep`.
- `HOST` default `127.0.0.1`. Fix the docstring (no "display power management").
- Front-end: `setTimeout` chain + `AbortController` timeout instead of `setInterval`; quoted CSS `url("…")`; reset gradient when no art; ignore stale color-extraction loads.
- Optional idle state: after `IDLE_TIMEOUT` (default 10 min, env var) with no new recognition, dim/fade to an "Listening…" view.

**scripts/wifi-portal.py**
- Parse nmcli `-t` output with escaped colons; `html.escape` SSIDs; guard missing ssid; omit `password` for open networks; add "Other (hidden network)" text field.
- Check nmcli result: on failure delete the failed profile, restart the hotspot, write status "wrong password" (shown on the portal page and on-screen); on success exit the process so port 80 isn't left open on the home LAN.

**scripts/wifi-portal-start.sh**
- Replace `sleep 20` + `^connected$` with a poll (up to ~60s) treating any `connected*` state as connected; `set -u`; abort if hotspot fails to start; name the hotspot connection explicitly (`con-name SongRecPi-Hotspot`) and take it down by name.
- Write setup-state file for the kiosk (see §3).

**Services / installer**
- `songrec.service`: `User=` the installing user, `Restart=always`, drop redundant `ExecStop`, paths templated.
- DNS hijack for captive popup: `/etc/NetworkManager/dnsmasq-shared.d/songrecpi-captive.conf` (`address=/#/10.42.0.1`).
- `cargo install songrec --no-default-features` (CLI only); add `pkg-config build-essential` to apt.
- No hardcoded `/home/pi`: use `$USER`/`$HOME`; app + scripts installed to `/opt/songrecpi`, units rendered from templates with `sed`.
- Kiosk launcher `/opt/songrecpi/kiosk.sh` waits for `curl -sf http://127.0.0.1:5000/current` before starting Chromium; adds `--disable-features=Translate`, and disables screen blanking (`xset s off -dpms` on X11 / note for labwc).
- Weekly cleanup moves to `/etc/cron.weekly/songrecpi`; remove the old user-crontab line if present. Remove dead logrotate config and the swap-disable step.
- README: correct OS (Trixie, desktop or Lite), new install/update docs; add `.gitignore`, delete `.DS_Store`; pin `flask` loosely (`flask>=2.2,<4`).

## 2. Lite / headless support
Yes, it's installable on Lite — Chromium just needs a minimal display server instead of the desktop session. Plan:
- `install.sh` auto-detects mode: desktop if a display manager/`graphical.target` is present (keep today's XDG autostart, pointing at `kiosk.sh`), otherwise Lite. Overridable with `--desktop` / `--lite`.
- **Lite path**: apt install `xserver-xorg xinit x11-xserver-utils chromium` (no WM needed for `--kiosk`); console autologin on tty1 via `raspi-config nonint do_boot_behaviour B2`; `~/.bash_profile` snippet `[ "$(tty)" = /dev/tty1 ] && exec startx /opt/songrecpi/kiosk.sh -- -nocursor`. If the SPI panel shows up as `/dev/fb1` (fbtft tft35a), write `/etc/X11/xorg.conf.d/99-songrecpi-fbdev.conf` pointing Xorg's fbdev driver at it; otherwise Xorg's default modesetting is used.
- `-nocursor` replaces `unclutter` on Lite; on the desktop `unclutter` is kept only for X11 sessions.
- Caveat to tell the user: I can't test the SPI panel here, so the first Lite boot needs a real-device check.

## 3. On-screen WiFi setup QR code
- `wifi-portal-start.sh` / `wifi-portal.py` write `/run/songrecpi/setup.json` (`{"mode":"setup","status":"waiting|connecting|failed|connected","ssid":…}`), world-readable; removed on success.
- `app.py`: `/current` includes a `setup` field read from that file; new `/qr/wifi.svg` and `/qr/portal.svg` endpoints rendered **offline** with `python3-qrcode` (apt) as SVG:
  - QR 1: `WIFI:T:WPA;S:SongRecPi;P:songrecpi;;` — phone camera scan joins the hotspot.
  - QR 2: `http://10.42.0.1` — opens the portal if the popup doesn't appear.
- Kiosk UI switches to a setup view sized for 480×320: two QR codes with short captions ("1. Scan to join", "2. Scan to set up"), plus the status line ("Wrong password — try again", "Connecting…"). Returns to the song view automatically once the file disappears.
- Hotspot SSID/password move to one shared config (`/etc/songrecpi.conf`, sourced by the shell script, read by both Python apps) so the QR and hotspot can't drift.

## 4. Update script (`update.sh`)
- Run from the repo clone on the Pi: `bash update.sh`.
- Steps: `git pull --ff-only`; then `install.sh --update`, which runs only the idempotent deploy steps (copy files to `/opt/songrecpi`, render units, captive DNS conf, cron, kiosk launcher, migrations), `daemon-reload`, restart `songrec`, and restart the kiosk (desktop: relaunch chromium; Lite: restart getty/X).
- Flags: `--songrec` also rebuilds songrec (`cargo install --force songrec --no-default-features`); `--system` also runs `apt full-upgrade`.
- **Migration for the existing Pi**: removes the old user crontab line, replaces `~/.config/autostart/kiosk.desktop` to use `kiosk.sh`, points `songrec.service` at `/opt/songrecpi/app.py` (old `~/app.py` renamed to `~/app.py.old`), detects the existing songrec binary (`~/.cargo/bin/songrec`) instead of rebuilding.
- If the Pi copy isn't a git clone, the script says how to clone it.

## Files
Modify: `app.py`, `scripts/wifi-portal.py`, `scripts/wifi-portal-start.sh`, `services/songrec.service`, `services/wifi-portal.service`, `install.sh`, `README.md`, `requirements.txt`.
Add: `update.sh`, `scripts/kiosk.sh`, `config/songrecpi.conf`, `.gitignore`. Delete: `.DS_Store`.

## Verification (in this container)
- `python3 -m py_compile` both Python files; `bash -n` + `shellcheck` on all shell scripts.
- `app.py` with a fake `SONGREC_PATH` script emitting sample JSON, malformed lines, non-UTF-8 bytes and a crash → `/current` updates, worker survives, backoff resets, SIGINT exits < 1s.
- Create a fake `setup.json` → Playwright screenshot at 480×320 of the setup view (QR codes decode correctly via a QR decoder if available), then remove it → song view returns. Screenshot song view with long title.
- `wifi-portal.py` with a sample networks file (escaped colons, HTML-ish SSID, open network) and a stub `nmcli` on PATH for success/failure paths.
- `install.sh` / `update.sh`: run in a dry-run mode (`DRY_RUN=1` echoes privileged commands) to check flow for desktop and Lite.
- On-device (user): `bash update.sh` on the current Pi; boot with/without known WiFi; wrong-password retry; scan QR codes; optionally a fresh Lite install.
