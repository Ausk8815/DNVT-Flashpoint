# Flashpoint DNVT

A SIP bridge and management system for DNVT (Digital Nonsecure Voice Terminal) military field telephones. Connects Cold War-era secure phones to modern VoIP/PBX systems via USB.

Heavily based on the work of [Nick Andre](https://github.com/nickandre) and [rrruark](https://github.com/rrruark), whose DNVT hardware adapter and CVSD codec implementations made this project possible.

## Features

- **SIP/VoIP Bridge** — Make and receive calls through any SIP-compatible PBX (Asterisk, FreePBX, etc.)
- **Local Intercom** — Direct phone-to-phone calling between up to 4 DNVT lines with zero-latency raw CVSD passthrough
- **CVSD Codec** — Native C++ implementation for real-time 32kHz CVSD encode/decode
- **Audio Pipeline** — CVSD 32kHz &harr; PCM 8kHz (box filter decimation) &harr; G.711 &mu;-law RTP
- **Configurable Dial Plan** — Mode-based dialing with SIP, intercom, and reserved function keys
- **Modified Firmware** — Enhanced RP2040 firmware with USB traffic mode, mid-call digit detection, and PTT handling
- **Incoming Call Support** — Ring detection, auto-answer, and call state management

## Hardware Requirements

- DNVT phone (TA-1035, TA-1042, or compatible)
- [dnvt-switch](https://github.com/nickandre/dnvt-fw) USB adapter (RP2040/Pico-based)
- Windows PC with USB port (Linux support coming soon)
- SIP/PBX server (optional, for VoIP connectivity)

## Platform Support

| Platform | Status |
|----------|--------|
| Windows | ✅ Supported |
| Linux | 🚧 Coming soon — native libusb support makes this straightforward, stay tuned |
| macOS | ❓ Untested — should work with libusb, contributions welcome |

## Software Requirements

- Python 3.10+
- MinGW-w64 (for building the C++ DLL)
- [Zadig](https://zadig.akeo.ie/) (for WinUSB driver installation)

### Python Dependencies

```
pip install pyVoIP numpy sounddevice
```

## Quick Start

1. **Install USB Driver** — Run Zadig, select the DNVT adapter (VID `CAFE` PID `6942`), install WinUSB driver

2. **Flash Firmware** — Hold BOOTSEL on the Pico, connect USB, drag `Reference/dnvt-fw-master/build/dnvt-switch.uf2` to the RPI-RP2 drive

3. **Build the DLL**
   ```
   cd dnvt_bridge
   build.bat
   ```

4. **Configure SIP** — Edit `sip_extensions.ini` with your PBX credentials:
   ```ini
   [line1]
   enabled = true
   sip_server = 10.0.3.7
   username = 21003
   password = your_password
   display_name = DNVT_1
   ```

5. **Run**
   ```
   run.bat
   ```

## Dial Plan

| Keys | Action |
|------|--------|
| `P` + digits + `C` | SIP call (P=SIP mode, dial number, C=send) |
| `R` | Redial last SIP number |
| `1`-`4` | Intercom call to local line |
| `I`, `F`, `O` | Reserved for future use |

In SIP mode, `R` maps to `#` for navigating IVR menus.

## Architecture

```
DNVT Phone <--CVSD/DiffManchester--> RP2040 Pico <--USB--> C++ DLL <--PCM--> Python <--RTP--> SIP/PBX
                                      (firmware)         (dnvt_bridge)      (pyVoIP)
```

The C++ DLL handles all timing-critical work:
- USB bulk transfers (10,000 packets/sec)
- CVSD decode (32kHz bitstream to 16-bit PCM)
- Audio decimation (32kHz to 8kHz with 4-tap box filter)
- CVSD encode (8kHz PCM upsampled and encoded for TX)
- Ring buffers for thread-safe audio exchange with Python

Python handles SIP signaling, call state management, and the dial plan.

## Project Structure

```
dnvt_sip.py          — Main application (SIP bridge + dial plan)
dnvt_bridge_py.py    — Python bindings for the C++ DLL
sip_bridge.py        — pyVoIP wrapper for SIP call management
config.py            — Configuration loader
sip_extensions.ini   — SIP line configuration

dnvt_bridge/
  dnvt_bridge.cpp    — Native C++ USB I/O and audio bridge
  dnvt_bridge.h      — DLL API header
  build.bat          — MinGW build script

cvsd_codec/
  cvsd_codec.cpp     — CVSD encoder/decoder (exponential + IIR)
  cvsd_codec.h       — Codec API

Reference/
  dnvt-fw-master/    — Modified RP2040 firmware (fork of dnvt-switch)
  dnvt-master/       — Original CVSD codec reference (Python)
```

## Roadmap

- **PBX-to-PBX CVSD Passthrough** — Currently researching direct DNVT codec passthrough between switches over SIP, bypassing the CVSD↔PCM↔u-law transcode entirely. This would allow two DNVT switches on different PBX servers to connect phones with native CVSD audio end-to-end, preserving full codec fidelity across WAN links.
- **Linux Support** — Native build with libusb (no driver install needed)
- **Management GUI** — PySide6 interface for real-time line monitoring and configuration
- **Local Intercom** — Direct CVSD word passthrough between lines (zero transcode latency)
- **Dial Plan Configuration** — INI-based routing rules for flexible number handling

## Acknowledgments

This project would not exist without the foundational work of:

- **[Nick Andre](https://github.com/nickandre)** — [dnvt-fw](https://github.com/nickandre/dnvt-fw) (RP2040 firmware and USB adapter) and [dnvt](https://github.com/nickandre/dnvt) (CVSD codec and protocol research)
- **[rrruark](https://github.com/rrruark)** — Hardware design, protocol analysis, and DNVT research

Our CVSD codec, USB protocol handling, and firmware are heavily based on their work.

## License

See individual component licenses. The dnvt-fw firmware and CVSD reference code are by their respective authors.
