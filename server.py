"""
BT Smoker Monitor — BLE poller + WebSocket server
Usage: python3 server.py [--interval 30] [--port 8080]
"""
import asyncio
import json
import logging
import argparse
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

from bleak import BleakScanner, BleakClient, BleakError
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn

# ── BLE UUIDs ────────────────────────────────────────────────────────────────
CHAR_IP   = '0000bb01-0000-1000-8000-00805f9b34fb'
CHAR_TEMP = '0000cc01-0000-1000-8000-00805f9b34fb'

PROBE_DISCONNECTED = 999
TARGET_PREFIX      = 'NXE'
HISTORY_MAX_AGE    = 24 * 60 * 60   # seconds
CONFIG_PATH        = Path('/data/config.json')

# ── ETA / stall constants ─────────────────────────────────────────────────────
STALL_WINDOW_SECS = 20 * 60   # how far back to look for a stall (20 min)
STALL_THRESHOLD_F = 2.0       # °F range within stall window that counts as stalled

log = logging.getLogger('smoker')

# ── Packet decoder ────────────────────────────────────────────────────────────
def read_u16_le(data: bytes, offset: int) -> int:
    return data[offset] | (data[offset + 1] << 8)

def decode_packet(data: bytes):
    if len(data) < 20:
        return None
    return {
        'setPoint':     read_u16_le(data, 4),
        'grill':        read_u16_le(data, 6),
        'probeTargets': [read_u16_le(data, 8),  read_u16_le(data, 10)],
        'probes':       [read_u16_le(data, 16), read_u16_le(data, 18)],
    }

# ── ETA computation ───────────────────────────────────────────────────────────
def _linreg_rate(points: list[dict]) -> float | None:
    """Least-squares slope in °F/sec over a list of {temp, ts} dicts."""
    n = len(points)
    if n < 2:
        return None
    t0  = points[0]['ts']
    xs  = [p['ts'] - t0 for p in points]
    ys  = [p['temp']    for p in points]
    xm  = sum(xs) / n
    ym  = sum(ys) / n
    num = sum((xs[i] - xm) * (ys[i] - ym) for i in range(n))
    den = sum((xs[i] - xm) ** 2            for i in range(n))
    return (num / den) if den else None

def compute_probe_eta(history: list[dict], target: int, current_temp: int) -> tuple[int | None, bool]:
    """
    Return (eta_mins_or_None, stalled).

    Uses linear regression over all probe history since detection.
    Detects a stall when temp hasn't moved ≥ STALL_THRESHOLD_F in the last
    STALL_WINDOW_SECS, and excludes the stall period from the regression so
    the rate estimate reflects actual cooking progress.
    """
    if len(history) < 2 or target >= PROBE_DISCONNECTED or current_temp >= PROBE_DISCONNECTED:
        return None, False

    if current_temp >= target:
        return 0, False

    now    = history[-1]['ts']
    cutoff = now - STALL_WINDOW_SECS
    window = [p for p in history if p['ts'] >= cutoff]

    # Stall: need ≥3 points in the window and barely any temp movement
    stalled = (
        len(window) >= 3 and
        (max(p['temp'] for p in window) - min(p['temp'] for p in window)) < STALL_THRESHOLD_F
    )

    # Regression: exclude the flat stall window if stalling so it doesn't drag the slope down
    reg_pts = [p for p in history if p['ts'] < cutoff] if stalled else history
    if len(reg_pts) < 2:
        return None, stalled

    rate = _linreg_rate(reg_pts)   # °F / sec
    if rate is None or rate <= 0:
        return None, stalled

    secs = (target - current_temp) / rate
    return max(1, round(secs / 60)), stalled

# ── ntfy.sh push notifications ────────────────────────────────────────────────
def _ntfy_post(topic: str, title: str, message: str, priority: str, tags: str):
    try:
        req = Request(f'https://ntfy.sh/{topic}', data=message.encode(), method='POST')
        req.add_header('Title', title)
        req.add_header('Priority', priority)
        if tags:
            req.add_header('Tags', tags)
        with urlopen(req, timeout=5):
            pass
    except Exception as e:
        log.warning(f'ntfy notification failed: {e}')

async def notify(title: str, message: str, priority: str = 'default', tags: str = ''):
    topic = state.get('ntfy_topic')
    if not topic:
        return
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _ntfy_post, topic, title, message, priority, tags)

