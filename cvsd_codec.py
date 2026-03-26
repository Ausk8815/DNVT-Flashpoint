"""
Python ctypes wrapper for the CVSD codec DLL.

Usage:
    import cvsd_codec

    # Encode: int16 numpy array -> hex string
    hex_data = cvsd_codec.encode(pcm_samples)

    # Decode: hex string -> int16 numpy array
    pcm_exp = cvsd_codec.decode_exp(hex_data)
    pcm_iir = cvsd_codec.decode_iir(hex_data)
"""

import ctypes
import os
import sys
import numpy as np

# Locate the DLL relative to this file
_dir = os.path.dirname(os.path.abspath(__file__))

# Try common build output locations
_dll_paths = [
    os.path.join(_dir, "build", "Release", "cvsd_codec.dll"),
    os.path.join(_dir, "build", "cvsd_codec.dll"),
    os.path.join(_dir, "build", "Debug", "cvsd_codec.dll"),
    os.path.join(_dir, "cvsd_codec.dll"),
]

_lib = None
for p in _dll_paths:
    if os.path.exists(p):
        _lib = ctypes.CDLL(p)
        break

if _lib is None:
    raise FileNotFoundError(
        f"cvsd_codec.dll not found. Searched:\n" +
        "\n".join(f"  {p}" for p in _dll_paths) +
        "\nRun build.bat first."
    )

# ---- Bind C functions ----

_lib.cvsd_encode.argtypes = [
    ctypes.POINTER(ctypes.c_int16),  # pcm_in
    ctypes.c_int,                     # num_samples
    ctypes.POINTER(ctypes.c_uint8),  # cvsd_out
    ctypes.POINTER(ctypes.c_int),    # out_len
]
_lib.cvsd_encode.restype = None

_lib.cvsd_decode_exp.argtypes = [
    ctypes.POINTER(ctypes.c_uint8),  # cvsd_in
    ctypes.c_int,                     # num_nibbles
    ctypes.POINTER(ctypes.c_int16),  # pcm_out
    ctypes.POINTER(ctypes.c_int),    # out_len
]
_lib.cvsd_decode_exp.restype = None

_lib.cvsd_decode_iir.argtypes = [
    ctypes.POINTER(ctypes.c_uint8),  # cvsd_in
    ctypes.c_int,                     # num_nibbles
    ctypes.POINTER(ctypes.c_int16),  # pcm_out
    ctypes.POINTER(ctypes.c_int),    # out_len
]
_lib.cvsd_decode_iir.restype = None

# ---- Streaming decoder bindings ----

_lib.cvsd_stream_decoder_create.argtypes = [ctypes.c_int]
_lib.cvsd_stream_decoder_create.restype = ctypes.c_void_p

_lib.cvsd_stream_decode_words.argtypes = [
    ctypes.c_void_p,                     # dec
    ctypes.POINTER(ctypes.c_uint32),     # words_in
    ctypes.c_int,                         # num_words
    ctypes.POINTER(ctypes.c_int16),      # pcm_out
    ctypes.POINTER(ctypes.c_int),        # out_len
]
_lib.cvsd_stream_decode_words.restype = None

_lib.cvsd_stream_decoder_reset.argtypes = [ctypes.c_void_p]
_lib.cvsd_stream_decoder_reset.restype = None

_lib.cvsd_stream_decoder_destroy.argtypes = [ctypes.c_void_p]
_lib.cvsd_stream_decoder_destroy.restype = None

# ---- Streaming encoder bindings ----

_lib.cvsd_stream_encoder_create.argtypes = []
_lib.cvsd_stream_encoder_create.restype = ctypes.c_void_p

_lib.cvsd_stream_encode_words.argtypes = [
    ctypes.c_void_p,                     # enc
    ctypes.POINTER(ctypes.c_int16),      # pcm_in
    ctypes.c_int,                         # num_samples
    ctypes.POINTER(ctypes.c_uint32),     # words_out
    ctypes.POINTER(ctypes.c_int),        # out_len
]
_lib.cvsd_stream_encode_words.restype = None

_lib.cvsd_stream_encoder_reset.argtypes = [ctypes.c_void_p]
_lib.cvsd_stream_encoder_reset.restype = None

_lib.cvsd_stream_encoder_destroy.argtypes = [ctypes.c_void_p]
_lib.cvsd_stream_encoder_destroy.restype = None

# ---- Tone detector bindings ----

_lib.tone_detector_create.argtypes = [
    ctypes.POINTER(ctypes.c_double),  # freqs
    ctypes.c_int,                      # num_freqs
    ctypes.c_int,                      # sample_rate
    ctypes.c_int,                      # block_size
    ctypes.c_double,                   # threshold
]
_lib.tone_detector_create.restype = ctypes.c_void_p

