# SongRecPi 🎵

A Raspberry Pi kiosk that continuously listens to ambient audio and displays the currently playing song title, artist, and album art on an attached LCD screen.

## Hardware

- Raspberry Pi (tested on Pi 4)
- GeeekPi 3.5" TFT Touchscreen LCD ([Amazon](https://amazon.com/GeeekPi-Raspberry-Touchscreen-Heatsinks-320x480/dp/B083C12N57/))
- USB microphone

## How It Works

- [SongRec](https://github.com/marin-m/songrec) runs continuously in the background, piping Shazam recognition results as JSON
- A Flask app (`songrec.service`, running as your user) reads the JSON stream and serves a kiosk webpage at `127.0.0.1:5000`
- Chromium launches in kiosk mode pointing at that page (`/opt/songrecpi/kiosk.sh`)
- Album art is fetched from Shazam's CDN and the background gradient adapts to the dominant color of the art
- After 10 minutes without a new recognition the screen fades to a quiet "Listening…" view

## Supported OS

Raspberry Pi OS **Bullseye** or **Trixie**, 32- or 64-bit, **desktop or Lite**:

- **Desktop**: the kiosk starts from the desktop session's autostart.
- **Lite**: no desktop is installed. The Pi auto-logs in on the console and starts a bare X server running only Chromium (`kiosk-x.sh`). The installer picks Lite automatically when the Pi doesn't boot to a graphical session; force either with `--desktop` / `--lite`.

> Lite + SPI LCD: if `fbcp` mirrors HDMI to the panel, Xorg uses the default framebuffer; otherwise `kiosk-x.sh` points Xorg's fbdev driver at the panel (`/dev/fbN` named `fb_*`). Check the screen on the first Lite boot.

## WiFi Setup (for gifting / new installs)

If the Pi boots without a known WiFi network saved, it broadcasts a setup hotspot and **the screen shows two QR codes**:

1. **Scan to join**: joins the `SongRecPi` hotspot (password `songrecpi`).
2. **Scan to set up**: opens the setup page at `http://10.42.0.1`, if the captive-portal popup didn't appear on its own.

Pick your network (or "Other (hidden network)"), enter the password, and the Pi connects and drops the hotspot. If the password is wrong, both the screen and the setup page say so and the hotspot comes back so you can try again. Credentials are saved permanently and it will auto-connect on every future boot.

The hotspot name and password live in `/etc/songrecpi.conf` (used by both the hotspot and the on-screen QR code).

## Fresh Install

Clone this repo onto the Pi and run the installer as your normal user (not with `sudo`):

```bash
git clone https://github.com/EvanPeacock/SongRecPi.git ~/SongRecPi
cd ~/SongRecPi
bash install.sh
sudo reboot
```

> **Note:** If SongRec isn't installed yet it's compiled from source via Cargo (CLI only), which takes 20–40 minutes and needs about 1.5 GB free. An existing `~/.cargo/bin/songrec` is reused.

> **Bullseye:** the WiFi setup hotspot needs NetworkManager. If `nmcli` isn't managing WiFi, switch with `sudo raspi-config` → Advanced Options → Network Config → NetworkManager.

## Updating

From the clone on the Pi:

```bash
cd ~/SongRecPi
bash update.sh
```

This pulls the latest code (`git pull --ff-only`), redeploys the app to `/opt/songrecpi`, re-renders the systemd units, restarts the app and relaunches the kiosk. No reboot needed. Options:

| Flag | Effect |
|---|---|
| `--songrec` | Also rebuild SongRec from source |
| `--system` | Also run `apt full-upgrade` first |

`update.sh` also migrates an older install: the app moves from `~/app.py` (kept as `~/app.py.old`) to `/opt/songrecpi`, `songrec.service` stops running as root, old kiosk autostart entries are renamed to `*.desktop.old`, and the old weekly crontab line moves to `/etc/cron.weekly/songrecpi`.

Preview what either script would do with `DRY_RUN=1 bash update.sh`.

## File Structure

```
songrecpi/
├── app.py                        # Flask app + SongRec worker + kiosk HTML + setup QR codes
├── install.sh                    # Installer (fresh install, and --update used by update.sh)
├── update.sh                     # git pull + redeploy on an existing Pi
├── requirements.txt              # Python dependencies (normally installed via apt)
├── config/
│   └── songrecpi.conf            # Hotspot settings → /etc/songrecpi.conf
├── scripts/
│   ├── kiosk.sh                  # Waits for the app, then runs Chromium in kiosk mode
│   ├── kiosk-x.sh                # Lite only: starts a bare X server for kiosk.sh
│   ├── wifi-portal.py            # Captive portal web server
│   └── wifi-portal-start.sh      # Boot wrapper: checks connection, starts hotspot
└── services/
    ├── songrec.service           # Systemd unit template for the Flask app
    └── wifi-portal.service       # Systemd unit template for the WiFi portal
```

## Environment Variables

Set these in `/etc/default/songrecpi` (one `NAME=value` per line) and run `sudo systemctl restart songrec`:

| Variable | Default | Description |
|---|---|---|
| `SONGREC_PATH` | detected by the installer | Path to SongRec binary |
| `HOST` | `127.0.0.1` | Flask bind address (`0.0.0.0` to view from other devices) |
| `PORT` | `5000` | Flask port |
| `POLL_INTERVAL` | `1.0` | How often the UI polls for track updates (seconds) |
| `IDLE_TIMEOUT` | `600` | Seconds without a new recognition before the "Listening…" view; `0` disables |

## Troubleshooting

**Song not updating / blank screen**
```bash
sudo systemctl status songrec
journalctl -u songrec -f
cat ~/.cache/songrecpi-kiosk.log
```

**WiFi portal not appearing**
```bash
sudo systemctl status wifi-portal
journalctl -u wifi-portal --no-pager
```
The portal waits up to 60s for NetworkManager to connect before starting the hotspot.

**Screen blanks after a while (Wayland desktop on Trixie)**
`kiosk.sh` disables blanking on X11. On a Wayland desktop use `sudo raspi-config` → Display Options → Screen Blanking → No.

**Manually connect to WiFi via keyboard**
```bash
nmcli dev wifi list
sudo nmcli dev wifi connect "NetworkName" password "password"
```