# ── App state ─────────────────────────────────────────────────────────────────
app      = FastAPI()
clients: set[WebSocket] = set()
state    = {
    'last':          None,
    'smoker_online': False,
    'ip':            None,
    'address':       None,   # string address
    'ble_device':    None,   # BLEDevice object from last scan
    'rssi':          None,
    'adapter':       None,
    'history':       [],
    'log_history':   [],
    'interval':      30,
    'ntfy_topic':    None,
    # per-probe ETA state (reset when probe disconnects)
    'probe_history': [[], []],    # [{temp, ts}, …] since probe first detected
    'probe_eta':     [None, None],  # minutes to target, or None
    'probe_stalled': [False, False],
    # notification dedup
    'notified': {
        'probe_at_temp':   [False, False],
        'probe_over_temp': [False, False],
        'grill_at_temp':   False,
        'grill_over_temp': False,
    },
}

# ── WebSocket broadcast ───────────────────────────────────────────────────────
async def broadcast(msg: dict):
    if not clients:
        return
    text = json.dumps(msg)
    dead = set()
    for ws in clients:
        try:
            await ws.send_text(text)
        except Exception:
            dead.add(ws)
    clients.difference_update(dead)

# ── Config file ───────────────────────────────────────────────────────────────
def load_config() -> dict:
    """Load ntfy_topic from CONFIG_PATH. Returns {} if missing or malformed."""
    try:
        if CONFIG_PATH.exists():
            return json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    except Exception as e:
        log.warning(f'Could not read config file: {e}')
    return {}

def save_config(ntfy_topic: str, adapter: str = '') -> None:
    """Persist ntfy_topic and adapter to CONFIG_PATH."""
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {'ntfy_topic': ntfy_topic}
        if adapter:
            data['adapter'] = adapter
        CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding='utf-8')
    except Exception as e:
        log.warning(f'Could not write config file: {e}')

# ── Server-side log history ───────────────────────────────────────────────────
def add_log(tag: str, msg: str, cls: str, ts: float):
    state['log_history'].append({'tag': tag, 'msg': msg, 'cls': cls, 'ts': ts})
    cutoff = time.time() - HISTORY_MAX_AGE
    state['log_history'] = [e for e in state['log_history'] if e['ts'] >= cutoff]

# ── Clock-aligned sleep ───────────────────────────────────────────────────────
async def sleep_to_next_tick(interval: int) -> None:
    """Sleep until the next wall-clock multiple of *interval* seconds."""
    now   = time.time()
    delta = math.ceil(now / interval) * interval - now
    await asyncio.sleep(max(delta, 0.001))

# ── Notification checker ──────────────────────────────────────────────────────
async def check_notifications(dec: dict):
    probes   = dec['probes']
    targets  = dec['probeTargets']
    grill    = dec['grill']
    setpoint = dec['setPoint']
    n        = state['notified']

    # Grill at temp
    if not n['grill_at_temp'] and abs(grill - setpoint) <= 5 and grill <= setpoint + 5:
        n['grill_at_temp'] = True
        await notify('Smoker at Temperature', f'Smoker reached {setpoint}°F set point.', tags='fire')
    if n['grill_at_temp'] and grill < setpoint - 10:
        n['grill_at_temp'] = False

    # Grill over temp
    if not n['grill_over_temp'] and grill > setpoint + 5:
        n['grill_over_temp'] = True
        await notify('⚠️ Smoker Over Temperature', f'Smoker is {grill}°F — set point is {setpoint}°F.',
                     priority='urgent', tags='rotating_light')
    if n['grill_over_temp'] and grill <= setpoint + 5:
        n['grill_over_temp'] = False

    # Per-probe
    for i, (temp, target) in enumerate(zip(probes, targets)):
        if temp >= PROBE_DISCONNECTED or target >= PROBE_DISCONNECTED:
            n['probe_at_temp'][i]   = False
            n['probe_over_temp'][i] = False
            continue

        if not n['probe_at_temp'][i] and temp >= target:
            n['probe_at_temp'][i] = True
            await notify(f'Probe {i + 1} at Temperature', f'Probe {i + 1} reached {target}°F.',
                         priority='high', tags='meat_on_bone')
        if n['probe_at_temp'][i] and temp < target - 5:
            n['probe_at_temp'][i] = False

        if not n['probe_over_temp'][i] and temp > target + 5:
            n['probe_over_temp'][i] = True
            await notify(f'⚠️ Probe {i + 1} Over Temperature',
                         f'Probe {i + 1} is {temp}°F — target is {target}°F.',
                         priority='urgent', tags='rotating_light')
        if n['probe_over_temp'][i] and temp <= target + 5:
            n['probe_over_temp'][i] = False

