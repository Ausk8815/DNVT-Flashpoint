"""
Call manager — orchestrates DNVT phone lines, SIP calls, and audio bridging.

Maps DNVT hardware states to SIP call states and manages the full call lifecycle
for all 4 phone lines. Called from the main USB loop each packet.
"""

import time
import threading
from enum import Enum

import numpy as np
from scipy.signal import resample
from pyVoIP.VoIP import CallState

from config import AppConfig
from sip_bridge import SipLine
from audio_bridge import AudioBridge
import usb_protocol as usb


class LineState(Enum):
    IDLE = "idle"
    DIALING = "dialing"          # collecting digits, no SIP call yet
    CALLING = "calling"          # SIP INVITE sent, waiting for answer
    RINGING_IN = "ringing_in"    # SIP incoming, RING sent to DNVT phone
    CONNECTED = "connected"      # SIP call active, audio bridging
    INTERCOM = "intercom"        # direct line-to-line, no SIP
    LOOPBACK = "loopback"        # local echo test — audio goes through codec chain and back
    HANGING_UP = "hanging_up"    # one side hung up, cleaning up


class RoutingMode(Enum):
    NONE = "none"           # no mode selected yet — waiting for mode key
    PRIORITY = "priority"   # P key — SIP calling
    IMMEDIATE = "immediate" # I key — intercom (direct line-to-line)
    FLASH = "flash"         # F key — (future)
    FLASH_OVERRIDE = "fo"   # O key — (future)

# Map DNVT AUTOVON keys to routing modes
MODE_KEYS = {
    'P': RoutingMode.PRIORITY,     # SIP
    'I': RoutingMode.IMMEDIATE,    # Intercom
    'F': RoutingMode.FLASH,
    'O': RoutingMode.FLASH_OVERRIDE,
}


