"""
Passively sniff BLE advertisements from the smoker without connecting.
No GATT connection = smoker keeps running normally.
"""
import asyncio
from bleak import BleakScanner

TARGET_NAME = "NXE-13CB970"

def callback(device, adv):
    if not device.name or TARGET_NAME not in device.name:
        return

    print(f"\n[{device.name}]  RSSI: {adv.rssi} dBm")

    if adv.manufacturer_data:
        for company_id, data in adv.manufacturer_data.items():
            hex_str = data.hex()
            u16s = [int.from_bytes(data[i:i+2], 'little') for i in range(0, len(data)-1, 2)]
            print(f"  Manufacturer 0x{company_id:04X}: {hex_str}")
            print(f"  As uint16-LE: {u16s}")
    else:
        print("  No manufacturer data in advertisement.")

    if adv.service_data:
        for uuid, data in adv.service_data.items():
            print(f"  Service data [{uuid}]: {data.hex()}")
    else:
        print("  No service data in advertisement.")

async def main():
    print(f"Passively sniffing advertisements for '{TARGET_NAME}'...")
    print("(No connection made — smoker will keep running)\n")

    scanner = BleakScanner(callback)
    await scanner.start()
    await asyncio.sleep(30)
    await scanner.stop()
    print("\nDone.")

asyncio.run(main())