# ── BLE polling loop ──────────────────────────────────────────────────────────
async def scan_and_read() -> tuple[dict | None, object | None, int | None]:
    """
    Scan for the smoker, connect, and read temperature.

    Uses BleakScanner.discover() so the scanner stops cleanly before we
    connect.  The BLEDevice object returned by discover() retains the BlueZ
    D-Bus path so BleakClient can connect without re-scanning.

    Critical: do NOT pass bluez={'adapter': …} to BleakClient.  The
    BLEDevice already carries the correct adapter path from the scanner;
    specifying it again triggers a different BlueZ connection path that
    causes le-connection-abort-by-local on Realtek adapters.

    Returns (decoded_packet, ble_device, rssi) or (None, None, None).
    """
    print(f'Scanning for smoker ({TARGET_PREFIX}*)…')
    scanner_kwargs = {'timeout': 15, 'scanning_mode': 'active'}
    if state['adapter']:
        scanner_kwargs['bluez'] = {'adapter': state['adapter']}

    discovered = await BleakScanner.discover(**scanner_kwargs, return_adv=True)
    found_pair = next(
        ((dev, adv) for dev, adv in discovered.values()
         if dev.name and dev.name.startswith(TARGET_PREFIX)),
        None,
    )
    if not found_pair:
        return None, None, None

    found_device, found_adv = found_pair
    found_rssi = found_adv.rssi
    print(f'Found: {found_device.name}  ({found_device.address})  RSSI: {found_rssi} dBm')

    async with BleakClient(found_device, timeout=45) as client:
        if not state['ip']:
            try:
                raw_ip = await client.read_gatt_char(CHAR_IP)
                state['ip'] = ''.join(chr(b) for b in raw_ip if 32 <= b < 127)
                print(f'Smoker IP: {state["ip"]}')
            except Exception:
                pass
        raw = await client.read_gatt_char(CHAR_TEMP)
        return decode_packet(bytes(raw)), found_device, found_rssi

async def _process_reading(dec: dict, tick_time: float, smoker_was_offline: bool, ble_device, rssi) -> bool:
    """Update state and broadcast a successful reading. Returns new smoker_was_offline value."""
    if smoker_was_offline:
        print('Smoker reconnected.')
        add_log('SYS', 'Smoker reconnected.', 'tag-sys', tick_time)
        await notify('Smoker Connected', 'Smoker monitor reconnected.', tags='white_check_mark')
        smoker_was_offline = False

    state['ble_device'] = ble_device
    state['address']    = ble_device.address if ble_device else state['address']
    if rssi is not None:
        state['rssi'] = rssi

    for i, (temp, target) in enumerate(zip(dec['probes'], dec['probeTargets'])):
        if temp >= PROBE_DISCONNECTED:
            state['probe_history'][i].clear()
            state['probe_eta'][i]     = None
            state['probe_stalled'][i] = False
        else:
            state['probe_history'][i].append({'temp': temp, 'ts': tick_time})
            eta_mins, stalled = compute_probe_eta(state['probe_history'][i], target, temp)
            state['probe_eta'][i]     = eta_mins
            state['probe_stalled'][i] = stalled
            if stalled:
                log.info(f'Probe {i + 1} stall detected at {temp}°F')

    dec['ip']       = state['ip']
    dec['address']  = state['address']
    dec['adapter']  = state.get('adapter') or 'default'
    dec['rssi']     = state['rssi']
    dec['ts']       = tick_time
    dec['interval'] = state['interval']
    dec['eta']      = state['probe_eta'][:]
    dec['stalled']  = state['probe_stalled'][:]

    state['last'] = dec
    state['smoker_online'] = True
    state['history'].append(dec)
    cutoff = time.time() - HISTORY_MAX_AGE
    state['history'] = [p for p in state['history'] if p['ts'] >= cutoff]

    await broadcast(dec)
    await check_notifications(dec)

    probes_str = ', '.join(
        f'{p}°F → {dec["probeTargets"][i]}°F' if p < PROBE_DISCONNECTED else 'NC'
        for i, p in enumerate(dec['probes'])
    )
    log.info(f'Smoker: {dec["grill"]}°F  Set: {dec["setPoint"]}°F  Probes: [{probes_str}]')
    return smoker_was_offline