class CallManager:
    """
    Manages SIP integration for all 4 DNVT phone lines.

    Called from the main USB loop on each HOST_PACKET to process state changes,
    digits, and audio. Returns pending DEVICE_PACKET data (commands + audio words).
    """

    def __init__(self, config: AppConfig):
        self.config = config

        # Per-line state
        self.line_states = [LineState.IDLE] * 4
        self.dialed_digits = [""] * 4
        self.digit_timers = [None] * 4   # time of last digit
        self.routing_modes = [RoutingMode.NONE] * 4
        self.prev_hw_states = [None] * 4
        self.intercom_peer = [None] * 4  # index of connected peer line
        self._ring_traffic_count = [0] * 4  # consecutive traffic-state packets during ring

        # SIP lines
        self.sip_lines = []
        for i in range(4):
            sip = SipLine(i, config.lines[i], incoming_callback=self._on_sip_incoming)
            self.sip_lines.append(sip)

        # Audio bridges
        self.bridges = [AudioBridge() for _ in range(4)]

        # Pending commands to send to device
        self._pending_commands = {}

        # Intercom: pending audio words to send to each line
        self._intercom_tx = [[] for _ in range(4)]

        # Audio threads for SIP I/O (separate for inbound/outbound)
        self._sip_rx_thread = None
        self._sip_tx_thread = None
        self._running = False

    def start(self):
        """Start SIP registration for all enabled lines and audio thread."""
        for sip in self.sip_lines:
            sip.start()

        self._running = True
        self._sip_rx_thread = threading.Thread(target=self._sip_rx_loop, daemon=True)
        self._sip_rx_thread.start()
        self._sip_tx_thread = threading.Thread(target=self._sip_tx_loop, daemon=True)
        self._sip_tx_thread.start()

    def stop(self):
        """Unregister all SIP lines and stop audio thread."""
        self._running = False
        if self._sip_rx_thread:
            self._sip_rx_thread.join(timeout=2.0)
        if self._sip_tx_thread:
            self._sip_tx_thread.join(timeout=2.0)
        for sip in self.sip_lines:
            sip.stop()

    def process_packet(self, states, data_lens, phone_data, digits):
        """
        Process one HOST_PACKET's worth of data.
        Called from the main USB loop.

        Args:
            states:     list of 4 mapped phone states (0-7)
            data_lens:  list of 4 data word counts
            phone_data: list of 4 lists of uint32 audio words
            digits:     list of 4 (char or None)
        """
        for p in range(4):
            hw_state = states[p]
            hw_prev = self.prev_hw_states[p]

            # State transition detection
            if hw_state != hw_prev:
                self._on_hw_state_change(p, hw_prev, hw_state)
                self.prev_hw_states[p] = hw_state

            # During RINGING_IN, count stable traffic packets to detect real pickup
            if self.line_states[p] == LineState.RINGING_IN:
                if hw_state == 2:
                    self._ring_traffic_count[p] += 1
                    if self._ring_traffic_count[p] == 50:
                        self.sip_lines[p].answer_call()
                        self._pending_commands[p] = usb.PLAINTEXT_COMMAND
                        self.line_states[p] = LineState.CONNECTED
                        self._ring_traffic_count[p] = 0
                        print(f"  [{self._ts()}] Phone {p+1}: SIP call answered (stable traffic)")
                else:
                    self._ring_traffic_count[p] = 0

            # Digit handling
            if digits[p] is not None:
                self._on_digit(p, digits[p])

            # Feed DNVT audio to bridge (for SIP outbound)
            if self.line_states[p] == LineState.CONNECTED and data_lens[p] > 0:
                words = [w for w in phone_data[p] if w != usb.NULL_AUDIO]
                if words:
                    self.bridges[p].feed_dnvt_audio(words)

            # Loopback: decode → resample down → pitch shift → resample up → encode → back
            if self.line_states[p] == LineState.LOOPBACK and data_lens[p] > 0:
                words = [w for w in phone_data[p] if w != usb.NULL_AUDIO]
                if words:
                    self.bridges[p].feed_dnvt_audio(words)
                    # Take from outbound queue, pitch-shift, feed to inbound
                    pcm_8k = self.bridges[p].get_sip_audio(max_samples=1000)
                    if pcm_8k is not None and len(pcm_8k) > 0:
                        # Pitch shift up ~50% by resampling: fewer samples = higher pitch
                        shifted = resample(pcm_8k.astype(np.float64), int(len(pcm_8k) * 0.67))
                        shifted = np.clip(shifted, -32768, 32767).astype(np.int16)
                        self.bridges[p].feed_sip_audio(shifted)

            # Intercom: route audio words directly to peer line
            if self.line_states[p] == LineState.INTERCOM and data_lens[p] > 0:
                peer = self.intercom_peer[p]
                if peer is not None:
                    words = [w for w in phone_data[p] if w != usb.NULL_AUDIO]
                    if words:
                        self._intercom_tx[peer].extend(words)

            # Check dial timeout
            if self.line_states[p] == LineState.DIALING:
                self._check_dial_timeout(p)

            # Check SIP call state changes
            self._check_sip_state(p)

    def get_device_data(self, data_lens=None):
        """
        Get pending commands and audio words to send via DEVICE_PACKET.
        Rate-matched: send exactly data_lens[p] words per phone to match
        the PIO TX/RX lockstep rate.

        Args:
            data_lens: list of 4 ints — how many RX words we got per phone this cycle

        Returns:
            (commands, audio_data) where:
            commands:   dict of {phone_idx: cmd_byte} or None
            audio_data: dict of {phone_idx: [uint32 words]} or None
        """
        if data_lens is None:
            data_lens = [0] * 4

        commands = self._pending_commands.copy() if self._pending_commands else None
        self._pending_commands.clear()

        audio_data = {}
        for p in range(4):
            n = data_lens[p]  # match RX rate

            # SIP or loopback audio — always send at least 1 word when connected
            # (before PTT, phone sends no RX data but still needs TX to hear SIP)
            if self.line_states[p] in (LineState.CONNECTED, LineState.LOOPBACK):
                want = max(n, 1)  # at least 1 word per cycle
                words = self.bridges[p].get_dnvt_words(max_words=want)
                if words:
                    audio_data[p] = words
            # Intercom audio
            elif self.line_states[p] == LineState.INTERCOM and n > 0:
                if self._intercom_tx[p]:
                    audio_data[p] = self._intercom_tx[p][:n]
                    self._intercom_tx[p] = self._intercom_tx[p][n:]

        return commands, audio_data if audio_data else None

    # ---- Internal state handling ----

    def _ts(self):
        """Timestamp for log lines."""
        return time.strftime("%H:%M:%S")

    def _on_hw_state_change(self, p, old_state, new_state):
        """Handle DNVT phone hardware state change."""
        # During RINGING_IN, log state changes but don't answer
        # (answering is handled by stable-traffic counter in process_packet)
        if self.line_states[p] == LineState.RINGING_IN:
            if new_state == 0:  # back to idle — ring failed
                print(f"  [{self._ts()}] Phone {p+1}: ring failed, back to idle")
                self._ring_traffic_count[p] = 0
                self._hangup_line(p)
            else:
                print(f"  [{self._ts()}] Phone {p+1}: ringing hw={old_state}->{new_state}")
            return

        # Phone went off-hook
        if old_state == 0 and new_state != 0:
            print(f"  [{self._ts()}] Phone {p+1}: OFF HOOK")
            # Answer incoming intercom call
            caller = self._find_intercom_caller(p)
            if caller is not None:
                self.intercom_peer[p] = caller
                self.line_states[p] = LineState.INTERCOM
                self.line_states[caller] = LineState.INTERCOM
                self.routing_modes[p] = RoutingMode.IMMEDIATE
                self._pending_commands[p] = usb.PLAINTEXT_COMMAND
                print(f"  [{self._ts()}] Phone {p+1}: intercom answered (connected to Line {caller+1})")
                return

        # Phone went on-hook
        if old_state is not None and old_state != 0 and new_state == 0:
            dialed = self.dialed_digits[p]
            if dialed:
                print(f"  [{self._ts()}] Phone {p+1}: ON HOOK (dialed: {dialed})")
            else:
                print(f"  [{self._ts()}] Phone {p+1}: ON HOOK")
            self._hangup_line(p)
            return

        # Phone entered dial state
        if new_state == 1 and self.line_states[p] == LineState.IDLE:
            self.line_states[p] = LineState.DIALING
            self.dialed_digits[p] = ""
            self.digit_timers[p] = None
            self.routing_modes[p] = RoutingMode.NONE
            print(f"  [{self._ts()}] Phone {p+1}: dial number, then press: P=Priority(SIP)  I=Immediate(Intercom)  F=Flash  O=Flash Override")
            return

        # Phone entered traffic (might be from PLAINTEXT_COMMAND)
        if new_state == 2 and self.line_states[p] == LineState.CALLING:
            # SIP was answered and we sent PLAINTEXT_COMMAND
            self.line_states[p] = LineState.CONNECTED
            print(f"  [{self._ts()}] Phone {p+1}: connected — audio bridge active")
            return

        # Log other transitions
        if old_state is not None:
            print(f"  [{self._ts()}] Phone {p+1}: {usb.state_name(old_state)} -> {usb.state_name(new_state)}")

    def _on_digit(self, p, digit):
        """Handle a dialed digit from the DNVT phone.

        Dial number first, then press mode key (P/I/F/O) to act on it.
        R maps to #, C maps to * (only applied when mode is selected).
        No timeouts — nothing happens until a mode key is pressed.
        """
        # Mode key pressed — act on the accumulated number
        if digit in MODE_KEYS:
            self.routing_modes[p] = MODE_KEYS[digit]
            number = self.dialed_digits[p]

            # Apply remaps now that we know the mode
            if self.routing_modes[p] == RoutingMode.PRIORITY:
                number = number.replace('R', '#').replace('C', '*')

            print(f"  [{self._ts()}] Phone {p+1}: {self.routing_modes[p].value} -> {number}")

            if number:
                self._dispatch_call(p, number)
            else:
                print(f"  [{self._ts()}] Phone {p+1}: no number dialed")
            return

        # Accumulate digits (R and C stored raw, remapped when mode is chosen)
        self.dialed_digits[p] += digit
        print(f"  [{self._ts()}] Phone {p+1}: digit '{digit}'  (number so far: {self.dialed_digits[p]})")

    def _check_dial_timeout(self, p):
        """No-op — we don't use timeouts, mode key triggers the call."""
        pass

    def _dispatch_call(self, p, number):
        """Route a call based on the current routing mode."""
        mode = self.routing_modes[p]
        if mode == RoutingMode.PRIORITY:
            if number == '0':
                self._initiate_loopback(p)       # Local echo test
                return
            self._initiate_call(p, number)       # SIP
        elif mode == RoutingMode.IMMEDIATE:
            self._initiate_intercom(p, number)    # direct line-to-line
        elif mode == RoutingMode.FLASH:
            print(f"  [{self._ts()}] Phone {p+1}: FLASH mode not implemented yet")
        elif mode == RoutingMode.FLASH_OVERRIDE:
            print(f"  [{self._ts()}] Phone {p+1}: FLASH OVERRIDE mode not implemented yet")
        else:
            print(f"  [{self._ts()}] Phone {p+1}: no mode selected")

    def _initiate_loopback(self, p):
        """Start local loopback echo test — audio goes through full codec chain and back."""
        print(f"  [{self._ts()}] Phone {p+1}: LOOPBACK echo test (0P)")
        self.line_states[p] = LineState.LOOPBACK
        self.bridges[p].reset()
        self._pending_commands[p] = usb.PLAINTEXT_COMMAND

    def _initiate_call(self, p, number):
        """Start a SIP call for the given number."""
        if not self.sip_lines[p].config.enabled:
            print(f"  [{self._ts()}] Phone {p+1}: SIP not enabled for this line")
            return
        if not self.sip_lines[p].registered:
            print(f"  [{self._ts()}] Phone {p+1}: SIP not registered, can't call {number}")
            return

        print(f"  [{self._ts()}] Phone {p+1}: dialing SIP {number}")
        self.line_states[p] = LineState.CALLING
        self.bridges[p].reset()

        call = self.sip_lines[p].make_call(number)
        if not call:
            self.line_states[p] = LineState.IDLE
            return

        # Send PLAINTEXT to enter traffic mode immediately
        # (so we can start receiving/sending audio)
        self._pending_commands[p] = usb.PLAINTEXT_COMMAND

    def _initiate_intercom(self, p, number):
        """Connect two DNVT lines directly (no SIP). number is target line 1-4."""
        try:
            target = int(number) - 1  # convert to 0-indexed
        except ValueError:
            print(f"  [{self._ts()}] Phone {p+1}: invalid intercom target '{number}'")
            return

        if target < 0 or target > 3:
            print(f"  [{self._ts()}] Phone {p+1}: intercom line {number} out of range (1-4)")
            return
        if target == p:
            print(f"  [{self._ts()}] Phone {p+1}: can't intercom yourself")
            return

        # Ring the target phone
        print(f"  [{self._ts()}] Phone {p+1}: intercom -> Line {target+1}")
        self.line_states[p] = LineState.CALLING
        self.intercom_peer[p] = target

        # Send RING to target, PLAINTEXT to caller (enter traffic)
        self._pending_commands[target] = usb.RING_COMMAND
        self._pending_commands[p] = usb.PLAINTEXT_COMMAND

    def _check_sip_state(self, p):
        """Check SIP call state and react."""
        sip = self.sip_lines[p]
        call_state = sip.call_state

        if call_state is None:
            return

        # SIP call was answered (outbound)
        if self.line_states[p] == LineState.CALLING and call_state == CallState.ANSWERED:
            self.line_states[p] = LineState.CONNECTED
            self._pending_commands[p] = usb.PLAINTEXT_COMMAND
            print(f"  [{self._ts()}] Phone {p+1}: SIP answered — connecting audio")

        # SIP call ended (remote hangup)
        if call_state == CallState.ENDED and self.line_states[p] in (
            LineState.CONNECTED, LineState.CALLING, LineState.RINGING_IN
        ):
            print(f"  [{self._ts()}] Phone {p+1}: SIP call ended (remote)")
            self._hangup_line(p)

    def _find_intercom_caller(self, p):
        """Find which line is calling line p via intercom."""
        for i in range(4):
            if i != p and self.intercom_peer[i] == p and self.line_states[i] in (LineState.CALLING, LineState.INTERCOM):
                return i
        return None

    def _hangup_line(self, p):
        """Clean up a call on line p."""
        if self.line_states[p] != LineState.IDLE:
            # Disconnect intercom peer
            peer = self.intercom_peer[p]
            if peer is not None and self.line_states[peer] == LineState.INTERCOM:
                self.intercom_peer[peer] = None
                self.line_states[peer] = LineState.IDLE
                self.routing_modes[peer] = RoutingMode.NONE
                self._intercom_tx[peer].clear()
                print(f"  [{self._ts()}] Phone {peer+1}: intercom peer hung up")
            self.intercom_peer[p] = None
            self._intercom_tx[p].clear()

            self.sip_lines[p].hangup()
            self.bridges[p].reset()
            self.line_states[p] = LineState.IDLE
            self.dialed_digits[p] = ""
            self.digit_timers[p] = None
            self.routing_modes[p] = RoutingMode.NONE

    def _on_sip_incoming(self, line_index, sip_line, call):
        """Callback from SipLine when an incoming SIP INVITE arrives."""
        if self.line_states[line_index] != LineState.IDLE:
            print(f"  [{self._ts()}] Phone {line_index+1}: incoming SIP call rejected (line busy)")
            try:
                call.deny()
            except Exception:
                pass
            return

        print(f"  [{self._ts()}] Phone {line_index+1}: incoming SIP call — ringing (hw={self.prev_hw_states[line_index]})")
        self.line_states[line_index] = LineState.RINGING_IN
        self.bridges[line_index].reset()
        self._pending_commands[line_index] = usb.RING_COMMAND

    # ---- Audio I/O threads ----

    def _sip_rx_loop(self):
        """Background thread: read audio from SIP and feed to bridge.

        Non-blocking read with tight polling. Separate from TX thread.
        """
        sip_rx_count = 0
        dbg_counter = 0
        while self._running:
            any_active = False
            got_data = False
            for p in range(4):
                if self.line_states[p] != LineState.CONNECTED:
                    continue
                any_active = True
                sip = self.sip_lines[p]
                bridge = self.bridges[p]

                # Read multiple frames per cycle to drain pyVoIP's buffer
                for _ in range(5):
                    pcm = sip.read_audio()
                    if pcm is not None and len(pcm) > 0:
                        sip_rx_count += len(pcm)
                        bridge.feed_sip_audio(pcm)
                        got_data = True
                    else:
                        break

            if any_active:
                dbg_counter += 1
                if dbg_counter % 500 == 0:
                    print(f"  [{self._ts()}] [audio] sip_rx={sip_rx_count}")
                    sip_rx_count = 0
                if not got_data:
                    time.sleep(0.005)  # 5ms poll when no data (reduce GIL pressure)
            else:
                time.sleep(0.05)

    def _sip_tx_loop(self):
        """Background thread: send complete 160-sample frames to SIP as they become available."""
        sip_tx_count = 0
        dbg_counter = 0
        while self._running:
            any_active = False
            did_work = False
            for p in range(4):
                if self.line_states[p] != LineState.CONNECTED:
                    continue
                any_active = True
                bridge = self.bridges[p]
                sip = self.sip_lines[p]

                # Send all available complete frames
                for _ in range(10):  # max 10 frames per cycle to avoid starvation
                    out_pcm = bridge.get_sip_audio(frame_size=160)
                    if out_pcm is None:
                        break
                    sip_tx_count += len(out_pcm)
                    sip.write_audio(out_pcm)
                    did_work = True

            if any_active:
                dbg_counter += 1
                if dbg_counter % 500 == 0:
                    print(f"  [{self._ts()}] [audio] sip_tx={sip_tx_count}")
                    sip_tx_count = 0
                if not did_work:
                    time.sleep(0.002)
            else:
                time.sleep(0.05)
