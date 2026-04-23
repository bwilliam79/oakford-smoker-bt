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
from urllib.parse import quote
from urllib.request import Request, urlopen

from bleak import BleakScanner, BleakClient, BleakError
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request as FastAPIRequest, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
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
PROBE_HISTORY_MAX_AGE_SECS = 4 * 60 * 60   # keep last 4h of probe points for ETA regression

# ── BLE reconnect backoff ────────────────────────────────────────────────────
BACKOFF_START_SECS = 5
BACKOFF_MAX_SECS   = 60

# ── WebSocket idle cleanup ───────────────────────────────────────────────────
WS_IDLE_TIMEOUT_SECS = 120   # drop connections with no pong/activity for this long
WS_REAPER_INTERVAL   = 30    # how often to sweep stale clients

# ── Auth ─────────────────────────────────────────────────────────────────────
AUTH_TOKEN = os.environ.get('AUTH_TOKEN', '').strip() or None

# ── CORS ─────────────────────────────────────────────────────────────────────
_default_origins = 'http://localhost:8080,http://127.0.0.1:8080,http://localhost:8888,http://127.0.0.1:8888'
CORS_ORIGINS = [o.strip() for o in os.environ.get('CORS_ORIGINS', _default_origins).split(',') if o.strip()]

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
        # URL-encode topic so special chars like ?, #, /, etc. can't break the URL path.
        # urllib.request.urlopen uses HTTPS with TLS certificate verification by default
        # (via the system's SSL/TLS trust store). Explicit `verify` isn't a thing on
        # urlopen — if we ever switch to `requests`, add `verify=True`.
        safe_topic = quote(topic, safe='')
        req = Request(f'https://ntfy.sh/{safe_topic}', data=message.encode(), method='POST')
        req.add_header('Title', title)
        req.add_header('Priority', priority)
        if tags:
            req.add_header('Tags', tags)
        with urlopen(req, timeout=5):
            pass
    except Exception:
        log.exception('ntfy notification failed')

async def notify(title: str, message: str, priority: str = 'default', tags: str = ''):
    topic = state.get('ntfy_topic')
    if not topic:
        return
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _ntfy_post, topic, title, message, priority, tags)

# ── App state ─────────────────────────────────────────────────────────────────
app      = FastAPI()
# Register CORS once — allow localhost by default, configurable via CORS_ORIGINS env var.
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)
clients: dict = {}   # ws -> last_seen_ts
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

# ── Auth middleware for /api/* routes ────────────────────────────────────────
@app.middleware('http')
async def auth_middleware(request: FastAPIRequest, call_next):
    if AUTH_TOKEN and request.url.path.startswith('/api/'):
        if request.headers.get('X-Auth-Token') != AUTH_TOKEN:
            return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    return await call_next(request)

# ── WebSocket broadcast ───────────────────────────────────────────────────────
async def broadcast(msg: dict):
    if not clients:
        return
    text = json.dumps(msg)
    dead = []
    for ws in list(clients.keys()):
        try:
            await ws.send_text(text)
        except Exception:
            log.exception('WebSocket send failed; dropping client')
            dead.append(ws)
    for ws in dead:
        clients.pop(ws, None)

# ── Config file ───────────────────────────────────────────────────────────────
def load_config() -> dict:
    """Load ntfy_topic from CONFIG_PATH. Returns {} if missing or malformed.

    Values are coerced to strings so downstream code (which expects str) can't
    crash on an int/bool/None smuggled into the JSON file by hand-editing.
    """
    try:
        if CONFIG_PATH.exists():
            raw = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
            if not isinstance(raw, dict):
                log.warning('Config file is not a JSON object; ignoring')
                return {}
            out = {}
            for key in ('ntfy_topic', 'adapter'):
                if key in raw and raw[key] is not None:
                    out[key] = str(raw[key]).strip()
            return out
    except Exception:
        log.exception('Could not read config file')
    return {}