async def poll_loop(interval: int):
    smoker_was_offline = False
    await sleep_to_next_tick(interval)
    print(f'Polling aligned — first tick at {time.strftime("%H:%M:%S")}')
    while True:
        tick_time = math.floor(time.time() / interval) * interval

        try:
            dec, ble_device, rssi = await scan_and_read()

            if dec:
                smoker_was_offline = await _process_reading(dec, tick_time, smoker_was_offline, ble_device, rssi)
            else:
                # Scan found nothing
                state['smoker_online'] = False
                await broadcast({'smoker_offline': True})
                if not smoker_was_offline:
                    print('Smoker not found — will keep retrying.')
                    add_log('WARN', 'Smoker offline — retrying…', 'tag-warn', time.time())
                    await notify('Smoker Disconnected', 'Lost connection to smoker. Retrying…', tags='warning')
                    smoker_was_offline = True

        except (BleakError, Exception) as e:
            print(f'BLE error: {type(e).__name__}: {e}')
            state['smoker_online'] = False
            await broadcast({'smoker_offline': True})
            if not smoker_was_offline:
                print('Smoker unreachable — will keep retrying.')
                add_log('WARN', 'Smoker unreachable — retrying…', 'tag-warn', tick_time)
                await notify('Smoker Disconnected', 'Lost connection to smoker. Retrying…', tags='warning')
                smoker_was_offline = True

        sleep_for = tick_time + interval - time.time()
        await asyncio.sleep(max(sleep_for, 0.001))

# ── HTTP / WebSocket routes ───────────────────────────────────────────────────
@app.get('/')
async def index():
    return HTMLResponse(Path('index.html').read_text(encoding='utf-8'))

@app.get('/favicon.svg')
async def favicon():
    from fastapi.responses import Response
    return Response(Path('favicon.svg').read_bytes(), media_type='image/svg+xml')

@app.get('/api/state')
async def api_state():
    return state['last'] or {}

@app.get('/api/config')
async def get_config():
    return {
        'ntfy_topic': state.get('ntfy_topic') or '',
        'adapter': state.get('adapter') or '',
    }

@app.post('/api/config')
async def post_config(body: dict):
    topic = str(body.get('ntfy_topic', '')).strip()
    state['ntfy_topic'] = topic or None

    adapter = str(body.get('adapter', '')).strip()
    old_adapter = state.get('adapter') or ''
    state['adapter'] = adapter or None
    adapter_changed = adapter != old_adapter

    save_config(topic, adapter)
    parts = [f'ntfy: {topic or "(none)"}']
    if adapter_changed:
        parts.append(f'adapter: {adapter or "(auto)"}')
        print(f'Bluetooth adapter changed to {adapter or "(auto)"} — takes effect next scan cycle.')
    print(f'Config saved — {", ".join(parts)}')
    return {'ok': True, 'ntfy_topic': topic, 'adapter': adapter, 'adapter_changed': adapter_changed}

@app.get('/api/adapters')
async def get_adapters():
    """List available Bluetooth adapters with USB product names from sysfs."""
    adapters = []
    try:
        bt_dir = Path('/sys/class/bluetooth')
        if bt_dir.exists():
            for hci_link in sorted(bt_dir.iterdir()):
                hci_id = hci_link.name
                real = hci_link.resolve()
                # Walk up from .../bluetooth/hciX until we find a dir with a 'product' file
                usb_dev = real.parent
                while usb_dev != usb_dev.parent and not (usb_dev / 'product').exists():
                    usb_dev = usb_dev.parent
                product = _read_sysfs(usb_dev / 'product')
                manufacturer = _read_sysfs(usb_dev / 'manufacturer')
                # Build a friendly label from USB descriptor strings
                if manufacturer and product:
                    name = f'{manufacturer} {product}' if manufacturer not in product else product
                elif product:
                    name = product
                else:
                    name = ''
                # Check if adapter is up via hciconfig (faster than parsing operstate)
                up = False
                try:
                    result = subprocess.run(['hciconfig', hci_id], capture_output=True, text=True, timeout=3)
                    up = 'UP RUNNING' in result.stdout
                except Exception:
                    pass
                adapters.append({'id': hci_id, 'name': name, 'up': up})
    except Exception as e:
        log.warning(f'Failed to enumerate adapters: {e}')
    return {'adapters': adapters, 'current': state.get('adapter') or ''}


