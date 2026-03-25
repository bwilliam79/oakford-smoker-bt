# BT Smoker Monitor

A local web dashboard for monitoring a Nexgrill Bluetooth smoker in real time. A Python backend polls the smoker via BLE every N seconds and pushes live temperature data to any browser via WebSocket — no cloud, no app required.

![Dashboard showing smoker and probe temperatures with chart](https://img.shields.io/badge/python-3.9%2B-blue) ![License: MIT](https://img.shields.io/badge/license-MIT-green)

---

## Features

- **Live temperature dashboard** — smoker temp, up to 2 meat probes
- **Trending chart** — scrolling history of all temps with target lines
- **ETA to target** — estimated time remaining for each probe
- **At / Over / Under temperature alerts** — visual indicators with bold colour coding
- **Audible alarms** — siren when a probe exceeds its target; voice announcement when a probe reaches its target
- **Smoker at-temperature timer** — shows how long the smoker has held its set temperature
- **Offline detection** — dashboard reflects when the smoker goes out of BLE range
- **Runs as a Linux systemd service** — set it and forget it on a always-on box

---

## Requirements

- Python 3.9+
- A Bluetooth adapter (built-in or USB)
- Nexgrill smoker (tested with NXE-13CB970)
- Chrome or any modern browser for the dashboard

---

## Installation

```bash
git clone https://github.com/bwilliam79/bt-smoker-monitor.git
cd bt-smoker-monitor

# Create and activate a virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

---

## Usage

### Run locally

```bash
python3 server.py
```

Then open **http://localhost:8080** in Chrome.

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--port` | `8080` | Web server port |
| `--interval` | `30` | BLE poll interval in seconds |
| `--address` | auto | Hardcode BLE address, skipping discovery (e.g. `AA:BB:CC:DD:EE:FF`) |
| `--adapter` | auto | Bluetooth adapter to use when multiple are present (e.g. `hci1`) |
| `--debug` | off | Enable verbose poll logging to stdout |

Example — poll every 10 seconds with debug output:

```bash
python3 server.py --interval 10 --port 8080 --debug
```

---

## Running as a Linux Service

The included `smoker.service` systemd unit runs the monitor automatically at boot.

**1. Clone the repo on your Linux box:**

```bash
git clone https://github.com/bwilliam79/bt-smoker-monitor.git
cd bt-smoker-monitor
```

**2. Set up the virtualenv:**

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**3. Edit `smoker.service`** if your username or install path differs from `/home/pi/bt-smoker-monitor`:

```ini
User=your-username
WorkingDirectory=/home/your-username/bt-smoker-monitor
ExecStart=/home/your-username/bt-smoker-monitor/venv/bin/python3 server.py --interval 30 --port 8080
```

**4. Install and enable the service:**

```bash
sudo cp smoker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable smoker
sudo systemctl start smoker
```

**5. Check it's running:**

```bash
sudo systemctl status smoker
```

**6. Access the dashboard** from any device on your network:

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
| **Connected / Disconnected** | WebSocket + BLE smoker status |
| **Smoker card** (blue) | Current grill temp, set point, at-temp timer, over/under temp indicator |
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

The server connects briefly to read temperature data, then disconnects — minimising the window during which the smoker's controller is displaced.

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `bleak` | Cross-platform BLE client |
| `fastapi` | Async web framework |
| `uvicorn` | ASGI server |
| `websockets` | WebSocket support for uvicorn |
