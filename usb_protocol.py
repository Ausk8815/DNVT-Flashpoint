"""
USB protocol layer for DNVT switch communication.

Handles parsing HOST_PACKETs (device→host) and building DEVICE_PACKETs (host→device)
including audio data words for SIP→DNVT bridging.

Packet structures (from usb_structures.h):

HOST_PACKET (56 bytes):
    uint16_t phone_states      — 4 phones × 4 bits
    uint8_t  data_lengths      — 4 phones × 2 bits (0-3 words each)
    uint8_t  reserved
    uint32_t data[4][3]        — up to 3 audio words per phone
    uint8_t  phone_digits[4]   — one digit per phone (0 = none)

DEVICE_PACKET (53 bytes):
    uint32_t data[4][3]        — up to 3 audio words per phone
    uint8_t  data_lengths      — 4 phones × 2 bits
    uint8_t  phone_commands[4] — command per phone
"""

import struct
import numpy as np

# USB identifiers
VID = 0xCAFE
PID = 0x6942
EP_IN = 0x82   # Bulk IN  — HOST_PACKET from device
EP_OUT = 0x01  # Bulk OUT — DEVICE_PACKET to device

# HOST_PACKET format (little-endian, packed)
HOST_PACKET_FMT = "<HBB12I4B"
HOST_PACKET_SIZE = struct.calcsize(HOST_PACKET_FMT)  # 56

# DEVICE_PACKET size
DEVICE_PACKET_SIZE = 53

# Phone commands
NO_COMMAND         = 0x0
RING_COMMAND       = 0x1
PLAINTEXT_COMMAND  = 0x2
DISCONNECT_COMMAND = 0x3
RING_DISMISS_CMD   = 0x4

# Mapped phone state names
PHONE_STATES = {
    0: "idle",
    1: "dial",
    2: "traffic",
    3: "ring",
    4: "await_ring",
    5: "unreachable",
    6: "req_ring",
    7: "transition",
}

NULL_AUDIO = 0xAAAAAAAA


def state_name(state_val):
    return PHONE_STATES.get(state_val, f"unknown({state_val})")


def parse_host_packet(data):
    """
    Parse a raw 64-byte HOST_PACKET.

    Returns:
        (states, data_lens, phone_data, phone_digits) or None if too short.
        states:      list of 4 ints (mapped phone state 0-7)
        data_lens:   list of 4 ints (0-3 words per phone)
        phone_data:  list of 4 lists of uint32 words
        phone_digits: list of 4 (char or None)
    """
    if len(data) < HOST_PACKET_SIZE:
        return None
    fields = struct.unpack(HOST_PACKET_FMT, bytes(data[:HOST_PACKET_SIZE]))
    phone_states_raw = fields[0]
    data_lengths_raw = fields[1]
    data_words = fields[3:15]
    digits = fields[15:19]

    states = []
    data_lens = []
    phone_data = []
    for p in range(4):
        states.append((phone_states_raw >> (p * 4)) & 0xF)
        dl = (data_lengths_raw >> (p * 2)) & 0x3
        data_lens.append(dl)
        base = p * 3
        phone_data.append([data_words[base + j] for j in range(dl)])

    phone_digits = []
    for d in digits:
        phone_digits.append(chr(d) if d != 0 else None)
    return states, data_lens, phone_data, phone_digits


def byte_swap_32(val):
    """Byte-swap a uint32 (matching firmware's C host behavior for TX audio)."""
    return (
        ((val >> 24) & 0xFF) |
        ((val << 8) & 0xFF0000) |
        ((val >> 8) & 0xFF00) |
        ((val << 24) & 0xFF000000)
    )


def build_device_packet(commands=None, audio_data=None):
    """
    Build a 53-byte DEVICE_PACKET.

    Args:
        commands:   dict of {phone_index: command_byte} or None
        audio_data: dict of {phone_index: list_of_uint32_words} or None
                    Each list can have 0-3 words. Words are byte-swapped
                    before packing (matching firmware convention).

    Returns:
        bytes of length DEVICE_PACKET_SIZE (53)
    """
    buf = bytearray(DEVICE_PACKET_SIZE)

    # Pack audio data: bytes 0-47 = uint32_t data[4][3]
    # No byte-swap — firmware passes DEVICE_PACKET data directly to PIO
    data_lengths = 0
    if audio_data:
        for phone_idx, words in audio_data.items():
            if not words:
                continue
            count = min(len(words), 3)
            data_lengths |= (count << (phone_idx * 2))
            for j in range(count):
                offset = (phone_idx * 3 + j) * 4
                struct.pack_into("<I", buf, offset, words[j])

    # Byte 48: data_lengths
    buf[48] = data_lengths

    # Bytes 49-52: phone_commands[4]
    if commands:
        for phone_idx, cmd in commands.items():
            buf[49 + phone_idx] = cmd

    return bytes(buf)
