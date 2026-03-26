"""
Python wrapper for the DNVT bridge DLL.

Provides a clean interface for the native USB I/O + CVSD codec bridge.
All timing-critical work runs in C++ — Python only handles SIP and state logic.
"""

import ctypes
import os
import numpy as np

# Load the DLL (add directory to search path so libusb-1.0.dll is found)
_dir = os.path.dirname(os.path.abspath(__file__))
_dll_path = os.path.join(_dir, "dnvt_bridge.dll")
os.add_dll_directory(_dir)
_lib = ctypes.CDLL(_dll_path)

# ---- C struct mirror ----

class PhoneStatus(ctypes.Structure):
    _fields_ = [
        ("state",     ctypes.c_uint8),
        ("digit",     ctypes.c_char),
        ("raw_state", ctypes.c_uint8),
        ("rx_words",  ctypes.c_uint16),
        ("tx_words",  ctypes.c_uint16),
    ]

# ---- Function signatures ----

_lib.bridge_init.restype = ctypes.c_int
_lib.bridge_init.argtypes = []

_lib.bridge_shutdown.restype = None
_lib.bridge_shutdown.argtypes = []

_lib.bridge_is_running.restype = ctypes.c_int
_lib.bridge_is_running.argtypes = []

_lib.bridge_get_status.restype = None
_lib.bridge_get_status.argtypes = [ctypes.POINTER(PhoneStatus)]

_lib.bridge_send_command.restype = None
_lib.bridge_send_command.argtypes = [ctypes.c_int, ctypes.c_uint8]

_lib.bridge_get_audio_8k.restype = ctypes.c_int
_lib.bridge_get_audio_8k.argtypes = [
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_int16),
    ctypes.c_int,
]

_lib.bridge_put_audio_8k.restype = ctypes.c_int
_lib.bridge_put_audio_8k.argtypes = [
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_int16),
    ctypes.c_int,
]

_lib.bridge_load_tone.restype = ctypes.c_int
_lib.bridge_load_tone.argtypes = [
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_uint32),
    ctypes.c_int,
]

_lib.bridge_play_tone.restype = None
_lib.bridge_play_tone.argtypes = [ctypes.c_int, ctypes.c_int]

_lib.bridge_set_intercom.restype = None
_lib.bridge_set_intercom.argtypes = [ctypes.c_int, ctypes.c_int]

_lib.bridge_get_digit.restype = ctypes.c_char
_lib.bridge_get_digit.argtypes = [ctypes.c_int]

_lib.bridge_clear_audio.restype = None
_lib.bridge_clear_audio.argtypes = [ctypes.c_int]

# ---- Constants ----

NUM_PHONES = 4

CMD_NONE         = 0x00
CMD_RING         = 0x01
CMD_PLAINTEXT    = 0x02
CMD_DISCONNECT   = 0x03
CMD_RING_DISMISS = 0x04

STATE_IDLE       = 0
STATE_DIAL       = 1
STATE_TRAFFIC    = 2
STATE_RING       = 3
STATE_TRANSITION = 7

STATE_NAMES = {
    0: "idle", 1: "dial", 2: "traffic", 3: "ring",
    4: "await_ring", 5: "unreachable", 6: "req_ring", 7: "transition",
}

# ---- Python API ----

def init():
    """Initialize the bridge. Returns 0 on success."""
    return _lib.bridge_init()

def shutdown():
    """Shut down the bridge."""
    _lib.bridge_shutdown()

def is_running():
    """Check if the bridge is running."""
    return bool(_lib.bridge_is_running())

def get_status():
    """Get status of all 4 phones. Returns list of PhoneStatus."""
    statuses = (PhoneStatus * NUM_PHONES)()
    _lib.bridge_get_status(statuses)
    return list(statuses)

def send_command(phone, cmd):
    """Send a command to a phone (0-3)."""
    _lib.bridge_send_command(phone, cmd)

def get_audio_8k(phone, max_samples=960):
    """
    Get decoded audio from a phone (8kHz int16 PCM).
    Returns numpy array of int16 samples, or None if no data.
    """
    buf = (ctypes.c_int16 * max_samples)()
    n = _lib.bridge_get_audio_8k(phone, buf, max_samples)
    if n <= 0:
        return None
    # Convert only the first n samples from the ctypes buffer
    return np.frombuffer(buf, dtype=np.int16, count=n).copy()

def put_audio_8k(phone, pcm):
    """
    Put audio to send to a phone (8kHz int16 PCM).
    pcm: numpy array of int16 samples.
    Returns number of samples accepted.
    """
    pcm = np.ascontiguousarray(pcm, dtype=np.int16)
    ptr = pcm.ctypes.data_as(ctypes.POINTER(ctypes.c_int16))
    return _lib.bridge_put_audio_8k(phone, ptr, len(pcm))

def load_tone(phone, words):
    """Load pre-encoded CVSD tone words for a phone. words: numpy uint32 array or file path."""
    import numpy as np
    if isinstance(words, str):
        words = np.fromfile(words, dtype=np.uint32)
    words = np.ascontiguousarray(words, dtype=np.uint32)
    ptr = words.ctypes.data_as(ctypes.POINTER(ctypes.c_uint32))
    return _lib.bridge_load_tone(phone, ptr, len(words))

def play_tone(phone, on=True):
    """Start/stop playing the loaded tone on a phone."""
    _lib.bridge_play_tone(phone, 1 if on else 0)

def set_intercom(phone_a, phone_b):
    """Connect two phones for raw CVSD intercom. phone_b=-1 to disconnect."""
    _lib.bridge_set_intercom(phone_a, phone_b)

def get_digit(phone):
    """Get last digit from phone (and clear). Returns char or None."""
    d = _lib.bridge_get_digit(phone)
    return d.decode('ascii') if d != b'\x00' else None

def clear_audio(phone):
    """Clear audio buffers and reset codecs for a phone."""
    _lib.bridge_clear_audio(phone)

def state_name(state_val):
    """Get human-readable state name."""
    return STATE_NAMES.get(state_val, f"unknown({state_val})")