def _read_sysfs(path: Path) -> str:
    """Read a single-line sysfs attribute, returning '' on any failure."""
    try:
        return path.read_text().strip()
    except Exception:
        return ''

@app.post('/api/ntfy-test')
async def ntfy_test():
    topic = state.get('ntfy_topic')
    if not topic:
        from fastapi.responses import JSONResponse
        return JSONResponse({'error': 'No ntfy topic configured'}, status_code=400)
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(
            None, _ntfy_post, topic,
            'BT Smoker Monitor Test',
            f'Test notification from BT Smoker Monitor.',
            'default', 'fire,white_check_mark'
        )
        return {'ok': True}
    except Exception as e:
        from fastapi.responses import JSONResponse
        return JSONResponse({'error': str(e)}, status_code=502)

@app.post('/api/clear-history')
async def clear_history():
    state['history'].clear()
    state['log_history'].clear()
    await broadcast({'type': 'clear_history'})
    return {'ok': True}

@app.websocket('/ws')
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    log.info(f'Client connected  ({len(clients)} total)')
    if state['history'] or state['log_history']:
        await ws.send_text(json.dumps({'type': 'history', 'data': state['history'], 'logs': state['log_history'], 'smoker_online': state['smoker_online']}))
    elif state['last'] and state['smoker_online']:
        await ws.send_text(json.dumps(state['last']))
    try:
        while True:
            await ws.receive_text()  # keep-alive (pings)
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)
        log.info(f'Client disconnected  ({len(clients)} total)')

# ── Entry point ───────────────────────────────────────────────────────────────
async def main(interval: int, port: int, address: str | None, adapter: str | None, ntfy_topic: str):
    state['interval']   = interval
    state['ntfy_topic'] = ntfy_topic or None   # baseline from CLI/env

    # Config file persists settings across restarts
    cfg = load_config()
    if 'ntfy_topic' in cfg:
        state['ntfy_topic'] = cfg['ntfy_topic'] or None

    # CLI --adapter wins, then config file, then None (system default)
    if adapter:
        state['adapter'] = adapter
    elif 'adapter' in cfg and cfg['adapter']:
        state['adapter'] = cfg['adapter']

    print(f'ntfy topic : {state["ntfy_topic"] or "(disabled)"}')
    print(f'BT adapter : {state["adapter"] or "(system default)"}')
    if address:
        print(f'Using hardcoded address: {address}')
        state['address'] = address

    print(f'Starting web server on http://0.0.0.0:{port}')

    server_cfg = uvicorn.Config(app, host='0.0.0.0', port=port, log_level='warning')
    server     = uvicorn.Server(server_cfg)

    await asyncio.gather(
        poll_loop(interval),
        server.serve(),
    )

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='BT Smoker Monitor')
    parser.add_argument('--interval',   type=int, default=30,   help='Poll interval in seconds (default: 30)')
    parser.add_argument('--port',       type=int, default=8080, help='Web server port (default: 8080)')
    parser.add_argument('--address',    type=str, default=None, help='Hardcode BLE address, skipping discovery (e.g. AA:BB:CC:DD:EE:FF)')
    parser.add_argument('--adapter',    type=str, default=None, help='Bluetooth adapter to use (e.g. hci1). Defaults to system default.')
    parser.add_argument('--ntfy-topic', type=str, default=os.environ.get('NTFY_TOPIC', ''), help='ntfy.sh topic for push notifications (or set NTFY_TOPIC env var)')
    parser.add_argument('--debug',      action='store_true', help='Enable verbose logging')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.debug else logging.WARNING,
        format='%(asctime)s  %(levelname)-8s  %(message)s',
        datefmt='%H:%M:%S',
    )
    asyncio.run(main(args.interval, args.port, args.address, args.adapter, args.ntfy_topic))
