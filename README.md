# BT Smoker Monitor

A local web dashboard for monitoring a Nexgrill Bluetooth smoker in real time. A Python backend polls the smoker via BLE every N seconds and pushes live temperature data to any browser via WebSocket — no cloud, no app required.

![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue) ![License: MIT](https://img.shields.io/badge/license-MIT-green)

---

## Features

- **Auto-discovery** — finds the smoker automatically by scanning for known BLE characteristics
- **Live temperature dashboard** — smoker temp, up to 2 meat probes
- **Trending chart** — 24-hour scrolling history of all temps with target lines
- **ETA to target** — estimated time remaining for each probe
- **At / Over / Under temperature alerts** — visual indicators with colour coding
- **Audible alarms** — siren when a probe exceeds its target; voice announcement when a probe reaches its target
- **Smoker at-temperature timer** — shows how long the smoker has held its set temperature
- **Offline detection** — dashboard reflects when the smoker goes out of BLE range; cards hide automatically
- **Multi-client** — open the dashboard in multiple browsers; all show consistent live state
- **Runs as a Docker container or Linux systemd service**

---

## Requirements

- Python 3.9+ **or** Docker
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
  -v /var/run/dbus:/var/run/dbus \
  --cap-add=NET_ADMIN \
  --cap-add=NET_RAW \
  ghcr.io/bwilliam79/bt-smoker-monitor:latest \
  --interval 30 --port 8080
```

Then open **http://\<host-ip\>:8080** in any browser.

### Bluetooth flags explained

| Flag | Why it's needed |
|------|----------------|
| `--net=host` | Lets bleak scan for BLE advertisements on the host's radio |
| `-v /var/run/dbus:/var/run/dbus` | Gives the container access to the host's `bluetoothd` via D-Bus |
| `--cap-add=NET_ADMIN` / `NET_RAW` | Allows the raw socket operations BlueZ requires |

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--port` | `8080` | Web server port |
| `--interval` | `30` | BLE poll interval in seconds |
| `--address` | *(auto)* | BLE address — only needed if auto-discovery fails |
| `--adapter` | *(auto)* | Bluetooth adapter when multiple are present (e.g. `hci1`) |
| `--debug` | off | Enable verbose poll logging |

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

The server will scan for the smoker automatically. If it isn't found within a few seconds it will keep retrying — no manual address needed in most cases.

---

## Running as a Linux Service

The included `smoker.service` systemd unit runs the monitor automatically at boot.

**1. Install (after cloning and setting up the venv above):**

Edit `smoker.service` if your username or path differs from `/home/pi/bt-smoker-monitor`:

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
| **Status bar** | WebSocket connection and BLE smoker status |
| **Smoker card** (blue) | Current grill temp, set point, at-temp timer, over/under temp indicator — hidden when smoker is unreachable |
| **Probe 1 card** (red) | Current probe temp, target, ETA — hidden when probe not connected |
| **Probe 2 card** (yellow) | Current probe temp, target, ETA — hidden when probe not connected |
| **Chart** | 24-hour scrolling history with dashed target lines |

### Temperature indicators

| State | Display |
|-------|---------|
| Heating toward target | ETA: ~23 min |
| Within 5°F of target | At Temperature |
| More than 5°F over target | **Over Temperature** (red, bold + pulsing) |
| More than 5°F under target | **Under Temperature** (yellow, bold) |

### Alarms

- **Probe reaches target** — voice announcement: *"Probe 1 has reached its target temperature of 145 degrees."* Re-announces if the probe drops more than 5°F below target and comes back up.
- **Probe over temperature** — siren alarm repeating every 30 seconds. Clears automatically once temp drops back within 5°F of target.
- **Smoker over temperature** — same siren behaviour as probes.

> **Note:** Browsers require a user interaction before playing audio. Click anywhere on the page after loading to ensure alarms will sound.

---

## Bluetooth Notes

The smoker only supports **one BLE connection at a time**. When `server.py` is polling, the official Nexgrill app cannot connect simultaneously. Disconnect the app before starting the server.

The server connects briefly to read temperature data, then disconnects — minimising the window during which the smoker's controller is occupied.

If you have multiple Bluetooth adapters and the smoker isn't detected, use `--adapter hci1` (or whichever adapter is closest to the smoker) to select a specific one.

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `bleak` | Cross-platform BLE client |
| `fastapi` | Async web framework |
| `uvicorn` | ASGI server |
| `websockets` | WebSocket support for uvicorn |
