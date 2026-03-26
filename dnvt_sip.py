"""
DNVT SIP Bridge — uses native C++ DLL for USB I/O and audio processing.

All timing-critical work (USB read/write, CVSD decode/encode, resampling)
runs in C++ at native speed. Python handles SIP signaling and state logic.
"""

import sys
import time
import threading
import audioop
import numpy as np
import sounddevice as sd

import dnvt_bridge_py as bridge
from config import load_config
from sip_bridge import SipLine
from pyVoIP.VoIP import CallState

# ============================================================================
# State management
# ============================================================================

class LineState:
    IDLE = "idle"
    DIALING = "dialing"
    CALLING = "calling"
    RINGING_IN = "ringing_in"
    CONNECTED = "connected"
    INTERCOM = "intercom"
    SPEAKER_TEST = "speaker_test"  # dial 0 — play phone audio on PC speakers

MODE_KEYS = {'P': 'priority', 'I': 'immediate', 'F': 'flash', 'O': 'flash_override'}

# Tone IDs
TONE_DIAL = 0
TONE_RINGBACK = 1
TONE_REORDER = 2
tones_loaded = {}


class LineManager:
    def __init__(self, index, config, sip_line):
        self.index = index
        self.config = config
        self.sip = sip_line
        self.state = LineState.IDLE
        self.mode = None
        self.dialed = ""
        self.prev_hw = None
        self.ring_traffic_count = 0
        self.sip_mode = False          # True after pressing C (SIP prefix)
        self.last_sip_number = None    # for R=redial

    def ts(self):
        return time.strftime("%H:%M:%S")


