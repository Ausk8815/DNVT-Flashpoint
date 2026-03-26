/**
 * DNVT USB Bridge DLL — handles all timing-critical work in native C++:
 *   - USB bulk I/O (read/write HOST_PACKET/DEVICE_PACKET)
 *   - CVSD decode/encode (streaming, stateful)
 *   - Resample 32kHz <-> 8kHz
 *   - Thread-safe ring buffers for PCM audio in/out
 *
 * Python calls simple functions to get/put 8kHz PCM and read phone state.
 */

#ifndef DNVT_BRIDGE_H
#define DNVT_BRIDGE_H

#include <stdint.h>

#ifdef _WIN32
    #define BRIDGE_API extern "C" __declspec(dllexport)
#else
    #define BRIDGE_API extern "C" __attribute__((visibility("default")))
#endif

#define NUM_PHONES 4

// Phone states (mapped, from firmware)
enum PhoneState : uint8_t {
    PHONE_IDLE          = 0,
    PHONE_DIAL          = 1,
    PHONE_TRAFFIC       = 2,
    PHONE_RING          = 3,
    PHONE_AWAIT_RING    = 4,
    PHONE_UNREACHABLE   = 5,
    PHONE_REQ_RING      = 6,
    PHONE_TRANSITION    = 7,
};

// Commands to send to firmware
enum PhoneCommand : uint8_t {
    CMD_NONE            = 0x00,
    CMD_RING            = 0x01,
    CMD_PLAINTEXT       = 0x02,
    CMD_DISCONNECT      = 0x03,
    CMD_RING_DISMISS    = 0x04,
};

// Per-phone status snapshot
struct PhoneStatus {
    uint8_t state;          // PhoneState
    char    digit;          // last digit received (0 if none)
    uint8_t raw_state;      // internal firmware state
    uint16_t rx_words;      // words received since last query
    uint16_t tx_words;      // words sent since last query
};

/**
 * Initialize the bridge. Opens USB device and starts the I/O thread.
 * Returns 0 on success, negative on error.
 */
BRIDGE_API int bridge_init(void);

/**
 * Shut down the bridge. Stops I/O thread and closes USB.
 */
BRIDGE_API void bridge_shutdown(void);

/**
 * Check if the bridge is running.
 */
BRIDGE_API int bridge_is_running(void);

/**
 * Get current status of all phones.
 * status: array of NUM_PHONES PhoneStatus structs (caller allocates)
 */
BRIDGE_API void bridge_get_status(PhoneStatus* status);

/**
 * Send a command to a phone.
 * phone: 0-3
 * cmd: PhoneCommand value
 */
BRIDGE_API void bridge_send_command(int phone, uint8_t cmd);

/**
 * Get decoded audio from a phone (DNVT -> host, 8kHz int16 PCM).
 * phone: 0-3
 * buf: output buffer (caller allocates)
 * max_samples: buffer capacity in samples
 * Returns number of samples written (0 if no data available).
 */
BRIDGE_API int bridge_get_audio_8k(int phone, int16_t* buf, int max_samples);

/**
 * Put audio to send to a phone (host -> DNVT, 8kHz int16 PCM).
 * phone: 0-3
 * buf: PCM samples to send
 * num_samples: number of samples
 * Returns number of samples accepted.
 */
BRIDGE_API int bridge_put_audio_8k(int phone, const int16_t* buf, int num_samples);

/**
 * Get the last digit dialed on a phone (and clear it).
 * Returns the digit character, or 0 if none.
 */
BRIDGE_API char bridge_get_digit(int phone);

/**
 * Clear audio buffers for a phone (call start/end).
 */
BRIDGE_API void bridge_clear_audio(int phone);

#endif // DNVT_BRIDGE_H
