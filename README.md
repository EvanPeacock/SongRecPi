# SongRecPi 🎵

A Raspberry Pi kiosk that continuously listens to ambient audio and displays the currently playing song title, artist, and album art on an attached LCD screen.

## Hardware

- Raspberry Pi (tested on Pi 4)
- GeeekPi 3.5" TFT Touchscreen LCD ([Amazon](https://amazon.com/GeeekPi-Raspberry-Touchscreen-Heatsinks-320x480/dp/B083C12N57/)) 
- USB microphone

## How It Works

- [SongRec](https://github.com/marin-m/songrec) runs continuously in the background, piping Shazam recognition results as JSON
- A Flask app reads the JSON stream and serves a kiosk webpage at `localhost:5000`
- Chromium launches in kiosk mode pointing at that page
- Album art is fetched from Shazam's CDN and the background gradient adapts to the dominant color of the art

## WiFi Setup (for gifting / new installs)

If the Pi boots without a known WiFi network saved, it automatically broadcasts a hotspot:

| Field | Value |
|---|---|
| **SSID** | `SongRecPi` |
| **Password** | `songrecpi` |
| **Portal URL** | `http://10.42.0.1` (or auto-popup) |

Connect to the hotspot on your phone or laptop — a captive portal should pop up automatically (like a coffee shop login page). Pick your network, enter your password, and the Pi will connect and drop the hotspot. Credentials are saved permanently and it will auto-connect on every future boot.

## Fresh Install

Clone this repo onto a fresh Raspberry Pi OS Bullseye (32-bit) install and run:

```bash
bash install.sh
```

Then reboot:

```bash
sudo reboot
```

> **Note:** SongRec is compiled from source via Cargo, so the first install takes a while (20–40 minutes ish)


## File Structure

```
songrecpi/
├── app.py                        # Flask app + SongRec worker + kiosk HTML
├── requirements.txt              # Python dependencies
├── install.sh                    # Fresh install script
├── scripts/
│   ├── wifi-portal.py            # Captive portal web server
│   └── wifi-portal-start.sh     # Boot wrapper: checks connection, starts hotspot
└── services/
    ├── songrec.service           # Systemd service for the Flask app
    └── wifi-portal.service       # Systemd service for the WiFi portal
```

## Environment Variables

These can be set in `songrec.service` under `[Service]` if you need to override defaults:

| Variable | Default | Description |
|---|---|---|
| `SONGREC_PATH` | `/home/pi/.cargo/bin/songrec` | Path to SongRec binary |
| `HOST` | `0.0.0.0` | Flask bind address |
| `PORT` | `5000` | Flask port |
| `POLL_INTERVAL` | `1.0` | How often the UI polls for track updates (seconds) |

## Troubleshooting

**Song not updating / blank screen**
```bash
sudo systemctl status songrec
journalctl -u songrec -f
```

**WiFi portal not appearing**
```bash
sudo systemctl status wifi-portal
journalctl -u wifi-portal --no-pager
```
If already connected but portal ran anyway, check that NetworkManager settled before the 20s timeout in `wifi-portal-start.sh` — increase the sleep if needed.

**Manually connect to WiFi via keyboard**
```bash
nmcli dev wifi list
sudo nmcli dev wifi connect "NetworkName" password "password"
```
