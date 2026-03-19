import asyncio
from bleak import BleakScanner, BleakClient

TARGET_NAME = "NXE-13CB970"

async def main():
    print("Scanning for devices...")
    devices = await BleakScanner.discover(timeout=10)

    target = next((d for d in devices if d.name and TARGET_NAME in d.name), None)
    if not target:
        print(f"Device '{TARGET_NAME}' not found. Devices seen:")
        for d in devices:
            print(f"  {d.name or '(unnamed)':30s}  {d.address}")
        return

    print(f"\nFound: {target.name}  ({target.address})")
    print("Connecting...")

    async with BleakClient(target.address) as client:
        print(f"Connected: {client.is_connected}\n")
        for svc in client.services:
            print(f"SERVICE  {svc.uuid}  —  {svc.description}")
            for ch in svc.characteristics:
                props = ", ".join(ch.properties)
                print(f"  CHAR  {ch.uuid}  [{props}]  —  {ch.description}")
                if "read" in ch.properties:
                    try:
                        val = await client.read_gatt_char(ch.uuid)
                        print(f"         VALUE (hex): {val.hex()}")
                        print(f"         VALUE (asc): {''.join(chr(b) if 32<=b<127 else '.' for b in val)}")
                    except Exception as e:
                        print(f"         (read failed: {e})")

asyncio.run(main())