_lib.tone_detector_feed.argtypes = [
    ctypes.c_void_p,                   # det
    ctypes.POINTER(ctypes.c_int16),    # pcm_in
    ctypes.c_int,                       # num_samples
    ctypes.POINTER(ctypes.c_uint8),    # results
]
_lib.tone_detector_feed.restype = ctypes.c_int

_lib.tone_detector_feed_mag.argtypes = [
    ctypes.c_void_p,                   # det
    ctypes.POINTER(ctypes.c_int16),    # pcm_in
    ctypes.c_int,                       # num_samples
    ctypes.POINTER(ctypes.c_uint8),    # results
    ctypes.POINTER(ctypes.c_double),   # magnitudes
]
_lib.tone_detector_feed_mag.restype = ctypes.c_int

_lib.tone_detector_reset.argtypes = [ctypes.c_void_p]
_lib.tone_detector_reset.restype = None

_lib.tone_detector_destroy.argtypes = [ctypes.c_void_p]
_lib.tone_detector_destroy.restype = None


def _hex_to_nibbles(hex_data: str) -> np.ndarray:
    """Convert hex string (e.g. 'fa0b3c') to numpy array of nibble values."""
    return np.array([int(c, 16) for c in hex_data], dtype=np.uint8)


def _nibbles_to_hex(nibbles: np.ndarray, count: int) -> str:
    """Convert nibble array back to hex string."""
    return ''.join(f'{b:x}' for b in nibbles[:count])


def encode(pcm_samples: np.ndarray) -> str:
    """
    Encode PCM samples to CVSD hex string.

    Args:
        pcm_samples: numpy array of int16 PCM samples (32 kHz mono)

    Returns:
        Hex string of CVSD-encoded data (one hex char per nibble)
    """
    pcm = np.ascontiguousarray(pcm_samples, dtype=np.int16)
    num_samples = len(pcm)

    # Output: at most num_samples nibbles (1 bit per sample, 4 samples per nibble)
    max_nibbles = (num_samples + 3) // 4
    out_buf = np.zeros(max_nibbles, dtype=np.uint8)
    out_len = ctypes.c_int(0)

    _lib.cvsd_encode(
        pcm.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
        num_samples,
        out_buf.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)),
        ctypes.byref(out_len),
    )

    return _nibbles_to_hex(out_buf, out_len.value)


def decode_exp(hex_data: str) -> np.ndarray:
    """
    Decode CVSD hex string to PCM using exponential averaging filter.

    Args:
        hex_data: hex string of CVSD-encoded data

    Returns:
        numpy array of int16 PCM samples (32 kHz mono)
    """
    nibbles = _hex_to_nibbles(hex_data)
    num_nibbles = len(nibbles)

    in_buf = np.ascontiguousarray(nibbles, dtype=np.uint8)
    out_buf = np.zeros(num_nibbles * 4, dtype=np.int16)
    out_len = ctypes.c_int(0)

    _lib.cvsd_decode_exp(
        in_buf.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)),
        num_nibbles,
        out_buf.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
        ctypes.byref(out_len),
    )

    return out_buf[:out_len.value]


# ============================================================================
# Streaming decoder for real-time audio
# ============================================================================

DECODER_EXP = 0
DECODER_IIR = 1


class StreamDecoder:
    """
    Stateful CVSD streaming decoder for real-time audio.

    Maintains internal state between calls so it can decode continuous
    audio streams chunk-by-chunk (e.g. from USB packets).
    """

    def __init__(self, decoder_type: int = DECODER_EXP):
        self._handle = _lib.cvsd_stream_decoder_create(decoder_type)
        if not self._handle:
            raise RuntimeError("Failed to create stream decoder")

    def decode_words(self, words: np.ndarray) -> np.ndarray:
        """
        Decode an array of uint32 words (raw PIO data from DNVT).
        Each word = 32 CVSD bits = 32 PCM samples at 32 kHz.

        Args:
            words: numpy array of uint32 values

        Returns:
            numpy array of int16 PCM samples
        """
        words = np.ascontiguousarray(words, dtype=np.uint32)
        num_words = len(words)
        if num_words == 0:
            return np.array([], dtype=np.int16)

        out_buf = np.zeros(num_words * 32, dtype=np.int16)
        out_len = ctypes.c_int(0)

        _lib.cvsd_stream_decode_words(
            self._handle,
            words.ctypes.data_as(ctypes.POINTER(ctypes.c_uint32)),
            num_words,
            out_buf.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
            ctypes.byref(out_len),
        )
        return out_buf[:out_len.value]

    def reset(self):
        """Reset decoder state (e.g. when a call ends)."""
        _lib.cvsd_stream_decoder_reset(self._handle)

    def __del__(self):
        if hasattr(self, '_handle') and self._handle:
            _lib.cvsd_stream_decoder_destroy(self._handle)
            self._handle = None


