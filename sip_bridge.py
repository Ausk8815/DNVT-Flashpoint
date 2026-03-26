"""
SIP bridge layer — wraps pyVoIP to provide per-line SIP registration and call handling.

Each SipLine represents one DNVT phone line registered as a SIP extension.
Audio is exchanged as linear PCM int16 @ 8kHz via audiodirect u-law conversion.
"""

import threading
import time
import audioop
import numpy as np

from pyVoIP.VoIP import VoIPPhone, CallState, PhoneStatus

from config import LineConfig


class SipLine:
    """
    SIP extension for one DNVT phone line.

    Handles registration, incoming/outgoing calls, and audio I/O.
    Audio is bridged via callbacks — the CallManager sets these up.
    """

    def __init__(self, line_index, config: LineConfig, incoming_callback=None):
        """
        Args:
            line_index:         0-3 (DNVT phone line number)
            config:             LineConfig from sip_extensions.ini
            incoming_callback:  called with (line_index, SipLine, VoIPCall) on incoming
        """
        self.line_index = line_index
        self.config = config
        self.incoming_callback = incoming_callback
        self.phone = None
        self.active_call = None
        self._started = False
        # Our own RTP sender state
        self._rtp_sock = None
        self._rtp_dest = None
        self._rtp_pt = 0
        self._rtp_ssrc = 0
        self._rtp_seq = 0
        self._rtp_ts = 0

    def start(self):
        """Register this line with the PBX."""
        if not self.config.enabled:
            return
        try:
            # Determine our local IP toward the PBX
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((self.config.sip_server, self.config.sip_port))
            local_ip = s.getsockname()[0]
            s.close()

            self.phone = VoIPPhone(
                server=self.config.sip_server,
                port=self.config.sip_port,
                username=self.config.username,
                password=self.config.password,
                callCallback=self._on_incoming,
                myIP=local_ip,
                sipPort=5060,
            )
            self.phone.start()
            self._started = True
            print(f"  [SIP] Line {self.line_index+1}: registered as {self.config.username}@{self.config.sip_server} (local: {local_ip}:5060)")
        except Exception as e:
            import traceback
            print(f"  [SIP] Line {self.line_index+1}: registration failed: {e}")
            traceback.print_exc()

    def stop(self):
        """Unregister and clean up."""
        if self.phone and self._started:
            try:
                if self.active_call and self.active_call.state == CallState.ANSWERED:
                    self.active_call.hangup()
            except Exception:
                pass
            try:
                self.phone.stop()
            except Exception:
                pass
            self._started = False
            print(f"  [SIP] Line {self.line_index+1}: unregistered")

    @property
    def registered(self):
        if not self.phone or not self._started:
            return False
        try:
            return self.phone.get_status() == PhoneStatus.REGISTERED
        except Exception:
            return False

    def make_call(self, number):
        """
        Initiate an outbound SIP call.

        Args:
            number: destination SIP number/extension

        Returns:
            VoIPCall object or None on failure
        """
        if not self.registered:
            print(f"  [SIP] Line {self.line_index+1}: not registered, can't call")
            return None
        if self.active_call and self.active_call.state == CallState.ANSWERED:
            print(f"  [SIP] Line {self.line_index+1}: already in a call")
            return None
        try:
            call = self.phone.call(number)
            self.active_call = call
            print(f"  [SIP] Line {self.line_index+1}: calling {number}")
            return call
        except Exception as e:
            print(f"  [SIP] Line {self.line_index+1}: call failed: {e}")
            return None

    def answer_call(self):
        """Answer the current incoming call."""
        if self.active_call and self.active_call.state == CallState.RINGING:
            try:
                self.active_call.answer()
                # Stop pyVoIP's built-in transmitter — we send RTP ourselves
                self._stop_pyvoip_transmitter()
                print(f"  [SIP] Line {self.line_index+1}: answered")
            except Exception as e:
                print(f"  [SIP] Line {self.line_index+1}: answer failed: {e}")

    def hangup(self):
        """Hang up the current call."""
        if self.active_call:
            try:
                if self.active_call.state == CallState.ANSWERED:
                    self.active_call.hangup()
                elif self.active_call.state == CallState.RINGING:
                    self.active_call.deny()
                print(f"  [SIP] Line {self.line_index+1}: hung up")
            except Exception:
                pass
            self.active_call = None
            if self._rtp_sock:
                try:
                    self._rtp_sock.close()
                except:
                    pass
                self._rtp_sock = None
                self._rtp_dest = None

    def read_audio(self):
        """
        Read audio from the SIP call (incoming RTP).

        pyVoIP internally decodes u-law to unsigned 8-bit linear PCM
        (ulaw2lin width=1, then bias +128). We convert to signed int16
        using audioop for proper sample width conversion.

        Returns:
            numpy array of int16 PCM @ 8kHz, or None
        """
        if not self.active_call or self.active_call.state != CallState.ANSWERED:
            return None
        try:
            # Non-blocking read — blocking can hang the thread if call ends
            data = self.active_call.read_audio(length=160, blocking=False)
            if not data or len(data) == 0:
                return None
            # pyVoIP returns b'\x80' * 160 for silence — skip it
            if data == b'\x80' * len(data):
                return None
            # pyVoIP's parse_pcmu does: ulaw2lin(payload, 1) then bias(data, 1, 128)
            # So data is UNSIGNED 8-bit (0-255, center=128)
            # Convert to int16: subtract center, scale to 16-bit range
            arr = np.frombuffer(data, dtype=np.uint8).astype(np.int16)
            pcm_16 = (arr - 128) * 256  # center at 0, scale to ~±32768
            return pcm_16.astype(np.int16)
        except Exception:
            return None

    def write_audio(self, pcm_8k):
        """
        Write audio to the SIP call (outgoing RTP).
        Uses pyVoIP's RTP client directly with our own seq/ts management.
        """
        if not self.active_call or self.active_call.state != CallState.ANSWERED:
            return
        try:
            rtp = self.active_call.RTPClients[0]
            ulaw_payload = audioop.lin2ulaw(pcm_8k.astype(np.int16).tobytes(), 2)

            seq = getattr(rtp, '_our_seq', rtp.outSequence)
            ts = getattr(rtp, '_our_ts', rtp.outTimestamp)

            packet = b"\x80"
            packet += bytes([int(rtp.preference)])
            packet += (seq & 0xFFFF).to_bytes(2, byteorder="big")
            packet += (ts & 0xFFFFFFFF).to_bytes(4, byteorder="big")
            packet += rtp.outSSRC.to_bytes(4, byteorder="big")
            packet += ulaw_payload

            sock = getattr(rtp, '_real_sout', rtp.sout)
            sock.sendto(packet, (rtp.outIP, rtp.outPort))

            rtp._our_seq = (seq + 1) & 0xFFFF
            rtp._our_ts = (ts + 160) & 0xFFFFFFFF
        except Exception:
            pass

    def _stop_pyvoip_transmitter(self):
        """Kill pyVoIP's built-in RTP transmitter so our manual sends work cleanly."""
        try:
            import socket as _socket
            if self.active_call:
                for rtp in self.active_call.RTPClients:
                    if not hasattr(rtp, '_real_sout'):
                        rtp._real_sout = rtp.sout
                        rtp._our_seq = rtp.outSequence
                        rtp._our_ts = rtp.outTimestamp

                        # Replace sout with dummy so trans() doesn't send real packets
                        dummy = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
                        rtp.sout = dummy

                        # Slow down trans() so it barely runs
                        import time as _time
                        def _blocking_read(length=160, _t=_time):
                            _t.sleep(1.0)
                            return b'\xff' * length
                        rtp.pmout.read = _blocking_read
        except Exception as e:
            print(f"  [SIP] _stop_pyvoip_transmitter error: {e}")

    @property
    def call_state(self):
        """Current call state or None."""
        if self.active_call:
            return self.active_call.state
        return None

    def _on_incoming(self, call):
        """Internal callback from pyVoIP on incoming INVITE."""
        self.active_call = call
        print(f"  [SIP] Line {self.line_index+1}: incoming call")
        if self.incoming_callback:
            self.incoming_callback(self.line_index, self, call)