def save_config(ntfy_topic: str, adapter: str = '') -> None:
    """Persist ntfy_topic and adapter to CONFIG_PATH."""
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {'ntfy_topic': ntfy_topic}
        if adapter:
            data['adapter'] = adapter
        CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding='utf-8')
    except Exception:
        log.exception('Could not write config file')

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
        # Always re-read IP on reconnect so we don't serve a stale address forever.
        try:
            raw_ip = await client.read_gatt_char(CHAR_IP)
            state['ip'] = ''.join(chr(b) for b in raw_ip if 32 <= b < 127)
            print(f'Smoker IP: {state["ip"]}')
        except Exception:
            log.exception('Failed to read smoker IP over GATT; leaving IP as-is')
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
            ph = state['probe_history'][i]
            ph.append({'temp': temp, 'ts': tick_time})
            ph_cutoff = tick_time - PROBE_HISTORY_MAX_AGE_SECS
            if ph and ph[0]['ts'] < ph_cutoff:
                state['probe_history'][i] = [p for p in ph if p['ts'] >= ph_cutoff]
            eta_mins, stalled = compute_probe_eta(state['probe_history'][i], target, temp)
            state['probe_eta'][i]     = eta_mins
            state['probe_stalled'][i] = stalled
            if stalled:
                log.info(f'Probe {i + 1} stall detected at {temp}°F')

    dec['ip']        = state['ip']
    dec['address']   = state['address']
    dec['adapter']   = state.get('adapter') or 'default'
    dec['rssi']      = state['rssi']
    dec['ts']        = tick_time
    dec['interval']  = state['interval']
    dec['eta']       = state['probe_eta'][:]
    dec['stalled']   = state['probe_stalled'][:]
    dec['connected'] = True

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

def _mark_disconnected():
    """Clear stale temp/probe values and flag the cached 'last' payload as disconnected."""
    state['smoker_online'] = False
    state['rssi'] = None
    state['ip']   = None   # will be re-read on reconnect
    state['probe_eta']     = [None, None]
    state['probe_stalled'] = [False, False]
    # Rebuild state['last'] as a new dict — the previous dict is shared with the last
    # entry in state['history'] (see _process_reading), so mutating it in place would
    # retroactively corrupt historical data.
    if state['last'] is not None:
        state['last'] = {
            **state['last'],
            'connected': False,
            'grill':     None,
            'probes':    [None, None],
            'rssi':      None,
            'eta':       [None, None],
            'stalled':   [False, False],
        }

async def poll_loop(interval: int):
    smoker_was_offline = False
    backoff = BACKOFF_START_SECS
    await sleep_to_next_tick(interval)
    print(f'Polling aligned — first tick at {time.strftime("%H:%M:%S")}')
    while True:
        tick_time = math.floor(time.time() / interval) * interval

        success = False
        try:
            dec, ble_device, rssi = await scan_and_read()

            if dec:
                smoker_was_offline = await _process_reading(dec, tick_time, smoker_was_offline, ble_device, rssi)
                success = True
                backoff = BACKOFF_START_SECS   # reset backoff on any good read
            else:
                # Scan found nothing
                _mark_disconnected()
                await broadcast({'smoker_offline': True, 'connected': False})
                if not smoker_was_offline:
                    print('Smoker not found — will keep retrying.')
                    add_log('WARN', 'Smoker offline — retrying…', 'tag-warn', time.time())
                    await notify('Smoker Disconnected', 'Lost connection to smoker. Retrying…', tags='warning')
                    smoker_was_offline = True

        except BleakError:
            log.exception('BLE error during scan/read')
            _mark_disconnected()
            await broadcast({'smoker_offline': True, 'connected': False})
            if not smoker_was_offline:
                print('Smoker unreachable — will keep retrying.')
                add_log('WARN', 'Smoker unreachable — retrying…', 'tag-warn', tick_time)
                await notify('Smoker Disconnected', 'Lost connection to smoker. Retrying…', tags='warning')
                smoker_was_offline = True
        except Exception:
            log.exception('Unexpected error in poll loop')
            _mark_disconnected()
            await broadcast({'smoker_offline': True, 'connected': False})
            if not smoker_was_offline:
                print('Smoker unreachable — will keep retrying.')
                add_log('WARN', 'Smoker unreachable — retrying…', 'tag-warn', tick_time)
                await notify('Smoker Disconnected', 'Lost connection to smoker. Retrying…', tags='warning')
                smoker_was_offline = True

        if success:
            # Clock-aligned sleep until the next poll tick
            sleep_for = tick_time + interval - time.time()
            await asyncio.sleep(max(sleep_for, 0.001))
        else:
            # Exponential backoff when we can't reach the smoker — avoid thrashing the adapter
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_SECS)