class StreamEncoder:
    """
    Stateful CVSD streaming encoder for real-time audio.

    Maintains internal state between calls so it can encode continuous
    audio streams chunk-by-chunk (e.g. for SIP→DNVT bridging).
    """

    def __init__(self):
        self._handle = _lib.cvsd_stream_encoder_create()
        if not self._handle:
            raise RuntimeError("Failed to create stream encoder")

    def encode_words(self, pcm: np.ndarray) -> np.ndarray:
        """
        Encode PCM samples to uint32 words (raw PIO data for DNVT).
        Each word = 32 CVSD bits = 32 PCM samples at 32 kHz.

        Args:
            pcm: numpy array of int16 PCM samples

        Returns:
            numpy array of uint32 values (CVSD words)
        """
        pcm = np.ascontiguousarray(pcm, dtype=np.int16)
        num_samples = len(pcm)
        if num_samples == 0:
            return np.array([], dtype=np.uint32)

        max_words = (num_samples + 31) // 32
        out_buf = np.zeros(max_words, dtype=np.uint32)
        out_len = ctypes.c_int(0)

        _lib.cvsd_stream_encode_words(
            self._handle,
            pcm.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
            num_samples,
            out_buf.ctypes.data_as(ctypes.POINTER(ctypes.c_uint32)),
            ctypes.byref(out_len),
        )
        return out_buf[:out_len.value]

    def reset(self):
        """Reset encoder state."""
        _lib.cvsd_stream_encoder_reset(self._handle)

    def __del__(self):
        if hasattr(self, '_handle') and self._handle:
            _lib.cvsd_stream_encoder_destroy(self._handle)
            self._handle = None


def decode_iir(hex_data: str) -> np.ndarray:
    """
    Decode CVSD hex string to PCM using IIR Chebyshev Type II lowpass filter.

    Args:
        hex_data: hex string of CVSD-encoded data

    Returns:
        numpy array of int16 PCM samples (32 kHz mono)
    """
    nibbles = _hex_to_nibbles(hex_data)
    num_nibbles = len(nibbles)

    in_buf = np.ascontiguousarray(nibbles, dtype=np.uint8)
    out_buf = np.zeros(num_nibbles * 4, dtype=np.int16)
    out_len = ctypes.c_int(0)

    _lib.cvsd_decode_iir(
        in_buf.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)),
        num_nibbles,
        out_buf.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
        ctypes.byref(out_len),
    )

    return out_buf[:out_len.value]


class ToneDetector:
    """
    Real-time tone detector using the Goertzel algorithm.

    Detects presence of specific frequencies in PCM audio streams.
    Call feed() with PCM chunks; it returns which tones are present.
    """

    def __init__(self, frequencies: list, sample_rate: int = 32000,
                 block_size: int = 320, threshold: float = 0.05):
        """
        Args:
            frequencies: list of frequencies to detect (Hz)
            sample_rate: audio sample rate
            block_size:  samples per detection window (320 = 10ms at 32kHz)
            threshold:   magnitude threshold (0.0-1.0) for tone-present
        """
        self.frequencies = list(frequencies)
        self.num_freqs = len(frequencies)

        freqs_arr = (ctypes.c_double * self.num_freqs)(*frequencies)
        self._handle = _lib.tone_detector_create(
            freqs_arr, self.num_freqs, sample_rate, block_size, threshold
        )
        if not self._handle:
            raise RuntimeError("Failed to create tone detector")

    def feed(self, pcm: np.ndarray):
        """
        Feed PCM samples and detect tones.

        Args:
            pcm: numpy array of int16 PCM samples

        Returns:
            (detected, magnitudes) where:
                detected:   list of bools, one per frequency
                magnitudes: list of floats, Goertzel magnitude per frequency
        """
        pcm = np.ascontiguousarray(pcm, dtype=np.int16)
        results = np.zeros(self.num_freqs, dtype=np.uint8)
        mags = np.zeros(self.num_freqs, dtype=np.float64)

        _lib.tone_detector_feed_mag(
            self._handle,
            pcm.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
            len(pcm),
            results.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)),
            mags.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
        )

        return [bool(r) for r in results], [float(m) for m in mags]

    def reset(self):
        _lib.tone_detector_reset(self._handle)

    def __del__(self):
        if hasattr(self, '_handle') and self._handle:
            _lib.tone_detector_destroy(self._handle)
            self._handle = None
