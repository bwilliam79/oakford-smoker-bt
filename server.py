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
    'ip':            None,
    'address':       None,
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
        await notify('Smoker at Temperature', f'Grill reached {setpoint}°F set point.', tags='fire')
    if n['grill_at_temp'] and grill < setpoint - 10:
        n['grill_at_temp'] = False

    # Grill over temp
    if not n['grill_over_temp'] and grill > setpoint + 5:
        n['grill_over_temp'] = True
        await notify('⚠️ Smoker Over Temperature', f'Grill is {grill}°F — set point is {setpoint}°F.',
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
async def find_smoker():
    print(f'Scanning for smoker ({TARGET_PREFIX}*)…')
    kwargs = {'timeout': 12, 'scanning_mode': 'active'}
    if state['adapter']:
        kwargs['bluez'] = {'adapter': state['adapter']}
    devices = await BleakScanner.discover(**kwargs)
    match = next((d for d in devices if d.name and d.name.startswith(TARGET_PREFIX)), None)
    if match:
        print(f'Found: {match.name}  ({match.address})')
        return match.address
    return None

async def read_rssi(address: str, timeout: float = 5.0) -> int | None:
    """Scan for a specific device and return its RSSI from advertisement data."""
    result  = None
    found   = asyncio.Event()

    def callback(device, adv_data):
        nonlocal result
        if device.address.upper() == address.upper() and adv_data.rssi is not None:
            result = adv_data.rssi
            found.set()

    kwargs = {'scanning_mode': 'active'}
    if state['adapter']:
        kwargs['bluez'] = {'adapter': state['adapter']}
    async with BleakScanner(callback, **kwargs):
        try:
            await asyncio.wait_for(found.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    return result

async def poll_loop(interval: int):
    smoker_was_offline = False
    await sleep_to_next_tick(interval)
    print(f'Polling aligned — first tick at {time.strftime("%H:%M:%S")}')
    while True:
        # Snap tick_time to the current boundary so timestamps are always clean
        tick_time = math.floor(time.time() / interval) * interval

        # Discover smoker if we don't have an address yet
        if not state['address']:
            try:
                state['address'] = await find_smoker()
            except Exception as e:
                log.warning(f'BLE scan error: {e}')
                state['address'] = None
            if not state['address']:
                await broadcast({'smoker_offline': True})
                if not smoker_was_offline:
                    print('Smoker not found — will keep retrying.')
                    add_log('WARN', 'Smoker offline — retrying…', 'tag-warn', time.time())
                    smoker_was_offline = True
                await asyncio.sleep(tick_time + interval - time.time())
                continue

        try:
            # Read RSSI from advertisement data before connecting
            rssi = await read_rssi(state['address'], timeout=5.0)
            if rssi is not None:
                state['rssi'] = rssi

            kwargs = {'timeout': 30}
            if state['adapter']:
                kwargs['bluez'] = {'adapter': state['adapter']}
            async with BleakClient(state['address'], **kwargs) as client:
                # Read IP once
                if not state['ip']:
                    try:
                        raw_ip = await client.read_gatt_char(CHAR_IP)
                        state['ip'] = ''.join(chr(b) for b in raw_ip if 32 <= b < 127)
                        print(f'Smoker IP: {state["ip"]}')
                    except Exception:
                        pass

                raw = await client.read_gatt_char(CHAR_TEMP)
                dec = decode_packet(bytes(raw))

            if dec:
                if smoker_was_offline:
                    print('Smoker reconnected.')
                    add_log('SYS', 'Smoker reconnected.', 'tag-sys', tick_time)
                    smoker_was_offline = False

                # ── Update probe histories and compute server-side ETAs ────────
                for i, (temp, target) in enumerate(zip(dec['probes'], dec['probeTargets'])):
                    if temp >= PROBE_DISCONNECTED:
                        # Probe disconnected — reset history and ETA
                        state['probe_history'][i].clear()
                        state['probe_eta'][i]     = None
                        state['probe_stalled'][i] = False
                    else:
                        state['probe_history'][i].append({'temp': temp, 'ts': tick_time})
                        eta_mins, stalled = compute_probe_eta(
                            state['probe_history'][i], target, temp
                        )
                        state['probe_eta'][i]     = eta_mins
                        state['probe_stalled'][i] = stalled
                        if stalled:
                            log.info(f'Probe {i + 1} stall detected at {temp}°F')

                dec['ip']       = state['ip']
                dec['address']  = state['address']
                dec['rssi']     = state['rssi']
                dec['ts']       = tick_time
                dec['interval'] = state['interval']
                dec['eta']      = state['probe_eta'][:]
                dec['stalled']  = state['probe_stalled'][:]

                state['last']  = dec
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

        except (BleakError, Exception) as e:
            await broadcast({'smoker_offline': True})
            print(f'BLE connect error: {type(e).__name__}: {e}')
            if not smoker_was_offline:
                print('Smoker unreachable — will keep retrying.')
                add_log('WARN', 'Smoker unreachable — retrying…', 'tag-warn', tick_time)
                smoker_was_offline = True
            # Clear address so next iteration re-scans
            state['address'] = None

        # Sleep until the next tick boundary relative to when this tick started
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
        await ws.send_text(json.dumps({'type': 'history', 'data': state['history'], 'logs': state['log_history'], 'smoker_online': state['last'] is not None}))
    elif state['last']:
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
    state['ntfy_topic'] = ntfy_topic or None
    if ntfy_topic:
        print(f'Push notifications enabled (ntfy topic: {ntfy_topic})')
    if address:
        print(f'Using hardcoded address: {address}')
        state['address'] = address
    if adapter:
        print(f'Using adapter: {adapter}')
        state['adapter'] = adapter
        add_log('SYS', f'Using BT adapter: {adapter}', 'tag-sys', time.time())

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