# ── HTTP / WebSocket routes ───────────────────────────────────────────────────
@app.get('/')
async def index():
    return HTMLResponse(Path('index.html').read_text(encoding='utf-8'))

@app.get('/favicon.svg')
async def favicon():
    return Response(Path('favicon.svg').read_bytes(), media_type='image/svg+xml')

@app.get('/manifest.json')
async def manifest():
    return Response(Path('manifest.json').read_bytes(), media_type='application/manifest+json')

@app.get('/service-worker.js')
async def service_worker():
    # Service-Worker-Allowed lets the worker scope the whole site.
    # Cache-Control: no-cache so browsers pick up SW updates on every load.
    return Response(
        Path('service-worker.js').read_bytes(),
        media_type='application/javascript',
        headers={'Service-Worker-Allowed': '/', 'Cache-Control': 'no-cache'},
    )

@app.get('/icon-192.png')
async def icon_192():
    return Response(Path('icon-192.png').read_bytes(), media_type='image/png')

@app.get('/icon-512.png')
async def icon_512():
    return Response(Path('icon-512.png').read_bytes(), media_type='image/png')

@app.get('/apple-touch-icon.png')
async def apple_touch_icon():
    return Response(Path('apple-touch-icon.png').read_bytes(), media_type='image/png')

VENDOR_DIR = Path(__file__).resolve().parent / 'vendor'

@app.get('/vendor/{filename}')
async def vendor_asset(filename: str):
    # Locally-hosted third-party JS/CSS (e.g. chart.js). Resolve and confine to
    # VENDOR_DIR so a crafted filename can't traverse outside it.
    candidate = (VENDOR_DIR / filename).resolve()
    try:
        candidate.relative_to(VENDOR_DIR)
    except ValueError:
        return Response(status_code=404)
    if not candidate.is_file():
        return Response(status_code=404)
    media = 'application/javascript' if candidate.suffix == '.js' else 'application/octet-stream'
    # Immutable: vendor files are pinned via SRI, so browsers can cache aggressively.
    return Response(candidate.read_bytes(), media_type=media,
                    headers={'Cache-Control': 'public, max-age=31536000, immutable'})

@app.get('/api/state')
async def api_state():
    last = state['last'] or {}
    # Always surface the current connection status on top of the cached payload.
    if last:
        last = dict(last)
        last['connected'] = bool(state.get('smoker_online'))
    return last

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
                # Check if adapter is up/running by reading rfkill + operstate from sysfs.
                # Avoids shelling out to the deprecated hciconfig binary.
                up = _is_adapter_up(hci_link)
                adapters.append({'id': hci_id, 'name': name, 'up': up})
    except Exception:
        log.exception('Failed to enumerate adapters')
    return {'adapters': adapters, 'current': state.get('adapter') or ''}


def _read_sysfs(path: Path) -> str:
    """Read a single-line sysfs attribute, returning '' on any failure."""
    try:
        return path.read_text().strip()
    except Exception:
        return ''

def _is_adapter_up(hci_link: Path) -> bool:
    """Return True if the HCI adapter is powered and running."""
    # /sys/class/bluetooth/hciX/flags is a hex bitfield; bit 0 == up, bit 4 == running.
    flags_raw = _read_sysfs(hci_link / 'flags')
    if flags_raw:
        try:
            flags = int(flags_raw, 16)
            return bool(flags & 0x1) and bool(flags & 0x10)
        except ValueError:
            pass
    return False

