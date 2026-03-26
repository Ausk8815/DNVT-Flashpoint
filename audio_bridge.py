"""
Audio bridge between CVSD (32kHz) and G.711/PCM (8kHz) for SIP integration.

Each DNVT phone line gets one AudioBridge instance that handles:
- Outbound: CVSD uint32 words -> PCM 32kHz -> resample -> PCM 8kHz (for SIP RTP)
- Inbound:  PCM 8kHz (from SIP RTP) -> resample -> PCM 32kHz -> CVSD uint32 words
"""

import threading
import wave
import time
import numpy as np

import cvsd_codec

# Sample rates
DNVT_RATE = 32000   # CVSD native rate
SIP_RATE = 8000     # G.711 rate
RESAMPLE_UP = 4     # 8k -> 32k
RESAMPLE_DOWN = 4   # 32k -> 8k


class AudioBridge:
    """Bidirectional audio bridge for one DNVT phone line."""

    def __init__(self):
        self.decoder = cvsd_codec.StreamDecoder(cvsd_codec.DECODER_EXP)
        self.encoder = cvsd_codec.StreamEncoder()

        # Separate locks for each direction to avoid contention
        self._sip_lock = threading.Lock()    # protects _sip_pcm32_buf, _sip_pcm_buf
        self._dnvt_lock = threading.Lock()   # protects _dnvt_word_buf

        # DNVT -> SIP direction
        self._sip_pcm_buf = np.array([], dtype=np.int16)    # PCM @ 8k ready for SIP

        # SIP -> DNVT direction
        self._dnvt_word_buf = []  # CVSD words ready for phone

        # Debug: WAV capture of what we send to SIP
        self._capture_file = None
        self._capture_frames = 0
        # Debug: raw CVSD word capture
        self._raw_words = []
        self._word_delivery = []  # (timestamp, n_words) per feed call

    def reset(self):
        """Reset codec state and clear buffers (call ended)."""
        self.decoder.reset()
        self.encoder.reset()
        with self._sip_lock:
            self._sip_pcm_buf = np.array([], dtype=np.int16)
        with self._dnvt_lock:
            self._dnvt_word_buf.clear()
        self._stop_capture()
        # Save raw word capture for offline analysis
        if self._raw_words:
            import struct
            fn = f"capture_raw_words_{int(time.time())}.bin"
            with open(fn, 'wb') as f:
                for w in self._raw_words:
                    f.write(struct.pack('<I', w))
            print(f"  [capture] Saved {len(self._raw_words)} raw CVSD words to {fn}")
            # Save delivery timing
            fn2 = f"capture_delivery_{int(time.time())}.txt"
            with open(fn2, 'w') as f:
                for ts, n in self._word_delivery:
                    f.write(f"{ts:.6f} {n}\n")
            print(f"  [capture] Saved delivery timing to {fn2}")
        self._raw_words = []
        self._word_delivery = []

    def _start_capture(self):
        """Start capturing SIP TX audio to WAV file."""
        if self._capture_file is not None:
            return
        fn = f"capture_sip_tx_{int(time.time())}.wav"
        self._capture_file = wave.open(fn, 'w')
        self._capture_file.setnchannels(1)
        self._capture_file.setsampwidth(2)
        self._capture_file.setframerate(8000)
        self._capture_frames = 0
        print(f"  [capture] Recording SIP TX to {fn}")

    def _stop_capture(self):
        """Stop capturing."""
        if self._capture_file is not None:
            self._capture_file.close()
            print(f"  [capture] Saved {self._capture_frames} frames")
            self._capture_file = None
            self._capture_frames = 0

    def feed_dnvt_audio(self, cvsd_words):
        """
        Feed CVSD audio words received from the DNVT phone.
        Decodes to PCM @ 32k, resamples in chunks, buffers for SIP output.
        """
        if not cvsd_words:
            return

        # Capture raw words and timing
        self._raw_words.extend(cvsd_words)
        self._word_delivery.append((time.monotonic(), len(cvsd_words)))

        word_array = np.array(cvsd_words, dtype=np.uint32)
        pcm_32k = self.decoder.decode_words(word_array)
        if len(pcm_32k) == 0:
            return

        # Simple downsampling: take every 4th sample (32k -> 8k)
        # Much faster than resample_poly and avoids chunk-boundary artifacts
        pcm_8k = pcm_32k[::RESAMPLE_DOWN].copy()

        with self._sip_lock:
            self._sip_pcm_buf = np.concatenate([self._sip_pcm_buf, pcm_8k])
            # Cap to ~200ms at 8kHz = 1600 samples
            if len(self._sip_pcm_buf) > 1600:
                self._sip_pcm_buf = self._sip_pcm_buf[-1600:]

    def feed_sip_audio(self, pcm_8k):
        """
        Feed PCM audio received from SIP (RTP decoded).
        Resamples 8k->32k, encodes to CVSD, buffers words for phone.
        """
        if len(pcm_8k) == 0:
            return

        # Simple upsampling: repeat each sample 4x (8k -> 32k)
        # Much faster than resample_poly, avoids blocking
        pcm_32k = np.repeat(pcm_8k, RESAMPLE_UP)
        words = self.encoder.encode_words(pcm_32k)
        if len(words) == 0:
            return

        with self._dnvt_lock:
            self._dnvt_word_buf.extend(words.tolist())
            # Cap buffer to ~200ms to prevent lag buildup
            if len(self._dnvt_word_buf) > 200:
                self._dnvt_word_buf = self._dnvt_word_buf[-200:]

    def get_sip_audio(self, frame_size=160):
        """
        Get PCM audio to send via SIP RTP.
        Returns exactly frame_size samples when available, None otherwise.
        """
        with self._sip_lock:
            if len(self._sip_pcm_buf) < frame_size:
                return None
            out = self._sip_pcm_buf[:frame_size].copy()
            self._sip_pcm_buf = self._sip_pcm_buf[frame_size:]

        # Capture to WAV for debugging
        if self._capture_file is None:
            self._start_capture()
        if self._capture_file is not None:
            self._capture_file.writeframes(out.astype(np.int16).tobytes())
            self._capture_frames += 1

        return out

    def get_dnvt_words(self, max_words=3):
        """
        Get CVSD words to send to DNVT phone via DEVICE_PACKET.
        Returns list of uint32 values, up to max_words.
        """
        with self._dnvt_lock:
            out = self._dnvt_word_buf[:max_words]
            self._dnvt_word_buf = self._dnvt_word_buf[max_words:]
            return out
