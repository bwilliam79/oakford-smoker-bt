"""
BT Smoker Monitor — BLE poller + WebSocket server
Usage: python3 server.py [--interval 30] [--port 8080]
"""
import asyncio
import json
import logging
import argparse
import math
import time
from pathlib import Path

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

# ── App state ─────────────────────────────────────────────────────────────────
app      = FastAPI()
clients: set[WebSocket] = set()
state    = {'last': None, 'ip': None, 'address': None, 'rssi': None, 'adapter': None, 'history': [], 'log_history': [], 'interval': 30}

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
            state['address'] = await find_smoker()
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

            kwargs = {'timeout': 15}
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
                dec['ip']       = state['ip']
                dec['address']  = state['address']
                dec['rssi']     = state['rssi']
                dec['ts']       = tick_time
                dec['interval'] = state['interval']
                state['last']  = dec
                state['history'].append(dec)
                cutoff = time.time() - HISTORY_MAX_AGE
                state['history'] = [p for p in state['history'] if p['ts'] >= cutoff]
                await broadcast(dec)
                probes_str = ', '.join(
                    f'{p}°F → {dec["probeTargets"][i]}°F' if p < PROBE_DISCONNECTED else 'NC'
                    for i, p in enumerate(dec['probes'])
                )
                log.info(f'Smoker: {dec["grill"]}°F  Set: {dec["setPoint"]}°F  Probes: [{probes_str}]')

        except (BleakError, Exception):
            await broadcast({'smoker_offline': True})
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
async def main(interval: int, port: int, address: str | None, adapter: str | None):
    state['interval'] = interval
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
    parser.add_argument('--interval', type=int,  default=30,   help='Poll interval in seconds (default: 30)')
    parser.add_argument('--port',     type=int,  default=8080, help='Web server port (default: 8080)')
    parser.add_argument('--address',  type=str,  default=None, help='Hardcode BLE address, skipping discovery (e.g. AA:BB:CC:DD:EE:FF)')
    parser.add_argument('--adapter',  type=str,  default=None, help='Bluetooth adapter to use (e.g. hci1). Defaults to system default.')
    parser.add_argument('--debug',    action='store_true',     help='Enable verbose logging')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.debug else logging.WARNING,
        format='%(asctime)s  %(levelname)-8s  %(message)s',
        datefmt='%H:%M:%S',
    )
    asyncio.run(main(args.interval, args.port, args.address, args.adapter))