@app.post('/api/ntfy-test')
async def ntfy_test():
    topic = state.get('ntfy_topic')
    if not topic:
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
        log.exception('ntfy test notification failed')
        return JSONResponse({'error': str(e)}, status_code=502)

@app.post('/api/clear-history')
async def clear_history():
    state['history'].clear()
    state['log_history'].clear()
    # Drop per-probe ETA state too so post-clear ETAs aren't computed off
    # pre-clear readings.
    state['probe_history'] = [[], []]
    state['probe_eta']     = [None, None]
    state['probe_stalled'] = [False, False]
    state['notified'] = {
        'probe_at_temp':   [False, False],
        'probe_over_temp': [False, False],
        'grill_at_temp':   False,
        'grill_over_temp': False,
    }
    await broadcast({'type': 'clear_history'})
    return {'ok': True}

@app.websocket('/ws')
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    clients[ws] = time.time()
    log.info(f'Client connected  ({len(clients)} total)')
    if state['history'] or state['log_history']:
        await ws.send_text(json.dumps({'type': 'history', 'data': state['history'], 'logs': state['log_history'], 'smoker_online': state['smoker_online']}))
    elif state['last'] and state['smoker_online']:
        await ws.send_text(json.dumps(state['last']))
    try:
        while True:
            # Any inbound frame (including FastAPI/Uvicorn protocol-level pings that
            # arrive as text) refreshes the last-seen timestamp.
            await ws.receive_text()
            clients[ws] = time.time()
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception('WebSocket loop error')
    finally:
        clients.pop(ws, None)
        log.info(f'Client disconnected  ({len(clients)} total)')

async def ws_reaper():
    """Drop WebSocket connections that have had no inbound activity for WS_IDLE_TIMEOUT_SECS."""
    while True:
        await asyncio.sleep(WS_REAPER_INTERVAL)
        now = time.time()
        stale = [ws for ws, ts in clients.items() if now - ts > WS_IDLE_TIMEOUT_SECS]
        for ws in stale:
            try:
                await ws.close(code=1001)
            except Exception:
                pass
            clients.pop(ws, None)
            log.info(f'Reaped idle WebSocket client  ({len(clients)} total)')

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

    if AUTH_TOKEN:
        print('Auth enabled: /api/* routes require X-Auth-Token header.')
    else:
        print('WARNING: AUTH_TOKEN not set — /api/* is open. Set AUTH_TOKEN in env to lock it down.')

    print(f'Starting web server on http://0.0.0.0:{port}')

    server_cfg = uvicorn.Config(app, host='0.0.0.0', port=port, log_level='warning')
    server     = uvicorn.Server(server_cfg)

    await asyncio.gather(
        poll_loop(interval),
        ws_reaper(),
        server.serve(),
    )

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='BT Smoker Monitor')
    parser.add_argument('--interval',   type=int, default=30,   help='Poll interval in seconds (default: 30)')
    parser.add_argument('--port',       type=int, default=8080, help='Web server port (default: 8080)')
    parser.add_argument('--address',    type=str, default=None, help='Hardcode BLE address, skipping discovery (e.g. AA:BB:CC:DD:EE:FF)')
    parser.add_argument('--adapter',    type=str, default=os.environ.get('BT_ADAPTER') or None, help='Bluetooth adapter to use (e.g. hci1). Defaults to BT_ADAPTER env var, then system default.')
    parser.add_argument('--ntfy-topic', type=str, default=os.environ.get('NTFY_TOPIC', ''), help='ntfy.sh topic for push notifications (or set NTFY_TOPIC env var)')
    parser.add_argument('--debug',      action='store_true', help='Enable verbose logging')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.debug else logging.WARNING,
        format='%(asctime)s  %(levelname)-8s  %(message)s',
        datefmt='%H:%M:%S',
    )
    asyncio.run(main(args.interval, args.port, args.address, args.adapter, args.ntfy_topic))