def main():
    config = load_config()

    # Init native bridge
    rc = bridge.init()
    if rc != 0:
        print(f"Bridge init failed: {rc}")
        sys.exit(1)

    import os

    # Init SIP lines
    lines = []
    for i in range(4):
        sip = SipLine(i, config.lines[i], incoming_callback=None)
        lm = LineManager(i, config.lines[i], sip)
        lines.append(lm)

    # Set incoming callback after lines are created
    for lm in lines:
        lm.sip.incoming_callback = lambda idx, sip, call, _lm=lm: on_sip_incoming(_lm, lines)

    # Start SIP registration
    for lm in lines:
        lm.sip.start()

    # Single SIP audio thread — avoids GIL contention between RX and TX
    running = True

    def sip_audio_loop():
        while running:
            any_active = False
            for lm in lines:
                if lm.state != LineState.CONNECTED:
                    continue
                any_active = True

                # RX: SIP -> DLL (multiple frames per cycle to drain pyVoIP buffer)
                for _ in range(5):
                    pcm = lm.sip.read_audio()
                    if pcm is not None and len(pcm) > 0:
                        bridge.put_audio_8k(lm.index, pcm)
                    else:
                        break

                # TX: DLL -> SIP — accumulate until we have a full 160-sample frame
                if not hasattr(lm, '_tx_buf'):
                    lm._tx_buf = np.array([], dtype=np.int16)
                    lm._tx_frames_sent = 0

                # Pull only what we need — enough for one frame plus a little headroom
                need = 160 - len(lm._tx_buf)
                if need > 0:
                    pcm = bridge.get_audio_8k(lm.index, max_samples=need)
                    if pcm is not None and len(pcm) > 0:
                        lm._tx_buf = np.concatenate([lm._tx_buf, pcm])

                # Send one frame if ready
                if len(lm._tx_buf) >= 160:
                    frame = lm._tx_buf[:160]
                    lm._tx_buf = lm._tx_buf[160:]
                    lm.sip.write_audio(frame)
                    lm._tx_frames_sent += 1
                    # Record to WAV if active
                    if hasattr(lm, '_sip_wav') and lm._sip_wav:
                        lm._sip_wav.writeframes(frame.astype(np.int16).tobytes())

                # Debug
                if lm._tx_frames_sent > 0 and lm._tx_frames_sent % 150 == 0:
                    print(f"  [sip_tx] frames={lm._tx_frames_sent} buf={len(lm._tx_buf)}")

            if not any_active:
                time.sleep(0.05)
            else:
                time.sleep(0.001)

    audio_thread = threading.Thread(target=sip_audio_loop, daemon=True)
    audio_thread.start()

    print("DNVT SIP Bridge running... (Ctrl+C to quit)")
    for lm in lines:
        if lm.config.enabled:
            reg = "R" if lm.sip.registered else "-"
            print(f"  Line {lm.index+1}: {lm.config.username}@{lm.config.sip_server} ({lm.config.display_name})")
    print("-" * 60)

    last_status_time = time.time()
    last_statuses = [None] * 4

    try:
        while True:
            time.sleep(0.010)  # 10ms poll — all USB work is in the DLL

            if not bridge.is_running():
                print("Bridge stopped unexpectedly!")
                break

            # Poll phone status
            statuses = bridge.get_status()

            for lm in lines:
                p = lm.index
                st = statuses[p]
                hw = st.state

                # State change
                if hw != lm.prev_hw:
                    on_hw_change(lm, lm.prev_hw, hw, lines)
                    lm.prev_hw = hw

                # Digit
                digit = bridge.get_digit(p)
                if digit:
                    on_digit(lm, digit, lines)

                # Ringing: count stable traffic for answer detection
                if lm.state == LineState.RINGING_IN:
                    if hw == bridge.STATE_TRAFFIC:
                        lm.ring_traffic_count += 1
                        if lm.ring_traffic_count >= 5:  # ~50ms of stable traffic
                            bridge.clear_audio(p)  # flush stale data
                            lm.sip.answer_call()
                            bridge.send_command(p, bridge.CMD_PLAINTEXT)
                            lm.state = LineState.CONNECTED
                            lm.ring_traffic_count = 0
                            print(f"  [{lm.ts()}] Phone {p+1}: SIP call answered")
                    else:
                        lm.ring_traffic_count = 0

                # Intercom: check if target entered traffic (picked up)
                if lm.state == LineState.INTERCOM and hasattr(lm, 'intercom_target'):
                    target_idx = lm.intercom_target
                    target_st = statuses[target_idx]
                    if target_st.state == bridge.STATE_TRAFFIC:
                        print(f"  [{lm.ts()}] Phone {p+1}: intercom connected to line {target_idx+1}")
                        bridge.set_intercom(p, target_idx)

                # Intercom: disconnect on hangup (either side)
                if lm.state == LineState.INTERCOM and hw == 0:
                    if hasattr(lm, 'intercom_target'):
                        target_idx = lm.intercom_target
                        bridge.set_intercom(p, -1)
                        print(f"  [{lm.ts()}] Phone {p+1}: intercom disconnected")
                        # Reset target line too
                        if target_idx < len(lines):
                            lines[target_idx].state = LineState.IDLE

                # Check SIP call ended
                if lm.sip.call_state == CallState.ENDED and lm.state in (
                    LineState.CONNECTED, LineState.CALLING, LineState.RINGING_IN
                ):
                    print(f"  [{lm.ts()}] Phone {p+1}: SIP call ended (remote)")
                    hangup_line(lm)

                # Check SIP answered (outbound)
                if lm.state == LineState.CALLING and lm.sip.call_state == CallState.ANSWERED:
                    lm.state = LineState.CONNECTED
                    bridge.clear_audio(p)  # flush stale data
                    bridge.send_command(p, bridge.CMD_PLAINTEXT)
                    lm.sip._stop_pyvoip_transmitter()
                    # Record outbound audio to WAV for debugging
                    import wave as _wave
                    lm._sip_wav = _wave.open('sip_call_tx.wav', 'w')
                    lm._sip_wav.setnchannels(1)
                    lm._sip_wav.setsampwidth(2)
                    lm._sip_wav.setframerate(8000)
                    print(f"  [{lm.ts()}] Phone {p+1}: SIP answered — audio bridge active (recording sip_call_tx.wav)")

                    # Speaker test: record to WAV
                if lm.state == LineState.SPEAKER_TEST:
                    pcm = bridge.get_audio_8k(p, max_samples=960)
                    if pcm is not None and len(pcm) > 0 and hasattr(lm, '_test_wav') and lm._test_wav:
                        if not hasattr(lm, '_wav_dbg_count'):
                            lm._wav_dbg_count = 0
                        lm._wav_dbg_count += 1
                        if lm._wav_dbg_count <= 30:
                            nz = int(np.count_nonzero(pcm))
                            print(f"  [wav] read {len(pcm)} samples, {nz} nonzero, rms={int(np.sqrt(np.mean(pcm.astype(float)**2)))}")
                        lm._test_wav.writeframes(pcm.astype(np.int16).tobytes())

            # Periodic status
            now = time.time()
            if now - last_status_time >= 10.0:
                parts = []
                for lm in lines:
                    s = statuses[lm.index]
                    reg = "R" if lm.sip.registered else "-"
                    parts.append(f"{lm.index+1}:{lm.state}({reg})")
                rx_list = [str(statuses[i].rx_words) for i in range(4)]
                tx_list = [str(statuses[i].tx_words) for i in range(4)]
                print(f"  [status] lines=[{' '.join(parts)}] rx=[{','.join(rx_list)}] tx=[{','.join(tx_list)}]")
                last_status_time = now

    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        running = False
        for lm in lines:
            lm.sip.stop()
        bridge.shutdown()


