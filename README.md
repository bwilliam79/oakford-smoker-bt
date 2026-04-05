# BT Smoker Monitor

A local web dashboard for monitoring a Nexgrill Bluetooth smoker in real time. A Python backend polls the smoker via BLE every N seconds and pushes live temperature data to any browser via WebSocket — no cloud, no app required.

![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue) ![License: MIT](https://img.shields.io/badge/license-MIT-green)

---

## Features

- **Auto-discovery** — finds the smoker by scanning for BLE devices with the NXE prefix; retries automatically when out of range
- **Live temperature dashboard** — smoker temp, up to 2 meat probes
- **Trending chart** — 24-hour scrolling history of all temps with target lines
- **Server-side ETA** — estimated time remaining per probe, computed server-side via linear regression over the full probe history so all connected clients see the same value instantly
- **Stall detection** — detects when a probe temp stalls (< 2°F movement in 20 min), excludes the stall period from the regression to keep the rate accurate, and shows *"In stall – ETA paused"*
- **At / Over / Under temperature alerts** — visual indicators with colour coding and pulsing animation
- **Audible alarms** — siren when a probe exceeds its target; voice announcement when a probe reaches its target
- **Push notifications** — optional ntfy.sh integration for probe at temp, probe over temp, grill at temp, grill over temp, smoker connected, and smoker disconnected events
- **Smoker at-temperature timer** — shows how long the smoker has held its set temperature
- **Offline detection** — dashboard reflects when the smoker is unreachable; cards hide automatically
- **Multi-client** — open the dashboard in multiple browsers simultaneously; all receive the same server-computed state
- **In-app settings** — gear icon (⚙️) opens a settings modal to configure the ntfy.sh topic and select the Bluetooth adapter; adapter changes restart the service automatically
- **Runs as a Docker container or Linux systemd service**

---

## Requirements

- Python 3.12+ **or** Docker
- A Bluetooth adapter (built-in or USB)
- Nexgrill smoker (tested with NXE-13CB970)
- Any modern browser for the dashboard

---

## Docker (recommended)

Pre-built images for `amd64`, `arm64`, and `armv7` are published to GitHub Container Registry on every push.

### Run

```bash
docker run -d \
  --name smoker \
  --restart unless-stopped \
  --net=host \
  --privileged \
  -v /var/run/dbus:/var/run/dbus \
  -v /path/to/smoker-data:/data \
  ghcr.io/bwilliam79/bt-smoker-monitor:latest \
  --port 8080
```

Then open **http://\<host-ip\>:8080** in any browser.

> **Tip:** Replace `/path/to/smoker-data` with a directory on your host (e.g. `/home/user/docker/smoker-data`). This is where the app persists its configuration (ntfy topic, selected adapter) across container restarts.

### Bluetooth flags explained

| Flag | Why it's needed |
|------|----------------|
| `--net=host` | Lets Bleak scan for BLE advertisements on the host's radio |
| `--privileged` | Grants the container access to host Bluetooth hardware |
| `-v /var/run/dbus:/var/run/dbus` | Gives the container access to the host's `bluetoothd` via D-Bus |
| `-v ...:/data` | Persists app configuration (ntfy topic, adapter) across restarts |

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--port` | `8080` | Web server port |
| `--interval` | `30` | BLE poll interval in seconds |
| `--address` | *(auto)* | Hardcode a BLE address to skip scanning |
| `--adapter` | *(auto)* | Bluetooth adapter to use (e.g. `hci1`). Overridden by the value saved in `/data/config.json` |
| `--ntfy-topic` | *(none)* | ntfy.sh topic for push notifications. Can also be set via `NTFY_TOPIC` env var or the in-app settings UI |
| `--debug` | off | Enable verbose poll logging |

---

## Configuration (in-app settings)

Click the **⚙️** icon in the top-right corner of the dashboard to open the settings modal.

| Setting | Description |
|---------|-------------|
| **Bluetooth Adapter** | Select from detected `hci*` adapters. Changing this will automatically restart the service to apply the new adapter. |
| **ntfy.sh Topic** | Push notification topic. Leave blank to disable. |

Settings are saved to `/data/config.json` inside the container and persist across restarts. The config file takes precedence over CLI flags and environment variables.

---

## Push Notifications

The app uses [ntfy.sh](https://ntfy.sh) — a free, open-source push notification service — to send alerts to your phone.

### Setup

1. Install the **ntfy** app on your phone ([iOS](https://apps.apple.com/app/ntfy/id1625641461) / [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy))
2. Subscribe to your chosen topic in the ntfy app
3. Enter the same topic in the app's settings modal (⚙️)

### Notification events

| Event | Priority |
|-------|----------|
| Probe at temperature | High |
| Probe over temperature | Urgent |
| Grill at set point | Default |
| Grill over temperature | Urgent |
| Smoker connected | Default |
| Smoker disconnected | Default |

---

## Manual Installation

### 1. Clone and install dependencies

```bash
git clone https://github.com/bwilliam79/bt-smoker-monitor.git
cd bt-smoker-monitor
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Run

```bash
python3 server.py
```

Then open **http://localhost:8080** in any browser.

The server will scan for the smoker automatically and keep retrying until found.

---

## Running as a Linux Service

The included `smoker.service` systemd unit runs the monitor automatically at boot.

**1. Edit `smoker.service`** if your username or path differs:

```ini
User=your-username
WorkingDirectory=/home/your-username/bt-smoker-monitor
ExecStart=/home/your-username/bt-smoker-monitor/venv/bin/python3 server.py --interval 30 --port 8080
```

**2. Enable and start:**

```bash
sudo cp smoker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable smoker
sudo systemctl start smoker
```

**3. Access the dashboard** from any device on your network:

```
http://<your-linux-box-ip>:8080
```

### Service management

```bash
sudo systemctl stop smoker      # stop
sudo systemctl restart smoker   # restart
journalctl -u smoker -f         # follow logs
```

---

## Dashboard

| Element | Description |
|---------|-------------|
| **⚙️ button** | Opens the settings modal (top-right corner) |
| **Status bar** | WebSocket connection and BLE smoker status |
| **Smoker card** (blue) | Current grill temp, set point, at-temp timer, over/under temp indicator |
| **Probe 1 card** (red) | Current probe temp, target, ETA — hidden when probe not connected |
| **Probe 2 card** (yellow) | Current probe temp, target, ETA — hidden when probe not connected |
| **Chart** | 24-hour scrolling history with dashed target lines |
| **Log panel** | Connection events, alarms, and setting changes |

### Temperature indicators

| State | Display |
|-------|---------|
| Heating toward target | ETA ~23 min (server-computed, updates every poll) |
| Temp stalled | In stall – ETA paused |
| Within 5°F of target | At Temperature |
| More than 5°F over target | **Over Temperature** (pulsing border) |
| More than 5°F under target | **Under Temperature** |

### Alarms

- **Probe reaches target** — voice announcement + push notification. Re-fires if probe drops 5°F below target and climbs back.
- **Probe over temperature** — siren repeating every 30 seconds + push notification. Clears automatically when temp drops back within range.
- **Smoker over temperature** — same behaviour as probes.

> **Note:** Browsers require a user interaction before playing audio. Tap/click the page once after loading to ensure alarms sound.

---

## Bluetooth Notes

The smoker only supports **one BLE connection at a time**. Close the official Nexgrill app before using this monitor, otherwise connections will fail.

The server scans and connects within the same BleakScanner context to prevent the device being evicted from BlueZ's cache between discovery and connection — a common cause of `BleakDeviceNotFoundError` on Linux.

If you have multiple Bluetooth adapters, select the correct one from the in-app settings (⚙️). The selection is saved to the config file and the service restarts automatically to apply the change.

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `bleak` | Cross-platform BLE client |
| `fastapi` | Async web framework |
| `uvicorn` | ASGI server |
| `websockets` | WebSocket support for uvicorn |
