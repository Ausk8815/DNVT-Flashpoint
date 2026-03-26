"""
DNVT Phone Monitor + SIP Bridge
Connects to the DNVT switch via USB, bridges each phone line to a SIP extension.
Audio is bidirectional: DNVT CVSD <-> SIP G.711 via real-time transcoding.

USB Device: VID 0xCAFE, PID 0x6942 ("DNVT Adapter" by Nick's Knacks)
Protocol:   Bulk EP2 IN (0x82) for HOST_PACKET, Bulk EP1 OUT (0x01) for DEVICE_PACKET
"""

import sys
import os
import time
import usb.core
import usb.util
import usb.backend.libusb1 as libusb1

import usb_protocol as usb_proto
from config import load_config
from call_manager import CallManager


def _get_backend():
    """Get the libusb1 backend, using the bundled DLL if needed on Windows."""
    backend = libusb1.get_backend()
    if backend is not None:
        return backend
    try:
        import libusb._dll as _dll
        dll_dir = os.path.dirname(_dll.__file__)
        dll_path = os.path.join(dll_dir, "_platform", "windows", "x86_64", "libusb-1.0.dll")
        if os.path.exists(dll_path):
            backend = libusb1.get_backend(find_library=lambda x: dll_path)
    except ImportError:
        pass
    return backend


def find_device():
    """Find and configure the DNVT switch USB device."""
    backend = _get_backend()
    if backend is None:
        print("ERROR: libusb-1.0 backend not found.")
        print("Install it with:  pip install libusb")
        sys.exit(1)
    dev = usb.core.find(idVendor=usb_proto.VID, idProduct=usb_proto.PID, backend=backend)
    if dev is None:
        print(f"DNVT switch not found (VID={usb_proto.VID:#06x} PID={usb_proto.PID:#06x})")
        print("Is it plugged in? Is the WinUSB driver installed (use Zadig)?")
        sys.exit(1)
    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
    except (NotImplementedError, usb.core.USBError):
        pass
    dev.set_configuration()
    print(f"Connected to {dev.manufacturer} - {dev.product} (S/N: {dev.serial_number})")
    return dev


def main():
    config = load_config()
    dev = find_device()

    call_mgr = CallManager(config)
    call_mgr.start()

    print(f"\nDNVT SIP Bridge running... (Ctrl+C to quit)")
    for i, lc in enumerate(config.lines):
        if lc.enabled:
            print(f"  Line {i+1}: {lc.username}@{lc.sip_server} ({lc.display_name})")
    print("-" * 60)

    pkt_count = 0
    last_debug_time = time.time()
    audio_rx_count = [0] * 4
    audio_tx_count = [0] * 4

    empty_pkt = usb_proto.build_device_packet()
    try:
        dev.write(usb_proto.EP_OUT, empty_pkt, timeout=1000)
    except usb.core.USBError:
        pass

    try:
        while True:
            try:
                raw = dev.read(usb_proto.EP_IN, 64, timeout=100)
            except usb.core.USBTimeoutError:
                continue
            except usb.core.USBError as e:
                print(f"\nUSB error: {e}")
                break

            pkt_count += 1

            result = usb_proto.parse_host_packet(raw)
            if result is None:
                continue

            states, data_lens, phone_data, digits = result

            # Feed to call manager with rate info for TX matching
            call_mgr.process_packet(states, data_lens, phone_data, digits)

            # Get commands + audio, rate-matched to RX
            commands, audio_data = call_mgr.get_device_data(data_lens)

            # Send DEVICE_PACKET
            if commands or audio_data or pkt_count % 10 == 0:
                pkt = usb_proto.build_device_packet(commands=commands, audio_data=audio_data)
                try:
                    dev.write(usb_proto.EP_OUT, pkt, timeout=10)
                except usb.core.USBError:
                    pass

            # Track audio flow
            if audio_data:
                for p_idx, words in audio_data.items():
                    audio_tx_count[p_idx] += len(words)
            for p in range(4):
                if data_lens[p] > 0:
                    audio_rx_count[p] += data_lens[p]

            # Periodic status
            now = time.time()
            if now - last_debug_time >= 10.0:
                line_info = []
                for i in range(4):
                    ls = call_mgr.line_states[i].value
                    reg = "R" if call_mgr.sip_lines[i].registered else "-"
                    line_info.append(f"{i+1}:{ls}({reg})")
                rx_info = [str(audio_rx_count[i]) for i in range(4)]
                tx_info = [str(audio_tx_count[i]) for i in range(4)]
                print(f"  [status] pkts={pkt_count} lines=[{' '.join(line_info)}] rx=[{','.join(rx_info)}] tx=[{','.join(tx_info)}]")
                audio_rx_count = [0] * 4
                audio_tx_count = [0] * 4
                last_debug_time = now

    except KeyboardInterrupt:
        print("\n\nShutting down...")
    finally:
        call_mgr.stop()
        usb.util.dispose_resources(dev)


if __name__ == "__main__":
    main()