# ============================================================================
# Event handlers
# ============================================================================

def on_hw_change(lm, old, new, lines):
    p = lm.index

    # During ringing, just log
    if lm.state == LineState.RINGING_IN:
        if new == 0:
            print(f"  [{lm.ts()}] Phone {p+1}: ring failed")
            hangup_line(lm)
        return

    # Off hook
    if old == 0 and new != 0 and old is not None:
        print(f"  [{lm.ts()}] Phone {p+1}: OFF HOOK")

    # On hook
    if old is not None and old != 0 and new == 0:
        print(f"  [{lm.ts()}] Phone {p+1}: ON HOOK" +
              (f" (dialed: {lm.dialed})" if lm.dialed else ""))
        hangup_line(lm)
        return

    # Enter dial
    if new == bridge.STATE_DIAL and lm.state == LineState.IDLE:
        lm.state = LineState.DIALING
        lm.dialed = ""
        lm.mode = None
        print(f"  [{lm.ts()}] Phone {p+1}: dial, then press: P=SIP  I=Intercom  F=Flash  O=Override")

    # Enter traffic from calling
    if new == bridge.STATE_TRAFFIC and lm.state == LineState.CALLING:
        lm.state = LineState.CONNECTED
        bridge.clear_audio(lm.index)
        lm.sip._stop_pyvoip_transmitter()
        # Record outbound audio to WAV for debugging
        import wave as _wave
        lm._sip_wav = _wave.open('sip_call_tx.wav', 'w')
        lm._sip_wav.setnchannels(1)
        lm._sip_wav.setsampwidth(2)
        lm._sip_wav.setframerate(8000)
        print(f"  [{lm.ts()}] Phone {p+1}: connected (recording sip_call_tx.wav)")


def on_digit(lm, digit, lines):
    p = lm.index

    # P = enter SIP mode
    if digit == 'P' and not lm.sip_mode:
        lm.sip_mode = True
        lm.dialed = ""
        print(f"  [{lm.ts()}] Phone {p+1}: SIP mode — dial number then C to send")
        return

    # C in SIP mode: if digits already entered = send, otherwise = * prefix
    if digit == 'C':
        if lm.sip_mode:
            if lm.dialed:
                # Send the call
                number = lm.dialed.replace('R', '#')
                print(f"  [{lm.ts()}] Phone {p+1}: SIP call -> {number}")
                lm.last_sip_number = number
                make_sip_call(lm, number)
                return
            else:
                # No digits yet — C = * prefix
                lm.dialed += '*'
                print(f"  [{lm.ts()}] Phone {p+1}: SIP '*' (number: *)")
                return
        # C outside SIP mode — ignore or future use
        return

    # R = redial last SIP number (outside SIP mode)
    if digit == 'R' and not lm.sip_mode:
        if lm.last_sip_number:
            print(f"  [{lm.ts()}] Phone {p+1}: redial -> {lm.last_sip_number}")
            make_sip_call(lm, lm.last_sip_number)
        else:
            print(f"  [{lm.ts()}] Phone {p+1}: nothing to redial")
        return

    # Reserved keys
    if digit in ('I', 'F', 'O'):
        print(f"  [{lm.ts()}] Phone {p+1}: {digit} (reserved)")
        return

    # In SIP mode, accumulate digits (R = # in SIP)
    if lm.sip_mode:
        lm.dialed += digit
        display = lm.dialed.replace('R', '#')
        print(f"  [{lm.ts()}] Phone {p+1}: SIP '{digit}' (number: {display})")
        return

    # 0 = operator (speaker test / direct traffic)
    if digit == '0':
        print(f"  [{lm.ts()}] Phone {p+1}: OPERATOR — direct traffic")
        bridge.send_command(p, bridge.CMD_PLAINTEXT)
        lm.state = LineState.SPEAKER_TEST
        # Open WAV for recording
        import wave as _wave
        lm._test_wav = _wave.open('speaker_test.wav', 'w')
        lm._test_wav.setnchannels(1)
        lm._test_wav.setsampwidth(2)
        lm._test_wav.setframerate(8000)
        lm._wav_dbg_count = 0
        return

    # Single digit 1-9 = instant local line call (intercom)
    if digit in '123456789':
        target = int(digit)
        if target < 1 or target > 4:
            print(f"  [{lm.ts()}] Phone {p+1}: invalid line {target}")
            return
        target_idx = target - 1  # 0-indexed
        if target_idx == p:
            print(f"  [{lm.ts()}] Phone {p+1}: can't call yourself")
            return
        print(f"  [{lm.ts()}] Phone {p+1}: intercom -> line {target}")
        # Put both phones into traffic
        bridge.send_command(p, bridge.CMD_PLAINTEXT)
        bridge.send_command(target_idx, bridge.CMD_RING)
        lm.state = LineState.INTERCOM
        lm.intercom_target = target_idx
        # Intercom bridge will be set up when target answers (enters traffic)
        return

    lm.dialed += digit
    print(f"  [{lm.ts()}] Phone {p+1}: digit '{digit}'  (so far: {lm.dialed})")


def make_sip_call(lm, number):
    p = lm.index
    if not lm.sip.registered:
        print(f"  [{lm.ts()}] Phone {p+1}: SIP not registered")
        return
    lm.state = LineState.CALLING
    lm.sip.make_call(number)
    bridge.send_command(p, bridge.CMD_PLAINTEXT)


def dispatch_call(lm, number, lines):
    p = lm.index
    if lm.mode == 'priority':
        if number == '0':
            # Speaker test mode — play phone audio on PC speakers using callback
            print(f"  [{lm.ts()}] Phone {p+1}: SPEAKER TEST (0P) — recording to speaker_test.wav")
            lm.state = LineState.SPEAKER_TEST
            import wave as _wave
            lm._test_wav = _wave.open('speaker_test.wav', 'w')
            lm._test_wav.setnchannels(1)
            lm._test_wav.setsampwidth(2)
            lm._test_wav.setframerate(8000)
            bridge.clear_audio(p)
            bridge.send_command(p, bridge.CMD_PLAINTEXT)
            return
        if not lm.sip.registered:
            print(f"  [{lm.ts()}] Phone {p+1}: SIP not registered")
            return
        print(f"  [{lm.ts()}] Phone {p+1}: dialing SIP {number}")
        lm.state = LineState.CALLING
        lm.sip.make_call(number)
        bridge.send_command(p, bridge.CMD_PLAINTEXT)
    elif lm.mode == 'immediate':
        print(f"  [{lm.ts()}] Phone {p+1}: intercom not yet implemented in DLL mode")
    else:
        print(f"  [{lm.ts()}] Phone {p+1}: mode {lm.mode} not implemented")


def hangup_line(lm):
    if lm.state != LineState.IDLE:
        # Close test WAV if active
        if hasattr(lm, '_test_wav') and lm._test_wav:
            try:
                lm._test_wav.close()
                print(f"  Saved speaker_test.wav")
            except:
                pass
            lm._test_wav = None
        if hasattr(lm, '_sip_wav') and lm._sip_wav:
            try:
                lm._sip_wav.close()
                print(f"  Saved sip_call_tx.wav")
            except:
                pass
            lm._sip_wav = None
        lm.sip.hangup()
        lm.state = LineState.IDLE
        lm.dialed = ""
        lm.mode = None
        lm.sip_mode = False
        lm.ring_traffic_count = 0
        if hasattr(lm, '_tx_buf'):
            lm._tx_buf = np.array([], dtype=np.int16)


def on_sip_incoming(lm, lines):
    p = lm.index
    if lm.state != LineState.IDLE:
        print(f"  [{lm.ts()}] Phone {p+1}: incoming SIP rejected (busy)")
        try:
            lm.sip.active_call.deny()
        except:
            pass
        return
    print(f"  [{lm.ts()}] Phone {p+1}: incoming SIP call — ringing")
    lm.state = LineState.RINGING_IN
    lm.ring_traffic_count = 0
    bridge.send_command(p, bridge.CMD_RING)


if __name__ == "__main__":
    main()
